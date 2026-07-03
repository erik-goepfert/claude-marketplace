# Email Maintenance Skill — Implementation Plan

**Companion to:** `email-skill-design.md` (all § references point there)
**Accounts:** iCloud + Gmail, both from day one
**Starting state:** himalaya installed and configured for direct IMAP access
to both accounts. No Maildir, no databases, no skill file.

The plan is phased so triage starts **now** and infrastructure lands
underneath a working practice. Two design properties make this safe:

1. **`history.db` is self-contained by design** (§9) — natural keys, no
   foreign keys into the index — so it can start accumulating from the first
   session, before the index exists. Nothing recorded during the MVP is thrown
   away.
2. **The flag/state model runs on plain IMAP flags** (§10), which himalaya
   already speaks against both accounts today.

---

## Phase 0 — Prerequisites (hours, do first)

Small tasks that protect everything after them.

- [x] **0.1 Verification tests §13.1 and §13.2, both accounts.**
      Sent-copy behavior and reply threading headers. Not needed by the MVP
      mechanically, but mail sent *during* the MVP must carry
      `In-Reply-To`/`References` and land exactly one Sent copy, or it won't
      thread correctly once the index exists. Record the outcomes in the doc.
      **Outcomes (2026-07-02):**
      - *Sent-copy behavior:* iCloud's SMTP does **not** auto-save to Sent —
        himalaya's own save-copy is the only copy (verified: one entry).
        Gmail's SMTP **does** auto-save to Sent, and with himalaya's
        `message.send.save-copy` left at its default, every send produced
        **two** Sent entries (one Gmail-assigned Message-ID, one with none).
        Fixed by setting `message.send.save-copy = false` on the `gmail`
        account in `~/.config/himalaya/config.toml` — iCloud keeps the
        default (`save-copy` true).
      - *Reply threading headers:* checked two pre-existing sent replies
        (not new test sends) — iCloud msg 217 and Gmail msg 3368. Both carry
        correct `In-Reply-To` and `References` (Gmail's chain has all 5
        ancestors). himalaya's reply composition needs no fix on either
        account.
      - Test messages sent to verify Sent-copy behavior were moved to Trash
        on both accounts after the check.
- [x] **0.2 `init_history_db.py`** — creates `history.db` from the §9 schema
      (events, session_notes, contacts, rules, sender_stats view). Trivial
      script; the schema is written.
      **Outcome:** `scripts/init_history_db.py` added (idempotent,
      stdlib-only). Run: creates `history.db` at `$EMAIL_TRIAGE_DATA_DIR`
      (default `~/.local/share/email-triage/`). Verified against both
      accounts' data dir; all 4 tables + `sender_stats` view present.
- [x] **0.3 Per-account folder map.** Archive/Trash/Sent names differ per
      account (`Archive` vs Gmail's explicit `Archive` label, `Sent` vs
      `[Gmail]/Sent Mail`). Create the Gmail `Archive` label now. This map is
      config the skill reads, not knowledge it improvises — and the skill
      carries a first-run bootstrap: if the config is missing, run
      `folder list` per account and confirm targets with the user before
      writing it.
      **Outcome:** Gmail `Archive` label created via
      `himalaya folder create -a gmail Archive`. Folder map written to
      `~/.local/share/email-triage/config.yaml` (inbox/archive/trash/sent/junk
      per account; iCloud already had `Archive`/`Sent Messages`/
      `Deleted Messages`/`Junk`; Gmail uses the new `Archive` label,
      `[Gmail]/Sent Mail`, `[Gmail]/Trash`, `[Gmail]/Spam`).

**Exit:** both accounts verified, empty history DB exists, folder map written.

---

## Phase 1 — MVP: usable triage over direct IMAP (days)

Goal: run real triage sessions on both accounts while the full stack is built.
No mbsync, no Maildir, no index.

