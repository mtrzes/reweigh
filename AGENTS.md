# Agent rules (peer-bridge)

- `plugins/peer-bridge/HOW-IT-WORKS.md` describes current behaviour for users. A change to the tools, job states,
  components or boundaries updates it in the same commit; `test_contracts` fails when a tool or state is missing from it.
- Tests follow [ADR 0002](docs/adr/0002-test-architecture.md): each critical path proved once, at the lowest level.
- Decisions go in `docs/adr/` with the sections Status, Enforced by, Context, Decision, Consequences and Reconsider if; current decision only; history is in git.
- Deterministic code reads exact tokens and structure; judgement about language goes to a model or the operator.
