# ADR-0002: Each critical path is proved once, at the lowest level that can prove it

- Status: Accepted
- Enforced by: standing duty: a change to a critical path lands with the test that proves it, shown to fail without
  the change; review deletes a test that repeats another

## Context
The bridge stands between agents and processes, private input and persisted state. A mutation check found nine
breakages that the whole suite let pass, while a fifth of the suite repeated other tests and most of its run time was
spent waiting.

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

## Consequences
Most rule changes need only a pure test, which runs in milliseconds. Integration tests stay few. A fix is not done
until a test fails on the code before it.

## Reconsider if
The suite runs too long to be run before every commit, or a defect escapes on a path no test owns.
