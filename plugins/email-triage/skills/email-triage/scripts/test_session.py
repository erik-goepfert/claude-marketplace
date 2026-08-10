#!/usr/bin/env python3
"""Tests for session.py -- the session lifecycle script. Temp data dir +
stub mbsync only; never touches real data or servers."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from io import StringIO
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import session  # noqa: E402
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

# One line per argument, so a test can prove the box spec arrived as ONE
# argument. `echo "$@"` cannot show that: it joins with spaces, so a spec that
# had been word-split would print identically to one that had not.
ARGV_STUB = """#!/bin/sh
for a in "$@"; do echo "ARG:$a" >> "$STUBLOG"; done
exit 0
"""

ALWAYS_FAIL_STUB = """#!/bin/sh
echo 'far side box <script>alert(1)</script> cannot be opened' >&2
exit 1
"""

# Two accounts and a bracketed, space-bearing folder name -- the shapes that
# made hand-quoting a hazard: `[` and `]` are shell globs, and the space would
# split the argument.
CONFIG_TWO = """\
accounts:
  acct:
    tiers:
      untriaged: INBOX
      active: Active
      sent: "[Gmail]/Sent Mail"
      drafts: Drafts
  other:
    tiers:
      untriaged: INBOX
      active: Live
      sent: Sent Messages
      drafts: Drafts
