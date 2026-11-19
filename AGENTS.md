# Agent rules (peer-bridge)

## Before you change

| Area | Read first | Enforced by |
|---|---|---|
| a tool, argument or job state | [HOW-IT-WORKS](plugins/peer-bridge/HOW-IT-WORKS.md) tables, the [plugin README](plugins/peer-bridge/README.md) section, `plugins/peer-bridge/tests/fixtures/mcp_input_schemas.json` | `test_contracts` |
| a peer's command line, environment or sandbox | [SECURITY](SECURITY.md), [ADR 0003](docs/adr/0003-peers-only-read.md) | `test_core` |
| failure diagnosis or any probe | [ADR 0004](docs/adr/0004-causes-from-exact-facts.md) | `test_core`, `test_bridge` |
| which module holds a rule | [ADR 0005](docs/adr/0005-module-boundaries.md) | `test_contracts` |
| thread reading | [HOW-IT-WORKS](plugins/peer-bridge/HOW-IT-WORKS.md#thread-reading), [ADR 0001](docs/adr/0001-codex-thread-reader-source.md) | `test_reader` |
| tests or CI | [ADR 0002](docs/adr/0002-test-architecture.md) | review |
| a release or stored state | [ADR 0006](docs/adr/0006-releases-and-stored-state.md), [CHANGELOG](CHANGELOG.md) | `test_contracts`, `test_jobs`, `test_core`, CI by hand |

Run both suites before each commit: [Verification](plugins/peer-bridge/README.md#verification).

## Rules

- Each fact has one home: behaviour in `plugins/peer-bridge/HOW-IT-WORKS.md`, arguments and setup in the plugin README,
  trust and retention in `SECURITY.md`; other files link to it. A change to the tools, job states, components or
  boundaries updates HOW-IT-WORKS in the same commit; `test_contracts` fails when a tool or job state is missing from
  it.
- Tests follow [ADR 0002](docs/adr/0002-test-architecture.md): each critical path proved once, at the lowest level.
  CI runs them for a pull request and, started by hand, on the release commit before it is tagged; the `v*` tag's run
  only checks that the tag names the version in `plugin.json`.
- Decisions go in `docs/adr/` with the sections Status, Enforced by, Context, Decision, Consequences and Reconsider if;
  current decision only; history is in git.
- Deterministic code reads exact tokens and structure; judgement about language goes to a model or the operator. An
  inconclusive probe or a failed call is could-not-tell, never absence
  ([ADR 0004](docs/adr/0004-causes-from-exact-facts.md)).
- Before committing a review fix, find every site with the same pattern (other callers, sibling except lists, other
  modes of a reused helper); fix each or name it in the commit body.
- A new helper, class, parameter or constant needs a non-test caller; a parameter only a test passes is refused. The
  commit body gives net production and test lines.
- A user-visible change adds a line under `## Unreleased` in `CHANGELOG.md` in the same commit; a release renames the
  heading.
- A rejected proposal likely to come back gets a why-comment at the site naming the test that holds it, or an ADR if
  it is a decision.
- Never rewrite a branch once pushed or merged; bring main in by merge, not rebase.
- Ask the operator before changing `AGENTS.md`, an ADR, the CI workflow or the test design, and before merging a branch
  that leaves a design question open.
- Commit a file only when something uses it: code, a test or its fixture, a user doc, an ADR, the changelog. Only
  `plugins/peer-bridge/` ships; `dev/` holds public maintainer tools. Session artifacts (plans, review transcripts,
  parity reports, scratch notes) stay out; what they settled goes in the commit message, an ADR, `HOW-IT-WORKS.md` or
  the backlog.
- The backlog, `plugins/peer-bridge/TODO.md`, is local only: it is gitignored and never committed, and a fresh clone or
  worktree does not have it.
