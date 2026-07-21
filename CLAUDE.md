# Repository guidance for Claude

## Git workflow

Which workflow applies depends on where the work is running.

### Working locally — commit and push straight to `main`

- **Default to `main`.** For local development, commit on `main` and
  `git push`. Don't create a branch or open a PR unless asked to. This is a
  solo repo and the PR round-trip is pure overhead when the user is iterating
  alongside you.
- **Fetch first.** `git fetch origin main` before starting so the local branch
  isn't stale — cloud work may have merged something since.
- **Still ask before pushing anything unusual.** Rewriting history, force
  pushes, and deleting remote branches are not covered by this default.

### Working in the cloud — branch and open a PR

Autonomous or remote runs (cloud agents, scheduled jobs, anything without the
user watching) should go through review rather than land directly:

- **New work → new PR.** Start a branch, open a PR, and let the user merge it.
- **Branch from the up-to-date default branch.** `git fetch origin main` and
  branch from the latest `main` so the branch already includes whatever was
  just merged — avoid stale bases and don't re-introduce already-merged diffs.

### Always

- **A merged PR is finished.** Never add commits on top of already-merged
  history, and never reuse or reopen a merged PR for follow-up work. Once a
  PR has merged, treat any follow-up as a fresh change.
