# Changelog

## Unreleased

- The consult-peer skill has two request shapes: a steelman, where the peer makes the strongest case for a change before
  its critique, and a double-crux, where the lead and the peer settle a disputed finding by a question about the pinned
  code that would change one side's mind.

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
