# peer-bridge TODO

## Before the repository goes public

- [x] Licence: Apache-2.0 (`LICENSE`).
- [x] Root README: what it does, a pinned review to try, what the peer can read, install.
- [ ] GitHub Actions CI: `.github/workflows/tests.yml` runs both unit suites on macOS and Linux, Python 3.11 to 3.13.
      It has not run on GitHub yet, so Linux is still untested.
- [ ] Compatibility matrix of verified Claude Code and Codex CLI versions.
- [x] `CHANGELOG.md` and `SECURITY.md`.
- [ ] A short demo: a real transcript excerpt of a pinned review and one follow-up, about eight lines, in the root
      README. Text, not a GIF, so it can be searched and copied.
- [ ] Make the repository public, then turn on GitHub's private vulnerability reporting, which SECURITY.md names;
      GitHub offers it only for public repositories.

Every release bumps the version in both `plugin.json` files and adds a `CHANGELOG.md` entry: Codex caches an installed
plugin by version, so a release under an unchanged version does not reach it. A dependency change in `bridge.py`'s
script header is followed by `uv lock --script plugins/peer-bridge/scripts/bridge.py`.

Later hardening, from the 1.0.0 reviews:

- [ ] Pass the `get_consultation` deadline into state-lock acquisition. `bridge.py` owns `wait_seconds` and was outside the scope of the background-job fixes.
- [ ] Expire conversation records, and limit how many jobs and how large a prompt and context a start accepts.
- [ ] Make the model-catalogue check advisory, so a stale `models_cache.json` cannot block a new model.
- [ ] Contain a peer's whole process tree, not only its group.
- [ ] Linux: require a local state directory (flock is per process on NFS) and name a missing Codex sandbox.
- [ ] Make seed thread enumeration cancellable. Its repository checks run `git` through `subprocess.run` in a worker thread that cancellation cannot stop.

Thread reader (opt-in), from the review of its P2 fixes:

- [ ] A read returns `name: null` when the title sits before the scanned window; take it from a title-only pass.
- [ ] A session that never moved never marks the start of its stay, so listings rescan it up to the 32 MiB cap.
- [ ] Refuse malformed Claude content blocks with a fixed reason instead of skipping them or failing on a join.
- [ ] Walk history folders with `os.scandir` and skip one that vanishes mid-listing (`rglob` raises).
- [ ] Cap distinct `git rev-parse` probes per read; decide whether the Claude leaf comes from `last-prompt.leafUuid`
      and whether a rewound stay elsewhere should still end the current one.

Parity tool (`dev/`), from the review of its P2 fixes:

- [ ] Bound `pages()` by page count or an overall deadline, and give `compare()` and the clone lock an overall budget.
- [ ] Kill an app-server group left by a parity run that was itself killed, before the next run uses the copy.
- [ ] Confine the bundled executable's real path to the signed ChatGPT.app bundle.
- [ ] fsync the copy record and reports before their rename, and rebuild an unreadable copy record.
- [ ] Time out codesign, ps and cp; record a server request that arrives after the 0.25 s shutdown drain.

## Move the Codex thread reader to Codex's app-server API

`list_codex_threads` and `read_codex_thread` parse Codex's session files (`scripts/thread_reader.py::CodexFilesSource`),
an internal format Codex can change in any release. The replacement is an app-server source behind `ThreadSource`,
reading through `thread/list` and `thread/items/list`; [ADR 0001](../../docs/adr/0001-codex-thread-reader-source.md)
sets when it may take over. The developer tool `dev/codex_app_server.py` already runs ChatGPT.app's signed Codex
executable on a sandboxed clone, sends only the allowed requests, refuses server requests, and compares every readable
thread with the file reader.

Left before it can replace the file reader (run `uv run --no-project --script dev/codex_app_server.py` after each Codex
update):

- [ ] Find out why the app-server does not return 9 threads the file reader reads (run of 2026-11-16 on Codex
      0.169.0-alpha.11.1; earlier runs had none missing).

- [ ] Decide whether Codex writing its own log database (`logs_2.sqlite`) inside the clone fails the check. It is the
      only change the runs so far found; see ADR 0001's Consequences.
- [ ] Pass the parity check on two consecutive Codex releases over the same clone, and confirm no release was skipped
      between them; the check cannot tell offline.
- [ ] Once it replaces the reader, read a fresh clone of the Codex home on each use, never the live home: an app-server
      start deletes other Codex processes' temporary folders.
- [ ] Returned Codex turns are bound only to the rollout's `session_meta.cwd`. A turn that records its own directory
      (`turn_context.payload.cwd`) is not checked against the repository; binding turns to it would add `turn_context`
      to the records ADR 0001 lists, so it needs that ADR changed first.

Reconsider the order if a Codex release breaks the file reader and no parser fix restores it, if Codex Desktop's own
app-server becomes reachable without starting a second one, or if forked threads must become readable.

## Parked: Gemini CLI peer

Add Gemini CLI as a third peer behind the same adapter interface, with the same read-only tool restrictions, nesting guard
and environment scrubbing as the Claude and Codex adapters.
