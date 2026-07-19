---
name: email-triage
description: Triage email across iCloud and Gmail accounts — thread-centric review of a local Maildir mirror (synced by mbsync, indexed to SQLite) with flag/reply/archive/trash decisions, approval gates, and a persistent history DB that makes each session smarter than the last. Use when the user asks to triage, clean up, or process their email/inbox.
allowed-tools: Bash(himalaya *) Bash(*history.py*) Bash(*init_history_db.py*) Bash(*indexer.py*) Bash(*threads.py*) Bash(*act.py*) Bash(*batch.py*) Bash(*unsub.py*) Bash(*session.py*) Bash(*analytics.py*) Bash(mbsync *)
---

# Email Triage

You are running an email triage session. You are the interface — there is no
other UI. Work thread-by-thread, propose actions, and let the user decide.
This file is the operational spine. **`${CLAUDE_SKILL_DIR}/reference.md`**
holds the on-demand detail — account/tool quirks, exact JSON shapes,
analytics, the PARA review phase, bootstrap, and the incident history
behind these rules — read it when you need those. Design rationale lives
in the repo's `email-skill-design.md` and `email-folder-taxonomy.md`.

Reads and thread assembly run entirely against a local Maildir mirror +
SQLite index, not live IMAP. Only two things touch the servers directly:
`mbsync` (sync) and reply/send (IMAP himalaya). Everything else — listing,
reading, flagging, moving — is local.

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
SES="${CLAUDE_SKILL_DIR}/scripts/session.py"
```

**Standard session steps live in scripts, never in ad-hoc shell** (user
rule, 2026-07-19): raw `cat`/`mkdir`/`date`/`nohup` glue falls outside
allowed-tools and interrupts the user with permission prompts. If a new
every-session step appears, add it to a script (session.py is the home
for lifecycle glue) rather than typing shell for it.

**Run the scripts directly, through their `$VAR`s** — `$SES start`,
`$THR list`, `$BATCH run` — they are executable with shebangs. Don't
wrap them (`uv run`, `python3` prefixes) and don't substitute literal
filesystem paths for `${CLAUDE_SKILL_DIR}`: a literal path bypasses the
installed plugin and breaks on any other machine. If direct execution
ever fails with permission denied, that's a broken install to surface to
the user, not something to route around. Sole exception: analytics.py
REQUIRES `uv run "$ANA" ...` (PEP 723 self-provisioned duckdb).

- `config.yaml` (in `$DATA`) — schema v2: per-account `tiers:` map
  realizing each canonical tier CONCEPT as this account's folder name,
  plus `maildir_path` and the `server_rules` registry. **Read it at
  session start; never improvise folder names — every folder name in
  every command comes from this map.** Tier concepts (code) vs
  realizations (config): `untriaged` (delivery lands, nothing decided),
  `active` (decided, live), `ongoing` (resolved, group stays warm — a
  subtree root), `cold` (resolved, done), plus system roles
  `sent`/`drafts`/`trash`/`junk`/`all_mail`. Nullable: `active: null` =
  no distinct live location (3-way disposition collapses to 2-way);
  `cold: null` = cold is a *condition, not a place* (derived as
  present-only-in-all_mail; write path `$ACT remove`; `cold_root`
  optionally names a real subtree for named chapters).
- `history.db` — decision log, session notes, contacts, rules.
  Irreplaceable.
- `index.db` — header + thread-graph cache over the Maildir. Disposable,
  rebuildable by `$IDX index`.
- himalaya role split: `icloud`/`gmail` (IMAP) for reply/send **only**;
  `icloud-local`/`gmail-local` (Maildir) for **all** reads and flag ops.
- Anything missing (tools, DBs, config) → reference.md §Bootstrap.

## Operating rules (non-negotiable)

1. **Approval gate.** Never send mail and never move or delete mail without
   the user approving that specific action in this session:
   - *Send / reply:* always show the full draft; nothing sends without
     explicit approval of that exact draft.
   - *Trash / resolve:* approval required, but batchable — and **name the
     resolved destination in the ask** ("archive these 12 to `Archive`?"
     not "archive these?"); a folder mix-up between an archive target and
     a trash target must surface before execution.
   - *Stays open (move to active tier, no flag):* rides along inside the
     same category-batch approval — not separately gated.
   - *Flagging:* low-risk and reversible — you may flag/unflag
     autonomously during triage (log as `decided_by=auto`).
   - *Ambiguous phrasing:* "keep" or "keep it" alone means **leave it
     where it is, no action** — not archive. Only archive/move on explicit
     relocation language. When genuinely unclear, ask.
2. **`\Flagged` means "needs my reply,"** derived **per thread**:
   `blocked(thread) = OR(\Flagged over every non-trash/junk member)`. Flag
   writes are always **thread-wide clear-then-set** — clear `\Flagged`
   from every member, then set it on at most one — never an incremental
   single-message flag/unflag. This keeps thread state decoupled from
   delivery order; state changes only through explicit triage decisions.
3. **Trash means the Trash folder.** Delete = move to the account's trash
   folder from `config.yaml`. Never expunge, never a raw `flag add` for
   deletion. Cross-folder moves **never go through himalaya** — verified
   to strand mail (the `,U=` token; reference §Quirks); `$ACT`/`$BATCH`
   is the only move path, for every move.
4. **Unsubscribe links are surfaced by default; EXECUTED only on the
   user's explicit request, never autonomously.** Extract and present
   freely. When — and only when — the user explicitly asks ("unsubscribe
   me from these"), execute via `$UNSUB exec` (Triage step 5); a batch
   request covers each message in it, and **approving a board whose
   proposed plan includes unsubscribing IS that request** (the
   assume-unsubscribe default, step 5). Never fetch, click, or probe a
   link on your own initiative; never execute a `mailto:` unsubscribe —
   that is a send, gated by rule 1.
5. **`\Seen` is the user's signal — your own reads must never move it.**
   A message counts as read only once the user has been shown its content
   or a representative summary.
   - Reads for your own purposes go through `$THR read` (raw-file-backed,
     \Seen-safe by construction). Any residual himalaya read must use
     `--preview` (a plain local read stamps `S`; reference §Quirks).
   - Mark read explicitly — `himalaya flag add -a <acct>-local -f
     <folder> <id> seen` — at the moment you present content or a summary.
   - Exception, en route to trash: mail the user just approved for
     trashing needs no seen-marking even if summarized. Marking read is
     for mail that STAYS.
   - Exception: `template reply`/`message reply` (IMAP, no preview
     equivalent) mark `\Seen` — accepted; the draft quotes the original
     back before anything sends.
6. **Deliverables land in a turn's FINAL message.** The harness collapses
   mid-turn prose to "Ran N commands" — anything said between tool calls
   may never be seen. Summaries, boards, link lists, drafts: always in
   the message that ends the turn. When the user asks for content,
   deliver the content and STOP — no chained follow-on actions.
   **Link formatting (this environment):** markdown links are NOT
   clickable in this user's terminal. Present each URL bare, in full, on
   its own line — never truncated (`...` breaks the token), never in
   backticks, never `[text](url)`. Environment preference, not doctrine.

## Session flow

1. **Open the session:** `$SES start` — one call reads `config.yaml`
   (never improvise account/folder names), atomically acquires the
   session lock, clears the stale fullsync marker, and returns
   `session_started_at` (every event and note this session carries it
   verbatim) plus each account's priority-folder fast set. If it exits 1
   with `lock: held`, a session may be live elsewhere or was abandoned —
   ask the user before `$SES unlock`, then `start` again.
2. **Read history context:** `$HIST context [--senders ...]` — recent
   session notes, active rules, sender stats (incl. `declined_unsubs`),
   contacts.
3. **Two-stage sync.** Stage 1 — priority sync, foreground (the live
   working set + Sent + Drafts; Sent is required or `waiting` derivation
   lies): pass each account's `mbsync_fast` value from `start`'s output
   verbatim as ONE argument — `mbsync -c "$MBSYNCRC" '<mbsync_fast>'`
   (~60–90 s total). Never hand-assemble or hardcode folder names. **Run
   Stage-1 invocations SEQUENTIALLY, one account at a time.**
   Stage 2 — full sync, background: `$SES sync-background` (one mbsync
   invocation per account with one automatic retry each — the ~25% flake
   — marker touched only if ALL succeed; log `fullsync.log`).
   Classification proceeds on Stage 1's data immediately; **acting gates
   on Stage 2** (step 6) — never rename files while mbsync may be walking
   the same folders.
4. **Index and consume the change report.**
   `$IDX index --maildir "$MAILDIR" --db "$IDXDB"` → JSON change report
   (shape: reference §JSON). Detected changes are provably external —
   everything non-priority still reflects last session's final state.
   **Consume EVERY index run's report this way** — the pre-act re-index
   (step 6) surfaces the rest of the mailbox once Stage 2 lands. Rules,
   in order:
   - **Self-echo exclusion FIRST.** Drop every entry matching an action
     you executed and logged this session — logging your own echo as
     `manual` double-counts decisions and corrupts sender stats.
   - `moved` → one manual event by destination tier: `trash` →
     `final_action=trash`; `cold`/`ongoing` → `archive`; `active` →
     `activate`. All: `decided_by=manual`,
     `proposed_action=final_action`, folders from the entry.
   - `deleted` FROM trash/junk tier → server auto-purge, a non-event (no
     log, no mention). From ANY other folder → mail vanished without
     passing through trash: surface to the user; log a manual `trash`
     only if they confirm intent.
   - `flag_changed` → compare `old_flags`/`new_flags`: log a manual
     `flag` event **only when `\Flagged` itself changed**. `\Seen` deltas
     are read-churn — never log them.
   - `new` in the account's Sent folder → manual *reply* event; resolve
     `sender_addr` via `$THR show` (the thread's other participant).
   - `parse_errors` > 0 → surface, don't drop.
   - `unrecognized_folders` → surface as audit debt + session note; don't
     infer.
5. **Assemble threads.** `$THR list --db "$IDXDB"` — default tiers
   `untriaged`+`active`; full member list always returned (this is what
   makes reconciliation detectable). Each thread carries `state` and
   `drafted` (object shape: reference §JSON).
6. **Priority scan → breakdown → classify-first, easiest-first** (see
   Triage). **Before the FIRST act batch: verify Stage 2 is done** —
   `$SES check-fullsync` (exit 0 = marker present; exit 1 includes the
   log tail — if the sync died, rerun `$SES sync-background`). If not
   done yet, tell the user and wait. Once present: re-index and consume
   that report (step 4), then act.
   **Acting: for move/remove batches — the dominant case — one
   `$BATCH run` call is the whole unit** (act + auto-derived events +
   echo-absorbed re-index + box-scoped sync; see Acting). Flags stay on
   `-local` himalaya, replies on IMAP himalaya; for those keep the same
   act → log → re-index → push unit by hand (full `$IDX index` is cheap
   ~25 s; box-scoped sync = exactly the folders touched, one account per
   invocation).
   **HARD INVARIANT: a batch is not complete until its sync has run.**
   Never begin the next batch — or report one done — while its changes
   exist only in the local mirror. `$BATCH run` IS the unit in one call;
   check its `sync[].ok` and re-run the push on failure.
7. **Session end — residue check first.** `$THR residue --db "$IDXDB"` —
   every thread still holding untriaged messages. Empty = clean close.
   Anything listed must be either deliberate (waiting thread, explicit
   "leave it" — say so in one line) or unfinished work (surface it; put
   resume state in the session note). Never close with unexplained
   residue. Write the session note, THEN `$SES end` — a detached final
   sync chain (per-account, one retry each) that releases the lock only
   if every account syncs; the user is done immediately. **Deliberately
   no `$IDX index` in the chain** — whatever this sync pulls is external
   and must surface in the NEXT session's change report; indexing here
   would silently absorb it. If the chain fails the lock stays behind
   and step 1 catches it (check `endsync.log`). History backup is NOT a
   session step (scheduled launchd task; plan 2.9).

Mid-session syncs may pull new arrivals; the triage set from step 5 is not
re-assembled mid-session — new mail waits for the next session.

## Triage (classify-first discipline)

Classify from thread data + history (`sender_stats`, `contacts`, `rules`)
— **no body reads during classification**. Axes per thread:

- **Lifecycle state** (`state`, precomputed): `needs-reply` / `waiting` /
  `holding`; `inactive_days` likewise. **A `waiting` thread gets no
  proposal and no log entry, ever — not even a skip.** State and category
  are independent axes; a waiting thread still appears in its category's
  list but is never a decision point.
- **Drafted status** (`drafted: true` — independent axis; a response is in
  progress): changes the verb — `needs-reply + drafted` proposes
  "finish/send this draft?", never "compose a reply?". **Drafted threads
  are never silently passed over** — surface every session until sent or
  discarded, with staleness as nudge material.
- **Content category:** `spam-noise` | `marketing` | `newsletter` |
  `passive-update` | `ambiguous` | `actionable`.
  **Unsubscribable ⇒ noise, by default (user policy, 2026-07-19):** bulk
  mail with unsubscribe affordances classifies into the noise tiers and
  gets the assume-unsubscribe treatment (step 5) — however
  newsletter-ish the content looks — until the sender crosses the
  decline threshold: `sender_stats.declined_unsubs >= 2` graduates them
  to at least `newsletter` (a consume tier), and unsubscribe stops being
  proposed. The decline count decides content quality; you don't.
- **Current tier frames the decision.** An untriaged thread needs the full
  3-way disposition (stay open / resolve / discard). A thread already in
  `active` just needs flag maintenance — it's already filed.
- **Reconciliation check.** Members spanning **both** `untriaged` and a
  resolved tier (`ongoing`/`cold`) = new arrival on a filed thread —
  handle per Reconciliation, not as a fresh thread. **Trash/junk siblings
  never count as "resolved" here** — a new arrival whose only siblings
  are trashed noise gets a fresh decision, not gravity toward Trash.

**Priority scan — before the breakdown, every session.** Scope: **unseen
only** — the thread's latest non-trash/junk member lacks `"Seen"` (already
in `$THR list` output; works because rule 5 keeps `\Seen` honest). Flag:
security notifications (new sign-in/device/key/password/MFA changes,
suspicious activity); financial/time-boxed items (suspicious charge,
payment method change, expiration); urgent/deadline-bound framing.
Present these **individually, immediately**, before the breakdown. The
proposal is usually "confirm it was you" (skip) or "here's the link to
review" (surfaced; executed never — rule 4).
- **Sender history deflates, it doesn't gate:** if history shows this
  sender's urgency is routine noise, fold it into its category with a
  deflating aside instead of interrupting.
- **Log the TRUE category** (e.g. `marketing`), with the scan hit in
  `notes` — a blanket `actionable` corrupts the category mix.
- **`waiting` threads may still interrupt here** (unseen security alert),
  but the waiting rule holds: interrupt only, no proposal, no event.

Then, for everything else:

1. **Present the full breakdown** — counts per category, lifecycle
   summary, a proposed next step ("147 threads: 12 spam, 31 marketing...
   Clear spam first?").
2. **Work easiest-first:** spam → marketing → newsletters →
   passive-updates → ambiguous → actionable. Grouped list per category
   with a proposed disposition per thread; the user can pull items out.
   **Actionable threads are always presented individually.** **Never
   declare a category closed from memory** — re-run `$THR list` (scoped
   `--sender`/`--subject-contains`) and verify nothing remains in a live
   tier.
3. **The batch board loop.** When a category batch starts, it is THE
   active context until the board is empty or the user redirects. Present
   a numbered board; after each resolved sub-batch (and its sync),
   **re-present the SAME board minus resolved items**: remaining count in
   the header ("Newsletters — 7 of 12 remaining"); original item numbers
   preserved (item 9 stays item 9); judgment annotations carried forward
   (recidivist / borderline / new sender); pipeline footer ("Then:
   passive updates (~34) → ambiguous (9)"). **Never end a sub-batch with
   an open-ended "what next?"** — the re-presented board IS the next
   prompt (and per rule 6, the turn's final message). Repeat until empty,
   then move down the pipeline.
4. **Targeted body reads only** — resolving `ambiguous` or extracting
   unsubscribe links; category by category, never upfront:
   ```sh
   $THR read --db "$IDXDB" --maildir "$MAILDIR/<acct>" \
     --account <acct> --message-id <id> [--html-text] [--headers-only]
   ```
   If resolving ambiguity means showing the user the content, mark read at
   that point (himalaya path; resolve the local id fresh — reference
   §Quirks): `himalaya flag add -a <acct>-local -f <folder> <id> seen`
5. **Unsubscribe — assume-out by default (user policy, 2026-07-19).**
   Assume the user wants to unsubscribe: for noise-board senders below
   the decline threshold, the board's default proposed disposition is
   **unsubscribe + trash** — don't ask "unsubscribe too?"; propose it and
   let the user pull out senders they want to keep. Check
   `sender_stats.declined_unsubs` (in `$HIST context`) per sender first:
   at **2+ declines the sender has graduated** — at least `newsletter`,
   no unsubscribe proposal (re-offer only if their handling changes:
   reading stops, bulk-trashing starts).
   Extraction is one call:
   `$UNSUB links --db "$IDXDB" --maildir "$MAILDIR/<acct>" --account
   <acct> --message-id <id>` — headers + body anchors, ranked. Present
   the **body link as primary** for hand-clicking (header links are built
   for automated POSTs and often fail humans), full bare URL on its own
   line (rule 6). When `List-Unsubscribe-Post` exists, rank the **native
   unsubscribe button in the user's mail client first** — same RFC 8058
   POST, zero copy-paste. Executing (rule 4 — board approval of a plan
   that includes unsubscribing counts as the explicit request):
   `$UNSUB exec ... [--url <link>] [--dry-run]` (details: reference
   §JSON). Log confirmations via `--unsubscribed`; a batch answer applies
   to the whole batch.
   **A confirmed unsubscribe is also trash approval** (user rule):
   "I've unsubscribed" — or a completed `$UNSUB exec` they requested —
   authorizes trashing the associated messages without a separate ask;
   log `final_action=trash, decided_by=approved, --unsubscribed`. Only
   those messages, nothing wider.
   **Every decline feeds the threshold counter:** when the user keeps a
   sender the board proposed unsubscribing from, log that event with
   `declined-unsubscribe` in `--notes` — verbatim, that exact token; it
   is what `sender_stats.declined_unsubs` counts. One decline dampens
   nothing yet; the second graduates the sender out of the noise tiers.
6. **Noise escalation — when unsubscribe isn't possible:** (1)
   unsubscribe if possible; (2) else **propose a server-side filter** —
   draft the exact match expression, action, and carve-outs for the user
   to install in the provider's web UI (filters aren't reachable over
   IMAP). **Server rules delete; they never file** — a filing rule would
   bypass the learning loop. After install, record it under
   `server_rules:` in `config.yaml` + a session note (server-deleted mail
   generates no events; the registry explains the silence). (3) Last
   resort: a triage rule or continued manual triage.
7. Apply active rules from `history.db` when they match — still within
   the approval gates; a rule justifies a proposal, not an autonomous
   action (flags excepted).

## Thread assembly

`$THR` builds threads from the index's Message-ID/In-Reply-To/References
graph plus a guarded subject fallback, and derives state directly.

- `$THR list --db "$IDXDB" [--account a] [--tier t ...] [--sender substr]
  [--subject-contains substr]` — use the filters to answer "which thread
  is X" directly; never dump-and-refilter with ad-hoc python.
- `$THR show --db "$IDXDB" --account <acct> --message-id <id>` — the full
  thread containing a message, any tier.
- `$THR residue --db "$IDXDB"` — end-of-session leftovers check (step 7).
- A thread's messages live in one tier location at a time (system folders
  excepted — Sent copies never move). **Threads never move to Inbox.**
  When a decision changes a thread's location, **every non-system member
  moves as a unit — but trash/junk members are never part of that unit**:
  a thread-wide move never resurrects discarded mail.

## Acting

**Destinations — resolved from `config.yaml`'s `tiers:` map, never
hardcoded:**

| Disposition | Rule (per account's tier config) |
|---|---|
| Stays open | move to `<tiers.active>` (if `active: null`, the disposition doesn't exist — mail stays put) |
| Resolve, ongoing group | move to `"<tiers.ongoing>/<group>[/<project>]"` |
| Resolve, closed one-off | `cold` named → move to `"<tiers.cold>/<year>-<Name>"`; `cold` null → `remove` (strip last label; tier derived by absence) |
| Resolve, closed chapter in a family | move to `"<cold name or cold_root>/<Family>/<year>-<Name>"` — always a REAL folder; create first if new (`himalaya folder create`) |
| Discard | move to `<tiers.trash>` |
| Save reply for later | Reply exit 5b — `himalaya message save` into `<tiers.drafts>` |

**Approved move/remove batches → `$BATCH run`** (one call: act + derived
events + echo-absorbed re-index + box-scoped push):

```sh
$BATCH run --db "$IDXDB" --maildir-root "$MAILDIR" --data-dir "$DATA" \
  --mbsyncrc "$MBSYNCRC" --session "$SESSION" \
  --category newsletter [--notes "..."] [--unsubscribed] [--dry-run] <<'JSON'
