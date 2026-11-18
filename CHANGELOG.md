# Changelog

## Unreleased

- The consult-peer skill has two request shapes: a steelman, where the peer makes the strongest case for a change before
  its critique, and a double-crux, where the lead and the peer settle a disputed finding by a question about the pinned
  code that would change one side's mind.

## 1.0.0-beta.2

Hardening from architecture, security and implementation reviews of 1.0.0-beta.1.

- Thread reading and `seed_thread` are off unless `PEER_BRIDGE_THREADS=1` is set for the MCP server. The Codex plugin
  now forwards the bridge's settings (`PEER_BRIDGE_*`, `XDG_STATE_HOME`, `CLAUDE_CONFIG_DIR`) with `env_vars`.
- Pinned checkouts: git runs with no inherited `GIT_*` variable, no transport, no lazy fetch and no replacement
  objects; filters configured only for linked worktrees are disabled too; removal touches only the bridge's own
  worktree.
- The peer is found and launched with a `PATH` of absolute entries only. Sign-in probes and git commands run under the
  process guard, and a conversation stays locked until its guard has stopped the peer's group.
- Cleanup refuses a state directory, `jobs/` or `worktrees/` that is shared or a symlink before touching anything in it.
- Background jobs: a job that never starts is `lost` whichever call sees it first; a job whose state lock stays busy
  answers "The consultation job is busy. Try again." after a second; an unreadable request is deleted and the job
  `failed`; only a regular file marks a cancellation; a CLI `start` stopped by SIGTERM or SIGHUP cleans up its job.
- Thread reads check the repository on the same bytes they return, count a Claude Code session only from its latest
  stay in the repository, follow its active branch, refuse a history whose recorded ID does not match its file, and
  bound their scans.
- Codex replies containing U+2028 or other Unicode line separators no longer fail to parse.
- CI runs the unit tests on macOS and Linux with Python 3.11 to 3.13.

## 1.0.0-beta.1

First public release.

- Consultations: `ask_codex` and `ask_claude`, resumable with `conversation_id`, pinned to a commit with `ref`, seeded
  from the other app's thread with `seed_thread`; Codex's `model` and `reasoning_effort` per call.
- Background jobs: `start_codex_consultation`, `start_claude_consultation`, `get_consultation` and
  `cancel_consultation`, with the states `queued`, `running`, `completed`, `failed`, `cancelled` and `lost`, and
  `cancelling` while a cancelled job's runner stops.
- Thread reading (experimental): `list_*_threads` and `read_*_thread` over both apps' history files.
- Read-only peers: see [SECURITY.md](SECURITY.md) for what the bridge enforces and what it does not.
- A Claude Code plugin and a Codex plugin, each with its own marketplace in this repository.
