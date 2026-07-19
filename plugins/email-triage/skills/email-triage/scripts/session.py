#!/usr/bin/env python3
"""Session lifecycle for email-triage: every standard shell step a session
runs, captured as one whitelisted script (2026-07-19).

Born from the first permission prompt of the first post-Phase-5 session:
the raw `cat config.yaml; mkdir session.lock; date -u` bundle fell outside
the skill's allowed-tools, as did the nohup sync chains and marker checks.
Rule: if every session needs it, it lives in a script, not in ad-hoc shell.

Subcommands:
  start            Read config + acquire the session lock + stamp the
                   session + clear the fullsync marker + priority-folder
                   fast sets, all in one JSON. Exits 1 (and touches
                   nothing) if the lock is already held.
  unlock           Remove a stale session lock -- only after the user has
                   approved removing it.
  sync-background  Detached full sync of every account (one mbsync
                   invocation per account, one automatic retry each);
                   touches .fullsync-done only if ALL succeed. Stage 2 of
                   the two-stage sync.
  check-fullsync   Is Stage 2 done? Marker status + log tail when not.
  end              Detached final sync chain that releases the session
                   lock only on success. Write the session note BEFORE
                   calling this. Deliberately runs no indexing: whatever
                   this sync pulls must surface as external change in the
                   NEXT session's change report.
"""

import argparse
import datetime
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indexer  # noqa: E402

DEFAULT_DATA_DIR = Path(os.environ.get("EMAIL_TRIAGE_DATA_DIR", "~/.local/share/email-triage")).expanduser()
DEFAULT_MBSYNCRC = os.path.expanduser("~/.config/isync/mbsyncrc")


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit(obj: dict) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def config_path_for(args) -> Path:
    return args.config or (args.data_dir / "config.yaml")


def accounts_for(args) -> list:
    """Sync targets: explicit --account wins, else config accounts, else
    Maildir subdirectories."""
    if args.account:
        return sorted(set(args.account))
    parsed = indexer.parse_config_tiers(config_path_for(args))
    if parsed:
        return sorted(parsed)
    maildir = args.data_dir / "mail"
    if maildir.is_dir():
        return sorted(d.name for d in maildir.iterdir()
                      if d.is_dir() and not d.name.startswith("."))
    return sorted(indexer.DEFAULT_TIER_MAPS)


def full_sync_chain(accounts: list, mbsync: str, mbsyncrc: str, tail: str) -> str:
    """One shell chain: per-account invocation with ONE automatic retry
    (the ~25% `read: unexpected EOF` flake), `tail` only if ALL accounts
    synced. Stricter than the old hand-typed `a; b && tail` chain, which
    could mark success with the first account failed."""
    parts = []
    for account in accounts:
        cmd = f"{shlex.quote(mbsync)} -c {shlex.quote(mbsyncrc)} {shlex.quote(account)}"
        parts.append(f"({cmd} || {cmd})")
    return " && ".join(parts) + f" && {tail}"


def launch_detached(chain: str, log_path: Path) -> int:
    with open(log_path, "ab") as log:
        log.write(f"--- {utc_now()} ---\n".encode())
        log.flush()
        proc = subprocess.Popen(["sh", "-c", chain], stdout=log, stderr=log,
                                start_new_session=True)
    return proc.pid


def cmd_start(args) -> int:
    data_dir = args.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = data_dir / "session.lock"
    marker = data_dir / ".fullsync-done"
    cfg = config_path_for(args)

    config_text = cfg.read_text() if cfg.is_file() else None

    try:
        lock.mkdir()
        acquired = True
    except FileExistsError:
        acquired = False

    # Diagnostic (2026-07-19; verified null in live sessions -- the base
    # dir comes from the skill launch banner): kept cheap, catches a
    # future harness change.
    env = {
        "CLAUDE_SKILL_DIR": os.environ.get("CLAUDE_SKILL_DIR"),
        "CLAUDE_PLUGIN_ROOT": os.environ.get("CLAUDE_PLUGIN_ROOT"),
    }

    if not acquired:
        # R8: a held lock means no session started -- the only question on
        # the table is "unlock?"; don't dump config/priority noise into it.
        emit({
            "lock": "held",
            "hint": ("a session may be live elsewhere or was abandoned -- "
                     "ask the user before `session.py unlock`; check "
                     "endsync.log if the last session's end chain failed"),
            "env": env,
        })
        return 1

    out = {
        "session_started_at": utc_now(),
        "lock": "acquired",
        "config": config_text,
        "config_path": str(cfg),
        "priority_folders": indexer.priority_fast_map(cfg),
        "env": env,
    }
    if config_text is None:
        out["warning"] = ("config.yaml missing -- from-scratch setup; see "
                          "reference.md Bootstrap, do not improvise")
    marker.unlink(missing_ok=True)
    out["fullsync_marker_cleared"] = True
    emit(out)
    return 0


def cmd_unlock(args) -> int:
    lock = args.data_dir / "session.lock"
    if not lock.exists():
        emit({"unlock": "no lock present"})
        return 0
    lock.rmdir()  # fails loudly if somehow non-empty
    emit({"unlock": "removed"})
    return 0


def cmd_sync_background(args) -> int:
    data_dir = args.data_dir
    marker = data_dir / ".fullsync-done"
    log = data_dir / "fullsync.log"
    accounts = accounts_for(args)
    chain = full_sync_chain(accounts, args.mbsync, args.mbsyncrc,
                            f"touch {shlex.quote(str(marker))}")
    pid = launch_detached(chain, log)
    emit({"stage2": "launched", "pid": pid, "accounts": accounts,
          "log": str(log), "marker": str(marker)})
    return 0


def cmd_check_fullsync(args) -> int:
    data_dir = args.data_dir
    done = (data_dir / ".fullsync-done").is_file()
    out = {"fullsync_done": done}
    log = data_dir / "fullsync.log"
    if not done and log.is_file():
        out["log_tail"] = log.read_text().splitlines()[-5:]
    emit(out)
    return 0 if done else 1


def cmd_end(args) -> int:
    data_dir = args.data_dir
    lock = data_dir / "session.lock"
    log = data_dir / "endsync.log"
    accounts = accounts_for(args)
    chain = full_sync_chain(accounts, args.mbsync, args.mbsyncrc,
                            f"rmdir {shlex.quote(str(lock))}")
    pid = launch_detached(chain, log)
    emit({"end_chain": "launched", "pid": pid, "accounts": accounts,
          "log": str(log),
          "note": "lock releases only if every account syncs; a surviving "
                  "lock surfaces at the next session's start"})
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--config", type=Path, default=None,
                    help="config.yaml path; default <data-dir>/config.yaml")
    ap.add_argument("--mbsyncrc", default=DEFAULT_MBSYNCRC,
                    help="mbsync config (never auto-discovered)")
    ap.add_argument("--mbsync", default="mbsync", help="mbsync binary (tests override)")
    ap.add_argument("--account", action="append",
                    help="sync only this account (repeatable); default: all configured")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("start", cmd_start), ("unlock", cmd_unlock),
                     ("sync-background", cmd_sync_background),
                     ("check-fullsync", cmd_check_fullsync), ("end", cmd_end)):
        p = sub.add_parser(name)
        p.set_defaults(func=fn)
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
