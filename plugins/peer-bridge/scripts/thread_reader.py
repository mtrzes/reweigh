"""Read recent Codex threads and Claude Code sessions without writing to them.

A thread is readable only when its recorded working directory belongs to the same git repository as the request,
worktrees included, and only agent messages come back: never prompts, tool calls, tool output or reasoning. The listing
only picks a thread; the read opens its file once and decides again, from the bytes it returns messages from, whether
the thread belongs to the repository, and refuses a file that changes while it is read. Both sources
read the apps' session files. They open files read-only, never open a database or a lock file, and start no process
except git. When a file looks different from what the parser knows, the reader refuses it with a named reason instead
of guessing. The Codex source sits behind ThreadSource so an app-server source can replace it, under the conditions
in docs/adr/0001-codex-thread-reader-source.md at the repository root.

This module does the git access and picks what to read; history_capture.py opens and reads the files, and
thread_parsing.py holds the rules applied to what they hold.
"""

import os
import stat
import subprocess
from dataclasses import replace
from functools import cache
from itertools import islice
from pathlib import Path
from typing import Callable, Protocol

from consultation import git_environment
from history_capture import MAX_TAIL_BYTES, read_header, read_once, read_stable
from thread_parsing import (
    GIT_LOCATION_ENV, HistoryLookup, NOT_IN_REPOSITORY, REDIRECTED_REPOSITORY, RepositoryLayout, TailWindows,
    ThreadListing, ThreadSnapshot, ThreadSummary, ThreadUnavailable, UUID_PATTERN, claude_cwd_search, core_worktree,
    reciprocal, claude_head, claude_snapshot, claude_title_search,
    claude_turns_search, codex_main_thread, codex_snapshot, git_output_path, is_interactive_claude_session,
    is_supported_history, listing_result, match_thread, newest_first, parse_session_index, rollout_identifier,
    scan_codex, search_tail, snapshot_result, turns_search,
)

__all__ = [
    "GIT_LOCATION_ENV", "ClaudeSessionsSource", "CodexFilesSource", "ThreadReader", "ThreadSource",
    "RepositoryRedirected", "ThreadUnavailable", "borrowed_objects", "git_common_dir", "repository_layout",
    "layout_paths", "LAYOUT_ARGUMENTS",
]

# Each search starts at 256 KiB of a history's tail and doubles its window up to the most a capture keeps.
TAIL_WINDOWS = TailWindows(256 * 1024, MAX_TAIL_BYTES)
MAX_HEAD_LINES = 50


def listed(directory, keep):
    """The entries of directory that keep passes, and 1 if directory could not be listed, else 0. A missing directory
    lists as empty; one that cannot be listed is counted, never taken as empty, as Path.glob and Path.rglob would."""
    try:
        with os.scandir(directory) as entries:
            return [entry for entry in entries if keep(entry)], 0
    except (FileNotFoundError, NotADirectoryError):
        return [], 0
    except OSError:
        return [], 1


def regular_files_below(directory, matches):
    """Regular files at any depth below directory whose name passes matches, without following symlinks, and how many
    directories could not be listed."""
    found, unreadable, pending = [], 0, [directory]
    while pending:
        entries, missed = listed(pending.pop(), lambda entry: entry.is_dir(follow_symlinks=False)
                                 or matches(entry.name) and entry.is_file(follow_symlinks=False))
        unreadable += missed
        for entry in entries:
            (pending if entry.is_dir(follow_symlinks=False) else found).append(Path(entry.path))
    return found, unreadable


def entries_one_level_down(directory, matches):
    """Entries of any type in each subdirectory of directory whose name passes matches, like Path.glob("*/name"), and
    how many directories could not be listed."""
    folders, unreadable = listed(directory, lambda entry: entry.is_dir())
    found = []
    for folder in folders:
        entries, missed = listed(folder.path, lambda entry: matches(entry.name))
        unreadable += missed
        found.extend(Path(entry.path) for entry in entries)
    return found, unreadable


class ThreadSource(Protocol):
    provider: str

    def list_threads(self, include: Callable[[str], bool]) -> ThreadListing:
        """List main threads whose recorded working directory passes include."""

    def read_thread(self, summary: ThreadSummary, max_messages: int, include: Callable[[str], bool]) -> ThreadSnapshot:
        """Return the thread's latest turn and up to max_messages of its most recent agent messages.

        The listing only picks summary.id. The source decides again, from the same bytes it returns messages from,
        whether the thread's working directory passes include, and refuses the thread otherwise.
        """


