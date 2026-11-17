"""Read recent Codex threads and Claude Code sessions without writing to them.

A thread is readable only when its recorded working directory belongs to the same git repository as the request,
worktrees included, and only agent messages come back: never prompts, tool calls, tool output or reasoning. The listing
only picks a thread; the read opens its file once and decides again, from the bytes it returns messages from, whether
the thread belongs to the repository, and refuses a file that changes while it is read. Both sources
read the apps' session files. They open files read-only, never open a database or a lock file, and start no process
except git. When a file looks different from what the parser knows, the reader refuses it with a named reason instead
of guessing. The Codex source sits behind ThreadSource so an app-server source can replace it, under the conditions
in docs/adr/0001-codex-thread-reader-source.md at the repository root.

This module does the file and git access; thread_parsing.py holds the rules it applies to what it reads.
"""

import io
import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from functools import cache
from itertools import islice
from pathlib import Path
from typing import Callable, Protocol

from consultation import git_environment
from thread_parsing import (
    GIT_LOCATION_ENV, NOT_IN_REPOSITORY, ThreadListing, ThreadSnapshot, ThreadSummary, ThreadUnavailable, claude_head,
    claude_last_cwd, claude_snapshot, codex_main_thread, codex_snapshot, complete_records,
    git_output_path, is_interactive_claude_session, is_supported_history, listing_result, match_thread, newest_first,
    parse_line, parse_session_index, rollout_format, rollout_identifier, scan_claude, scan_codex, scan_is_enough,
    snapshot_result, UUID_PATTERN,
)

__all__ = [
    "GIT_LOCATION_ENV", "ClaudeSessionsSource", "CodexFilesSource", "ThreadReader", "ThreadSource",
    "ThreadUnavailable", "git_common_dir",
]

INITIAL_TAIL_BYTES = 256 * 1024
MAX_TAIL_BYTES = 32 * 1024 * 1024
# A Codex session_meta line carries the base instructions, so the first line can be large.
MAX_FIRST_LINE_BYTES = 4 * 1024 * 1024
MAX_HEAD_LINES = 50
COMPRESSED_SLICE_BYTES = 1024
# A rollout whose content is larger than this is refused: decompressing it whole would take too long.
MAX_DECOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
# Title and index lines are short; a longer one is skipped rather than read whole.
MAX_INDEX_LINE_BYTES = 64 * 1024
# What must stay the same while a history file is read for it to count as the same, unchanged file.
IDENTITY_FIELDS = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
CHANGED_WHILE_READ = "The thread's history file changed while it was read."


