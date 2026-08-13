#!/usr/bin/env python3
"""Tests for history.py's 5.5 additions: add-rule / set-rule / add-contact
(interaction patterns promoted from prose notes into the DB). Runs the CLI
via subprocess against a temp data dir; never touches real data."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).parent

sys.path.insert(0, str(SCRIPTS))
import history  # noqa: E402
import untrusted  # noqa: E402


class HistoryCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="email-triage-history-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.run_ok(str(SCRIPTS / "init_history_db.py"), "--data-dir", str(self.tmp))

    def run_ok(self, script, *args):
        proc = subprocess.run([sys.executable, script, *args],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def hist(self, *args):
        return self.run_ok(str(SCRIPTS / "history.py"), "--data-dir", str(self.tmp), *args)

    def context(self):
        return json.loads(self.hist("context"))

    def test_add_rule_interaction_pattern_and_deactivate(self):
        out = self.hist("add-rule", "--id", "updates-digest",
                        "--predicate", "sender = updates@example.com",
                        "--action", "summarize-then-trash",
                        "--origin", "user")
        self.assertIn("rule updates-digest added", out)
        rules = self.context()["active_rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["id"], "updates-digest")
        self.assertEqual(rules[0]["action"], "summarize-then-trash")
        self.assertEqual(rules[0]["origin"], "user")

        self.hist("set-rule", "updates-digest", "--active", "0")
        self.assertEqual(self.context()["active_rules"], [])
        self.hist("set-rule", "updates-digest", "--active", "1")
        self.assertEqual(len(self.context()["active_rules"]), 1)

    # -- rule CRUD (2026-08-10) --------------------------------------------
    #
    # The table used to be append-plus-a-visibility-bit: a badly worded rule
    # could only be retired and rewritten, leaving a dead row forever. The
    # guard rail that replaced that immutability is `events.rule_id` -- a claim
    # about the basis a decision was made on, which the mutating verbs refuse to
    # rewrite without --force.

    def hist_fail(self, *args):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "history.py"),
             "--data-dir", str(self.tmp), *args],
            capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        return proc.stderr

    def a_rule(self, slug="my-rule", predicate="sender = a@example.com",
               action="trash"):
        self.hist("add-rule", "--id", slug, "--predicate", predicate,
                  "--action", action, "--origin", "user")

    def rules(self, *args):
        return json.loads(self.hist("list-rules", *args))

    def cite_rule(self, slug):
        """An event whose decision claims rule `slug` as its basis."""
        self.hist("log-event", "--session", "2026-08-10T00:00:00Z",
                  "--sender", "a@example.com", "--category", "newsletter",
                  "--proposed", "trash", "--final", "trash",
                  "--decided-by", "approved", "--rule-id", slug)

    def test_list_rules_shows_retired_ones_which_context_hides(self):
        self.a_rule(slug="keep-this")
        self.a_rule(slug="retire-this")
        self.hist("set-rule", "retire-this", "--active", "0")
        self.assertEqual([r["id"] for r in self.context()["active_rules"]],
                         ["keep-this"])
        self.assertEqual([r["id"] for r in self.rules()["rules"]],
                         ["keep-this", "retire-this"])
        self.assertEqual(self.rules("--filter", "active")["count"], 1)
        self.assertEqual([r["id"] for r in
                          self.rules("--filter", "inactive")["rules"]],
                         ["retire-this"])

    def test_list_rules_reports_citation_counts(self):
        self.a_rule()
        self.assertEqual(self.rules()["rules"][0]["cited_by"], 0)
        self.cite_rule("my-rule")
        self.cite_rule("my-rule")
        self.assertEqual(self.rules()["rules"][0]["cited_by"], 2)

    def test_update_rewrites_text_and_stamps_updated_at(self):
        self.a_rule()
        self.assertIsNone(self.rules()["rules"][0]["updated_at"])
        self.hist("update-rule", "my-rule", "--action", "summarize-then-trash")
        row = self.rules()["rules"][0]
        self.assertEqual(row["action"], "summarize-then-trash")
        self.assertEqual(row["predicate"], "sender = a@example.com")  # untouched
        self.assertRegex(row["updated_at"], r"^\d{4}-\d{2}-\d{2}T")

    def test_update_with_no_fields_is_an_error(self):
        self.a_rule()
        self.assertIn("nothing to update",
                      self.hist_fail("update-rule", "my-rule"))

    def test_toggling_active_is_not_a_rewrite(self):
        """Retirement changes what is in force from now on and leaves the
        recorded basis intact, so it is never gated and stamps no updated_at."""
        self.a_rule()
        self.cite_rule("my-rule")
        self.hist("update-rule", "my-rule", "--active", "0")
        row = self.rules()["rules"][0]
        self.assertEqual(row["active"], 0)
        self.assertIsNone(row["updated_at"])

    def test_rewriting_a_cited_rule_is_refused_then_allowed_with_force(self):
        self.a_rule()
        self.cite_rule("my-rule")
        err = self.hist_fail("update-rule", "my-rule", "--action", "rewritten")
        self.assertIn("1 event(s) cite rule my-rule", err)
        self.assertIn("--force", err)
        self.assertIn("set-rule my-rule --active 0", err)   # the lossless route
        self.assertEqual(self.rules()["rules"][0]["action"], "trash")

        self.hist("update-rule", "my-rule", "--action", "rewritten", "--force")
        self.assertEqual(self.rules()["rules"][0]["action"], "rewritten")

    def test_delete_removes_an_uncited_rule(self):
        self.a_rule()
        self.assertIn("rule my-rule deleted", self.hist("delete-rule", "my-rule"))
        self.assertEqual(self.rules()["count"], 0)

    def test_deleting_a_cited_rule_is_refused_then_allowed_with_force(self):
        self.a_rule()
        self.cite_rule("my-rule")
        err = self.hist_fail("delete-rule", "my-rule")
        self.assertIn("1 event(s) cite rule my-rule", err)
        self.assertEqual(self.rules()["count"], 1)      # nothing removed

        out = self.hist("delete-rule", "my-rule", "--force")
        self.assertIn("forced", out)
        self.assertEqual(self.rules()["count"], 0)

    def test_missing_id_errors_on_every_rule_verb(self):
        for argv in (("update-rule", "nope", "--action", "x"),
                     ("delete-rule", "nope")):
            with self.subTest(argv=argv):
                self.assertIn("no rule with id nope", self.hist_fail(*argv))

    def test_rule_prose_is_escaped_and_declared_in_list_rules(self):
        """predicate/action are free text a model may have drafted, so they are
        untrusted for the same reason event notes are. `id` is OURS -- a
        validated slug, never sender-derived -- so it stays trusted."""
        self.a_rule(predicate="subject has <b>sale</b>", action="trash & file")
        out = self.rules()
        declared = set(out["_trust"]["untrusted_fields"])
        self.assertIn("rules.predicate", declared)
        self.assertIn("rules.action", declared)
        self.assertIn("&lt;b&gt;", out["rules"][0]["predicate"])
        self.assertNotIn("rules.id", declared)
        self.assertNotIn("count", declared)
        self.assertNotIn("rules.cited_by", declared)

    # -- slug ids (2026-08-10) ---------------------------------------------
    #
    # Numbers collided with the SKILL's own engine rules 1-6: "rule 4" read as
    # either, and two rules ended up citing an engine rule by bare number while
    # themselves sitting under that number.

    def test_slug_format_is_enforced(self):
        # `--id=X` rather than `--id X` so a leading-dash value reaches the
        # validator: argparse would otherwise reject it as a missing argument,
        # which tests argparse and not us.
        for bad in ("Receipt-Filing", "receipt_filing", "receipt--filing",
                    "-receipt", "receipt-", "ab", "x" * 33, "receipt filing",
                    "réçu"):
            with self.subTest(slug=bad):
                self.assertIn("kebab-case", self.hist_fail(
                    "add-rule", f"--id={bad}", "--predicate", "p",
                    "--action", "a", "--origin", "user"))
        self.assertEqual(self.rules()["count"], 0)

    def test_identical_duplicate_says_so_and_points_at_update(self):
        """A collision is information: same text means the caller is re-adding
        what exists, and the fix is `update-rule`, not a new name."""
        self.a_rule()
        err = self.hist_fail("add-rule", "--id", "my-rule",
                             "--predicate", "sender = a@example.com",
                             "--action", "trash", "--origin", "user")
        self.assertIn("IDENTICAL", err)
        self.assertIn("update-rule my-rule", err)

    def test_differing_duplicate_shows_the_existing_text_to_compare(self):
        self.a_rule()
        err = self.hist_fail("add-rule", "--id", "my-rule",
                             "--predicate", "something else entirely",
                             "--action", "hold", "--origin", "user")
        self.assertIn("DIFFERS", err)
        self.assertIn("sender = a@example.com", err)     # the existing predicate
        self.assertIn("different id", err)
        # Never auto-suffixed: one rule, unchanged.
        self.assertEqual(self.rules()["count"], 1)
        self.assertEqual(self.rules()["rules"][0]["action"], "trash")

    def test_a_slug_still_cited_by_events_cannot_be_reused(self):
        """This is what replaces a date qualifier in the id. An orphaned
        reference is honest; a re-bound one reads correct forever and is not."""
        self.a_rule()
        self.cite_rule("my-rule")
        self.hist("delete-rule", "my-rule", "--force")
        self.assertEqual(self.rules()["count"], 0)

        err = self.hist_fail("add-rule", "--id", "my-rule", "--predicate", "p",
                             "--action", "a", "--origin", "user")
        self.assertIn("still cited by 1 event(s)", err)
        self.assertIn("re-point", err)
        self.assertEqual(self.rules()["count"], 0)

    def test_rename_moves_the_slug_and_is_gated_like_a_rewrite(self):
        self.a_rule()
        self.hist("update-rule", "my-rule", "--new-id", "better-name")
        row = self.rules()["rules"][0]
        self.assertEqual(row["id"], "better-name")
        self.assertRegex(row["updated_at"], r"^\d{4}-\d{2}-\d{2}T")

        self.cite_rule("better-name")
        err = self.hist_fail("update-rule", "better-name", "--new-id", "third")
        self.assertIn("1 event(s) cite rule better-name", err)
        self.assertEqual(self.rules()["rules"][0]["id"], "better-name")

    def test_rename_onto_a_taken_slug_is_refused(self):
        self.a_rule(slug="first")
        self.a_rule(slug="second")
        err = self.hist_fail("update-rule", "first", "--new-id", "second")
        self.assertIn("already exists", err)
        self.assertEqual([r["id"] for r in self.rules()["rules"]],
                         ["first", "second"])

    def test_set_rule_missing_id_errors(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "history.py"), "--data-dir", str(self.tmp),
             "set-rule", "99", "--active", "0"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no rule with id 99", proc.stderr)

    def test_declined_unsubs_counted_in_sender_stats(self):
        # Two declines + one unrelated trash for the same sender: the
        # assume-unsubscribe policy's threshold counter must read 2.
        for notes in ("declined-unsubscribe", "declined-unsubscribe; keeps reading", None):
            args = ["log-event", "--session", "2026-07-19T00:00:00Z",
                    "--sender", "News@Example.com", "--category", "newsletter",
                    "--proposed", "trash", "--final", "hold",
                    "--decided-by", "override"]
            if notes:
                args += ["--notes", notes]
            self.hist(*args)
        (s,) = json.loads(self.hist("context", "--all-senders"))["sender_stats"]
        self.assertEqual(s["sender_addr"], "news@example.com")
        self.assertEqual(s["declined_unsubs"], 2)

    def _event(self, sender, final, proposed=None, decided="approved"):
        return self.hist("log-event", "--session", "2026-08-06T00:00:00Z",
                         "--sender", sender, "--proposed", proposed or final,
                         "--final", final, "--decided-by", decided)

    def _stats(self, sender):
        # --all-senders since 2026-08-07: context no longer prefetches the dump,
        # and these tests are asserting the sender_stats view's own arithmetic.
        rows = json.loads(self.hist("context", "--all-senders"))["sender_stats"]
        return next(r for r in rows if r["sender_addr"] == sender)

    def test_retired_verbs_are_rejected(self):
        # 'skip' was renamed to 'hold' and 'draft' removed entirely
        # (2026-08-06). Guard both so neither can quietly come back.
        for verb in ("skip", "draft"):
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "history.py"), "--data-dir", str(self.tmp),
                 "log-event", "--session", "s", "--sender", "a@b.com",
                 "--proposed", verb, "--final", verb, "--decided-by", "approved"],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2, f"{verb} should be rejected")
            self.assertIn("invalid choice", proc.stderr)

    def test_rates_are_null_without_terminal_actions(self):
        # Non-terminal only (flag/activate/hold): there is NO evidence of how
        # this sender's threads finish, so the rates must be NULL -- not 0.0,
        # which reads as "never replies" and is what the pre-2026-08-06
        # denominator asserted from zero terminal events.
        self._event("solo@example.com", "flag")
        s = self._stats("solo@example.com")
        self.assertIsNone(s["reply_rate"])
        self.assertIsNone(s["trash_rate"])
        self.assertEqual(s["decisions"], 1)   # it WAS a decision
        self.assertEqual(s["holds"], 0)

    def test_non_terminal_actions_do_not_dilute_rates(self):
        # activate then reply on the same sender = one conversation that
        # ended in a reply. Old denominator (!= 'skip') gave 0.5.
        self._event("pete@example.com", "activate")
        self._event("pete@example.com", "reply")
        s = self._stats("pete@example.com")
        self.assertEqual(s["reply_rate"], 1.0)
        self.assertEqual(s["decisions"], 2)

    def test_hold_is_the_only_non_decision(self):
        self._event("h@example.com", "hold", proposed="trash", decided="override")
        s = self._stats("h@example.com")
        self.assertEqual(s["decisions"], 0)
        self.assertEqual(s["holds"], 1)
        self.assertIsNone(s["reply_rate"])

    def test_migration_renames_skip_and_reshapes_view(self):
        import sqlite3
        db = self.tmp / "history.db"
        self._event("m@example.com", "trash")
        conn = sqlite3.connect(db)
        # Simulate a pre-migration DB: legacy value in both action columns
        # and the old single-filter view.
        conn.execute("UPDATE events SET proposed_action='skip', final_action='skip'")
        conn.execute("DROP VIEW sender_stats")
        conn.execute("CREATE VIEW sender_stats AS SELECT sender_addr, "
                     "SUM(final_action != 'skip') AS decisions, "
                     "SUM(final_action = 'skip') AS skips FROM events GROUP BY sender_addr")
        conn.commit(); conn.close()

        self.run_ok(str(SCRIPTS / "init_history_db.py"), "--data-dir", str(self.tmp))

        conn = sqlite3.connect(db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sender_stats)")}
        vals = [r[0] for r in conn.execute(
            "SELECT DISTINCT proposed_action FROM events "
            "UNION SELECT DISTINCT final_action FROM events")]
        n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()
        self.assertEqual(vals, ["hold"])          # renamed in BOTH columns
        self.assertEqual(n, 1)                    # no rows lost
        self.assertIn("holds", cols)              # view reshaped
        self.assertNotIn("skips", cols)
        self.assertIn("declined_unsubs", cols)    # earlier migration intact

    def test_migration_recreates_old_view_with_declined_unsubs(self):
        import sqlite3
        db = self.tmp / "history.db"
        conn = sqlite3.connect(db)
        conn.execute("DROP VIEW sender_stats")
        conn.execute("CREATE VIEW sender_stats AS SELECT sender_addr FROM events GROUP BY sender_addr")
        conn.commit()
        conn.close()
        self.run_ok(str(SCRIPTS / "init_history_db.py"), "--data-dir", str(self.tmp))
        conn = sqlite3.connect(db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sender_stats)")}
        conn.close()
        self.assertIn("declined_unsubs", cols)

    def test_add_contact_upsert_preserves_unset_fields(self):
        self.hist("add-contact", "Dad@Example.com",
                  "--relation", "dad", "--vip", "1")
        (c,) = self.context()["contacts"]
        self.assertEqual(c["addr"], "dad@example.com")  # normalized
        self.assertEqual(c["relation"], "dad")
        self.assertEqual(c["is_vip"], 1)

        # Second call touches only the given field.
        self.hist("add-contact", "dad@example.com", "--notes", "prefers calls")
        (c,) = self.context()["contacts"]
        self.assertEqual(c["relation"], "dad")
        self.assertEqual(c["is_vip"], 1)
        self.assertEqual(c["notes"], "prefers calls")


class SenderOptoutTests(unittest.TestCase):
    """sender_optouts + event_optouts (2026-08-13).

    An opt-out is a fact about a SENDER RELATIONSHIP and happens once;
    `events.unsubscribed` is a fact about a MESSAGE and is correctly true on all
    16 rows of a batch. Both are real. Only the second had a home before this,
    so the when/how/scope of an act had to be reverse-engineered with MIN()/MAX()
    over duplicated booleans, and scope had nowhere to live at all."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="email-triage-optout-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.init()

    def init(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "init_history_db.py"),
             "--data-dir", str(self.tmp)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def hist(self, *args):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "history.py"),
             "--data-dir", str(self.tmp), *args],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def sql(self, query, params=()):
        conn = sqlite3.connect(self.tmp / "history.db")
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(query, params)]
        finally:
            conn.close()

    def log(self, session, sender, occurred_at, unsubscribed=False):
        args = ["log-event", "--session", session, "--occurred-at", occurred_at,
                "--sender", sender, "--category", "marketing",
                "--proposed", "trash", "--final", "trash",
                "--decided-by", "approved"]
        if unsubscribed:
            args.append("--unsubscribed")
        self.hist(*args)

    def optouts(self, *args):
        return json.loads(self.hist("optouts", *args))

    # -- the write path -----------------------------------------------------

    def test_set_unsubscribed_records_the_act_once_for_many_events(self):
        """The Roy Cooper shape: one board, one yes, 16 trashings. Sixteen event
        rows are correct; ONE opt-out row is the act."""
        for i in range(16):
            self.log("S1", "info@e.example.com", f"2026-08-07T20:06:{i:02d}Z")
        self.hist("set-unsubscribed", "--session", "S1",
                  "--senders", "info@e.example.com",
                  "--scope", "all campaign mail",
                  "--mechanism", "one-click-post")
        out = self.optouts()
        self.assertEqual(out["count"], 1)
        row = out["optouts"][0]
        self.assertEqual(row["sender_addr"], "info@e.example.com")
        self.assertEqual(row["kind"], "unsubscribed")
        self.assertEqual(row["mechanism"], "one-click-post")
        self.assertEqual(row["scope"], "all campaign mail")
        self.assertEqual(row["events_covered"], 16)
        self.assertEqual(
            self.sql("SELECT COUNT(*) n FROM events WHERE unsubscribed=1")[0]["n"],
            16)

    def test_amending_twice_in_one_session_does_not_mint_two_acts(self):
        """The DNC shape: messages trashed at 01:31, a straggler amended at
        14:27, one click. Idempotent per (sender, session, kind)."""
        self.log("S1", "a@x.com", "2026-08-13T01:31:57Z")
        self.hist("set-unsubscribed", "--session", "S1", "--senders", "a@x.com")
        self.log("S1", "a@x.com", "2026-08-13T14:27:30Z")
        self.hist("set-unsubscribed", "--session", "S1", "--senders", "a@x.com",
                  "--scope", "filled in on the second pass")
        out = self.optouts()
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["optouts"][0]["events_covered"], 2)
        # A later call supplies what the first omitted rather than duplicating.
        self.assertEqual(out["optouts"][0]["scope"], "filled in on the second pass")

    def test_two_attempts_in_different_sessions_are_two_acts(self):
        """BHCCU: opted out 2026-07-03, mailed again, opted out again 07-18.
        "Did the second attempt stick?" is the question this table answers, and
        it needs both rows to be askable."""
        self.log("S1", "b@x.com", "2026-07-03T00:00:00Z")
        self.hist("set-unsubscribed", "--session", "S1", "--senders", "b@x.com")
        self.log("S2", "b@x.com", "2026-07-18T00:00:00Z")
        self.hist("set-unsubscribed", "--session", "S2", "--senders", "b@x.com")
        self.assertEqual(self.optouts()["count"], 2)

    def test_scope_is_what_stops_a_false_recidivism_alarm(self):
        self.log("S1", "noreply@medium.example", "2026-08-13T00:00:00Z")
        self.hist("set-unsubscribed", "--session", "S1",
                  "--senders", "noreply@medium.example",
                  "--scope", "marketing digests only; login codes expected to continue")
        self.assertIn("login codes expected",
                      self.optouts()["optouts"][0]["scope"])

    def test_optouts_filters_by_sender(self):
        for addr in ("a@x.com", "b@x.com"):
            self.log("S1", addr, "2026-08-13T00:00:00Z")
        self.hist("set-unsubscribed", "--session", "S1",
                  "--senders", "a@x.com,b@x.com")
        self.assertEqual(self.optouts()["count"], 2)
        self.assertEqual(self.optouts("--sender", "a@x.com")["count"], 1)

    # -- the backfill -------------------------------------------------------

    def test_backfill_groups_by_session_not_by_timestamp(self):
        """The grouping key that the live DNC data settled. Its four events span
        01:31 and 14:27 from ONE click; grouping on occurred_at would mint two
        acts. Sessions are the unit an opt-out actually happens in."""
        conn = sqlite3.connect(self.tmp / "history.db")
        conn.execute("DELETE FROM event_optouts")
        conn.execute("DELETE FROM sender_optouts")
        conn.commit()
        conn.close()
        for at in ("2026-08-13T01:31:57Z", "2026-08-13T14:27:30Z"):
            self.log("S1", "dnc@x.com", at, unsubscribed=True)
        self.log("S2", "dnc@x.com", "2026-09-01T00:00:00Z", unsubscribed=True)

        self.init()          # re-run migrate(), which backfills
        rows = self.sql("SELECT * FROM sender_optouts ORDER BY occurred_at")
        self.assertEqual(len(rows), 2)                      # 2 sessions, not 3
        self.assertEqual(rows[0]["occurred_at"], "2026-08-13T01:31:57Z")
        self.assertEqual(rows[0]["kind"], "unsubscribed")
        self.assertEqual(rows[0]["confirmed"], 1)
        # Unknown rather than guessed, for records predating the table.
        self.assertIsNone(rows[0]["mechanism"])
        self.assertIsNone(rows[0]["scope"])
        self.assertEqual(
            self.sql("SELECT COUNT(*) n FROM event_optouts")[0]["n"], 3)

    def test_backfill_is_idempotent_and_leaves_events_untouched(self):
        before = self.sql("PRAGMA table_info(events)")
        self.log("S1", "c@x.com", "2026-08-13T00:00:00Z", unsubscribed=True)
        self.init()
        self.init()          # a second migrate() must not duplicate
        self.assertEqual(self.sql("SELECT COUNT(*) n FROM sender_optouts")[0]["n"], 1)
        # The whole point of the junction: events' schema is frozen.
        self.assertEqual(self.sql("PRAGMA table_info(events)"), before)

    def test_one_event_cannot_be_claimed_by_two_optouts(self):
        """UNIQUE(event_id) does the work an events.optout_id column would have
        done structurally. The relationship is 1:N, not many-to-many."""
        self.log("S1", "d@x.com", "2026-08-13T00:00:00Z", unsubscribed=True)
        self.init()
        conn = sqlite3.connect(self.tmp / "history.db")
        try:
            conn.execute("INSERT INTO sender_optouts (sender_addr, occurred_at, "
                         "kind) VALUES ('d@x.com', '2026-09-01T00:00:00Z', "
                         "'unsubscribed')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO event_optouts (event_id, optout_id) "
                             "SELECT id, 2 FROM events LIMIT 1")
        finally:
            conn.close()

    def test_optout_prose_is_escaped_and_the_address_stays_a_handle(self):
        self.log("S1", "e@x.com", "2026-08-13T00:00:00Z")
        self.hist("set-unsubscribed", "--session", "S1", "--senders", "e@x.com",
                  "--scope", "digests <b>only</b>")
        out = self.optouts()
        declared = set(out["_trust"]["untrusted_fields"])
        self.assertIn("optouts.scope", declared)
        self.assertIn("optouts.sender_addr", declared)     # handle: unescaped
        self.assertIn("&lt;b&gt;", out["optouts"][0]["scope"])
        self.assertEqual(out["optouts"][0]["sender_addr"], "e@x.com")
        self.assertNotIn("optouts.kind", declared)


