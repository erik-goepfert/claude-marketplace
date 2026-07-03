---
name: email-triage
description: Triage email across iCloud and Gmail accounts — thread-centric review of the inbox with flag/reply/archive/trash decisions, approval gates, and a persistent history DB that makes each session smarter than the last. Use when the user asks to triage, clean up, or process their email/inbox.
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
2. If `history.db` is missing: `python3 scripts/init_history_db.py`.
3. If `config.yaml` is missing: run `himalaya folder list -a <account>` for
   each account in `himalaya account list`, propose inbox/archive/trash/sent/
   junk targets, **confirm them with the user**, then write `config.yaml`.

## Operating rules (non-negotiable)

1. **Approval gate.** Never send mail and never move or delete mail without
   the user approving that specific action in this session:
   - *Send / reply:* always show the full draft; nothing is sent without
     explicit approval of that exact draft.
   - *Trash / archive:* approval required, but batchable ("archive these 12?").
   - *Flagging:* low-risk and reversible — you may flag autonomously during
     triage (log it as `decided_by=auto`).
2. **`\Flagged` means "needs my reply"** — reserved system-wide. The flag in
   Apple Mail and the star in Gmail are the user queueing a thread for you.
   Never use the flag for anything else.
3. **Trash means the Trash folder.** Delete = `message move` to the account's
   trash folder from `config.yaml`. Never expunge, never `flag add deleted`.
4. **Unsubscribe links are surfaced, never followed.** Extract and present
   them; never fetch, click, or otherwise interact with them.

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
- **Content category:** `spam-noise` | `marketing` | `newsletter` |
  `passive-update` (receipts, notifications, alerts) | `ambiguous` |
  `actionable`.

Then:

1. **Present the full breakdown first** — counts per category, lifecycle
   summary, and a proposed next step. Example: "147 threads: 12 spam,
   31 marketing, 14 newsletters, 58 passive updates, 9 ambiguous,
   9 actionable. Clear spam first?"
2. **Work easiest-first**, noise before attention: spam → marketing →
   newsletters → passive-updates → ambiguous → actionable. Show each category
   as a grouped list with a proposed bulk action; the user can pull
   individual threads out before approving. **Actionable threads are always
   presented individually.**
3. **Targeted body reads only**, for exactly two reasons: resolving an
   `ambiguous` classification, or extracting unsubscribe links from
   marketing/newsletters — category by category as each is worked, never
   upfront. Read with:
   `himalaya message read -a <acct> -f INBOX <id>`
4. **Unsubscribe extraction:** prefer the `List-Unsubscribe` header
   (`himalaya message read --header list-unsubscribe ...`) over body-scraped
   links. Present the link; the user acts on it themselves (rule 4).
5. Apply active rules from `history.db` when they match — still within the
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

- **Flag / unflag** (autonomous allowed):
  `himalaya flag add -a <acct> -f INBOX <id> flagged`
  `himalaya flag remove -a <acct> -f INBOX <id> flagged`
- **Archive** (approved): `himalaya message move -a <acct> -f INBOX <archive-folder> <id>...`
- **Trash** (approved): `himalaya message move -a <acct> -f INBOX <trash-folder> <id>...`
- **Reply** (approved, draft always shown):
  1. `himalaya template reply -a <acct> -f INBOX <id>` — generates the
     template with `From`/`To`/`Subject`/`In-Reply-To` prefilled.
  2. **Add the `References` header yourself** — the template omits it. Fetch
     the parent's headers
     (`himalaya message read -a <acct> -f INBOX --header references --header message-id <id>`)
     and set `References:` = parent's References + parent's Message-ID.
  3. Write the body (quote below, reply above, matching the user's voice —
     confirm tone on first sessions). Save the full template to a temp file.
  4. Show the complete draft to the user. Only after explicit approval:
     `himalaya template send -a <acct> "$(cat <tmpfile>)"`.
  Sending sets `\Answered` on the original automatically.

## Logging (every decision, at act time)

Use `scripts/history.py` (stdlib-only; `--data-dir` defaults to the data
directory):

```sh
# Session-start context (recent notes, active rules, sender stats, contacts):
python3 scripts/history.py context [--senders addr1,addr2,...]

# One event per executed decision, at act time:
python3 scripts/history.py log-event --session "$SESSION" \
  --account gmail --sender "who@example.com" --subject "..." \
  --thread-state holding --category newsletter --inactive-days 4 \
  --proposed trash --final trash --decided-by approved \
  --from-folder INBOX --to-folder "[Gmail]/Trash" \
  [--message-id "..."] [--thread-root-id "..."] [--rule-id N] [--notes "..."]

# Notes, the moment something is noteworthy:
python3 scripts/history.py log-note --session "$SESSION" "resume state / context / watch item"
```

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
  don't log skips for them.
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