def open_history(path, root=None):
    """Open a regular history file without following symlinks below the app's configured home."""
    root = path.parent if root is None else root
    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    parent = os.open(root, directory_flags)
    try:
        for part in path.relative_to(root).parts[:-1]:
            child = os.open(part, directory_flags | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ThreadUnavailable("The thread's history file is not a regular file.")
        os.set_blocking(descriptor, True)
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def bounded_lines(source, limit=MAX_INDEX_LINE_BYTES):
    """The file's lines, each read at most limit bytes at a time; a longer line arrives in pieces that fail to parse."""
    return iter(lambda: source.readline(limit), b"")


class ThreadSource(Protocol):
    provider: str

    def list_threads(self, include: Callable[[str], bool]) -> ThreadListing:
        """List main threads whose recorded working directory passes include."""

    def read_thread(self, summary: ThreadSummary, max_messages: int, include: Callable[[str], bool]) -> ThreadSnapshot:
        """Return the thread's latest turn and up to max_messages of its most recent agent messages.

        The listing only picks summary.id. The source decides again, from the same bytes it returns messages from,
        whether the thread's working directory passes include, and refuses the thread otherwise.
        """

    def has_history(self, session_id: str) -> bool:
        """Whether the app still keeps a history file for the session, in any format, consultations included."""


def zstandard_module():
    try:
        import zstandard  # declared in bridge.py's script dependencies
    except ModuleNotFoundError as exc:
        # Only the package's absence; a broken installation keeps its own error.
        if exc.name != "zstandard":
            raise
        raise ThreadUnavailable("Reading compressed Codex history needs the zstandard package.") from exc
    return zstandard


@contextmanager
def decompression_errors():
    """Report damaged compressed history as a named refusal."""
    try:
        yield
    except Exception as exc:
        # Only a zstd error is translated; zstandard is loaded by then if a compressed file was being read.
        zstandard = sys.modules.get("zstandard")
        if zstandard is not None and isinstance(exc, zstandard.ZstdError):
            raise ThreadUnavailable("The thread's compressed history file is damaged.") from exc
        raise


def zstd_reader(source):
    """A buffered reader of a zstd-compressed file's content."""
    decompressor = zstandard_module().ZstdDecompressor()
    return io.BufferedReader(decompressor.stream_reader(source, read_across_frames=True))


def read_first_line(path, root):
    with open_history(path, root) as raw, decompression_errors():
        recency = os.fstat(raw.fileno()).st_mtime
        source = zstd_reader(raw) if rollout_format(path.name) == "zstd" else raw
        line = source.readline(MAX_FIRST_LINE_BYTES)
    return first_record(line), recency


def first_record(line):
    if not line.endswith(b"\n"):
        raise ThreadUnavailable("The thread's history file has no complete first line.")
    return parse_line(line)


class HistoryChanged(Exception):
    """The history file holds fewer bytes than when it was opened."""


class PlainHistory:
    """A plain history file read through one open descriptor, only up to the size it had when it was opened."""

    def __init__(self, source, end):
        self.source, self.end = source, end

    def read_at(self, start, length):
        self.source.seek(start)
        data = self.source.read(length)
        if len(data) != length:
            raise HistoryChanged
        return data

    def lines(self, limit):
        """The file's lines from its start, each read at most limit bytes at a time."""
        position = 0
        while position < self.end:
            self.source.seek(position)
            line = self.source.readline(min(limit, self.end - position))
            if not line:
                raise HistoryChanged
            position += len(line)
            yield line

    def first_line(self):
        return first_record(next(self.lines(MAX_FIRST_LINE_BYTES), b""))

    def tail(self, window):
        """Return the complete records in the last window bytes, and whether the window reached the start."""
        start = max(0, self.end - window)
        return complete_records(self.read_at(start, self.end - start), start == 0), start == 0


class CompressedHistory:
    """A zstd rollout's first line and last content, from one decompression of one open descriptor."""

    def __init__(self, source, limit=None):
        self.head, self.content, self.from_start = decompress_history(source, limit)

    def first_line(self):
        end = self.head.find(b"\n")
        return first_record(self.head[:end + 1] if end >= 0 else self.head)

    def tail(self, window):
        if window >= len(self.content):
            return complete_records(self.content, self.from_start), True
        return complete_records(self.content[-window:], False), False


def decompress_history(raw, limit=None):
    """Decompress a zstd rollout, keeping its content's first line, its last limit bytes and whether those start at
    the beginning.

    A compressed file cannot be read from its end, so the whole stream is decompressed once. The decompressor returns
    a cut-off file's partial content without an error, so the file is refused unless its last frame ends and its
    content ends with a complete record: otherwise its last surviving turn need not be the thread's latest.
    """
    limit = MAX_TAIL_BYTES if limit is None else limit
    zstandard = zstandard_module()
    head, tail, total, frame_open = bytearray(), bytearray(), 0, False
    decompressor = zstandard.ZstdDecompressor().decompressobj()
    with decompression_errors():
        while block := raw.read(1024 * 1024):
            # A zstd block holds at most 128 KiB and needs at least 3 bytes, so one call on a slice of
            # COMPRESSED_SLICE_BYTES returns a bounded amount of content however compressible the file is.
            for start in range(0, len(block), COMPRESSED_SLICE_BYTES):
                piece = block[start:start + COMPRESSED_SLICE_BYTES]
                while piece:
                    content = decompressor.decompress(piece)
                    frame_open = True
                    total += len(content)
                    if total > MAX_DECOMPRESSED_BYTES:
                        raise ThreadUnavailable("The thread's compressed history is too large to read.")
                    if len(head) < MAX_FIRST_LINE_BYTES and b"\n" not in head:
                        head += content[:MAX_FIRST_LINE_BYTES - len(head)]
                    tail += content
                    if len(tail) > limit:
                        del tail[:len(tail) - limit]
                    if decompressor.eof:
                        # Codex may write several frames; whatever follows a frame's end starts the next one.
                        piece, frame_open = decompressor.unused_data, False
                        decompressor = zstandard.ZstdDecompressor().decompressobj()
                    else:
                        piece = b""
    if frame_open:
        raise ThreadUnavailable("The thread's compressed history file is incomplete.")
    if tail and not tail.endswith(b"\n"):
        # Codex compresses a rollout it has finished writing, so its last record must be whole.
        raise ThreadUnavailable("The thread's compressed history file ends inside a record.")
    return bytes(head), bytes(tail), total <= limit


def file_identity(stat_result):
    return tuple(getattr(stat_result, name) for name in IDENTITY_FIELDS)


def path_identity(path):
    try:
        return file_identity(path.lstat())
    except OSError:
        return None


def read_unchanged(root, locate, read):
    """Return read(history) for the file locate() names, opened once, if it stayed the same file throughout.

    The decision on which repository a thread belongs to and the messages returned come from the same descriptor and
    the same extent. The file must keep its identity, size and times while it is read, and its path must still name it
    afterwards; a file that changes is read once more, then refused.
    """
    for _ in range(2):
        path = locate()
        with open_history(path, root) as source:
            opened = os.fstat(source.fileno())
            if rollout_format(path.name) == "zstd":
                history = CompressedHistory(source)
            else:
                history = PlainHistory(source, opened.st_size)
            try:
                result = read(history)
            except HistoryChanged:
                continue
            identities = {file_identity(opened), file_identity(os.fstat(source.fileno())), path_identity(path)}
        if len(identities) == 1:
            return result
    raise ThreadUnavailable(CHANGED_WHILE_READ)


def grown(window):
    return min(window * 2, MAX_TAIL_BYTES)


def scan_tail(history, scan, enough):
    """Grow the tail window until the scan has enough, or reaches the whole file or the cap."""
    window = INITIAL_TAIL_BYTES
    while True:
        records, whole = history.tail(window)
        result = scan(records)
        if whole or window >= MAX_TAIL_BYTES or enough(result):
            return result
        window = grown(window)


def scan_turns(history, max_messages, scan):
    result = scan_tail(history, scan, lambda result: scan_is_enough(result, max_messages))
    if result.latest is None:
        raise ThreadUnavailable("No turn was found in the thread's recent history.")
    return result


class CodexFilesSource:
    """Codex Desktop and interactive CLI threads, from the rollout files under CODEX_HOME or the given home."""

    provider = "codex"

    def __init__(self, home=None):
        self.fixed_home = Path(home) if home else None

    @property
    def home(self):
        return self.fixed_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()

    def rollouts(self):
        found = {}
        for archived, folder in ((False, "sessions"), (True, "archived_sessions")):
            directory = self.home / folder
            if not directory.is_dir():
                continue
            for path in directory.rglob("rollout-*"):
                identifier = rollout_identifier(path.name)
                if identifier:
                    try:
                        if stat.S_ISREG(path.lstat().st_mode):
                            found.setdefault(identifier, []).append((path, archived))
                    except FileNotFoundError:
                        continue
        return found

    def has_history(self, session_id):
        return session_id in self.rollouts()

    def writer_lock_present(self, session_id):
        return (self.home / "thread-writer-locks" / f"{session_id}.lock").exists()

    def names(self):
        index = self.home / "session_index.jsonl"
        try:
            with open_history(index, self.home) as source:
                return parse_session_index(bounded_lines(source))
        except (OSError, ThreadUnavailable):
            return {}

    def list_threads(self, include):
        names = self.names()
        threads, unsupported = [], []
        for identifier, files in self.rollouts().items():
            if not is_supported_history(files):
                # A duplicated history or an unknown format is a migration trigger, not something to guess around.
                unsupported.append(identifier)
                continue
            path, archived = files[0]
            try:
                record, recency = read_first_line(path, self.home)
                main = codex_main_thread(record, identifier)
            except (OSError, ThreadUnavailable):
                # Counted, never silently dropped: a file this reader cannot open may be one Codex changed.
                unsupported.append(identifier)
                continue
            if main is None or not include(main[1]):
                continue
            source, cwd, forked_from_id = main
            threads.append(ThreadSummary(
                id=identifier, name=names.get(identifier), cwd=cwd, recency_at=recency,
                archived=archived, source_kind=source, forked_from_id=forked_from_id,
                writer_lock_present=self.writer_lock_present(identifier),
            ))
        return ThreadListing(threads, tuple(unsupported))

    def rollout(self, identifier):
        files = self.rollouts().get(identifier, [])
        if not is_supported_history(files):
            raise ThreadUnavailable("The thread's history file is duplicated or in an unsupported format.")
        return files[0][0]

    def read_thread(self, summary, max_messages, include):
        def read(history):
            main = codex_main_thread(history.first_line(), summary.id)
            if main is None or not include(main[1]):
                raise ThreadUnavailable(NOT_IN_REPOSITORY)
            source, cwd, forked_from_id = main
            if forked_from_id:
                raise ThreadUnavailable("Forked Codex threads are not supported yet.")
            current = replace(summary, cwd=cwd, source_kind=source, forked_from_id=forked_from_id)
            return codex_snapshot(current, scan_turns(history, max_messages, scan_codex), max_messages)

        return read_unchanged(self.home, lambda: self.rollout(summary.id), read)


class ClaudeSessionsSource:
    """Claude Code sessions with a person at the keyboard, from the session files under the Claude config directory."""

    provider = "claude"

    @property
    def home(self):
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()

    def has_history(self, session_id):
        return any((self.home / "projects").glob(f"*/{session_id}.jsonl"))

    def session_file(self, session_id):
        paths = list((self.home / "projects").glob(f"*/{session_id}.jsonl"))
        if len(paths) != 1:
            raise ThreadUnavailable("The session's history file was not found.")
        return paths[0]

    @staticmethod
    def current_cwd(history, identifier):
        """The working directory the session's latest records name and its entrypoint, for an interactive session;
        else (None, None).

        The window grows until a record names a directory: a large record without one can follow the latest move. If
        none does within the cap, which repository the session is in is unknown, and the session is refused.
        """
        first_cwd, entrypoint = claude_head(islice(history.lines(MAX_FIRST_LINE_BYTES), MAX_HEAD_LINES), identifier)
        if first_cwd is None or not is_interactive_claude_session(entrypoint):
            return None, None
        window = INITIAL_TAIL_BYTES
        while True:
            records, whole = history.tail(window)
            cwd = claude_last_cwd(records, identifier)
            if cwd is not None:
                return cwd, entrypoint
            if whole or window >= MAX_TAIL_BYTES:
                raise ThreadUnavailable("The session's recent history does not name its working directory.")
            window = grown(window)

    def list_threads(self, include):
        threads, unreadable = [], []
        # Subagent transcripts sit one level deeper and are not sessions of their own.
        for path in (self.home / "projects").glob("*/*.jsonl"):
            if not UUID_PATTERN.fullmatch(path.stem):
                continue
            try:
                with open_history(path, self.home) as source:
                    opened = os.fstat(source.fileno())
                    history = PlainHistory(source, opened.st_size)
                    cwd, entrypoint = self.current_cwd(history, path.stem)
                    if cwd is None or not include(cwd):
                        continue
                    def scan(records):
                        return scan_claude(records, include, title_only=True, identifier=path.stem)

                    name = scan_tail(history, scan, lambda result: result.segment_start).title
                threads.append(ThreadSummary(id=path.stem, name=name, cwd=cwd, recency_at=opened.st_mtime,
                                             source_kind=entrypoint))
            except (OSError, ThreadUnavailable, HistoryChanged):
                unreadable.append(path.stem)
        return ThreadListing(threads, tuple(unreadable))

    def read_thread(self, summary, max_messages, include):
        def read(history):
            belongs = cache(include)
            cwd, entrypoint = self.current_cwd(history, summary.id)
            if cwd is None or not belongs(cwd):
                raise ThreadUnavailable(NOT_IN_REPOSITORY)
            scan = scan_turns(history, max_messages,
                              lambda records: scan_claude(records, belongs, identifier=summary.id))
            # The title is quoted into seed context, so it too comes only from the session's stay in this repository.
            current = replace(summary, cwd=cwd, name=scan.title, source_kind=entrypoint)
            return claude_snapshot(current, scan, max_messages)

        return read_unchanged(self.home, lambda: self.session_file(summary.id), read)


def git_common_dir(directory):
    """Return the repository's shared git directory, identical for all of its worktrees, or None."""
    # The bridge's isolated git environment: no inherited GIT_* variable, such as a GIT_TRACE file to append to, and
    # a PATH of absolute entries, so no git in the directory itself runs.
    environment = git_environment(os.environ)
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, timeout=10, env=environment, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    location = None if result.returncode else git_output_path(result.stdout)
    return Path(location).resolve() if location else None


class ThreadReader:
    """Apply the same-repository rule, agent-only output and size caps on top of a ThreadSource."""

    def __init__(self, source, common_dir=git_common_dir):
        self.source = source
        self.common_dir = common_dir

    def same_repository(self, working_directory):
        repository = self.common_dir(working_directory)
        if repository is None:
            raise ThreadUnavailable("working_directory must be inside a git repository.")
        def include(cwd):
            # Only an absolute path: an empty or relative one would resolve against the server's own directory.
            return Path(cwd).is_absolute() and Path(cwd).is_dir() and self.common_dir(cwd) == repository

        return include

    def list(self, working_directory, limit):
        listing = self.source.list_threads(cache(self.same_repository(working_directory)))
        return listing_result(self.source.provider, newest_first(listing), limit)

    def read(self, working_directory, thread, max_messages):
        include = self.same_repository(working_directory)
        # The listing only picks the thread; the source decides again from the bytes it reads the messages from.
        summary = match_thread(newest_first(self.source.list_threads(cache(include))).threads, thread)
        return snapshot_result(self.source.provider, self.source.read_thread(summary, max_messages, include))