class SetUnsubscribedTests(unittest.TestCase):
    """Amending `unsubscribed` after the fact. Needed because SKILL step 5
    assumes confirmation precedes logging -- true for the automated one-click
    path (a 2xx IS the confirmation), false for manual links the user clicks
    later, sometimes after the session closed."""

    SESSION = "2026-08-07T00:00:00Z"
    OTHER = "2026-07-01T00:00:00Z"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="email-triage-unsub-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.run_ok(str(SCRIPTS / "init_history_db.py"), "--data-dir", str(self.tmp))
        for session, addr in ((self.SESSION, "a@x.example"),
                              (self.SESSION, "a@x.example"),
                              (self.SESSION, "b@x.example"),
                              (self.OTHER,   "a@x.example")):
            self.hist("log-event", "--session", session, "--sender", addr,
                      "--proposed", "trash", "--final", "trash",
                      "--decided-by", "approved")

    run_ok = HistoryCliTests.run_ok
    hist = HistoryCliTests.hist

    def flags(self):
        conn = sqlite3.connect(self.tmp / "history.db")
        try:
            return conn.execute(
                "SELECT session_started_at, sender_addr, unsubscribed "
                "FROM events ORDER BY id").fetchall()
        finally:
            conn.close()

    def test_amends_only_the_named_session_and_senders(self):
        out = json.loads(self.hist("set-unsubscribed", "--session", self.SESSION,
                                   "--senders", "a@x.example"))
        self.assertEqual(out["updated"], 2)
        rows = self.flags()
        # this session's a@ -> set; this session's b@ -> untouched;
        # the OTHER session's a@ -> untouched.
        self.assertEqual([r[2] for r in rows], [1, 1, 0, 0])

    def test_is_idempotent(self):
        self.hist("set-unsubscribed", "--session", self.SESSION,
                  "--senders", "a@x.example")
        out = json.loads(self.hist("set-unsubscribed", "--session", self.SESSION,
                                   "--senders", "a@x.example"))
        self.assertEqual(out["updated"], 0)   # already 1, nothing left to change

    def test_addresses_are_lowercased_to_match_stored_form(self):
        out = json.loads(self.hist("set-unsubscribed", "--session", self.SESSION,
                                   "--senders", "A@X.Example"))
        self.assertEqual(out["updated"], 2)

    def test_unknown_sender_changes_nothing(self):
        out = json.loads(self.hist("set-unsubscribed", "--session", self.SESSION,
                                   "--senders", "nobody@x.example"))
        self.assertEqual(out["updated"], 0)
        self.assertEqual(out["per_sender"], [])
        self.assertEqual([r[2] for r in self.flags()], [0, 0, 0, 0])