class CodexFilesSource:
    """Codex Desktop and interactive CLI threads, from the rollout files under CODEX_HOME or the given home."""

    provider = "codex"

    def __init__(self, home=None):
        self.fixed_home = Path(home) if home else None

    @property
    def home(self):
        return self.fixed_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()

    def rollouts(self):
        """Every rollout file, archived or not, by thread ID, as (path, archived) pairs: the one lookup for listing,
        reading and failure diagnosis."""
        found, unreadable = {}, 0
        for archived, folder in ((False, "sessions"), (True, "archived_sessions")):
            paths, missed = regular_files_below(self.home / folder, lambda name: name.startswith("rollout-"))
            unreadable += missed
            for path in paths:
                identifier = rollout_identifier(path.name)
                if identifier:
                    found.setdefault(identifier, []).append((path, archived))
        return HistoryLookup(found, unreadable)

    def writer_lock_present(self, session_id):
        return (self.home / "thread-writer-locks" / f"{session_id}.lock").exists()

    def names(self):
        try:
            return read_once(self.home / "session_index.jsonl", self.home,
                             lambda index: parse_session_index(index.suffix_lines()))
        except (OSError, ThreadUnavailable):
            # Names are optional: without a readable index the threads are listed unnamed.
            return {}

    def list_threads(self, include):
        names = self.names()
        threads, unsupported = [], []
        rollouts = self.rollouts()
        for identifier, files in rollouts.found.items():
            if not is_supported_history(files):
                # A duplicated history or an unknown format is a migration trigger, not something to guess around.
                unsupported.append(identifier)
                continue
            path, archived = files[0]
            try:
                record, recency = read_header(path, self.home)
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
        return ThreadListing(threads, tuple(unsupported), rollouts.unreadable_directories)

    def rollout(self, identifier):
        files = self.rollouts().found.get(identifier, [])
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
            scan = search_tail(history.tail, turns_search(scan_codex, max_messages), TAIL_WINDOWS)
            return codex_snapshot(current, scan, max_messages)

        return read_stable(self.home, lambda: self.rollout(summary.id), read)


class ClaudeSessionsSource:
    """Claude Code sessions with a person at the keyboard, from the session files under the Claude config directory."""

    provider = "claude"

    @property
    def home(self):
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()

    def history_files(self, matches):
        """Every history entry whose name passes matches, consultations included: the one lookup for listing, reading
        and failure diagnosis."""
        return HistoryLookup(*entries_one_level_down(self.home / "projects", matches))

    def session_files(self, session_id):
        return self.history_files(lambda name: name == f"{session_id}.jsonl")

    def session_file(self, session_id):
        paths = self.session_files(session_id).found
        if len(paths) != 1:
            raise ThreadUnavailable("The session's history file was not found.")
        return paths[0]

    @staticmethod
    def current_cwd(history, identifier):
        """The working directory the session's latest records name and its entrypoint, for an interactive session;
        else (None, None)."""
        first_cwd, entrypoint = claude_head(islice(history.head_lines(), MAX_HEAD_LINES), identifier)
        if first_cwd is None or not is_interactive_claude_session(entrypoint):
            return None, None
        return search_tail(history.tail, claude_cwd_search(identifier), TAIL_WINDOWS), entrypoint

    def list_threads(self, include):
        threads, unreadable = [], []
        # Subagent transcripts sit one level deeper and are not sessions of their own.
        files = self.history_files(lambda name: name.endswith(".jsonl"))
        for path in files.found:
            if not UUID_PATTERN.fullmatch(path.stem):
                continue
            try:
                summary = read_once(path, self.home, lambda history: self.listed(history, path.stem, include))
            except (OSError, ThreadUnavailable):
                unreadable.append(path.stem)
                continue
            if summary is not None:
                threads.append(summary)
        return ThreadListing(threads, tuple(unreadable), files.unreadable_directories)

    def listed(self, history, identifier, include):
        """The listing's summary of a session whose current working directory passes include, else None."""
        cwd, entrypoint = self.current_cwd(history, identifier)
        if cwd is None or not include(cwd):
            return None
        try:
            name = search_tail(history.tail, claude_title_search(include, identifier), TAIL_WINDOWS).title
        except ThreadUnavailable:
            # Counted as unreadable, a session refused only after it matched the repository would show in the
            # machine-wide count from this repository alone. It is listed without a title; its read refuses it.
            name = None
        return ThreadSummary(id=identifier, name=name, cwd=cwd, recency_at=history.recency, source_kind=entrypoint)

    def read_thread(self, summary, max_messages, include):
        def read(history):
            belongs = cache(include)
            cwd, entrypoint = self.current_cwd(history, summary.id)
            if cwd is None or not belongs(cwd):
                raise ThreadUnavailable(NOT_IN_REPOSITORY)

            scan = search_tail(history.tail, claude_turns_search(belongs, summary.id, max_messages), TAIL_WINDOWS)
            # The title is quoted into seed context, so it too comes only from the session's stay in this repository.
            # The turns search stops once it has enough messages; the title comes from the listing's own search.
            name = search_tail(history.tail, claude_title_search(belongs, summary.id), TAIL_WINDOWS).title
            current = replace(summary, cwd=cwd, name=name, source_kind=entrypoint)
            return claude_snapshot(current, scan, max_messages)

        return read_stable(self.home, lambda: self.session_file(summary.id), read)


