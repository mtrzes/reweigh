# Security

peer-bridge lets one coding agent ask another a question. It is built so that the peer it starts cannot change
anything: it does not make what the peer reads, or what the agents do with the reply, safe. This page says where that
line runs.

## What the bridge enforces

- **The peer cannot write.** Claude runs with the read and search tools the
  [README](plugins/peer-bridge/README.md#runtime-boundaries) lists, in `dontAsk` mode, with no MCP servers or project
  settings, and ordinary hooks disabled; administrator-managed hooks follow Claude Code's managed policy. Codex runs in
  its read-only sandbox, without the user's "always allow" rules, plugins, MCP servers, skills' MCP installs or apps,
  and with web search limited to cached results from OpenAI's index. A peer that tries to edit, run a command that
  writes, or reach the network other than through its search tool is refused by its own CLI's restrictions; the bridge
  chooses those restrictions and checks the exact command line in its tests. The one known write, a Claude agent type
  with worktree isolation, is under Sub-agents below.
- **The restrictions are checked against the installed CLIs.** Before each Codex consultation, a lost restriction
  refuses the call; [HOW-IT-WORKS](plugins/peer-bridge/HOW-IT-WORKS.md#one-consultation) says how it is checked. When
  the check cannot tell, the peer starts if time remains and a successful result carries a warning. Claude Code's
  restrictions are checked only by the contract tests, not before each start.
- **What a consulted repository can and cannot do.** It cannot make the bridge run its code (a Codex peer may run it
  inside its read-only sandbox), or point it at another repository on your machine. It can still influence what the
  peer answers, and the hints the bridge gives when a consultation fails. This assumes the `PATH` the app starts the
  bridge with names no folder inside the repository.
- **A consultation does not nest by accident.** The bridge marks the peer's environment, and the commands Codex runs,
  and refuses to start inside a marked one; the Codex plugin forwards the mark. This guards against recursion, not
  against a process that drops the mark.
- **Pinning runs no repository code.** Preparing a pinned checkout runs no git hook, file-system monitor or filter
  driver, including one configured only for linked worktrees; git uses no transport, so a partial clone's missing object
  fails instead of being fetched, and no replacement object, so the checkout holds the commit named; Git LFS downloads
  nothing; and no inherited `GIT_*` variable reaches the bridge's git. Removing the checkout touches no other worktree
  of the repository. This assumes the repository's configuration does not change while the checkout is made. Pinning
  writes a temporary worktree record under `worktrees/` in the repository's common git directory (`git rev-parse
  --git-common-dir`), removed with the checkout, and is refused in a folder whose `.git` points at a git directory that
  does not point back, or whose `worktrees/` registry exists and is
  not a real folder inside it. A repository whose objects come from elsewhere, through `objects/info/alternates` or a
  link, can still show the peer another repository's files at a commit both share.
- **Nothing runs from the consulted directory by name.** The peer is found, and launched with, a `PATH` of absolute
  entries only, so a file in the repository named like a command never runs in place of it through a relative entry.
  An absolute entry that points into the repository is the user's own setting and is kept.
- **The peer's process group ends with the consultation**, also when the bridge is killed, and so do the sign-in
  probes and git commands it runs during a consultation. Their guards all hold the conversation lock until their
  process groups stop, so no other consultation takes over a checkout that is still in use.
- **State files are private.** The state directory and everything in it are readable by the user only, and the bridge
  opens its files without following symlinks. Cleanup checks that the state directory and its folders are private,
  real directories before it lists or removes anything in them. Job cleanup requires a canonical UUID in each job,
  staging or deletion directory name. Only a regular file can mark a job for cancellation.
- **Thread history is scoped to the requested repository.** Each read attempt checks recorded working directories again
  against the repository resolved at the start of the request. A Claude sidechain cannot change the main session's
  repository. Replies and titles come from the main session's current stay there, including titles used to list and
  select sessions. A read scoped to this repository answers alike for another repository's thread, a history that cannot
  be placed in a repository, and a missing one; only the listing's two machine-wide counts span all histories. A folder
  whose `.git` points at a git directory that does not point back belongs to no repository.
- **History filenames are checked against the app's records.** The bridge opens only regular files and follows
  no symlinks below the configured app home. A history's recorded session ID must match the filename.
- **The tools' read-only hint.** Only the list, read and get tools are annotated read-only. `ask_*` and `start_*` are
  not: they send repository content to the other provider, so a host that skips approval for read-only tools does not
  skip them. Whether the host asks is up to the host.

## What it does not protect against

- **Reading.** A Codex peer can read any file the user can read, home folder included. A Claude peer reads inside the
  consulted directory, any `extra_directories` the call names, Claude Code's own temporary folder, and wherever the
  user's own Claude settings add a directory or allow a read. Anything a peer reads goes to its model provider, can go
  into its web searches, goes into its CLI's history, and can come back in the reply.
- **Prompt injection.** Files in the repository, its instructions (AGENTS.md, CLAUDE.md), its skills (Codex) and
  seeded threads can steer the peer. Its reply is advice from a model that may have read hostile text; check it before
  acting on it. Opening a link, loading a remote image, or running a command from a reply can send private data to a
  third party.
- **Sub-agents.** A peer may start sub-agents unless `PEER_BRIDGE_SUBAGENTS=0`. Each reads the repository's
  instructions too, and each adds to the consultation's cost and time. That they keep the peer's restrictions is an
  observation, not a tested guarantee: a Codex sub-agent was seen with the read-only sandbox, and, from reading
  Claude Code's code, `--tools` also bounds its sub-agents. One of your own Claude agent types that sets
  `isolation: worktree` creates a git worktree in the consulted repository when the peer starts it: a write, which
  `PEER_BRIDGE_SUBAGENTS=0` avoids, as it also removes Skill, since a skill can fork a sub-agent.
- **Codex threads continued elsewhere.** A Codex thread is placed in a repository by the working directory its session
  started in. Turns it later ran in another directory, for example after resuming it elsewhere, are returned with it.
  This is one reason the thread tools are off unless `PEER_BRIDGE_THREADS=1`.
- **The lead agent.** A lead agent can route any question it likes through a peer, including one that reads files its
  own rules would deny it, and can list another repository's threads by naming its directory.
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
  context are deleted when the runner reads them. If the request is unreadable and the job record is valid, the
  runner deletes the request and records a fixed failure message. If SIGTERM or SIGHUP interrupts the CLI while it
  creates a job, the bridge deletes that job's prompt and context during cancellation.
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
