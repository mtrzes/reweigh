"""Read recent Codex threads and Claude Code sessions without writing to them.

A thread is readable only when its recorded working directory belongs to the same git repository as the request,
worktrees included, and only agent messages come back: never prompts, tool calls, tool output or reasoning. Both sources
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
from itertools import islice
from pathlib import Path
from typing import Callable, Protocol

from thread_parsing import (
    GIT_LOCATION_ENV, ThreadListing, ThreadSnapshot, ThreadSummary, ThreadUnavailable, claude_head, claude_last_cwd,
    claude_snapshot, claude_title, codex_main_thread, codex_snapshot, complete_records, is_interactive_claude_session,
    is_supported_history, listing_result, match_thread, newest_first, parse_line, parse_session_index,
    rollout_format, rollout_identifier, scan_claude, scan_codex, scan_is_enough, snapshot_result, UUID_PATTERN,
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


def open_history(path):
    """Open one of the apps' history files read-only, without blocking, and only if it is a regular file: a FIFO or a
    device in its place is refused at once."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
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

    def read_thread(self, summary: ThreadSummary, max_messages: int) -> ThreadSnapshot:
        """Return the thread's latest turn and up to max_messages of its most recent agent messages."""

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


def read_first_line(path):
    with open_history(path) as raw, decompression_errors():
        source = zstd_reader(raw) if rollout_format(path.name) == "zstd" else raw
        line = source.readline(MAX_FIRST_LINE_BYTES)
    if not line.endswith(b"\n"):
        raise ThreadUnavailable("The thread's history file has no complete first line.")
    return parse_line(line)


def read_tail(path, window):
    """Return the complete records in the file's last window bytes, and whether the window reached the start."""
    size = path.stat().st_size
    start = max(0, size - window)
    with open_history(path) as source:
        source.seek(start)
        data = source.read(size - start)
    return complete_records(data, start == 0), start == 0


def file_identity(stat_result):
    return stat_result.st_ino, stat_result.st_size, stat_result.st_mtime_ns


def compressed_tail(path, limit=None):
    """Decompress a zstd rollout, keeping only its content's last limit bytes and whether they start at the beginning.

    A compressed file cannot be read from its end, so the whole stream is decompressed once. The decompressor returns
    a cut-off file's partial content without an error, so the file is refused unless its last frame ends, its content
    ends with a complete record, and it is the same unchanged file afterwards: otherwise its last surviving turn need
    not be the thread's latest.
    """
    limit = MAX_TAIL_BYTES if limit is None else limit
    zstandard = zstandard_module()
    tail, total, frame_open = bytearray(), 0, False
    decompressor = zstandard.ZstdDecompressor().decompressobj()
    with open_history(path) as raw, decompression_errors():
        opened = file_identity(os.fstat(raw.fileno()))
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
                    tail += content
                    if len(tail) > limit:
                        del tail[:len(tail) - limit]
                    if decompressor.eof:
                        # Codex may write several frames; whatever follows a frame's end starts the next one.
                        piece, frame_open = decompressor.unused_data, False
                        decompressor = zstandard.ZstdDecompressor().decompressobj()
                    else:
                        piece = b""
        unchanged = file_identity(os.fstat(raw.fileno())) == opened == file_identity(path.stat())
    if not unchanged:
        raise ThreadUnavailable("The thread's compressed history file changed while it was read.")
    if frame_open:
        raise ThreadUnavailable("The thread's compressed history file is incomplete.")
    if tail and not tail.endswith(b"\n"):
        # Codex compresses a rollout it has finished writing, so its last record must be whole.
        raise ThreadUnavailable("The thread's compressed history file ends inside a record.")
    return bytes(tail), total <= limit


def tail_reader(path):
    """Return read(window) -> (records, whole) for a rollout; whole means a larger window cannot add records."""
    if rollout_format(path.name) != "zstd":
        return lambda window: read_tail(path, window)
    content, from_start = compressed_tail(path)

    def read(window):
        if window >= len(content):
            return complete_records(content, from_start), True
        return complete_records(content[-window:], False), False

    return read


