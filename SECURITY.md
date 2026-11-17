# Security

peer-bridge lets one coding agent ask another a question. It is built so that the peer it starts cannot change
anything: it does not make what the peer reads, or what the agents do with the reply, safe. This page says where that
line runs.

## What the bridge enforces

- **The peer cannot write.** Claude runs with only Read, Glob and Grep, in `dontAsk` mode, with no MCP servers, slash
  commands or project settings, and ordinary hooks disabled; administrator-managed hooks follow Claude Code's managed
  policy. Codex runs in its read-only sandbox, without the user's "always allow" rules, plugins, MCP servers, apps or
  web search. A peer that tries to edit, run a command that writes, or reach the network is refused by its own CLI's
  restrictions; the bridge chooses those restrictions and checks the exact command line in its tests.
- **A consultation does not nest by accident.** The bridge marks the peer's environment, and the commands Codex runs,
  and refuses to start inside a marked one. This guards against recursion, not against a process that drops the mark.
- **Pinning runs no repository code.** Preparing a pinned checkout runs no git hook, file-system monitor or filter
  driver, including one configured only for linked worktrees; git uses no transport, so a partial clone's missing
  object fails instead of being fetched, and no replacement object, so the checkout holds the commit named; Git LFS
  downloads nothing; and no inherited `GIT_*` variable reaches the bridge's git. Removing the checkout touches no other
  worktree of the repository. This assumes the repository's configuration does not change while the checkout is made.
- **Nothing runs from the consulted directory by name.** The peer is found, and launched with, a `PATH` of absolute
  entries only, so a file in the repository named like a command never runs in place of it.
- **The peer's process group ends with the consultation**, also when the bridge is killed, and so do the sign-in
  probes and git commands it runs during a consultation. Their guards all hold the conversation lock until their
  process groups stop, so no other consultation takes over a checkout that is still in use.
- **State files are private.** The state directory and everything in it are readable by the user only, and the bridge
  opens its files without following symlinks. Cleanup checks that the state directory and its folders are private,
  real directories before it lists or removes anything in them.
- **The tools' read-only hint.** `ask_*` and `start_*` are annotated read-only: they change nothing in the user's
  project. They do write the bridge's own records, jobs and checkouts in its state directory.

## What it does not protect against

- **Reading.** A Codex peer can read any file the user can read, home folder included. A Claude peer reads inside the
  consulted directory, Claude Code's own temporary folder, and wherever the user's own Claude settings add a directory
  or allow a read. Anything a peer reads goes to its model
  provider and into its CLI's history, and can come back in the reply.
- **Prompt injection.** Files in the repository, its instructions (AGENTS.md, CLAUDE.md) and seeded threads can steer
  the peer. Its reply is advice from a model that may have read hostile text; check it before acting on it.
- **The lead agent.** A lead agent can route any question it likes through a peer, including one that reads files its
  own rules would deny it.
- **Tokens in the environment.** Commands Codex runs see only the core environment, but both peer CLIs inherit the
  bridge's environment for their own authentication.
- **Descendants that leave the process group.** A process that starts its own session or group is not stopped with
  the consultation; it stays inside the peer's sandbox.
- **Other processes running as the same user.** They can read the state directory, both apps' histories and the peers'
  transcripts.
- **Managed policy.** Administrator settings for either CLI still apply and can change what a peer may do.

## What is kept, and for how long

- A background consultation's reply stays in the state directory until a later background start removes it, at the
  earliest seven days after the job finished; with no later start it stays until the user removes it. Its prompt and
  context are deleted as soon as the runner has read them. If SIGTERM or SIGHUP interrupts the CLI while it creates
  a job, the bridge deletes that job's prompt and context during cancellation.
- Conversation records, which bind a conversation to its peer and project, are kept until the user removes them.
- Each peer CLI keeps its own transcript of every consultation, as long as it keeps any history.
- The developer tool `dev/codex_app_server.py` keeps a full copy of the Codex history, without credentials, until it is
  removed. Run it only on a machine nobody else uses.

## Advice

Treat replies as untrusted input. Do not consult a peer on a repository that holds secrets you would not paste into
the chat. Keep tokens you do not want a peer CLI to hold out of the environment it starts in.

## Reporting a vulnerability

Report it privately through GitHub's private vulnerability reporting on this repository: open the Security tab and
choose "Report a vulnerability". Please do not open a public issue for it.
