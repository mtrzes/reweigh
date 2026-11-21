# ADR-0003: peer-bridge only reads; split work runs in each app's own worktree

- Status: Accepted
- Enforced by: test_core `test_claude_command` and `test_codex_command` (read-only tools and sandbox);
  test_contracts `test_tools_their_annotations_and_envelopes` (only the list, read and get tools are marked read-only)

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
- Every peer peer-bridge starts only reads: Claude with Read, Glob, Grep, WebSearch, Skill and Agent, none of which
  writes, Codex in its read-only sandbox. Codex can
  run commands, which its sandbox keeps from writing. No tool, flag or command lets a peer edit, write files or write to
  a session.
- A tool that consults or starts a consultation is not marked read-only, though the peer only reads: it sends
  repository content to the other provider, so a host that skips approval for read-only tools still asks. Only
  the list, read and get tools carry `readOnlyHint`. Annotations are hints a host may ignore.
- Split work goes through the apps themselves: the lead puts the handoff in its reply; the operator starts the other
  app in its own worktree; the lead follows it with `read_codex_thread` or `read_claude_thread` and reviews the
  resulting branch with `ask_*` and `ref`.
- If writing delegation is ever built, it is a separate tool with its own repository and tests, sharing at most
  `peer_guard.py` and the environment rules, and peer-bridge does not depend on it.

## Consequences
peer-bridge stays small and its read-only claim stays simple to check. Splitting work costs the operator one step:
starting the other app with the handoff.

## Reconsider if
A host can prove that a person, not an agent, confirmed a specific job; or either app gains a supported interface for
writing into its sessions.