- [x] **1.0 Plugin scaffold.** Repo with `.claude-plugin/plugin.json` and
      `skills/email-triage/SKILL.md`; helper scripts and schema DDL under the
      skill directory (§7.7). Develop against the working copy with
      `claude --plugin-dir <repo>`. All state (`config.yaml`, `history.db`,
      later `index.db`) lives in `~/.local/share/email-triage/` — never in
      the repo.
      **Outcome (2026-07-02):** plugin lives at `plugins/email-triage/`
      inside this marketplace repo (so 1.5 is a path-based `marketplace.json`
      entry). `plugin.json` written; `init_history_db.py` moved from
      `scripts/` to `skills/email-triage/scripts/`. Develop with
      `claude --plugin-dir plugins/email-triage`.
- [x] **1.1 Skill file (SKILL.md).** The main writing task:
      - Session flow: stamp `session_started_at` → read recent session notes +
        active rules + sender_stats from `history.db` → `envelope list -m
        INBOX --output json` per account → triage → act → log.
      - Triage workflow per §11: classify everything from envelopes + history
        before any body read; present the category breakdown (lifecycle state
        × content category) with a proposed next step; work categories
        easiest-first; targeted body reads only (ambiguity + unsubscribe);
        surface unsubscribe links, never follow them.
      - Operating rules verbatim from §4: approval gate (drafts always shown;
        sends/moves/trash approved; flags autonomous), `\Flagged` = needs-reply,
        trash = move to Trash folder.
      - Per-account folder map from 0.3.
      - Gmail constraint: `envelope list` only, never `envelope search` (§5).
      **Outcome (2026-07-02):** written at
      `plugins/email-triage/skills/email-triage/SKILL.md`. Verified against
      himalaya v1.2.0 before writing: envelope JSON shape (bare array, string
      ids, `{name, addr}` from/to, string-array flags) is documented in the
      skill; the non-interactive reply path is `template reply` → edit →
      `template send`. **Finding:** `template reply` prefills `In-Reply-To`
      but *omits* `References` — the skill constructs it from the parent's
      headers (§13.2's fallback). Correct folder flag is `-f` (the plan's
      `-m` was wrong).
