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
  sync             Both stages of the two-stage sync, `--scope` picking
                   which. The scopes NEST -- `full` re-syncs everything
                   `priority` did, INBOX included -- which is what makes
                   the step-6 re-index able to surface mail that arrived
                   during Stage 1. Hence inclusive names, not hot/cold.
                     priority  Stage 1: the live working set + sent +
                               drafts. FOREGROUND and sequential --
                               classification runs on this data, so
                               returning before it lands defeats it.
                     full      Stage 2: every folder, DETACHED -- it takes
                               minutes and classification proceeds
                               meanwhile. Touches .fullsync-done only if
                               ALL accounts succeed.
                   Box lists come from config.yaml via
                   indexer.priority_fast_map, never from the caller: this
                   command exists so no caller hand-assembles a box list
                   or hand-quotes `[Gmail]/Sent Mail` (2026-08-10).
  sync-background  Alias for `sync --scope full`, kept because reference.md
                   and cmd_check_fullsync's own hint name it.
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
import untrusted  # noqa: E402

DEFAULT_DATA_DIR = Path(os.environ.get("EMAIL_TRIAGE_DATA_DIR", "~/.local/share/email-triage")).expanduser()
DEFAULT_MBSYNCRC = os.path.expanduser("~/.config/isync/mbsyncrc")


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Trust classification (Phase 6.2; untrusted-content-skill-design.md §5).
# Applied centrally in emit() rather than per subcommand, so a field added to
# any future subcommand's output is classified by default-deny automatically
# instead of depending on someone remembering a second edit.
#
# Everything this script emits is its own bookkeeping, config the USER wrote, or
# our environment -- with exactly one exception, `log_tail`.
TRUSTED_FIELDS = {
    "session_started_at", "lock", "hint", "warning", "config", "config_path",
    "unlock", "stage2", "end_chain", "log", "marker", "note", "accounts",
    "scope", "all_ok", "unknown_accounts", "synced.account", "synced.boxes",
    "synced.attempts", "synced.ok",
}
# priority_folders is keyed by account name and env by variable name: dynamic
# key spaces, but OURS (config and os.environ), never a sender's. That is the
# only condition under which a subtree may be trusted wholesale.
TRUSTED_SUBTREES = {"priority_folders", "env"}
# Absent on purpose, so default-deny escapes and declares them. Both are
# mbsync's own output -- an external process whose text is not a vocabulary we
# control, reporting on data senders produced. Nothing feeds either back, so
# escaping is free.
#   log_tail        -- check-fullsync's tail of fullsync.log
#   synced.stderr   -- a failed foreground attempt's tail (same call as
#                      batch.py's `sync.stderr`, same reasoning)


def emit(obj: dict) -> None:
    print(json.dumps(untrusted.apply_trust(obj, TRUSTED_FIELDS,
                                           trusted_subtrees=TRUSTED_SUBTREES),
                     indent=2, sort_keys=True))


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


def sync_specs(specs: list, mbsync: str, mbsyncrc: str) -> list:
    """Foreground per-account sync, ONE retry each (the ~25% `read: unexpected
    EOF` flake). Sequential by construction -- it is a loop, so the ordering
    the SKILL used to ask a caller to honour can no longer be gotten wrong.

    Result shape deliberately matches batch.py's `sync_boxes`, so a reader who
    knows one knows the other: account / boxes / attempts / ok, plus a stderr
    tail only on failure. Argv, never a shell string -- folder names contain
    spaces and `[Gmail]/...` contains glob characters, and hand-quoting those
    is the hazard this whole command removes."""
    results = []
    for account, boxes in specs:
        spec = f"{account}:{','.join(boxes)}"
        attempts, ok, err = 0, False, ""
        while attempts < 2 and not ok:
            attempts += 1
            proc = subprocess.run([mbsync, "-c", mbsyncrc, spec],
                                  capture_output=True, text=True)
            ok = proc.returncode == 0
            err = (proc.stderr or "").strip()[-500:]
        results.append({"account": account, "boxes": list(boxes),
                        "attempts": attempts, "ok": ok,
                        **({} if ok else {"stderr": err})})
    return results


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


