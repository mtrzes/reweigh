# Agent rules (peer-bridge)

- `plugins/peer-bridge/HOW-IT-WORKS.md` describes current behaviour for users. A change to the tools, job states,
  components or boundaries updates it in the same commit; `test_contracts` fails when a tool or state is missing from it.
- Tests follow [ADR 0002](docs/adr/0002-test-architecture.md): each critical path proved once, at the lowest level.
  Run both suites locally before each commit ([Verification](plugins/peer-bridge/README.md#verification)); CI runs
  only for a release, started by hand on the release commit and again on its `v*` tag.
- Decisions go in `docs/adr/` with the sections Status, Enforced by, Context, Decision, Consequences and Reconsider if; current decision only; history is in git.
- Deterministic code reads exact tokens and structure; judgement about language goes to a model or the operator.
- Commit a file only when something uses it: code, a test or its fixture, a user doc, an ADR, the changelog.
  Session artifacts (plans, review transcripts, parity reports, scratch notes) stay out; what they settled goes in the
  commit message, an ADR, `HOW-IT-WORKS.md` or the backlog.
- The backlog, `plugins/peer-bridge/TODO.md`, is local only: it is gitignored and never committed, and a fresh clone or
  worktree does not have it.
