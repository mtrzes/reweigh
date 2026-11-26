"""Pure rules for reading Codex threads and Claude Code sessions: records in, decisions out.

Nothing here opens a file, starts a process or reads the environment. thread_reader.py does that and passes the
records, lines and file facts it gathered to these functions.
"""

import json
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

MAX_MESSAGE_CHARACTERS = 24_000
MAX_TAIL_RECORDS = 128 * 1024
CODEX_MAIN_SOURCES = {"vscode", "cli"}
CODEX_TURN_EVENTS = ("task_started", "task_complete", "turn_aborted", "item_completed")
CODEX_MESSAGE_PHASES = (None, "commentary", "final_answer")
MALFORMED_CODEX_EVENT = "The thread's history file has a malformed line for a Codex event."
MALFORMED_CLAUDE_MESSAGE = "The thread's history file has a malformed line for a Claude message."
# Codex compresses older rollouts with zstd; the records inside are the same JSONL.
ROLLOUT_FORMATS = ((".jsonl", "jsonl"), (".jsonl.zst", "zstd"))
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
FINISHED_STATUSES = {"completed", "failed", "interrupted"}
# The turn status a Claude reply's stop_reason leaves: a reply cut off at a limit still ends the turn, and only a tool
# call, a paused server tool or a reply still being written (no stop_reason yet) leaves the turn running.
CLAUDE_STOP_STATUSES = {
    "end_turn": "completed", "stop_sequence": "completed", "max_tokens": "completed",
    "model_context_window_exceeded": "completed", "refusal": "failed",
    "tool_use": "inProgress", "pause_turn": "inProgress", None: "inProgress",
}
# Claude Code records an interruption as a user turn with exactly one of these texts.
CLAUDE_INTERRUPTION_MARKERS = {"[Request interrupted by user]", "[Request interrupted by user for tool use]"}
# Claude Code's title records and the field each keeps its title in; a custom title wins over a generated one.
CLAUDE_TITLE_RECORDS = (("custom-title", "customTitle"), ("ai-title", "aiTitle"))
# Variables that would point git at another repository than the one a request names.
GIT_LOCATION_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")
# The same answer for a thread of another repository, an unsupported history and no thread at all.
NOT_IN_REPOSITORY = "No readable thread with that ID or name belongs to this repository."
NO_TURN = "No turn was found in the thread's recent history."
NO_CLAUDE_CWD = "The session's recent history does not name its working directory."
ANCESTRY_UNRESOLVED = "The Claude session's active ancestry could not be resolved in recent history."


class ThreadUnavailable(Exception):
    """A thread cannot be read safely. The message names the reason and never quotes the thread."""


@dataclass(frozen=True)
class ThreadSummary:
    id: str
    name: str | None
    cwd: str
    recency_at: float
    archived: bool = False
    source_kind: str = ""
    forked_from_id: str | None = None
    writer_lock_present: bool = False
    """The thread's writer-lock file exists. Only a hint that an app holds the thread: a crash can leave it behind."""


@dataclass(frozen=True)
class TurnState:
    turn_id: str | None
    status: str
    """completed, failed, interrupted or inProgress."""
    liveness: str | None = None
    """For an unfinished turn: abandoned when no app can be writing the thread, otherwise unknown; None if finished."""


@dataclass(frozen=True)
class AgentMessage:
    turn_id: str | None
    text: str
    phase: str | None = None


@dataclass(frozen=True)
class ThreadSnapshot:
    summary: ThreadSummary
    latest_turn: TurnState
    messages: list[AgentMessage]


@dataclass(frozen=True)
class ThreadListing:
    threads: list[ThreadSummary]
    unsupported: tuple[str, ...] = ()
    """IDs of threads skipped because their history is duplicated or in a format this reader does not parse, from any
    repository: the working directory of such a history cannot be read."""
    unreadable_directories: int = 0
    """How many directories below the history, from any repository, could not be listed: a thread may be in one."""


@dataclass(frozen=True)
class HistoryLookup:
    """The history files a lookup found, and how many directories it could not list: more files may be in them."""
    found: object
    unreadable_directories: int = 0


@dataclass(frozen=True)
class Scan:
    """What one pass over a history's records found."""
    latest: TurnState | None
    messages: list[AgentMessage]
    completions: dict = field(default_factory=dict)
    """Codex only: each completed turn's recorded final agent message."""
    title: str | None = None
    """Claude only: the session's title, taken only from the records that count."""
    segment_start: bool = False
    """Claude only: the records include where the session entered the requested repository, so older records cannot
    count and a larger window cannot add any."""


