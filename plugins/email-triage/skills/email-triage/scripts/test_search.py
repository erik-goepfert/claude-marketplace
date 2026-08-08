#!/usr/bin/env python3
"""Tests for search.py — one search across all three stores (Phase 11.6).

Synthetic fixtures only: a temp Maildir, a real index.db built by indexer, and a
real history.db built by init_history_db.py. Never touches live data."""

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import indexer  # noqa: E402
import search  # noqa: E402


class SearchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.maildir = self.data / "mail"
        for sub in ("cur", "new", "tmp"):
            (self.maildir / "acct" / "INBOX" / sub).mkdir(parents=True)
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "init_history_db.py"),
             "--data-dir", str(self.data)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def tearDown(self):
        self._tmp.cleanup()

    # -- fixture helpers ----------------------------------------------------

    def put(self, key, body="hello", subject="s", frm="a@example.com",
            to="me@example.com"):
        m = EmailMessage()
        m["From"] = frm
        m["To"] = to
        m["Subject"] = subject
        m["Date"] = "Mon, 4 Aug 2026 09:00:00 +0000"
        m["Message-ID"] = f"<{key}@x>"
        m.set_content(body)
        (self.maildir / "acct" / "INBOX" / "cur" / f"{key}:2,S").write_bytes(
            m.as_bytes())

    def do_index(self, no_bodies=False):
        import argparse
        ns = argparse.Namespace(
            maildir=self.maildir, db=self.data / "index.db", account=None,
            config=None, suggest_events=False, session=None,
            no_bodies=no_bodies)
        with contextlib.redirect_stdout(io.StringIO()):
            indexer.cmd_index(ns)

    def hist(self, sql, params=()):
        conn = sqlite3.connect(self.data / "history.db")
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def run_search(self, term, limit=20):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = search.main(["--term", term, "--data-dir", str(self.data),
                                "--limit", str(limit)])
        self.assertEqual(code, 0)
        return json.loads(buf.getvalue())

    # -- the three stores ---------------------------------------------------

    def test_envelope_hit_on_subject_from_and_to(self):
        self.put("s1", subject="About the sprinkler")
        self.put("s2", frm="sprinkler-co@example.com")
        self.put("s3", to="Sprinkler Co <hello@example.com>")
        self.put("s4", subject="unrelated", frm="x@example.com")
        self.do_index()
        out = self.run_search("sprinkler")
        self.assertEqual(out["counts"]["envelopes"], 3)

    def test_body_hit_comes_from_the_fts_index(self):
        self.put("b1", body="Raimund mentioned the sprinkler contractor.",
                 subject="unrelated")
        self.do_index()
        out = self.run_search("sprinkler")
        self.assertEqual(out["counts"]["envelopes"], 0)
        self.assertEqual([h["message_id"] for h in out["bodies"]], ["b1@x"])

    def test_history_hits_across_all_four_tables(self):
        self.hist("INSERT INTO session_notes (session_started_at, written_at, "
                  "note) VALUES ('2026-08-01T00:00:00Z', "
                  "'2026-08-01T00:00:00Z', 'the sprinkler thread is open')")
        self.hist("INSERT INTO rules (predicate, action, created_at, active, "
                  "origin) VALUES ('sender is sprinkler-co', 'archive', "
                  "'2026-08-01T00:00:00Z', 1, 'user')")
        self.hist("INSERT INTO contacts (addr, display_name, relation) VALUES "
                  "('bob@example.com', 'Bob', 'the sprinkler guy')")
        self.hist("INSERT INTO events (session_started_at, occurred_at, "
                  "message_id, sender_addr, subject, proposed_action, "
                  "final_action, decided_by) VALUES "
                  "('2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z', '<e1@x>', "
                  "'quotes@example.com', 'sprinkler quote', 'archive', "
                  "'archive', 'manual')")
        out = self.run_search("sprinkler")
        for table in ("session_notes", "rules", "contacts", "events"):
            with self.subTest(table=table):
                self.assertEqual(len(out["history"][table]), 1)
        self.assertEqual(out["counts"]["total"], 4)

    def test_every_hit_carries_something_to_go_read_it_with(self):
        self.put("b1", body="sprinkler")
        self.do_index()
        self.hist("INSERT INTO contacts (addr, relation) VALUES "
                  "('c@example.com', 'sprinkler')")
        out = self.run_search("sprinkler")
        self.assertIn("message_id", out["bodies"][0])
        self.assertIn("addr", out["history"]["contacts"][0])

    # -- literal matching ---------------------------------------------------

    def test_term_is_literal_not_a_pattern(self):
        """A term is a phrase, so regex and LIKE metacharacters in it are
        characters. `LIKE` was the obvious alternative and is worse: a term
        containing % would silently match everything."""
        self.put("lit", subject="version 1.2.3 released")
        self.put("other", subject="version 1x2x3 released")
        self.do_index()
        out = self.run_search("1.2.3")
        self.assertEqual(out["counts"]["envelopes"], 1)
        self.assertEqual(out["envelopes"][0]["subject"], "version 1.2.3 released")

    def test_percent_in_a_term_does_not_wildcard(self):
        self.put("pct", subject="50% off")
        self.put("nope", subject="nothing to see")
        self.do_index()
        out = self.run_search("%")
        self.assertEqual(out["counts"]["envelopes"], 1)

    def test_matching_is_case_insensitive(self):
        self.put("c1", subject="SPRINKLER repair")
        self.do_index()
        self.assertEqual(self.run_search("sprinkler")["counts"]["envelopes"], 1)

    def test_quote_in_a_term_does_not_break_the_fts_query(self):
        """FTS5 escapes a double quote by doubling it. Without that, a term
        containing one is a syntax error rather than a search."""
        self.put("q1", body='he said "sprinkler" twice')
        self.do_index()
        out = self.run_search('"sprinkler"')
        self.assertEqual(out["unavailable"], [])
        self.assertEqual(len(out["bodies"]), 1)

    def test_asterisk_in_a_term_is_not_a_prefix_operator(self):
        self.put("a1", body="a literal sprinkler mention")
        self.do_index()
        out = self.run_search("sprink*")
        self.assertEqual(out["unavailable"], [])
        self.assertEqual(len(out["bodies"]), 0)   # $IDX search is for prefixes

    # -- honest reporting ---------------------------------------------------

    def test_absent_body_index_is_reported_not_a_silent_zero(self):
        """"no body hits" and "body search does not exist yet" must not look the
        same -- that is the failure mode the whole item exists to avoid."""
        self.put("n1", body="sprinkler")
        self.do_index(no_bodies=True)
        conn = sqlite3.connect(self.data / "index.db")
        conn.execute("DROP TABLE body_fts")
        conn.commit()
        conn.close()
        out = self.run_search("sprinkler")
        self.assertEqual(out["bodies"], [])
        self.assertTrue(any("backfill-bodies" in u for u in out["unavailable"]))

    def test_missing_databases_are_reported(self):
        empty = Path(self._tmp.name) / "empty"
        empty.mkdir()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            search.main(["--term", "x", "--data-dir", str(empty)])
        out = json.loads(buf.getvalue())
        self.assertEqual(len(out["unavailable"]), 2)
        self.assertEqual(out["counts"]["total"], 0)

    def test_limit_applies_per_section(self):
        for i in range(5):
            self.put(f"m{i}", subject="sprinkler")
        self.do_index()
        out = self.run_search("sprinkler", limit=2)
        self.assertEqual(len(out["envelopes"]), 2)

    # -- trust --------------------------------------------------------------

    def test_sender_prose_is_escaped_and_declared_in_every_section(self):
        # The subject must MATCH as well as contain markup -- otherwise the
        # envelopes section is empty and there is no path to assert on.
        self.put("t1", body="sprinkler",
                 subject="Re: sprinkler <b>bold</b> & co")
        self.do_index()
        self.hist("INSERT INTO session_notes (session_started_at, written_at, "
                  "note) VALUES ('2026-08-01T00:00:00Z', "
                  "'2026-08-01T00:00:00Z', 'sprinkler <script>')")
        out = self.run_search("sprinkler")
        declared = set(out["_trust"]["untrusted_fields"])
        self.assertIn("envelopes.subject", declared)
        self.assertIn("bodies.subject", declared)
        self.assertIn("history.session_notes.note", declared)
        self.assertIn("&lt;b&gt;", out["bodies"][0]["subject"])
        self.assertIn("&lt;script&gt;", out["history"]["session_notes"][0]["note"])

    def test_handles_and_our_own_fields_stay_trusted(self):
        self.put("t2", body="sprinkler")
        self.do_index()
        out = self.run_search("sprinkler")
        declared = set(out["_trust"]["untrusted_fields"])
        self.assertIn("bodies.message_id", declared)      # handle: unescaped
        self.assertEqual(out["bodies"][0]["message_id"], "t2@x")
        self.assertNotIn("term", declared)
        self.assertNotIn("counts.total", declared)
        self.assertNotIn("bodies.rank", declared)

    def test_payload_is_fully_classified(self):
        import untrusted
        self.put("t3", body="sprinkler", subject="s")
        self.do_index()
        self.hist("INSERT INTO contacts (addr, relation) VALUES "
                  "('c@example.com', 'sprinkler')")
        out = self.run_search("sprinkler")
        paths = untrusted.string_paths(out)
        trust = out["_trust"]
        undeclared = (paths - {p for p in paths if p.startswith("_trust")}
                      - set(trust["untrusted_fields"]))
        # Whatever is left must be something we deliberately called trusted.
        for path in undeclared:
            self.assertTrue(
                path == "term" or path.startswith("counts.")
                or path == "unavailable"
                or path.split(".")[-1] in {
                    "account", "folder", "folder_tier", "date_utc", "rank",
                    "session_started_at", "written_at", "occurred_at",
                    "final_action", "created_at", "id"},
                f"{path} is neither declared untrusted nor knowingly trusted")


if __name__ == "__main__":
    unittest.main(verbosity=1)
