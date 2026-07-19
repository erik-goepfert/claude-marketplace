# Email Triage — reference

On-demand companion to SKILL.md (5.6 split). Load this file when you need:
account/tool quirks, exact JSON shapes, analytics, the PARA review phase,
bootstrap, or the incident history behind a spine rule. The spine's rules
are complete without this file; this is the *why* and the *exact shapes*.

## Bootstrap (from-scratch setup)

Only if something is missing. Verify `himalaya`, `mbsync`, and `python3`
are installed; walk the user through anything missing. If `history.db` is
missing: `${CLAUDE_SKILL_DIR}/scripts/init_history_db.py`. If `index.db`
is missing: `$IDX init --db "$IDXDB"` (or just run `index` — it
auto-inits). If `config.yaml` or `~/.config/isync/mbsyncrc` is missing,
this is a from-scratch setup outside a normal triage session — stop and
walk the user through the full build sequence in `implementation-plan.md`
rather than improvising it here.

himalaya accounts live in `~/.config/himalaya/config.toml`, not this
skill's config: `icloud`/`gmail` are IMAP (reply/send only),
`icloud-local`/`gmail-local` are Maildir pointed at `$MAILDIR/<account>`
(all reads and flag ops).

## Account and tool quirks (all live-verified)

- **`mbsync` requires `-c "$MBSYNCRC"` on every invocation** — not
  auto-discovered (2.1). Box-scoped syntax:
  `mbsync -c "$MBSYNCRC" 'gmail:INBOX,Areas/Taxes'`. **One account per
  invocation** — combined `icloud gmail` calls flake with
  `read: unexpected EOF` ~25% of the time (opening the second channel); a
  single retry always recovered. Gmail costs ~60–80 s per invocation
  regardless of scope; a full gmail channel walk is 5–8 min.
- **`himalaya envelope list` defaults to a 10-envelope page** on every
  backend — a listing that "looks complete" at 10 is silently truncated.
  Always pass `-s 500` (or page with `-p`). Cost a live session ~9 tool
  calls to rediscover.
- **`$ACT` accepts message-ids on stdin** (`... move ... -` with one id
  per line) — use it whenever an id contains shell-hostile characters
  (backslashes bit a live session) or for long batches.
- **Gmail:** `envelope search` fails (no IMAP SORT) — only
  `envelope list`, on both the IMAP and `-local` accounts (query-style
  search via `envelope list -a gmail -f <folder> 'subject X'` does work).
  The star is `\Flagged`. Gmail's `[Gmail]/Trash` and `[Gmail]/Spam`
  auto-purge in ~30 days — a hard ceiling, vs. a comfortable floor on
  iCloud.
- **Moves via `-local` himalaya are unusable** — verified live (2.6): the
  `,U=<uid>` filename token travels to the target folder and mbsync
  hard-errors ("UID N is beyond highest assigned UID"), having already
  synced the source-side deletion (a real stranding risk). The act layer
  (`$ACT`/`$BATCH`) strips the token and is the only move path. Flag ops
  via `-local` himalaya, by contrast, are mbsync-safe (suffix-only rename,
  `,U=` preserved in place).
- **himalaya Maildir-backend ids are unstable** — an id-mapper over the
  folder, unrelated to the index's `maildir_key` (verified: an id changed
  0→4 across one sync). Always resolve them fresh, immediately before
  use, by matching `from_addr` + `date_utc` in `envelope list` output.
- **`himalaya message save` takes the message on STDIN only** (verified
  4.5): the argument form silently saves an EMPTY message, and without
  stdin redirection the command hangs waiting for input. Always
  `... message save ... < tmpfile`.
- **A plain local `message read` stamps `S`** on the Maildir filename
  (mbsync then syncs `\Seen` to the server). `--preview` suppresses it.
  `$THR read` never touches the filename at all.
- **iCloud:** himalaya's save-copy is the only Sent copy (iCloud SMTP does
  not auto-save) — leave `save-copy` at its default on the `icloud`
  account.
- **Gmail SMTP auto-saves to Sent** — `message.send.save-copy = false` is
  set on the `gmail` account in himalaya's config; don't "fix" a missing
  local save there.

