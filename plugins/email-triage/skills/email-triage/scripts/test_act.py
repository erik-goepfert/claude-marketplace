#!/usr/bin/env python3
"""Tests for act.py — the local Maildir move/remove act layer.

Synthetic fixtures only: a temp Maildir tree plus an index.db built with
indexer.py's real schema. Never touches real data."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import act  # noqa: E402
import indexer  # noqa: E402
import untrusted  # noqa: E402


def make_folder(root, folder):
    for sub in ("cur", "new", "tmp"):
        os.makedirs(os.path.join(root, folder, sub), exist_ok=True)


def put_file(root, folder, name, body=b"Subject: t\n\nx\n"):
    make_folder(root, folder)
    path = os.path.join(root, folder, "cur", name)
    with open(path, "wb") as f:
        f.write(body)
    return path


class ActTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "acct")
        self.db = os.path.join(self.tmp.name, "index.db")
        from pathlib import Path
        indexer.init_db(Path(self.db)).close()
        self.conn = sqlite3.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def index_row(self, folder, key, mid, filename):
        self.conn.execute(
            """INSERT INTO envelopes (message_id, folder, maildir_key,
               maildir_file, account, folder_tier)
               VALUES (?, ?, ?, ?, 'acct', 'untriaged')""",
            (mid, folder, key, filename))
        self.conn.commit()

    def run_cli(self, *argv):
        from io import StringIO
        from contextlib import redirect_stdout
        buf = StringIO()
        with redirect_stdout(buf):
            code = act.main(list(argv))
        return code, json.loads(buf.getvalue())

    def test_move_strips_uid_token(self):
        put_file(self.root, "INBOX", "key1,U=99:2,F")
        make_folder(self.root, "Archive")
        self.index_row("INBOX", "key1,U=99", "<m1>", "key1,U=99:2,F")
        code, out = self.run_cli(
            "move", "--db", self.db, "--maildir", self.root,
            "--account", "acct", "--from", "INBOX", "--to", "Archive", "<m1>")
        self.assertEqual(code, 0)
        self.assertEqual(out["moved"][0]["file"], "key1:2,F")
        self.assertTrue(os.path.exists(
            os.path.join(self.root, "Archive", "cur", "key1:2,F")))
        self.assertFalse(os.listdir(os.path.join(self.root, "INBOX", "cur")))

    def test_move_resolves_current_filename_not_indexed_one(self):
        # Indexed under one name; since then a sync re-stamped U= and a flag
        # changed. Resolution must go by the stable stem, not the stale name.
        put_file(self.root, "INBOX", "key2,U=7:2,FS")
        self.index_row("INBOX", "key2,U=7", "<m2>", "key2,U=7:2,")
        code, out = self.run_cli(
            "move", "--db", self.db, "--maildir", self.root,
            "--account", "acct", "--from", "INBOX", "--to", "Archive", "<m2>")
        self.assertEqual(code, 0)
        self.assertEqual(out["moved"][0]["file"], "key2:2,FS")

    def test_move_refuses_collision(self):
        put_file(self.root, "INBOX", "key3,U=1:2,")
        put_file(self.root, "Archive", "key3:2,")
        self.index_row("INBOX", "key3,U=1", "<m3>", "key3,U=1:2,")
        code, out = self.run_cli(
            "move", "--db", self.db, "--maildir", self.root,
            "--account", "acct", "--from", "INBOX", "--to", "Archive", "<m3>")
        self.assertEqual(code, 1)
        self.assertIn("refusing to overwrite", out["failed"][0]["error"])
        # source untouched
        self.assertTrue(os.path.exists(
            os.path.join(self.root, "INBOX", "cur", "key3,U=1:2,")))

    def test_move_stale_index_fails_cleanly(self):
        make_folder(self.root, "INBOX")
        make_folder(self.root, "Archive")
        self.index_row("INBOX", "ghost", "<m4>", "ghost:2,")
        code, out = self.run_cli(
            "move", "--db", self.db, "--maildir", self.root,
            "--account", "acct", "--from", "INBOX", "--to", "Archive", "<m4>")
        self.assertEqual(code, 1)
        self.assertIn("index stale", out["failed"][0]["error"])

    def test_remove_with_other_copy(self):
        put_file(self.root, "Areas/Receipts", "key5,U=4:2,S")
        put_file(self.root, "[Gmail]/All Mail", "key5b,U=8:2,S")
        self.index_row("Areas/Receipts", "key5,U=4", "<m5>", "key5,U=4:2,S")
        self.index_row("[Gmail]/All Mail", "key5b,U=8", "<m5>", "key5b,U=8:2,S")
        code, out = self.run_cli(
            "remove", "--db", self.db, "--maildir", self.root,
            "--account", "acct", "--from", "Areas/Receipts", "<m5>")
        self.assertEqual(code, 0)
        self.assertEqual(out["removed"][0]["removed_from"], "Areas/Receipts")
        self.assertFalse(os.listdir(
            os.path.join(self.root, "Areas/Receipts", "cur")))
        self.assertTrue(os.listdir(
            os.path.join(self.root, "[Gmail]/All Mail", "cur")))

    def test_remove_refuses_last_copy(self):
        put_file(self.root, "Areas/Receipts", "key6,U=4:2,")
        self.index_row("Areas/Receipts", "key6,U=4", "<m6>", "key6,U=4:2,")
        code, out = self.run_cli(
            "remove", "--db", self.db, "--maildir", self.root,
            "--account", "acct", "--from", "Areas/Receipts", "<m6>")
        self.assertEqual(code, 1)
        self.assertIn("ONLY copy", out["failed"][0]["error"])
        self.assertTrue(os.listdir(
            os.path.join(self.root, "Areas/Receipts", "cur")))

    # -- Phase 6.2: escape + `_trust` manifest --

    def test_manifest_is_complete(self):
        put_file(self.root, "INBOX", "k9:2,")
        make_folder(self.root, "Archive")
        self.index_row("INBOX", "k9", "<m9>", "k9:2,")
        code, out = self.run_cli(
            "move", "--db", self.db, "--maildir", self.root,
            "--account", "acct", "--from", "INBOX", "--to", "Archive",
            "<m9>", "<never-indexed>")
        self.assertEqual(code, 1)  # one moved, one failed
        undeclared = (untrusted.string_paths(out)
                      - {p for p in untrusted.string_paths(out)
                         if p.startswith("_trust")}
                      - set(act.TRUSTED_FIELDS)
                      - set(out["_trust"]["untrusted_fields"]))
        self.assertEqual(undeclared, set())

    def test_error_prose_is_untrusted_because_it_quotes_the_sender(self):
        """The finding this classification exists for: `failed.error` reads like
        our own prose and is -- but every message interpolates `message_id!r`,
        so a sender picks part of the string. "We wrote the field" is not the
        test; "can a sender reach the value" is."""
        make_folder(self.root, "INBOX")  # main() needs the maildir root to exist
        code, out = self.run_cli(
            "move", "--db", self.db, "--maildir", self.root,
            "--account", "acct", "--from", "INBOX", "--to", "Archive",
            "<a&b<script>@x>")
        self.assertEqual(code, 1)
        self.assertIn("failed.error", out["_trust"]["untrusted_fields"])
        err = out["failed"][0]["error"]
        self.assertNotIn("<script>", err)
        self.assertIn("&lt;script&gt;", err)

    def test_message_id_handle_survives_unaltered(self):
        # act.py itself is the consumer: the model pastes this id straight back
        # in, and the lookup is exact string equality.
        hostile = "<a&b@x.com>"
        put_file(self.root, "INBOX", "k8:2,")
        make_folder(self.root, "Archive")
        self.index_row("INBOX", "k8", hostile, "k8:2,")
        code, out = self.run_cli(
            "move", "--db", self.db, "--maildir", self.root,
            "--account", "acct", "--from", "INBOX", "--to", "Archive", hostile)
        self.assertEqual(code, 0)
        self.assertEqual(out["moved"][0]["message_id"], hostile)
        self.assertIn("moved.message_id", out["_trust"]["untrusted_fields"])

    def test_folder_names_stay_bare(self):
        put_file(self.root, "INBOX", "k7:2,")
        make_folder(self.root, "Archive")
        self.index_row("INBOX", "k7", "<m7>", "k7:2,")
        _code, out = self.run_cli(
            "move", "--db", self.db, "--maildir", self.root,
            "--account", "acct", "--from", "INBOX", "--to", "Archive", "<m7>")
        declared = set(out["_trust"]["untrusted_fields"])
        for path in ("moved.from_folder", "moved.to_folder", "moved.file"):
            self.assertNotIn(path, declared)


if __name__ == "__main__":
    unittest.main(verbosity=1)