class QuerySurfaceTests(unittest.TestCase):
    """`$HIST query`: read-only SQL. Enforcement is SQLite's -- there is NO SQL
    parsing, deliberately, so these tests ARE the security boundary."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="email-triage-query-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.run_ok(str(SCRIPTS / "init_history_db.py"), "--data-dir", str(self.tmp))
        self.hist("log-note", "--session", "2026-08-07T00:00:00Z", "a note")

    run_ok = HistoryCliTests.run_ok
    hist = HistoryCliTests.hist

    def q(self, sql, *extra):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "history.py"), "--data-dir",
             str(self.tmp), "query", "--sql", sql, *extra],
            capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr

    def refused(self, sql, expect):
        code, out, err = self.q(sql)
        self.assertNotEqual(code, 0, f"NOT refused: {sql}")
        self.assertIn(expect, (err + out).lower())

    def test_reads_work(self):
        code, out, _ = self.q("SELECT COUNT(*) AS n FROM session_notes")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["rows"][0]["n"], 1)
        self.assertEqual(payload["row_count"], 1)

    def test_writes_are_refused(self):
        for sql in ("INSERT INTO session_notes (session_started_at, written_at, "
                    "note) VALUES ('a','b','c')",
                    "UPDATE session_notes SET note = 'x'",
                    "DELETE FROM session_notes",
                    "DROP TABLE events",
                    "CREATE TABLE t(a)"):
            with self.subTest(sql=sql[:24]):
                self.refused(sql, "readonly")

    def test_attach_escape_is_closed(self):
        """The hole `mode=ro` does NOT close on its own: an attached database
        carries its OWN flags, so a read-only connection can attach
        'file:...?mode=rwc' and write THERE. Verified to succeed without
        setlimit(SQLITE_LIMIT_ATTACHED, 0), which is why that layer exists."""
        target = self.tmp / "evil.db"
        self.refused(f"ATTACH DATABASE 'file:{target}?mode=rwc' AS e",
                     "too many attached")
        self.assertFalse(target.exists(), "ATTACH created a writable database")

    def test_stacked_statements_are_refused(self):
        # A second statement is where a blacklist-style parser leaks; the driver
        # refuses it outright.
        self.refused("SELECT 1; DROP TABLE events", "one statement")

    def test_runaway_query_is_interrupted(self):
        self.refused("WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x+1 "
                     "FROM r) SELECT count(*) FROM r", "interrupted")

    def test_the_database_is_actually_unchanged(self):
        for sql in ("DELETE FROM session_notes", "DROP TABLE session_notes"):
            self.q(sql)
        code, out, _ = self.q("SELECT COUNT(*) AS n FROM session_notes")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["rows"][0]["n"], 1)

    def test_row_cap_truncates_visibly_never_silently(self):
        for i in range(8):
            self.hist("log-note", "--session", "2026-08-07T00:00:00Z", f"n{i}")
        code, out, _ = self.q("SELECT note FROM session_notes", "--limit", "3")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(len(payload["rows"]), 3)
        self.assertTrue(payload["truncated"])
        self.assertIn("--limit 3", payload["hint"])

    def test_arbitrary_columns_are_classified_untrusted_wins_ties(self):
        """Per-table keying cannot apply to a join, so classification is by bare
        name with untrusted winning any tie. `account` is trusted only in events;
        `notes` is untrusted in events AND contacts, so `notes` is untrusted
        everywhere here."""
        self.hist("add-contact", "pete@x.example", "--notes", "friend <b>x</b>")
        code, out, _ = self.q("SELECT addr, notes, is_vip FROM contacts")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        declared = set(payload["_trust"]["untrusted_fields"])
        self.assertIn("rows.notes", declared)
        self.assertIn("rows.addr", declared)          # handle: declared...
        self.assertNotIn("rows.is_vip", declared)     # an int, never listed
        row = payload["rows"][0]
        self.assertIn("&lt;b&gt;", row["notes"])      # ...prose escaped
        self.assertEqual(row["addr"], "pete@x.example")  # ...handle unaltered

    def test_unknown_column_names_default_to_untrusted(self):
        code, out, _ = self.q("SELECT 'x' AS surprise_column FROM session_notes "
                              "LIMIT 1")
        self.assertEqual(code, 0)
        self.assertIn("rows.surprise_column",
                      json.loads(out)["_trust"]["untrusted_fields"])


class ContextPrefetchTests(unittest.TestCase):
    """sender_stats stopped being prefetched 2026-08-07: it is a KEYED lookup
    whose keys come from `$THR list --summary`, i.e. after this call. Notes,
    rules and contacts stay because they are free-form judgement material you
    cannot know to ask for."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="email-triage-ctx-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.run_ok(str(SCRIPTS / "init_history_db.py"), "--data-dir", str(self.tmp))
        for addr in ("a@x.example", "b@x.example"):
            self.hist("log-event", "--session", "2026-08-07T00:00:00Z",
                      "--sender", addr, "--proposed", "trash", "--final", "trash",
                      "--decided-by", "approved")

    run_ok = HistoryCliTests.run_ok
    hist = HistoryCliTests.hist

    def ctx(self, *extra):
        return json.loads(self.hist("context", *extra))

    def test_sender_stats_absent_by_default_but_counted(self):
        payload = self.ctx()
        self.assertNotIn("sender_stats", payload)
        self.assertEqual(payload["sender_stats_available"], 2)
        self.assertIn("--summary", payload["sender_stats_hint"])
        # The free-form blocks are still prefetched.
        for key in ("session_notes", "active_rules", "contacts"):
            self.assertIn(key, payload)

    def test_senders_flag_fetches_exactly_those(self):
        payload = self.ctx("--senders", "a@x.example")
        self.assertEqual([r["sender_addr"] for r in payload["sender_stats"]],
                         ["a@x.example"])
        self.assertNotIn("sender_stats_available", payload)

    def test_all_senders_restores_the_dump(self):
        payload = self.ctx("--all-senders")
        self.assertEqual(len(payload["sender_stats"]), 2)


