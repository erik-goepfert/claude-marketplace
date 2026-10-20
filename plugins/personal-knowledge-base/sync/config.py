"""Paths and limits shared by sync.py and hooks/personal_knowledge_base_hook.py.

Must stay compatible with Apple's /usr/bin/python3 (3.9): the launchd timer
runs on it, so macOS privacy approvals attach to a binary that Homebrew
upgrades don't replace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
BRANCHES = ("main", "icloud", "claude")


@dataclass
class Config:
    main: Path = HOME / "Source/personal-knowledge-base"
    claude: Path = HOME / "Source/personal-knowledge-base-claude"
    icloud: Path = HOME / "Library/Mobile Documents/iCloud~md~obsidian/Documents/personal-knowledge-base"

    log: Path = HOME / "Library/Logs/personal-knowledge-base-sync.log"
    state_dir: Path = HOME / "Library/Caches/personal-knowledge-base-sync"

    git_bin: str = "git"
    claude_bin: str = str(HOME / ".local/bin/claude")

    quiet_seconds: float = 120       # timer: phone edits must be this old to sync
    lock_wait_seconds: float = 180   # on demand: how long to wait for a running cycle
    git_timeout: float = 30          # any single precheck step (normally milliseconds)
    claude_timeout: float = 600      # the whole headless run (normally ~20 s)
    sync_min_interval: float = 60    # hook: seconds between pre-access syncs
    dataless_wait_seconds: float = 30      # on demand: wait for iCloud to finish downloading
    dataless_fail_seconds: float = 45 * 60  # still not downloaded after this: a failure

    extra_env: dict = field(default_factory=dict)  # tests only

    @property
    def roots(self):
        return (self.main, self.claude, self.icloud)

    def worktree(self, branch):
        return {"main": self.main, "icloud": self.icloud, "claude": self.claude}[branch]
