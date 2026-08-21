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

## Documentation and comments

This repo has a habit of accreting a per-change engineering journal — every
fixed route adding paragraphs to the README and blocks of comments to the code.
Don't feed it.

- **The README is for a reader, not a changelog.** It outlines how the project
  works and how to run it. It carries **no route-specific content** — no named
  routes, streets, per-route measurements, or before/after numbers. If a change
  doesn't alter how the project works, the README doesn't change.
- **Comment the mechanism, not the investigation.** Say what a constant or a
  branch is for and what breaks without it, in a few lines. Leave out the
  debugging story, the measurements taken along the way, and the routes that
  prompted it — a named route in a comment should be a hand-tuned table entry
  explaining itself, and one or two lines at that.
- **A worked example is not documentation of a fix.** Prefer the general
  statement of the failure over the instance that revealed it.
- **A row in a hand-tuned table gets no comment.** `PINNED_ANCHORS`,
  `OVERRIDE_PATHS`, `MAP_LABELS` and the rest carry a preamble saying what the
  table is for; the row is the fix, and why a route needed one belongs in the
  commit message. The one exception is a few words naming what an opaque key
  is — `("ladot", "708"): "WM",  # DASH Wilmington, clockwise` — which is the
  row's identity, not its justification.
- **`implementation_notes.md`** holds what someone needs to do further work:
  the hand-tuned tables and when to reach for each, verification gotchas,
  invariants. Keep it concise, and put new notes there rather than in the README.
