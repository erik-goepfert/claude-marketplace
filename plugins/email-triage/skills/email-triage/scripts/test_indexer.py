#!/usr/bin/env python3
"""Self-test for indexer.py (design refs: email-skill-design.md §7.4/§8,
implementation-plan.md item 2.4's taxonomy scope). Builds a tiny synthetic
Maildir under tempfile, never touches the real mirror.

Covers:
  - two accounts (icloud, gmail), a few folders including a nested one
    (Archive/Housing/2025-Colorado-Move)
  - an All-Mail-only gmail message (virtual Archive tier)
  - a dual-labeled gmail message (Areas/Taxes + [Gmail]/All Mail -> tier
    resolves to 'areas', All-Mail copy ignored)
  - a missing-Message-ID message (synthetic id)
  - an unrecognized folder (surfaced in the change report)
  - idempotence: running index twice with no filesystem changes reports no
    changes on the second run
  - a flag rename, a cross-folder move, and a deletion, each correctly
    classified on a third run
  - the message_tiers view returning the right tier for every message at
    each stage
"""

import importlib.util
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

_INDEXER_PATH = Path(__file__).resolve().parent / "indexer.py"
_spec = importlib.util.spec_from_file_location("indexer", _INDEXER_PATH)
indexer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(indexer)


def write_message(folder_path: Path, unique: str, flags, headers: dict, subdir: str = "cur") -> Path:
    """Write one synthetic Maildir message file. flags=None omits the ':2,'
    suffix entirely (as in new/ before any flag has ever been set)."""
    for d in ("cur", "new", "tmp"):
        (folder_path / d).mkdir(parents=True, exist_ok=True)
    filename = unique if flags is None else f"{unique}:2,{flags}"
    lines = [f"{k}: {v}" for k, v in headers.items() if v is not None]
    content = "\r\n".join(lines) + "\r\n\r\n" + "body text, never read by the indexer\r\n"
    path = folder_path / subdir / filename
    path.write_bytes(content.encode("utf-8"))
    return path


class IndexerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="email-triage-indexer-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.maildir_root = self.tmp / "mail"
        self.db_path = self.tmp / "index.db"

        icloud = self.maildir_root / "icloud"
        gmail = self.maildir_root / "gmail"

        # icloud: INBOX (= account root) + a nested Archive folder.
        self.inbox_icloud = icloud
        self.archive_housing = icloud / "Archive" / "Housing" / "2025-Colorado-Move"

        # M1: will be flag-renamed in the mutation phase.
        self.m1_path = write_message(
            self.inbox_icloud,
            "m1-unique",
            "S",
            {
                "Message-ID": "<m1@example.com>",
                "Date": "Mon, 01 Jun 2026 10:00:00 -0700",
                "From": "Alice <alice@example.com>",
                "To": "erik@example.com",
                "Subject": "Flag me later",
            },
        )
        # M2: will be moved cross-folder in the mutation phase.
        self.m2_path = write_message(
            self.inbox_icloud,
            "m2-unique",
            "",
            {
                "Message-ID": "<m2@example.com>",
                "Date": "Tue, 02 Jun 2026 11:00:00 -0700",
                "From": "Bob <bob@example.com>",
                "To": "erik@example.com",
                "Subject": "Move me later",
            },
        )
        # M4: stays put; exercises nested folder naming.
        write_message(
            self.archive_housing,
            "m4-unique",
            "S",
            {
                "Message-ID": "<m4@example.com>",
                "Date": "Wed, 03 Jun 2026 12:00:00 -0700",
                "From": "Carol <carol@example.com>",
                "To": "erik@example.com",
                "Subject": "Closing statement",
            },
        )
        # M5: will be deleted in the mutation phase.
        self.m5_path = write_message(
            self.archive_housing,
            "m5-unique",
            "S",
            {
                "Message-ID": "<m5@example.com>",
                "Date": "Thu, 04 Jun 2026 13:00:00 -0700",
                "From": "Dave <dave@example.com>",
                "To": "erik@example.com",
                "Subject": "Old receipt",
            },
        )

        # gmail: INBOX with a missing-Message-ID message.
        gmail_inbox = gmail / "INBOX"
        write_message(
            gmail_inbox,
            "g1-unique",
            "",
            {
                "Date": "Fri, 05 Jun 2026 09:00:00 +0000",
                "From": "noreply@service.example.com",
                "To": "erik@gmail.example.com",
                "Subject": "No Message-ID here",
            },
        )

        # gmail: dual-labeled message (Areas/Taxes + [Gmail]/All Mail).
        areas_taxes = gmail / "Areas" / "Taxes"
        all_mail = gmail / "[Gmail]" / "All Mail"
        g2_headers = {
            "Message-ID": "<g2@example.com>",
            "Date": "Sat, 06 Jun 2026 08:00:00 +0000",
            "From": "irs@example.com",
            "To": "erik@gmail.example.com",
            "Subject": "Your tax document",
        }
        write_message(areas_taxes, "g2-taxes-copy", "S", g2_headers)
        write_message(all_mail, "g2-allmail-copy", "S", g2_headers)

        # gmail: All-Mail-only message (virtual Archive tier).
        write_message(
            all_mail,
            "g3-unique",
            "S",
            {
                "Message-ID": "<g3@example.com>",
                "Date": "Sun, 07 Jun 2026 07:00:00 +0000",
                "From": "friend@example.com",
                "To": "erik@gmail.example.com",
                "Subject": "Archived thread",
            },
        )

        # gmail: unrecognized folder (folder audit leftover).
        newsletter = gmail / "Newsletter"
        write_message(
            newsletter,
            "g4-unique",
            "S",
            {
                "Message-ID": "<g4@example.com>",
                "Date": "Mon, 08 Jun 2026 06:00:00 +0000",
                "From": "digest@newsletter.example.com",
                "To": "erik@gmail.example.com",
                "Subject": "Weekly digest",
            },
        )

    def run_indexer(self, accounts=None):
        # No --config passed (mirrors a bare bootstrap, no config.yaml on
        # disk) -- exercises the DEFAULT_TIER_MAPS fallback (4.2).
        conn = indexer.init_db(self.db_path)
        try:
            resolved = accounts or indexer.resolve_accounts(self.maildir_root, None)
            tier_maps = indexer.load_tier_maps(self.tmp / "no-such-config.yaml", resolved)
            report = indexer.run_index(conn, self.maildir_root, resolved, tier_maps)
            conn.commit()
        finally:
            conn.close()
        return report

    def query_tiers(self):
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("SELECT account, message_id, tier FROM message_tiers").fetchall()
        finally:
            conn.close()
        return {(a, m): t for a, m, t in rows}

    def test_full_lifecycle(self):
        # --- First run: everything is new. ---
        report1 = self.run_indexer()

        self.assertEqual(report1["parse_errors"], 0)
        self.assertEqual(len(report1["new"]), 9, report1["new"])
        self.assertEqual(report1["moved"], [])
        self.assertEqual(report1["deleted"], [])
        self.assertEqual(report1["flag_changed"], [])
        self.assertEqual(report1["unrecognized_folders"], ["Newsletter"])

        new_ids = {e["message_id"] for e in report1["new"]}
        self.assertIn("m1@example.com", new_ids)
        self.assertIn("g2@example.com", new_ids)  # appears twice (two folders), but message_id is shared
        # Synthetic id present for the missing-Message-ID message.
        synthetic_ids = [mid for mid in new_ids if mid.startswith("synthetic-")]
        self.assertEqual(len(synthetic_ids), 1)

        tiers = self.query_tiers()
        self.assertEqual(tiers[("icloud", "m1@example.com")], "untriaged")
        self.assertEqual(tiers[("icloud", "m2@example.com")], "untriaged")
        self.assertEqual(tiers[("icloud", "m4@example.com")], "cold")
        self.assertEqual(tiers[("icloud", "m5@example.com")], "cold")
        self.assertEqual(tiers[("gmail", synthetic_ids[0])], "untriaged")
        self.assertEqual(tiers[("gmail", "g2@example.com")], "ongoing")  # non-allmail wins over allmail
        self.assertEqual(tiers[("gmail", "g3@example.com")], "cold")  # allmail-only -> virtual Cold
        self.assertEqual(tiers[("gmail", "g4@example.com")], "unrecognized")
        self.assertEqual(len(tiers), 8)  # g2's two rows collapse into one message_tiers row

        # --- Second run, no filesystem changes: idempotent. ---
        report2 = self.run_indexer()
        self.assertEqual(report2["new"], [])
        self.assertEqual(report2["moved"], [])
        self.assertEqual(report2["deleted"], [])
        self.assertEqual(report2["flag_changed"], [])
        self.assertEqual(report2["parse_errors"], 0)

        # --- Mutations: flag rename, cross-folder move, deletion. ---
        m1_new = self.m1_path.with_name("m1-unique:2,FS")
        self.m1_path.rename(m1_new)

        m2_new = self.archive_housing / "cur" / self.m2_path.name
        self.m2_path.rename(m2_new)

        self.m5_path.unlink()

        report3 = self.run_indexer()

        self.assertEqual(report3["new"], [])
        self.assertEqual(report3["parse_errors"], 0)

        self.assertEqual(len(report3["flag_changed"]), 1)
        fc = report3["flag_changed"][0]
        self.assertEqual(fc["message_id"], "m1@example.com")
        self.assertEqual(fc["account"], "icloud")
        self.assertEqual(fc["folder"], "INBOX")
        self.assertEqual(fc["old_flags"], ["Seen"])
        self.assertEqual(fc["new_flags"], ["Flagged", "Seen"])

        self.assertEqual(len(report3["moved"]), 1)
        mv = report3["moved"][0]
        self.assertEqual(mv["message_id"], "m2@example.com")
        self.assertEqual(mv["account"], "icloud")
        self.assertEqual(mv["from_folder"], "INBOX")
        self.assertEqual(mv["to_folder"], "Archive/Housing/2025-Colorado-Move")

        self.assertEqual(len(report3["deleted"]), 1)
        de = report3["deleted"][0]
        self.assertEqual(de["message_id"], "m5@example.com")
        self.assertEqual(de["account"], "icloud")
        self.assertEqual(de["folder"], "Archive/Housing/2025-Colorado-Move")

        # DB state reflects the mutations.
        conn = sqlite3.connect(self.db_path)
        try:
            is_flagged, flags_json = conn.execute(
                "SELECT is_flagged, flags FROM envelopes WHERE message_id = 'm1@example.com'"
            ).fetchone()
            self.assertEqual(is_flagged, 1)
            self.assertIn("Flagged", json.loads(flags_json))
        finally:
            conn.close()

        tiers_after = self.query_tiers()
        self.assertEqual(tiers_after[("icloud", "m2@example.com")], "cold")  # moved into Archive
        self.assertNotIn(("icloud", "m5@example.com"), tiers_after)  # deleted
        self.assertEqual(len(tiers_after), 7)

        # --- Fourth run, no further changes: idempotent again. ---
        report4 = self.run_indexer()
        self.assertEqual(report4["new"], [])
        self.assertEqual(report4["moved"], [])
        self.assertEqual(report4["deleted"], [])
        self.assertEqual(report4["flag_changed"], [])

    def test_init_creates_schema_only(self):
        conn = indexer.init_db(self.db_path)
        conn.close()
        conn = sqlite3.connect(self.db_path)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        finally:
            conn.close()
        self.assertIn("envelopes", tables)
        self.assertIn("message_tiers", tables)
        # No rows -- init doesn't touch the (nonexistent) maildir.
        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM envelopes").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_bad_maildir_root_is_fatal(self):
        with self.assertRaises(indexer.FatalError):
            indexer.resolve_accounts(self.tmp / "does-not-exist", None)

    def test_unknown_account_is_fatal(self):
        with self.assertRaises(indexer.FatalError):
            indexer.resolve_accounts(self.maildir_root, ["not-a-real-account"])

    def test_account_filter_scopes_to_selected_accounts(self):
        conn = indexer.init_db(self.db_path)
        try:
            accounts = indexer.resolve_accounts(self.maildir_root, ["icloud"])
            tier_maps = indexer.load_tier_maps(self.tmp / "no-such-config.yaml", accounts)
            report = indexer.run_index(conn, self.maildir_root, accounts, tier_maps)
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(all(e["account"] == "icloud" for e in report["new"]))
        self.assertEqual(report["unrecognized_folders"], [])  # Newsletter lives under gmail, not indexed


