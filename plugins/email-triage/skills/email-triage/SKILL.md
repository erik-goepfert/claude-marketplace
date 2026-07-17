---
name: email-triage
description: Triage email across iCloud and Gmail accounts — thread-centric review of a local Maildir mirror (synced by mbsync, indexed to SQLite) with flag/reply/archive/trash decisions, approval gates, and a persistent history DB that makes each session smarter than the last. Use when the user asks to triage, clean up, or process their email/inbox.
allowed-tools: Bash(himalaya *) Bash(*history.py*) Bash(*init_history_db.py*) Bash(*indexer.py*) Bash(*threads.py*) Bash(*act.py*) Bash(*analytics.py*) Bash(mbsync *)
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
```

- `config.yaml` (in `$DATA`) — accounts, per-account system-folder map,
  `maildir_path`, `server_rules` registry. **Read it at session start; never
  improvise folder names.** `Active`, `Areas/<area>[/<project>]`, and
  `Archive[/<family>]/<chapter>` are **not** in config — they're fixed
  canonical names, identical on both accounts (see Canonical destinations).
  Caveat: `accounts.gmail.folders.archive` is a leftover MVP-era explicit
  label, kept only for the interim (implementation-plan 2.3) — it is **not**
  the flat-leaf archive destination under this model; see Acting.
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
4. **Unsubscribe links are surfaced, never followed.** Extract and present
   them; never fetch, click, or otherwise interact with them.
5. **`\Seen` is the user's signal, not triage state — your own reads must
   never move it.** A message counts as read only once the user has been
   shown its content or a representative summary of it. Concretely, on the
   `-local` (Maildir) accounts:
   - Any read for your own purposes (unsubscribe extraction, resolving
     `ambiguous`, thread-state checks) uses `--preview` —
     verified (2.6): a plain local `message read` stamps `S` on the Maildir
     filename, which mbsync would then sync as `\Seen` to the server;
     `--preview` leaves the filename untouched.
   - Only mark a message read by explicitly setting it —
     `himalaya flag add -a <acct>-local -f <folder> <id> seen` — at the
     moment you actually present its content or summary to the user.
   - Exception: `himalaya template reply` / `message reply` (run against the
     **IMAP** account, since sending is IMAP-only) have no preview
     equivalent and do mark `\Seen`. Accepted, since a reply draft quotes
     the original back to the user before anything sends.

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
   mbsync -c "$MBSYNCRC" 'gmail:INBOX,Active,[Gmail]/Sent Mail'
   mbsync -c "$MBSYNCRC" 'icloud:INBOX,Active,Sent Messages'
   ```
   (~60–90 s total; gmail's per-invocation overhead dominates.)
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
   `unrecognized_folders`. This is provably-external change (the skill
   re-indexes its own actions immediately as it acts, §Acting) — log it:
   - `moved` entries whose destination tier is `trash` → one manual event,
     `final_action=trash`. Destination tier `archive`/`areas` →
     `final_action=archive`. Destination tier `active` →
     `final_action=activate`.
     All: `decided_by=manual`, `proposed_action=final_action`,
     `from_folder`/`to_folder` from the report entry.
   - `flag_changed` entries → `final_action=flag`, `decided_by=manual`
     (covers both an external flag-set and an external flag-clear).
   - `new` entries whose folder is the account's Sent folder → a manual
     *reply* event (`final_action=reply`, `decided_by=manual`); resolve
     `sender_addr` via `$THR show --account <acct> --message-id <id>` and
     take the thread's other participant.
   - `parse_errors` > 0 → surface to the user, don't silently drop it.
   - `unrecognized_folders` → surface as audit debt (the 2.3 folder audit
     hasn't finished absorbing something); log a session note, don't infer
     Archive.
7. **Assemble threads.** `$THR list --db "$IDXDB"` — default tiers
   `inbox`+`active` (every thread with ≥1 member in either; the full member
   list, including any Areas/Archive/Sent siblings, always comes back —
   this is what makes reconciliation detectable, see below).
8. **Priority scan**, then **breakdown**, then **classify-first, easiest-
   first** (see Triage). **Before the FIRST act batch: verify Stage 2 is
   done** — `[ -f "$DATA/.fullsync-done" ]`. If not yet, tell the user
   ("full sync still finishing — a minute or two") and wait for the
   marker; never act while mbsync may be walking the Maildir. Once
   present: re-index and consume that change report (external changes
   from the non-priority folders surface here — log manual events per
   step 6), then act normally.
   **Act in approved batches** (see Acting): resolve-
   before-act, `$ACT` for moves/removes, `-local` himalaya for flags,
   IMAP himalaya for replies, log events at act time, re-index touched state
   (a full `$IDX index` run is cheap, ~25 s — no need to scope it), then a
   **box-scoped sync** at the batch boundary:
   `mbsync -c "$MBSYNCRC" 'gmail:INBOX,Areas/Taxes'` (list exactly the
   folders the batch touched) — never a full-channel sync per action; Gmail's
   ~60–80 s per-invocation overhead is paid regardless of scope, so batch
   actions per sync.
   **HARD INVARIANT: a batch is not complete until its sync has run.**
   Never begin the next batch — and never report a batch as done — while
   the previous batch's changes exist only in the local mirror. Why: in the
   first live session, six batches of trashes sat local-only for 35 minutes
   until the user asked "have you pushed our changes to the server?" — a
   crash in that window would have left all of it live in the mailbox.
   Act → log → re-index → **push** is one unit.
9. **Session end — background chain, user is done immediately.** Every
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
  computed (days since the latest non-trash/junk member).
  **A `waiting` thread gets no proposal and no log entry, ever — not even a
  skip.** Lifecycle state and content category are independent axes, so a
  `waiting` thread can still land in `actionable` or any other category and
  surface in that category's list; leave it out of the batch entirely.
- **Content category:** `spam-noise` | `marketing` | `newsletter` |
  `passive-update` (receipts, notifications, alerts) | `ambiguous` |
  `actionable`.
- **Current tier matters for what's being decided.** A thread whose only
  live-tier member is in `inbox` needs the full 3-way disposition (stay open
  in `Active` / resolve to `Areas`-or-`Archive` / discard). A thread already
  in `active` just needs flag maintenance (flag / unflag / resolve / trash)
  — it's already filed.
- **Reconciliation check.** If a listed thread's members span **both**
  `inbox` and a resolved tier (`areas`/`archive`), it's a new arrival on an
  already-filed thread — handle per Reconciliation below, not as a fresh
  thread.

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
here's the link to review/revoke" (surfaced, never followed, rule 4); log as
`category=actionable` with a `notes` value noting the priority-scan hit. Only
once these are resolved does the regular flow proceed.

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
3. **Targeted body reads only**, for exactly two reasons: resolving an
   `ambiguous` classification, or extracting unsubscribe links — category by
   category as worked, never upfront. Resolve the current local id
   immediately before reading (`himalaya envelope list -a <acct>-local -f
   <folder> -o json`, matched against the thread member by `from_addr` +
   `date_utc` — Maildir ids are internal to himalaya, not derivable from the
   index), then:
   `himalaya message read --preview -a <acct>-local -f <folder> <id>`
   If resolving the ambiguity means actually showing the user the content,
   mark it read at that point:
   `himalaya flag add -a <acct>-local -f <folder> <id> seen`
4. **Unsubscribe extraction:** extract BOTH the `List-Unsubscribe` header
   AND the body's footer unsubscribe link, and **surface the body link as
   primary** (revised 2026-07-16 after live sessions: header links are
   built for RFC 8058 automated one-click POSTs and frequently fail when a
   human clicks them; body links on ESP click-tracking domains worked
   reliably). Note: `--header list-unsubscribe` does NOT suppress the
   body — looping it over many ids produces a giant blob. Read each
   message once to a temp file and grep both the header and the footer out
   of it:
   `himalaya message read --preview -a <acct>-local -f <folder> <id> > "$tmp"`
   **Link formatting is load-bearing:** present the FULL URL as a markdown
   link — never truncate/elide it (`...` breaks the token) and never wrap
   it in backticks (unclickable in the terminal). Both failure modes cost
   real user round-trips in the first live session.
   The user acts on the link themselves (rule 4). Ask ("unsubscribe too,
   or just clear this one?") and log the answer via `--unsubscribed` on
   that event. Don't ask per-thread if the user gave a batch answer
   ("unsubscribe from all of these") — apply it to each event in the batch.
   **A confirmed unsubscribe is also trash approval** (user rule,
   2026-07-10): "I've unsubscribed" means you may trash the associated
   message or batch without a separate ask — the confirmation IS the
   approval for those specific messages. Log normally
   (`final_action=trash, decided_by=approved, --unsubscribed`). It approves
   only the messages the unsubscribe was about, nothing wider.
5. **Noise escalation — when unsubscribe isn't possible.** Three-step
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
6. Apply active rules from `history.db` when they match — still within the
   approval gates; a rule justifies a proposal, not an autonomous action
   (flags excepted).

## Thread assembly

No in-context grouping anymore — `$THR` builds threads from the index's
Message-ID/In-Reply-To/References graph plus a guarded subject fallback
(participant overlap required), and derives lifecycle state directly.

- `$THR list --db "$IDXDB" [--account icloud|gmail] [--tier t1 --tier t2 ...]
  [--sender substr] [--subject-contains substr]`
  — every thread with ≥1 member in the given tier set (default `inbox`,
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
  thread's location, **every non-system member moves as a unit**.

## Acting

**Canonical destinations** (fixed names, not in `config.yaml`, identical
across accounts except where noted):

| Disposition | iCloud | Gmail |
|---|---|---|
| Stays open | `$ACT move --to Active` | `$ACT move --to Active` |
| Resolve, ongoing area | `$ACT move --to "Areas/<area>[/<project>]"` | same |
| Resolve, one-off closure (flat leaf) | `$ACT move --to "Archive/<year>-<Name>"` | `$ACT remove` (strip the last label — message persists in All Mail only; the indexer infers tier `archive` by absence, taxonomy's "virtual Archive") |
| Resolve, closed chapter in a family | `$ACT move --to "Archive/<Family>/<year>-<Name>"` | `$ACT move --to "Archive/<Family>/<year>-<Name>"` — a **real** label (hierarchy can't be expressed by absence); create it first if new: `himalaya folder create -a gmail "Archive/<Family>/<year>-<Name>"` (verified two-level nesting, §13.6) |
| Discard | `$ACT move --to "<config accounts.<acct>.folders.trash>"` | same |

`$ACT move --db "$IDXDB" --maildir "$MAILDIR/<acct>" --account <acct> --from
<folder> --to <folder> <message-id>...` and `$ACT remove ... --from <folder>
<message-id>...` both take multiple message-ids in one call — use this for
batch actions. `$ACT remove` refuses to strip a message's only copy (guards
against deleting mail instead of archiving it). **Resolve-before-act is
built into `$ACT`** — it reads the current filename off disk at call time,
never from a cached listing; if it reports "message file not found on disk,"
the index is stale — rerun `$IDX index` and retry.

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
  5. Show the complete draft to the user. Only after explicit approval:
     `himalaya template send -a <acct> "$(cat <tmpfile>)"`.
  Sending sets `\Answered` automatically (and marks `\Seen` on the original
  — accepted per rule 5's exception). The next sync/index picks this up
  locally.

## Reconciliation (new arrival on a filed thread)

New mail continuing an existing thread arrives in Inbox regardless of the
thread's filed location — delivery has no knowledge of it. Detected per
Triage's reconciliation check (a listed thread spanning `inbox` + a resolved
tier). Three outcomes, always a fresh, explicit decision — arrival triggers
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

**Action vocabulary (Phase 2.7).** The `final_action` enum is
`flag | reply | archive | trash | skip | activate`. `activate` is the 2.7
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

1. **Signal:** per `Areas/<area>/<project>` subfolder — `$THR list --tier
   areas`, grouped by `folder`, look at days since the latest member's
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
