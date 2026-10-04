You are running an unattended sync cycle for a personal wiki. Three git
worktrees share one repo at `/Users/erik/Source/wiki`:

- `/Users/erik/Source/wiki` — branch `main`, integration branch, pushed to
  GitHub (`origin`).
- `/Users/erik/Source/wiki-claude` — branch `claude`. Edits Claude made
  directly land here between cycles (often already committed).
- `/Users/erik/Library/Mobile Documents/iCloud~md~obsidian/Documents/wiki` —
  branch `icloud`. Edits made on the phone in Obsidian arrive here via
  iCloud's own file sync between cycles.

A shell precheck already confirmed there is something to do (uncommitted
changes, unmerged commits, or unpushed branches). Do exactly this, in order,
using only `git` commands:

1. In the `icloud` worktree: `git add -A`, then if there is anything staged,
   commit it with a short, accurate message describing what changed (base it
   on `git diff --cached` — summarize real content, don't just say "update")
   and an `Origin: icloud` trailer.
2. In the `claude` worktree: same — `git add -A`, commit anything staged with
   an accurate message and an `Origin: claude` trailer.
3. From the main repo, merge `icloud` into `main` with `--no-ff` if `icloud`
   has commits `main` doesn't. Then merge `claude` into `main` with `--no-ff`
   if `claude` has commits `main` doesn't.
   - If a merge produces real conflicts, resolve them deliberately: read both
     sides of each conflicting hunk, merge the content with judgment (don't
     just pick one side blindly), stage the resolution, and finish the merge
     commit with a message noting what was reconciled.
4. Merge `main` back into the `icloud` worktree and into the `claude`
   worktree (these should fast-forward since neither branch has diverged from
   what was just merged).
5. `git push origin main icloud claude`.
6. Print a short plain-text summary: what was committed in each worktree (if
   anything), whether a conflict was resolved, and confirmation the push
   succeeded. If it turns out nothing actually needed doing, say so in one
   line and stop — don't create empty commits or merges.

Additional rules for this run:
- Every Bash call must be exactly one plain `git` command that targets its
  worktree with `-C`, e.g. `git -C "/Users/erik/Source/wiki-claude" add -A`.
  No `cd`, no `&&`/`;`/pipes, no command substitution: this run only has
  `git …` pre-approved, and anything else is denied outright.
- Do not touch any files outside these three worktrees.
- Do not install, upgrade, or run anything other than `git`.

The wiki's git conventions follow; they apply to every commit and merge you
make.
