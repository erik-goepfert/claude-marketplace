#!/usr/bin/env python3
"""Tests for roquery.py — the shared read-only query boundary (Phase 11.1).

These tests ARE the boundary. There is no SQL parsing to unit-test, so every
guarantee is asserted by probing the real engine: a claim like "it cannot ATTACH"
is only true because this file demonstrates it. That is also why the escape
probes assert on OUTCOME (no file created, no row changed) and not merely on an
error message."""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import roquery  # noqa: E402


class RoQueryBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, txt TEXT)")
        c.executemany("INSERT INTO t (txt) VALUES (?)",
                      [(f"row{i}",) for i in range(10)])
        c.commit()
        c.close()

    def tearDown(self):
        self.tmp.cleanup()

    def q(self, sql, limit=roquery.ROW_CAP):
        conn = roquery.connect(self.db)
        try:
            return roquery.run(conn, sql, limit)
        finally:
            conn.close()

    def rowcount(self):
        c = sqlite3.connect(self.db)
        try:
            return c.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        finally:
            c.close()

    # -- layer 1 + 2: cannot write ------------------------------------------

    def test_reads_work(self):
        rows, truncated = self.q("SELECT txt FROM t ORDER BY id LIMIT 3")
        self.assertEqual([r["txt"] for r in rows], ["row0", "row1", "row2"])
        self.assertFalse(truncated)

    def test_writes_are_refused_and_change_nothing(self):
        for sql in ("UPDATE t SET txt='x'",
                    "DELETE FROM t",
                    "INSERT INTO t (txt) VALUES ('x')",
                    "DROP TABLE t",
                    "CREATE TABLE u (a INTEGER)",
                    "ALTER TABLE t ADD COLUMN c TEXT",
                    # A CTE wrapping DML: the shape a regex blacklist misses.
                    "WITH x AS (SELECT 1) DELETE FROM t"):
            with self.subTest(sql=sql):
                with self.assertRaises(roquery.QueryError):
                    self.q(sql)
        self.assertEqual(self.rowcount(), 10)

    def test_state_changing_pragmas_are_refused(self):
        for sql in ("PRAGMA journal_mode=WAL", "PRAGMA user_version=42"):
            with self.subTest(sql=sql):
                with self.assertRaises(roquery.QueryError):
                    self.q(sql)

    # -- layer 3: cannot ATTACH (the one that actually needed closing) -------

    def test_attach_to_a_writable_file_is_refused_and_creates_nothing(self):
        """Verified 2026-08-07 that WITHOUT setlimit this SUCCEEDS -- a
        read-only connection can attach a rwc database and write there, because
        the attached database carries its own flags. Asserting the absence of
        the file is the real test; the error message is incidental."""
        target = os.path.join(self.tmp.name, "evil.db")
        with self.assertRaises(roquery.QueryError):
            self.q(f"ATTACH DATABASE 'file:{target}?mode=rwc' AS e")
        self.assertFalse(os.path.exists(target))

    # -- driver properties --------------------------------------------------

    def test_stacked_statements_are_refused(self):
        with self.assertRaises(roquery.QueryError):
            self.q("SELECT 1; DROP TABLE t")
        self.assertEqual(self.rowcount(), 10)

    def test_no_filesystem_functions_exist(self):
        """readfile/writefile are sqlite CLI extensions, not library builtins,
        so the query language cannot reach the filesystem."""
        for sql in ("SELECT readfile('/etc/passwd')",
                    "SELECT writefile('/tmp/x', 'y')"):
            with self.subTest(sql=sql):
                with self.assertRaises(roquery.QueryError):
                    self.q(sql)

    # -- layer 4: runaway queries -------------------------------------------

    def test_runaway_query_is_interrupted(self):
        with self.assertRaises(roquery.QueryError) as ctx:
            self.q("WITH RECURSIVE r(i) AS (SELECT 1 UNION ALL "
                   "SELECT i+1 FROM r) SELECT COUNT(*) FROM r")
        self.assertIn("interrupt", str(ctx.exception).lower())

    # -- REGEXP -------------------------------------------------------------

    def test_regexp_is_registered(self):
        rows, _t = self.q("SELECT txt FROM t WHERE txt REGEXP '^row[12]$' "
                          "ORDER BY id")
        self.assertEqual([r["txt"] for r in rows], ["row1", "row2"])

    def test_regexp_handles_null_without_raising(self):
        rows, _t = self.q("SELECT NULL AS n WHERE NULL REGEXP 'x'")
        self.assertEqual(rows, [])

    def test_bad_regexp_reports_the_real_reason(self):
        """sqlite3 collapses any user-function exception into "user-defined
        function raised exception", so the cause is carried out of band. Without
        that, an unbalanced paren is indistinguishable from a crash."""
        with self.assertRaises(roquery.QueryError) as ctx:
            self.q("SELECT 1 WHERE 'a' REGEXP '('")
        self.assertIn("bad REGEXP pattern", str(ctx.exception))

    def test_glob_and_like_need_no_registration(self):
        rows, _t = self.q("SELECT txt FROM t WHERE txt GLOB 'row?' "
                          "AND txt LIKE 'row%' ORDER BY id LIMIT 2")
        self.assertEqual([r["txt"] for r in rows], ["row0", "row1"])

    # -- row cap ------------------------------------------------------------

    def test_truncation_is_reported_not_silent(self):
        rows, truncated = self.q("SELECT * FROM t", limit=4)
        self.assertEqual(len(rows), 4)
        self.assertTrue(truncated)

    def test_exactly_at_the_limit_is_not_truncated(self):
        rows, truncated = self.q("SELECT * FROM t", limit=10)
        self.assertEqual(len(rows), 10)
        self.assertFalse(truncated)

    # -- trust resolution ---------------------------------------------------

    def test_untrusted_wins_every_tie(self):
        """The same bare name can be trusted in one table and untrusted in
        another; a join has no single table. Over-wrapping is a cosmetic cost,
        under-wrapping is a security failure."""
        trusted, handles = roquery.bare_name_trust(
            {"a": {"shared", "safe"}},
            {"b": {"shared", "key"}},
            {"c": {"shared"}})
        self.assertNotIn("shared", trusted)
        self.assertNotIn("shared", handles)
        self.assertIn("safe", trusted)
        self.assertIn("key", handles)

    def test_handles_beat_trusted_but_lose_to_untrusted(self):
        trusted, handles = roquery.bare_name_trust(
            {"a": {"both"}}, {"b": {"both"}}, {"c": set()})
        self.assertIn("both", handles)
        self.assertNotIn("both", trusted)

    def test_payload_declares_unknown_columns_as_untrusted(self):
        out = roquery.payload([{"mystery": "value"}], False, 10,
                              {"known"}, set())
        self.assertIn("rows.mystery", out["_trust"]["untrusted_fields"])

    def test_payload_hint_only_on_truncation(self):
        self.assertNotIn("hint", roquery.payload([], False, 10, set(), set()))
        self.assertIn("hint", roquery.payload([{"a": "b"}], True, 1, set(),
                                              set()))


if __name__ == "__main__":
    unittest.main(verbosity=1)
