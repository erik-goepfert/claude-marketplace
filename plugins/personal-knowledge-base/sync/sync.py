#!/usr/bin/python3
"""One personal-knowledge-base sync cycle -- the single entry point.

Called by the launchd timer (--timer), by the plugin's hooks, and by
/personal-knowledge-base-sync. Reconciles the `icloud` and `claude` worktrees
through `main` and pushes to GitHub (design: erik-goepfert-private
projects/personal-knowledge-base/README.md).

    1. Lock      -- one cycle at a time, however it was started.
    2. Precheck  -- plain git, no model: is there anything to do at all? Are
       the phone's changes fully downloaded by iCloud yet?
    3. Headless `claude -p` does the commits/merges/push, in dontAsk mode with
       only `Bash(git *)` pre-approved: any other tool call is denied, so note
       content can't steer it beyond git.
    4. Postcheck -- trust the repo, not the run's summary.

Modes:
    --timer     launchd backstop. Skips if a cycle is running; defers while
                phone edits are still arriving (quiet window).
    (default)   on demand. Waits for a running cycle; ignores the quiet window.

Every step has a time limit, so a hang (e.g. a macOS privacy prompt nobody
sees) becomes a logged failure that releases the lock. Failures are recorded in
<state_dir>/failed-<mode>, cleared by that mode's next clean run; the plugin's
hook and skill surface them to Claude.

Exit codes: 0 synced / nothing to do / deferred / skipped, 1 failed.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import BRANCHES, Config  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# st_flags bit for a file whose contents iCloud hasn't downloaded: names and
# metadata only, and reading it fails ("Resource deadlock avoided"). Happens
# for a few seconds whenever a phone edit arrives (metadata first), and for
# good when macOS offloads a file to save space. (stat.SF_DATALESS, which
# Python only names from 3.13.)
SF_DATALESS = 0x40000000


def is_dataless(st):
    return bool(st.st_flags & SF_DATALESS)


class SyncFailure(Exception):
    pass


class _Alarm(Exception):
    pass


def timestamp():
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def timed_out(what, secs, prog, icloud):
    msg = f"TIMED OUT after {secs:g}s: {what}."
    if icloud:
        msg += (f" Likely a macOS privacy (TCC) prompt for `{prog}`: check for a prompt "
                "on the Mac, or System Settings > Privacy & Security > Files and Folders.")
    return msg


def parse_porcelain_z(out):
    """Paths from `git status --porcelain -z` (renames/copies carry a second,
    original path, which is skipped)."""
    paths, entries, i = [], out.split("\0"), 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if not entry:
            continue
        paths.append(entry[3:])
        if "R" in entry[:2] or "C" in entry[:2]:
            i += 1
    return paths


@dataclass
class Pending:
    icloud: list      # changed paths in the iCloud worktree
    claude: list      # changed paths in the claude worktree
    diverged: bool    # main/icloud/claude don't all point at one commit
    unpushed: list    # branches whose tip differs from origin's
    main_tip: str     # main's commit when checked

    def anything(self):
        return bool(self.icloud or self.claude or self.diverged or self.unpushed)


class Sync:
    # Where the version-pin notice goes when the live Claude Code CLI drifts
    # from config.py's pinned claude_bin (see check_claude_version_pin).
    MAINTENANCE_NOTE = "Projects/Personal knowledge base setup.md"
    MAINTENANCE_ANCHOR = "## Maintenance\n\n"

    def __init__(self, cfg: Config, mode: str):
        self.cfg, self.mode = cfg, mode
        self.fail_file = cfg.state_dir / f"failed-{mode}"
        self.stamp_file = cfg.state_dir / "last-check"
        self.lock_file = cfg.state_dir / "sync.lock"
        self.dataless_since_file = cfg.state_dir / "dataless-since"

    # --- plumbing ------------------------------------------------------------

    def log(self, msg):
        self.cfg.log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cfg.log, "a", encoding="utf-8") as f:
            f.write(f"{timestamp()} [{self.mode}] {msg}\n")

    def git(self, cwd, *args, what):
        try:
            r = subprocess.run(
                [self.cfg.git_bin, "-C", str(cwd), *args],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=self.cfg.git_timeout,
                encoding="utf-8", errors="surrogateescape",  # launchd may have no locale
            )
        except subprocess.TimeoutExpired:
            raise SyncFailure(timed_out(what, self.cfg.git_timeout, "git",
                                        icloud=cwd == self.cfg.icloud))
        if r.returncode != 0:
            raise SyncFailure(f"{what} (exit {r.returncode}): {r.stderr.strip()}")
        return r.stdout

    @contextlib.contextmanager
    def deadline(self, what, prog):
        """Time limit for this process's own file access (no child to kill).
        A syscall blocked on a privacy decision is interruptible by signals."""
        def on_alarm(signum, frame):
            raise _Alarm()
        old = signal.signal(signal.SIGALRM, on_alarm)
        signal.setitimer(signal.ITIMER_REAL, self.cfg.git_timeout)
        try:
            yield
        except _Alarm:
            raise SyncFailure(timed_out(what, self.cfg.git_timeout, prog, icloud=True))
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)

    def acquire_lock(self):
        """flock on sync.lock: timer doesn't wait, on demand waits a while."""
        fd = open(self.lock_file, "w")
        give_up = time.monotonic() + (0 if self.mode == "timer" else self.cfg.lock_wait_seconds)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= give_up:
                    fd.close()
                    return None
                time.sleep(0.5)

    # --- the cycle -------------------------------------------------------------

    def check_claude_version_pin(self):
        """If Claude Code's own updater has moved past the version sync.py is
        pinned to, leave a task in the vault so Erik notices from his phone --
        the headless run can't author note content itself (only `Bash(git *)`
        is allowed), so this writes and commits the note directly, once per
        new live version seen."""
        cfg = self.cfg
        pinned = os.path.basename(cfg.claude_bin)
        try:
            live = os.path.basename(os.readlink(cfg.claude_live_symlink))
        except OSError:
            return  # not on the Mac, symlink missing, etc.
        if live == pinned:
            return
        notified = cfg.state_dir / "claude-version-notified"
        with contextlib.suppress(FileNotFoundError):
            if notified.read_text().strip() == live:
                return  # already flagged this version
        note_path = cfg.claude / self.MAINTENANCE_NOTE
        try:
            text = note_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return  # note's gone missing; don't guess where else this goes
        if self.MAINTENANCE_ANCHOR not in text:
            return  # section's gone missing; don't guess where to insert
        task = (f"- [ ] Claude Code CLI updated to `{live}` (sync still pinned to `{pinned}`) "
                f"— approve it in System Settings → Privacy & Security → Files and Folders, "
                f"then bump `sync/config.py`'s `claude_bin` #task/inbox\n")
        note_path.write_text(text.replace(self.MAINTENANCE_ANCHOR, self.MAINTENANCE_ANCHOR + task, 1),
                              encoding="utf-8")
        self.git(cfg.claude, "add", self.MAINTENANCE_NOTE, what="staging the version-pin notice")
        self.git(cfg.claude, "commit", "-m", f"Note: Claude Code CLI updated to {live}",
                  "--trailer", "Origin: claude", what="committing the version-pin notice")
        notified.write_text(live, encoding="utf-8")
        self.log(f"noted: Claude Code CLI updated to {live}, sync still pinned to {pinned}")

    def check_pending(self) -> Pending:
        cfg = self.cfg
        status = ("status", "--porcelain", "-z", "--untracked-files=all")
        icloud = parse_porcelain_z(self.git(cfg.icloud, *status, what="git status in the iCloud vault"))
        claude = parse_porcelain_z(self.git(cfg.claude, *status, what="git status in the claude worktree"))
        refs = {}
        out = self.git(cfg.main, "for-each-ref", "--format=%(refname) %(objectname)",
                       "refs/heads", "refs/remotes/origin", what="git for-each-ref in the main repo")
        for line in out.splitlines():
            name, sha = line.split(" ", 1)
            refs[name] = sha
        tips = {refs.get(f"refs/heads/{b}") for b in BRANCHES}
        unpushed = [b for b in BRANCHES
                    if refs.get(f"refs/heads/{b}") != refs.get(f"refs/remotes/origin/{b}")]
        return Pending(icloud, claude, diverged=len(tips) != 1, unpushed=unpushed,
                       main_tip=refs.get("refs/heads/main", ""))

    def commits_missing_origin(self, since):
        """New commits on main (since `since`) without a readable Origin: trailer."""
        out = self.git(self.cfg.main, "log", "--format=%h%x00%(trailers:key=Origin,valueonly,separator=,)%x00%s",
                       f"{since}..main", what="git log in the main repo")
        missing = []
        for line in out.splitlines():
            sha, origin, subject = line.split("\0", 2)
            if not origin.strip():
                missing.append(f"{sha} {subject}")
        return missing

    def icloud_stats(self, paths):
        """{path: (mtime, dataless)} for changed iCloud files that still exist
        (deleted ones are skipped). Metadata only -- works on dataless files.
        Read here, not via a `stat` child, so no extra program needs approval."""
        stats = {}
        with self.deadline("reading file metadata in the iCloud vault", "python3"):
            for p in paths:
                try:
                    st = (self.cfg.icloud / p).stat()
                except FileNotFoundError:
                    continue  # deleted on the phone
                stats[p] = (st.st_mtime, is_dataless(st))
        return stats

    def wait_for_downloads(self, pending):
        """Deal with changed iCloud files that are still dataless. Returns
        (pending, stats) once none are, or None if the cycle should defer."""
        stats = self.icloud_stats(pending.icloud)
        give_up = time.monotonic() + (self.cfg.dataless_wait_seconds if self.mode == "ondemand" else 0)
        while any(dataless for _, dataless in stats.values()):
            if time.monotonic() >= give_up:
                break
            time.sleep(2)  # on demand: the caller wants it now; usually seconds
            pending = self.check_pending()
            stats = self.icloud_stats(pending.icloud)

        stuck = sorted(p for p, (_, dataless) in stats.items() if dataless)
        if not stuck:
            with contextlib.suppress(FileNotFoundError):
                self.dataless_since_file.unlink()
            return pending, stats

        # A few seconds is a phone edit arriving; for long, it's offloading.
        try:
            since = float(self.dataless_since_file.read_text())
        except (FileNotFoundError, ValueError):
            since = time.time()
            self.dataless_since_file.write_text(str(since))
        names = ", ".join(stuck[:5])
        waited = time.time() - since
        if waited > self.cfg.dataless_fail_seconds:
            raise SyncFailure(
                f"{len(stuck)} changed file(s) in the iCloud vault have had no downloaded contents "
                f"for {waited / 60:.0f} min ({names}). iCloud isn't downloading them -- likely "
                "offloaded to save space; set the vault folder to Keep Downloaded in Finder.")
        self.log(f"deferred: iCloud still downloading {len(stuck)} file(s): {names}")
        if self.mode == "ondemand":
            print(f"sync deferred: iCloud is still downloading {len(stuck)} phone-edited "
                  f"file(s) ({names}), so the vault may be missing the newest phone edits.",
                  file=sys.stderr)
        return None

    def run_headless(self):
        cfg = self.cfg
        prompt = ((PLUGIN_ROOT / "sync/prompt.md").read_text(encoding="utf-8") + "\n\n"
                  + (PLUGIN_ROOT / "skills/personal-knowledge-base/git-conventions.md").read_text(encoding="utf-8"))
        # PERSONAL_KNOWLEDGE_BASE_SYNC_HEADLESS: this plugin's hooks stand down
        # inside the run (otherwise they'd try to sync from within the sync).
        env = dict(os.environ, PERSONAL_KNOWLEDGE_BASE_SYNC_HEADLESS="1", **cfg.extra_env)
        with open(cfg.log, "a", encoding="utf-8") as logf:
            logf.write(f"=== {timestamp()} [{self.mode}] sync start ===\n")
            logf.flush()
            proc = subprocess.Popen(
                [cfg.claude_bin, "-p", prompt,
                 "--permission-mode", "dontAsk",
                 "--allowedTools", "Bash(git *)",
                 "--add-dir", str(cfg.claude),
                 "--add-dir", str(cfg.icloud)],
                cwd=cfg.main, env=env, stdin=subprocess.DEVNULL,
                stdout=logf, stderr=subprocess.STDOUT,
                start_new_session=True,  # so a timeout can kill its git children too
            )
            try:
                rc = proc.wait(timeout=cfg.claude_timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                rc = None
            logf.write(f"=== {timestamp()} [{self.mode}] sync end "
                       f"({'timed out' if rc is None else f'exit {rc}'}) ===\n")
        if rc is None:
            raise SyncFailure(f"headless claude run TIMED OUT after {cfg.claude_timeout:g}s "
                              "(killed); see its output above in the log")
        if rc != 0:
            raise SyncFailure(f"headless claude run exited {rc}; see its output above in the log")

    def cycle(self):
        self.check_claude_version_pin()
        pending = self.check_pending()
        self.stamp_file.touch()
        if not pending.anything():
            return  # deliberately not logged (every 5 min + every hook)

        # Changed phone files whose contents iCloud hasn't downloaded can't be
        # read, so `git add` would fail on them. Wait (on demand) or defer.
        stats = {}
        if pending.icloud:
            ready = self.wait_for_downloads(pending)
            if ready is None:
                return
            pending, stats = ready

        # Quiet window: don't commit a note while it's still being typed on the
        # phone. Only files git sees as changed count, so Obsidian's ignored
        # UI-state churn can't hold the cycle off forever.
        if self.mode == "timer" and stats:
            newest = max(mtime for mtime, _ in stats.values())
            if time.time() - newest < self.cfg.quiet_seconds:
                self.log(f"deferred: phone edits changed {time.time() - newest:.0f}s ago, "
                         "waiting for them to settle")
                return

        self.run_headless()

        # The headless run can exit 0 having done nothing (e.g. a denied tool
        # call it merely reported).
        after = self.check_pending()
        # Phone changes still uncommitted count only if untouched since we
        # looked: a phone edit landing mid-run is fine, the next cycle takes it.
        after_stats = self.icloud_stats(after.icloud)
        left = sorted(p for p in after.icloud
                      if p in stats and p in after_stats and after_stats[p][0] == stats[p][0])
        no_origin = self.commits_missing_origin(pending.main_tip)
        if no_origin:
            raise SyncFailure(
                "headless run made commits on main without a readable `Origin:` trailer "
                f"(see git-conventions.md, use --trailer): {'; '.join(no_origin[:5])}")
        if after.claude or after.diverged or after.unpushed or left:
            raise SyncFailure(
                "headless run finished but left work undone (claude worktree dirty: "
                f"{'yes' if after.claude else 'no'}; phone changes uncommitted: "
                f"{', '.join(left[:5]) or 'none'}; branches diverged: "
                f"{'yes' if after.diverged else 'no'}; unpushed: "
                f"{', '.join(after.unpushed) or 'none'}); see its output above in the log")

    def run(self):
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            lock = self.acquire_lock()
            if lock is None:
                if self.mode == "timer":
                    self.log("skipped: another sync cycle is running")
                    return 0
                raise SyncFailure(f"waited {self.cfg.lock_wait_seconds:g}s for another sync cycle "
                                  "to finish; one may be stuck (check: ps ax | grep '[s]ync.py')")
            with lock:
                self.cycle()
        except SyncFailure as e:
            self.log(f"FAILED: {e}")
            self.fail_file.write_text(f"{timestamp()} {e}\n", encoding="utf-8")
            print(f"sync ({self.mode}) failed: {e} -- see {self.cfg.log}", file=sys.stderr)
            return 1
        with contextlib.suppress(FileNotFoundError):
            self.fail_file.unlink()
        return 0


def main(argv=None, cfg=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--timer", action="store_true", help="launchd backstop mode")
    args = parser.parse_args(argv)
    return Sync(cfg or Config(), "timer" if args.timer else "ondemand").run()


if __name__ == "__main__":
    sys.exit(main())
