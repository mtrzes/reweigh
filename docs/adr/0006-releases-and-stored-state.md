# ADR-0006: A release has one version in three places; stored state stays readable or is refused

- Status: Accepted
- Enforced by: test_repository `test_both_manifests_and_the_newest_release_in_the_changelog_name_one_version`; test_jobs
  `test_job_request_round_trips_and_reads_requests_written_before_model_choices`; test_core
  `test_stored_records_and_outcomes_are_validated` and `test_resumed_record`; `.github/workflows/tests.yml` (the tag
  names the version, per ADR 0002); standing duty: a release runs the steps below in order

## Context
Users install the plugin from this repository's marketplaces, or from another marketplace that pins a `v*` tag, and the
installed copy holds only `plugins/reweigh/`. One user's state directory outlives each upgrade: conversation
records, background jobs and their requests, written by whichever release ran them. Between releases, changes collect on
`main`.

## Decision
- The two `plugin.json` files and the newest `CHANGELOG.md` release heading, `## <version>`, name one version; no
  other document states it. Links from `plugins/reweigh/` to files outside it go through
  `https://github.com/mtrzes/reweigh/blob/v<version>/`, so an installed copy reads its own release's documents.
- Between releases, each user-visible change adds a line under `## Unreleased`, above the newest release.
- A release, in order: set the version in both manifests and in those links, rename `## Unreleased` to
  `## <version>`, run both suites locally, start CI by hand on that commit ([ADR 0002](0002-test-architecture.md)),
  and once it passes, tag that commit `v<version>`.
- A field added to stored state gets a default when it is missing, so state an earlier release wrote stays readable;
  today that covers `pinned_commit`, the result's optional fields, and a request's `model` and `reasoning_effort`.
  Readers check the fields they use and ignore others; a missing or malformed field they need stops the resume or job
  with a fixed message, never a guess.
- Only `plugins/reweigh/` ships. Techniques that work with any peer, such as review modes and prompts, live outside
  this repository.

## Consequences
An installed plugin's links match its version, at the cost of a link to a document added since the last release
resolving only once that release is tagged. A record from a much older release may be refused rather than migrated. The
changelog of a release is written as the work lands, not reconstructed from git.

## Reconsider if
A record needs a field that older records cannot default (write a migration instead), the plugin starts shipping
documents it now links to, or a second distribution channel needs its own version.
