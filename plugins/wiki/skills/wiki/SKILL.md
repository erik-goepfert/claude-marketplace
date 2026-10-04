---
name: wiki
description: >
  Erik's personal wiki — the Obsidian vault "wiki" that Claude and Erik both read and write,
  backed by git (~/Source/wiki, ~/Source/wiki-claude, and the iCloud vault). Use whenever reading,
  searching, creating, or editing wiki notes, looking up context about Erik or his projects that
  may live in the wiki, or committing/syncing wiki changes. Takes precedence over the general
  obsidian skill for anything that writes to this vault.
---

# Wiki

A shared personal wiki: Claude edits it on the Mac, Erik edits it in Obsidian on
the phone. Git reconciles the two. Design and history:
`~/Source/erik-goepfert-private/projects/wiki/README.md`.

## Where things live

| Path | Branch | Who writes |
|---|---|---|
| `~/Source/wiki-claude` | `claude` | **Claude — the only place you edit** |
| `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/wiki` | `icloud` | Erik (Obsidian, via iCloud) |
| `~/Source/wiki` | `main` | The sync job only |

- **Read and edit only in `~/Source/wiki-claude`.** After a sync it has the same
  content as the other two, so there's no reason to read elsewhere.
- **Never write to the iCloud vault or `~/Source/wiki`**, with any tool, including
  Bash (`echo >`, `mv`, `sed -i`, `git commit`, …). A hook blocks Edit/Write there;
  Bash isn't covered, so that part is on you.
- The separate `my-personal-vault` is **not** this wiki. Don't touch it unless
  Erik asks.

## Syncing is automatic

This plugin's hooks run `sync/sync.sh`:

- **Before you touch the wiki** (any tool call referencing a wiki path), at most
  once a minute, so you read the latest phone edits.
- **At the end of your turn**, if `~/Source/wiki-claude` has uncommitted changes
  or commits not yet on `main`, so Erik sees your edits in Obsidian.

A launchd timer also runs it as a backstop. `/wiki-sync` runs it by hand.
Don't run `git merge`/`git push` yourself; the sync owns those.

## Committing your edits

Commit in `~/Source/wiki-claude` when a logical change is done, rather than
leaving it for the sync job: you know the intent, so you write the better
message. Follow [git-conventions.md](git-conventions.md), in particular the
`Origin: claude` trailer and the never-rewrite-history rule:

```bash
git -C ~/Source/wiki-claude add -A
git -C ~/Source/wiki-claude commit -m "Add packing list to 'Japan trip'" --trailer "Origin: claude"
```

## Authoring notes

- Plain Obsidian-flavored Markdown. For syntax (wikilinks, embeds, properties,
  callouts, tasks), the general **obsidian** skill is the reference.
- **The obsidian skill and its CLI are for knowledge, searching and reading
  only — never for writing.** Its CLI acts on whatever vault the desktop app
  has open, which bypasses the `claude` worktree. Create and edit notes with
  normal file edits in `~/Source/wiki-claude`.
- Filename = note title (`Japan trip.md`); link with `[[Japan trip]]`.
- Keep properties (frontmatter) minimal; add them when they earn their place
  (`tags`, `aliases`, dates).
- Prefer editing an existing note over creating a near-duplicate; search first.
- Structure isn't settled yet (see the design README's open questions).
  Follow whatever exists; ask Erik before creating new top-level folders or
  reorganizing.
- Erik writes here too. Don't reformat or restyle his notes beyond the change
  he asked for.
- Never touch `.obsidian/` unless asked; it's Obsidian's config.