def parse_line(line):
    try:
        record = json.loads(line)
    except (ValueError, RecursionError) as exc:
        raise ThreadUnavailable("The thread's history file has a malformed line.") from exc
    if not isinstance(record, dict):
        raise ThreadUnavailable("The thread's history file has a malformed line.")
    return record


@dataclass(frozen=True)
class TailRecords:
    """The complete records in a window of a history's last bytes, decoded afresh on each iteration."""
    data: bytes
    from_start: bool
    """The window starts at byte zero, so no earlier record exists."""

    def __iter__(self):
        return complete_records(self.data, self.from_start)


def complete_records(data, from_start):
    """Yield the complete records in a bounded tail window one at a time.

    Unless the block starts at the beginning of the file, its leading partial line is dropped. An unterminated last
    line, which the app may still be writing, is always dropped.
    """
    if not from_start:
        first_newline = data.find(b"\n")
        if first_newline == -1:
            return
        start = first_newline + 1
    else:
        start = 0
    for count in range(MAX_TAIL_RECORDS + 1):
        end = data.find(b"\n", start)
        if end == -1:
            return
        if count == MAX_TAIL_RECORDS:
            raise ThreadUnavailable("The thread's recent history contains too many records.")
        line = data[start:end]
        if line.strip():
            yield parse_line(line)
        start = end + 1


def scan_is_enough(scan, max_messages):
    return scan.latest is not None and (len(scan.messages) >= max_messages or scan.segment_start)


# Tail windows


EARLIER = object()
"""A tail search's answer when only earlier records can decide."""


@dataclass(frozen=True)
class TailWindows:
    """The tail windows a search is offered: initial bytes first, doubling up to cap."""
    initial: int
    cap: int


def search_tail(tail, search, windows):
    """Offer search ever larger windows of a history's tail until it decides, and return what it decides.

    tail(size) returns the TailRecords of the last size bytes. search(records, final) returns its result, or EARLIER
    when only earlier records can decide. final is True for the last window, which reaches byte zero or the cap, and
    there the search must decide; records.from_start tells the two apart.
    """
    window = windows.initial
    while True:
        records = tail(window)
        final = records.from_start or window >= windows.cap
        result = search(records, final)
        if result is not EARLIER:
            return result
        if final:
            raise RuntimeError("A tail search asked for records before its last window.")
        window = min(window * 2, windows.cap)


def tail_search(examine, decides, last):
    """A search that returns what examine(records) found once decides(found) holds, and last(found) on its last
    window. found is EARLIER when the records themselves say only earlier ones can decide."""
    def search(records, final):
        found = examine(records)
        if found is not EARLIER and decides(found):
            return found
        return last(found) if final else EARLIER

    return search


def turns_search(scan, max_messages):
    """Search for the latest turn and up to max_messages messages with scan(records), which returns a Scan.

    Once no earlier records remain, fewer messages stand, no turn is a refusal and an unresolved ancestry gap is a
    refusal of its own.
    """
    def last(result):
        if result is EARLIER:
            raise ThreadUnavailable(ANCESTRY_UNRESOLVED)
        if result.latest is None:
            raise ThreadUnavailable(NO_TURN)
        return result

    return tail_search(scan, lambda result: scan_is_enough(result, max_messages), last)


def text_of(blocks, block_type):
    """Join the text of the blocks of one type, skipping anything else such as tool calls or reasoning."""
    if not isinstance(blocks, list):
        return ""
    return "".join(
        block["text"] for block in blocks
        if isinstance(block, dict) and block.get("type") == block_type and isinstance(block.get("text"), str)
    )


def keep_recent(snapshot_messages, max_messages):
    return snapshot_messages[-max_messages:]


def require_history_id(recorded, expected):
    if recorded != expected:
        raise ThreadUnavailable("The thread's recorded ID does not match its history filename.")


# Codex


def rollout_identifier(file_name):
    """The thread ID a rollout file name carries, or None."""
    identifiers = UUID_PATTERN.findall(file_name)
    return identifiers[-1] if identifiers else None


def rollout_format(file_name):
    """The rollout's storage format from its exact file-name suffix: "jsonl", "zstd" (zstd-compressed JSONL) or None."""
    for suffix, kind in ROLLOUT_FORMATS:
        if file_name.endswith(suffix):
            return kind
    return None


def is_supported_history(files):
    """Whether a thread's rollout files are one file in a format the reader parses. files holds (path, archived)."""
    return len(files) == 1 and rollout_format(files[0][0].name) is not None


def parse_session_index(lines):
    """Map thread IDs to names from session_index.jsonl lines, skipping lines that do not parse."""
    names = {}
    for line in lines:
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            names[entry["id"]] = entry.get("thread_name")
    return names


