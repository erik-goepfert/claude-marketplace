#!/usr/bin/python3
"""Tests for sync.py: real temporary git repos, fake `claude`.

    /usr/bin/python3 -m unittest discover -s plugins/personal-knowledge-base/sync -v
"""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import sync
from config import Config

# Does what the headless run is asked to do, with plain git: commit both
# worktrees, merge into main, fast-forward back, push.
FAKE_CLAUDE_SYNC = """#!/bin/bash
set -e
commit_wt() {
  git -C "$1" add -A
  git -C "$1" diff --cached --quiet || git -C "$1" commit -qm "fake sync commit" --trailer "Origin: $2"
}
commit_wt "$FAKE_ICLOUD" icloud
commit_wt "$FAKE_CLAUDE" claude
for b in icloud claude; do
  if [ -n "$(git rev-list main..$b)" ]; then
    git merge -q --no-ff --no-commit $b >/dev/null
    git commit -qm "Merge $b into main" --trailer "Origin: $b"
  fi
done
git -C "$FAKE_ICLOUD" merge -q --ff-only main
git -C "$FAKE_CLAUDE" merge -q --ff-only main
git push -q origin main icloud claude 2>/dev/null
echo "fake: synced"
"""


def sh(*cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


class SyncTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        t = Path(self._tmp.name)
        sh("git", "init", "-q", "--bare", "-b", "main", str(t / "origin.git"))
        sh("git", "clone", "-q", str(t / "origin.git"), str(t / "main"))
        main = t / "main"
        for k, v in (("user.name", "Test"), ("user.email", "test@example.com")):
            sh("git", "config", k, v, cwd=main)
        (main / "Note.md").write_text("hello\n")
        sh("git", "add", "-A", cwd=main)
        sh("git", "commit", "-qm", "seed", cwd=main)
        sh("git", "branch", "icloud", cwd=main)
        sh("git", "branch", "claude", cwd=main)
        sh("git", "push", "-q", "origin", "main", "icloud", "claude", cwd=main)
        sh("git", "worktree", "add", "-q", str(t / "icloud"), "icloud", cwd=main)
        sh("git", "worktree", "add", "-q", str(t / "claude"), "claude", cwd=main)
        sh("git", "fetch", "-q", "origin", cwd=main)

        self.t = t
        self.cfg = Config(
            main=main, claude=t / "claude", icloud=t / "icloud",
            log=t / "log", state_dir=t / "state",
            claude_bin=self.fake_claude(FAKE_CLAUDE_SYNC),
            claude_live_symlink=t / "bin" / "no-such-live-claude-symlink",
            lock_wait_seconds=1, git_timeout=2, claude_timeout=5,
            extra_env={"FAKE_ICLOUD": str(t / "icloud"), "FAKE_CLAUDE": str(t / "claude")},
        )

    def tearDown(self):
        self._tmp.cleanup()

    # --- helpers ---------------------------------------------------------------

    def fake_claude(self, script, name="claude"):
        (self.t / "bin").mkdir(exist_ok=True)
        path = self.t / "bin" / name
        path.write_text(textwrap.dedent(script))
        path.chmod(0o755)
        return str(path)

    def run_sync(self, timer=False):
        return sync.main(["--timer"] if timer else [], cfg=self.cfg)

    def log(self):
        return self.cfg.log.read_text() if self.cfg.log.exists() else ""

    def failure(self, mode):
        f = self.cfg.state_dir / f"failed-{mode}"
        return f.read_text() if f.exists() else None

    def tip(self, ref):
        return subprocess.run(["git", "-C", str(self.cfg.main), "rev-parse", ref],
                              capture_output=True, text=True, check=True).stdout.strip()

    def phone_edit(self, age=0, name="Note.md", text="phone edit\n"):
        p = self.cfg.icloud / name
        p.write_text(text)
        t = time.time() - age
        os.utime(p, (t, t))

    def seed_maintenance_note(self):
        projects = self.cfg.claude / "Projects"
        projects.mkdir(exist_ok=True)
        note = projects / "Personal knowledge base setup.md"
        note.write_text("Intro.\n\n## Maintenance\n\n- existing bullet\n\n## Vault setup\n\n- x\n")
        return note

    def set_live_claude(self, version):
        target = self.t / "bin" / version
        target.write_text("#!/bin/bash\n")
        symlink = self.t / "bin" / "live-claude"
        symlink.symlink_to(target)
        self.cfg.claude_live_symlink = symlink

    def assert_all_synced(self):
        tips = {self.tip(r) for r in ("main", "icloud", "claude",
                                      "origin/main", "origin/icloud", "origin/claude")}
        self.assertEqual(len(tips), 1)

    # --- tests -----------------------------------------------------------------

    def test_nothing_to_do_is_silent(self):
        self.assertEqual(self.run_sync(timer=True), 0)
        self.assertEqual(self.log(), "")
        self.assertTrue((self.cfg.state_dir / "last-check").exists())

    def test_timer_defers_fresh_phone_edit(self):
        self.phone_edit(age=0)
        self.assertEqual(self.run_sync(timer=True), 0)
        self.assertIn("deferred", self.log())
        self.assertNotIn("sync start", self.log())

    def test_timer_syncs_settled_phone_edit(self):
        self.phone_edit(age=600)
        self.assertEqual(self.run_sync(timer=True), 0)
        self.assertIn("fake: synced", self.log())
        self.assert_all_synced()
        self.assertEqual((self.cfg.claude / "Note.md").read_text(), "phone edit\n")

    def test_on_demand_ignores_quiet_window(self):
        self.phone_edit(age=0)
        self.assertEqual(self.run_sync(), 0)
        self.assert_all_synced()

    def test_claude_edit_syncs_to_icloud(self):
        (self.cfg.claude / "New note.md").write_text("from claude\n")
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual((self.cfg.icloud / "New note.md").read_text(), "from claude\n")
        self.assert_all_synced()

    def test_committed_but_unmerged_branch_is_detected(self):
        # Regression: the bash version's single `rev-list a..b c..d ...` call
        # always came back empty, so this only synced if the push check caught it.
        (self.cfg.claude / "x.md").write_text("x\n")
        sh("git", "add", "-A", cwd=self.cfg.claude)
        sh("git", "commit", "-qm", "x", cwd=self.cfg.claude)
        sh("git", "push", "-q", "origin", "claude", cwd=self.cfg.claude)
        pending = sync.Sync(self.cfg, "ondemand").check_pending()
        self.assertTrue(pending.diverged)
        self.assertEqual(pending.unpushed, [])

    def test_timer_defers_while_icloud_downloads(self):
        self.phone_edit(age=600)
        with mock.patch("sync.is_dataless", return_value=True):
            self.assertEqual(self.run_sync(timer=True), 0)
        self.assertIn("still downloading 1 file(s): Note.md", self.log())
        self.assertNotIn("sync start", self.log())
        self.assertTrue((self.cfg.state_dir / "dataless-since").exists())

    def test_dataless_for_too_long_is_a_failure(self):
        self.phone_edit(age=600)
        self.cfg.state_dir.mkdir(parents=True)
        (self.cfg.state_dir / "dataless-since").write_text(str(time.time() - 3600))
        with mock.patch("sync.is_dataless", return_value=True):
            self.assertEqual(self.run_sync(timer=True), 1)
        self.assertIn("Keep Downloaded", self.failure("timer"))

    def test_on_demand_waits_for_download_then_syncs(self):
        self.phone_edit(age=0)
        self.cfg.dataless_wait_seconds = 10
        calls = []

        def downloading_at_first(st):
            calls.append(st)
            return len(calls) == 1

        with mock.patch("sync.is_dataless", side_effect=downloading_at_first):
            self.assertEqual(self.run_sync(), 0)
        self.assert_all_synced()
        self.assertFalse((self.cfg.state_dir / "dataless-since").exists())

    def test_on_demand_gives_up_waiting_and_says_so(self):
        self.phone_edit(age=0)
        self.cfg.dataless_wait_seconds = 1
        err = io.StringIO()
        with mock.patch("sync.is_dataless", return_value=True), redirect_stderr(err):
            self.assertEqual(self.run_sync(), 0)
        self.assertIn("still downloading", err.getvalue())
        self.assertNotIn("sync start", self.log())

    def test_uncommitted_phone_change_fails_postcheck(self):
        # Syncs everything except the iCloud worktree's changes.
        self.cfg.claude_bin = self.fake_claude(
            FAKE_CLAUDE_SYNC.replace('commit_wt "$FAKE_ICLOUD" icloud\n', ''),
            "partial")
        self.phone_edit(age=600)
        self.assertEqual(self.run_sync(), 1)
        self.assertIn("phone changes uncommitted: Note.md", self.failure("ondemand"))

    def test_merge_without_origin_trailer_fails_postcheck(self):
        # The bug this guards against: Origin passed as a separate -m paragraph.
        self.cfg.claude_bin = self.fake_claude(
            FAKE_CLAUDE_SYNC.replace(
                'git commit -qm "Merge $b into main" --trailer "Origin: $b"',
                'git commit -qm "Merge $b into main" -m "Origin: $b" -m "Co-Authored-By: X <x@example.com>"'),
            "notrailer")
        self.phone_edit(age=600)
        self.assertEqual(self.run_sync(), 1)
        failure = self.failure("ondemand")
        self.assertIn("without a readable `Origin:` trailer", failure)
        self.assertIn("Merge icloud into main", failure)
        self.assertNotIn("fake sync commit", failure)  # the content commit had it

    def test_synced_commits_carry_origin(self):
        self.phone_edit(age=600)
        (self.cfg.claude / "New note.md").write_text("from claude\n")
        self.assertEqual(self.run_sync(), 0)
        out = subprocess.run(
            ["git", "-C", str(self.cfg.main), "log", "--format=%(trailers:key=Origin,valueonly,separator=)",
             "HEAD~2..HEAD"], capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(sorted(out), ["claude", "claude", "icloud", "icloud"])

    def test_phone_edit_landing_mid_run_is_fine(self):
        self.cfg.claude_bin = self.fake_claude(
            FAKE_CLAUDE_SYNC + 'sleep 0.1; echo "typed during sync" >> "$FAKE_ICLOUD/Note.md"\n',
            "racy")
        self.phone_edit(age=600)
        self.assertEqual(self.run_sync(), 0)
        self.assertIsNone(self.failure("ondemand"))

    def test_headless_failure_is_recorded(self):
        self.cfg.claude_bin = self.fake_claude("#!/bin/bash\necho boom\nexit 3\n", "bad")
        self.phone_edit(age=600)
        self.assertEqual(self.run_sync(), 1)
        self.assertIn("exited 3", self.failure("ondemand"))

    def test_headless_doing_nothing_fails_postcheck(self):
        self.cfg.claude_bin = self.fake_claude("#!/bin/bash\necho 'all done!'\n", "lazy")
        (self.cfg.claude / "New note.md").write_text("from claude\n")
        self.assertEqual(self.run_sync(), 1)
        self.assertIn("left work undone", self.failure("ondemand"))

    def test_headless_timeout_kills_it(self):
        self.cfg.claude_bin = self.fake_claude("#!/bin/bash\nsleep 60\n", "slow")
        self.cfg.claude_timeout = 1
        self.phone_edit(age=600)
        self.assertEqual(self.run_sync(), 1)
        self.assertIn("TIMED OUT", self.failure("ondemand"))

    def test_git_hang_times_out_with_privacy_hint(self):
        hang = self.fake_claude(
            f'#!/bin/bash\n[[ "$*" == *"{self.cfg.icloud}"* ]] && exec sleep 60\nexec git "$@"\n',
            "hanggit")
        self.cfg.git_bin = hang
        start = time.monotonic()
        self.assertEqual(self.run_sync(timer=True), 1)
        self.assertLess(time.monotonic() - start, 10)
        self.assertIn("TIMED OUT", self.failure("timer"))
        self.assertIn("privacy (TCC) prompt for `git`", self.failure("timer"))

    def test_lock_timer_skips_on_demand_fails(self):
        import fcntl
        self.cfg.state_dir.mkdir(parents=True)
        with open(self.cfg.state_dir / "sync.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(self.run_sync(timer=True), 0)
            self.assertIn("skipped", self.log())
            self.assertEqual(self.run_sync(), 1)
            self.assertIn("waited", self.failure("ondemand"))

    def test_clean_run_clears_only_its_own_failure(self):
        self.cfg.state_dir.mkdir(parents=True)
        (self.cfg.state_dir / "failed-timer").write_text("old timer failure\n")
        (self.cfg.state_dir / "failed-ondemand").write_text("old on-demand failure\n")
        self.assertEqual(self.run_sync(timer=True), 0)
        self.assertIsNone(self.failure("timer"))
        self.assertIsNotNone(self.failure("ondemand"))

    def test_claude_version_pin_notices_update(self):
        note = self.seed_maintenance_note()
        self.set_live_claude("9.9.9")
        self.assertEqual(self.run_sync(), 0)
        text = note.read_text()
        self.assertIn("Claude Code CLI updated to `9.9.9`", text)
        self.assertIn("#task/inbox", text)
        self.assert_all_synced()

    def test_claude_version_pin_does_not_repeat(self):
        note = self.seed_maintenance_note()
        self.set_live_claude("9.9.9")
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual(note.read_text().count("Claude Code CLI updated to `9.9.9`"), 1)

    def test_claude_version_pin_failed_commit_leaves_no_duplicate(self):
        note = self.seed_maintenance_note()
        before = note.read_text()
        self.set_live_claude("9.9.9")
        self.cfg.git_bin = self.fake_claude(
            '#!/bin/bash\n[[ " $* " == *" commit "* ]] && { echo boom >&2; exit 128; }\nexec git "$@"\n',
            "nocommit")
        self.assertEqual(self.run_sync(), 1)
        self.assertEqual(note.read_text(), before)
        self.cfg.git_bin = "git"
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual(note.read_text().count("Claude Code CLI updated to `9.9.9`"), 1)

    def test_claude_version_pin_matching_is_silent(self):
        symlink = self.t / "bin" / "live-claude-matching"
        symlink.symlink_to(self.cfg.claude_bin)  # basename ("claude") matches the pin
        self.cfg.claude_live_symlink = symlink
        self.assertEqual(self.run_sync(timer=True), 0)
        self.assertEqual(self.log(), "")

    def test_emoji_filenames(self):
        self.phone_edit(age=600, name="Hello 🌎.md", text="hi\n")
        self.assertEqual(self.run_sync(timer=True), 0)
        self.assertEqual((self.cfg.claude / "Hello 🌎.md").read_text(), "hi\n")


class ParseTest(unittest.TestCase):
    def test_porcelain_z(self):
        out = " M a.md\0?? dir/b c.md\0R  new.md\0old.md\0"
        self.assertEqual(sync.parse_porcelain_z(out), ["a.md", "dir/b c.md", "new.md"])


if __name__ == "__main__":
    unittest.main()
