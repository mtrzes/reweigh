# ADR-0004: Codex is said to be outside a git repository only when no .git entry is found, as Codex checks

- Status: Accepted
- Enforced by: test_core `test_codex_finds_a_repository_by_a_git_entry_in_the_directory_or_a_parent`; test_bridge
  `test_failures_are_named_from_the_facts_the_bridge_gathers` (a real folder outside any repository); standing duty:
  after a Codex update, run `codex exec` in a folder with no `.git` above it, in a bare repository and below an
  empty `.git` file, and check that only the last one runs

## Context
Codex refuses to run outside a git repository unless the directory is trusted, and says so only in prose on stderr,
with exit status 1. A failed consultation should tell the caller to move `working_directory` when that is the cause,
but the bridge names a cause only from an exact field or check, never from the CLI's text.

git cannot give that check. `git rev-parse` exits 128 for every fatal error: outside a repository, on a missing or
unreadable directory, under an unreadable parent. Only its stderr text tells them apart. Its answer also differs
from Codex's: Codex 0.169 runs below an empty `.git` file, which git rejects, and refuses in a bare repository, which
git accepts.

## Decision
- The fact is whether the resolved `working_directory` or any parent up to `/` holds a `.git` entry of any kind,
  read with `lstat`. That is the check Codex makes; git is not asked.
- A level that is gone, not a directory, or unreadable, or a `.git` lookup that fails other than as missing, leaves
  the fact unknown, and no cause is named.
- The walk is a pure function with the `lstat` call injected, tested table-driven.

## Consequences
A Codex consultation in a folder outside any repository again gets a message that names the cause. The bridge copies
undocumented Codex behaviour: if Codex changes its check, the message can name a wrong cause until the standing duty
catches it, since tests never run the user's Codex. If Codex's system configuration trusts a directory outside any
repository, Codex runs there, and another failure there is reported as outside git; the message names that case.

## Reconsider if
Codex reports the refusal as a field, event or distinct exit status; Codex changes how it finds a repository; or
consultations start passing `--skip-git-repo-check`.