def cmd_sync(args) -> int:
    return _sync_full(args) if args.scope == "full" else _sync_priority(args)


def _sync_full(args) -> int:
    """Stage 2, detached. Every folder including the ones Stage 1 already
    covered -- the scopes nest, and re-covering INBOX is load-bearing: mail
    arriving during Stage 1 reaches the index only via step 6's re-index."""
    data_dir = args.data_dir
    marker = data_dir / ".fullsync-done"
    log = data_dir / "fullsync.log"
    accounts = accounts_for(args)
    chain = full_sync_chain(accounts, args.mbsync, args.mbsyncrc,
                            f"touch {shlex.quote(str(marker))}")
    pid = launch_detached(chain, log)
    emit({"scope": "full", "stage2": "launched", "pid": pid,
          "accounts": accounts, "log": str(log), "marker": str(marker)})
    return 0


def _sync_priority(args) -> int:
    """Stage 1, foreground. Exit 1 if ANY account failed both attempts: a
    partial Stage 1 means that account's mirror still holds last session's
    state, and classifying against it silently reports stale mail as current."""
    pmap = indexer.priority_fast_map(config_path_for(args))
    wanted = accounts_for(args)
    unknown = [a for a in wanted if a not in pmap]
    results = sync_specs([(a, pmap[a]["boxes"]) for a in wanted if a in pmap],
                         args.mbsync, args.mbsyncrc)
    all_ok = bool(results) and all(r["ok"] for r in results) and not unknown
    out = {"scope": "priority", "synced": results, "all_ok": all_ok}
    if unknown:
        # Named an account config.yaml does not define. Never silently sync
        # nothing and report success.
        out["unknown_accounts"] = unknown
    if not all_ok:
        out["hint"] = ("rerun the failed account(s); until Stage 1 succeeds "
                       "their mirror holds last session's state, so anything "
                       "classified from it is stale")
    emit(out)
    return 0 if all_ok else 1


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


def add_shared_options(p, *, suppress: bool) -> None:
    """Options accepted BOTH before and after the subcommand, because
    `sync --scope priority --account gmail` is the order anyone actually
    writes, while argparse only honours pre-subcommand flags by default.

    On the subparser copies every default is SUPPRESS, so a flag left off does
    not overwrite the top-level value with a default -- argparse's
    subparser-defaults trap, and the reason this is a function rather than a
    parent parser."""
    def d(value):
        return argparse.SUPPRESS if suppress else value
    p.add_argument("--data-dir", type=Path, default=d(DEFAULT_DATA_DIR))
    p.add_argument("--config", type=Path, default=d(None),
                   help="config.yaml path; default <data-dir>/config.yaml")
    p.add_argument("--mbsyncrc", default=d(DEFAULT_MBSYNCRC),
                   help="mbsync config (never auto-discovered)")
    p.add_argument("--mbsync", default=d("mbsync"),
                   help="mbsync binary (tests override)")
    p.add_argument("--account", action="append", default=d(None),
                   help="act on this account only (repeatable); default: all configured")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_shared_options(ap, suppress=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    def add(name, **kw):
        p = sub.add_parser(name, **kw)
        add_shared_options(p, suppress=True)
        return p

    for name, fn in (("start", cmd_start), ("unlock", cmd_unlock),
                     ("check-fullsync", cmd_check_fullsync), ("end", cmd_end)):
        add(name).set_defaults(func=fn)

    p = add("sync", help="Stage 1 (--scope priority, foreground) or "
                         "Stage 2 (--scope full, detached)")
    p.add_argument("--scope", required=True, choices=("priority", "full"),
                   help="priority = live working set + sent + drafts; full = "
                        "every folder, a SUPERSET of priority. Required on "
                        "purpose: a default either way would let a half sync "
                        "pass for a whole one")
    p.set_defaults(func=cmd_sync)
    # An alias, not a second implementation.
    add("sync-background").set_defaults(func=cmd_sync, scope="full")
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
