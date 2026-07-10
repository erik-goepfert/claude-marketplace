#!/usr/bin/env python3
"""Tests for threads.py -- thread assembly + state derivation over index.db.

Builds a synthetic index.db per test by reusing indexer.py's schema
(indexer.init_db) and inserting envelope rows directly -- no Maildir, no real
data dir. Never touches ~/.local/share/email-triage/.

Tests call threads.py's real CLI entry points (cmd_list / cmd_show, built via
the same build_parser() main() uses) and capture stdout, rather than
reimplementing a parallel assertion path against internal functions -- this
exercises argument parsing and the exit-1 error path along with the
assembly/state logic.
"""

import contextlib
import datetime
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import indexer  # noqa: E402
import threads  # noqa: E402


def iso(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def days_ago(n: int) -> str:
    return iso(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=n))


class ThreadsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "index.db"
        self.conn = indexer.init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        self._tmpdir.cleanup()

    def insert(self, *, message_id, folder, folder_tier, account,
               in_reply_to=None, references_chain=None, subject=None,
               from_addr=None, to_addr=None, date_utc=None, flags=None,
               is_flagged=0, is_answered=0, maildir_key=None):
        maildir_key = maildir_key or message_id
        self.conn.execute(
            """INSERT INTO envelopes
               (message_id, folder, maildir_key, maildir_file, in_reply_to,
                references_chain, subject, from_addr, to_addr, date_utc,
                flags, is_flagged, is_answered, account, indexed_at, folder_tier)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message_id, folder, maildir_key, maildir_key + ":2,",
                in_reply_to, json.dumps(references_chain or []), subject,
                from_addr, to_addr, date_utc, json.dumps(flags or []),
                is_flagged, is_answered, account, "2026-01-01T00:00:00Z",
                folder_tier,
            ),
        )
        self.conn.commit()

    # -- helpers to drive the real CLI entry points --

    def run_list(self, **kwargs):
        argv = ["list", "--db", str(self.db_path)]
        for a in kwargs.pop("account", []) or []:
            argv += ["--account", a]
        for t in kwargs.pop("tier", []) or []:
            argv += ["--tier", t]
        assert not kwargs
        parser = threads.build_parser()
        args = parser.parse_args(argv)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            threads.cmd_list(args)
        return json.loads(buf.getvalue())

    def run_show(self, account, message_id):
        parser = threads.build_parser()
        args = parser.parse_args(["show", "--db", str(self.db_path), "--account", account, "--message-id", message_id])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            threads.cmd_show(args)
        return json.loads(buf.getvalue())

    def run_show_expect_exit1(self, account, message_id):
        parser = threads.build_parser()
        args = parser.parse_args(["show", "--db", str(self.db_path), "--account", account, "--message-id", message_id])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                threads.cmd_show(args)
        self.assertEqual(cm.exception.code, 1)
        return err.getvalue()

    # (a) 3-message In-Reply-To chain assembles as one thread.
    def test_in_reply_to_chain_assembles_one_thread(self):
        self.insert(message_id="A", folder="INBOX", folder_tier="inbox", account="ic",
                    subject="Fence quote", from_addr="me@x.com", to_addr="them@y.com",
                    date_utc=days_ago(3))
        self.insert(message_id="B", folder="Sent Messages",
                    folder_tier="sent", account="ic", in_reply_to="A", references_chain=["A"],
                    subject="Re: Fence quote", from_addr="me@x.com", to_addr="them@y.com",
                    date_utc=days_ago(2), is_answered=1)
        self.insert(message_id="C", folder="INBOX", folder_tier="inbox", account="ic",
                    in_reply_to="B", references_chain=["A", "B"], subject="Re: Fence quote",
                    from_addr="them@y.com", to_addr="me@x.com", date_utc=days_ago(1))

        result = self.run_list(tier=["inbox", "active", "sent"])
        self.assertEqual(len(result), 1)
        thread = result[0]
        self.assertEqual(thread["member_count"], 3)
        self.assertEqual(thread["root_message_id"], "A")
        self.assertEqual({m["message_id"] for m in thread["members"]}, {"A", "B", "C"})

    # (b) A References-only link (gap in the parent walk) still joins.
    def test_references_only_link_joins_despite_missing_parent(self):
        self.insert(message_id="A", folder="INBOX", folder_tier="inbox", account="ic",
                    subject="Taxes 2025", from_addr="cpa@firm.com", to_addr="me@x.com",
                    date_utc=days_ago(10))
        # B is deliberately never indexed (dropped/never synced).
        self.insert(message_id="C", folder="INBOX", folder_tier="inbox", account="ic",
                    in_reply_to="B", references_chain=["A", "B"], subject="Re: Taxes 2025",
                    from_addr="me@x.com", to_addr="cpa@firm.com", date_utc=days_ago(1))

        result = self.run_list(tier=["inbox"])
        self.assertEqual(len(result), 1)
        thread = result[0]
        self.assertEqual(thread["member_count"], 2)
        self.assertEqual(thread["root_message_id"], "A")

    # (c) subject fallback joins ONLY with participant overlap.
    def test_subject_fallback_positive_with_participant_overlap(self):
        self.insert(message_id="X1", folder="INBOX", folder_tier="inbox", account="ic",
                    subject="Invoice #42", from_addr="billing@vendor.com", to_addr="me@x.com",
                    date_utc=days_ago(5))
        self.insert(message_id="X2", folder="Active", folder_tier="active", account="ic",
                    subject="Re: Invoice #42", from_addr="me@x.com", to_addr="billing@vendor.com",
                    date_utc=days_ago(1))
        result = self.run_list(tier=["inbox", "active"])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["member_count"], 2)

    def test_subject_fallback_negative_without_participant_overlap(self):
        self.insert(message_id="Y1", folder="INBOX", folder_tier="inbox", account="ic",
                    subject="Invoice #99", from_addr="billing@vendorA.com", to_addr="me@x.com",
                    date_utc=days_ago(5))
        self.insert(message_id="Y2", folder="INBOX", folder_tier="inbox", account="ic",
                    subject="Re: Invoice #99", from_addr="billing@vendorB.com", to_addr="someone-else@z.com",
                    date_utc=days_ago(1))
        result = self.run_list(tier=["inbox"])
        self.assertEqual(len(result), 2)
        self.assertEqual({t["member_count"] for t in result}, {1})

    # (d) flagged mid-thread message => needs-reply even when latest is unflagged.
    def test_flagged_mid_thread_message_forces_needs_reply(self):
        self.insert(message_id="A", folder="INBOX", folder_tier="inbox", account="ic",
                    subject="Contractor question", from_addr="them@y.com", to_addr="me@x.com",
                    date_utc=days_ago(5), is_flagged=1)
        self.insert(message_id="B", folder="INBOX", folder_tier="inbox", account="ic",
                    in_reply_to="A", references_chain=["A"], subject="Re: Contractor question",
                    from_addr="them@y.com", to_addr="me@x.com", date_utc=days_ago(1), is_flagged=0)
        result = self.run_list(tier=["inbox"])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["state"], "needs-reply")

    # (e) latest-is-sent unflagged => waiting, inactive_days from the sent member.
    def test_latest_is_sent_unflagged_gives_waiting(self):
        self.insert(message_id="A", folder="INBOX", folder_tier="inbox", account="ic",
                    subject="Question", from_addr="them@y.com", to_addr="me@x.com",
                    date_utc=days_ago(9))
        self.insert(message_id="B", folder="Sent Messages", folder_tier="sent", account="ic",
                    in_reply_to="A", references_chain=["A"], subject="Re: Question",
                    from_addr="me@x.com", to_addr="them@y.com", date_utc=days_ago(4), is_answered=1)
        result = self.run_list(tier=["inbox", "sent"])
        self.assertEqual(len(result), 1)
        thread = result[0]
        self.assertEqual(thread["state"], "waiting")
        self.assertEqual(thread["inactive_days"], 4)

    # (f) flagged message in Trash does NOT make the thread needs-reply.
    def test_flagged_trash_message_does_not_block(self):
        self.insert(message_id="A", folder="INBOX", folder_tier="inbox", account="ic",
                    subject="Old thread", from_addr="them@y.com", to_addr="me@x.com",
                    date_utc=days_ago(20), is_flagged=0)
        self.insert(message_id="B", folder="Deleted Messages", folder_tier="trash", account="ic",
                    in_reply_to="A", references_chain=["A"], subject="Re: Old thread",
                    from_addr="spammer@y.com", to_addr="me@x.com", date_utc=days_ago(1), is_flagged=1)
        result = self.run_list(tier=["inbox"])
        self.assertEqual(len(result), 1)
        thread = result[0]
        self.assertEqual(thread["member_count"], 2)
        self.assertEqual(thread["state"], "holding")
        # inactive_days measured from A (the latest non-trash/junk member),
        # not from B's more recent date.
        self.assertEqual(thread["inactive_days"], 20)

    # (g) All-Mail duplicate rows don't double members.
    def test_allmail_duplicate_rows_dedup(self):
        self.insert(message_id="A", folder="Active", folder_tier="active", account="gmail",
                    subject="Project update", from_addr="them@y.com", to_addr="me@x.com",
                    date_utc=days_ago(2), maildir_key="A-active")
        self.insert(message_id="A", folder="[Gmail]/All Mail", folder_tier="allmail", account="gmail",
                    subject="Project update", from_addr="them@y.com", to_addr="me@x.com",
                    date_utc=days_ago(2), maildir_key="A-allmail")
        result = self.run_list(account=["gmail"], tier=["active"])
        self.assertEqual(len(result), 1)
        thread = result[0]
        self.assertEqual(thread["member_count"], 1)
        self.assertEqual(thread["members"][0]["folder_tier"], "active")

    # (h) list --tier inbox excludes a fully-archived thread; show still assembles it.
    def test_list_tier_filter_excludes_archived_thread_but_show_finds_it(self):
        self.insert(message_id="A", folder="Archive", folder_tier="archive", account="ic",
                    subject="Closed chapter", from_addr="them@y.com", to_addr="me@x.com",
                    date_utc=days_ago(400))
        self.insert(message_id="B", folder="Archive", folder_tier="archive", account="ic",
                    in_reply_to="A", references_chain=["A"], subject="Re: Closed chapter",
                    from_addr="me@x.com", to_addr="them@y.com", date_utc=days_ago(399))

        result = self.run_list(tier=["inbox"])
        self.assertEqual(result, [])

        # Default tiers (inbox+active) should also exclude it.
        result_default = self.run_list()
        self.assertEqual(result_default, [])

        thread = self.run_show("ic", "A")
        self.assertEqual(thread["member_count"], 2)
        self.assertEqual(thread["root_message_id"], "A")

    def test_show_unknown_message_id_exits_1_with_clear_error(self):
        self.insert(message_id="A", folder="INBOX", folder_tier="inbox", account="ic",
                    subject="Hi", from_addr="them@y.com", to_addr="me@x.com", date_utc=days_ago(1))
        stderr_text = self.run_show_expect_exit1("ic", "does-not-exist")
        self.assertIn("does-not-exist", stderr_text)
        self.assertIn("error", stderr_text.lower())

    # (i) threads never merge across accounts.
    def test_threads_never_span_accounts(self):
        # Same message_id, same subject/participants, but different accounts:
        # must never be treated as one thread.
        for account in ("ic", "gmail"):
            # maildir_key must be distinct per row -- UNIQUE(folder, maildir_key)
            # holds regardless of account, matching indexer.py's real schema
            # (real Maildir keys are unique hashes and never collide across
            # accounts in practice).
            self.insert(message_id="DUP", folder="INBOX", folder_tier="inbox", account=account,
                        subject="Renewal notice", from_addr="billing@service.com", to_addr="me@x.com",
                        date_utc=days_ago(2), maildir_key=f"DUP-{account}")
        result = self.run_list(tier=["inbox"])
        self.assertEqual(len(result), 2)
        accounts_seen = {t["account"] for t in result}
        self.assertEqual(accounts_seen, {"ic", "gmail"})
        for t in result:
            self.assertEqual(t["member_count"], 1)

        # Also true for genuinely linked (In-Reply-To) messages that happen to
        # share a message_id across accounts (shouldn't happen in practice,
        # but the account partition must hold regardless).
        thread_ic = self.run_show("ic", "DUP")
        thread_gmail = self.run_show("gmail", "DUP")
        self.assertEqual(thread_ic["account"], "ic")
        self.assertEqual(thread_gmail["account"], "gmail")
        self.assertEqual(thread_ic["member_count"], 1)
        self.assertEqual(thread_gmail["member_count"], 1)


if __name__ == "__main__":
    unittest.main()