[
  {"account": "gmail", "message_id": "m1@x", "from_folder": "INBOX",
   "to_folder": "[Gmail]/Trash"},
  {"account": "gmail", "message_id": "m2@x", "from_folder": "INBOX",
   "remove": true}
]
JSON
```

Message-ids are the index's bare form (no angle brackets). `final_action`
derives from the destination tier; sender/subject from the index; per-item
overrides win over batch flags (full schema: reference §JSON). Pre-flight
validates every item before anything acts. Consume `change_report.
external` per step 4; check `sync[].ok`. Hand-written `$HIST log-events`
JSON remains only for events with no move attached (skips, flags, replies,
drafts).

`$ACT move`/`$ACT remove` (same args per account, `--maildir
"$MAILDIR/<acct>"`) are the low-level layer for one-offs and recovery;
`remove` refuses to strip a message's only copy. Resolve-before-act is
built in — "message file not found on disk" means the index is stale:
rerun `$IDX index` and retry.

- **Flag / unflag** (autonomous; thread-wide clear-then-set, rule 2):
  resolve current local ids fresh for every flagged member
  (`himalaya envelope list -a <acct>-local -f <folder> -s 500 -o json` —
  the default page is 10, silently truncated; matched by
  `from_addr`+`date_utc`), `flag remove ... flagged` on all, then
  `flag add ... flagged` on at most one (the latest).
- **Move / resolve / trash** (approved): `$BATCH run` (or `$ACT` one-off)
  applied to every non-system, non-trash/junk member.
- **Reply** (approved; IMAP account — the one non-local operation):
  1. `References` **from the local index**: `$THR show` → every member's
     `message_id` in date order up to and including the parent.
  2. Resolve the parent's **IMAP-side** id (separate id-space):
     `himalaya envelope list -a <acct> -f <folder> -s 500 -o json`,
     matched by `from_addr`/`date_utc`/`subject`.
  3. `himalaya template reply -a <acct> -f <folder> <id>` — prefills
     everything except `References:`; add it from step 1.
  4. Write the body (quote below, reply above, the user's voice); save
     the full template to a temp file.
  5. Show the complete draft. Three exits:
     - **Send** (explicit approval of that exact draft):
       `himalaya template send -a <acct> "$(cat <tmpfile>)"`.
     - **Save for later**: write the full RFC822 (headers incl.
       In-Reply-To/References + body, LF endings) to drafts LOCALLY —
       **on stdin, never as an argument** (argument form silently saves
       EMPTY; without stdin it hangs):
       `himalaya message save -a <acct>-local -f "<tiers.drafts>" < <tmpfile>`
       Resolve the draft's local id FRESH, then
       `himalaya flag add -a <acct>-local -f "<tiers.drafts>" <id> draft seen`.
       The batch-boundary sync (must include drafts) pushes it as a
       standard IMAP draft the phone can open, edit, send. Log
       `final_action=draft` (decisive); thread keeps its flag. Sent from
       the phone later → the Sent detection logs the manual reply; the
       draft's disappearance is expected report noise, no event.
     - **Discard**: delete the temp file; log per the thread's actual
       disposition.

## Reconciliation (new arrival on a filed thread)

Detected per Triage's reconciliation check. Three outcomes, always a
fresh, explicit decision — arrival triggers attention, never state:

- **Stays resolved** (default expectation): move just the new message to
  join its siblings.
- **Reopens:** move the whole thread (non-trash/junk) to the active tier.
- **Refiles:** move the whole thread to the new resolved location.

Log per the normal action vocabulary; note `"reconciliation:
stays-resolved"` / `"reopens"` / `"refiles"` in the event's `notes`.

