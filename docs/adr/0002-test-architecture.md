# ADR-0002: Each critical path is proved once, at the lowest level that can prove it

- Status: Accepted
- Enforced by: standing duty: a change to a critical path lands with the test that proves it, shown to fail without
  the change; review deletes a test that repeats another; `.github/workflows/tests.yml` runs the suites for a pull
  request and when started by hand, and on a `v*` tag only checks that the tag names the plugin's version

## Context
The bridge stands between agents and processes, private input and persisted state. A mutation check found nine
breakages that the whole suite let pass, while a fifth of the suite repeated other tests and most of its run time was
spent waiting. One maintainer runs the suites on macOS; users run the bridge on macOS or Linux with whichever Python
3.11 or later `uv` picks on their machine, and outside contributors open pull requests.

## Decision
- Critical paths: safety boundaries (peer restrictions, nesting, environment, symlinks, sandbox), failure handling,
  private input, process lifetime, concurrency and persisted state, and the MCP and CLI contracts. Each has a test
  that fails when it breaks.
- Rules live in pure modules and are tested there, table-driven, with no process, file or environment.
- An integration test proves only what needs a process, a file or a lock, one behaviour per test.
- An interleaving is produced with hooks or locks, never with timing; every wait has a deadline.
- Tests run on temporary homes, isolated git config and fakes, never the user's CLIs, history or network.
- Shared fixtures and fakes live in `tests/support.py`; test files never import each other.
- A test that repeats what another proves, at the same or a lower level, is deleted.
- Both suites run locally before each commit, on Python 3.11. CI runs them on Linux with Python 3.11 for a pull request,
  and on macOS and Linux with Python 3.11 and 3.15 when started by hand on the release commit before it is tagged. The
  tag's own run only checks that the tag is `v` followed by the version in the plugin's `plugin.json`.

## Consequences
Most rule changes need only a pure test, which runs in milliseconds. Integration tests stay few. A fix is not done
until a test fails on the code before it. A contributor's pull request shows whether both suites pass before anyone
checks it out. A break that shows only on Python 3.15 surfaces at the release run, not at the commit that caused it;
one that shows only on Python 3.12 to 3.14 is not tested for. A commit tagged without the release run is not tested.

## Reconsider if
The suite runs too long to be run before every commit, a defect escapes on a path no test owns, a release run finds a
platform-only break whose cause takes long to find among the commits since the last release, or a user reports a break
on a Python version between 3.11 and the newest.
