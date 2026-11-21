# ADR-0007: An approved item has a line budget; a check becomes code only when it is cheap and exact

- Status: Accepted
- Enforced by: review; standing duty: each commit body gives net production and test lines per approved item

## Context
The 2026-11-20 security walkthrough approved about 25 items. Its branch went through twelve review rounds, and the later
rounds' findings were mostly in code the earlier rounds' fixes had added; an object-store containment check was cut
after it kept producing new P1 findings. After two trim passes it still landed with about 460 net production and 920
net test lines. Claude Code and Codex change often, so every name the bridge checks at runtime is code that has to keep
up with them.

## Decision
- A check becomes code only when it reads an exact token, field, exit status or file existence and fits the budget;
  otherwise `SECURITY.md` states the limit.
- An approved item gets about 100 net production lines. Tests are reported, not capped. An item over the budget stops
  for the operator's sign-off, with the count and what the lines buy.
- A review loop stops when most of a round's findings are in code the previous round added; the design goes back to
  the operator to cut or simplify.
- A check that the installed CLIs still honour the bridge's restrictions runs before each consultation only when it
  needs no model call, no sign-in and no planted files. Anything else runs as a release contract test on the release
  commit ([ADR 0002](0002-test-architecture.md), [ADR 0006](0006-releases-and-stored-state.md)).

## Consequences
- Some hostile-repository findings stay documented in `SECURITY.md` rather than closed.
- Large fixes arrive split, each with its own sign-off.
- A rename that only a canary would catch goes unnoticed until the next release.

## Reconsider if
The bridge gains maintainers, or users who routinely consult untrusted repositories; or a restriction is lost between
releases in practice.