## Logging (every decision, at act time)

`$BATCH run` logs move/remove events itself. For everything else:

```sh
# One event:
$HIST log-event --session "$SESSION" \
  --account gmail --sender "who@example.com" --subject "..." \
  --thread-state holding --category newsletter --inactive-days 4 \
  --proposed trash --final trash --decided-by approved \
  --from-folder INBOX --to-folder "[Gmail]/Trash" \
  [--message-id "..."] [--thread-root-id "..."] [--rule-id N] \
  [--notes "..."] [--unsubscribed]

# Batch insert (all-or-nothing). WARNING: a quoted heredoc (<<'JSON') does
# NOT expand $SESSION — write the real timestamp into the JSON:
$HIST log-events <<'JSON'
[{"session_started_at": "2026-07-09T18:15:00Z", "account": "gmail",
  "sender_addr": "a@example.com", "category": "spam-noise",
  "proposed_action": "trash", "final_action": "trash",
  "decided_by": "approved", "from_folder": "INBOX",
  "to_folder": "[Gmail]/Trash"}]
JSON

# Notes, the moment something is noteworthy:
$HIST log-note --session "$SESSION" "resume state / watch item"
```

`decided_by`: `auto` (autonomous — flags only), `approved` (as-is),
`override` (user changed the outcome — always log the original
`proposed_action`; **the richest learning signal** — "leave it" after a
proposal is `final_action=skip, decided_by=override`), `manual` (change
report, step 4).

