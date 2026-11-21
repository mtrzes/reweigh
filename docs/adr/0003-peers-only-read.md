# ADR-0003: reweigh only reads; split work runs in each app's own worktree

- Status: Accepted
- Enforced by: test_core `test_claude_command` and `test_codex_command` (read-only tools and sandbox);
  test_contracts `test_tools_their_annotations_and_envelopes` (only the list, read and get tools are marked read-only);
  test_contracts `test_the_installed_codex_knows_every_name_the_bridge_passes` and
  `test_the_installed_claude_keeps_every_restriction_and_offers_every_tool` (the installed CLIs keep the restrictions)

## Context
Some work could be split between Claude Code and Codex, and a peer that edits would allow it. A delegate tool was
designed and reviewed: the peer writing in its own worktree and branch after the operator confirms each job in the
host. It cannot keep that promise. An MCP elicitation accept does not prove a person answered it (hooks, SDK
handlers and computer use can answer), nothing binds the answer to the job that runs, and the lead agent has a shell
anyway. Configuration the worktree carries can widen the peer's rules, escapes by socket, loopback and Linux were
untested, and the check on the caller's tree cannot tell the peer's writes from the caller's own. Closing these would
add about a quarter to the code and a second mode to every review. Both CLIs already start a session in a new git
worktree of their own (`claude --worktree`, `codex --worktree`).

## Decision
- Every peer reweigh starts is restricted to reading by its own CLI: Claude to read and search tools, Codex to its
  read-only sandbox, where it can run commands that cannot write. SECURITY.md names the one known exception and how to
  turn it off.
- A tool that consults or starts a consultation is not marked read-only, though the peer only reads: it sends repository
  content to the other provider, so a host that skips approval for read-only tools does not skip them; whether it asks
  is the host's decision. Every tool carries `readOnlyHint`; only the list, read and get tools set it to true.
  Annotations are hints a host may ignore.
- Split work goes through the apps themselves: the lead puts the handoff in its reply; the operator starts the other
  app in its own worktree; the lead follows it with `read_codex_thread` or `read_claude_thread` and reviews the
  resulting branch with `ask_*` and `ref`.
- If writing delegation is ever built, it is a separate tool with its own repository and tests, sharing at most
  `peer_guard.py` and the environment rules, and reweigh does not depend on it.

## Consequences
reweigh stays small. Splitting work costs the operator one step: starting the other app with the handoff.

## Reconsider if
A host can prove that a person, not an agent, confirmed a specific job; or either app gains a supported interface for
writing into its sessions.
