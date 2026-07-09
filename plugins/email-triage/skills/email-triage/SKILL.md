---
name: email-triage
description: Triage email across iCloud and Gmail accounts — thread-centric review of the inbox with flag/reply/archive/trash decisions, approval gates, and a persistent history DB that makes each session smarter than the last. Use when the user asks to triage, clean up, or process their email/inbox.
allowed-tools: Bash(himalaya *) Bash(*history.py*) Bash(*init_history_db.py*)
---

# Email Triage

You are running an email triage session. You are the interface — there is no
other UI. Work thread-by-thread, propose actions, and let the user decide.
Design rationale lives in the repo's `email-skill-design.md`; this file is
operational.

## State and first-run bootstrap

All mutable state lives in the data directory — default
`~/.local/share/email-triage/`, overridable via `$EMAIL_TRIAGE_DATA_DIR`.
Never write state into the plugin directory.

- `config.yaml` — accounts and per-account folder map. **Read it at session
  start; never improvise folder names.**
- `history.db` — decision log, session notes, contacts, rules. Irreplaceable.

Helper scripts live in `scripts/` next to this file (when installed as a
plugin, under `$CLAUDE_PLUGIN_ROOT/skills/email-triage/scripts/`).

**Bootstrap (only if something is missing):**
1. Verify `himalaya` is installed (`himalaya --version`); if not, walk the
   user through installing it (`brew install himalaya`) and configuring their
   accounts before continuing.
2. If `history.db` is missing: `${CLAUDE_SKILL_DIR}/scripts/init_history_db.py`.
3. If `config.yaml` is missing: run `himalaya folder list -a <account>` for
   each account in `himalaya account list`, propose inbox/archive/trash/sent/
   junk targets, **confirm them with the user**, then write `config.yaml`.

## Operating rules (non-negotiable)

