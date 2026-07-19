---
name: email-triage
description: Triage email across iCloud and Gmail accounts — thread-centric review of a local Maildir mirror (synced by mbsync, indexed to SQLite) with flag/reply/archive/trash decisions, approval gates, and a persistent history DB that makes each session smarter than the last. Use when the user asks to triage, clean up, or process their email/inbox.
allowed-tools: Bash(himalaya *) Bash(*history.py*) Bash(*init_history_db.py*) Bash(*indexer.py*) Bash(*threads.py*) Bash(*act.py*) Bash(*batch.py*) Bash(*unsub.py*) Bash(*analytics.py*) Bash(mbsync *)
---

# Email Triage

You are running an email triage session. You are the interface — there is no
other UI. Work thread-by-thread, propose actions, and let the user decide.
Design rationale lives in the repo's `email-skill-design.md` (state model,
data flow) and `email-folder-taxonomy.md` (folder tiers, flag semantics,
thread-state derivation); this file is operational.

**Phase 2.7 cutover.** Reads and thread assembly now run entirely against a
local Maildir mirror + SQLite index, not live IMAP. Only two things still
touch the servers directly: `mbsync` (sync) and reply/send. Everything else —
listing, reading, flagging, moving — goes through the local backend.

## State and stack layout

All mutable state lives in the data directory — default
`~/.local/share/email-triage/`, overridable via `$EMAIL_TRIAGE_DATA_DIR`.
Never write state into the plugin directory.

```
DATA="${EMAIL_TRIAGE_DATA_DIR:-$HOME/.local/share/email-triage}"
MAILDIR="$DATA/mail"                    # $MAILDIR/icloud, $MAILDIR/gmail
IDXDB="$DATA/index.db"
LOCK="$DATA/session.lock"
MBSYNCRC=~/.config/isync/mbsyncrc       # NOT auto-discovered — every mbsync call needs -c
IDX="${CLAUDE_SKILL_DIR}/scripts/indexer.py"
THR="${CLAUDE_SKILL_DIR}/scripts/threads.py"
ACT="${CLAUDE_SKILL_DIR}/scripts/act.py"
HIST="${CLAUDE_SKILL_DIR}/scripts/history.py"
UNSUB="${CLAUDE_SKILL_DIR}/scripts/unsub.py"
BATCH="${CLAUDE_SKILL_DIR}/scripts/batch.py"
```

