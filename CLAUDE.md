# Repository guidance for Claude

## Pull request workflow

- **A merged PR is finished.** Never add commits on top of already-merged
  history, and never reuse or reopen a merged PR for follow-up work.
- **New work → new PR.** Once the PR for a branch has merged, treat any
  follow-up change as a fresh change: start a new branch and open a new PR
  for it.
- **Branch from the up-to-date default branch.** Before starting new work,
  `git fetch origin main` and branch from the latest `main` so the branch
  already includes whatever was just merged — avoid stale bases and don't
  re-introduce already-merged diffs.