def scan_tail(path, max_messages, scan):
    """Grow the tail window until scan finds a turn and enough messages, or the whole file or the cap is read."""
    read = tail_reader(path)
    window = INITIAL_TAIL_BYTES
    while True:
        records, whole = read(window)
        result = scan(records)
        if whole or window >= MAX_TAIL_BYTES or scan_is_enough(result, max_messages):
            break
        window *= 2
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
                if path.is_file() and identifier:
                    found.setdefault(identifier, []).append((path, archived))
        return found

    def has_history(self, session_id):
        return session_id in self.rollouts()

    def writer_lock_present(self, session_id):
        return (self.home / "thread-writer-locks" / f"{session_id}.lock").exists()

    def names(self):
        index = self.home / "session_index.jsonl"
        if not index.is_file():
            return {}
        with open_history(index) as source:
            return parse_session_index(bounded_lines(source))

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
                main = codex_main_thread(read_first_line(path))
            except (OSError, ThreadUnavailable):
                # Counted, never silently dropped: a file this reader cannot open may be one Codex changed.
                unsupported.append(identifier)
                continue
            if main is None or not include(main[1]):
                continue
            source, cwd, forked_from_id = main
            threads.append(ThreadSummary(
                id=identifier, name=names.get(identifier), cwd=cwd, recency_at=path.stat().st_mtime,
                archived=archived, source_kind=source, forked_from_id=forked_from_id,
                writer_lock_present=self.writer_lock_present(identifier),
            ))
        return ThreadListing(threads, tuple(unsupported))

    def read_thread(self, summary, max_messages):
        if summary.forked_from_id:
            raise ThreadUnavailable("Forked Codex threads are not supported yet.")
        files = self.rollouts().get(summary.id, [])
        if not is_supported_history(files):
            raise ThreadUnavailable("The thread's history file is duplicated or in an unsupported format.")
        return codex_snapshot(summary, scan_tail(files[0][0], max_messages, scan_codex), max_messages)


class ClaudeSessionsSource:
    """Claude Code sessions with a person at the keyboard, from the session files under the Claude config directory."""

    provider = "claude"

    @property
    def home(self):
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()

    def has_history(self, session_id):
        return any((self.home / "projects").glob(f"*/{session_id}.jsonl"))

    @staticmethod
    def head(path):
        """Return the session's first recorded working directory and its entrypoint from its first entries."""
        with open_history(path) as source:
            return claude_head(islice(bounded_lines(source, MAX_FIRST_LINE_BYTES), MAX_HEAD_LINES))

    @staticmethod
    def current_cwd(path):
        """The working directory the session's latest records name, else None."""
        records, _ = read_tail(path, INITIAL_TAIL_BYTES)
        return claude_last_cwd(records)

    @staticmethod
    def title(path):
        with open_history(path) as source:
            return claude_title(bounded_lines(source))

    def list_threads(self, include):
        threads, unreadable = [], []
        # Subagent transcripts sit one level deeper and are not sessions of their own.
        for path in (self.home / "projects").glob("*/*.jsonl"):
            if not UUID_PATTERN.fullmatch(path.stem):
                continue
            try:
                first_cwd, entrypoint = self.head(path)
                if first_cwd is None or not is_interactive_claude_session(entrypoint):
                    continue
                cwd = self.current_cwd(path) or first_cwd
                if not include(cwd):
                    continue
                threads.append(ThreadSummary(
                    id=path.stem, name=self.title(path), cwd=cwd, recency_at=path.stat().st_mtime,
                    source_kind=entrypoint,
                ))
            except (OSError, ThreadUnavailable):
                unreadable.append(path.stem)
        return ThreadListing(threads, tuple(unreadable))

    def read_thread(self, summary, max_messages):
        paths = list((self.home / "projects").glob(f"*/{summary.id}.jsonl"))
        if len(paths) != 1:
            raise ThreadUnavailable("The session's history file was not found.")
        return claude_snapshot(summary, scan_tail(paths[0], max_messages, scan_claude), max_messages)


def git_common_dir(directory):
    """Return the repository's shared git directory, identical for all of its worktrees, or None."""
    environment = {name: value for name, value in os.environ.items() if name not in GIT_LOCATION_ENV}
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=10, env=environment, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


class ThreadReader:
    """Apply the same-repository rule, agent-only output and size caps on top of a ThreadSource."""

    def __init__(self, source, common_dir=git_common_dir):
        self.source = source
        self.common_dir = common_dir

    def same_repository(self, working_directory):
        repository = self.common_dir(working_directory)
        if repository is None:
            raise ThreadUnavailable("working_directory must be inside a git repository.")
        decided = {}

        def include(cwd):
            if cwd not in decided:
                # Only an absolute path: an empty or relative one would resolve against the server's own directory.
                decided[cwd] = (Path(cwd).is_absolute() and Path(cwd).is_dir()
                                and self.common_dir(cwd) == repository)
            return decided[cwd]

        return include

    def listing(self, working_directory):
        return newest_first(self.source.list_threads(self.same_repository(working_directory)))

    def list(self, working_directory, limit):
        return listing_result(self.source.provider, self.listing(working_directory), limit)

    def read(self, working_directory, thread, max_messages):
        summary = match_thread(self.listing(working_directory).threads, thread)
        return snapshot_result(self.source.provider, self.source.read_thread(summary, max_messages))
