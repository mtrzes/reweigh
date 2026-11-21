# ADR-0005: Rules live in pure modules; adapters depend on them, and nothing depends on bridge.py

- Status: Accepted
- Enforced by: test_repository `test_imports_point_from_the_adapters_to_the_rules`

## Context
Much of the bridge is decisions: which flags a peer gets, which facts name a cause, which job state follows. ADR 0002
tests those decisions table-driven, without a process, a file or the environment, which works only if they live where no
process, file or environment is reached. The bridge has one script that assembles the MCP server, the command line and
the job runner, and adapters around it for jobs, thread reading, state files, cleanup and the process guard.

## Decision
- `consultation.py` and `thread_parsing.py` hold the shared decision logic. The adapters pass in environment values,
  paths and probe results, or callbacks that do the I/O; the rules start no process, open no file, and import no
  shipped module but each other. One exception today: `state_directory()` expands `~` through Python.
- An adapter keeps the rules bound to its I/O: `jobs.py` the request file's format and its lock waits,
  `thread_reader.py` the repository check made on the bytes it returns, `history_capture.py` its byte budgets.
- `bridge.py` assembles the server, the command line and the runner; no shipped module imports it. `jobs.run_job`
  takes the consultation as a callback, and a job's runner is started as `bridge.py run-job`.
- `peer_guard.py` uses only the standard library and runs under `python -I`, since it outlives the bridge's failures
  and stops the peer's process group.
- `preflight.py` runs the Codex pre-launch check through the guarded runner `bridge.py` passes in; its decisions
  (which names a command uses, which rows and exit statuses mean what) are rules in `consultation.py`.
- Adapters share `state_files.py` and `cleanup.py` as they need.

## Consequences
A rule change needs only a pure test. A new rule that needs I/O goes in its adapter, beside the I/O, and its test is an
integration test. The pure modules' purity is checked by imports only: a call into `os` or `pathlib` that touches the
system passes the guard and is left to review.

## Reconsider if
Jobs or thread reading must ship separately, or an import cycle or a measured startup cost calls for a tighter
boundary.
