#!/usr/bin/env python3
"""Self-test for analytics.py (design ref: email-skill-design.md §9
"Analytics access"; implementation-plan.md Phase 3).

DuckDB is an optional dependency (analytics.py's whole reason for existing
is that it isn't required by core triage), so every test here is skipped
-- not failed -- when `duckdb` isn't importable.

Builds a synthetic history.db (schema via init_history_db.py's SCHEMA) and
a minimal index.db (schema via indexer.py's init_db) in a tempfile data
dir, never touching the real ~/.local/share/email-triage files. The
synthetic data covers:
  - 2 categories (marketing, passive-update), 4 senders, 2 sessions
  - a deliberate-deferral skip (decided_by=override, final=skip) and an
    override that changed the outcome (proposed=archive, final=reply)
  - an `activate` action (2.7 vocabulary)
  - a confirmed unsubscribe (events.unsubscribed=1)
  - a rule with events (via rule_id) and a rule with none (zero-event row)
  - envelopes making exactly one chronic-trash sender a `candidates` hit
    (recent arrival) and a second, otherwise-identical sender a near-miss
    (arrival outside the recency window)
"""

import datetime
import importlib.util
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

try:
    import duckdb  # noqa: F401

    HAVE_DUCKDB = True
except ImportError:
    HAVE_DUCKDB = False

_SCRIPTS_DIR = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


init_history_db = _load("init_history_db")
indexer = _load("indexer")
# analytics.py itself does `import duckdb` at module scope and calls
# sys.exit(1) if that fails -- only load it when duckdb is actually
# importable, so a duckdb-less environment skips cleanly instead of the
# whole test module aborting on import.
analytics = _load("analytics") if HAVE_DUCKDB else None


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def days_ago_iso(days: int) -> str:
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


EVENT_COLUMNS = (
    "session_started_at", "occurred_at", "account", "thread_root_id", "message_id",
    "sender_addr", "subject", "thread_state", "category", "inactive_days",
    "proposed_action", "final_action", "from_folder", "to_folder",
    "decided_by", "rule_id", "notes", "unsubscribed",
)


def insert_event(conn: sqlite3.Connection, **fields) -> int:
    fields.setdefault("account", "testacct")
    fields.setdefault("thread_root_id", None)
    fields.setdefault("message_id", None)
    fields.setdefault("subject", "test subject")
    fields.setdefault("thread_state", "holding")
    fields.setdefault("inactive_days", 1)
    fields.setdefault("from_folder", "INBOX")
    fields.setdefault("to_folder", None)
    fields.setdefault("rule_id", None)
    fields.setdefault("notes", None)
    fields.setdefault("unsubscribed", 0)
    row = tuple(fields[c] for c in EVENT_COLUMNS)
    cur = conn.execute(
        f"INSERT INTO events ({', '.join(EVENT_COLUMNS)}) VALUES ({', '.join('?' * len(EVENT_COLUMNS))})",
        row,
    )
    return cur.lastrowid


