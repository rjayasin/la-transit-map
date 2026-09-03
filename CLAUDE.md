# Repository guidance for Claude

## Writing style

Write plainly, in code comments, docstrings, Markdown, commit messages and pull
request bodies alike.

- Say what a thing does in the shortest sentence that does it.
- No literary register: no rhetorical questions, no aphorisms, no repetition
  for rhythm, no dramatised failures ("the shape falls into the hole"), no
  stacked em-dash asides. State the mechanism.
- Prefer short sentences and a period over a long sentence and a dash.
- Numbers and identifiers over adjectives. "3 px wide" beats "hair-thin".
- Don't narrate the reasoning that led to the code. Say what the code does and
  what breaks without it.

## Git workflow

Which workflow applies depends on where the work is running.

### Working locally: commit and push straight to `main`

- **Default to `main`.** For local development, commit on `main` and
  `git push`. Don't create a branch or open a PR unless asked. This is a solo
  repo and the PR round-trip is overhead when the user is iterating alongside
  you.
- **Fetch first.** `git fetch origin main` before starting, so the local branch
  isn't stale. Cloud work may have merged something.
- **Ask before pushing anything unusual.** Rewriting history, force pushes and
  deleting remote branches are not covered by this default.

### Working in the cloud: branch and open a PR

Autonomous or remote runs (cloud agents, scheduled jobs, anything without the
user watching) go through review rather than landing directly.

- **New work, new PR.** Start a branch, open a PR, let the user merge it.
- **Branch from the up-to-date default branch.** `git fetch origin main` and
  branch from the latest `main`, so the branch includes whatever was just
  merged and doesn't re-introduce merged diffs.

### Always

- **A merged PR is finished.** Never add commits on top of merged history and
  never reuse or reopen a merged PR. Treat follow-up work as a fresh change.

## Documentation and comments

This repo tends to accrete a per-change engineering journal, with every fixed
route adding paragraphs to the README and blocks of comments to the code. Don't
feed it.

- **The README is for a reader, not a changelog.** It describes how the project
  works and how to run it. It carries no route-specific content: no named
  routes, streets, per-route measurements or before/after numbers. If a change
  doesn't alter how the project works, the README doesn't change.
- **Comment the mechanism, not the investigation.** Say what a constant or a
  branch is for and what breaks without it, in a few lines. Leave out the
  debugging story, the measurements taken along the way and the routes that
  prompted it.
- **A worked example is not documentation of a fix.** State the failure in
  general terms rather than describing the instance that revealed it.
- **A row in a hand-tuned table gets no comment.** `PINNED_ANCHORS`,
  `OVERRIDE_PATHS`, `MAP_LABELS` and the rest have a preamble saying what the
  table is for. The row is the fix; why a route needed one belongs in the
  commit message. The exception is a few words naming an opaque key, as in
  `("ladot", "708"): "WM",  # DASH Wilmington, clockwise`. That is the row's
  identity, not its justification.
- **`implementation_notes.md`** holds what someone needs in order to do further
  work: the hand-tuned tables and when to reach for each, verification
  gotchas, invariants. Keep it concise, and put new notes there rather than in
  the README.
