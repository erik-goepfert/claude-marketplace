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
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import session  # noqa: E402

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

    def run_ses(self, *argv):
        buf = StringIO()
        with redirect_stdout(buf):
            code = session.main(["--data-dir", str(self.tmp),
                                 "--mbsyncrc", "/dev/null",
                                 "--mbsync", str(self.stub), *argv])
        return code, json.loads(buf.getvalue())

    def test_start_bundles_config_lock_stamp_priority(self):
        (self.tmp / ".fullsync-done").touch()  # stale marker from last time
        code, out = self.run_ses("start")
        self.assertEqual(code, 0)
        self.assertEqual(out["lock"], "acquired")
        self.assertTrue((self.tmp / "session.lock").is_dir())
        self.assertRegex(out["session_started_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertIn("untriaged: INBOX", out["config"])
        self.assertEqual(out["priority_folders"]["acct"]["mbsync_fast"],
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