1. **Approval gate.** Never send mail and never move or delete mail without
   the user approving that specific action in this session:
   - *Send / reply:* always show the full draft; nothing is sent without
     explicit approval of that exact draft.
   - *Trash / archive:* approval required, but batchable — and **name the
     resolved destination folder in the ask** ("archive these 12 to
     `Archive`?" not just "archive these 12?"). A folder mix-up between an
     archive target and a trash target is an easy silent slip; saying the
     folder out loud surfaces it before execution, not after.
   - *Flagging:* low-risk and reversible — you may flag autonomously during
     triage (log it as `decided_by=auto`).
   - *Ambiguous phrasing:* "keep" or "keep it" alone means **leave it where
     it is, no action** — not archive. Only archive/move on explicit
     relocation language ("archive," "file it away," "move it," "get it out
     of the inbox"). When genuinely unclear, ask rather than assume.
2. **`\Flagged` means "needs my reply"** — reserved system-wide. The flag in
   Apple Mail and the star in Gmail are the user queueing a thread for you.
   Never use the flag for anything else.
3. **Trash means the Trash folder.** Delete = `message move` to the account's
   trash folder from `config.yaml`. Never expunge, never `flag add deleted`.
4. **Unsubscribe links are surfaced, never followed.** Extract and present
   them; never fetch, click, or otherwise interact with them.
5. **`\Seen` is the user's signal, not triage state — your own reads must
   never move it.** A message counts as read only once the user has been
   shown its content or a representative summary of it; Claude privately
   extracting a header or a body to classify or resolve ambiguity is not
   that. Concretely:
   - Any read done for your own purposes (unsubscribe extraction, resolving
     `ambiguous`, pulling a parent's headers for `References`) uses
     `himalaya message read --preview` — confirmed this suppresses `\Seen`
     (plain `message read`, even with `--header` to filter output, still
     fetches and marks the full message; verified empirically).
   - Only mark a message read by explicitly setting it —
     `himalaya flag add -a <acct> -f INBOX <id> seen` — at the moment you
     actually present its content or summary to the user. Don't rely on a
     read command's side effect for this; make it a deliberate action.
   - Exception: `himalaya template reply` / `message reply` have no preview
     equivalent and do mark `\Seen`. That's fine here — a reply draft
     quotes the original back to the user before anything sends, so the
     "shown a representative summary" condition is already met by the time
     it happens.

## Session flow

1. **Stamp the session.** `session_started_at` = current UTC time, ISO 8601
   (e.g. `2026-07-02T22:15:00Z`), taken once. Every event and note this
   session carries it verbatim — it is the session's identity.
2. **Read history context** from `history.db`:
   - session notes from the last ~5 sessions (resume state, watch items);
   - active rules (`SELECT * FROM rules WHERE active = 1`);
   - `sender_stats` for context once you know who's in the inbox.
3. **List both inboxes:**
   ```sh
   himalaya envelope list -a icloud -f INBOX -s 500 -o json
   himalaya envelope list -a gmail  -f INBOX -s 500 -o json
   ```
   If a page comes back full, fetch subsequent pages (`-p 2`, …).
4. **Group messages into threads** in-context (see Thread grouping).
5. **Classify every thread before reading any body** (see Triage).
6. **Present the breakdown**, then work through categories with the user.
7. **Act and log as you go** — every executed decision becomes an event at
   act time; notes are written the moment something is noteworthy, never
   saved up for session close (the close may never come).

## Triage (classify-first discipline)

Classify from envelope data (sender, subject, date, flags) plus history
(sender_stats, contacts, rules) — **no body reads during classification**.
Two axes per thread:

- **Lifecycle state** (MVP approximation from flags):
  | Latest message in thread | State |
  |---|---|
  | has `\Flagged` (`Flagged` in JSON) | `needs-reply` |
  | has `\Answered` (`Answered`) | `waiting` (you replied; approximate in MVP) |
  | otherwise | `holding` |
  Inactive-days ≈ days since the latest message's date.
  **A `waiting` thread gets no proposal and no log entry, ever — not even a
  skip.** Lifecycle state and content category are independent axes, so a
  `waiting` thread can still land in `actionable` or any other category and
  surface in that category's list. When it does, leave it out of the
  proposal/approval for that batch entirely; there's nothing to decide about
  a thread that's waiting on someone else, regardless of category (see Skip
  rules under Logging).
- **Content category:** `spam-noise` | `marketing` | `newsletter` |
  `passive-update` (receipts, notifications, alerts) | `ambiguous` |
  `actionable`.

**Priority scan — before the breakdown, before anything else.** Security and
time-sensitive signals are surfaced first, regardless of what content
category they'd otherwise land in. This is not left to in-the-moment
judgment — always check for it, every session.

**Scope: unseen only.** A thread enters this scan only if its latest
message lacks `\Seen` — check `flags` in the envelope JSON already fetched
at session start, no extra read needed. A message you've already seen (in
this client or another) has already had its one chance to interrupt you;
don't re-surface it here. This only works because rule 5 keeps `\Seen`
uncorrupted — a signal Claude's own reads never touch is one this scan can
actually trust. An already-seen security-shaped thread still gets triaged
normally in the regular flow below, just not as a priority interrupt.

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
here's the link to review/revoke" (surfaced, never followed, per rule 4);
treat as `actionable` for logging purposes (`category=actionable`, and note
in the free-text `notes` field that it was a priority-scan hit). Only once
these are resolved does the regular classify-first flow below proceed.

Then, for everything else:

1. **Present the full breakdown** — counts per category, lifecycle summary,
   and a proposed next step. Example: "147 threads: 12 spam, 31 marketing,
   14 newsletters, 58 passive updates, 9 ambiguous, 9 actionable. Clear spam
   first?"
2. **Work easiest-first**, noise before attention: spam → marketing →
   newsletters → passive-updates → ambiguous → actionable. Show each category
   as a grouped list with a proposed bulk action; the user can pull
   individual threads out before approving. **Actionable threads are always
   presented individually.**
3. **Targeted body reads only**, for exactly two reasons: resolving an
   `ambiguous` classification, or extracting unsubscribe links from
   marketing/newsletters — category by category as each is worked, never
   upfront. Read with `--preview` (rule 5 — this is you looking, not the
   user):
   `himalaya message read --preview -a <acct> -f INBOX <id>`
   If resolving the ambiguity means actually showing the user the content
   or a summary of it, mark it read at that point:
   `himalaya flag add -a <acct> -f INBOX <id> seen`
4. **Unsubscribe extraction:** prefer the `List-Unsubscribe` header
   (`himalaya message read --preview --header list-unsubscribe ...`) over
   body-scraped links. Present the link; the user acts on it themselves
   (rule 4) — extracting it is never itself a reason to mark `\Seen`.
   Clicking it is invisible to you by design — **ask** ("unsubscribe too, or
   just clear this one?") and log the answer via `--unsubscribed` on that
   event (see Logging). Don't ask on every single marketing/newsletter
   thread if the user has clearly indicated a batch answer ("unsubscribe
   from all of these") — apply it to each event in the batch.
5. **Noise escalation — when unsubscribe isn't possible.** The standing
   policy is a three-step ladder: (1) unsubscribe if possible; (2) when it
   isn't (or the user declines but still wants the mail gone), **propose a
   server-side filter**; (3) last resort, a triage rule or continued manual
   triage. A filter proposal means drafting the exact config for the user to
   install in the provider's web UI — filters aren't reachable over IMAP —
   including match expression, action, and any carve-outs for transactional
   senders (e.g. Substack newsletters are filterable but login magic links
   come from `no-reply@substack.com`, which must be excluded). **Server
   rules delete; they never file into folders** — a filing rule would create
   a second untriaged path into tier folders and hide mail from the learning
   loop. After the user installs one, record it under that account's
   `server_rules:` in `config.yaml` and log a session note — server-deleted
   mail generates no events, and the registry is what explains the silence
   to future sessions.
6. Apply active rules from `history.db` when they match — still within the
   approval gates; a rule justifies a proposal, not an autonomous action
   (flags excepted).

## Thread grouping (MVP)

No persistent threading in the MVP — the References-based index arrives in
Phase 2. Group in-context from the envelope listing:

- **Normalize subjects:** repeatedly strip leading reply/forward prefixes
  (`Re:`, `RE:`, `Fwd:`, `Fw:`, `AW:`, …), case-insensitively, and collapse
  whitespace.
- **Group by normalized subject, guarded by participants:** two messages
  join the same thread only if they also share at least one participant
  address (from/to). Unguarded subject matching would merge every unrelated
  "Invoice" in the inbox.
- When the guard splits something that might be one conversation, present
  both fragments adjacently rather than agonizing — the user sees both
  during triage anyway.
- A thread's state and inactive-days come from its **latest** message
  (see Triage).

## Acting

Resolve folder names from `config.yaml`. All ids are per-folder himalaya
envelope ids from the JSON listing.

`flag add/remove`, `message move`, and `message delete` all take multiple ids
in one call — **use this for batch actions** ("archive these 12") instead of
looping per-message; it's one himalaya invocation, and pairs with `log-events`
below for one bulk history write to match.

- **Flag / unflag** (autonomous allowed):
  `himalaya flag add -a <acct> -f INBOX <id>... flagged`
  `himalaya flag remove -a <acct> -f INBOX <id>... flagged`
- **Archive** (approved): `himalaya message move -a <acct> -f INBOX <archive-folder> <id>...`
- **Trash** (approved): `himalaya message move -a <acct> -f INBOX <trash-folder> <id>...`
- **Reply** (approved, draft always shown):
  1. `himalaya template reply -a <acct> -f INBOX <id>` — generates the
     template with `From`/`To`/`Subject`/`In-Reply-To` prefilled.
  2. **Add the `References` header yourself** — the template omits it. Fetch
     the parent's headers with `--preview` (mechanical extraction, not
     showing the user anything yet — rule 5):
     (`himalaya message read --preview -a <acct> -f INBOX --header references --header message-id <id>`)
     and set `References:` = parent's References + parent's Message-ID.
  3. Write the body (quote below, reply above, matching the user's voice —
     confirm tone on first sessions). Save the full template to a temp file.
  4. Show the complete draft to the user. Only after explicit approval:
     `himalaya template send -a <acct> "$(cat <tmpfile>)"`.
  Sending sets `\Answered` on the original automatically. Note:
  `template reply` itself (step 1) does mark `\Seen` on the original, with
  no preview equivalent — accepted per rule 5's exception, since the draft
  it produces quotes that content back to the user before anything sends.

## Logging (every decision, at act time)

Use `${CLAUDE_SKILL_DIR}/scripts/history.py` (stdlib-only, executable;
`--data-dir` defaults to the data directory). `${CLAUDE_SKILL_DIR}` resolves
correctly whether this skill is installed at the personal, project, or
plugin level:

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
  [--unsubscribed]   # only if the user confirmed they clicked the link (see Triage step 4)

# Batch of events after one bulk himalaya action (all-or-nothing insert):
$HIST log-events <<'JSON'
[
  {"session_started_at": "$SESSION", "account": "gmail", "sender_addr": "a@example.com",
   "category": "spam-noise", "proposed_action": "trash", "final_action": "trash",
   "decided_by": "approved", "from_folder": "INBOX", "to_folder": "[Gmail]/Trash"},
  {"session_started_at": "$SESSION", "account": "gmail", "sender_addr": "b@example.com",
   "category": "spam-noise", "proposed_action": "trash", "final_action": "trash",
   "decided_by": "approved", "from_folder": "INBOX", "to_folder": "[Gmail]/Trash"}
]
JSON
# Same field names as log-event's flags (snake_case, matching the events
# columns). If any item fails validation, nothing in the batch is inserted —
# check before running the himalaya bulk call, not after.

# Notes, the moment something is noteworthy:
$HIST log-note --session "$SESSION" "resume state / context / watch item"
```

Use `log-events` for any batch you executed as one bulk himalaya call (a
worked category's bulk archive/trash) — one event per message/thread in the
batch, still each with its own sender/subject/category. Individual
`log-event` calls remain right for one-at-a-time actionable/security-alert
threads and replies.

`decided_by` semantics:
- `auto` — you acted autonomously (flags only).
- `approved` — user approved your proposal as-is.
- `override` — user changed the outcome. **Overrides are the richest
  learning signal** — always log the original `proposed_action` alongside
  the `final_action`. "Leave it" after a proposal is
  `final_action=skip, decided_by=override`.
- `manual` — an action the user took outside the session (only if they tell
  you about it in the MVP; automated detection arrives in Phase 2).

Skip rules:
- A Claude-proposed skip on a thread that presented a real decision is logged
  **once per thread per session**.
- Threads in `waiting` state are **not skip-eligible** — nothing to decide;
  don't log skips for them, and don't propose anything for them in the first
  place (see the lifecycle-state note in Triage). A `waiting` thread showing
  up mixed into an `actionable` or other category's batch is not itself a
  decision point — pass over it silently.
- Threads never reviewed (session abandoned, "skip the rest") are **not
  logged as events** — that would be false data. Leave resume state in a
  session note instead.

Session notes (`session_notes` table) carry what events can't: resume state
("got through needs-reply, never touched holding"), stats context ("mass
cleanup — don't read these 300 trash events as normal rates"), watch items
("contractor reply expected this week"). Write them **the moment** the
noteworthy thing happens. At session start, read the recent ones back.

## Account quirks

- **Gmail:** `envelope search` fails (no IMAP SORT) — **only ever
  `envelope list`**. The star is `\Flagged`. Archive target is the explicit
  `Archive` label from `config.yaml` — never emulate Gmail's native archive
  (remove-from-inbox), which would strand mail in All Mail only.
  `message.send.save-copy = false` is set in the himalaya config because
  Gmail's SMTP auto-saves to Sent — don't "fix" a missing local save.
- **iCloud:** himalaya's save-copy is the only Sent copy (iCloud SMTP does
  not auto-save) — leave `save-copy` at its default there.
- Envelope JSON shape (verified against himalaya v1.2.0): bare array;
  `id` is a **string**; `flags` is an array of strings (e.g. `["Flagged"]`);
  `from`/`to` are `{name, addr}` objects; `date` is local-tz RFC-ish
  (`2026-07-02 14:12-07:00`).
