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
      **Amendment (2026-07-04):** found (via direct testing, prompted by a
      user question, not a session review) that `himalaya message read`
      marks `\Seen` even when only extracting a header — the full message
      is fetched underneath regardless of `--header` filtering. SKILL.md's
      two documented body-read paths (ambiguous-classification, unsubscribe
      extraction) were calling plain `message read`, so Claude's own
      internal peeking was silently marking things read — corrupting a
      signal that belongs to the user, not to triage. Fixed: both paths
      (plus the parent-header lookup for `References`) now use `--preview`,
      confirmed empirically to suppress `\Seen`; marking read is now a
      deliberate `flag add ... seen` only when content is actually shown to
      the user. New Operating rule 5 in SKILL.md and §4.5 in the design doc
      codify this. `template reply`/`message reply` have no preview
      equivalent and were left as-is — accepted, since the draft they
      produce quotes the original back to the user before anything sends.
      **Amendment (2026-07-04):** with `\Seen` now trustworthy (Claude's own
      reads can no longer dirty it), the priority scan (§11) is scoped to
      unseen messages only — an already-seen thread has had its one chance
      to interrupt and re-surfacing it is just noise. Checked from envelope
      flags already in hand, no extra read.
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

- [x] **2.1 mbsync channels, both accounts.** `Sync Full` / `Expunge Both` /
      `CopyArrivalDate yes`; `Patterns INBOX *` — everything syncs,
      **including `[Gmail]/All Mail`** (§7.1). Initial clones (the only slow
      syncs).
      **Outcome (2026-07-09):** done — both initial clones completed cleanly
      (exit 0, zero log errors). iCloud: 18 folders, 2,030 messages, 396 MB.
      Gmail: 30 mailboxes, 16,586 messages, 3.3 GB; `[Gmail]/All Mail` came
      down complete (10,667 = the server-side count), Starred/Important
      correctly absent. Config at `~/.config/isync/mbsyncrc` (chmod 600,
      `PassCmd` → keychain, no plaintext secrets); draft + runbook archived
      in the session scratchpad. Build findings, all folded into §7.1/§13
      and the taxonomy doc: (a) no `Sync All` keyword exists — it's
      `Sync Full`; (b) `INBOX` must be listed explicitly in `Patterns`
      (wildcards don't match it); (c) mbsync does NOT auto-discover
      `~/.config/isync/mbsyncrc` — every invocation needs `-c` (the 2.6
      skill wiring must carry this); (d) `AuthMechs PLAIN` is required —
      cyrus-SASL otherwise picks an uncompletable mechanism (XOAUTH2) and
      fails with "unable to find a callback"; (e) `[Gmail]/Starred` +
      `[Gmail]/Important` excluded as zero-unique-message virtual views
      (user-approved). `config.yaml` updated: `maildir_path` set, stale
      All-Mail exclusion replaced. Ran *before* the 2.3 folder audit —
      acceptable per the ordering-preference note; the audit's folder moves
      will propagate as renames/moves on later syncs.
      **Revised (2026-07-09, taxonomy):** the earlier `!Inactive/*` and
      All-Mail exclusions are both dropped. The taxonomy
      (`email-folder-taxonomy.md`) dissolves legacy folders instead of
      parking them, and All Mail is the only place natively-archived Gmail
      mail exists — Gmail's `Archive` tier is virtual, read from the native
      no-label state, so All Mail must mirror. Duplicate label/All-Mail
      copies are deduped at index time (2.4).
      **Pre-clone sizing (2026-07-04):** Google reports 3 GB total for the
      Gmail account — disk space is a non-issue even at the full ~6 GB
      worst case now that everything mirrors. `MaxSize` stays unused.
      Attachments come down automatically as part of each message file
      (Maildir = complete raw message; extract later via `himalaya
      attachment download` — local, no network). Prefer running the 2.3
      folder audit *before* the initial clone so the first sync doesn't
      pull folders about to be reorganized — ordering preference, not a
      requirement.
- [x] **2.2 Verification suite §13 in full, both accounts** — now including
      §13.3 round-trip (local flag/move/trash → server; phone-side changes →
      local), §13.4 envelope JSON schema, §13.5 Gmail label-removal
      round-trip (delete from a label's local folder → sync → label removed
      server-side, message stays in All Mail; the virtual-Archive model
      rests on this, and it depends on Gmail's IMAP auto-expunge settings —
      pin them), and §13.6 nested folder/label creation on both accounts
      (`Areas/<area>/<project>` needs two levels). **Gate everything
      downstream on this.**
      **Outcome (2026-07-09): ALL PASS.** §13.1/§13.2 stand from Phase 0;
      §13.4's IMAP shape stands from Phase 1 (Maildir-backend variant
      re-verifies at 2.6 when those accounts exist). Run today:
      - **§13.3 (iCloud + gmail): PASS**, all directions — local flag/move/
        trash each propagated correctly (move = delete+re-append per §5,
        flags survive), external server-side changes pulled correctly.
      - **§13.5 (gmail): PASS — the virtual-Archive gate.** Local delete
        from a label's folder synced as "remove label": label emptied
        server-side, message survived in All Mail (flag intact), not
        trashed. This account's expunge-from-last-folder behavior is
        confirmed compatible; if Gmail settings are ever changed, re-run
        this test.
      - **§13.6 (both): PASS** — `himalaya folder add "A/B"` creates
        two-level nesting on both accounts, slash-separated; parents are
        not auto-created as separately listable folders (create them
        explicitly when both levels must be visible).
      **Act-layer findings for 2.6** (design §5/§12 confirmed empirically):
      strip the `,U=<uid>` filename token when relocating a Maildir file
      across folders; resolve-before-act is mandatory — a sync re-stamps
      UIDs, and a pre-sync filename reference broke a move in this very
      test run. **Latency findings for §15.5:** iCloud incrementals are
      seconds; gmail syncs pay ~60–80 s of per-session overhead regardless
      of scope (a single 25-message box took 81 s; full 30-box channel
      ~5–8 min) — 2.6 must batch actions per sync and use box-scoped
      invocations (`mbsync 'gmail:INBOX,...'`, verified working), not
      per-action full-channel syncs. Test artifacts fully cleaned up
      (messages trashed, `ZZZ-Skilltest*` folders deleted server+local).
