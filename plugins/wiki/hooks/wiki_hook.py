#!/usr/bin/python3
"""Hooks for the wiki plugin.

    wiki_hook.py pre   PreToolUse: block Edit/Write into the iCloud vault or the
                       main repo; sync before any tool call that touches the wiki
                       (throttled); report recorded sync failures (once per
                       session) so Claude tells Erik.
    wiki_hook.py stop  Stop: if the claude worktree has changes not yet on main,
                       kick off a sync in the background so they reach Obsidian.

Plugin hooks fire on every tool call in every session, so the common path --
"this call has nothing to do with the wiki" -- must return fast and silently.
"""

import json
import os
import subprocess
import sys
import time

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_ROOT, "sync"))
from config import Config  # noqa: E402

SYNC = ["/usr/bin/python3", os.path.join(PLUGIN_ROOT, "sync", "sync.py")]
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def under(path, root):
    path, root = os.path.realpath(path), os.path.realpath(str(root))
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
    root, home = str(root), os.path.expanduser("~")
    tilde = "~" + root[len(home):] if root.startswith(home) else root
    return root in command or tilde in command


def pre(data, cfg):
    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}
    roots = cfg.roots

    if tool == "Bash":
        command = tool_input.get("command", "")
        touches = any(mentions(command, r) for r in roots)
    else:
        paths = tool_paths(tool_input, data.get("cwd") or os.getcwd())
        if tool in WRITE_TOOLS:
            for p in paths:
                for root, what in ((cfg.icloud, "the iCloud vault"),
                                   (cfg.main, "the main wiki repo")):
                    if under(p, root):
                        deny(f"Wiki edits go in {cfg.claude} (branch `claude`), "
                             f"never {what}. Make the same edit there; the sync "
                             f"propagates it.")
                        return
        touches = any(under(p, r) for p in paths for r in roots)

    if not touches:
        return

    notes = []
    stamp = cfg.state_dir / "last-check"
    try:
        fresh = time.time() - os.path.getmtime(stamp) < cfg.sync_min_interval
    except OSError:
        fresh = False
    if not fresh:
        result = subprocess.run(SYNC, capture_output=True, text=True)
        if result.returncode != 0:
            notes.append(f"An on-demand wiki sync just failed (exit {result.returncode}), "
                         f"so the wiki may be stale: {result.stderr.strip()}")
        elif result.stderr.strip():  # e.g. deferred: iCloud still downloading
            notes.append(result.stderr.strip())

    notes += unreported_failures(cfg, data.get("session_id", ""))
    if notes:
        context("\n".join(notes) + f"\nTell Erik about this now (log: {cfg.log}) "
                "and offer to investigate it together. Don't try to fix it silently.")


def unreported_failures(cfg, session_id):
    """Recorded failures (sync.py's failed-<mode> files) this session hasn't seen."""
    state = cfg.state_dir
    found = []
    for mode in ("timer", "ondemand"):
        try:
            with open(os.path.join(state, f"failed-{mode}")) as f:
                found.append(f"[{mode}] {f.read().strip()}")
        except OSError:
            pass
    if not found:
        return []
    key = session_id + "\n" + "\n".join(found)
    seen = os.path.join(state, "reported")
    try:
        with open(seen) as f:
            if f.read() == key:
                return []
    except OSError:
        pass
    with open(seen, "w") as f:
        f.write(key)
    return ["The wiki sync has a recorded failure (cleared by that mode's next clean run):"] + found


def stop(data, cfg):
    def git(*args):
        return subprocess.run([cfg.git_bin, "-C", str(cfg.claude), *args],
                              capture_output=True, text=True).stdout.strip()

    pending = git("status", "--porcelain") or git("rev-list", "main..claude")
    if pending:
        # Detached: the user shouldn't wait on the sync at the end of a turn.
        subprocess.Popen(SYNC, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
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
    cfg = Config()
    if not cfg.claude.is_dir():
        return  # not on the Mac (e.g. a cloud session)
    data = json.load(sys.stdin)
    {"pre": pre, "stop": stop}[sys.argv[1]](data, cfg)


if __name__ == "__main__":
    main()