"""


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="email-triage-session-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        (self.tmp / "config.yaml").write_text(CONFIG_TEXT)
        self.stublog = self.tmp / "mbsync.log"
        self.stub = self.tmp / "mbsync-stub"
        self.stub.write_text(MBSYNC_STUB)
        self.stub.chmod(0o755)
        os.environ["STUBLOG"] = str(self.stublog)
        os.environ.pop("FAIL_ONCE_MARKER", None)

    def run_ses(self, *argv, stub=None):
        buf = StringIO()
        with redirect_stdout(buf):
            code = session.main(["--data-dir", str(self.tmp),
                                 "--mbsyncrc", "/dev/null",
                                 "--mbsync", str(stub or self.stub), *argv])
        return code, json.loads(buf.getvalue())

    def write_stub(self, name, text):
        path = self.tmp / name
        path.write_text(text)
        path.chmod(0o755)
        return path

    def write_config(self, text) -> list:
        (self.tmp / "alt.yaml").write_text(text)
        return ["--config", str(self.tmp / "alt.yaml")]

    def test_start_bundles_config_lock_stamp_priority(self):
        (self.tmp / ".fullsync-done").touch()  # stale marker from last time
        code, out = self.run_ses("start")
        self.assertEqual(code, 0)
        self.assertEqual(out["lock"], "acquired")
        self.assertTrue((self.tmp / "session.lock").is_dir())
        self.assertRegex(out["session_started_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertIn("untriaged: INBOX", out["config"])
        self.assertEqual(out["priority_folders"]["acct"]["mbsync_spec"],
                         "acct:INBOX,Active,Sent,Drafts")
        self.assertTrue(out["fullsync_marker_cleared"])
        self.assertFalse((self.tmp / ".fullsync-done").exists())

    def test_start_with_lock_held_touches_nothing(self):
        (self.tmp / "session.lock").mkdir()
        (self.tmp / ".fullsync-done").touch()
        code, out = self.run_ses("start")
        self.assertEqual(code, 1)
        self.assertEqual(out["lock"], "held")
        self.assertIn("hint", out)
        # marker survives: a held lock means we did NOT start a session
        self.assertTrue((self.tmp / ".fullsync-done").exists())
        # R8: held-lock output is minimal -- no config/priority dump, no
        # session stamp (nothing started)
        self.assertNotIn("config", out)
        self.assertNotIn("priority_folders", out)
        self.assertNotIn("session_started_at", out)

    def test_unlock(self):
        (self.tmp / "session.lock").mkdir()
        code, out = self.run_ses("unlock")
        self.assertEqual(code, 0)
        self.assertFalse((self.tmp / "session.lock").exists())
        code, out = self.run_ses("unlock")  # idempotent
        self.assertEqual(code, 0)

    def test_sync_background_touches_marker_and_check_gates(self):
        code, out = self.run_ses("check-fullsync")
        self.assertEqual(code, 1)
        self.assertFalse(out["fullsync_done"])

        code, out = self.run_ses("sync-background")
        self.assertEqual(code, 0)
        self.assertEqual(out["accounts"], ["acct"])
        marker = self.tmp / ".fullsync-done"
        self.assertTrue(wait_for(marker.is_file), "marker never appeared")
        self.assertIn("acct", self.stublog.read_text())

        code, out = self.run_ses("check-fullsync")
        self.assertEqual(code, 0)
        self.assertTrue(out["fullsync_done"])

    def test_sync_background_retries_flake_once(self):
        os.environ["FAIL_ONCE_MARKER"] = str(self.tmp / "failed-once")
        self.addCleanup(os.environ.pop, "FAIL_ONCE_MARKER", None)
        self.run_ses("sync-background")
        marker = self.tmp / ".fullsync-done"
        self.assertTrue(wait_for(marker.is_file), "marker never appeared after retry")
        self.assertEqual(len(self.stublog.read_text().splitlines()), 2)

    # -- `sync --scope` (2026-08-10): Stage 1 stops being ad-hoc shell ------
    #
    # The point of these is that the caller supplies intent (`priority` or
    # `full`, optionally an account) and NEVER a folder list. Everything about
    # which boxes to sync is config's business, so these tests assert on what
    # reached mbsync's argv.

    def test_priority_syncs_the_config_boxes_and_reports_per_account(self):
        code, out = self.run_ses("sync", "--scope", "priority")
        self.assertEqual(code, 0)
        self.assertEqual(out["scope"], "priority")
        self.assertTrue(out["all_ok"])
        self.assertEqual(out["synced"], [{
            "account": "acct",
            "boxes": ["INBOX", "Active", "Sent", "Drafts"],
            "attempts": 1, "ok": True,
        }])
        self.assertEqual(self.stublog.read_text().strip(),
                         "-c /dev/null acct:INBOX,Active,Sent,Drafts")

    def test_priority_is_finished_before_it_returns(self):
        """The foreground guarantee, and the reason scope decides detachment:
        classification runs on this data, so `sync` returning while mbsync is
        still walking would hand back last session's mirror. Note the absence
        of `wait_for` -- sync-background's test needs one, this must not."""
        self.run_ses("sync", "--scope", "priority")
        self.assertIn("acct:INBOX", self.stublog.read_text())

    def test_priority_retries_a_flake_once(self):
        os.environ["FAIL_ONCE_MARKER"] = str(self.tmp / "failed-once")
        self.addCleanup(os.environ.pop, "FAIL_ONCE_MARKER", None)
        code, out = self.run_ses("sync", "--scope", "priority")
        self.assertEqual(code, 0)
        self.assertTrue(out["all_ok"])
        self.assertEqual(out["synced"][0]["attempts"], 2)
        self.assertEqual(len(self.stublog.read_text().splitlines()), 2)

    def test_priority_failure_exits_1_and_says_the_mirror_is_stale(self):
        """Stage 1 had no retry and no failure signal at all while it was a
        hand-typed command: a flake left the mirror stale and classification
        proceeded on it anyway."""
        stub = self.write_stub("mbsync-fail", ALWAYS_FAIL_STUB)
        code, out = self.run_ses("sync", "--scope", "priority", stub=stub)
        self.assertEqual(code, 1)
        self.assertFalse(out["all_ok"])
        self.assertFalse(out["synced"][0]["ok"])
        self.assertEqual(out["synced"][0]["attempts"], 2)
        self.assertIn("stale", out["hint"])

    def test_failed_attempt_stderr_is_escaped_and_declared(self):
        """mbsync's own text, about folders a sender's mail sits in -- same
        call as batch.py's `sync.stderr`, so default-deny must reach it."""
        stub = self.write_stub("mbsync-fail", ALWAYS_FAIL_STUB)
        _code, out = self.run_ses("sync", "--scope", "priority", stub=stub)
        self.assertIn("synced.stderr", out["_trust"]["untrusted_fields"])
        self.assertNotIn("<script>", out["synced"][0]["stderr"])
        self.assertIn("&lt;script&gt;", out["synced"][0]["stderr"])

    def test_box_spec_reaches_mbsync_as_one_unsplit_argument(self):
        """The hazard the command removes. `[Gmail]/Sent Mail` carries shell
        globs AND a space; it used to be hand-quoted in a typed command, where
        one missing quote splits the spec or globs the brackets. argv has no
        such failure mode -- assert it really is argv."""
        stub = self.write_stub("mbsync-argv", ARGV_STUB)
        cfg = self.write_config(CONFIG_TWO)
        code, _out = self.run_ses("sync", "--scope", "priority",
                                  "--account", "acct", *cfg, stub=stub)
        self.assertEqual(code, 0)
        self.assertIn("ARG:acct:INBOX,Active,[Gmail]/Sent Mail,Drafts",
                      self.stublog.read_text().splitlines())

    def test_account_scopes_the_sync_to_one_account(self):
        cfg = self.write_config(CONFIG_TWO)
        code, out = self.run_ses("sync", "--scope", "priority",
                                 "--account", "other", *cfg)
        self.assertEqual(code, 0)
        self.assertEqual([r["account"] for r in out["synced"]], ["other"])
        self.assertIn("other:INBOX,Live,Sent Messages,Drafts",
                      self.stublog.read_text())
        self.assertNotIn("acct:", self.stublog.read_text())

    def test_unknown_account_is_reported_not_a_silent_success(self):
        """`--account typo` must not sync nothing and call it done."""
        code, out = self.run_ses("sync", "--scope", "priority",
                                 "--account", "nope")
        self.assertEqual(code, 1)
        self.assertFalse(out["all_ok"])
        self.assertEqual(out["unknown_accounts"], ["nope"])
        self.assertFalse(self.stublog.exists())

    def test_full_syncs_whole_channels_so_the_scopes_nest(self):
        """Why the names are inclusive rather than hot/cold: `full` re-covers
        the priority boxes, INBOX included. A bare channel name, no box spec.
        Mail arriving during Stage 1 reaches the index only because of this."""
        stub = self.write_stub("mbsync-argv", ARGV_STUB)
        code, out = self.run_ses("sync", "--scope", "full", stub=stub)
        self.assertEqual(code, 0)
        self.assertEqual(out["scope"], "full")
        self.assertTrue(wait_for(lambda: self.stublog.is_file()))
        self.assertTrue(wait_for(
            lambda: "ARG:acct" in self.stublog.read_text().splitlines()))
        self.assertNotIn("ARG:acct:INBOX", self.stublog.read_text())

    def test_scope_is_required(self):
        """No default: `priority` would silently half-sync, `full` would block
        the session for minutes. Making the caller say which is the feature."""
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                session.main(["--data-dir", str(self.tmp), "sync"])
        self.assertEqual(ctx.exception.code, 2)

    def test_sync_background_is_an_alias_for_scope_full(self):
        _code, out = self.run_ses("sync-background")
        self.assertEqual(out["scope"], "full")
        self.assertEqual(out["stage2"], "launched")

    def test_end_releases_lock_only_on_success(self):
        (self.tmp / "session.lock").mkdir()
        code, out = self.run_ses("end")
        self.assertEqual(code, 0)
        lock = self.tmp / "session.lock"
        self.assertTrue(wait_for(lambda: not lock.exists()), "lock never released")
        self.assertTrue((self.tmp / "endsync.log").is_file())

    def test_end_failure_leaves_lock_for_next_start(self):
        (self.tmp / "session.lock").mkdir()
        # Stub that always fails -> retry also fails -> rmdir never runs.
        always_fail = self.tmp / "mbsync-fail"
        always_fail.write_text("#!/bin/sh\nexit 1\n")
        always_fail.chmod(0o755)
        buf = StringIO()
        with redirect_stdout(buf):
            session.main(["--data-dir", str(self.tmp), "--mbsyncrc", "/dev/null",
                          "--mbsync", str(always_fail), "end"])
        time.sleep(0.5)
        self.assertTrue((self.tmp / "session.lock").is_dir())
        code, out = self.run_ses("start")
        self.assertEqual(code, 1)
        self.assertEqual(out["lock"], "held")

    # -- Phase 6.2: escape + `_trust` manifest, applied centrally in emit() --

    def test_start_manifest_is_complete_and_config_stays_bare(self):
        code, out = self.run_ses("start")
        self.assertEqual(code, 0)
        undeclared = (untrusted.string_paths(out)
                      - {p for p in untrusted.string_paths(out)
                         if p.startswith("_trust")}
                      - set(session.TRUSTED_FIELDS)
                      - {p for p in untrusted.string_paths(out)
                         if any(p == t or p.startswith(t + ".")
                                for t in session.TRUSTED_SUBTREES)}
                      - set(out["_trust"]["untrusted_fields"]))
        self.assertEqual(undeclared, set())
        # config.yaml is the USER's text, not a sender's -- escaping it would
        # corrupt the one field the model reads to learn the tier names.
        self.assertEqual(out["_trust"]["untrusted_fields"], [])

    def test_priority_folders_subtree_is_trusted_despite_dynamic_keys(self):
        # Keyed by account name, so no exact allowlist can name the paths. The
        # subtree escape is legitimate here precisely because the keys come
        # from config, never from a sender.
        _code, out = self.run_ses("start")
        self.assertTrue(out["priority_folders"])
        for path in untrusted.string_paths(out):
            if path.startswith("priority_folders"):
                self.assertNotIn(path, out["_trust"]["untrusted_fields"])

    def test_log_tail_is_escaped_and_declared(self):
        """mbsync's own output is the one untrusted thing this script emits: an
        external process's text, reporting on data senders produced, in no
        vocabulary we control."""
        (self.tmp / "fullsync.log").write_text(
            "Reading configuration file /dev/null\n"
            "far side box <script>alert(1)</script> cannot be opened\n"
            "C: 1/1 B: 0/23 M: 0/0 S: 0/0\n")
        code, out = self.run_ses("check-fullsync")
        self.assertEqual(code, 1)  # marker absent
        self.assertFalse(out["fullsync_done"])
        self.assertIn("log_tail", out["_trust"]["untrusted_fields"])
        joined = " ".join(out["log_tail"])
        self.assertNotIn("<script>", joined)
        self.assertIn("&lt;script&gt;", joined)

    def test_log_tail_absent_once_the_marker_lands(self):
        (self.tmp / ".fullsync-done").touch()
        code, out = self.run_ses("check-fullsync")
        self.assertEqual(code, 0)
        self.assertNotIn("log_tail", out)
        self.assertEqual(out["_trust"]["untrusted_fields"], [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