- [ ] **2.3 Folder audit & taxonomy migration.** The high-level structure is
      now fixed (`email-folder-taxonomy.md`: Inbox / Active / Areas/… /
      Archive, canonical across both accounts via per-account adapters);
      this step instantiates it against real mail, per account:
      (a) decide the concrete Areas and project subfolders with data in
      hand — analyze per-folder volumes, sender domains, and content mix
      from envelope listings or the index (the 2026-07-04 scope expansion,
      unchanged) — and create the `Active` / `Areas/…` structure (§13.6
      verifies nesting works first);
      (b) dissolve every legacy folder into the taxonomy — contents to an
      Area, a project subfolder, or Archive; delete the emptied folder.
      This includes the MVP-era explicit `Archive` label on Gmail: its
      contents become natively archived (label removed, mail remains in All
      Mail), since post-taxonomy Gmail's Archive tier is virtual;
      (c) confirm no Gmail label survives outside the taxonomy — the 2.4
      indexer treats unrecognized labels as audit leftovers to surface, not
      as Archive.
      **Superseded (2026-07-09):** the original "park inactive folders under
      `Inactive/`" approach — nothing is parked or excluded from sync
      anymore. And since All Mail now syncs (2.1), the earlier "mirror is a
      working set, not a backup" caveat is gone: the mirror is a complete
      local copy, natively-archived Gmail mail included.
- [ ] **2.4 Indexer** (`index.db`): three-pass reconciliation, change-report
      output contract, synthetic Message-IDs, UTC normalization (§7.4, §8).
      **Added scope (2026-07-09, taxonomy):** canonical-tier derivation —
      dedup Gmail All-Mail duplicates by Message-ID (a message's tier comes
      from its non-All-Mail folder when one exists), classify All-Mail-only
      messages as the virtual `Archive` tier (Sent/Trash/Spam excluded from
      the inference), and surface unrecognized labels rather than letting
      them fall through to Archive.
- [ ] **2.5 Thread assembly queries:** recursive CTE / References-root
      grouping, guarded subject fallback, both entry modes, inactive-days
      (§9–10 of the design's threading section).
      **Revised (2026-07-09, taxonomy):** thread state is
      `OR(\Flagged over the thread)`, not the latest message's flag —
      decoupled from recency by design. `waiting` vs. `holding` is derived
      via Sent correlation on unflagged Active threads; the
      needs-reply/holding/waiting event vocabulary is unchanged.
- [ ] **2.6 Switch the skill's read/act layer to the Maildir backend:**
      himalaya Maildir accounts, session lockfile, resolve-before-act,
      batch-boundary syncs (§7.5, §11, §12). Measure sync latency (§15.5).
      **Added scope (2026-07-09, taxonomy):** the two thread-wide write
      paths — flag writes are always clear-then-set across the whole thread,
      and tier moves always move the thread as a unit (system folders stay
      put; threads never move *to* Inbox). On Gmail, moving to Archive =
      removing tier labels (mechanics per the §13.5 verification).
- [ ] **2.7 Skill upgrade:** thread-based triage from `index.db`; accurate
      inactive-days; manual events from the change report (external moves,
      flags, and Sent-detected replies, §9); rule proposals from sender_stats.
      **Added scope (2026-07-09, taxonomy):** cut SKILL.md over to the
      Active-tier state model (the taxonomy doc's Migration section lists
      exactly what replaces what); reconciliation flow for arrivals on
      filed threads (stays-resolved → join siblings / reopens → whole
      thread to Active / refiles → whole thread to new location); and the
      PARA review phase — weekly/on-demand graduation proposals for
      `Areas/<area>/<project>` subfolders (approval-gated, inactivity as
      candidate signal only, no event logged).
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
      **Decision (2026-07-04):** the learning loop itself (context read,
      sender_stats, rule proposals) is core and SQLite-backed — this item is
      only about DuckDB as a richer query engine. When analytics deepen in
      the skill, they land as `history.py` subcommands (e.g. per-domain
      stats, trends), not as raw SQL written in-session; and if a helper
      does adopt DuckDB, the attach quirks documented in §9 (aggregate
      views unreadable via the sqlite scanner; boolean predicates need
      `::INT` casts) get encoded in that helper once, never in SKILL.md.
- [ ] Richer rule predicates, only when sender-equality proves insufficient
      (§15.4).
- [ ] Skip-pattern nudges ("deferred five times — archive or long-term hold?").
