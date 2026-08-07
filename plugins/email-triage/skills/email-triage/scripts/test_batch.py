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
import untrusted  # noqa: E402

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

    def test_thread_of_expands_live_members_only(self):
        # The DoorDash-shaped case from the 2026-07-19 session: a thread
        # with an INBOX arrival and an old Trash sibling. thread_of must
        # act on the INBOX member ONLY -- the trash sibling never moves.
        write_message(self.acct / "Trash", "k7", "S",
                      msg_headers("m7@x", "shop@example.com", "Order ready"))
        write_message(self.acct / "INBOX", "k8", "",
                      {**msg_headers("m8@x", "shop@example.com", "Re: Order ready"),
                       "In-Reply-To": "<m7@x>"})
        self.reindex()
        code, out, _ = self.run_batch(
            [{"account": "acct", "thread_of": "m8@x", "disposition": "trash"}],
            "--category", "passive-update")
        self.assertEqual(code, 0, out)
        (mv,) = out["acted"]["moved"]
        self.assertEqual(mv["message_id"], "m8@x")
        self.assertEqual(mv["from_folder"], "INBOX")
        self.assertEqual(mv["to_folder"], "Trash")  # resolved from config tiers
        # trash sibling untouched
        self.assertTrue(list((self.acct / "Trash" / "cur").iterdir()))
        (ev,) = self.events()
        self.assertEqual(ev["final_action"], "trash")

    def test_thread_of_works_by_any_member_and_carries_overrides(self):
        # Anchor by the TRASH member's id; expansion still yields the live
        # member, and per-item overrides ride along to expanded items.
        write_message(self.acct / "Trash", "k7", "S",
                      msg_headers("m7@x", "shop@example.com", "Order ready"))
        write_message(self.acct / "INBOX", "k8", "",
                      {**msg_headers("m8@x", "shop@example.com", "Re: Order ready"),
                       "In-Reply-To": "<m7@x>"})
        self.reindex()
        code, out, _ = self.run_batch(
            [{"account": "acct", "thread_of": "m7@x", "disposition": "trash",
              "notes": "recidivist"}])
        self.assertEqual(code, 0, out)
        self.assertEqual(out["acted"]["moved"][0]["message_id"], "m8@x")
        (ev,) = self.events()
        self.assertEqual(ev["notes"], "recidivist")

    def test_thread_of_with_no_live_members_refuses_preflight(self):
        write_message(self.acct / "Trash", "k7", "S",
                      msg_headers("m7@x", "shop@example.com", "Old noise"))
        self.reindex()
        code, out, err = self.run_batch(
            [{"account": "acct", "thread_of": "m7@x", "disposition": "trash"}])
        self.assertEqual(code, 1)
        self.assertIn("no members in a live tier", err)
        self.assertIn("nothing was acted", err)

    def test_disposition_on_plain_item_resolves_per_account(self):
        code, out, _ = self.run_batch(
            [{"account": "acct", "message_id": "m2@x", "from_folder": "INBOX",
              "disposition": "activate"}])
        self.assertEqual(code, 0, out)
        self.assertEqual(out["acted"]["moved"][0]["to_folder"], "Active")
        (ev,) = self.events()
        self.assertEqual(ev["final_action"], "activate")

    def test_activate_derived_from_active_tier(self):
        code, out, _ = self.run_batch(
            [{"account": "acct", "message_id": "m2@x", "from_folder": "INBOX",
              "to_folder": "Active"}])
        self.assertEqual(code, 0, out)
        (ev,) = self.events()
        self.assertEqual(ev["final_action"], "activate")


class ActiveZoneSeenTests(BatchTests):
    """User rule 2026-08-07: leaving the active zone marks the mail read.
    `\Seen` means "awaiting your attention" inside the live tiers and is noise
    outside them -- an unread badge on something already decided."""

    def unread_inbox(self, key, mid):
        write_message(self.acct / "INBOX", key, "",   # no flags = unread
                      msg_headers(mid, "bulk@x.example", "Some subject"))
        self.reindex()
        return mid

    def moved_filename(self, folder, key):
        hits = list((self.acct / folder / "cur").glob(f"{key}*"))
        self.assertEqual(len(hits), 1, f"expected one file for {key} in {folder}")
        return hits[0].name

    def test_trash_marks_read(self):
        mid = self.unread_inbox("z1", "z1@x")
        code, out, _err = self.run_batch(
            [{"account": "acct", "message_id": mid, "from_folder": "INBOX",
              "disposition": "trash"}])
        self.assertEqual(code, 0)
        self.assertEqual(out["acted"]["moved"][0]["message_id"], mid)
        self.assertIn("S", self.moved_filename("Trash", "z1").split(":2,")[1])

    def test_archive_marks_read(self):
        mid = self.unread_inbox("z2", "z2@x")
        code, out, _err = self.run_batch(
            [{"account": "acct", "message_id": mid, "from_folder": "INBOX",
              "to_folder": "Archive"}])
        self.assertEqual(code, 0)
        self.assertEqual(out["acted"]["moved"][0]["message_id"], mid)
        self.assertIn("S", self.moved_filename("Archive", "z2").split(":2,")[1])

    def test_activate_does_NOT_mark_read(self):
        """The distinction the rule turns on: `activate` keeps the thread in the
        live tier, so its unread state is still a real signal about work
        outstanding. Marking it read would erase that."""
        mid = self.unread_inbox("z3", "z3@x")
        code, out, _err = self.run_batch(
            [{"account": "acct", "message_id": mid, "from_folder": "INBOX",
              "disposition": "activate"}])
        self.assertEqual(code, 0)
        self.assertEqual(out["acted"]["moved"][0]["message_id"], mid)
        self.assertNotIn("S", self.moved_filename("Active", "z3").split(":2,")[1])

    def test_leaves_active_zone_set_matches_the_action_vocabulary(self):
        # Terminal-but-in-zone is a contradiction; guard the set explicitly so a
        # new action cannot silently default to "does not mark read".
        self.assertEqual(batch.LEAVES_ACTIVE_ZONE, {"trash", "archive"})
        self.assertNotIn("activate", batch.LEAVES_ACTIVE_ZONE)