# The rev-parse arguments that print a working tree's layout, one absolute path per line, in RepositoryLayout order.
LAYOUT_ARGUMENTS = ("rev-parse", "--path-format=absolute", "--show-toplevel", "--absolute-git-dir", "--git-common-dir")


def layout_paths(stdout):
    """The three paths LAYOUT_ARGUMENTS prints, or None when git printed anything else."""
    lines = stdout.split(b"\n")
    if lines[-1:] == [b""]:
        lines = lines[:-1]
    if len(lines) != 3 or not all(lines):
        return None
    return tuple(Path(os.fsdecode(line)).resolve() for line in lines)


def back_reference(git_dir, name):
    """The path a git directory's one-line file (gitdir, commondir) names, resolved against the git directory."""
    try:
        text = (git_dir / name).read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return Path("/nonexistent-unreadable")  # present but unreadable: never matches
    location = git_output_path(text)
    return (git_dir / os.fsdecode(location)).resolve() if location else Path("/nonexistent-unreadable")


def repository_layout(top, git_dir, common_dir):
    """Read the back references git's own setup writes, for reciprocal() to compare."""
    try:
        mode = os.lstat(top / ".git").st_mode
        dot_git = "directory" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else None
    except OSError:
        dot_git = None
    try:
        worktree = core_worktree((git_dir / "config").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        worktree = None
    return RepositoryLayout(top, git_dir, common_dir, dot_git, back_reference(git_dir, "gitdir"),
                            (git_dir / worktree).resolve() if worktree else None, back_reference(git_dir, "commondir"))


def borrowed_objects(common_dir):
    """The resolved entries of the repository's objects/info/alternates; an unreadable file counts as outside."""
    objects = common_dir / "objects"
    try:
        lines = (objects / "info" / "alternates").read_bytes().splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        return [Path("/nonexistent-unreadable")]
    entries = [os.fsdecode(line).strip() for line in lines]
    return [(objects / entry).resolve() for entry in entries if entry and not entry.startswith("#")]


class RepositoryRedirected(ThreadUnavailable):
    """The working tree's .git points at a git directory that does not point back."""


def git_common_dir(directory):
    """Return the repository's shared git directory, identical for all of its worktrees, or None outside one.

    Raises RepositoryRedirected when the working tree and its git directory do not point at each other."""
    # The bridge's isolated git environment: no inherited GIT_* variable, such as a GIT_TRACE file to append to, and
    # a PATH of absolute entries, so no git in the directory itself runs.
    environment = git_environment(os.environ)
    try:
        result = subprocess.run(["git", "-C", str(directory), *LAYOUT_ARGUMENTS], capture_output=True, timeout=10,
                                env=environment, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    paths = None if result.returncode else layout_paths(result.stdout)
    if paths is None:
        return None
    if not reciprocal(repository_layout(*paths)):
        raise RepositoryRedirected(REDIRECTED_REPOSITORY)
    return paths[2]


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
            if not (Path(cwd).is_absolute() and Path(cwd).is_dir()):
                return False
            try:
                return self.common_dir(cwd) == repository
            except RepositoryRedirected:
                return False  # a folder that only claims to belong to this repository

        return include

    def listing(self, include):
        return newest_first(self.source.list_threads(cache(include)))

    def list(self, working_directory, limit):
        return listing_result(self.source.provider, self.listing(self.same_repository(working_directory)), limit)

    def read(self, working_directory, thread, max_messages):
        # A read by ID goes through selection too, because an ID can equal another thread's exact name, which
        # selection reports as ambiguous; a direct lookup by ID would pick one of the two silently.
        include = self.same_repository(working_directory)
        summary = match_thread(self.listing(include).threads, thread)
        return snapshot_result(self.source.provider, self.source.read_thread(summary, max_messages, include))