- [x] **1.2 Event + note logging helpers.** Small Python CLI (or documented
      sqlite3 one-liners) the skill calls to insert events and session notes.
      Full `proposed/final/decided_by` semantics and skip rules from §9 apply
      from session one.
      **Outcome (2026-07-02):** `scripts/history.py` (stdlib-only) with
      `log-event` / `log-note` / `context` subcommands; enums enforced at the
      CLI, sender lowercased on insert. Tested against a throwaway DB:
      inserts land, `sender_stats` computes skip/override semantics
      correctly, `context` emits clean JSON. SKILL.md's logging section now
      shows the exact invocations.
      **Amendment (2026-07-02, mid-first-session):** added
      `events.unsubscribed` (user-confirmed, since the click is invisible to
      the skill by design — §4.4) via an additive migration in
      `init_history_db.py` (checks `PRAGMA table_info`, `ALTER TABLE ADD
      COLUMN` if missing) and a `--unsubscribed` flag on `log-event`. Applied
      to the live `history.db`, which already had 17 real events — all
      preserved, migration verified idempotent-safe.
      **Amendment (2026-07-02):** himalaya's `flag add`/`message move`/
      `message delete` all take multiple ids in one call — added a matching
      `log-events` bulk subcommand (JSON array via stdin/`--file`, shared
      validation with `log-event`, all-or-nothing insert via one
      transaction) so a batch himalaya action logs in one call too. Tested:
      valid batch inserts atomically; a batch with one invalid item aborts
      with no partial writes.
      **Amendment (2026-07-02):** made both scripts executable (`chmod +x`;
      both already had a `python3` shebang). Initially tried granting
      permission by editing the user's *global* `~/.claude/settings.json`
      — wrong instinct. The correct, self-contained mechanism is `SKILL.md`
      frontmatter's `allowed-tools` field, which pre-approves tools while
      the skill is active without touching any user settings file (confirmed
      against the current skills docs; it's the documented pattern for a
      skill that bundles and runs its own scripts, e.g. the docs' own
      example uses `allowed-tools: Bash(python3 *)` verbatim). Reverted the
      global settings.json edit and instead added `allowed-tools` to
      SKILL.md's frontmatter — the plugin now travels with its own
      permissions.
      **Amendment (2026-07-02):** `Bash(python3 *)` was broader than needed.
      Confirmed against the current permissions docs that Bash wildcards
      match at any position (not just trailing), so tightened to
      `allowed-tools: Bash(himalaya *) Bash(*history.py*)
      Bash(*init_history_db.py*)` — scoped to exactly these two scripts
      (now invoked directly, no `python3` prefix, since they're executable)
      regardless of install path, rather than any python3 invocation.
      Switched the body's invocations to `${CLAUDE_SKILL_DIR}/scripts/...`
      too, so the actual command path resolves correctly whether the skill
      ends up at the personal, project, or plugin level — not just the
      permission rule.
- [x] **1.3 In-context thread grouping.** Claude groups the inbox loosely by
      normalized subject in-context during triage — no persistent threading.
      Fine at inbox scale.
      **Outcome (2026-07-02):** "Thread grouping (MVP)" section in SKILL.md —
      iterative prefix-stripping normalization plus the §10 participant-
      overlap guard (subject match alone never merges threads).
- [ ] **1.4 First real sessions.** Both accounts. Expect skill-prompt
      iteration; capture friction as session notes — they're the requirements
      list for Phase 2.
      **In progress (2026-07-02):** first live session underway. Finding:
      unprompted, Claude correctly prioritized new-device security-alert
      emails (fresh GitHub/GitLab logins on this laptop) ahead of the regular
      flow, but on its own reasoning rather than an explicit rule — not
      reliable across sessions/models. Added a "Priority scan" step to
      SKILL.md's Triage section: security/time-sensitive signals (new
      sign-in, new SSH/API key, MFA changes, suspicious charges, expiration/
      deadline language) are now always surfaced individually, before the
      category breakdown, regardless of content category.
      **Session review (2026-07-02):** first full session completed and its
      transcript reviewed against SKILL.md's contract (a subagent read the
      ~862-line JSONL transcript so the raw log didn't have to enter this
      session's context). This session ran concurrently with the SKILL.md/
      history.py edits above, so its priority-scan-ordering and outdated-
      script findings just predate those fixes landing — not new bugs. Three
      genuine misses fixed in SKILL.md:
      - **Waiting-state threads got a skip logged** despite the explicit
        rule against it — added a stronger note right at the lifecycle-state
        table (where the gap actually originates: lifecycle state and
        content category are independent axes, so a `waiting` thread can
        still surface in an `actionable` batch) plus reinforced the
        cross-referenced Skip rules bullet under Logging.
      - **A trash batch actually executed as an archive move** (folder
        mix-up, self-caught mid-session) — approval asks must now name the
        resolved destination folder ("archive these 12 to `Archive`?"),
        surfacing a mismatch before execution instead of after.
      - **"Keep the legacy account" was executed as an archive** — added
        explicit guidance that "keep"/"keep it" alone means no action, not
        archive; only explicit relocation language triggers a move.
      Noted but not fixed: repeated "what's remaining" asks with no running
      tally (friction, not a bug) — a real Phase 2 candidate once the index
      exists, since a live remaining-count is exactly what thread-state
      rollup queries give for free.
