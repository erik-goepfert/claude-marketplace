#!/usr/bin/env python3
"""Tests for unsub.py -- unsubscribe-link extraction + consent-gated exec.

Synthetic fixtures only, same shape as test_act.py/test_threads.py's
ReadCommandTests: a temp Maildir tree plus an index.db built with
indexer.py's real schema. Never touches ~/.local/share/email-triage/, and
`exec`'s network path is mocked in the one test that exercises it (all
other exec coverage here is --dry-run or a pre-network short-circuit, so
no real request is ever attempted)."""

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indexer  # noqa: E402
import unsub  # noqa: E402


def make_maildir_folder(root, folder):
    for sub in ("cur", "new", "tmp"):
        os.makedirs(os.path.join(root, folder, sub), exist_ok=True)


def put_maildir_file(root, folder, name, raw_bytes: bytes) -> str:
    make_maildir_folder(root, folder)
    path = os.path.join(root, folder, "cur", name)
    with open(path, "wb") as f:
        f.write(raw_bytes)
    return path


class UnsubTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.maildir_root = os.path.join(self._tmpdir.name, "acct")
        self.db_path = Path(self._tmpdir.name) / "index.db"
        self.conn = indexer.init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        self._tmpdir.cleanup()

    def index_row(self, folder, key, message_id, filename, folder_tier="untriaged"):
        self.conn.execute(
            """INSERT INTO envelopes (message_id, folder, maildir_key, maildir_file,
               account, folder_tier) VALUES (?, ?, ?, ?, 'acct', ?)""",
            (message_id, folder, key, filename, folder_tier),
        )
        self.conn.commit()

    def write_message(self, folder, key, msg: EmailMessage):
        put_maildir_file(self.maildir_root, folder, f"{key}:2,", msg.as_bytes())
        self.index_row(folder, key, msg["Message-ID"], f"{key}:2,")

    def _base_argv(self, command, message_id, *extra):
        return [
            command, "--db", str(self.db_path), "--account", "acct",
            "--message-id", message_id, "--maildir", self.maildir_root, *extra,
        ]

    def run_links(self, message_id):
        parser = unsub.build_parser()
        args = parser.parse_args(self._base_argv("links", message_id))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            unsub.cmd_links(args)
        return json.loads(buf.getvalue())

    def run_exec(self, message_id, *extra):
        parser = unsub.build_parser()
        args = parser.parse_args(self._base_argv("exec", message_id, *extra))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            unsub.cmd_exec(args)
        return json.loads(buf.getvalue())

    def run_exec_expect_exit1(self, message_id, *extra):
        parser = unsub.build_parser()
        args = parser.parse_args(self._base_argv("exec", message_id, *extra))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                unsub.cmd_exec(args)
        self.assertEqual(cm.exception.code, 1)
        return err.getvalue()

    # -- fixture builders (per Phase 5.3's required coverage) --

    def _no_unsub_message(self) -> EmailMessage:
        """multipart text/plain+text/html, nothing unsubscribe-related at all."""
        msg = EmailMessage()
        msg["From"] = "friend@example.com"
        msg["To"] = "me@example.com"
        msg["Subject"] = "Dinner Friday?"
        msg["Message-ID"] = "<plain-noun@example.com>"
        msg.set_content("Want to grab dinner Friday?\n")
        msg.add_alternative("<html><body><p>Want to grab dinner Friday?</p></body></html>", subtype="html")
        return msg

    def _header_one_click_message(self) -> EmailMessage:
        """List-Unsubscribe with both mailto and https + List-Unsubscribe-Post."""
        msg = EmailMessage()
        msg["From"] = "newsletter@example.com"
        msg["To"] = "me@example.com"
        msg["Subject"] = "Weekly newsletter"
        msg["Message-ID"] = "<news-oneclick@example.com>"
        msg["List-Unsubscribe"] = "<https://example.com/unsub?u=1>, <mailto:unsub@example.com>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        msg.set_content("This week's stories.\n")
        msg.add_alternative("<html><body><p>This week's stories.</p></body></html>", subtype="html")
        return msg

    def _image_button_message(self) -> EmailMessage:
        """html-only, base64 transfer encoding, image-button unsubscribe
        (<a href="..."><img alt="Unsubscribe"></a>), no header links at all."""
        msg = EmailMessage()
        msg["From"] = "promo@example.com"
        msg["To"] = "me@example.com"
        msg["Subject"] = "Big sale this weekend"
        msg["Message-ID"] = "<promo-imgbtn@example.com>"
        html = (
            "<html><body><p>Everything 50% off!</p>"
            '<a href="https://example.com/img-unsub?u=2">'
            '<img src="cid:btn.png" alt="Unsubscribe"></a>'
            "</body></html>"
        )
        msg.set_content(html, subtype="html", cte="base64")
        return msg

    def _ranking_message(self) -> EmailMessage:
        """Header https link present but NOT one-click, AND a body anchor-
        text unsubscribe link -- the header link is the automated offer
        (GET; designed for programmatic use), the body link is surfaced
        manual-only (user policy 2026-07-19)."""
        msg = EmailMessage()
        msg["From"] = "deals@example.com"
        msg["To"] = "me@example.com"
        msg["Subject"] = "Deals digest"
        msg["Message-ID"] = "<ranking1@example.com>"
        msg["List-Unsubscribe"] = "<https://example.com/header-only-unsub>"
        html = (
            "<html><body><p>Deals inside.</p>"
            '<p><a href="https://example.com/body-unsub">Unsubscribe</a> from this list.</p>'
            "</body></html>"
        )
        msg.set_content("Deals inside. Unsubscribe: https://example.com/body-unsub\n")
        msg.add_alternative(html, subtype="html")
        return msg

    # -- links --

    def test_links_no_unsubscribe_anything(self):
        msg = self._no_unsub_message()
        self.write_message("INBOX", "k1", msg)
        out = self.run_links(msg["Message-ID"])
        self.assertEqual(out["header"], {"https": [], "mailto": [], "one_click_post": False})
        self.assertEqual(out["body"], [])
        self.assertEqual(out["recommended"],
                         {"automated": None, "automated_method": None,
                          "manual": None, "mailto": None})

    def test_links_header_one_click_post_https_and_mailto(self):
        msg = self._header_one_click_message()
        self.write_message("INBOX", "k2", msg)
        out = self.run_links(msg["Message-ID"])
        self.assertEqual(out["header"]["https"], ["https://example.com/unsub?u=1"])
        self.assertEqual(out["header"]["mailto"], ["mailto:unsub@example.com"])
        self.assertTrue(out["header"]["one_click_post"])
        self.assertEqual(out["recommended"]["automated"], "https://example.com/unsub?u=1")
        self.assertEqual(out["recommended"]["automated_method"], "one-click-post")
        self.assertIsNone(out["recommended"]["manual"])

    def test_links_image_button_unsubscribe_via_img_alt(self):
        msg = self._image_button_message()
        self.write_message("INBOX", "k3", msg)
        out = self.run_links(msg["Message-ID"])
        self.assertEqual(len(out["body"]), 1)
        link = out["body"][0]
        self.assertEqual(link["url"], "https://example.com/img-unsub?u=2")
        self.assertEqual(link["source"], "img-alt")
        self.assertEqual(link["text"], "Unsubscribe")
        # No header links at all: nothing automated -- the body link is
        # offered for MANUAL clicking only (user policy 2026-07-19).
        self.assertIsNone(out["recommended"]["automated"])
        self.assertEqual(out["recommended"]["manual"], "https://example.com/img-unsub?u=2")

    def test_links_plain_header_https_is_automated_get_body_is_manual(self):
        msg = self._ranking_message()
        self.write_message("INBOX", "k4", msg)
        out = self.run_links(msg["Message-ID"])
        self.assertFalse(out["header"]["one_click_post"])
        # Header URL is designed for programmatic use -> the automated
        # offer even without one-click-post (method GET); the body link is
        # surfaced separately, manual-only (user policy 2026-07-19).
        self.assertEqual(out["recommended"]["automated"], "https://example.com/header-only-unsub")
        self.assertEqual(out["recommended"]["automated_method"], "get")
        self.assertEqual(out["recommended"]["manual"], "https://example.com/body-unsub")

    # -- exec --

    def test_exec_dry_run_default_post_shape(self):
        msg = self._header_one_click_message()
        self.write_message("INBOX", "k5", msg)
        out = self.run_exec(msg["Message-ID"], "--dry-run")
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["method"], "POST")
        self.assertEqual(out["url"], "https://example.com/unsub?u=1")
        self.assertEqual(out["headers"]["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(out["headers"]["User-Agent"], "email-triage-unsub/1.0")
        self.assertEqual(out["body"], "List-Unsubscribe=One-Click")

    def test_exec_default_requires_one_click_https(self):
        msg = self._no_unsub_message()
        self.write_message("INBOX", "k6", msg)
        parser = unsub.build_parser()
        args = parser.parse_args(self._base_argv("exec", msg["Message-ID"], "--dry-run"))
        with self.assertRaises(unsub.FatalError):
            unsub.cmd_exec(args)

    def test_exec_provenance_check_rejects_foreign_url(self):
        msg = self._header_one_click_message()
        self.write_message("INBOX", "k7", msg)
        stderr_text = self.run_exec_expect_exit1(
            msg["Message-ID"], "--url", "https://not-in-this-message.example.com/unsub", "--dry-run",
        )
        self.assertIn("provenance check failed", stderr_text)  # not in headers

    def test_exec_mailto_never_executed(self):
        msg = self._header_one_click_message()
        self.write_message("INBOX", "k8", msg)
        with mock.patch.object(unsub, "_send", side_effect=AssertionError("network must not be called")):
            out = self.run_exec(msg["Message-ID"], "--url", "mailto:unsub@example.com")
        self.assertEqual(out["method"], "mailto")
        self.assertEqual(out["url"], "mailto:unsub@example.com")
        self.assertIsNone(out["status"])
        self.assertEqual(out["note"], "route via normal send flow")

    def test_exec_mailto_short_circuits_even_with_dry_run(self):
        msg = self._header_one_click_message()
        self.write_message("INBOX", "k9", msg)
        with mock.patch.object(unsub, "_send", side_effect=AssertionError("network must not be called")):
            out = self.run_exec(msg["Message-ID"], "--url", "mailto:unsub@example.com", "--dry-run")
        self.assertEqual(out["method"], "mailto")
        self.assertEqual(out["note"], "route via normal send flow")

    def test_exec_refuses_body_link_even_with_provenance(self):
        # The link IS in the message -- provenance would pass -- but body
        # links are manual-only (user policy 2026-07-19): a confirmation
        # step usually follows, so a bare GET would claim false success.
        msg = self._image_button_message()
        self.write_message("INBOX", "k10", msg)
        stderr_text = self.run_exec_expect_exit1(
            msg["Message-ID"], "--url", "https://example.com/img-unsub?u=2", "--dry-run",
        )
        self.assertIn("manual-only", stderr_text)

    def test_exec_url_get_on_plain_header_https_ok(self):
        msg = self._ranking_message()
        self.write_message("INBOX", "k11", msg)
        out = self.run_exec(msg["Message-ID"], "--url", "https://example.com/header-only-unsub", "--dry-run")
        self.assertEqual(out["method"], "GET")
        self.assertEqual(out["url"], "https://example.com/header-only-unsub")
        self.assertIsNone(out["body"])
        self.assertNotIn("Content-Type", out["headers"])

    # -- _send: pure response-classification logic, no real network (opener mocked) --

    def test_send_post_2xx_reports_silence_is_success(self):
        class FakeResponse:
            status = 202
            headers = {}

            def read(self):
                return b""

        fake_opener = mock.MagicMock()
        fake_opener.open.return_value = FakeResponse()
        with mock.patch.object(unsub.urllib.request, "build_opener", return_value=fake_opener):
            result = unsub._send("POST", "https://example.com/u", {}, b"List-Unsubscribe=One-Click")
        self.assertEqual(result["status"], 202)
        self.assertIn("success", result["note"])

    def test_send_get_large_html_response_flagged_as_interactive(self):
        class FakeResponse:
            status = 200
            headers = {"Content-Type": "text/html; charset=utf-8"}

            def read(self):
                return b"<html>" + b"x" * 3000 + b"</html>"

        fake_opener = mock.MagicMock()
        fake_opener.open.return_value = FakeResponse()
        with mock.patch.object(unsub.urllib.request, "build_opener", return_value=fake_opener):
            result = unsub._send("GET", "https://example.com/u", {}, None)
        self.assertEqual(result["status"], 200)
        self.assertIn("interactive page", result["note"])


if __name__ == "__main__":
    unittest.main()
