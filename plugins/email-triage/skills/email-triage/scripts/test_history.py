#!/usr/bin/env python3
"""Tests for history.py's 5.5 additions: add-rule / set-rule / add-contact
(interaction patterns promoted from prose notes into the DB). Runs the CLI
via subprocess against a temp data dir; never touches real data."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).parent


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
        out = self.hist("add-rule",
                        "--predicate", "sender = updates@example.com",
                        "--action", "summarize-then-trash",
                        "--origin", "user")
        self.assertIn("rule 1 added", out)
        rules = self.context()["active_rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["action"], "summarize-then-trash")
        self.assertEqual(rules[0]["origin"], "user")

        self.hist("set-rule", "1", "--active", "0")
        self.assertEqual(self.context()["active_rules"], [])
        self.hist("set-rule", "1", "--active", "1")
        self.assertEqual(len(self.context()["active_rules"]), 1)

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
        (s,) = json.loads(self.hist("context"))["sender_stats"]
        self.assertEqual(s["sender_addr"], "news@example.com")
        self.assertEqual(s["declined_unsubs"], 2)

    def _event(self, sender, final, proposed=None, decided="approved"):
        return self.hist("log-event", "--session", "2026-08-06T00:00:00Z",
                         "--sender", sender, "--proposed", proposed or final,
                         "--final", final, "--decided-by", decided)

    def _stats(self, sender):
        rows = json.loads(self.hist("context"))["sender_stats"]
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