def codex_main_thread(meta, identifier):
    """Return (source, cwd, forked_from_id) for a Desktop or interactive CLI thread's session_meta, else None.

    Subagent and guardian threads record their source as an object; consultations record "exec".
    """
    payload = meta.get("payload") if meta.get("type") == "session_meta" else None
    if isinstance(payload, dict):
        require_history_id(payload.get("id"), identifier)
    source = payload.get("source") if isinstance(payload, dict) else None
    if not isinstance(source, str) or source not in CODEX_MAIN_SOURCES:
        return None
    cwd = payload.get("cwd")
    if not isinstance(cwd, str):
        return None
    return source, cwd, payload.get("forked_from_id")


def codex_agent_message(item, turn_id):
    if not isinstance(item, dict) or not isinstance(item.get("type"), str):
        raise ThreadUnavailable(MALFORMED_CODEX_EVENT)
    if item["type"] != "AgentMessage":
        return None
    content, phase = item.get("content"), item.get("phase")
    if not isinstance(content, list) or phase not in CODEX_MESSAGE_PHASES:
        raise ThreadUnavailable(MALFORMED_CODEX_EVENT)
    text = []
    for block in content:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise ThreadUnavailable(MALFORMED_CODEX_EVENT)
        if block["type"] == "Text":
            if not isinstance(block.get("text"), str):
                raise ThreadUnavailable(MALFORMED_CODEX_EVENT)
            text.append(block["text"])
    joined = "".join(text)
    return AgentMessage(turn_id, joined, phase) if joined.strip() else None


def scan_codex(records):
    latest, messages, completions = None, [], {}
    for record in records:
        if record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
            raise ThreadUnavailable(MALFORMED_CODEX_EVENT)
        kind, turn_id = payload.get("type"), payload.get("turn_id")
        if kind not in CODEX_TURN_EVENTS:
            continue
        if turn_id is not None and not isinstance(turn_id, str):
            raise ThreadUnavailable(MALFORMED_CODEX_EVENT)
        if kind == "task_started":
            latest = TurnState(turn_id, "inProgress")
        elif kind == "task_complete":
            error, final = payload.get("error"), payload.get("last_agent_message")
            if ((error is not None and not isinstance(error, (dict, str)))
                    or (final is not None and not isinstance(final, str))):
                raise ThreadUnavailable(MALFORMED_CODEX_EVENT)
            latest = TurnState(turn_id, "failed" if error else "completed")
            completions[turn_id] = final
        elif kind == "turn_aborted":
            latest = TurnState(turn_id, "interrupted")
        elif message := codex_agent_message(payload.get("item"), turn_id):
            messages.append(message)
    return Scan(latest, messages, completions)


def codex_snapshot(summary, scan, max_messages):
    latest, messages = scan.latest, scan.messages
    expected = scan.completions.get(latest.turn_id) if latest.status == "completed" else None
    produced = [message.text for message in messages if message.turn_id == latest.turn_id]
    if isinstance(expected, str) and expected.strip() and (not produced or produced[-1].strip() != expected.strip()):
        # No message at all for a turn that recorded one means the records changed shape: older turns' text would
        # otherwise pass for this one's.
        raise ThreadUnavailable("The thread's final message does not match its turn record.")
    if latest.status == "inProgress":
        # Codex removes the lock file when its writer stops cleanly, so only its absence is conclusive.
        latest = TurnState(latest.turn_id, latest.status, "unknown" if summary.writer_lock_present else "abandoned")
    return ThreadSnapshot(summary, latest, keep_recent(messages, max_messages))


# Claude Code


def is_main_claude_record(entry):
    return entry.get("isSidechain", False) is False


def main_claude_records(records, identifier):
    for entry in records:
        if is_main_claude_record(entry):
            if identifier is not None and "sessionId" in entry:
                require_history_id(entry["sessionId"], identifier)
            yield entry


def claude_head(lines, identifier):
    """Return the session's first recorded working directory and its entrypoint, None when it records none."""
    for line in lines:
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(entry, dict) and is_main_claude_record(entry) and isinstance(entry.get("cwd"), str):
            require_history_id(entry.get("sessionId"), identifier)
            entrypoint = entry.get("entrypoint")
            return entry["cwd"], entrypoint if isinstance(entrypoint, str) and entrypoint else None
    return None, None


def claude_last_cwd(records, identifier=None):
    """The working directory the session's most recent records name: a session can move after it starts."""
    cwd = None
    for entry in main_claude_records(records, identifier):
        if isinstance(entry.get("cwd"), str):
            cwd = entry["cwd"]
    return cwd