- `config.yaml` (in `$DATA`) — schema v2 (Phase 4): per-account `tiers:`
  map realizing each canonical tier CONCEPT as this account's folder name,
  plus `maildir_path` and the `server_rules` registry. **Read it at session
  start; never improvise folder names — every folder name in every command
  comes from this map.** Tier concepts (code) vs realizations (config):
  `untriaged` (delivery lands, nothing decided), `active` (decided, live),
  `ongoing` (resolved, group stays warm — a subtree root), `cold` (resolved,
  done), plus system roles `sent`/`drafts`/`trash`/`junk`/`all_mail`.
  Nullable: `active: null` = no distinct live location (3-way disposition
  collapses to 2-way); `cold: null` = cold is a *condition, not a place*
  (derived as present-only-in-all_mail; write path `$ACT remove`;
  `cold_root` optionally names a real subtree for named chapters —
  gmail's `Archive/...` labels).
- `history.db` — decision log, session notes, contacts, rules. Irreplaceable.
- `index.db` — header + thread-graph cache over the Maildir. Disposable,
  rebuildable by `$IDX index`.
- `$DATA/mail/{icloud,gmail}` — the local Maildir mirror, synced by mbsync.
- himalaya accounts (in `~/.config/himalaya/config.toml`, not this skill's
  config): `icloud`/`gmail` are IMAP, `icloud-local`/`gmail-local` are
  Maildir pointed at `$MAILDIR/<account>`. Role split (verified, 2.6):

  | Account | Backend | Used for |
  |---|---|---|
  | `icloud`, `gmail` | IMAP | reply/send **only** — also sets `\Answered` and marks `\Seen`, accepted (rule 5) |
  | `icloud-local`, `gmail-local` | Maildir | **all** reads and flag ops |

**Bootstrap (only if something is missing):** verify `himalaya`, `mbsync`,
and `python3` are installed; walk the user through anything missing. If
`history.db` is missing: `${CLAUDE_SKILL_DIR}/scripts/init_history_db.py`. If
`index.db` is missing: `$IDX init --db "$IDXDB"` (or just run `index` — it
auto-inits). If `config.yaml` or `~/.config/isync/mbsyncrc` is missing, this
is a from-scratch setup outside a normal triage session — stop and walk the
user through the full build sequence in `implementation-plan.md` rather than
improvising it here.

## Operating rules (non-negotiable)

1. **Approval gate.** Never send mail and never move or delete mail without
   the user approving that specific action in this session:
   - *Send / reply:* always show the full draft; nothing is sent without
     explicit approval of that exact draft.
   - *Trash / resolve (Areas/Archive):* approval required, but batchable —
     and **name the resolved destination in the ask** ("archive these 12 to
     `Archive`?" / "file these 3 under `Areas/Taxes`?" not just "archive
     these?"). A folder mix-up between an archive target and a trash target
     is an easy silent slip; saying the destination out loud surfaces it
     before execution, not after.
   - *Stays open (move to `Active`, no flag):* rides along inside the same
     category-batch approval as the archive/trash proposals for that
     category ("9 actionable: 2 archive, 1 trash, 6 stay open in Active,
     3 of those flagged — proceed?") — not a separately gated action.
   - *Flagging:* low-risk and reversible — you may flag/unflag autonomously
     during triage (log it as `decided_by=auto`).
   - *Ambiguous phrasing:* "keep" or "keep it" alone means **leave it where
     it is, no action** — not archive. Only archive/move on explicit
     relocation language ("archive," "file it away," "move it," "get it out
     of the inbox"). When genuinely unclear, ask rather than assume.
2. **`\Flagged` means "needs my reply,"** derived **per thread**, not per
   message: `blocked(thread) = OR(\Flagged over every non-trash/junk member)`.
   Flag writes are always **thread-wide clear-then-set** — clear `\Flagged`
   from every member, then set it on at most one — never an incremental
   single-message flag/unflag. Why: this decouples thread state from message
   recency (taxonomy doc) — a new arrival can no longer silently reopen or
   close a thread as a side effect of delivery order; state changes only
   through an explicit triage decision.
3. **Trash means the Trash folder.** Delete = `$ACT move` to the account's
   trash folder from `config.yaml`. Never expunge, never a raw `flag add`
   for deletion. Cross-folder moves **never go through himalaya** — verified
   to strand mail (mbsync hard-errors on the `,U=` token himalaya's Maildir
   backend preserves across folders); `$ACT` strips it. This applies to
   every move: Trash, `Active`, `Areas/…`, `Archive/…`.
4. **Unsubscribe links are surfaced by default; EXECUTED only on the
   user's explicit request, never autonomously.** Extract and present them
   freely. When — and only when — the user explicitly asks you to perform
   an unsubscribe ("unsubscribe me from these"), execute it via
   `$UNSUB exec` (see Triage step 5); a batch request covers each message
   in that batch. Never fetch, click, or probe a link on your own
   initiative, and never execute a `mailto:` unsubscribe directly — that
   is a send, and it goes through the normal reply/send gate (rule 1).
5. **`\Seen` is the user's signal, not triage state — your own reads must
   never move it.** A message counts as read only once the user has been
   shown its content or a representative summary of it. Concretely, on the
   `-local` (Maildir) accounts:
   - Any read for your own purposes (resolving `ambiguous`, thread-state
     checks) uses `$THR read` — raw-file-backed, \Seen-safe by
     construction (it never touches the Maildir filename). If you must
     fall back to himalaya for a read, always pass `--preview` —
     verified (2.6): a plain local `message read` stamps `S` on the Maildir
     filename, which mbsync would then sync as `\Seen` to the server;
     `--preview` leaves the filename untouched.
   - Only mark a message read by explicitly setting it —
     `himalaya flag add -a <acct>-local -f <folder> <id> seen` — at the
     moment you actually present its content or summary to the user.
   - Exception, en route to trash (5.7): a message the user has just
     approved for trashing needs no seen-marking even if you summarized
     it — `\Seen` bookkeeping on mail headed for the Trash folder is
     wasted calls and wasted sync churn. Marking read is for mail that
     STAYS.
   - Exception: `himalaya template reply` / `message reply` (run against the
     **IMAP** account, since sending is IMAP-only) have no preview
     equivalent and do mark `\Seen`. Accepted, since a reply draft quotes
     the original back to the user before anything sends.
6. **Deliverables land in a turn's FINAL message.** The harness collapses
   mid-turn prose to "Ran N commands" — anything you say between tool
   calls may never be seen. Summaries, boards, link lists, drafts: always
   restate them in the message that ends the turn. And when the user asks
   for content (a summary, a link, a body), deliver the content and STOP —
   no chained follow-on actions in the same turn; the next action waits
   for their response.
   **Link formatting (this environment):** markdown links are NOT
   clickable in this user's terminal. Present each URL bare, in full, on
   its own line — never truncated/elided (`...` breaks the token), never
   in backticks, never `[text](url)`-wrapped. This is an environment
   preference, not doctrine — a different terminal may warrant markdown
   links again.

## Session flow

1. **Read `config.yaml`.** Never improvise account/folder names.
2. **Check the lockfile.** `mkdir "$LOCK"` (atomic). If it already exists,
   tell the user a session may be live elsewhere or was abandoned, and ask
   before removing it (`rmdir "$LOCK"`) and proceeding.
3. **Stamp the session.** `session_started_at` = current UTC time, ISO 8601
   (e.g. `2026-07-09T18:15:00Z`), taken once. Every event and note this
   session carries it verbatim.
4. **Read history context:** `$HIST context [--senders addr1,addr2,...]` —
   recent session notes, active rules, sender stats, contacts.
5. **Two-stage sync (session start).** Gmail's full channel costs 5–8 min;
   don't make the user wait for it before triage can even start.
   **Stage 1 — priority sync, foreground:** exactly the live working set +
   Sent (Sent is required: replies sent from other clients since last
   session live there, and `waiting`-state derivation lies without it):
   ```sh
   rm -f "$DATA/.fullsync-done"
   $IDX priority-folders     # config-driven fast set per account:
                             # tiers untriaged + active + sent + drafts
   ```
   Each account's `mbsync_fast` value is the ready-made argument — pass it
   verbatim as ONE argument: `mbsync -c "$MBSYNCRC" 'gmail:INBOX,Active,
   [Gmail]/Sent Mail,[Gmail]/Drafts'` (~60–90 s total; gmail's
   per-invocation overhead dominates). Never hand-assemble or hardcode the
   folder names — accounts differ and the config is the single source of
   truth. **Run the Stage-1 invocations SEQUENTIALLY, one account at a
   time** — parallel mbsync processes against the same Maildir tree are
   exactly the flake/race class this design exists to avoid, and Stage 1
   is foreground anyway.
   **Stage 2 — full sync, background**, one account per invocation
   (combined multi-channel invocations flake ~25% with
   `read: unexpected EOF`; per-account + one routine retry always
   recovers), completion marker at the end:
   ```sh
   nohup sh -c "mbsync -c '$MBSYNCRC' gmail; mbsync -c '$MBSYNCRC' icloud \
     && touch '$DATA/.fullsync-done'" > "$DATA/fullsync.log" 2>&1 &
   ```
   (Double-quote the `sh -c` payload so `$MBSYNCRC`/`$DATA` expand in THIS
   shell — single quotes would pass empty vars to the child.)
   Triage classification/discussion proceeds on Stage 1's data
   immediately. **Acting gates on Stage 2** (step 8) — act.py renaming
   files while mbsync walks the same folders is a race that must never
   happen.
6. **Index and consume the change report.** Runs right after Stage 1 (the
   priority folders are fresh; everything else reflects last session's
   final state, which is exactly what makes detected changes provably
   external). **Consume EVERY index run's change report this way, not
   just this one** — the pre-act re-index (step 8) will surface external
   changes from the rest of the mailbox once Stage 2 lands.
   `$IDX index --maildir "$MAILDIR" --db "$IDXDB"` → JSON on stdout:
   `new` / `moved` / `deleted` / `flag_changed` / `parse_errors` /
   `unrecognized_folders`. Interpretation rules, in order:
   - **Self-echo exclusion FIRST.** Before logging anything, drop every
     entry that matches an action you executed and already logged this
     session (same message, same movement/flag change). Those are your own
     actions echoing back — logging them again as `manual` double-counts
     the decision and corrupts sender stats. Only what remains is
     external.
   - `moved` entries whose destination tier is `trash` → one manual event,
     `final_action=trash`. Destination tier `cold`/`ongoing` →
     `final_action=archive`. Destination tier `active` →
     `final_action=activate`.
     All: `decided_by=manual`, `proposed_action=final_action`,
     `from_folder`/`to_folder` from the report entry.
   - `deleted` entries FROM a trash/junk-tier folder → server auto-purge
     (retention expiry), a non-event: no event log, no user mention.
     `deleted` from ANY other folder → mail vanished without passing
     through trash; surface it to the user (server rule? another client?)
     and log a manual `trash` event only if the user confirms intent.
   - `flag_changed` entries → compare `old_flags`/`new_flags`: log a
     manual event (`final_action=flag`, `decided_by=manual`) **only when
     `\Flagged` itself changed** (set or cleared). Deltas in `\Seen` or
     other flags are read-churn, not decisions — never log them (a single
     external mark-all-read once produced 460 would-be events).
   - `new` entries whose folder is the account's Sent folder → a manual
     *reply* event (`final_action=reply`, `decided_by=manual`); resolve
     `sender_addr` via `$THR show --account <acct> --message-id <id>` and
     take the thread's other participant.
   - `parse_errors` > 0 → surface to the user, don't silently drop it.
   - `unrecognized_folders` → surface as audit debt (the 2.3 folder audit
     hasn't finished absorbing something); log a session note, don't infer
     Archive.
7. **Assemble threads.** `$THR list --db "$IDXDB"` — default tiers
   `untriaged`+`active` (every thread with ≥1 member in either; the full
   member list, including ongoing/cold/sent siblings, always comes back —
   this is what makes reconciliation detectable, see below). Every thread
   object carries `drafted: true|false` — the second derived status,
   orthogonal to `state` (see Triage).
8. **Priority scan**, then **breakdown**, then **classify-first, easiest-
   first** (see Triage). **Before the FIRST act batch: verify Stage 2 is
   done** — `[ -f "$DATA/.fullsync-done" ]`. If not yet, tell the user
   ("full sync still finishing — a minute or two") and wait for the
   marker; never act while mbsync may be walking the Maildir. Once
   present: re-index and consume that change report (external changes
   from the non-priority folders surface here — log manual events per
   step 6), then act normally.
   **Act in approved batches** (see Acting). For move/remove batches — the
   dominant case — **one `$BATCH run` call is the whole unit**: it acts
   (via the act layer), auto-derives and logs the events, re-indexes with
   this batch's own echo absorbed (returning only provably-external
   residue — consume it per step 6), and runs the box-scoped sync itself.
   Flags stay on `-local` himalaya, replies on IMAP himalaya; for those,
   log events at act time and keep the same act → log → re-index → push
   unit by hand (a full `$IDX index` run is cheap, ~25 s; box-scoped sync
   `mbsync -c "$MBSYNCRC" 'gmail:INBOX,Areas/Taxes'` — exactly the folders
   touched, never a full-channel sync per action; Gmail's ~60–80 s
   per-invocation overhead is paid regardless of scope, so batch actions
   per sync).
   **HARD INVARIANT: a batch is not complete until its sync has run.**
   Never begin the next batch — and never report a batch as done — while
   the previous batch's changes exist only in the local mirror. Why: in the
   first live session, six batches of trashes sat local-only for 35 minutes
   until the user asked "have you pushed our changes to the server?" — a
   crash in that window would have left all of it live in the mailbox.
   Act → log → re-index → **push** is one unit — `$BATCH run` IS that unit
   in one call; check its `sync` result and re-run the push on a reported
   failure.
9. **Session end — residue check first, then the background chain.**
   Before anything else, run the residue check:
   `$THR residue --db "$IDXDB"` — a compact list of every thread still
   holding untriaged-tier messages. Empty is the definition of a clean
   close. Anything listed must be one of: deliberate (a `waiting` thread,
   an explicit "leave it") — say so in one line; or unfinished work —
   surface it to the user and put resume state in the session note. Never
   close a session with unexplained residue (the first live session
   declared victory twice with mail still in Inbox).
   Then the **background chain — user is done immediately.** Every
   batch was already box-pushed (step 8's invariant), so the final full
   sync mostly *pulls*; run it detached, releasing the lock only on
   success:
   ```sh
   nohup sh -c "mbsync -c '$MBSYNCRC' gmail; mbsync -c '$MBSYNCRC' icloud \
     && rmdir '$LOCK'" > "$DATA/endsync.log" 2>&1 &
   ```
   (Same quoting rule as Stage 2: double-quote the payload so the vars
   expand here.)
   Write the session note (if anything is unfinished) BEFORE launching
   the chain. **Deliberately no `$IDX index` in the chain:** our own
   actions were re-indexed at batch time, so whatever this sync pulls is
   provably external — the NEXT session's index run reports and logs it
   as manual events. Indexing here would silently absorb those changes
   and lose them. If the chain fails, the lock stays behind and the next
   session's stale-lock prompt (step 2) catches it — check
   `endsync.log` then.
   History backup is NOT a session step — it runs as a scheduled system
   task outside Claude (launchd invoking `$HIST backup`; plan 2.9). The
   first live session hit macOS TCC denials trying to write
   `~/Documents` from this shell; don't retry that fight in-session.

Note: mid-session syncs may pull **new arrivals**, which the indexer will
index, but the triage set assembled in step 7 is not re-assembled mid-session
— new mail waits for the next session. Keeps sessions deterministic and
terminable.

## Triage (classify-first discipline)

Classify from thread data (`$THR list` output: sender, subject, dates,
flags, tier) plus history (`sender_stats`, `contacts`, `rules`) — **no body
reads during classification**. Two axes per thread:

- **Lifecycle state**, from `$THR`'s `state` field (already the taxonomy's
  `OR(\Flagged)` / Sent-correlation derivation — nothing to recompute):
  `needs-reply` / `waiting` / `holding`. `inactive_days` is likewise already
  computed (days since the latest state-eligible member).
  **A `waiting` thread gets no proposal and no log entry, ever — not even a
  skip.** Lifecycle state and content category are independent axes, so a
  `waiting` thread can still land in `actionable` or any other category and
  surface in that category's list; leave it out of the batch entirely.
- **Drafted status** (`drafted: true` on the thread object — third axis,
  orthogonal to both others; derived from a `drafts`-tier member): a
  response is in progress. Changes the triage verb: `needs-reply + drafted`
  proposes "finish/send this draft?", never "compose a reply?".
  **Drafted threads are never silently passed over** — the inverse of the
  waiting rule: surface every session until the draft is sent or
  discarded, with staleness ("this draft is 3 sessions old") as nudge
  material. A draft never drives `state` (it isn't communication; it's its
  own axis).
- **Content category:** `spam-noise` | `marketing` | `newsletter` |
  `passive-update` (receipts, notifications, alerts) | `ambiguous` |
  `actionable`.
- **Current tier matters for what's being decided.** A thread whose only
  live-tier member is `untriaged` needs the full 3-way disposition (stay
  open in the active tier / resolve to ongoing-or-cold / discard — with
  `active: null` in config this collapses to 2-way). A thread already in
  `active` just needs flag maintenance (flag / unflag / resolve / trash)
  — it's already filed.
- **Reconciliation check.** If a listed thread's members span **both**
  `untriaged` and a resolved tier (`ongoing`/`cold`), it's a new arrival on
  an already-filed thread — handle per Reconciliation below, not as a fresh
  thread. **Trash/junk siblings never count as "resolved" for this check
  (5.7)** — a new arrival whose only siblings sit in trash/junk is a fresh
  thread getting a fresh decision, not a reconciliation case (previously-
  trashed noise from the same sender must not gravitationally pull new
  mail into Trash without a decision).

**Priority scan — before the breakdown, before anything else.** Security and
time-sensitive signals are surfaced first, regardless of content category.
Not left to in-the-moment judgment — always check, every session.

**Scope: unseen only.** A thread enters this scan only if its chronologically
latest non-trash/junk member (last entry of `$THR`'s `members` array, already
sorted oldest→newest, skipping any trash/junk members) lacks `"Seen"` in its
`flags` array — no extra read needed, it's already in the `$THR list` output.
A message already seen (in this client or another) has had its one chance to
interrupt; don't re-surface it. This only works because rule 5 keeps `\Seen`
uncorrupted. An already-seen security-shaped thread still triages normally
below, just not as a priority interrupt.

Flag a thread here if it's:

- a security notification: new sign-in / new device, new SSH or API key or
  app password added, password or recovery-info changed, MFA/2FA settings
  changed, suspicious or unrecognized activity;
- financial/time-boxed: a suspicious charge, a payment method change, an
  account or domain expiration warning;
- anything phrased as urgent/deadline-bound ("action required," "expires in
  N days," "confirm within...").

Present these **individually, immediately**, before the category breakdown —
"2 security alerts first: a new SSH key on your GitHub account and a GitLab
sign-in from a new location, both from today. Recognize these?" The proposed
action is usually "confirm it was you" (skip) or "you don't recognize this —
here's the link to review/revoke" (surfaced; executed never — rule 4). Only
once these are resolved does the regular flow proceed.

Scan modulation and logging (5.7):
- **Sender history deflates, it doesn't gate.** If sender stats/rules show
  this sender's "urgent" framing is routine noise (marketing deadlines,
  recurring "action required" that's always been trash), don't interrupt —
  fold it into its category with a deflating aside ("Acme's usual
  'expires soon', trashed the last 6"). The interrupt slot is for signals
  history can't vouch for.
- **Log the TRUE category**, not a blanket `actionable`: a marketing blast
  that tripped the scan logs `category=marketing`, with the scan hit
  recorded in `notes` ("priority-scan: expiry framing") — otherwise scan
  hits corrupt the category mix that rule proposals feed on.
- **`waiting` threads can still interrupt here** — an unseen security
  alert on a thread where you replied last still deserves its interrupt —
  but the waiting rule holds: interrupt only, no proposal, no logged
  event.

Then, for everything else:

1. **Present the full breakdown** — counts per category, lifecycle summary,
   a proposed next step. Example: "147 threads: 12 spam, 31 marketing,
   14 newsletters, 58 passive updates, 9 ambiguous, 9 actionable. Clear spam
   first?"
2. **Work easiest-first**, noise before attention: spam → marketing →
   newsletters → passive-updates → ambiguous → actionable. Show each category
   as a grouped list with a proposed disposition per thread (stay open /
   resolve-to / trash); the user can pull individual threads out before
   approving. **Actionable threads are always presented individually.**
   **Never declare a category closed from memory** — re-run `$THR list`
   (scoped with `--sender`/`--subject-contains` when useful) and verify
   nothing from the category remains in a live tier first. The first live
   session reported "all 18 handled" twice while a thread still sat in
   Inbox.
3. **The batch board loop (user request, 2026-07-18).** When a category
   batch starts, it is THE active context until the board is empty or the
   user explicitly redirects. Present the category as a numbered board;
   after each resolved sub-batch (and its sync, step 8's invariant),
   **re-present the SAME board minus the resolved items**:
   - remaining count in the header ("Newsletters — 7 of 12 remaining");
   - original item numbers preserved (item 9 stays item 9 until it's
     gone — renumbering breaks the user's in-flight references);
   - per-item judgment annotations carried forward (recidivist /
     borderline / new sender);
   - pipeline footer showing what's queued after this board ("Then:
     newsletters (8) → passive updates (~34) → ambiguous (9)").
   **Never end a sub-batch with an open-ended "what next?"** — the
   re-presented board IS the next prompt (and per rule 6 it must be the
   turn's final message). Repeat until the board is empty, then move to
   the next category in the pipeline.
4. **Targeted body reads only**, for exactly two reasons: resolving an
   `ambiguous` classification, or extracting unsubscribe links — category by
   category as worked, never upfront. The read path is `$THR read`
   (raw-file-backed: index row → RFC822 file → parsed headers +
   text/plain body; no himalaya id resolution, no \Seen risk, no lossy
   rendering):
   ```sh
   $THR read --db "$IDXDB" --maildir "$MAILDIR/<acct>" \
     --account <acct> --message-id <message-id> [--html-text] [--headers-only]
   ```
   `--html-text` prefers tag-stripped HTML (also the automatic fallback
   when there's no text/plain part); `--headers-only` for header checks.
   If resolving the ambiguity means actually showing the user the content,
   mark it read at that point (this one still goes through himalaya —
   resolve the local id fresh: `himalaya envelope list -a <acct>-local -f
   <folder> -o json`, matched by `from_addr` + `date_utc`):
   `himalaya flag add -a <acct>-local -f <folder> <id> seen`
5. **Unsubscribe.** Extraction is one call — `$UNSUB links` (same
   resolution args as `$THR read`) returns the `List-Unsubscribe` /
   `List-Unsubscribe-Post` headers AND the body's unsubscribe anchors
   (including image-button links by alt text), with a ranked
   recommendation. It replaces the old read-to-temp-file-and-grep recipe.
   Presenting to the user: **surface the body link as primary** for
   hand-clicking (revised 2026-07-16: header links are built for RFC 8058
   automated POSTs and frequently fail when a human clicks them; body
   links on ESP click-tracking domains worked reliably), formatted per
   rule 6 — full bare URL on its own line. When `List-Unsubscribe-Post`
   exists, **rank the native unsubscribe button in the user's mail client
   first** in your guidance — it performs the same RFC 8058 one-click POST
   with zero copy-paste.
   Executing for the user (rule 4 — only on their explicit request):
   ```sh
   $UNSUB exec --db "$IDXDB" --maildir "$MAILDIR/<acct>" \
     --account <acct> --message-id <message-id> [--url <link>] [--dry-run]
   ```
   Default is the RFC 8058 one-click POST (the same operation as the mail
   client's native button; a silent 2xx is success). `--url` falls back to
   GET on a specific link and hard-fails unless that URL re-matches the
   message's own extracted links at fire time (provenance check). `mailto:`
   entries are reported but never executed — they route through the normal
   send gate. Stdlib urllib, no cookies/auth, capped redirects, 15 s
   timeout.
   Ask ("unsubscribe too, or just clear this one?") and log the answer via
   `--unsubscribed` on that event. Don't ask per-thread if the user gave a
   batch answer ("unsubscribe from all of these") — apply it to each event
   in the batch.
   **A confirmed unsubscribe is also trash approval** (user rule,
   2026-07-10): "I've unsubscribed" — or a completed `$UNSUB exec` the
   user requested — means you may trash the associated message or batch
   without a separate ask; the confirmation IS the approval for those
   specific messages. Log normally
   (`final_action=trash, decided_by=approved, --unsubscribed`). It approves
   only the messages the unsubscribe was about, nothing wider.
   **A declined unsubscribe is a soft dampening signal (5.5):** if the
   user declines the offer but still clears the sender's mail, put
   `declined-unsubscribe` in that event's `--notes`, and don't re-offer
   for that sender in later sessions (prior events surface via context) —
   they're consuming the content. Re-offer only if their handling changes
   (reading stops, bulk-trashing starts). Notes convention only — no
   schema for it unless analytics later demand one.
6. **Noise escalation — when unsubscribe isn't possible.** Three-step
   ladder: (1) unsubscribe if possible; (2) when it isn't (or the user
   declines but still wants the mail gone), **propose a server-side filter**
   — draft the exact match expression, action, and carve-outs (e.g.
   Substack newsletters are filterable but login magic links must be
   excluded) for the user to install in the provider's web UI; filters
   aren't reachable over IMAP. **Server rules delete; they never file into
   folders** — a filing rule would create a second untriaged path into tier
   folders and bypass the learning loop. After the user installs one, record
   it under that account's `server_rules:` in `config.yaml` and log a
   session note — server-deleted mail generates no events, and the registry
   is what explains the silence to future sessions. (3) Last resort, a
   triage rule or continued manual triage.
7. Apply active rules from `history.db` when they match — still within the
   approval gates; a rule justifies a proposal, not an autonomous action
   (flags excepted).

## Thread assembly

No in-context grouping anymore — `$THR` builds threads from the index's
Message-ID/In-Reply-To/References graph plus a guarded subject fallback
(participant overlap required), and derives lifecycle state directly.

- `$THR list --db "$IDXDB" [--account icloud|gmail] [--tier t1 --tier t2 ...]
  [--sender substr] [--subject-contains substr]`
  — every thread with ≥1 member in the given tier set (default `untriaged`,
  `active`; both accounts if `--account` omitted). Use the two filters to
  answer "which thread is X" directly — don't dump the full JSON and
  re-filter it with ad-hoc python (the first live session did that 27
  times). Each thread object:
  `account`, `root_message_id`, `state`, `inactive_days`, `member_count`,
  `subject`, `members[]` (every member regardless of tier — `message_id`,
  `folder`, `folder_tier`, `date_utc`, `from_addr`, `subject`, `flags`,
  `is_flagged`).
- `$THR show --db "$IDXDB" --account <acct> --message-id <id>` — the full
  thread containing a given message, any tier.
- A thread's messages live in one tier location at a time (system folders
  excepted — Sent copies never move). **Threads never move to Inbox** —
  Inbox is delivery, not a destination. When a triage decision changes a
  thread's location, **every non-system member moves as a unit** — and
  **trash/junk members are never part of that unit (5.7)**: a thread-wide
  move never resurrects a member the user already discarded (mega-threads
  accumulate trashed noise; moving the thread must not drag it back out).

## Acting

**Destinations — resolved from `config.yaml`'s `tiers:` map, never
hardcoded.** The dispatch per disposition (Phase 4.4 — a third account
slots in by config alone):

| Disposition | Rule (per account's tier config) |
|---|---|
| Stays open | `$ACT move --to <tiers.active>` (if `active: null`, the disposition doesn't exist — mail stays put in untriaged) |
| Resolve, ongoing group | `$ACT move --to "<tiers.ongoing>/<group>[/<project>]"` |
| Resolve, closed one-off (flat leaf) | `cold` is a **name** → `$ACT move --to "<tiers.cold>/<year>-<Name>"`. `cold` is **null** → `$ACT remove` (strip the last label; message persists only in all_mail; the indexer derives tier `cold` by absence) |
| Resolve, closed chapter in a family | `$ACT move --to "<cold name or cold_root>/<Family>/<year>-<Name>"` — always a **real** folder/label even when cold is null (hierarchy can't be expressed by absence); create first if new (`himalaya folder create`) |
| Discard | `$ACT move --to <tiers.trash>` |
| Save reply for later | see Reply step 5b — `himalaya message save` into `<tiers.drafts>` |

**The standard path for an approved move/remove batch is `$BATCH run`**
(5.4) — act + auto-derived event log + echo-absorbed re-index + box-scoped
sync in one call. Items JSON on stdin or `--file`:

```sh
$BATCH run --db "$IDXDB" --maildir-root "$MAILDIR" --data-dir "$DATA" \
  --mbsyncrc "$MBSYNCRC" --session "$SESSION" \
  --category newsletter [--decided-by approved] [--notes "..."] \
  [--unsubscribed] [--dry-run] <<'JSON'
[
  {"account": "gmail", "message_id": "m1@x", "from_folder": "INBOX",
   "to_folder": "[Gmail]/Trash"},
  {"account": "gmail", "message_id": "m2@x", "from_folder": "INBOX",
   "remove": true}
]
JSON
```

Message-ids are the INDEX's bare form (no angle brackets — as `$THR`
reports them). Events are derived, not hand-written: `final_action` from
the destination's tier (trash/junk → `trash`, active → `activate`,
ongoing/cold or a `remove` → `archive`), sender/subject from the index
row; per-item overrides (`category`, `notes`, `thread_state`,
`inactive_days`, `unsubscribed`, ...) win over the batch-level flags.
Pre-flight validates EVERY item before anything acts (all-or-nothing —
a bad destination stops the batch with the mailbox untouched); a
destination must classify as trash/junk/active/ongoing/cold. The output's
`change_report.external` is step-6 material (self-echo already absorbed);
check `sync[].ok` — the batch invariant is only satisfied when the push
succeeded. Hand-written `$HIST log-events` JSON remains only for events
with no move attached (skips, flag decisions, replies, drafts).

`$ACT move --db "$IDXDB" --maildir "$MAILDIR/<acct>" --account <acct> --from
<folder> --to <folder> <message-id>...` and `$ACT remove ... --from <folder>
<message-id>...` are the low-level layer underneath (one-off moves,
recovery). `$ACT remove` refuses to strip a message's only copy (guards
against deleting mail instead of archiving it). **Resolve-before-act is
built into the act layer** — it reads the current filename off disk at call
time, never from a cached listing; if it reports "message file not found on
disk," the index is stale — rerun `$IDX index` and retry.

- **Flag / unflag** (autonomous allowed; thread-wide clear-then-set, rule 2):
  resolve current local ids for every currently-flagged member of the thread
  (`himalaya envelope list -a <acct>-local -f <folder> -o json`, matched as
  above), `himalaya flag remove -a <acct>-local -f <folder> <id>... flagged`
  on all of them, then, if the thread should be blocked-on-me,
  `himalaya flag add -a <acct>-local -f <folder> <id> flagged` on exactly
  one member (the latest is the natural choice).
- **Move / resolve / trash** (approved): `$ACT move` or `$ACT remove` per
  the table above, applied to every non-system member of the thread.
- **Reply** (approved, draft always shown; runs on the IMAP account —
  reply/send is the one thing that isn't local):
  1. Compute `References` **from the local index**, not a live read: `$THR
     show --account <acct> --message-id <parent-id>` → take every member's
     `message_id` in date order up to and including the parent, that
     ordered list is the References header. This is a Phase 2.7
     simplification — the MVP had to fetch the parent's headers with a
     `--preview` read; the index already has the full thread graph.
  2. Resolve the parent's **IMAP-side** id (separate id-space from
     `-local`): `himalaya envelope list -a <acct> -f <folder> -o json`,
     matched by `from_addr`/`date_utc`/`subject`.
  3. `himalaya template reply -a <acct> -f <folder> <id>` — prefills
     `From`/`To`/`Subject`/`In-Reply-To`. Add `References:` yourself from
     step 1 (the template omits it).
  4. Write the body (quote below, reply above, matching the user's voice).
     Save the full template to a temp file.
  5. Show the complete draft to the user. Three exits:
     - **Send** (explicit approval of that exact draft):
       `himalaya template send -a <acct> "$(cat <tmpfile>)"`. Sending sets
       `\Answered` automatically (and marks `\Seen` on the original —
       accepted per rule 5's exception). The next sync/index picks it up.
     - **Save for later** ("save it, I'll finish on my phone"): write the
       full RFC822 (headers incl. In-Reply-To/References + body, LF line
       endings) to the account's drafts folder LOCALLY — **on stdin, never
       as an argument** (verified 4.5: the argument form silently saves an
       EMPTY message, and without stdin redirection the command hangs
       waiting for it):
       `himalaya message save -a <acct>-local -f "<tiers.drafts>" < <tmpfile>`
       Then resolve the draft's local id FRESH (`envelope list` — himalaya
       Maildir ids are unstable across operations; verified the id changed
       0→4 over one sync) and flag it:
       `himalaya flag add -a <acct>-local -f "<tiers.drafts>" <id> draft seen`.
       New files carry no `,U=` token — mbsync-safe upload; the batch-
       boundary sync (which must include the drafts folder) pushes it as a
       standard IMAP draft the phone can open, edit, and send. Log
       `final_action=draft` (decisive). Thread keeps its flag — the reply
       is still owed; `drafted: true` surfaces it next session. Sent from
       the phone later → the change report's Sent detection logs the
       manual reply; the draft's disappearance (clients delete on send) is
       expected report noise — don't log an event for it.
     - **Discard**: delete the temp file; log per the user's actual
       disposition of the thread.

## Reconciliation (new arrival on a filed thread)

New mail continuing an existing thread arrives in Inbox regardless of the
thread's filed location — delivery has no knowledge of it. Detected per
Triage's reconciliation check (a listed thread spanning `untriaged` + a
resolved tier). Three outcomes, always a fresh, explicit decision — arrival triggers
attention, it doesn't itself change state:

- **Stays resolved** (default expectation): move the new Inbox message to
  join its siblings at their existing location (`$ACT move` for just that
  message).
- **Reopens:** move the **whole thread** to `Active`.
- **Refiles:** move the **whole thread** to the new resolved location.

Log per the normal action vocabulary (archive / activate / trash as it
resolves); note `"reconciliation: stays-resolved"` / `"reopens"` /
`"refiles"` in the event's free-text `notes`.

## Logging (every decision, at act time)

```sh
HIST="${CLAUDE_SKILL_DIR}/scripts/history.py"

# Session-start context (recent notes, active rules, sender stats, contacts):
$HIST context [--senders addr1,addr2,...]

# One event per executed decision, at act time:
$HIST log-event --session "$SESSION" \
  --account gmail --sender "who@example.com" --subject "..." \
  --thread-state holding --category newsletter --inactive-days 4 \
  --proposed trash --final trash --decided-by approved \
  --from-folder INBOX --to-folder "[Gmail]/Trash" \
  [--message-id "..."] [--thread-root-id "..."] [--rule-id N] [--notes "..."] \
  [--unsubscribed]

# Batch of events after one bulk act.py/himalaya call (all-or-nothing insert).
# Write the REAL session timestamp into the JSON — a quoted heredoc (<<'JSON')
# does NOT expand $SESSION, and an unexpanded literal would corrupt the log:
$HIST log-events <<'JSON'
[
  {"session_started_at": "2026-07-09T18:15:00Z", "account": "gmail", "sender_addr": "a@example.com",
   "category": "spam-noise", "proposed_action": "trash", "final_action": "trash",
   "decided_by": "approved", "from_folder": "INBOX", "to_folder": "[Gmail]/Trash"}
]
JSON

# Notes, the moment something is noteworthy:
$HIST log-note --session "$SESSION" "resume state / context / watch item"
```

`decided_by`: `auto` (you acted autonomously — flags only), `approved` (user
approved as-is), `override` (user changed the outcome — always log the
original `proposed_action` alongside; **the richest learning signal** —
"leave it" after a proposal is `final_action=skip, decided_by=override`),
`manual` (detected via the indexer's change report, §Session flow step 6).

**Action vocabulary.** The `final_action` enum is
`flag | reply | archive | trash | skip | activate | draft`. `draft` (4.5)
= composed a reply but saved it to Drafts instead of sending — decisive,
like activate. `activate` is the 2.7
addition: moved to the Active tier — triaged, stays open, no flag — with
`to_folder=Active`. It's a real decision (counts as decisive in
`sender_stats`), which is exactly why it is NOT logged as `skip`: skips are
excluded from decisive-action rates, and conflating "I decided this stays
open" with "no decision was made" would corrupt the learning signal. Because
every Inbox thread now gets a real disposition decision (stay open / resolve
/ discard), this is an improvement on the MVP, where "holding" threads
sometimes went unlogged entirely.

Skip rules:
- A Claude-proposed skip on a thread that presented a real decision is
  logged **once per thread per session**. (Staying open is NOT a skip —
  it's `activate`. A genuine skip is "reviewed, deferred, still in Inbox"
  or a no-op pass on an already-`Active` thread.)
- Threads in `waiting` state are **not skip-eligible** — don't log skips for
  them, don't propose anything for them. A `waiting` thread mixed into a
  batch is not a decision point — pass over it silently.
- Threads never reviewed (session abandoned, "skip the rest") are **not
  logged as events**. Leave resume state in a session note instead.

**Interaction patterns are DB facts, not prose (5.5).** When the user
states a per-sender/per-source disposition — "always surface these
immediately," "summarize these for me, then toss them," "scan for anything
odd and trash the rest" — promote it into the DB **the moment it's
stated**, never just into a session note:
- Who-someone-is facts → `$HIST add-contact <addr> [--relation "plumber"]
  [--vip 1] [--notes "..."]` (upsert; asserted facts only — stats stay
  derived).
- Repeatable dispositions → `$HIST add-rule --predicate "sender = ..."
  --action <x> --origin user`. The rule action vocabulary is WIDER than
  the event enum: alongside plain actions (`trash`), an action may name an
  interaction pattern — `summarize-then-trash`, `scan-then-trash`,
  `surface-immediately`. Rules you propose from analytics evidence use
  `--origin claude-proposed` and are only added after the user approves.
- Retire with `$HIST set-rule <id> --active 0` when the user reverses.

Why this matters: `$HIST context` surfaces both tables every session, so a
pattern recorded once frames all future triage — while the same pattern in
prose notes scrolls away, and sender_stats alone can't distinguish "noise
I clear" from "content I consume and then clear." The rule records which
one it is.

Session notes (`session_notes` table) carry resume state, stats context,
watch items, and `unrecognized_folders`/`parse_errors` audit debt from the
change report. Write them **the moment** the noteworthy thing happens.

## Analytics (on demand — user asks for stats/trends/patterns, or you need rule-proposal evidence)

`ANA="${CLAUDE_SKILL_DIR}/scripts/analytics.py"` — DuckDB over both DBs,
read-only, canned queries only (never write raw SQL in-session; the §9
attach gotchas are encoded in the script once). **Invoke via
`uv run "$ANA" <subcommand>`** — a PEP 723 block self-provisions the
optional duckdb dependency; core triage never needs it. If `uv` is
unavailable, skip analytics gracefully — `$HIST context` still carries
sender_stats.

| Subcommand | Answers |
|---|---|
| `categories` | action mix + unsubscribe rate per category ("how often do I unsubscribe from marketing?") |
| `domains [--min-events N] [--category X]` | per-domain trash/reply/archive/override rates |
| `trends [--weeks N]` | weekly volume + action mix + distinct senders/sessions |
| `rules` | per-rule usage + override rate (incl. unused rules) |
| `sessions [--limit N]` | per-session event counts, mix, duration spread |
| `candidates [--min-events N] [--days N]` | **rule-proposal fuel**: senders you nearly always trash that keep arriving, not yet covered by a rule — cite these numbers when proposing a rule |

## PARA review phase (on-demand / weekly, not every session)

Project graduation runs on a slower loop than per-session triage.

1. **Signal:** per `<tiers.ongoing>/<group>/<project>` subfolder —
   `$THR list --tier ongoing`, grouped by `folder`, look at days since the latest member's
   `date_utc` and whether any member is flagged. Falling volume + no open
   flags is a candidate.
2. **Propose, never auto-execute:** "`Areas/…/Fence`: 0 new messages, 0
   flagged threads in 45 days — graduate to Archive?"
3. **Naming on graduation:** year-of-closure prefix (judged from when the
   folder was last genuinely active, not stragglers), lands in its family
   folder when one fits — `Areas/W-141st-Ln` → `Archive/Housing/20XX-
   W-141st-Ln`. Create the destination first if new (`himalaya folder
   create`), then `$ACT move` every message in the source folder to it in
   one batch call.
4. **No event is logged** — folder-level, no per-sender learning signal;
   the active window is reconstructable from Maildir mtimes
   (`CopyArrivalDate yes`) if ever needed.

## Account quirks

- **`mbsync` requires `-c "$MBSYNCRC"` on every invocation** — not
  auto-discovered (verified, 2.1). Box-scoped syntax:
  `mbsync -c "$MBSYNCRC" 'gmail:INBOX,Areas/Taxes'`. **One account per
  invocation** — combined `icloud gmail` calls flake with
  `read: unexpected EOF` ~25% of the time (opening the second channel);
  a single retry always recovered.
- **`himalaya envelope list` defaults to a 10-envelope page** on every
  backend — a listing that "looks complete" at 10 is silently truncated.
  Always pass `-s 500` (or page with `-p`). Cost a live session ~9 tool
  calls to rediscover.
- **`$ACT` accepts message-ids on stdin** (`... move ... -` with one id
  per line) — use it whenever an id contains shell-hostile characters
  (backslashes bit a live session) or for long batches.
- **Gmail:** `envelope search` fails (no IMAP SORT) — only `envelope list`,
  on both the IMAP and `-local` accounts. The star is `\Flagged`. Gmail's
  `[Gmail]/Trash` and `[Gmail]/Spam` auto-purge in ~30 days — a hard
  ceiling, vs. a comfortable floor on iCloud.
- **Moves via `-local` himalaya are unusable** — verified live (2.6): the
  `,U=<uid>` filename token travels to the target folder and mbsync
  hard-errors, having already synced the source-side deletion (a real
  stranding risk). `$ACT` is the only move path. Flag ops via `-local`
  himalaya, by contrast, are mbsync-safe (suffix-only rename, `,U=`
  preserved in place).
- **iCloud:** himalaya's save-copy is the only Sent copy (iCloud SMTP does
  not auto-save) — leave `save-copy` at its default on the `icloud` account.
- **Gmail SMTP auto-saves to Sent** — `message.send.save-copy = false` is
  set on the `gmail` account in himalaya's config; don't "fix" a missing
  local save there.
- Envelope JSON shape (verified against himalaya v1.2.0, both IMAP and
  Maildir backends): bare array; `id` is a **string**; `flags` is an array
  of strings (e.g. `["Flagged"]`); `from`/`to` are `{name, addr}` objects;
  `date` is local-tz RFC-ish. Maildir-backend ids are internal to himalaya
  (an id-mapper over the folder), unrelated to the index's `maildir_key` —
  always resolve them fresh, immediately before use.