class TrustedColumnsTestsFixture(unittest.TestCase):
    """Fixture only — defines NO tests. Subclassing a TestCase that has
    tests re-runs every one of them under the subclass's name, which
    inflates the suite and reports each failure twice, from a class that
    did not cause it."""

    """Phase 6.5: trust as a column property, per table."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="email-triage-cols-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.run_ok(str(SCRIPTS / "init_history_db.py"),
                    "--data-dir", str(self.tmp))
        self.conn = sqlite3.connect(self.tmp / "history.db")
        self.addCleanup(self.conn.close)

    run_ok = HistoryCliTests.run_ok
    hist = HistoryCliTests.hist

    def real_columns(self, name):
        return {row[1] for row in self.conn.execute(f"PRAGMA table_info({name})")}


class TrustedColumnsTests(TrustedColumnsTestsFixture):
    """Tests over TrustedColumnsTestsFixture's fixture."""

    def test_every_real_column_is_classified(self):
        """The drift guard the plan asks for: an UNCLASSIFIED new column fails
        here, rather than silently going out merely-wrapped. Default-deny keeps
        it safe at runtime either way -- this test is about whether anyone
        LOOKED, which safety alone cannot tell you."""
        for table in ("events", "session_notes", "contacts", "rules",
                      "sender_stats"):
            with self.subTest(table=table):
                classified = (history.TRUSTED_COLUMNS[table]
                              | history.HANDLE_COLUMNS.get(table, set())
                              | history.UNTRUSTED_COLUMNS[table])
                real = self.real_columns(table)
                self.assertEqual(real - classified, set(),
                                 f"{table}: unclassified column(s)")
                self.assertEqual(classified - real, set(),
                                 f"{table}: classified column(s) that no longer exist")

    def test_sender_stats_columns_are_enumerated_not_described(self):
        # The view gained `holds` and lost `skips` on 2026-08-06. UC §6 said
        # "all counts, rates", which cannot fail a test; this can.
        self.assertIn("holds", history.TRUSTED_COLUMNS["sender_stats"])
        self.assertNotIn("skips", history.TRUSTED_COLUMNS["sender_stats"])

    def test_the_same_column_name_differs_by_table(self):
        # Why the allowlist is keyed per table and not by bare column name.
        self.assertIn("notes", history.UNTRUSTED_COLUMNS["events"])
        self.assertIn("notes", history.UNTRUSTED_COLUMNS["contacts"])
        self.assertIn("account", history.TRUSTED_COLUMNS["events"])

    def test_laundering_columns_are_not_trusted_anywhere(self):
        """The mechanical half of the confinement rule: these are exactly the
        columns a model could use to carry an injection across sessions, so
        `context` re-wraps them on every read."""
        self.assertIn("note", history.UNTRUSTED_COLUMNS["session_notes"])
        for col in ("notes", "subject"):
            self.assertIn(col, history.UNTRUSTED_COLUMNS["events"])

    def test_at_rest_text_carries_no_wrapping_tags(self):
        # Writes are CLEAN; the read path wraps. Self-tagging on write would
        # double-wrap and make the value un-re-readable.
        self.hist("log-note", "--session", "2026-08-06T00:00:00Z",
                  "sender claimed <b>urgent</b> & official")
        (stored,) = self.conn.execute("SELECT note FROM session_notes").fetchone()
        self.assertEqual(stored, "sender claimed <b>urgent</b> & official")
        self.assertNotIn("untrusted-content", stored)