def claude_cwd_search(identifier):
    """Search for the working directory the session's latest records name. A large record without one can follow the
    latest move, so only a named directory decides; if none is named once no earlier records remain, which repository
    the session is in is unknown, and the session is refused."""
    def last(_cwd):
        raise ThreadUnavailable(NO_CLAUDE_CWD)

    return tail_search(lambda records: claude_last_cwd(records, identifier), lambda cwd: cwd is not None, last)


def claude_turns_search(belongs, identifier, max_messages):
    """turns_search over a Claude session. Only a window that starts at byte zero holds the whole ancestry: in one cut
    from the file's end, a gap may be closed by earlier records."""
    return turns_search(
        lambda records: scan_claude(records, belongs, identifier=identifier, whole=records.from_start), max_messages)


def claude_title_search(belongs, identifier):
    """Search for the scan holding the title of the session's current stay in the repository: the stay's start
    decides, else the last window's records. Only the active branch can enter the repository, as for replies; if its
    ancestry is still unresolved in the last window, the session has no title here."""
    return tail_search(
        lambda records: scan_claude(records, belongs, title_only=True, identifier=identifier, whole=records.from_start),
        lambda scan: scan.segment_start, lambda scan: Scan(None, []) if scan is EARLIER else scan)


def is_interactive_claude_session(entrypoint):
    """Print-mode sessions, bridge consultations among them, have no person at the keyboard; a session that records
    no entrypoint cannot be told apart, so it is left out."""
    return isinstance(entrypoint, str) and bool(entrypoint) and not entrypoint.startswith("sdk")


def keep_title(titles, entry):
    for kind, key in CLAUDE_TITLE_RECORDS:
        if entry.get("type") == kind and isinstance(entry.get(key), str):
            titles[kind] = entry[key]


def chosen_title(titles):
    return titles.get("custom-title") or titles.get("ai-title")


def claude_active_branch(records, identifier, whole):
    """Return the last main node's ancestry, None for histories without parent links, or EARLIER when the ancestry
    reaches a parent before records cut from the file's end."""
    parents = {}
    unlinked_chat = False
    for entry in main_claude_records(records, identifier):
        if "parentUuid" not in entry:
            unlinked_chat |= entry.get("type") in ("user", "assistant")
            continue
        node, parent = entry.get("uuid"), entry["parentUuid"]
        if not isinstance(node, str) or node in parents or (parent is not None and not isinstance(parent, str)):
            raise ThreadUnavailable("The Claude session's history has malformed ancestry.")
        parents[node] = parent, len(parents)
    if not parents:
        return None
    if unlinked_chat:
        raise ThreadUnavailable("The Claude session's history has missing ancestry links.")

    active, position = set(), len(parents)
    node = next(reversed(parents))
    while node in parents:
        parent, preceding_position = parents[node]
        if preceding_position >= position:
            raise ThreadUnavailable("The Claude session's history has malformed ancestry.")
        active.add(node)
        node, position = parent, preceding_position
    # The oldest node of a cut-off window can have a parent just outside it. A gap later in the window needs a
    # wider scan to find out where the session resumed; in the whole file, no record can close it.
    if node is not None and position != 0 and not whole:
        return EARLIER
    if node is not None and whole:
        raise ThreadUnavailable(ANCESTRY_UNRESOLVED)
    return active