class RestampTest(unittest.TestCase):
    """mbsync re-stamps ,U=<uid> after uploading a file act.py wrote (U=
    stripped on move). Same message, same folder, new maildir key: must be
    absorbed silently -- NOT reported as deleted+new -- while genuine
    cross-folder moves still classify as moves (regression: the first
    version of this fix ate real moves by matching the orphan's own row)."""

    def _run(self, root, db):
        import io, contextlib, sys as _sys
        buf = io.StringIO()
        argv = _sys.argv
        try:
            # Explicit --config pointing at a nonexistent file: deterministic
            # DEFAULT_TIER_MAPS fallback, never the real ~/.local/share
            # config.yaml (which --config's own default would otherwise read).
            _sys.argv = [
                "indexer.py", "index", "--maildir", str(root), "--db", str(db),
                "--config", str(Path(root).parent / "no-such-config.yaml"),
            ]
            with contextlib.redirect_stdout(buf):
                try:
                    indexer.main()
                except SystemExit as e:
                    if e.code not in (0, None):
                        raise
        finally:
            _sys.argv = argv
        return json.loads(buf.getvalue())

    def test_uid_restamp_is_silent_but_moves_still_classify(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mail"
            acct = root / "icloud"
            for f in ("INBOX", "Archive"):
                for sub in ("cur", "new", "tmp"):
                    (acct / f / sub).mkdir(parents=True)
            (acct / "INBOX" / "cur" / "k1,U=5:2,").write_bytes(
                b"Message-ID: <x@e>\nSubject: s\nFrom: a@b\n"
                b"Date: Tue, 1 Jul 2026 10:00:00 +0000\n\nb\n")
            db = Path(tmp) / "i.db"
            self._run(root, db)
            # genuine cross-folder move still reports as a move
            (acct / "INBOX" / "cur" / "k1,U=5:2,").rename(
                acct / "Archive" / "cur" / "k1,U=5:2,")
            r = self._run(root, db)
            self.assertEqual(len(r["moved"]), 1)
            self.assertEqual(r["new"], [])
            self.assertEqual(r["deleted"], [])
            # in-place U= restamp is absorbed silently
            (acct / "Archive" / "cur" / "k1,U=5:2,").rename(
                acct / "Archive" / "cur" / "k1,U=99:2,")
            r = self._run(root, db)
            for key in ("new", "moved", "deleted", "flag_changed"):
                self.assertEqual(r[key], [], key)


# --------------------------------------------------------------------------
# Config parser (4.2): a fixture string mirroring the real config.yaml's
# tricky parts -- comments (inline and full-line), quoted values with
# brackets/slashes, bare values with spaces, null, the server_rules `>-`
# folded multiline block, excluded_from_sync -- none of which should ever
# leak into the parsed tiers map.
# --------------------------------------------------------------------------

FIXTURE_CONFIG = """\
# Machine-local config for the email-triage skill (not checked into the plugin repo).
#
# Schema v2 comment block, several lines, purely descriptive.

accounts:
  icloud:
    email: erik.goepfert@icloud.com
    himalaya_account: icloud
    server_rules: []           # none; same delete-only policy applies if added
    tiers:
      untriaged: INBOX
      active: Active
      ongoing: Areas
      cold: Archive            # a real folder AND a root (chapters beneath)
      sent: Sent Messages
      drafts: Drafts
      trash: Deleted Messages
      junk: Junk

  gmail:
    email: egep84@gmail.com
    himalaya_account: gmail
    server_rules:              # server-side filters (web-UI managed; invisible over
                               # IMAP, so this registry is the only drift check).
                               # Policy: server rules DELETE only -- never file into
                               # folders (design section 11, noise-handling escalation).
      - created: 2026-07-09
        matches: "from:(substack.com -no-reply@substack.com)"
        action: delete
        why: >-
          Substack newsletters can't be unsubscribed without unfollowing the
          author on Substack itself; content is read in the app, so the email
          is pure clutter. no-reply@substack.com is carved out because login
          magic links and payment receipts come from it. Verified 2026-07-09:
          backfill trashed 537 old issues; a login link arrived post-filter.
    tiers:
      untriaged: INBOX
      active: Active
      ongoing: Areas
      cold: null               # virtual -- no such place (the stale MVP-era
                               # `Archive` label was deleted 2026-07-17);
                               # named Archive/* chapter labels are still
                               # real and classify cold via the root below
      cold_root: Archive       # named-chapter subtree (Archive/Career/...);
                               # only meaningful alongside cold: null
      all_mail: "[Gmail]/All Mail"
      sent: "[Gmail]/Sent Mail"
      drafts: "[Gmail]/Drafts"
      trash: "[Gmail]/Trash"
      junk: "[Gmail]/Spam"
    excluded_from_sync:        # mbsync channel exclusions.
      - "[Gmail]/Starred"
      - "[Gmail]/Important"

# Local Maildir mirror root (Phase 2.1, initial clones 2026-07-09).
maildir_path: ~/.local/share/email-triage/mail
"""


class ConfigParserTest(unittest.TestCase):
    def test_parses_icloud_tiers(self):
        accounts = indexer.parse_config_tiers_text(FIXTURE_CONFIG)
        self.assertEqual(accounts["icloud"], {
            "untriaged": "INBOX",
            "active": "Active",
            "ongoing": "Areas",
            "cold": "Archive",
            "sent": "Sent Messages",  # bare value with a space
            "drafts": "Drafts",
            "trash": "Deleted Messages",
            "junk": "Junk",
        })

    def test_gmail_null_cold_cold_root_and_quoted_values(self):
        accounts = indexer.parse_config_tiers_text(FIXTURE_CONFIG)
        gmail = accounts["gmail"]
        self.assertIsNone(gmail["cold"])
        self.assertEqual(gmail["cold_root"], "Archive")
        self.assertEqual(gmail["all_mail"], "[Gmail]/All Mail")
        self.assertEqual(gmail["sent"], "[Gmail]/Sent Mail")
        self.assertEqual(gmail["trash"], "[Gmail]/Trash")
        self.assertEqual(gmail["junk"], "[Gmail]/Spam")

    def test_server_rules_and_excluded_from_sync_never_leak_into_tiers(self):
        # The >- folded multiline block (and its created/matches/action/why
        # keys), the inline "[]" list, and excluded_from_sync's own list
        # must all be walked over, not misparsed as tier entries.
        accounts = indexer.parse_config_tiers_text(FIXTURE_CONFIG)
        for leaked_key in ("created", "matches", "action", "why", "email",
                           "himalaya_account", "server_rules", "excluded_from_sync"):
            self.assertNotIn(leaked_key, accounts["icloud"])
            self.assertNotIn(leaked_key, accounts["gmail"])
        self.assertEqual(set(accounts.keys()), {"icloud", "gmail"})

    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(indexer.parse_config_tiers(Path("/nonexistent/config.yaml")), {})

    def test_load_tier_maps_falls_back_when_config_missing(self):
        tier_maps = indexer.load_tier_maps(Path("/nonexistent/config.yaml"), ["icloud", "gmail"])
        self.assertEqual(tier_maps["icloud"], indexer.DEFAULT_TIER_MAPS["icloud"])
        self.assertEqual(tier_maps["gmail"], indexer.DEFAULT_TIER_MAPS["gmail"])

    def test_load_tier_maps_uses_config_when_present(self):
        tmp = Path(tempfile.mkdtemp(prefix="email-triage-config-test-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cfg = tmp / "config.yaml"
        cfg.write_text(FIXTURE_CONFIG)
        tier_maps = indexer.load_tier_maps(cfg, ["icloud", "gmail"])
        self.assertEqual(tier_maps["icloud"]["cold"], "Archive")
        self.assertIsNone(tier_maps["gmail"]["cold"])
        self.assertEqual(tier_maps["gmail"]["cold_root"], "Archive")


# --------------------------------------------------------------------------
# classify_folder_tier (4.2): direct match, cold_root fallback, derived
# container, unrecognized -- unit-level, independent of any Maildir/db.
# --------------------------------------------------------------------------


class ClassifyFolderTierTest(unittest.TestCase):
    def test_direct_match_and_subtree(self):
        tier_map = {"untriaged": "INBOX", "cold": "Archive"}
        self.assertEqual(indexer.classify_folder_tier("INBOX", tier_map), "untriaged")
        self.assertEqual(indexer.classify_folder_tier("Archive", tier_map), "cold")
        self.assertEqual(indexer.classify_folder_tier("Archive/Housing/2020", tier_map), "cold")

    def test_all_mail_config_key_maps_to_allmail_tier_id(self):
        # 4.1's config key (all_mail) and the system-role tier id (allmail)
        # are spelled differently, deliberately.
        tier_map = {"all_mail": "[Gmail]/All Mail"}
        self.assertEqual(indexer.classify_folder_tier("[Gmail]/All Mail", tier_map), "allmail")

    def test_cold_root_used_only_when_cold_is_null(self):
        tier_map = {"cold": None, "cold_root": "Archive"}
        self.assertEqual(indexer.classify_folder_tier("Archive", tier_map), "cold")
        self.assertEqual(indexer.classify_folder_tier("Archive/Career/TWC", tier_map), "cold")

    def test_container_is_derived_from_ancestry_not_hardcoded(self):
        tier_map = {"sent": "[Gmail]/Sent Mail", "trash": "[Gmail]/Trash"}
        self.assertEqual(indexer.classify_folder_tier("[Gmail]", tier_map), "container")

    def test_bare_tier_root_classifies_as_tier_not_container(self):
        # 4.2 deliberately drops the old hardcoded ("[Gmail]", "Areas", ...)
        # container tuple: the matching rule (folder == name) now classifies
        # a bare "Areas" folder directly as the ongoing tier, not container
        # -- a documented behavior change from the pre-Phase-4 code.
        tier_map = {"ongoing": "Areas"}
        self.assertEqual(indexer.classify_folder_tier("Areas", tier_map), "ongoing")

    def test_unrecognized_when_nothing_matches(self):
        tier_map = {"untriaged": "INBOX"}
        self.assertEqual(indexer.classify_folder_tier("Newsletter", tier_map), "unrecognized")

    def test_null_tier_never_matches(self):
        tier_map = {"active": None}
        self.assertEqual(indexer.classify_folder_tier("Active", tier_map), "unrecognized")


# --------------------------------------------------------------------------
# priority-folders (4.3): config-driven, database-free.
# --------------------------------------------------------------------------


class PriorityFoldersTest(unittest.TestCase):
    def test_fast_set_order_and_null_skipped(self):
        tier_map = {"untriaged": "INBOX", "active": None, "sent": "Sent Messages", "drafts": "Drafts"}
        fast = [tier_map[k] for k in indexer.PRIORITY_FAST_TIER_KEYS if tier_map.get(k)]
        self.assertEqual(fast, ["INBOX", "Sent Messages", "Drafts"])

    def test_cmd_priority_folders_real_config_shape(self):
        import argparse
        import contextlib
        import io

        tmp = Path(tempfile.mkdtemp(prefix="email-triage-pf-test-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cfg = tmp / "config.yaml"
        cfg.write_text(FIXTURE_CONFIG)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            indexer.cmd_priority_folders(argparse.Namespace(config=cfg))
        result = json.loads(buf.getvalue())
        self.assertEqual(result["icloud"], {
            "fast": ["INBOX", "Active", "Sent Messages", "Drafts"],
            "slow_hint": "full channel",
        })
        self.assertEqual(result["gmail"]["fast"], ["INBOX", "Active", "[Gmail]/Sent Mail", "[Gmail]/Drafts"])

    def test_cmd_priority_folders_no_config_degrades_to_defaults(self):
        import argparse
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            indexer.cmd_priority_folders(argparse.Namespace(config=Path("/nonexistent/config.yaml")))
        result = json.loads(buf.getvalue())
        self.assertEqual(set(result.keys()), {"icloud", "gmail"})
        self.assertEqual(result["icloud"]["fast"], ["INBOX", "Active", "Sent Messages", "Drafts"])
        self.assertEqual(result["gmail"]["fast"], ["INBOX", "Active", "[Gmail]/Sent Mail", "[Gmail]/Drafts"])


if __name__ == "__main__":
    unittest.main()
