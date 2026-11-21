# peer-bridge

Consult Codex from Claude Code or Claude Code from Codex through local MCP tools. Each consultation starts a dedicated
CLI conversation; a bridge conversation ID lets follow-ups reuse its history. The bridge uses existing CLI authentication
and runs the peer in the project directory supplied with the request. It never writes to an existing desktop or terminal
chat, but it can read the latest agent messages of chats in the same repository. [HOW-IT-WORKS.md](HOW-IT-WORKS.md)
shows its shape and the paths a request takes.

[Setup](#setup) · [Consultation requests](#consultation-requests) · [Pinned refs](#pinned-refs) ·
[Background consultations](#background-consultations) · [Command line](#command-line-consultations) ·
[Reading desktop threads](#reading-desktop-threads) · [Runtime boundaries](#runtime-boundaries) ·
[Configuration](#configuration) · [State retention](#state-retention) · [Verification](#verification)

## Setup

Requires macOS or Linux and Python 3.11 or later. Install `uv`, Codex CLI and Claude Code on the machine running the MCP
server, and sign in to each peer CLI with its normal login flow. The script declares its dependencies in its header and
pins them in `scripts/bridge.py.lock`; `uv` installs them into a separate script environment.

Live consultations and follow-ups were verified on macOS with Codex CLI `0.162.0-alpha.17.2` and Claude Code
`2.1.286`; failure messages and compressed thread reading with Codex CLI `0.167.0-alpha.4.1` and Claude Code `2.1.309`.
On Linux, only the unit tests run, in CI, with Python 3.11 and 3.15.

On macOS, ChatGPT.app bundles the Codex CLI at `/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex`
without adding it to `PATH`. When `PEER_BRIDGE_CODEX_BIN` is unset and `codex` is not on `PATH`, the bridge uses that
bundled CLI, or the same path under `~/Applications`.

### Claude Code to Codex

Install the plugin in Claude Code 2.1.275 or later:

```text
/plugin install peer-bridge --marketplace mtrzes/peer-bridge
```

On earlier versions, add the marketplace first:

```text
/plugin marketplace add mtrzes/peer-bridge
/plugin install peer-bridge@peer-bridge
```

The plugin launches the bridge with `--peer codex`. Restart Claude Code if the new server does not appear in `/mcp`.

### Codex to Claude Code

The repository is also a Codex plugin marketplace. Add it and install the plugin:

```sh
codex plugin marketplace add mtrzes/peer-bridge
codex plugin add peer-bridge@peer-bridge
```

The plugin launches the bridge with `--peer claude` and brings the same `consult-peer` skill as the Claude Code plugin.
Codex installs a copy of the plugin; after updating the repository, run `codex plugin marketplace upgrade peer-bridge`
and add the plugin again. Restart Codex to load it.

Without the plugin, register the script directly and give it the same timeouts in the generated
`[mcp_servers.peer-bridge]` section of `~/.codex/config.toml` (`startup_timeout_sec = 60`, `tool_timeout_sec = 3900`),
and the `env_vars` list from the plugin's `codex-mcp.json`: Codex passes an MCP server only the environment variables
it names. `--directory` points `uv` at the plugin, so it never runs a Python environment from the folder Codex
starts in, and `--locked` refuses a lock file that no longer matches the script.

```sh
codex mcp add peer-bridge -- uv --directory /absolute/path/plugins/peer-bridge run --locked --no-project --no-config --script /absolute/path/plugins/peer-bridge/scripts/bridge.py --peer claude
```

The startup allowance covers the first `uv` dependency installation. For Claude Code versions that support
`MCP_TOOL_TIMEOUT`, launch Claude Code with `MCP_TOOL_TIMEOUT=3900000` to give consultations the same tool-call
allowance. The bridge's own timeout defaults to 600 seconds and goes up to 3600.

For a host that needs both tools, launch the script with `--peer both`.

## Consultation requests

`ask_codex` and `ask_claude` accept these arguments; `extra_directories` is Claude-only:

| Argument | Meaning |
|----------|---------|
| `prompt` | The scoped question or follow-up to send to the peer. |
| `working_directory` | An absolute path to an existing project directory. |
| `context` | Optional supporting evidence, such as a diff excerpt, relevant paths or test results; defaults to empty text. |
| `conversation_id` | Optional bridge UUID returned by an earlier request for this peer and directory. |
| `ref` | Optional commit, branch or tag to consult instead of the working tree; see [Pinned refs](#pinned-refs). |
| `seed_thread` | Optional ID or name of a thread in the peer's app (a Codex thread when asking Codex, a Claude Code session when asking Claude Code) whose latest agent messages join the context; see [Reading desktop threads](#reading-desktop-threads). |
| `model` | Optional: the model for this call, such as `gpt-6-astra` for Codex or `opus` for Claude; overrides `PEER_BRIDGE_CODEX_MODEL` or `PEER_BRIDGE_CLAUDE_MODEL`. |
| `reasoning_effort` | Optional: the reasoning effort for this call, such as `high` or `xhigh` (Codex) or `high` or `max` (Claude); overrides `PEER_BRIDGE_CODEX_REASONING_EFFORT` or `PEER_BRIDGE_CLAUDE_REASONING_EFFORT`. |
| `extra_directories` | Claude only, optional: up to 20 absolute paths of existing folders the peer may also read, passed as `--add-dir`; a `CLAUDE.md` in one of them is loaded as instructions too. A Codex peer already reads any file you can. |

`list_codex_models` (and the `codex-models` command) lists the models this Codex account offers, with each model's
reasoning efforts and default, from Codex's own catalogue (`models_cache.json` in `CODEX_HOME`). When that catalogue
is readable, a consultation whose model, or whose model's effort, it does not list is refused before Codex starts,
with the values it does list. Without a catalogue, or without a named model to check an effort against, the choice
goes to Codex unchecked.

For example, ask the available peer tool:

```json
{
  "prompt": "Compare the query behavior before and after this change. Explain any correctness issue with file references.",
  "working_directory": "/absolute/path/to/project",
  "context": "The change is in projects/querysets.py. Focus on paid donations and date filtering."
}
```

A successful response contains `provider`, `conversation_id` and `reply`; `model` and `reasoning_effort` when the
consultation chose them, from the request or the environment: the values the bridge passed to the peer CLI, which does
not report back what it ran with; and `warnings` when Codex's pre-launch check found a capability it no longer
accepts, or could not reach a verdict. Pass the returned ID on follow-ups, with the same peer and project directory.
Bridge metadata binds each ID to the provider and canonical directory; it does not accept a peer CLI's raw session ID. A
per-conversation file lock prevents overlapping resumes.

A failed consultation comes back as a fixed message; the CLI's own output never reaches the caller. The bridge names a
cause only when an exact field or a check establishes it, and never by reading the CLI's or the model's text:

| Message names | Established by |
|---------------|----------------|
| A thread held open elsewhere | On a Codex follow-up, a writer-lock file for the thread under `CODEX_HOME/thread-writer-locks` |
| A signed-out CLI | `claude auth status --json` reporting `loggedIn: false`, `codex login status` exiting 1, or Claude's result carrying `api_error_status` 401 |
| A conversation whose history is gone | On a follow-up, no session file for it under the peer's history directory, every folder of which could be listed |
| A model Claude Code does not know | A stderr line starting with Claude Code's `[claude-code:unrecognized_model]` tag |
| Codex outside a git repository | No `.git` entry in `working_directory` or any parent, the check Codex itself makes |

The bridge runs the sign-in check and the history lookup only while time remains from the consultation timeout, and
gives up looking for a `.git` entry after 10 seconds. If the checks and result fields establish no cause, the bridge
returns a generic error. When a Codex model or reasoning effort was chosen, per call or by environment, a failed Codex
consultation adds that the choice may be the cause, because Codex reports a rejected choice only in prose.

### Pinned refs

A peer reading the working tree while the calling agent keeps editing it reviews a moving target. With `ref`, the
bridge resolves the ref to a commit in the working directory's repository, checks that commit out as a detached
worktree under the state directory, and runs the peer in the same subdirectory of that checkout. The response adds
`commit`, the full hash of the checkout prepared; the peer may still have read files outside it. The handoff tells
the peer to read only inside that checkout and to say when something is absent at the commit; this is an instruction,
not a sandbox, because either peer can read outside it. The worktree is removed when the consultation ends, including
on timeout or cancellation, and the peer stays read-only. While it exists, git records it in the repository's
`.git/worktrees`. Pinning is refused in a folder whose `.git` points at a git directory that does not record it back,
as a folder unpacked from an archive can be set up to, or whose `.git/worktrees` is not a folder inside it.

Claude resumes a session only from the directory it ran in, so each conversation keeps one checkout path,
`worktrees/<conversation_id>`, recreated on every call. A follow-up without `ref` reads the conversation's pinned commit
again, and a follow-up with `ref` moves the conversation to the new commit. A conversation started without `ref` cannot
be pinned later; start a new one. Pinning registers the temporary worktree in the repository's `.git/worktrees` while
the consultation runs and leaves the working tree untouched.

### Background consultations

A review can take several minutes, and `ask_codex` or `ask_claude` holds the calling agent's turn for all of it.
`start_codex_consultation` and `start_claude_consultation` take the same arguments, check the request, start the
consultation in a detached runner, and return at once:

```json
{"job_id": "7047ca11-b7e9-4459-9956-82848805450f", "provider": "codex", "state": "queued"}
```

`get_consultation` takes the `job_id` and an optional `wait_seconds`, at most 60, and returns the job's `state`:
`queued`, `running`, `cancelling` while a cancelled job's runner stops, `completed`, `failed`, `cancelled`, or `lost`
when its runner stopped without a result or did not claim it within two minutes. A completed job adds the
`conversation_id` and `reply` an `ask_` tool returns, `commit` when pinned, `model` and `reasoning_effort` when
chosen, and `warnings` as for `ask_`; a failed, cancelled or lost one adds a fixed `error` message. `cancel_consultation` stops a running job, its
peer and the peer's child processes, and removes a pinned checkout. Each server sees only the jobs of the peers it
exposes. Each status read or cancellation can wait up to one second for the job's state lock before reporting that
the job is busy.

Each job runs in its own process, so it outlives a restart of the MCP server or the calling app, and a later server
reads its result. The runner deletes the request holding the prompt and context after trying to read it. The reply is kept as
[State retention](#state-retention) describes.

### Command-line consultations

The `ask` command runs a consultation from a shell and prints its result when it finishes:

```sh
uv --directory /absolute/path/plugins/peer-bridge run --locked --no-project --no-config --script /absolute/path/plugins/peer-bridge/scripts/bridge.py ask \
  --provider codex --working-directory /absolute/path/to/project --context-file "$PWD/review-context.md" < question.md
```

The prompt is read from stdin as UTF-8; `--context-file`, `--conversation-id`, `--ref`, `--seed-thread`, `--model`,
`--reasoning-effort` and the repeatable `--extra-directory` are optional and match the tool arguments. `uv --directory`
runs the command from the plugin folder, so give `--context-file` an absolute path. The command prints the tool's JSON
result and exits 0, or prints `{"error": "..."}` and exits 1. It applies the same restrictions, nesting guard, timeout,
environment cleanup and conversation store as the MCP tools, so a conversation started in one can continue in the other.
Stopping the command with SIGTERM or SIGHUP, even repeatedly, also stops the peer and its child processes and removes a
pinned checkout.

`start` takes the same arguments as `ask` and prints the new job. SIGTERM or SIGHUP during job creation cancels the
start and deletes the job's prompt and context. `job --job-id ID [--wait SECONDS]` and
`cancel --job-id ID` match `get_consultation` and `cancel_consultation`:

```sh
uv --directory /absolute/path/plugins/peer-bridge run --locked --no-project --no-config --script /absolute/path/plugins/peer-bridge/scripts/bridge.py start \
  --provider codex --working-directory /absolute/path/to/project < question.md
uv --directory /absolute/path/plugins/peer-bridge run --locked --no-project --no-config --script /absolute/path/plugins/peer-bridge/scripts/bridge.py job --job-id JOB_ID --wait 60
```

Use the peer's response as advice. The calling agent assesses it and applies the project's permission and review rules
before making changes or posting replies.

## Reading desktop threads

These tools are experimental and off unless `PEER_BRIDGE_THREADS=1` is set for the MCP server: they parse each app's
internal history files, which can change in any release, and refuse a file they cannot be sure of rather than guess.
`seed_thread` is off with them. In Claude Code, set the variable in the environment Claude Code starts from; the Codex
plugin forwards it from the environment Codex starts from.

`list_codex_threads` and `read_codex_thread` in Claude Code, and `list_claude_threads` and `read_claude_thread` in
Codex, read the other app's recent threads without writing to them, for example a review still open in Codex Desktop,
which no consultation can continue:

| Argument | Meaning |
|----------|---------|
| `working_directory` | An absolute path inside the git repository whose threads to read. |
| `limit` | Listing only: how many threads, newest first; defaults to 20, at most 100. |
| `thread` | Reading only: a thread ID or exact name from the listing. |
| `max_messages` | Reading only: how many of the latest agent messages; defaults to 3, at most 20. |

A thread is readable only when its recorded working directory belongs to the same git repository as
`working_directory`, its linked worktrees included; a submodule is a repository of its own. A thread whose worktree
has since been deleted is not readable. A
folder whose `.git` points at a git directory that does not record it back, as git does for its own worktrees and
submodules, belongs to no repository here. The read checks this
again on the same open file it reads the messages from, and refuses a file that changes while it is read, after one
more try. For Claude Code, replies, listed titles and exact-name selection use the main session's current stay in
the repository. After a rewind, replies follow the active branch. The reader looks back up to 32 MiB for the
main session's working directory and active branch links. It refuses a session when those records cannot establish
its directory or resolve a branch gap. Only agent messages come back, never prompts, tool calls, tool output or
reasoning, up to 24,000 characters. `latest_turn`
gives the turn's `status` (`completed`, `failed`, `interrupted` or `inProgress`) and `finished`; a Claude Code reply cut
off at its output limit counts as `completed` and a refusal as `failed`. For an unfinished turn, `liveness` is `abandoned` when
Codex keeps no writer-lock file for the thread, and `unknown` otherwise: the listing's `writer_lock_file` usually means
an app holds the thread, but a crash can leave the file behind, and Claude Code sessions record neither. Codex lists
only Desktop and interactive CLI threads and Claude Code only sessions with a person at the keyboard, so subagents and
bridge consultations stay out.

To build on a thread without touching it, pass its ID or name as `seed_thread` to `ask_codex` or `ask_claude`. The
bridge reads its three latest agent messages and adds them to the new consultation's context, quoted as reference
material. The CLI has the same reads:

```sh
uv --directory /absolute/path/plugins/peer-bridge run --locked --no-project --no-config --script /absolute/path/plugins/peer-bridge/scripts/bridge.py threads \
  --provider codex --working-directory /absolute/path/to/project
uv --directory /absolute/path/plugins/peer-bridge run --locked --no-project --no-config --script /absolute/path/plugins/peer-bridge/scripts/bridge.py read-thread \
  --provider codex --working-directory /absolute/path/to/project --thread "Review payment retries"
```

A history the reader does not know how to parse is refused with a reason. A Codex rollout it cannot place in a
repository, or a thread with more than one rollout file, is left out of the listing and reads like a missing thread;
the listing's `unsupported_threads_machine_wide` counts such rollouts across all of `CODEX_HOME`, not only this
repository's. A history folder the reader cannot list may hold threads of this repository; the listing's
`unreadable_history_directories_machine_wide` counts such folders across the whole history.

## Runtime boundaries

Claude runs as `claude -p` with `Read`, `Glob`, `Grep`, `WebSearch`, `Skill` and, unless `PEER_BRIDGE_SUBAGENTS=0`,
`Agent`, in `dontAsk` mode, and loads only the user's own settings (`--setting-sources user`), so a consulted
repository's `.claude/settings.json` cannot run commands; its `CLAUDE.md` loads through `--add-dir`. It reads inside the
consulted directory, any `extra_directories`, Claude Code's own temporary folder, and wherever the user's own settings
add a directory or allow a read; a read anywhere else, such as the home folder, is denied. A web search sends its query
to Anthropic.

Codex runs as `codex exec` in its read-only sandbox, without the user's config or "always allow" rules, and its
commands see only the core environment, so tokens in the user's environment do not reach them. It can read any file the
user can read. Saved Codex authentication and project instructions still load; user-level integrations and model
preferences do not, so choose a model per call with `model`, or set `PEER_BRIDGE_CODEX_MODEL`. Managed requirements
still apply and may refuse the consultation configuration.

Whatever a peer reads goes to its model provider, and each peer CLI keeps its own authentication, logs and transcript
outside the project. The full list of what the bridge enforces, and what it does not, is in
[SECURITY.md](https://github.com/mtrzes/peer-bridge/blob/v1.0.0-beta.2/SECURITY.md).

Claude consultations are saved as CLI sessions for the supplied project and named `peer-bridge <conversation_id>`.
They remain resumable by their Claude session ID. The [Claude session documentation][claude-sessions] excludes print-mode
sessions from the normal resume picker and `claude --continue`; transcript files alone do not establish picker
visibility. Bridge follow-ups should use the bridge conversation ID so the consultation restrictions are applied again.

[claude-sessions]: https://code.claude.com/docs/en/sessions#loop-p-agent-sdk-and-background-sessions

## Configuration

| Environment variable | Default or purpose |
|----------------------|--------------------|
| `PEER_BRIDGE_TIMEOUT_SECONDS` | Consultation timeout in seconds, more than 0 and at most `3600`; defaults to `600`. |
| `PEER_BRIDGE_STATE_DIR` | Overrides the private state directory (conversation records and locks, `jobs/`, `worktrees/`); must be an absolute path. |
| `XDG_STATE_HOME` | When set to an absolute path, bridge state lives at `$XDG_STATE_HOME/peer-bridge`; otherwise at `~/.local/state/peer-bridge`. |
| `PEER_BRIDGE_CLAUDE_BIN` | Claude Code executable; defaults to `claude` on `PATH`. |
| `PEER_BRIDGE_CODEX_BIN` | Codex executable; defaults to `codex` on `PATH`, then on macOS the CLI bundled with ChatGPT.app. A set value that does not resolve is an error, not a fallback. |
| `PEER_BRIDGE_CODEX_MODEL` | Optional default Codex model; a per-call `model` overrides it; otherwise the CLI default. |
| `PEER_BRIDGE_CODEX_REASONING_EFFORT` | Optional Codex reasoning effort, such as `high` or `xhigh`, passed as `model_reasoning_effort`; a per-call `reasoning_effort` overrides it. The bridge refuses a model or effort missing from Codex's catalogue before Codex starts. |
| `PEER_BRIDGE_CLAUDE_MODEL` | Optional default Claude model, such as `opus`, passed as `--model`; a per-call `model` overrides it; otherwise Claude Code's settings. Claude Code itself refuses a model it does not know. |
| `PEER_BRIDGE_CLAUDE_REASONING_EFFORT` | Optional Claude effort, such as `high` or `max`, passed as `--effort`; a per-call `reasoning_effort` overrides it. |
| `PEER_BRIDGE_THREADS` | `1` turns on the experimental thread tools and `seed_thread`; any other value, or none, leaves them off. |
| `PEER_BRIDGE_SUBAGENTS` | `0` stops a peer from starting sub-agents; any other value, or none, lets it start them. They keep its read-only sandbox, and each one adds to the consultation's cost and time. |
| `CODEX_HOME` | Codex's own setting; the thread reader reads Codex history from it, defaulting to `~/.codex`. |
| `CLAUDE_CONFIG_DIR` | Claude Code's own setting; the thread reader reads Claude Code sessions from it, defaulting to `~/.claude`. |

After increasing the bridge timeout, increase the host's MCP tool timeout to leave room for shutdown and result delivery.

## State retention

The state directory must be dedicated to the bridge and deny group and other access. New state directories are created
with mode `0700`; an existing directory with broader mode permissions is rejected without changing its permissions. When
setting `PEER_BRIDGE_STATE_DIR`, choose a private directory rather than a shared folder.

Bridge records are retained until explicitly removed. A successful consultation creates a `.json` record and a `.lock`
file with the same bridge UUID. A new failed consultation can leave only the lock file. There is no automatic expiry,
because deleting a record removes the bridge's ability to resume that conversation.

To retire conversations, stop every MCP server using this state directory, then remove the corresponding UUID's record
and lock file. Orphan lock files can be removed while those servers are stopped. Do not remove lock files while a server
is running: a process can retain a lock on an unlinked file while another process creates and locks its replacement.
Removing bridge state does not delete the peer CLI's transcript; manage those histories through the respective CLI.

A background job's files, including its reply, stay under `jobs/` until a later background start removes them, at the
earliest seven days after the job finished; a job whose files cannot be read is removed seven days after its last
change. A pinned consultation's checkout under `worktrees/` exists only while it runs. If the server is killed mid-run,
the next consultation removes the leftover, and git's record of it, once no consultation holds that conversation.

## Verification

From a checkout of the repository, at its root, install the locked dependencies into a virtual environment, then run
the bridge's tests and the developer tools' tests, before each commit. CI runs the same commands on Linux with Python
3.11 for a pull request, and on macOS and Linux with Python 3.11 and 3.15 when started by hand on a release commit
before it is tagged; the `v*` tag's run checks only that the tag names the plugin's version
(`.github/workflows/tests.yml`):

```sh
uv venv --managed-python --python 3.11 .venv
```

```sh
VIRTUAL_ENV="$PWD/.venv" uv sync --active --locked --script plugins/peer-bridge/scripts/bridge.py
```

```sh
.venv/bin/python -m unittest discover -s plugins/peer-bridge/tests
```

```sh
.venv/bin/python -m unittest discover -s dev/tests -t dev/tests
```

For quicker feedback while you work, `dev/run_tests.py` runs a suite in parallel processes. It fails unless every
discovered test ran exactly once and passed. It is optional: the serial commands above stay the check before each
commit, and CI runs only those.

```sh
.venv/bin/python dev/run_tests.py -s plugins/peer-bridge/tests
```

The tests use simulated peer executables, synthetic thread histories and temporary state; they call no model and read
no real history or settings. [ADR 0002](https://github.com/mtrzes/peer-bridge/blob/v1.0.0-beta.2/docs/adr/0002-test-architecture.md)
sets the rules a change to the tests follows.

The adapters follow the official [Claude headless interface](https://code.claude.com/docs/en/headless) and
[Codex non-interactive interface](https://developers.openai.com/codex/noninteractive).