def scan_claude(records, belongs, *, title_only=False, identifier=None, whole=True):
    """Find the latest turn, agent messages and title in the session's current stay in the requested repository, or
    EARLIER when the active branch reaches a parent before records cut from the file's end.

    A record that names a working directory sets where the following records are; belongs says whether a directory is
    in the requested repository, worktrees included. A move outside it drops everything found so far, also when the
    session comes back later, and records before the first one that names a directory are not counted: at a window
    cut from the file's end, nothing says where they were. title_only reads no message fields, only where the
    session is and its title records.
    """
    active = claude_active_branch(records, identifier, whole)
    if active is EARLIER:
        return EARLIER
    latest, messages, turn_id, titles = None, [], None, {}
    inside, segment_start = None, False
    for entry in main_claude_records(records, identifier):
        node = entry.get("uuid")
        on_branch = active is None or (isinstance(node, str) and node in active)
        cwd = entry.get("cwd")
        if isinstance(cwd, str):
            here = belongs(cwd)
            if not here:
                latest, messages, turn_id, titles = None, [], None, {}
                inside = False
            elif on_branch:
                # An abandoned branch cannot grant ownership, but a recorded move out still ends the stay.
                if inside is False:
                    segment_start = True
                inside = True
        if not inside:
            continue
        keep_title(titles, entry)
        if title_only or not on_branch or entry.get("isMeta"):
            continue
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        content = message.get("content")
        if entry.get("type") == "user":
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            if any(not isinstance(block, dict) or block.get("type") == "tool_result" for block in blocks):
                continue
            texts = [block.get("text") for block in blocks if block.get("type") == "text"]
            # A text block, or a prompt that is not a list of blocks, must carry its text: an empty or absent one
            # would pass for a new question.
            if any(not isinstance(text, str) for text in texts):
                raise ThreadUnavailable(MALFORMED_CLAUDE_MESSAGE)
            text = "".join(texts)
            if text in CLAUDE_INTERRUPTION_MARKERS:
                latest = TurnState(turn_id, "interrupted")
            else:
                turn_id = entry.get("uuid")
                latest = TurnState(turn_id, "inProgress")
        elif entry.get("type") == "assistant":
            if entry.get("isApiErrorMessage"):
                latest = TurnState(turn_id, "failed")
                continue
            stop_reason = message.get("stop_reason")
            if stop_reason is not None and not isinstance(stop_reason, str):
                raise ThreadUnavailable(MALFORMED_CLAUDE_MESSAGE)
            text = text_of(content, "text")
            if text.strip():
                messages.append(AgentMessage(turn_id, text))
            latest = TurnState(turn_id, CLAUDE_STOP_STATUSES.get(stop_reason))
    return Scan(latest, messages, title=chosen_title(titles), segment_start=segment_start)


def claude_snapshot(summary, scan, max_messages):
    latest = scan.latest
    if latest.status is None:
        raise ThreadUnavailable("The session's latest reply stopped for a reason this reader does not know.")
    if latest.status == "inProgress":
        latest = TurnState(latest.turn_id, latest.status, "unknown")
    return ThreadSnapshot(summary, latest, keep_recent(scan.messages, max_messages))


# Git


def git_output_path(stdout):
    """The path git printed on one line, as bytes: only the line's own end is removed, so a path that ends in spaces
    stays itself. None when git printed nothing."""
    if stdout.endswith(b"\n"):
        stdout = stdout[:-1]
    return os.fsdecode(stdout) if stdout else None


# Results


def iso_time(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def newest_first(listing):
    return replace(listing, threads=sorted(listing.threads, key=lambda summary: summary.recency_at, reverse=True))


def listing_result(provider, listing, limit):
    result = {"provider": provider, "threads": [
        {"id": summary.id, "name": summary.name, "updated_at": iso_time(summary.recency_at),
         "archived": summary.archived, "writer_lock_file": summary.writer_lock_present}
        for summary in listing.threads[:limit]
    ]}
    if listing.unsupported:
        # Not limited to this repository, so it reveals nothing about which threads belong to it.
        result["unsupported_threads_machine_wide"] = len(listing.unsupported)
    if listing.unreadable_directories:
        result["unreadable_history_directories_machine_wide"] = listing.unreadable_directories
    return result


def match_thread(threads, thread):
    """Pick the one listed thread whose ID or exact name is thread."""
    matches = [summary for summary in threads if thread in (summary.id, summary.name)]
    if not matches:
        # The same answer for an unsupported history, which may belong to another repository, as for no thread.
        raise ThreadUnavailable(NOT_IN_REPOSITORY)
    if len(matches) > 1:
        raise ThreadUnavailable("Several threads have that name. Pass the thread's ID instead.")
    return matches[0]


def cap_messages(messages, budget=MAX_MESSAGE_CHARACTERS):
    """Keep the newest text within budget characters, cutting the oldest kept message from its start.

    Returns the kept messages, oldest first, and whether anything was cut or left out.
    """
    kept, truncated = [], False
    for message in reversed(messages):
        text = message.text
        if len(text) > budget:
            text, truncated = text[len(text) - budget:], True
        if text:
            kept.insert(0, {"text": text, "phase": message.phase})
        budget -= len(text)
        if budget <= 0:
            truncated = truncated or len(kept) < len(messages)
            break
    return kept, truncated


def snapshot_result(provider, snapshot):
    messages, truncated = cap_messages(snapshot.messages)
    latest = snapshot.latest_turn
    return {
        "provider": provider, "id": snapshot.summary.id, "name": snapshot.summary.name,
        "updated_at": iso_time(snapshot.summary.recency_at),
        "latest_turn": {"status": latest.status, "finished": latest.status in FINISHED_STATUSES,
                        "liveness": latest.liveness},
        "messages": messages, "truncated": truncated,
    }

