# ADR-0004: A failure cause is named only from an exact fact; an inconclusive probe names none

- Status: Accepted
- Enforced by: test_core `test_failure_message_names_a_cause_only_when_the_facts_establish_it`, `test_failure_probes`
  and `test_codex_command`; test_bridge `test_cli_text_alone_never_names_a_cause` and
  `test_failures_are_named_from_the_facts_the_bridge_gathers`; test_reader
  `test_a_history_directory_that_cannot_be_listed_leaves_history_presence_unknown`

## Context
A failed consultation should tell the caller what to fix: sign in, close the app that holds the thread. Both CLIs
report many failures only in prose on stderr, which changes between releases; a pattern over that prose gains a
spelling with every release and never converges. The cause was also misnamed the other way, by reading an error as a
negative fact: a failed `git rev-parse` as "outside a repository", an unlistable history folder as "no history".

## Decision
- A failed run is described by `FailureFacts` in `consultation.py`: the saved conversation's resume state, the
  effective model choice, and facts from the CLI's structured output and targeted probes. A probed fact is True, False
  or None, and None means the bridge could not tell.
- A fact comes only from an exact field, an exit status, a file check, or a stderr line that starts with a fixed tag
  the CLI emits, never from a pattern over the CLI's or the model's text.
- An inconclusive probe gives None: a failed call, a non-zero exit that does not mean the fact, an unlistable path. A
  handler catches only the exception types that mean what it claims.
- The bridge gives the first diagnosis whose facts are established, in this order; a None blocks only the diagnoses
  that need that fact:
  1. The thread is open elsewhere: a resumed Codex conversation whose thread has a writer-lock file.
  2. The CLI is signed out: the sign-in probe says so (`claude auth status --json` gives `loggedIn: false`; `codex
     login status` exits 1), or Claude's result carries `api_error_status` 401.
  3. The history is gone: a resumed conversation has no history file, and every history folder could be listed.
  4. Claude Code does not know the model: a stderr line starts with `CLAUDE_UNRECOGNIZED_MODEL_TAG`.

  With none, the caller gets the generic message, with a hint when a Codex model or effort was chosen, per call or
  by environment.
- Codex refuses to run outside a git repository and says so only on stderr. A Codex consultation passes
  `--skip-git-repo-check`, so that failure does not occur and needs no fact.

## Consequences
A cause the CLIs report only in prose stays unnamed until they give a field, an exit status or a tag. Some facts are
indirect: a writer-lock file can outlive a crashed app. The writer-lock fact comes from Codex's own state. The sign-in
probe runs in an empty folder of its own, because `claude auth status` applies the settings of the folder it runs in.

## Reconsider if
A CLI reports a cause as a field, event or distinct exit status (add the fact).
