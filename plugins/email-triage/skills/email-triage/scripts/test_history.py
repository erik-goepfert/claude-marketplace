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
                    "--proposed", "trash", "--final", "skip",
                    "--decided-by", "override"]
            if notes:
                args += ["--notes", notes]
            self.hist(*args)
        (s,) = json.loads(self.hist("context"))["sender_stats"]
        self.assertEqual(s["sender_addr"], "news@example.com")
        self.assertEqual(s["declined_unsubs"], 2)

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