class ContextTrustTests(TrustedColumnsTestsFixture):
    """`$HIST context` is the DB->model door."""

    def run_context(self):
        payload = json.loads(self.hist("context"))
        return payload, payload.pop("_trust")

    def seed(self):
        self.hist("log-note", "--session", "2026-08-06T00:00:00Z",
                  "note with <b>markup</b> & an ampersand")
        self.hist("add-rule", "--id", "evil-senders",
                  "--predicate", "sender ends-with <evil>.com",
                  "--action", "trash", "--origin", "user")
        self.hist("add-contact", "pete@x.example",
                  "--display-name", "<script>Pete</script>",
                  "--relation", "friend")

    def test_manifest_is_complete(self):
        self.seed()
        payload, trust = self.run_context()
        trusted, _handles = history.column_trust(history.CONTEXT_TABLES)
        trusted = trusted | history.CONTEXT_EXTRA_TRUSTED
        undeclared = (untrusted.string_paths(payload) - trusted
                      - set(trust["untrusted_fields"]))
        self.assertEqual(undeclared, set())

    def test_listed_column_emits_bare_unlisted_emits_wrapped(self):
        self.seed()
        payload, trust = self.run_context()
        note = payload["session_notes"][0]
        self.assertEqual(note["session_started_at"], "2026-08-06T00:00:00Z")
        self.assertEqual(note["note"], "note with &lt;b&gt;markup&lt;/b&gt; "
                                      "&amp; an ampersand")
        self.assertIn("session_notes.note", trust["untrusted_fields"])
        self.assertNotIn("session_notes.session_started_at",
                         trust["untrusted_fields"])

    def test_rule_predicate_is_escaped_on_the_way_out(self):
        # UC §3's shared door -- analytics.py `rules` reads the same column and
        # classifies it the same way, from its own allowlist.
        self.seed()
        payload, trust = self.run_context()
        rule = payload["active_rules"][0]
        self.assertNotIn("<evil>", rule["predicate"])
        self.assertIn("&lt;evil&gt;", rule["predicate"])
        self.assertIn("active_rules.predicate", trust["untrusted_fields"])
        self.assertNotIn("active_rules.origin", trust["untrusted_fields"])

    def test_display_name_is_untrusted_but_addr_round_trips(self):
        # invariant 10: identity matches on the real address, never the name a
        # sender chose. And `addr` is the lookup key --senders takes.
        self.seed()
        payload, trust = self.run_context()
        contact = payload["contacts"][0]
        self.assertNotIn("<script>", contact["display_name"])
        self.assertEqual(contact["addr"], "pete@x.example")
        for path in ("contacts.display_name", "contacts.addr"):
            self.assertIn(path, trust["untrusted_fields"])

    def test_a_new_column_is_wrapped_without_any_code_change(self):
        """`context` uses SELECT *, which is the feature: default-deny sees an
        unknown column and classifies it untrusted with no edit required."""
        self.conn.execute("ALTER TABLE contacts ADD COLUMN nickname TEXT")
        self.conn.execute("INSERT INTO contacts (addr, nickname) VALUES (?, ?)",
                          ("x@y.example", "<b>nick</b>"))
        self.conn.commit()
        payload, trust = self.run_context()
        contact = next(c for c in payload["contacts"] if c["addr"] == "x@y.example")
        self.assertEqual(contact["nickname"], "&lt;b&gt;nick&lt;/b&gt;")
        self.assertIn("contacts.nickname", trust["untrusted_fields"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