def insert_envelope(conn: sqlite3.Connection, *, key: str, from_addr: str, date_utc: str,
                     folder_tier: str = "inbox", folder: str = "INBOX", account: str = "testacct") -> None:
    conn.execute(
        "INSERT INTO envelopes (message_id, folder, maildir_key, maildir_file, from_addr, "
        "date_utc, flags, is_flagged, is_answered, account, indexed_at, folder_tier) "
        "VALUES (?, ?, ?, ?, ?, ?, '[]', 0, 0, ?, ?, ?)",
        (f"<{key}@test>", folder, key, f"{key}:2,S", from_addr, date_utc, account, utc_now_iso(), folder_tier),
    )


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not installed -- analytics.py is an optional Phase 3 helper")
class AnalyticsTest(unittest.TestCase):
    S1 = "2026-01-01T00:00:00Z"
    S2 = "2026-01-08T00:00:00Z"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="email-triage-analytics-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data_dir = self.tmp

        # --- history.db ---
        hconn = sqlite3.connect(self.data_dir / "history.db")
        hconn.executescript(init_history_db.SCHEMA)

        # trashy@spam.com (marketing): 5 decisive trash events + 1 deferred
        # skip. Will be the `candidates` hit (recent envelope arrival).
        insert_event(hconn, session_started_at=self.S1, occurred_at="2026-01-01T00:05:00Z",
                     sender_addr="trashy@spam.com", category="marketing",
                     proposed_action="trash", final_action="trash", decided_by="approved")
        insert_event(hconn, session_started_at=self.S1, occurred_at="2026-01-01T00:10:00Z",
                     sender_addr="trashy@spam.com", category="marketing",
                     proposed_action="trash", final_action="trash", decided_by="approved")
        insert_event(hconn, session_started_at=self.S1, occurred_at="2026-01-01T00:15:00Z",
                     sender_addr="trashy@spam.com", category="marketing",
                     proposed_action="trash", final_action="trash", decided_by="approved",
                     unsubscribed=1)
        insert_event(hconn, session_started_at=self.S2, occurred_at="2026-01-08T00:05:00Z",
                     sender_addr="trashy@spam.com", category="marketing",
                     proposed_action="trash", final_action="trash", decided_by="approved")
        insert_event(hconn, session_started_at=self.S2, occurred_at="2026-01-08T00:10:00Z",
                     sender_addr="trashy@spam.com", category="marketing",
                     proposed_action="trash", final_action="trash", decided_by="approved")
        insert_event(hconn, session_started_at=self.S2, occurred_at="2026-01-08T00:12:00Z",
                     sender_addr="trashy@spam.com", category="marketing",
                     proposed_action="trash", final_action="skip", decided_by="override")

        # nearmiss@spam.com (marketing): same chronic-trash shape as trashy,
        # but its only envelope arrival is far outside the recency window --
        # must NOT show up in `candidates`.
        for i in range(5):
            insert_event(hconn, session_started_at=self.S1, occurred_at=f"2026-01-01T00:{i:02d}:00Z",
                         sender_addr="nearmiss@spam.com", category="marketing",
                         proposed_action="trash", final_action="trash", decided_by="approved")

        # friend@example.com (passive-update): override that changed the
        # outcome, an `activate`, and a deliberate-deferral skip.
        f1 = insert_event(hconn, session_started_at=self.S2, occurred_at="2026-01-08T00:00:00Z",
                          sender_addr="friend@example.com", category="passive-update",
                          proposed_action="archive", final_action="archive", decided_by="approved")
        f2 = insert_event(hconn, session_started_at=self.S2, occurred_at="2026-01-08T00:20:00Z",
                          sender_addr="friend@example.com", category="passive-update",
                          proposed_action="archive", final_action="reply", decided_by="override")
        insert_event(hconn, session_started_at=self.S2, occurred_at="2026-01-08T00:40:00Z",
                     sender_addr="friend@example.com", category="passive-update",
                     proposed_action="archive", final_action="activate", decided_by="approved")
        insert_event(hconn, session_started_at=self.S2, occurred_at="2026-01-08T02:00:00Z",
                     sender_addr="friend@example.com", category="passive-update",
                     proposed_action="reply", final_action="skip", decided_by="override")

        # rare@rareonce.com: a single event, only used to prove the
        # `domains --min-events` filter actually excludes low-volume domains.
        insert_event(hconn, session_started_at=self.S1, occurred_at="2026-01-01T00:20:00Z",
                     sender_addr="rare@rareonce.com", category="passive-update",
                     proposed_action="archive", final_action="archive", decided_by="approved")

        # ruletest@covered.com: shaped exactly like a `candidates` hit
        # (chronic trash, recent envelope arrival) EXCEPT an active rule's
        # predicate already names it -- must be excluded, proving the
        # "no active rule mentions the sender" clause actually runs rather
        # than every candidate happening to lack a rule by coincidence.
        for i in range(5):
            insert_event(hconn, session_started_at=self.S1, occurred_at=f"2026-01-01T01:{i:02d}:00Z",
                         sender_addr="ruletest@covered.com", category="marketing",
                         proposed_action="trash", final_action="trash", decided_by="approved")

        # rules: one that drove events (via rule_id -- ruleC), one that
        # never fired (zero-event row must still appear, per the LEFT JOIN
        # contract), one that covers ruletest@covered.com.
        hconn.execute(
            "INSERT INTO rules (created_at, active, predicate, action, origin) VALUES (?, 1, ?, 'archive', 'claude-proposed')",
            (self.S1, "sender = friend@example.com"),
        )
        rule_c_id = hconn.execute("SELECT id FROM rules WHERE predicate = 'sender = friend@example.com'").fetchone()[0]
        hconn.execute(
            "INSERT INTO rules (created_at, active, predicate, action, origin) VALUES (?, 1, ?, 'trash', 'user')",
            (self.S1, "sender = neverused@example.com"),
        )
        hconn.execute(
            "INSERT INTO rules (created_at, active, predicate, action, origin) VALUES (?, 1, ?, 'trash', 'user')",
            (self.S1, "sender = ruletest@covered.com"),
        )
        hconn.execute("UPDATE events SET rule_id = ? WHERE id IN (?, ?)", (rule_c_id, f1, f2))

        hconn.execute(
            "INSERT INTO session_notes (session_started_at, written_at, note) VALUES (?, ?, ?)",
            (self.S1, self.S1, "S1 note"),
        )
        hconn.execute(
            "INSERT INTO session_notes (session_started_at, written_at, note) VALUES (?, ?, ?)",
            (self.S2, self.S2, "S2 note 1"),
        )
        hconn.execute(
            "INSERT INTO session_notes (session_started_at, written_at, note) VALUES (?, ?, ?)",
            (self.S2, self.S2, "S2 note 2"),
        )

        # spam-noise: every event skipped, zero decisive -- proves
        # categories rates come back null (no div-by-zero) rather than
        # erroring, since this category never has a decisive action.
        insert_event(hconn, session_started_at=self.S1, occurred_at=self.S1,
                     sender_addr="x@x.com", category="spam-noise",
                     proposed_action="trash", final_action="skip", decided_by="override")

        hconn.commit()
        hconn.close()

        # --- index.db ---
        iconn = indexer.init_db(self.data_dir / "index.db")
        insert_envelope(iconn, key="hit", from_addr="Trashy@Spam.com", date_utc=days_ago_iso(5))
        insert_envelope(iconn, key="miss", from_addr="nearmiss@spam.com", date_utc=days_ago_iso(400))
        insert_envelope(iconn, key="covered", from_addr="ruletest@covered.com", date_utc=days_ago_iso(5))
        iconn.commit()
        iconn.close()

        self.con = analytics.connect(self.data_dir, need_index=True)

    def tearDown(self):
        self.con.close()

    def ns(self, **kwargs):
        import argparse
        return argparse.Namespace(**kwargs)

    def by_key(self, items, key, value):
        for item in items:
            if item[key] == value:
                return item
        self.fail(f"no item with {key}={value!r} in {items}")

    # -- categories: rates math, incl. the unsubscribe-rate metric --------

    def test_categories_rates_math(self):
        result = analytics.cmd_categories(self.con, self.ns())
        marketing = self.by_key(result["categories"], "category", "marketing")
        self.assertEqual(marketing["events"], 16)  # 6 trashy + 5 nearmiss + 5 ruletest
        self.assertEqual(marketing["decisive"], 15)  # the trashy skip excluded
        self.assertAlmostEqual(marketing["action_mix"]["trash_rate"], 1.0)
        self.assertAlmostEqual(marketing["unsubscribed"]["rate"], 1 / 15)
        self.assertEqual(marketing["unsubscribed"]["count"], 1)

        passive = self.by_key(result["categories"], "category", "passive-update")
        self.assertEqual(passive["decisive"], 4)  # friend's archive/reply/activate + rare's archive
        self.assertAlmostEqual(passive["action_mix"]["activate_rate"], 0.25)
        self.assertAlmostEqual(passive["action_mix"]["reply_rate"], 0.25)
        self.assertAlmostEqual(passive["action_mix"]["archive_rate"], 0.5)

    # -- domains: --min-events filter --------------------------------------

    def test_domains_min_events_filter(self):
        default = analytics.cmd_domains(self.con, self.ns(min_events=3, category=None))
        domains = {d["domain"] for d in default["domains"]}
        self.assertIn("spam.com", domains)
        self.assertIn("example.com", domains)
        self.assertNotIn("rareonce.com", domains, "single-event domain must be filtered out at the default threshold")

        loosened = analytics.cmd_domains(self.con, self.ns(min_events=1, category=None))
        domains_loose = {d["domain"] for d in loosened["domains"]}
        self.assertIn("rareonce.com", domains_loose, "--min-events 1 must surface the low-volume domain")

        spam = self.by_key(default["domains"], "domain", "spam.com")
        self.assertEqual(spam["events"], 11)
        self.assertEqual(spam["decisive"], 10)
        self.assertAlmostEqual(spam["trash_rate"], 1.0)

    def test_domains_category_filter(self):
        result = analytics.cmd_domains(self.con, self.ns(min_events=1, category="passive-update"))
        domains = {d["domain"] for d in result["domains"]}
        self.assertEqual(domains, {"example.com", "rareonce.com"})

    # -- candidates: hit included, near-miss excluded -----------------------

    def test_candidates_hit_and_near_miss(self):
        result = analytics.cmd_candidates(self.con, self.ns(min_events=5, days=30))
        senders = {c["sender"] for c in result["candidates"]}
        self.assertIn("trashy@spam.com", senders, "recent chronic-trash sender must surface as a candidate")
        self.assertNotIn("nearmiss@spam.com", senders, "stale-arrival sender must be excluded despite matching event history")

        hit = self.by_key(result["candidates"], "sender", "trashy@spam.com")
        self.assertEqual(hit["decisive"], 5)
        self.assertAlmostEqual(hit["trash_rate"], 1.0)
        self.assertEqual(hit["suggested_predicate"], "sender = trashy@spam.com")

    def test_candidates_respects_existing_rule(self):
        # ruletest@covered.com is shaped exactly like the trashy@spam.com
        # hit (5/5 trash, recent envelope) but an active rule already names
        # it -- must be excluded, proving the "no active rule mentions the
        # sender" clause runs rather than every candidate lacking a rule
        # by coincidence.
        result = analytics.cmd_candidates(self.con, self.ns(min_events=5, days=30))
        senders = {c["sender"] for c in result["candidates"]}
        self.assertNotIn("ruletest@covered.com", senders)
        self.assertIn("trashy@spam.com", senders)  # sanity: exclusion is rule-specific, not global

    # -- rules: zero-event row present, via LEFT JOIN ------------------------

    def test_rules_zero_event_row(self):
        result = analytics.cmd_rules(self.con, self.ns())
        never_used = self.by_key(result["rules"], "predicate", "sender = neverused@example.com")
        self.assertEqual(never_used["events_driven"], 0)
        self.assertIsNone(never_used["override_rate"])
        self.assertIsNone(never_used["last_used"])

        used = self.by_key(result["rules"], "predicate", "sender = friend@example.com")
        self.assertEqual(used["events_driven"], 2)
        self.assertAlmostEqual(used["override_rate"], 0.5)
        self.assertEqual(used["last_used"], "2026-01-08T00:20:00Z")

    # -- sessions: spread computed -------------------------------------------

    def test_sessions_spread(self):
        result = analytics.cmd_sessions(self.con, self.ns(limit=10))
        s1 = self.by_key(result["sessions"], "session_started_at", self.S1)
        self.assertEqual(s1["spread_minutes"], 64)  # 00:00 (nearmiss #0) to 01:04 (ruletest #4)
        self.assertEqual(s1["notes"], 1)

        s2 = self.by_key(result["sessions"], "session_started_at", self.S2)
        self.assertEqual(s2["spread_minutes"], 120)  # 00:00 (friend) to 02:00 (friend)
        self.assertEqual(s2["notes"], 2)
        self.assertEqual(s2["action_counts"]["activate"], 1)
        self.assertEqual(s2["action_counts"]["skip"], 2)  # trashy's deferred skip + friend's deferred skip

    # -- empty/small-data handling: no div-by-zero --------------------------

    def test_empty_category_has_null_rates(self):
        # spam-noise (inserted in setUp with its one event skipped) has
        # zero decisive events -- rates must be null, not a div-by-zero
        # error.
        result = analytics.cmd_categories(self.con, self.ns())
        spam_noise = self.by_key(result["categories"], "category", "spam-noise")
        self.assertEqual(spam_noise["decisive"], 0)
        self.assertIsNone(spam_noise["action_mix"]["trash_rate"])
        self.assertIsNone(spam_noise["unsubscribed"]["rate"])


if __name__ == "__main__":
    unittest.main()