## JSON shapes

**himalaya envelope JSON** (verified against himalaya v1.2.0, both
backends): bare array; `id` is a **string**; `flags` is an array of
strings (e.g. `["Flagged"]`); `from`/`to` are `{name, addr}` objects;
`date` is local-tz RFC-ish.

**`$THR list` / `show` thread object:** `account`, `root_message_id`,
`state` (`needs-reply` | `waiting` | `holding`), `drafted` (bool),
`inactive_days`, `member_count`, `subject`, `members[]` sorted
oldest→newest — each member: `message_id`, `folder`, `folder_tier`,
`date_utc`, `from_addr`, `subject`, `flags`, `is_flagged`. Message-ids
are the index's bare form (no angle brackets).

**`$THR list --compact` thread object (5.11):** `account`,
`root_message_id`, `state`, `drafted`, `inactive_days`, `member_count`,
`subject`, `tiers_present[]` (all member tiers — untriaged + ongoing/cold
spanning = reconciliation signal), `latest` (`message_id`, `folder`,
`folder_tier`, `date_utc`, `from_addr`, `flags` — the newest
non-trash/junk member; its flags drive the priority-scan unseen check).
No `members[]`.

**`$THR residue`:** `{"residue": [{account, root_message_id, state,
drafted, subject, from_addr, date_utc, residual_count, folders}],
"threads": N, "messages": N}`.

**`$IDX index` change report:** `new` / `moved` / `deleted` /
`flag_changed` / `parse_errors` / `unrecognized_folders`. Entries carry
`account`, `folder`, `message_id`, `subject`, `from_addr`, `date_utc`;
`moved` entries have `from_folder`/`to_folder` instead of `folder`;
`flag_changed` entries add `old_flags`/`new_flags` (5.2).

**`$IDX priority-folders`:** per account `{"fast": [names],
"mbsync_fast": "<acct>:<f1>,<f2>,...", "slow_hint": "full channel"}` —
pass `mbsync_fast` verbatim as ONE mbsync argument.

**`$BATCH run` items** (stdin or `--file`), array of:
`{"account", "message_id", "from_folder"}` plus exactly one of
`"to_folder"` / `"remove": true`; optional per-item event overrides:
`category`, `thread_state`, `inactive_days`, `proposed_action`,
`decided_by`, `notes`, `unsubscribed`, `rule_id`, `thread_root_id`,
`sender_addr`, `subject`. Output: `acted.moved/removed`, `failed`,
`events_logged`, `change_report` (`external` + `self_echo_absorbed` +
`parse_errors` + `unrecognized_folders`), `sync[]` (`account`, `boxes`,
`attempts`, `ok`).

**`$SES` (session.py):** `start` → `{session_started_at, lock:
"acquired"|"held", config (raw yaml text), config_path, priority_folders
(same map as $IDX priority-folders), fullsync_marker_cleared, env
(CLAUDE_SKILL_DIR / CLAUDE_PLUGIN_ROOT as the harness set them — the
invocation-path diagnostic)}` — exit 1 and touches nothing when the lock
is held. All scripts default `--db`/`--maildir`/`--maildir-root`/
`--data-dir`/`--mbsyncrc` from `$EMAIL_TRIAGE_DATA_DIR` (2026-07-19) —
path flags are overrides, not boilerplate. `sync-background` / `end` →
`{pid, accounts, log}` (detached chain: per-account mbsync with one
retry each; marker touch / lock rmdir only if ALL succeed).
`check-fullsync` → `{fullsync_done, log_tail?}`, exit 1 until done.
`unlock` removes a stale lock (user-approved only).

**`$UNSUB links`:** `header` (`https`/`mailto`/`one_click_post`), `body`
anchors (including image-button links by alt text), and `recommended:
{automated, automated_method (one-click-post|get|null), manual, mailto}`
— `automated` is header-derived only; `manual` is the top body link.
`exec` default is the RFC 8058 one-click POST; `--url` GET requires a
HEADER-derived URL (provenance re-checked at fire time) and **refuses
body links** (manual-only, user policy 2026-07-19 — a confirmation step
usually follows, so a bare GET claims false success); `mailto:` never
executed. Stdlib urllib, no cookies/auth, capped redirects, 15 s
timeout, `--dry-run` available.

