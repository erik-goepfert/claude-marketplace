# Wiki git conventions

Shared by interactive sessions (via the `wiki` skill) and the headless sync
run (appended to its prompt). One copy, so the two can't drift apart.

## Branches

- `main` — integration branch, the source of truth, pushed to GitHub.
- `icloud` — the Obsidian vault in iCloud Drive; phone edits land here.
- `claude` — `~/Source/wiki-claude`; the only place Claude edits.

`icloud` and `claude` never merge into each other directly; everything goes
through `main`.

## Commit messages

- Subject: short, accurate summary of the actual content change
  (`Add packing list to 'Japan trip'`), not "update" or "sync".
- Every commit carries an `Origin:` trailer naming where the change was made:
  - `Origin: icloud` — phone edits committed from the `icloud` worktree.
  - `Origin: claude` — edits committed from the `claude` worktree, whether by
    an interactive session or the sync job.
- Add it with `git commit --trailer "Origin: claude"` (or `icloud`).
- Merge commits into `main` are titled `Merge <branch> into main: <summary>`
  and carry the `Origin:` trailer of the branch being merged in. `git merge`
  has no `--trailer`, so pass it as the last `-m`:
  `git merge --no-ff claude -m "Merge claude into main: <summary>" -m "Origin: claude"`
- Git only reads trailers from the message's **final paragraph**. `Origin:`
  and any other trailers (e.g. `Co-Authored-By:`) must sit together in that
  one block, no blank line between them, or `Origin:` is silently lost.
- Query: `git log --format='%h %(trailers:key=Origin,valueonly,separator=) %s'`

## Merging

- Merges into `main` always use `--no-ff`.
- Merging `main` back into `icloud`/`claude` fast-forwards; after a cycle all
  three branches point at the same commit.
- Real conflicts are resolved deliberately: read both sides, merge the
  content with judgment, and say what was reconciled in the merge message.

## Never rewrite history

No `rebase`, `commit --amend`, `push --force`/`--force-with-lease`, or
`reset --hard`. Mistakes get fixed with a revert commit. Rewriting one branch
would strand commits the other worktrees already hold, and the history is the
wiki's journal of who wrote what, when.
