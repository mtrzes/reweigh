# ADR-0004: A failure cause is named only from an exact fact; an inconclusive probe names none

- Status: Accepted
- Enforced by: test_core `test_failure_message_names_a_cause_only_when_the_facts_establish_it`, `test_failure_probes`
  and `test_codex_finds_a_repository_by_a_git_entry_in_the_directory_or_a_parent`; test_bridge
  `test_cli_text_alone_never_names_a_cause` and `test_failures_are_named_from_the_facts_the_bridge_gathers`;
  test_reader `test_a_history_directory_that_cannot_be_listed_leaves_history_presence_unknown`; standing duty: after a
  Codex update, run `codex exec` in a folder with no `.git` above it, in a bare repository and below an empty `.git`
  file, and check that only the last one runs

## Context
A failed consultation should tell the caller what to fix: sign in, move `working_directory`, close the app that holds
the thread. Both CLIs report many failures only in prose on stderr, which changes between releases; a pattern over
that prose gains a spelling with every release and never converges. The cause was also misnamed the other way, by
reading an error as a negative fact: a failed `git rev-parse` as "outside a repository" (`ec612f2`, `08c0d86`), an
unlistable history folder as "no history" (`7972baf`).

## Decision
- A failed run is described by `FailureFacts` in `consultation.py`: the saved conversation's resume state, the
  effective model choice, and facts from the CLI's structured output and targeted probes. A probed fact is True, False
  or None, and None means the bridge could not tell.
- A probed fact comes only from an exact field (`loggedIn`, `api_error_status`), an exit status, a file or `lstat`
  check, or a stderr line that starts with a fixed tag the CLI emits (`CLAUDE_UNRECOGNIZED_MODEL_TAG`), never from a
  pattern over the CLI's or the model's text.
- An inconclusive probe gives None: a failed call, a non-zero exit that does not mean the fact, an unlistable path. A
  handler catches only the exception types that mean what it claims.
- The bridge gives the first diagnosis whose facts are established; a None blocks only the diagnoses that need that
  fact. With none, the caller gets the generic message, with a hint when a Codex model or effort was chosen.
- Worked case: Codex refuses to run outside a git repository and says so only on stderr. git cannot give the fact:
  `git rev-parse` exits 128 for several of the failures that matter, and Codex 0.169 runs below an empty `.git` file,
  which git rejects, and refuses in a bare repository, which git accepts. The fact is whether the resolved
  `working_directory` or a parent up to `/` holds a `.git` entry of any kind, read with `lstat`, as Codex checks. A
  level that is gone, not a directory or unreadable, or a lookup that fails other than as missing, leaves it None. The
  walk is a pure function with `lstat` injected, tested table-driven.

## Consequences
A cause the CLIs report only in prose stays unnamed until they give a field, an exit status or a tag. Some facts are
indirect: a writer-lock file can outlive a crashed app, and a `.git` entry is the check Codex makes, copied from
undocumented behaviour; if Codex changes it, the message can name a wrong cause until the standing duty catches it,
since no test exercises the installed Codex's repository check. If Codex's system configuration trusts a directory
outside any repository, Codex runs there, and another failure there is reported as outside git; the message names that
case. A consulted repository can shape the `.git` fact, which changes only which message a failure gets; the
writer-lock fact comes from Codex's own state. The sign-in probe runs in an empty folder of its own, because
`claude auth status` applies the settings of the folder it runs in.

## Reconsider if
A CLI reports a cause as a field, event or distinct exit status (add the fact); Codex changes how it finds a
repository; or consultations start passing `--skip-git-repo-check`.