class BatchTrustTests(BatchTests):
    """Phase 6.2 on batch.py, whose allowlist is COMPOSED from indexer's."""

    def hostile_inbox_message(self, key="kh", mid="hostile&1@x"):
        write_message(self.acct / "INBOX", key,
                      "", msg_headers(mid, '"<script>x</script>" <spam@x.example>',
                                      "Sale <b>now</b> & </untrusted-content-0>"))
        self.reindex()
        return mid

    def assert_complete(self, payload):
        undeclared = (untrusted.string_paths(payload)
                      - {p for p in untrusted.string_paths(payload)
                         if p.startswith("_trust")}
                      - set(batch.BATCH_TRUSTED)
                      - set(payload["_trust"]["untrusted_fields"]))
        self.assertEqual(undeclared, set())

    def test_dry_run_manifest_is_complete(self):
        mid = self.hostile_inbox_message()
        code, out, _err = self.run_batch(
            [{"account": "acct", "message_id": mid, "from_folder": "INBOX",
              "disposition": "trash"}], "--dry-run")
        self.assertEqual(code, 0)
        self.assert_complete(out)

    def test_full_run_manifest_is_complete(self):
        mid = self.hostile_inbox_message(key="kf", mid="hostile&2@x")
        code, out, _err = self.run_batch(
            [{"account": "acct", "message_id": mid, "from_folder": "INBOX",
              "disposition": "trash"}])
        self.assertEqual(code, 0)
        self.assert_complete(out)

    def test_event_sender_fields_and_notes_are_declared(self):
        """invariant 7: a model-written note derived from email content is
        untrusted too -- a summary of an injection still carries its words, and
        history.db is the laundering path the confinement rule closes."""
        mid = self.hostile_inbox_message(key="kn", mid="hostile&3@x")
        _code, out, _err = self.run_batch(
            [{"account": "acct", "message_id": mid, "from_folder": "INBOX",
              "disposition": "trash", "notes": "sender said <b>hi</b>"}],
            "--dry-run")
        declared = set(out["_trust"]["untrusted_fields"])
        for path in ("plan.event.sender_addr", "plan.event.subject",
                     "plan.event.notes"):
            self.assertIn(path, declared)
        self.assertIn("&lt;b&gt;", out["plan"][0]["event"]["notes"])

    def test_event_enums_and_folders_stay_bare(self):
        mid = self.hostile_inbox_message(key="ke", mid="hostile&4@x")
        _code, out, _err = self.run_batch(
            [{"account": "acct", "message_id": mid, "from_folder": "INBOX",
              "disposition": "trash"}], "--dry-run")
        declared = set(out["_trust"]["untrusted_fields"])
        for path in ("plan.event.final_action", "plan.event.decided_by",
                     "plan.event.from_folder", "plan.event.session_started_at",
                     "plan.account"):
            self.assertNotIn(path, declared)
        self.assertEqual(out["plan"][0]["event"]["final_action"], "trash")

    def test_message_id_handles_survive_the_whole_pipeline(self):
        # An `&` in the id must reach act.py byte-identical or the move fails.
        mid = self.hostile_inbox_message(key="kh2", mid="hostile&5@x")
        code, out, _err = self.run_batch(
            [{"account": "acct", "message_id": mid, "from_folder": "INBOX",
              "disposition": "trash"}])
        self.assertEqual(code, 0)
        self.assertEqual(out["acted"]["moved"][0]["message_id"], mid)
        self.assertIn("acted.moved.message_id", out["_trust"]["untrusted_fields"])
        # And the event logged for it carries the same unaltered id.
        self.assertEqual(out["events_logged"], 1)

    def test_nested_change_report_uses_the_composed_allowlist(self):
        mid = self.hostile_inbox_message(key="kc", mid="hostile&6@x")
        _code, out, _err = self.run_batch(
            [{"account": "acct", "message_id": mid, "from_folder": "INBOX",
              "disposition": "trash"}])
        # Its own echo is absorbed, so external is empty -- the point being
        # that the allowlist paths exist for it either way, one level deeper.
        self.assertIn("change_report", out)
        self.assertIn("change_report.external.new.account", batch.BATCH_TRUSTED)
        self.assertIn("change_report.external.new.message_id", batch.BATCH_HANDLES)


if __name__ == "__main__":
    unittest.main(verbosity=1)
