#!/usr/bin/env python3
"""Tests for batch.py -- the one-call act+log+index+sync batch executor.

Synthetic fixtures only: temp Maildir + real index.db/history.db schemas +
a stub mbsync that logs its argv. Never touches real data or the network."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import batch  # noqa: E402
import indexer  # noqa: E402

CONFIG_TEXT = """\
accounts:
  acct:
    tiers:
      untriaged: INBOX
      active: Active
      ongoing: Areas
      cold: Archive
      sent: Sent
      drafts: Drafts
      trash: Trash
      junk: Junk
"""

MBSYNC_STUB = """#!/bin/sh
echo "$@" >> "$STUBLOG"
if [ -n "$FAIL_ONCE_MARKER" ] && [ ! -f "$FAIL_ONCE_MARKER" ]; then
  touch "$FAIL_ONCE_MARKER"
  echo "read: unexpected EOF" >&2
  exit 1
fi
exit 0
"""


def write_message(folder_path: Path, unique: str, flags, headers: dict):
    for d in ("cur", "new", "tmp"):
        (folder_path / d).mkdir(parents=True, exist_ok=True)
    filename = unique if flags is None else f"{unique}:2,{flags}"
    lines = [f"{k}: {v}" for k, v in headers.items()]
    content = "\r\n".join(lines) + "\r\n\r\nbody\r\n"
    path = folder_path / "cur" / filename
    path.write_bytes(content.encode("utf-8"))
    return path


def msg_headers(mid: str, sender: str, subject: str):
    return {
        "Message-ID": f"<{mid}>",
        "Date": "Mon, 01 Jun 2026 10:00:00 -0700",
        "From": f"Somebody <{sender}>",
        "To": "erik@example.com",
        "Subject": subject,
    }


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="email-triage-batch-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.maildir = self.tmp / "mail"
        self.acct = self.maildir / "acct"
        self.db = self.tmp / "index.db"
        self.data_dir = self.tmp / "data"
        self.data_dir.mkdir()
        self.config = self.tmp / "config.yaml"
        self.config.write_text(CONFIG_TEXT)

        # history.db with the real schema
        subprocess.run(
            [sys.executable,
             str(Path(__file__).parent / "init_history_db.py"),
             "--data-dir", str(self.data_dir)],
            check=True, capture_output=True)

        # Messages: two in INBOX, one filed pair for the remove test.
        write_message(self.acct / "INBOX", "k1", "S",
                      msg_headers("m1@x", "News@Example.com", "Weekly news"))
        write_message(self.acct / "INBOX", "k2", "",
                      msg_headers("m2@x", "tax@example.com", "Tax doc"))
        write_message(self.acct / "Areas/Receipts", "k3", "S",
                      msg_headers("m3@x", "shop@example.com", "Receipt"))
        write_message(self.acct / "Archive", "k3b", "S",
                      msg_headers("m3@x", "shop@example.com", "Receipt"))
        for f in ("Active", "Trash", "Sent", "Drafts", "Junk", "Areas"):
            for d in ("cur", "new", "tmp"):
                (self.acct / f / d).mkdir(parents=True, exist_ok=True)

        # Build the index for real (also exercises config-driven tiers).
        self.reindex()

        # mbsync stub
        self.stublog = self.tmp / "mbsync.log"
        self.stub = self.tmp / "mbsync-stub"
        self.stub.write_text(MBSYNC_STUB)
        self.stub.chmod(0o755)
        os.environ["STUBLOG"] = str(self.stublog)
        os.environ.pop("FAIL_ONCE_MARKER", None)

    def reindex(self):
        accounts = indexer.resolve_accounts(self.maildir, None)
        tier_maps = indexer.load_tier_maps(self.config, accounts)
        conn = indexer.init_db(self.db)
        try:
            report = indexer.run_index(conn, self.maildir, accounts, tier_maps)
            conn.commit()
        finally:
            conn.close()
        return report

    def run_batch(self, items, *extra):
        items_file = self.tmp / "items.json"
        items_file.write_text(json.dumps(items))
        argv = ["run", "--db", str(self.db), "--maildir-root", str(self.maildir),
                "--data-dir", str(self.data_dir), "--config", str(self.config),
                "--mbsyncrc", "/dev/null", "--mbsync", str(self.stub),
                "--session", "2026-07-18T00:00:00Z",
                "--file", str(items_file), *extra]
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = batch.main(argv)
        stdout = out.getvalue()
        return code, (json.loads(stdout) if stdout.strip() else None), err.getvalue()

    def events(self):
        conn = sqlite3.connect(self.data_dir / "history.db")
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id")]
        finally:
            conn.close()

    def test_end_to_end_move_batch(self):
        # An external arrival dropped in before the batch runs must surface
        # as external residue while the batch's own echo is absorbed.
        write_message(self.acct / "INBOX", "k9", "",
                      msg_headers("m9@x", "new@example.com", "External arrival"))
        code, out, _ = self.run_batch(
            [{"account": "acct", "message_id": "m1@x", "from_folder": "INBOX",
              "to_folder": "Trash"},
             {"account": "acct", "message_id": "m2@x", "from_folder": "INBOX",
              "to_folder": "Areas/Taxes", "notes": "tax season"}],
            "--category", "newsletter")
        self.assertEqual(code, 0, out)

        # acted
        self.assertEqual(len(out["acted"]["moved"]), 2)
        self.assertTrue((self.acct / "Trash" / "cur").iterdir())
        self.assertTrue(list((self.acct / "Areas/Taxes" / "cur").iterdir()))

        # events auto-derived
        evs = self.events()
        self.assertEqual(out["events_logged"], 2)
        by_mid = {e["message_id"]: e for e in evs}
        e1 = by_mid["m1@x"]
        self.assertEqual(e1["final_action"], "trash")
        self.assertEqual(e1["proposed_action"], "trash")
        self.assertEqual(e1["sender_addr"], "news@example.com")  # lowered
        self.assertEqual(e1["subject"], "Weekly news")
        self.assertEqual(e1["category"], "newsletter")
        self.assertEqual(e1["decided_by"], "approved")
        e2 = by_mid["m2@x"]
        self.assertEqual(e2["final_action"], "archive")  # ongoing subtree
        self.assertEqual(e2["to_folder"], "Areas/Taxes")
        self.assertEqual(e2["notes"], "tax season")

        # self-echo absorbed; the external arrival is the only residue
        cr = out["change_report"]
        self.assertGreaterEqual(cr["self_echo_absorbed"], 2)
        self.assertEqual(cr["external"]["moved"], [])
        self.assertEqual(cr["external"]["deleted"], [])
        self.assertEqual([e["message_id"] for e in cr["external"]["new"]],
                         ["m9@x"])

        # one box-scoped sync, exactly the touched folders
        self.assertEqual(out["sync"][0]["boxes"], ["Areas/Taxes", "INBOX", "Trash"])
        self.assertEqual(self.stublog.read_text().count("\n"), 1)
        self.assertIn("acct:Areas/Taxes,INBOX,Trash", self.stublog.read_text())

    def test_remove_logs_archive_with_null_to_folder(self):
        code, out, _ = self.run_batch(
            [{"account": "acct", "message_id": "m3@x",
              "from_folder": "Areas/Receipts", "remove": True}],
            "--category", "passive-update")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(out["acted"]["removed"]), 1)
        (ev,) = self.events()
        self.assertEqual(ev["final_action"], "archive")
        self.assertIsNone(ev["to_folder"])
        self.assertEqual(ev["from_folder"], "Areas/Receipts")

    def test_preflight_refuses_bad_destination_before_acting(self):
        code, out, err = self.run_batch(
            [{"account": "acct", "message_id": "m1@x", "from_folder": "INBOX",
              "to_folder": "Trash"},
             {"account": "acct", "message_id": "m2@x", "from_folder": "INBOX",
              "to_folder": "Sent"}])
        self.assertEqual(code, 1)
        self.assertIn("not a triage destination", err)
        self.assertIn("nothing was acted", err)
        # first item must NOT have moved (all-or-nothing pre-flight)
        self.assertTrue(list((self.acct / "INBOX" / "cur").iterdir()))
        self.assertEqual(self.events(), [])
        self.assertFalse(self.stublog.exists())

    def test_dry_run_touches_nothing(self):
        code, out, _ = self.run_batch(
            [{"account": "acct", "message_id": "m1@x", "from_folder": "INBOX",
              "to_folder": "Trash"}],
            "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["plan"][0]["event"]["final_action"], "trash")
        self.assertEqual(len(list((self.acct / "INBOX" / "cur").iterdir())), 2)
        self.assertEqual(self.events(), [])
        self.assertFalse(self.stublog.exists())

    def test_act_failure_isolated_others_still_logged_and_synced(self):
        # m2's file vanishes between pre-flight and act (index stale).
        for f in (self.acct / "INBOX" / "cur").iterdir():
            if f.name.startswith("k2"):
                f.unlink()
        code, out, _ = self.run_batch(
            [{"account": "acct", "message_id": "m1@x", "from_folder": "INBOX",
              "to_folder": "Trash"},
             {"account": "acct", "message_id": "m2@x", "from_folder": "INBOX",
              "to_folder": "Active"}],
            "--category", "actionable")
        self.assertEqual(code, 1)
        self.assertEqual(len(out["acted"]["moved"]), 1)
        self.assertEqual(len(out["failed"]), 1)
        self.assertIn("index stale", out["failed"][0]["error"])
        self.assertEqual(out["events_logged"], 1)  # only the success
        self.assertEqual(self.events()[0]["message_id"], "m1@x")
        self.assertIn("acct:INBOX,Trash", self.stublog.read_text())

    def test_sync_retries_once_on_flake(self):
        os.environ["FAIL_ONCE_MARKER"] = str(self.tmp / "failed-once")
        self.addCleanup(os.environ.pop, "FAIL_ONCE_MARKER", None)
        code, out, _ = self.run_batch(
            [{"account": "acct", "message_id": "m1@x", "from_folder": "INBOX",
              "to_folder": "Trash"}])
        self.assertEqual(code, 0, out)
        self.assertEqual(out["sync"][0]["attempts"], 2)
        self.assertTrue(out["sync"][0]["ok"])

    def test_activate_derived_from_active_tier(self):
        code, out, _ = self.run_batch(
            [{"account": "acct", "message_id": "m2@x", "from_folder": "INBOX",
              "to_folder": "Active"}])
        self.assertEqual(code, 0, out)
        (ev,) = self.events()
        self.assertEqual(ev["final_action"], "activate")


if __name__ == "__main__":
    unittest.main(verbosity=1)
