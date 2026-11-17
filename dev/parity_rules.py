"""Pure rules for the shadow app-server source and the parity check: records in, decisions out.

A developer tool, not part of the shipped plugin: see codex_app_server.py and ADR 0001.
"""

from thread_parsing import (
    CODEX_MAIN_SOURCES, FINISHED_STATUSES, AgentMessage, ThreadSnapshot, ThreadSummary, ThreadUnavailable, TurnState,
    keep_recent,
)


def app_server_summary(thread, archived):
    """A ThreadSummary for a Desktop or interactive CLI thread from thread/list, else None."""
    if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
        return None
    source, cwd = thread.get("source"), thread.get("cwd")
    if not isinstance(source, str) or source not in CODEX_MAIN_SOURCES or not isinstance(cwd, str):
        return None
    recency = thread.get("recencyAt") or thread.get("updatedAt")
    name = thread.get("name")
    return ThreadSummary(
        id=thread["id"], name=name if isinstance(name, str) else None, cwd=cwd,
        recency_at=float(recency) if isinstance(recency, int) else 0.0, archived=archived, source_kind=source,
        forked_from_id=thread.get("forkedFromId") if isinstance(thread.get("forkedFromId"), str) else None,
    )


def app_server_agent_messages(entries):
    """The agent messages among thread/items/list entries, in order."""
    messages = []
    for entry in entries:
        item = entry.get("item") if isinstance(entry, dict) else None
        if isinstance(item, dict) and item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
            if item["text"].strip():
                phase = item.get("phase") if isinstance(item.get("phase"), str) else None
                messages.append(AgentMessage(entry.get("turnId"), item["text"], phase))
    return messages


def app_server_snapshot(summary, turns, messages_by_turn, max_messages):
    """A ThreadSnapshot from thread/turns/list (newest first) and each listed turn's agent messages."""
    if not turns or not isinstance(turns[0], dict):
        raise ThreadUnavailable("No turn was found in the thread's recent history.")
    latest = turns[0]
    if latest.get("status") not in {*FINISHED_STATUSES, "inProgress"}:
        raise ThreadUnavailable("The app-server reported a turn status this reader does not know.")
    messages = [message for turn in reversed(turns) for message in messages_by_turn.get(turn.get("id"), [])]
    return ThreadSnapshot(summary, TurnState(latest.get("id"), latest["status"]), keep_recent(messages, max_messages))


def final_message(snapshot):
    """The latest turn's last agent message, or None."""
    texts = [message.text for message in snapshot.messages if message.turn_id == snapshot.latest_turn.turn_id]
    return texts[-1].strip() if texts else None


def parity_differences(file_snapshot, app_snapshot):
    """The fields in which the app-server's view of a thread differs from the file reader's."""
    differences = []
    if file_snapshot.latest_turn.turn_id != app_snapshot.latest_turn.turn_id:
        differences.append("latest_turn")
    if file_snapshot.latest_turn.status != app_snapshot.latest_turn.status:
        differences.append("status")
    if final_message(file_snapshot) != final_message(app_snapshot):
        differences.append("final_agent_message")
    return differences


def parity_passed(report):
    """Whether one parity run passed: every thread matched, the copy's files stayed unchanged and the server sent no
    request. ADR 0001 requires the copy's rollouts and databases to stay unchanged, Codex's log database included."""
    # A run that compared no thread proves nothing.
    return report["threads_compared"] > 0 and not (
        report["missing"] or report["mismatches"] or report["thread_data_changes"]
        or report["other_database_changes"] or report["server_requests"])


def parity_verdict(reports):
    """Whether the app-server source may replace the file reader, by ADR 0001: on the latest report's copied Codex home,
    the latest run of each of the last two releases passed, in the order the runs happened, and the second is a newer
    Codex version than the first. Whether a release was skipped in between cannot be told offline.

    Consecutive runs of one Codex version form one release block. A failed later run of a release decides that block,
    so pass on A, fail on B, pass on C is not ready: B failed between them.
    """
    if not reports:
        return False, "No parity report yet."
    latest = max(reports, key=lambda report: report["ran_at"])
    runs = sorted((report for report in reports if report["copy_id"] == latest["copy_id"]),
                  key=lambda report: report["ran_at"])
    blocks = []
    for report in runs:
        if blocks and blocks[-1]["codex_version"] == report["codex_version"]:
            blocks[-1] = report
        else:
            blocks.append(report)
    if not blocks[-1]["passed"]:
        return False, "The latest parity report did not pass."
    if len(blocks) < 2:
        return False, "Parity must also pass on the previous Codex release over the same copied home."
    if not blocks[-2]["passed"]:
        return False, "Parity did not pass on the previous Codex release over the same copied home."
    older, newer = release_key(blocks[-2]["codex_version"]), release_key(blocks[-1]["codex_version"])
    if older is None or newer is None:
        return False, "A Codex version in the reports cannot be ordered."
    if newer <= older:
        return False, "The last two Codex releases checked are not an upgrade."
    return True, "Parity passed on two consecutive Codex releases over the same copied home."


def release_key(version):
    """An ordering key for `codex --version` output, by semantic-versioning precedence; None for any other text."""
    if not isinstance(version, str):
        return None
    name, _, token = version.strip().partition(" ")
    core, dash, prerelease = token.partition("-")
    numbers = core.split(".")
    if name != "codex-cli" or len(numbers) != 3 or not all(map(version_number, numbers)):
        return None
    release = tuple(map(int, numbers))
    if not dash:
        # A release sorts after every prerelease of the same version.
        return (*release, (1,))
    identifiers = []
    for part in prerelease.split("."):
        if not part or not all(char.isascii() and (char.isalnum() or char == "-") for char in part):
            return None
        if part.isdecimal():
            if not version_number(part):
                return None
            identifiers.append((0, int(part)))
        else:
            identifiers.append((1, part))
    return (*release, (0, *identifiers))


def version_number(token):
    return token.isascii() and token.isdecimal() and (token == "0" or token[0] != "0")
