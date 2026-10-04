#!/usr/bin/env python3
"""Hooks for the wiki plugin.

    wiki_hook.py pre   PreToolUse: block Edit/Write into the iCloud vault or the
                       main repo; sync before any tool call that touches the wiki
                       (throttled).
    wiki_hook.py stop  Stop: if the claude worktree has changes not yet on main,
                       kick off a sync in the background so they reach Obsidian.

Plugin hooks fire on every tool call in every session, so the common path --
"this call has nothing to do with the wiki" -- must return fast and silently.
"""

import json
import os
import re
import subprocess
import sys
import time

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYNC = os.path.join(PLUGIN_ROOT, "sync", "sync.sh")
SYNC_MIN_INTERVAL = 60  # seconds between pre-access syncs
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def load_config():
    """Parse config.sh's plain KEY="value" lines (only $HOME is expanded)."""
    home = os.path.expanduser("~")
    cfg = {}
    with open(os.path.join(PLUGIN_ROOT, "config.sh")) as f:
        for line in f:
            m = re.match(r'^(\w+)="(.*)"\s*$', line.strip())
            if m:
                cfg[m.group(1)] = m.group(2).replace("$HOME", home)
    return cfg


def under(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def tool_paths(tool_input, cwd):
    """File/dir paths a tool call targets, absolute."""
    paths = []
    for key in ("file_path", "path", "notebook_path"):
        p = tool_input.get(key)
        if isinstance(p, str) and p:
            p = os.path.expanduser(p)
            paths.append(p if os.path.isabs(p) else os.path.join(cwd, p))
    return paths


def mentions(command, root):
    home = os.path.expanduser("~")
    tilde = "~" + root[len(home):] if root.startswith(home) else root
    return root in command or tilde in command


def pre(data, cfg):
    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}
    roots = [cfg["WIKI_MAIN"], cfg["WIKI_CLAUDE"], cfg["WIKI_ICLOUD"]]

    if tool == "Bash":
        command = tool_input.get("command", "")
        touches = any(mentions(command, r) for r in roots)
    else:
        paths = tool_paths(tool_input, data.get("cwd") or os.getcwd())
        if tool in WRITE_TOOLS:
            for p in paths:
                for root, what in ((cfg["WIKI_ICLOUD"], "the iCloud vault"),
                                   (cfg["WIKI_MAIN"], "the main wiki repo")):
                    if under(p, root):
                        deny(f"Wiki edits go in {cfg['WIKI_CLAUDE']} (branch `claude`), "
                             f"never {what}. Make the same edit there; the sync "
                             f"propagates it.")
                        return
        touches = any(under(p, r) for p in paths for r in roots)

    if not touches:
        return

    stamp = os.path.join(cfg["WIKI_STATE_DIR"], "last-check")
    try:
        if time.time() - os.path.getmtime(stamp) < SYNC_MIN_INTERVAL:
            return
    except OSError:
        pass

    result = subprocess.run([SYNC], capture_output=True, text=True)
    if result.returncode != 0:
        context(f"Wiki sync failed (exit {result.returncode}); the wiki may be stale. "
                f"{result.stderr.strip()} See {cfg['WIKI_LOG']}.")


def stop(data, cfg):
    def git(*args):
        return subprocess.run(["git", "-C", cfg["WIKI_CLAUDE"], *args],
                              capture_output=True, text=True).stdout.strip()

    pending = git("status", "--porcelain") or git("rev-list", "main..claude")
    if pending:
        # Detached: the user shouldn't wait on the sync at the end of a turn.
        subprocess.Popen([SYNC], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


def context(text):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": text,
    }}))


def main():
    # Inside the headless sync run, stand down: it would otherwise try to
    # sync from within the sync.
    if os.environ.get("WIKI_SYNC_HEADLESS"):
        return
    cfg = load_config()
    if not os.path.isdir(cfg["WIKI_CLAUDE"]):
        return  # not on the Mac (e.g. a cloud session)
    data = json.load(sys.stdin)
    {"pre": pre, "stop": stop}[sys.argv[1]](data, cfg)


if __name__ == "__main__":
    main()