**Action vocabulary:** `flag | reply | archive | trash | skip | activate
| draft`. `activate` = moved to the active tier — triaged, stays open; a
real decision, decisive in `sender_stats`, which is exactly why it is NOT
`skip`. `draft` = composed but saved to Drafts — decisive likewise.

Skip rules: a proposed-then-deferred thread logs `skip` **once per thread
per session** (staying open is `activate`, not skip). `waiting` threads
are never skip-eligible. Threads never reviewed are not logged — resume
state goes in a session note.

**Interaction patterns are DB facts, not prose.** When the user states a
per-sender disposition ("always surface these immediately," "summarize
then toss," "scan and trash"), promote it **the moment it's stated**:
- who-someone-is facts → `$HIST add-contact <addr> [--relation "plumber"]
  [--vip 1] [--notes "..."]` (upsert; asserted facts only);
- repeatable dispositions → `$HIST add-rule --predicate "sender = ..."
  --action <x> --origin user` — actions may be interaction patterns
  (`summarize-then-trash`, `scan-then-trash`, `surface-immediately`), not
  just the event enum. Claude-proposed rules (cite `candidates`
  analytics) use `--origin claude-proposed`, added only after approval;
- retire with `$HIST set-rule <id> --active 0`.
A pattern recorded once frames every future session via `$HIST context`;
the same pattern in prose notes scrolls away, and sender stats alone
can't distinguish "noise I clear" from "content I consume then clear."

Session notes carry resume state, watch items, and
`unrecognized_folders`/`parse_errors` audit debt. Write them the moment
the noteworthy thing happens.

## On demand

- **Analytics** (user asks for stats/trends, or you need rule-proposal
  evidence): reference §Analytics — `uv run analytics.py <subcommand>`.
- **PARA review** (weekly-ish, user-invoked): graduation of ongoing
  project folders to cold storage — reference §PARA.
- **Quirks, JSON shapes, bootstrap, incident history**: reference.md.
