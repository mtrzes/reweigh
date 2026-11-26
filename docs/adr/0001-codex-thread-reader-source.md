# ADR-0001: Codex threads are read from rollout files; the app-server replaces that only after proven parity

- Status: Accepted
- Enforced by: test_reader (compressed and refused histories)

## Context
Claude Code needs the latest agent messages of a Codex Desktop thread in the same repository, because the desktop
holds each open thread's only writer and a consultation cannot continue it. Codex keeps that history in rollout files,
an internal format that already appears both plain and zstd-compressed, and serves it through an experimental
app-server. A second app-server on the live Codex home deletes other Codex processes' temporary folders on start,
writes its own log database, and was seen returning incomplete history and mislabelling a running turn.

## Decision
- `CodexFilesSource`, behind `ThreadSource` in `scripts/thread_reader.py`, reads rollouts read-only: plain `.jsonl`
  from its tail, `.jsonl.zst` decompressed once in bounded memory. It opens no database or lock file and starts no
  process except git.
- It uses only `session_meta`, `task_started`, `task_complete`, `turn_aborted` and agent-message `item_completed`
  records, lists only Desktop and interactive CLI threads, and takes a turn's status from the rollout's last lifecycle
  event. It refuses, with a named reason, a forked thread, a duplicated rollout or unknown format, a malformed line, a
  final message that disagrees with its turn record, and a compressed file that is cut off, ends inside a record,
  is damaged or changes while it is read.
- The app-server source is not in main: the developer tool that ran it on a sandboxed clone of the Codex home and
  compared every thread with the file reader was last present at commit `6cb1b532aefed0375110766dc43ab7b05c73419d`.
- It may replace the file reader only when the parity check passes on two consecutive Codex releases over the same
  clone, the later one newer: at least one thread is compared, every Desktop and CLI thread the file reader can read
  matches on latest turn, status and the three most recent agent messages in order, none is missing, the server sends
  no request, and none of the clone's rollouts, index or databases changes. A database the clone never held, such as the
  server's log database, does not count. A run that fails to start or to complete counts as not passed. Once it
  replaces the reader, it reads a fresh clone on each use, never the live home.
- Whenever the two sources disagree, the file result stands, or the read is refused with a reason.

## Consequences
Every Desktop and CLI thread that is in a readable format and passes the repository check is visible, and a thread the
reader cannot be sure of is refused rather than guessed. The parser follows Codex's internal format, so a Codex release
can break it until a fix or the app-server takes over. Nothing in main compares the file reader with the app-server
after a Codex update.

## Reconsider if
A Codex release breaks the file reader and no parser fix restores it; Codex Desktop's own app-server becomes reachable
without starting a second one; or forked threads must become readable. The thread tools stay experimental, off unless
`REWEIGH_THREADS=1`; they graduate once the reader binds each Codex turn to the directory it ran in, not only the
session's, and the parity check has passed on two consecutive Codex releases.
