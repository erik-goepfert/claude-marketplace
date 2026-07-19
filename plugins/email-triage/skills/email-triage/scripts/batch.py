#!/usr/bin/env python3
"""One-call batch executor for email-triage: act + log + re-index + push.

Phase 5.4. The SKILL's hard invariant is that act -> log-events -> re-index
-> box-scoped sync is ONE unit; before this script that unit cost four
model-driven tool calls per batch (12+ cycles in one live session), with
hand-written event JSON as the norm. Here the model supplies only the
decisions (message-id + destination per item, batch-level category/consent
context) and the script does the rest:

  1. Pre-flight: every item validated BEFORE anything acts (destination
     tier must derive a triage action; the envelope must be indexed; the
     event row must validate). All-or-nothing -- a bad item stops the batch
     while the mailbox is still untouched.
  2. Act via act.py (U=-strip moves / guarded removes).
  3. Log one history event per SUCCESSFUL act, auto-derived:
     final_action from the destination's tier (trash/junk -> trash,
     active -> activate, ongoing/cold or a remove -> archive),
     sender/subject from the index row. decided_by defaults to "approved"
     (this script only runs on approved batches).
  4. Re-index the whole Maildir root, absorbing this batch's own echo from
     the change report; only provably-external residue is returned, for the
     model to consume per SKILL step 4.
  5. Box-scoped mbsync per touched account (exactly the touched folders),
     one account per invocation, one automatic retry (the ~25% EOF flake).

Items JSON (stdin or --file), an array of:
  {"account": "gmail", "message_id": "<...>", "from_folder": "INBOX",
   "to_folder": "[Gmail]/Trash"}                # move, OR
  {"account": "gmail", "message_id": "<...>", "from_folder": "INBOX",
   "remove": true}                              # Gmail label strip
plus optional per-item event overrides: category, thread_state,
inactive_days, proposed_action, decided_by, notes, unsubscribed, rule_id,
thread_root_id, sender_addr, subject.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import act  # noqa: E402
import history  # noqa: E402
import indexer  # noqa: E402

# Destination tier -> logged action. Anything absent is not a triage
# destination and fails pre-flight (untriaged, sent, drafts, all_mail,
# container, unrecognized).
TIER_TO_ACTION = {
    "trash": "trash",
    "junk": "trash",
    "active": "activate",
    "ongoing": "archive",
    "cold": "archive",
}

ENVELOPE_SQL = """SELECT from_addr, subject FROM envelopes
                  WHERE account = ? AND message_id = ? AND folder = ?"""


class BatchError(Exception):
    pass


def load_items(raw: str) -> list:
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        raise BatchError(f"invalid JSON: {e}")
    if not isinstance(items, list) or not items:
        raise BatchError("expected a non-empty JSON array of item objects")
    return items


def prepare(conn, items: list, tier_maps: dict, args) -> list:
    """Validate every item and derive its act plan + event dict. Raises
    BatchError on the first problem -- nothing has acted yet."""
    prepared = []
    for i, item in enumerate(items):
        where = f"item {i}"
        account = item.get("account")
        mid = item.get("message_id")
        src = item.get("from_folder")
        dst = item.get("to_folder")
        remove = bool(item.get("remove"))
        if not account or not mid or not src:
            raise BatchError(f"{where}: account, message_id and from_folder are required")
        if remove == bool(dst):
            raise BatchError(f"{where}: exactly one of to_folder / remove:true is required")
        tier_map = tier_maps.get(account) or {}
        if not tier_map:
            raise BatchError(f"{where}: no tier map for account {account!r} (config.yaml)")

        if remove:
            final = "archive"
        else:
            tier = indexer.classify_folder_tier(dst, tier_map)
            final = TIER_TO_ACTION.get(tier)
            if final is None:
                raise BatchError(
                    f"{where}: destination {dst!r} classifies as tier {tier!r} -- "
                    "not a triage destination (trash/junk/active/ongoing/cold only)")

        row = conn.execute(ENVELOPE_SQL, (account, mid, src)).fetchone()
        if row is None and not item.get("sender_addr"):
            raise BatchError(
                f"{where}: message_id={mid!r} not indexed in folder {src!r} "
                f"(account {account!r}) -- index stale? run the indexer, or "
                "supply sender_addr explicitly")
        from_addr, subject = row if row else (None, None)

        event = {
            "session_started_at": args.session,
            "account": account,
            "message_id": mid,
            "thread_root_id": item.get("thread_root_id"),
            "sender_addr": item.get("sender_addr") or from_addr,
            "subject": item.get("subject") or subject,
            "thread_state": item.get("thread_state"),
            "category": item.get("category") or args.category,
            "inactive_days": item.get("inactive_days"),
            "proposed_action": item.get("proposed_action") or args.proposed or final,
            "final_action": final,
            "from_folder": src,
            "to_folder": None if remove else dst,
            "decided_by": item.get("decided_by") or args.decided_by,
            "rule_id": item.get("rule_id"),
            "notes": item.get("notes") or args.notes,
            "unsubscribed": item.get("unsubscribed", args.unsubscribed),
        }
        try:
            history.event_row_from_dict(event, index=i)
        except ValueError as e:
            raise BatchError(str(e))
        prepared.append({
            "account": account, "message_id": mid, "from_folder": src,
            "to_folder": None if remove else dst, "remove": remove,
            "event": event,
        })
    return prepared


def act_all(conn, maildir_root: Path, prepared: list):
    done, failed = [], []
    for p in prepared:
        acct_root = str(maildir_root / p["account"])
        try:
            if p["remove"]:
                result = act.remove_one(conn, acct_root, p["account"],
                                        p["from_folder"], p["message_id"])
            else:
                result = act.move_one(conn, acct_root, p["account"],
                                      p["from_folder"], p["to_folder"],
                                      p["message_id"])
            result["account"] = p["account"]
            done.append((p, result))
        except act.FatalError as e:
            failed.append({"account": p["account"], "message_id": p["message_id"],
                           "error": str(e)})
    return done, failed


def log_events(data_dir: Path, done: list) -> int:
    if not done:
        return 0
    conn = history.connect(data_dir)
    try:
        rows = [history.event_row_from_dict(p["event"]) for p, _ in done]
        conn.executemany(history.EVENT_INSERT_SQL, rows)
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def reindex_absorbing_echo(db_path: Path, maildir_root: Path, config_path, acted_ids: set) -> dict:
    """Full re-index; split the change report into this batch's own echo
    (absorbed) and external residue (returned for SKILL step-6 handling)."""
    accounts = indexer.resolve_accounts(maildir_root, None)
    tier_maps = indexer.load_tier_maps(config_path, accounts)
    conn = indexer.init_db(db_path)
    try:
        report = indexer.run_index(conn, maildir_root, accounts, tier_maps)
        conn.commit()
    finally:
        conn.close()
    external, absorbed = {}, 0
    for kind in ("new", "moved", "deleted", "flag_changed"):
        keep = []
        for entry in report[kind]:
            if (entry.get("account"), entry.get("message_id")) in acted_ids:
                absorbed += 1
            else:
                keep.append(entry)
        external[kind] = keep
    return {
        "external": external,
        "self_echo_absorbed": absorbed,
        "parse_errors": report["parse_errors"],
        "unrecognized_folders": report["unrecognized_folders"],
    }


def sync_boxes(done: list, mbsync: str, mbsyncrc: str) -> list:
    """Box-scoped push per touched account; one retry per account."""
    boxes = {}  # account -> set of folders
    for p, _result in done:
        s = boxes.setdefault(p["account"], set())
        s.add(p["from_folder"])
        if p["to_folder"]:
            s.add(p["to_folder"])
    results = []
    for account in sorted(boxes):
        spec = f"{account}:{','.join(sorted(boxes[account]))}"
        attempts, ok, err = 0, False, ""
        while attempts < 2 and not ok:
            attempts += 1
            proc = subprocess.run([mbsync, "-c", mbsyncrc, spec],
                                  capture_output=True, text=True)
            ok = proc.returncode == 0
            err = (proc.stderr or "").strip()[-500:]
        results.append({"account": account, "boxes": sorted(boxes[account]),
                        "attempts": attempts, "ok": ok,
                        **({} if ok else {"stderr": err})})
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="execute one approved batch: act + log + index + sync")
    p.add_argument("--db", type=Path, default=None,
                   help="index.db; default <data-dir>/index.db")
    p.add_argument("--maildir-root", type=Path, default=None,
                   help="Maildir root (one subdirectory per account); default <data-dir>/mail")
    p.add_argument("--data-dir", type=Path, default=history.DEFAULT_DATA_DIR,
                   help="data dir holding history.db (and default config.yaml)")
    p.add_argument("--config", type=Path, default=None,
                   help="config.yaml path; default <data-dir>/config.yaml")
    p.add_argument("--mbsyncrc", default=os.path.expanduser("~/.config/isync/mbsyncrc"),
                   help="mbsync config (never auto-discovered)")
    p.add_argument("--mbsync", default="mbsync", help="mbsync binary (tests override)")
    p.add_argument("--session", required=True, help="session_started_at (UTC ISO 8601)")
    p.add_argument("--category", choices=history.CATEGORIES,
                   help="batch-level default; items may override")
    p.add_argument("--decided-by", default="approved", choices=history.DECIDED_BY)
    p.add_argument("--proposed", choices=history.ACTIONS,
                   help="batch-level proposed_action; default = each item's derived final")
    p.add_argument("--notes", help="batch-level default notes")
    p.add_argument("--unsubscribed", action="store_true",
                   help="user confirmed unsubscribing from this whole batch")
    p.add_argument("--file", help="read the items JSON array from this file instead of stdin")
    p.add_argument("--dry-run", action="store_true",
                   help="validate + print the derived plan; act nothing")
    p.add_argument("--no-sync", action="store_true",
                   help="skip the mbsync push (caller owns the invariant then)")
    args = ap.parse_args(argv)

    if args.db is None:
        args.db = args.data_dir / "index.db"
    if args.maildir_root is None:
        args.maildir_root = args.data_dir / "mail"
    config_path = args.config or (args.data_dir / "config.yaml")
    out: dict = {}
    try:
        if not args.db.is_file():
            raise BatchError(f"no such db: {args.db}")
        if not args.maildir_root.is_dir():
            raise BatchError(f"no such maildir root: {args.maildir_root}")
        if not (args.data_dir / "history.db").is_file():
            raise BatchError(f"no history.db in {args.data_dir}")

        raw = Path(args.file).read_text() if args.file else sys.stdin.read()
        items = load_items(raw)
        accounts = sorted({it.get("account") for it in items if it.get("account")})
        tier_maps = indexer.load_tier_maps(config_path, accounts)

        conn = sqlite3.connect(args.db)
        try:
            prepared = prepare(conn, items, tier_maps, args)
        finally:
            conn.close()

        if args.dry_run:
            json.dump({"dry_run": True,
                       "plan": [{k: p[k] for k in ("account", "message_id",
                                                   "from_folder", "to_folder", "remove")}
                                | {"event": p["event"]} for p in prepared]},
                      sys.stdout, indent=2)
            print()
            return 0

        conn = sqlite3.connect(args.db)
        try:
            done, failed = act_all(conn, args.maildir_root, prepared)
        finally:
            conn.close()

        out["acted"] = {
            "moved": [r for p, r in done if not p["remove"]],
            "removed": [r for p, r in done if p["remove"]],
        }
        out["failed"] = failed
        out["events_logged"] = log_events(args.data_dir, done)

        acted_ids = {(p["account"], p["message_id"]) for p, _ in done}
        out["change_report"] = reindex_absorbing_echo(
            args.db, args.maildir_root, config_path, acted_ids)

        if args.no_sync:
            out["sync"] = "skipped (--no-sync)"
            sync_ok = True
        else:
            out["sync"] = sync_boxes(done, args.mbsync, args.mbsyncrc)
            sync_ok = all(s["ok"] for s in out["sync"])

        json.dump(out, sys.stdout, indent=2, sort_keys=True)
        print()
        return 0 if not failed and sync_ok else 1
    except BatchError as e:
        print(f"error: {e} -- nothing was acted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
