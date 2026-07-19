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
- [x] **2.3 Folder audit & taxonomy migration.**
      **Outcome (2026-07-09): executed in full, both accounts.** Process ran
      as five stages: Sonnet dossier agent profiled all 36 legacy folders
      from the local mirror (headers only); mapping proposal written to the
      data dir (`folder-audit-2026-07.md` — kept out of the repo, it's full
      of street addresses); user review resolved every disposition;
      execution via himalaya server-side moves with per-folder count
      verification; final sync + ghost cleanup. Results: iCloud 12 legacy
      folders → `Areas/{W-141st-Ln, Taxes, Receipts}` +
      `Archive/Housing/{8 year-prefixed chapters}` (1,182 messages moved,
      every count exact); gmail ~20 labels → `Areas/{Taxes, Receipts}` +
      `Archive/Career/{3}` + `Archive/Housing/{2}` + 4 flat leaves; 278
      career noise digests + 619 Substack messages trashed (537 via the
      user's new server-side delete-filter backfill); 3 tiny labels
      dissolved to native archive (All-Mail persistence verified by server
      search); `Active` created on both accounts for the 2.7 cutover; both
      bare `Archive` targets kept for the interim MVP. Design decisions
      that came out of the audit, all now in the taxonomy doc / design doc:
      optional **family folders** under Archive (Career, Housing;
      container-only; 3-level nesting verified); **year-of-closure prefix**
      on archived chapters; the **noise-handling escalation ladder**
      (unsubscribe → server-side delete-filter → triage rule) with the
      server-rules-delete-never-file constraint and a `server_rules:`
      registry in config.yaml. Notable mechanics finding: Gmail dual-labeled
      messages made the career→TWC merge self-deduplicating (37 "loose"
      messages were Message-ID-identical to existing TWC copies — verified
      against the pre-migration mirror; final 69 unique is complete).
      Migration logged as a history.db session note with triage
      implications (pre-policy sender_stats context, interim
      archive-to-catch-all gap). Deferred by choice: message-level re-file
      of iCloud's 69-message grab-bag Archive. The high-level structure is
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
- [x] **2.4 Indexer** (`index.db`): three-pass reconciliation, change-report
      output contract, synthetic Message-IDs, UTC normalization (§7.4, §8).
      **Outcome (2026-07-09):** built as `scripts/indexer.py` +
      `test_indexer.py` (Sonnet-drafted from the design docs, reviewed and
      verified live). §8 DDL verbatim plus additive `folder_tier` column;
      `message_tiers` view rolls up per-message canonical tier
      (All-Mail-only ⇒ Gmail virtual Archive; family/container dirs exempt
      from unrecognized-surfacing). Live results: full mirror (14,658
      messages) indexed in 24.8 s with zero parse errors — **§15.2
      answered: indexing performance is a non-issue**; second run fully
      idempotent; tier distribution reconciles with post-migration counts.
      Notable resolved ambiguity: orphan reconciliation matches *any*
      current row with the same Message-ID (not just this run's arrivals),
      which is what makes Gmail label-removal read as a move into the
      virtual Archive. Live external-change report validation (phone-side
      action → sync → report) deferred to the first 2.6/2.7 session, where
      it happens naturally; the classification logic is unit-tested.
      **Added scope (2026-07-09, taxonomy):** canonical-tier derivation —
      dedup Gmail All-Mail duplicates by Message-ID (a message's tier comes
      from its non-All-Mail folder when one exists), classify All-Mail-only
      messages as the virtual `Archive` tier (Sent/Trash/Spam excluded from
      the inference), and surface unrecognized labels rather than letting
      them fall through to Archive.
- [x] **2.5 Thread assembly queries:** recursive CTE / References-root
      grouping, guarded subject fallback, both entry modes, inactive-days
      (§9–10 of the design's threading section).
      **Outcome (2026-07-09):** built as `scripts/threads.py` +
      `test_threads.py` (11 cases green). Implementation notes: connected
      components via union-find in Python rather than a recursive CTE
      (same result, simpler to guard); subject fallback restricted to
      singleton (link-less) components as merge sources — prevents two
      independently-linked threads from ever merging on subject alone;
      state model is the revised taxonomy one (OR(\Flagged) over
      non-trash/junk members, anti-recency property unit-tested; trash/
      junk history-only; latest-is-sent ⇒ waiting). Live verification:
      47 threads across both inboxes; the single `waiting` thread was
      independently confirmed correct (user replied 2026-06-26; builder
      thread; inactive_days=13 exact); `show` round-trips by
      References-root. Threads remain a query-time derivation — no
      threads table, per §8.
      **Revised (2026-07-09, taxonomy):** thread state is
      `OR(\Flagged over the thread)`, not the latest message's flag —
      decoupled from recency by design. `waiting` vs. `holding` is derived
      via Sent correlation on unflagged Active threads; the
      needs-reply/holding/waiting event vocabulary is unchanged.
- [x] **2.6 Switch the skill's read/act layer to the Maildir backend:**
      himalaya Maildir accounts, session lockfile, resolve-before-act,
      batch-boundary syncs (§7.5, §11, §12). Measure sync latency (§15.5).
      **Outcome (2026-07-09): plumbing built and verified live; skill-side
      wiring (SKILL.md rewrite, session lockfile convention, batch-boundary
      orchestration) deliberately folded into 2.7 — they're SKILL.md
      concerns.** What exists and is verified:
      - `icloud-local`/`gmail-local` himalaya Maildir accounts (user
        config; requires `email` field; sending stays on the IMAP
        accounts' SMTP).
      - **Empirical verdict per operation:** himalaya-local reads are safe
        with `--preview` (verified controlled pair: preview leaves the
        filename alone; a plain read stamps `S`, which would sync `\Seen`
        — rule §4.5 applies identically on the local backend). Flag ops
        via himalaya-local are mbsync-safe (suffix-only rename). **Moves
        via himalaya-local are UNUSABLE**: the `,U=` token travels to the
        target folder and mbsync hard-errors ("UID N is beyond highest
        assigned UID"), refusing the upload while having already synced
        the source-side deletion — verified live, then repaired.
      - `scripts/act.py` is therefore the move layer: U=-stripping rename
        with resolve-before-act (index supplies folder+key, disk supplies
        the CURRENT filename), collision refusal, and a `remove`
        subcommand for the Gmail virtual-Archive write path (tier-label
        strip; guarded — refuses to delete a message's only copy).
        Verified live end-to-end: act.py move → box-scoped sync → server
        correct → indexer change report classifies exactly one "moved".
        `test_act.py` 6 cases green.
      - Indexer hardening found during this work: mbsync re-stamps `,U=`
        after uploading an act.py-written file — same message, same
        folder, new maildir key — which pass 3 misread as deleted+new
        (would have produced phantom `manual` events, §9). Fixed with
        self-excluding, snapshot-checked same-folder matching; regression
        test added (first fix attempt ate real moves — caught by the
        existing suite).
      **Added scope (2026-07-09, taxonomy):** the two thread-wide write
      paths — flag writes are always clear-then-set across the whole thread,
      and tier moves always move the thread as a unit (system folders stay
      put; threads never move *to* Inbox). On Gmail, moving to Archive =
      removing tier labels (mechanics per the §13.5 verification).
- [x] **2.7 Skill upgrade:** thread-based triage from `index.db`; accurate
      inactive-days; manual events from the change report (external moves,
      flags, and Sent-detected replies, §9); rule proposals from sender_stats.
      **Outcome (2026-07-09):** SKILL.md fully rewritten for the local
      stack (Sonnet-drafted, reviewed): 9-step session flow (lockfile via
      atomic mkdir → context → full sync → index+change-report consumption
      as manual events → threads.py assembly → priority scan/breakdown →
      batched acting with re-index + box-scoped syncs → close), Active-tier
      triage with the 3-way Inbox disposition, thread-wide clear-then-set
      flags, canonical-destinations table (incl. Gmail flat-leaf virtual
      archive via act.py remove vs. real family-chapter labels),
      reconciliation flow, PARA review phase, References-from-index reply
      simplification (no more --preview parent fetch). All five operating
      rules preserved and extended with the 2.6 verified facts.
      **Vocabulary change:** `activate` added to history.py's ACTIONS
      (moved-to-Active, stays open) — the draft's skip+to_folder convention
      would have corrupted sender_stats' decisive-action rates; smoke-
      tested. Docs aligned: taxonomy doc gains the "virtual = flat
      catch-all only; family chapters are real Gmail labels" precision;
      design §9 enum comment updated. Also fixed on the way through: the
      log-events example's quoted-heredoc $SESSION bug (an old code-review
      finding, faithfully reproduced by the drafting agent from the old
      file).
      **Validation: the first live triage session IS the test** — includes
      live external-change report validation deferred from 2.4. Interim
      notes: MVP-era `accounts.gmail.folders.archive` in config.yaml is
      dissolved at the first session's convenience; rule proposals from
      sender_stats remain prose-driven (skill reads context, proposes —
      unchanged from MVP, no new code needed).
      **Added scope (2026-07-09, taxonomy):** cut SKILL.md over to the
      Active-tier state model (the taxonomy doc's Migration section lists
      exactly what replaces what); reconciliation flow for arrivals on
      filed threads (stays-resolved → join siblings / reopens → whole
      thread to Active / refiles → whole thread to new location); and the
      PARA review phase — weekly/on-demand graduation proposals for
      `Areas/<area>/<project>` subfolders (approval-gated, inactivity as
      candidate signal only, no event logged).
- [x] **2.8 History backup.** `history.db` is the irreplaceable file — wire it
      into whatever backup covers the home directory, explicitly.
      **Outcome (2026-07-10):** the user's only home-dir replication is
      iCloud Drive, which never covers `~/.local` — so `history.py` gained
      a `backup` subcommand (SQLite online-backup API for consistency;
      dated copies + `config-latest.yaml` into
      `~/Documents/email-triage-backups/`, inside iCloud-synced Documents;
      pruned to newest 7; `--dest`/`$EMAIL_TRIAGE_BACKUP_DIR`
      overridable). Wired into the skill's session-end step; first backup
      taken. Honest caveat, accepted: iCloud sync is replication, not
      versioned backup — the 7 dated rotating copies are what provide the
      recovery window. Deliberately NOT moved the data dir into a synced
      location: the mirror's constant Maildir renames racing iCloud sync
      would invite conflicted copies.
      **Amendment (2026-07-16):** the skill-invoked session-end backup is
      retired — the first live session hit macOS TCC denials writing
      `~/Documents` from the shell (grants don't attach to already-running
      processes), and per the user, making Claude do it is unnecessary.
      Replaced by 2.9.
- [x] **2.10 Two-stage session sync (2026-07-16, user request: "initial
      sync is quite slow").** Session start: foreground priority sync
      (INBOX+Active+Sent both accounts, ~60–90 s) + backgrounded full sync
      with `.fullsync-done` marker; triage classification proceeds
      immediately; **first act batch gates on the marker** + a re-index
      whose change report is consumed like any other. Session end:
      final sync backgrounded in a chain that `rmdir`s the lock on
      success — no index in the chain (would silently absorb external
      changes that must surface as next session's manual events); a
      failed chain leaves the lock for the existing stale-lock prompt.
      Sent's inclusion in the priority set is load-bearing (phone replies
      → waiting-state correctness). Encoded in SKILL.md steps 5/6/8/9 and
      design §7.1; validation is the next live session.
- [ ] **2.9 Scheduled history backup (follow-up).** A launchd user agent
      (daily) invoking `history.py backup` — the subcommand already exists
      and does the right thing (SQLite online-backup API, dated copies,
      pruning); only the scheduling moves out of Claude's hands. Setup
      notes: launchd jobs get their own TCC identity, so either grant the
      invoking binary access to Documents (or Full Disk Access), or point
      `--dest` somewhere un-TCC'd that iCloud still syncs; verify the
      first scheduled run actually produced a file before trusting it.

**First live full-stack session (2026-07-10, reviewed 2026-07-16):**
59 threads → 0 in 81 min; `activate` vocabulary and change-report manual
events (2 external flags, 1 Sent-detected reply) worked in production —
this also closes 2.4's deferred live external-change validation. Review
found the iteration-cycle costs and all fixes are applied: batch-boundary
sync made a hard invariant (six batches sat local-only for 35 min until
the user asked); unsubscribe flow reworked (body links primary, full-URL
markdown formatting rules, `--header`-doesn't-suppress-body pattern);
`-s 500` pagination and per-account mbsync (+1 retry) documented;
`threads.py` gained `--sender`/`--subject-contains` (the session
hand-rolled 27 JSON filters); `act.py` gained stdin ids (backslash
quoting bit once); category closure now requires a re-list, not memory.

**Cutover notes**
- MVP-era events remain valid unchanged — self-containment was designed for
  exactly this.
- MVP-era sent mail threads correctly retroactively iff 0.1 passed.
- The external-change baseline starts at the first indexed session; phone
  actions before that are simply unrecorded history (§9's accepted caveat).

---

## Phase 3 — Optional, on demand

- [x] DuckDB attached read-only over both files for trend analytics (§9).
      **Outcome (2026-07-16):** built as `scripts/analytics.py` +
      `test_analytics.py` (8 tests green): categories / domains / trends /
      rules / sessions / candidates, JSON out, both DBs attached read-only,
      §9 gotchas encoded once at the attach site. One deviation from the
      2026-07-04 phrasing, deliberate: a separate script rather than
      history.py subcommands — the core logger stays stdlib-only, so
      nothing in the system depends on duckdb existing. Deployment: the
      system python3 has no duckdb module (the July 4 install was the CLI);
      solved with a PEP 723 inline-dependency block + `uv run` (uv present
      via brew) — self-provisioning, no venv, no install step. SKILL.md
      gains an Analytics section (canned-queries-only rule, graceful skip
      without uv) and allowed-tools coverage. Real-data first light:
      26.8% confirmed-unsubscribe rate on decisive marketing events (the
      metric that motivated the column); `candidates` surfaces
      e-tradealerts@etrade.com (9 decisive, 88.9% trash) as the first
      genuine rule proposal — pending only arrival recency within the
      default 30-day window. DuckDB ATTACH quirk found: the path can't be
      a bound parameter; interpolated with manual quote-escaping.
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

---

## Phase 4 — Tier vocabulary & config generalization (designed 2026-07-16, not yet executed)

Design converged over one long session (two-stage sync → taxonomy-agnostic
priority → the five standard spaces → drafts). Decisions are FINAL below;
only execution remains. Execute in order; each task is independently
committable.

**Governing decision — concepts in code, names in config.** The tier
vocabulary (IDs + semantics + derivation logic) is system code; every
folder-name realization of a tier is per-account config, uniform with how
`trash`/`sent` already work. Nobody adopting this system should need
Erik's folder names. Canonical *structure* stays mirrored across accounts;
*names* are per-user.

**Tier ID renames (decided):** `inbox`→`untriaged`, `areas`→`ongoing`,
`archive`→`cold`; `active` and the system roles (`sent`, `drafts`,
`trash`, `junk`, `allmail`, `container`, `unrecognized`) unchanged.
`untriaged`/`ongoing`/`cold` are concept names; the old IDs were Erik's
folder names masquerading as concepts. index.db is disposable — rename is
a rebuild, cheapest it will ever be.

- [x] **4.1 Config schema v2.** Per-account `tiers:` map replaces/extends
      `folders:`: `untriaged`, `active`, `ongoing` (subtree root), `cold`
      (folder AND root), `sent`, `drafts` (NEW), `trash`, `junk`,
      `all_mail` (NEW, gmail). **Nullable tiers:** `active: null` = no
      distinct live location (live mail stays in untriaged; 3-way
      disposition collapses to 2-way — degrades to the MVP model);
      `cold: null` = cold is a *condition, not a place* (derived as
      present-only-in-all_mail; write path is act.py remove; requires
      `all_mail`). Fix the stale gmail `archive: Archive` MVP leftover
      (label also gets deleted server-side — it's empty). Matching rule:
      folder == name OR folder startswith name + "/".
- [x] **4.2 Indexer reads the tier map.** Replace `SYSTEM_FOLDER_TIERS` +
      canonical-name checks in `classify_folder_tier` with the per-account
      config map (tiny restricted line-based parser for our known
      config.yaml shape — stdlib-only constraint holds). Built-in defaults
      = current hardcoded names, so absent config degrades gracefully
      (tests, bare bootstrap). Delete the dead container entries
      `Archive/Career`/`Archive/Housing` (unreachable — shadowed by the
      cold-root prefix rule; user-taxonomy leaked into code). Rename tier
      IDs everywhere: classifier, `message_tiers` view CASE/ranks,
      threads.py `DEFAULT_LIST_TIERS`/`HISTORY_TIERS`, analytics queries
      (`'inbox','allmail'` in candidates), SKILL prose. Rebuild index;
      **regression gate: tier counts must match the pre-rename index
      modulo the renames.**
- [x] **4.3 `priority-folders` subcommand** (indexer): emits per-account
      mbsync box lists. Fast set = tiers `untriaged` + `active` + `sent` +
      `drafts`; slow = everything else (`cold`, `ongoing`, `trash`,
      `junk`, `allmail`) — archiving activity must never promote cold
      storage into the fast lane, and under inbox-only delivery
      (server-rules-delete-never-file) no other folder can originate
      liveness. Empty index degrades to untriaged+sent. SKILL step 5
      builds Stage-1 sync commands from this output instead of hardcoded
      names.
- [x] **4.4 Config-driven archive dispatch** (SKILL Acting section): cold
      tier maps to a name → `act.py move` to it; `cold: null` →
      `act.py remove`. Replaces the hardcoded per-account row in the
      canonical-destinations table. A third account slots in by config
      alone.
- [x] **4.5 Drafts: independent thread status + server round-trip.**
      - `drafted` is a SECOND derived thread status, orthogonal to state
        (`\Flagged` → needs-reply/waiting/holding; `\Draft`-tier member →
        `drafted: true/false`). Drafts excluded from the STATE computation
        (a draft isn't communication; it can't move the conversation axis
        — it IS its own axis). All state×drafted combinations meaningful;
        `needs-reply + drafted` changes the triage verb to "finish/send
        this draft?".
      - threads.py: exclude tier `drafts` from state derivation, expose
        `drafted` on the thread object; tests for the axis independence.
      - **Drafted threads are never silently passed over** — surfaced
        every session until sent or discarded (staleness nudge:
        "this draft is 3 sessions old"). Inverse of waiting's
        never-propose rule; encode both in SKILL Triage + Skip rules.
      - **Save-for-later reply flow** (SKILL Reply section): third exit
        besides send/discard — `himalaya message save -a <acct>-local
        -f <drafts-folder>` (new files carry no ,U= token: mbsync-safe
        upload), `flag add ... draft`, then box-scoped Drafts sync at the
        batch boundary (existing invariant). Standard IMAP-append
        drafting; phone continues the thread via the In-Reply-To/
        References we already construct. Round-trip is existing
        machinery: sent-from-phone → manual reply event; unsent →
        drafted status next session. Drafts deletions in the change
        report (client deletes on send) are expected noise — surface,
        don't log events.
      - `draft` added to history.py ACTIONS (decisive, like activate);
        no new events column — lingering-draft analytics derivable from
        draft events without subsequent reply (facts, not aggregates).
      - Smoke test at execution: save dummy draft, sync, verify
        server-side, clean up; USER validates phone-side editability
        (the one hop not verifiable from here).
- [x] **4.6 Doc alignment.** Taxonomy doc: canonical-structure-vs-
      config-realized-names (supersedes "fixed canonical names identical
      on both accounts"); flag table gains `\Draft` row (set by
      draft-save; derives the independent `drafted` status);
      thread-state derivation section becomes two derived statuses, one
      per flag; tier-ID renames throughout. Design doc: §7.7/§9 config
      schema v2 note; §7.1 two-stage sync already documented. SKILL:
      all of the above operationally. Plugin reinstall at the end.

**Phase 4 outcome (2026-07-17): executed in full, committed across two
commits.** Code (4.2/4.3/4.5) Sonnet-drafted from this plan and verified:
50 tests green across four suites; real-index rebuild matched the baseline
tier counts EXACTLY modulo the renames; `priority-folders` emits the
expected fast sets for both accounts. Config v2 live (machine-local, with
pre-Phase-4 backup); stale empty gmail `Archive` label deleted server-side
(all 11 Archive/* children verified intact — Gmail slash-labels are
independent). One schema addition beyond the plan: `cold_root` (gmail),
naming the real chapter subtree that coexists with `cold: null`. Agent
improvement beyond spec: containers are now DERIVED
(proper-ancestor-of-a-configured-folder) — zero hardcoded container names
remain. Draft round-trip smoke-tested live against iCloud: save → flag →
box-scoped sync → server shows subject + [Seen, Draft]; three operational
findings encoded in SKILL.md: `message save` takes the message on STDIN
(the argument form silently saves an EMPTY message; without redirection it
hangs), LF line endings, and himalaya-local ids are unstable (0→4 across
one sync) so resolve fresh before flagging. Test draft left in iCloud
Drafts for the user's phone-side validation (`ZZZ-Skilltest draft
roundtrip v2` — delete from the phone; that deletion propagating back is
itself a bonus test). USER FOLLOW-UPS: phone check; 2.9 launchd backup
still pending.

**Explicitly rejected during design (recorded so they stay rejected):**
- *Recency as the priority-sync signal.* "Sync recently-active folders
  first" sounded taxonomy-agnostic, but both available signals (message
  `date_utc`, row `indexed_at`) are polluted by the skill's own filing:
  archiving yesterday's newsletter would stamp recent activity onto cold
  storage and promote it into the fast batch (the user's catch). Chasing
  "activity minus our own filings" collapses to "receives deliveries,"
  which under inbox-only delivery is just the untriaged tier — so
  priority is tier-derived, not activity-derived.
- *Overloading `skip` instead of adding action verbs.* When a disposition
  has no verb in the events enum (as "stays open in Active" didn't in
  2.7, and "saved a draft" didn't in Phase 4), the tempting shortcut is
  logging it as `final_action=skip` with a distinguishing `to_folder`.
  Rejected both times: `sender_stats` excludes skips from decisive-action
  rates, so logging a real decision as a skip silently corrupts the
  learning signal. Real dispositions get real verbs (`activate`,
  `draft`), even though that grows the enum.
- *Folder-name string matching below the config boundary.* Names are
  translated to tiers exactly once — at the indexer's classifier, fed by
  config — and every script beneath (threads, act, analytics) reasons in
  tiers only. Any code that compares against a folder-name string
  ("Areas/…", "[Gmail]/…") is either doing the classifier's job in the
  wrong place or hardcoding one user's mailbox; the 2026-07-16 audit
  found both kinds in the indexer, and 4.2 removes them.

---

## Phase 5 — Post-review hardening (designed 2026-07-18 from the second live session's notes + full review; in `gitignored/` at the repo root — near the code, never committed; deletable once Phases 5–6 close)

Execute top-down; each independently committable.

**Phase 5 executed to completion 2026-07-18** (commits 57b2aa6 [5.1+5.3
scripts], 07a1eaf [5.2], 65efe12 [5.3 prose+5.8, +5.1 prose], 32901ca
[5.4], 4c0a7d7 [5.5], d050029 [5.7], 8652282 [5.6]). All test suites
green (indexer 22, threads 16+7, act 6, unsub 12, batch 7, history 3).
Plugin cache refreshed after the final commit.

- [x] **5.1 Content access — `$THR read`.** Raw-file-backed (index
      `maildir_file` → RFC822 → email module): headers + text/plain body,
      `--html-text` for tag-stripped HTML. Kills the dominant friction
      class (unstable himalaya-local ids + lossy rendering + \Seen risk);
      \Seen-safe by construction. Replaces the himalaya read path for all
      content purposes.
- [x] **5.2 Change-report correctness.** Indexer: `flag_changed` entries
      gain old/new flag sets. SKILL step 6 rules: (a) self-echo exclusion —
      entries matching actions you executed and logged this session are
      your own echo, never manual events (fixes the double-logging
      contradiction); (b) `deleted` handling — from trash/junk = auto-purge
      non-event, from anywhere else = surface to user; (c) manual flag
      events for `\Flagged` deltas ONLY (460 \Seen-churn entries nearly
      poisoned sender_stats).
- [x] **5.3 Presentation & consent.**
      - Operating rule: deliverables only in a turn's FINAL message (the
        harness collapses mid-turn prose to "Ran N commands"; user missed
        summaries twice). Content requests get content + a full stop, no
        chained actions.
      - Link formatting: bare full URL on its own line (markdown links are
        NOT clickable in this user's terminal — the previous rule
        universalized one environment's fix). Environment preference, not
        doctrine.
      - Rule 4 carve-out: unsubscribe links surfaced by default; EXECUTED
        only on the user's explicit request, never autonomously.
      - `unsub.py`: `links` (raw-file extraction: List-Unsubscribe +
        -Post, body anchors incl. image-button alt text; replaces the
        failed grep recipe) and `exec` (consent-gated: RFC 8058 POST
        preferred — same op as the mail client's native button, silent
        2xx = success; GET fallback with --url that MUST re-match the
        message's own extracted links — provenance enforced at fire time;
        mailto reported but routed through the normal send gate; stdlib
        urllib, no cookies/auth, capped redirects, timeout).
        allowed-tools += Bash(*unsub.py*). Native-client-button ranked
        first in guidance when List-Unsubscribe-Post exists.
- [x] **5.4 `batch.py`** — act + log-events + index + box-scoped sync in
      ONE call, events auto-derived from act results plus per-batch
      category/decided_by context (call-count is the cost unit in a
      model-driven loop; 12+ four-call cycles last session). Hand-written
      event JSON becomes the exception, not the norm.
- [x] **5.5 Interaction patterns into the DB.** (Outcome: history.py
      `add-rule`/`set-rule`/`add-contact`; rule actions may be interaction
      patterns; declined-unsubscribe = notes convention. Upgraded
      2026-07-19 by user policy: **assume-unsubscribe** — unsubscribable
      mail is noise by default, proposed unsubscribe+trash, until 2+
      declines graduate the sender to newsletter-or-above; counter =
      `sender_stats.declined_unsubs` view column over the same notes
      convention, live DB migrated.) Promote per-sender
      dispositions (priority-contact, summarize-then-delete,
      scan-then-trash) from prose session notes into `contacts`/`rules`
      (rule vocabulary extends from actions to interaction patterns);
      `$HIST context` already surfaces both tables. "Declined unsubscribe"
      = soft dampening signal via notes convention (schema only if
      analytics later demand it). Fixes the learning loop's blind spot:
      sender stats can't distinguish "noise I clear" from "content I
      consume then clear."
- [x] **5.6 SKILL split:** ~250-line operational spine + on-demand
      `reference.md` (quirks, JSON shapes, PARA, analytics). At 646 lines
      the file is all load-bearing but past its ceiling. (Outcome: the
      file had grown to 822 lines by execution time; landed as a 505-line
      spine — every rule/command/trap kept, which set the floor above the
      ~250 estimate — plus 180-line reference.md; token-level loss audit
      clean.)
- [x] **5.7 Small cleanups:** (Outcome: all landed; ZZZ-Skilltest artifact
      deleted from [Gmail]/Trash live — verified sole \Deleted message,
      then expunged.) trash/junk siblings never trigger
      reconciliation; thread-unit moves never touch trash/junk members
      (mega-thread reality); Stage-1 syncs are SEQUENTIAL; \Seen-marking
      exception en route to trash; priority scan modulated by sender
      history (deflating context, not interrupt) and logs the TRUE
      category with the scan hit in notes; `$THR residue` subcommand +
      explicit end-of-session residue step; `priority-folders` emits
      mbsync-ready box strings; delete the leftover flagged ZZZ-Skilltest
      artifact in [Gmail]/Trash; waiting-threads-in-priority-scan
      clarified (unseen security interrupt allowed, still no proposal).
- [x] **5.8 The batch board loop (user request, 2026-07-18).** When a
      category batch starts, it stays the active context until empty or
      the user explicitly redirects: after each resolved sub-batch,
      re-present the SAME numbered board minus resolved items — count in
      the header, per-item annotations preserved (recidivist / borderline
      / new sender), pipeline footer ("Then: newsletters (8) → passive
      (~34) → ..."). Never end a sub-batch with an open-ended "what
      next?" — the re-presented board IS the next prompt. (Transcript-
      verified format from the 2026-07-18 session, requested live:
      "repeat that initial message minus the ones we've dealt with until
      we've cleared them all.")
- [x] **5.9 Session lifecycle script (added + done 2026-07-19, from
      first-session friction).** The session opened with a permission
      prompt for the raw `cat config; mkdir lock; date -u` bundle —
      plain shell falls outside allowed-tools. User rule: standard
      session steps live in scripts. New `session.py`
      (start/unlock/sync-background/check-fullsync/end) absorbs all
      lifecycle glue: one-JSON session open (config + lock + stamp +
      marker clear + priority fast sets), detached sync chains with
      per-account retry and success-gated marker/lock-release, acting
      gate check. SKILL session flow collapsed 9 steps → 7, all inside
      whitelisted scripts; 7 tests.
- [x] **5.10 Prompt-free invocation contract (2026-07-19, from the second
      startup permission prompt).** Root cause found: Claude Code's
      permission engine matches LITERAL statement text — `"$SES" start`
      can never auto-approve (the matcher can't see through a shell
      variable), and every extra statement in a compound command
      (assignments, `echo`) is another prompt trigger. Fixes: (a) every
      script now defaults `--db`/`--maildir`/`--maildir-root` from
      `$EMAIL_TRIAGE_DATA_DIR` — zero-preamble single-statement calls;
      (b) SKILL invocation convention rewritten: `$VAR`s are notation,
      commands are one statement with the script filename literal,
      `<scripts-dir>` written out from where the skill loaded (correct:
      the erik-plugins marketplace is a DIRECTORY source — the repo path
      IS the install on this machine); (c) `session.py start` reports
      `CLAUDE_SKILL_DIR`/`CLAUDE_PLUGIN_ROOT` as a diagnostic; (d) two
      prefix-matched allow rules for the scripts dirs go in
      `~/.claude/settings.json` (documented semantics, like the existing
      himalaya rule) — USER adds these; the agent is guardrail-blocked
      from widening its own permissions.
- [x] **5.11 Compact thread listing (2026-07-19, third startup prompt).**
      A full `$THR list` over a busy mailbox overflowed the tool-result
      budget into a saved file, which the session then inspected with
      `python3 -c` (unwhitelistable → prompt). `threads.py list
      --compact`: per-thread summaries (state/drafted/tiers_present/
      latest member) sized for classification — reconciliation signal
      and priority-scan scope included, no members[]. SKILL: compact is
      THE session-assembly form; `$THR show` before acting on
      multi-member threads; oversized tool-results files are inspected
      with the Read tool, never ad-hoc shell. 2 tests.

---

## Phase 6 — Batch presentation design (added 2026-07-18, user request)

5.8 defines the batch-board LOOP (board stays active until empty); this
phase designs the LOOK — expected to take live iteration, not one pass.

- [ ] **6.1 Survey CLI mail clients for borrowable idioms.** himalaya's
      own table output (baseline — user suggested emulating with tweaks);
      mutt/neomutt `index_format` (a %-format DSL purpose-built for
      dense one-line-per-message displays — likely the richest source);
      aerc's column config; notmuch/alot; mu4e headers view. Deliverable:
      a short comparison with 2–3 concrete board-format candidates.
- [ ] **6.2 Live iteration.** Present the candidates in a real triage
      session; the user picks/tweaks in place; repeat until it stops
      changing. Surfaces to cover beyond the batch board: category
      breakdown summary, priority-scan interrupt, thread detail view,
      session-close summary.
- [ ] **6.3 Encode the winner as exact templates** (in the skill, or
      reference.md once 5.6 lands). Open design question to resolve
      during iteration: freehand-from-template vs. script-rendered —
      a `threads.py board`/`render` subcommand would make boards
      deterministic tool output (consistent, cheap) with the model adding
      only the judgment annotations (recidivist / borderline / new
      sender); pure prose allows more adaptation but drifts. Likely
      hybrid; decide from what the iteration reveals.
      **Phase 7 note (2026-07-19):** whatever wins, build the renderer as
      a real `threads.py board` subcommand taking structured input — it
      is the pivot artifact both frontends share (see Phase 7): Claude
      Code sessions call it for deterministic boards; the TUI later
      embeds the same logic as its list widget.

---

## Phase 7 — TUI frontend via the Claude Agent SDK (designed 2026-07-19, user request; sequenced AFTER Phase 6 settles)

**Decision (user, 2026-07-19): both interaction models, same engine.**
Phase 6 keeps the freeform Claude Code session as-is — it stays the
fast-iteration, conversational way to run triage. Phase 7 adds a second
frontend: a dedicated TUI app with an Agent SDK session as the triage
engine behind it. Nothing in the engine forks: config.yaml, the tier
model, indexer/threads/batch/unsub/history scripts, history.db, and the
skill's rules are the shared substrate; the two frontends differ only in
who owns presentation and how approval is captured.

Architecture sketch (from the 2026-07-19 design discussion):

- **TUI app owns the terminal** (Textual is the working assumption —
  Python, same language as the scripts; board pane, thread-detail pane,
  prompt/status line). A free-text lane to the model is designed in from
  the start, not bolted on — live sessions are full of "wait, what's
  this thread about" digressions and the TUI must not lose them.
- **Long-lived Agent SDK streaming session** (`ClaudeSDKClient` shape,
  not one-shot `query()`) inside the app process. Skill content becomes
  the system prompt; the Bash tool still drives the existing scripts
  unchanged.
- **Presentation inverts from prose to tool calls:** the model gets
  custom tools — `present_board(items, annotations)`,
  `request_approval(batch_spec)`, `show_thread(...)`,
  `update_pipeline(...)` — and stops printing boards. The TUI renders
  deterministically and returns the user's selections (approve / pull
  item N / redirect) as structured tool results. The model contributes
  classification and judgment annotations only.
- **Approval gate becomes code:** the SDK's permission callback
  (`canUseTool`) structurally refuses any `batch.py run` / send that
  wasn't preceded by a confirmed `request_approval` — rule 1 enforced by
  the harness, not by prompt discipline.
- **Sync becomes supervised subprocesses** (progress line, retries)
  instead of nohup chains + marker files.
- **Headless door:** the same session shape minus the TUI = a scheduled
  pre-classification run that leaves a filled board waiting in the
  morning. Explicitly out of scope until the TUI itself works.

What the TUI dissolves (and what stays CC-only): SKILL rule 6
(final-message discipline) is a Claude Code harness artifact — the TUI
has no such failure mode; board re-presentation cost (5.8's re-printed
boards are re-tokenized every sub-batch; the TUI sends deltas); stable
item numbering (free in a list widget). The costs, eyes-open: it's a
real application with app code where Phases 0–6 have none; it gives up
Claude Code's free conversation UI, transcripts, compaction, and plugin
distribution; iteration is a code cycle, not a SKILL.md edit.

Tasks (draft — firm up after Phase 6):

- [ ] **7.1 Engine/frontend boundary audit.** Read the post-Phase-6
      SKILL and mark each rule ENGINE (holds in both frontends: tier
      model, sync invariants, logging, \Seen discipline) or FRONTEND
      (presentation/consent mechanics that the TUI implements in code:
      rule 6, board re-presentation, approval phrasing). Deliverable: the
      system prompt for the SDK session, derived from the spine with
      frontend-only rules stripped.
- [ ] **7.2 Walking skeleton.** Textual app + SDK streaming session +
      ONE custom tool (`present_board`) + the free-text lane. Success:
      a real session start (two-stage sync, index, threads, priority
      scan) ending in a rendered board the user can scroll.
- [ ] **7.3 Approval + acting loop.** `request_approval` tool +
      `canUseTool` gate wired to `$BATCH run`; resolved items leave the
      board; sync status line. The 5.8 loop as UI state.
- [ ] **7.4 Thread detail + reply flow.** `show_thread` pane ($THR
      read-backed), reply drafting in an editor pane, the three exits
      (send / save-for-later / discard) as keybindings over the same
      himalaya recipes.
- [ ] **7.5 Session lifecycle.** Lock, session stamp, residue check,
      session-end chain, history/notes — app-managed, engine-logged.
      Then a real A/B week: same mailbox, alternating frontends; fold
      what the TUI teaches back into the shared engine.
- [ ] **7.6 Packaging consideration (noted 2026-07-19, from the
      PATH/exec-bit discussion).** In-skill sessions stay on
      `$VAR`s/`${CLAUDE_SKILL_DIR}` — version-correct, self-contained,
      no PATH pollution (script names like session.py/batch.py are far
      too generic for the global PATH, and PATH would silently pick one
      of repo-vs-cache). But once multiple consumers exist (TUI, launchd
      backup/pre-classification jobs), package the scripts as a proper
      CLI: pyproject.toml with prefixed entry points
      (`email-triage-session`, `email-triage-batch`, ...) installed via
      `uv tool install`/pipx — unique names, versioned installs, callable
      from any shell. The skill keeps its `$VAR` convention regardless.