## Analytics (on demand)

`ANA="${CLAUDE_SKILL_DIR}/scripts/analytics.py"` — DuckDB over both DBs,
read-only, canned queries only (never write raw SQL in-session; the §9
attach gotchas are encoded in the script once). **Invoke via
`uv run "$ANA" <subcommand>`** — a PEP 723 block self-provisions the
optional duckdb dependency; core triage never needs it. If `uv` is
unavailable, skip analytics gracefully — `$HIST context` still carries
sender_stats.

| Subcommand | Answers |
|---|---|
| `categories` | action mix + unsubscribe rate per category |
| `domains [--min-events N] [--category X]` | per-domain trash/reply/archive/override rates |
| `trends [--weeks N]` | weekly volume + action mix + distinct senders/sessions |
| `rules` | per-rule usage + override rate (incl. unused rules) |
| `sessions [--limit N]` | per-session event counts, mix, duration spread |
| `candidates [--min-events N] [--days N]` | **rule-proposal fuel**: senders you nearly always trash that keep arriving, not yet covered by a rule — cite these numbers when proposing a rule |

## PARA review phase (on-demand / weekly, not every session)

Project graduation runs on a slower loop than per-session triage.

1. **Signal:** per `<tiers.ongoing>/<group>/<project>` subfolder —
   `$THR list --tier ongoing`, grouped by `folder`; look at days since the
   latest member's `date_utc` and whether any member is flagged. Falling
   volume + no open flags is a candidate.
2. **Propose, never auto-execute:** "`Areas/…/Fence`: 0 new messages, 0
   flagged threads in 45 days — graduate to Archive?"
3. **Naming on graduation:** year-of-closure prefix (judged from when the
   folder was last genuinely active, not stragglers), lands in its family
   folder when one fits — `Areas/W-141st-Ln` →
   `Archive/Housing/20XX-W-141st-Ln`. Create the destination first if new
   (`himalaya folder create`), then `$ACT move` every message in the
   source folder to it in one batch call.
4. **No event is logged** — folder-level, no per-sender learning signal;
   the active window is reconstructable from Maildir mtimes
   (`CopyArrivalDate yes`) if ever needed.

## Incident history (why the spine's rules exist)

- **Unsynced batches (step 6's hard invariant):** six approved trash
  batches sat local-only for 35 minutes until the user asked "have you
  pushed our changes to the server?" — a crash in that window would have
  left everything live in the mailbox.
- **\Seen churn (step 4's \Flagged-only rule):** one external
  mark-all-read produced 460 would-be `flag` events that would have
  poisoned sender_stats.
- **Premature victory (Triage's never-close-from-memory + step 7's
  residue check):** the first live session reported "all 18 handled"
  twice while a thread still sat in Inbox.
- **Ad-hoc refiltering (`$THR list` filters):** the first live session
  dumped full JSON and re-filtered it with python one-liners 27 times.
- **Heredoc `$SESSION` trap (Logging):** a quoted heredoc (`<<'JSON'`)
  does NOT expand `$SESSION`; an unexpanded literal would corrupt the
  log. Write the real timestamp into the JSON.
- **Markdown links (rule 6):** `[text](url)` and backticked or elided
  URLs are unclickable in this user's terminal; both failure modes cost
  real round-trips. Bare full URL on its own line.
- **Call-count economics (`$BATCH`):** the hand-orchestrated
  act→log→index→push unit cost 4+ tool calls per batch, 12+ cycles in one
  session — the reason batch.py exists.
- **TCC (backups):** the first live session hit macOS TCC denials writing
  `~/Documents` from this shell; history backup is a scheduled launchd
  task (plan 2.9), never a session step.
- **Permission prompts (session.py):** the first post-Phase-5 session
  opened with a prompt for the raw `cat config; mkdir lock; date -u`
  bundle — plain-shell glue falls outside allowed-tools, and the nohup
  sync chains would have prompted too. Hence the rule: every standard
  session step lives inside a whitelisted script.
