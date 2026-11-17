# ADR-0001: Codex threads are read from rollout files; the app-server replaces that only after proven parity

- Status: Proposed
- Enforced by: test_reader (compressed and refused histories); dev/tests test_app_server (message gate, sandbox
  isolation, signed executable, parity comparison, fingerprints) and test_parity_rules (`test_parity_passed`,
  `test_parity_verdict`); standing duty: run `dev/codex_app_server.py` after each Codex update

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
- The app-server source is a developer tool outside the plugin, `dev/codex_app_server.py`. It runs only on an APFS clone
  containing the session and archived-session rollouts, the session index, and the supported thread state databases
  with their WAL files. It publishes that clone with its identity in one rename and holds an exclusive lock from copy
  selection through report publication. It removes symlinks and sets `HOME`, `CODEX_HOME` and `TMPDIR` inside a sandbox.
  It runs only the Codex executable inside ChatGPT.app, after the system codesign confirms the app's seal and signing
  team. It sends only `initialize`,
  `initialized`, `thread/list` with `useStateDbOnly`, `thread/read` without `includeTurns`, `thread/turns/list` and
  `thread/items/list`, and refuses every server request.
- It may replace the file reader only when the parity check passes on two consecutive Codex releases over the same
  clone, the later one newer: at least one thread is compared, every Desktop and CLI thread the file reader can read
  matches on latest turn, status and the three most recent agent messages in order, none is missing, the server sends
  no request, and the clone's rollouts and databases are unchanged. The source pages through older turns until it has
  enough messages or reaches the end of the history. Parity records the release and an incomplete attempt before
  starting the server, so startup and request failures count toward the verdict. Once it replaces the reader, it reads
  a fresh clone on each use, never the live home.
- Whenever the two sources disagree, the file result stands, or the read is refused with a reason.

## Consequences
Every Desktop and CLI thread in a readable format is visible, and a thread the reader cannot be sure of is refused
rather than guessed. The parser follows Codex's internal format, so a Codex release can break it until a fix or the
app-server takes over. The parity check currently fails because the app-server writes Codex's log database, which the
rule above counts as a change, and because it no longer returns some threads the file reader reads.

## Reconsider if
A Codex release breaks the file reader and no parser fix restores it; Codex Desktop's own app-server becomes reachable
without starting a second one; a decision is taken on whether log-database writes inside the clone fail the check; or
forked threads must become readable.