- [ ] **1.5 Publish.** Add the plugin entry to the personal marketplace repo;
      install on a second machine via `/plugin marketplace add` +
      `/plugin install` as the portability proof — the dependency check and
      config bootstrap must carry the entire setup. Decide the multi-machine
      history strategy (§15.7) *before* running triage from the second
      machine.
      **In progress (2026-07-02):** the repo had no `marketplace.json` at
      all yet — the design doc assumed one already existed. Created
      `.claude-plugin/marketplace.json` (marketplace name `erik-plugins`,
      one entry pointing at `./plugins/email-triage`). Verified the full
      local mechanics work: `claude plugin marketplace add
      /Users/erik/Source/claude-marketplace` → `claude plugin install
      email-triage@erik-plugins` → `claude plugin list` / `plugin details`
      all succeeded (1 skill, ~82 always-on tokens). Confirmed the
      installed copy is a real snapshotted directory, not a symlink — it
      will not track live edits to the repo.
      **Deferred, deliberately:** uninstalled the plugin again and left
      development on `--plugin-dir` instead, per §7.7's own stated workflow
      ("development happens against the working copy... publication to the
      marketplace when stable") — we're still actively editing SKILL.md, so
      a snapshotted install would just drift. The `erik-plugins` marketplace
      registration itself (directory source) is left in place; harmless,
      and already proved the format is correct. Real GitHub push and the
      second-machine portability proof are on hold until the plugin is more
      stable — pushing requires the user's own git credentials (this
      sandbox has neither SSH key access nor `gh` auth configured). The
      multi-machine history.db strategy (§15.7) is provisionally decided as
      **designated primary machine** (this laptop); revisit if/when a
      second machine runs real triage, not just an install test.
      **Dev workflow note (2026-07-02):** `claude plugin update` turned out
      to respect the pinned `version: 0.1.0` and silently no-op when only
      file contents change (verified empirically: edited SKILL.md, ran
      `marketplace update` + `plugin update`, cache didn't move, CLI
      reported "already at the latest version"). A plain **uninstall +
      reinstall** does refresh the cache from source regardless of the
      version string (also verified). So the working split going forward:
      `--plugin-dir` for active back-and-forth editing sessions (this kind
      of session — no cache, always live); a real local marketplace install
      for standalone real-triage sessions, refreshed with
      `claude plugin uninstall email-triage@erik-plugins &&
      claude plugin install email-triage@erik-plugins` whenever a new batch
      of changes is ready to try, no version bump or `--plugin-dir` flag
      needed. Re-installed via the local `erik-plugins` marketplace so it's
      ready to use next session.

**Accepted MVP limitations** (all lifted by Phase 2):
- Inbox-only view; no cross-folder threading, no archived-thread history.
- `waiting` state inferred from the `\Answered` flag only; inactive-days for
  waiting threads approximate (no Sent-folder correlation).
- No external-change detection — phone actions go unrecorded as events.
- One IMAP round-trip per operation; sessions are network-bound.

**Exit:** triage sessions run end-to-end on both accounts; events and notes
accumulate across sessions; Claude visibly uses prior-session context.

---

## Phase 2 — Full stack (§16 build sequence, in order)

The local mirror and index land underneath the working MVP practice.

- [ ] **2.1 mbsync channels, both accounts.** `Sync All` / `Expunge Both` /
      `CopyArrivalDate yes`; `Patterns * !Inactive/*`; Gmail channel excludes
      `[Gmail]/All Mail` (§7.1). Initial clones (the only slow syncs).
- [ ] **2.2 Verification suite §13 in full, both accounts** — now including
      §13.3 round-trip (local flag/move/trash → server; phone-side changes →
      local) and §13.4 envelope JSON schema. **Gate everything downstream on
      this.**
- [ ] **2.3 Folder audit.** Move inactive folders under `Inactive/` (per-account
      pass; easiest in the native clients).
- [ ] **2.4 Indexer** (`index.db`): three-pass reconciliation, change-report
      output contract, synthetic Message-IDs, UTC normalization (§7.4, §8).
- [ ] **2.5 Thread assembly queries:** recursive CTE / References-root
      grouping, guarded subject fallback, both entry modes, INBOX+Sent rollup
      + inactive-days (§9–10 of the design's threading section).
- [ ] **2.6 Switch the skill's read/act layer to the Maildir backend:**
      himalaya Maildir accounts, session lockfile, resolve-before-act,
      batch-boundary syncs (§7.5, §11, §12). Measure sync latency (§15.5).
- [ ] **2.7 Skill upgrade:** thread-based triage from `index.db`; accurate
      inactive-days; manual events from the change report (external moves,
      flags, and Sent-detected replies, §9); rule proposals from sender_stats.
- [ ] **2.8 History backup.** `history.db` is the irreplaceable file — wire it
      into whatever backup covers the home directory, explicitly.

**Cutover notes**
- MVP-era events remain valid unchanged — self-containment was designed for
  exactly this.
- MVP-era sent mail threads correctly retroactively iff 0.1 passed.
- The external-change baseline starts at the first indexed session; phone
  actions before that are simply unrecorded history (§9's accepted caveat).

---

## Phase 3 — Optional, on demand

- [ ] DuckDB attached read-only over both files for trend analytics (§9).
- [ ] Richer rule predicates, only when sender-equality proves insufficient
      (§15.4).
- [ ] Skip-pattern nudges ("deferred five times — archive or long-term hold?").
