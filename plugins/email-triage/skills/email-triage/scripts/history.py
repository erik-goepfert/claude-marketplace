#!/usr/bin/env python3
"""CLI for the email-triage history DB: log events/notes, read session-start context.

Subcommands:
  log-event   Insert one triage decision (see design doc §9 for semantics).
  log-events  Insert a batch of decisions from a JSON array (one himalaya
              bulk action -> one bulk log call; all-or-nothing).
  log-note    Append a session note.
  context     Print session-start context (recent notes, active rules,
              sender stats) as JSON.
  backup      Consistent dated copy of history.db (+config.yaml) into a
              cloud-synced folder; prunes to the newest N (2.8).
"""

import argparse
import datetime
import shutil
import json
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_DATA_DIR = Path(os.environ.get("EMAIL_TRIAGE_DATA_DIR", "~/.local/share/email-triage")).expanduser()

ACTIONS = ("flag", "reply", "archive", "trash", "skip", "activate", "draft")
# "draft" (added Phase 4.5): composed a reply but saved it to the account's
# Drafts folder instead of sending (finish-on-phone flow). Decisive, like
# activate. Lingering-draft analytics derive from draft events without a
# subsequent reply -- facts, not aggregates; no extra column.
# "activate" (added Phase 2.7): moved to the Active tier -- triaged, stays
# open, no flag. A real decision, so it counts as decisive in sender_stats
# (unlike "skip"). to_folder carries "Active".
DECIDED_BY = ("auto", "approved", "override", "manual")
THREAD_STATES = ("needs-reply", "holding", "waiting")
CATEGORIES = ("spam-noise", "marketing", "newsletter", "passive-update", "ambiguous", "actionable")


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(data_dir: Path) -> sqlite3.Connection:
    db_path = data_dir / "history.db"
    if not db_path.exists():
        sys.exit(f"error: {db_path} not found — run init_history_db.py first")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


EVENT_INSERT_SQL = """INSERT INTO events (session_started_at, occurred_at, account,
           thread_root_id, message_id, sender_addr, subject,
           thread_state, category, inactive_days,
           proposed_action, final_action, from_folder, to_folder,
           decided_by, rule_id, notes, unsubscribed)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def event_row_from_dict(d: dict, *, index: int | None = None) -> tuple:
    """Validate one event dict (same fields as log-event's flags) and return
    the row tuple for EVENT_INSERT_SQL. Raises ValueError with the offending
    index (if given) on the first problem, so a bulk batch fails atomically
    and legibly rather than partially inserting."""
    where = f"item {index}: " if index is not None else ""

    def require(key: str):
        if not d.get(key):
            raise ValueError(f"{where}missing required field '{key}'")
        return d[key]

    def check_choice(key: str, choices: tuple[str, ...], required: bool = True):
        val = d.get(key)
        if val is None:
            if required:
                raise ValueError(f"{where}missing required field '{key}'")
            return None
        if val not in choices:
            raise ValueError(f"{where}'{key}'={val!r} not one of {choices}")
        return val

    session = require("session_started_at")
    sender = require("sender_addr").lower()
    proposed = check_choice("proposed_action", ACTIONS)
    final = check_choice("final_action", ACTIONS)
    decided_by = check_choice("decided_by", DECIDED_BY)
    check_choice("thread_state", THREAD_STATES, required=False)
    check_choice("category", CATEGORIES, required=False)

    return (
        session,
        d.get("occurred_at") or utc_now(),
        d.get("account"),
        d.get("thread_root_id"),
        d.get("message_id"),
        sender,
        d.get("subject"),
        d.get("thread_state"),
        d.get("category"),
        d.get("inactive_days"),
        proposed,
        final,
        d.get("from_folder"),
        d.get("to_folder"),
        decided_by,
        d.get("rule_id"),
        d.get("notes"),
        int(bool(d.get("unsubscribed", False))),
    )


def cmd_log_event(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    row = event_row_from_dict(
        {
            "session_started_at": args.session,
            "occurred_at": args.occurred_at,
            "account": args.account,
            "thread_root_id": args.thread_root_id,
            "message_id": args.message_id,
            "sender_addr": args.sender,
            "subject": args.subject,
            "thread_state": args.thread_state,
            "category": args.category,
            "inactive_days": args.inactive_days,
            "proposed_action": args.proposed,
            "final_action": args.final,
            "from_folder": args.from_folder,
            "to_folder": args.to_folder,
            "decided_by": args.decided_by,
            "rule_id": args.rule_id,
            "notes": args.notes,
            "unsubscribed": args.unsubscribed,
        }
    )
    conn.execute(EVENT_INSERT_SQL, row)
    conn.commit()
    print("event logged")


def cmd_log_events(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    raw = Path(args.file).read_text() if args.file else sys.stdin.read()
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"error: invalid JSON: {e}")
    if not isinstance(items, list):
        sys.exit("error: expected a JSON array of event objects")

    try:
        rows = [event_row_from_dict(item, index=i) for i, item in enumerate(items)]
    except ValueError as e:
        sys.exit(f"error: {e} — no events were logged (all-or-nothing)")

    conn.executemany(EVENT_INSERT_SQL, rows)
    conn.commit()
    print(f"{len(rows)} events logged")


def cmd_log_note(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    conn.execute(
        "INSERT INTO session_notes (session_started_at, written_at, note) VALUES (?, ?, ?)",
        (args.session, utc_now(), args.note),
    )
    conn.commit()
    print("note logged")


def cmd_add_rule(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """Rules are history turned into behavior (§9). Since 5.5 the action
    vocabulary is wider than the event enum: alongside plain actions
    ('trash') an action may name an INTERACTION PATTERN — how to handle the
    matching mail, e.g. 'summarize-then-trash', 'scan-then-trash',
    'surface-immediately'. Free text by design; the enum lives on events,
    not rules."""
    cur = conn.execute(
        "INSERT INTO rules (created_at, active, predicate, action, origin) VALUES (?, 1, ?, ?, ?)",
        (utc_now(), args.predicate, args.action, args.origin),
    )
    conn.commit()
    print(f"rule {cur.lastrowid} added")


def cmd_set_rule(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    cur = conn.execute(
        "UPDATE rules SET active = ? WHERE id = ?",
        (int(args.active), args.rule_id),
    )
    if cur.rowcount == 0:
        sys.exit(f"error: no rule with id {args.rule_id}")
    conn.commit()
    print(f"rule {args.rule_id} active={int(args.active)}")


def cmd_add_contact(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """Upsert one contact (asserted facts only — stats stay in views).
    Only the fields given are touched on an existing row."""
    addr = args.addr.strip().lower()
    existing = conn.execute("SELECT addr FROM contacts WHERE addr = ?", (addr,)).fetchone()
    if existing:
        sets, params = ["updated_at = ?"], [utc_now()]
        for col, val in (("display_name", args.display_name), ("relation", args.relation),
                         ("notes", args.notes)):
            if val is not None:
                sets.append(f"{col} = ?")
                params.append(val)
        if args.vip is not None:
            sets.append("is_vip = ?")
            params.append(int(args.vip))
        conn.execute(f"UPDATE contacts SET {', '.join(sets)} WHERE addr = ?", (*params, addr))
        verb = "updated"
    else:
        conn.execute(
            "INSERT INTO contacts (addr, display_name, relation, is_vip, notes, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (addr, args.display_name, args.relation,
             int(args.vip) if args.vip is not None else 0, args.notes, utc_now()),
        )
        verb = "added"
    conn.commit()
    print(f"contact {addr} {verb}")


def cmd_backup(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """history.db is the one irreplaceable file (design §9); the data dir
    (~/.local/...) sits outside iCloud/Documents sync, so backups must land
    somewhere the user's sync actually covers. Uses SQLite's online-backup
    API for a consistent copy even mid-write. Dated files, pruned to the
    newest --keep; config.yaml rides along as config-latest.yaml (folder
    map + server_rules registry — small and worth keeping)."""
    dest = Path(args.dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    target = dest / f"history-{stamp}.db"
    out = sqlite3.connect(target)
    try:
        conn.backup(out)
    finally:
        out.close()
    cfg = args.data_dir / "config.yaml"
    if cfg.is_file():
        shutil.copy2(cfg, dest / "config-latest.yaml")
    copies = sorted(dest.glob("history-*.db"))
    pruned = []
    for old in copies[:-args.keep] if args.keep > 0 else []:
        old.unlink()
        pruned.append(old.name)
    print(json.dumps({"backed_up": str(target),
                      "copies": len(copies) - len(pruned),
                      "pruned": pruned}))


def cmd_context(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    recent_sessions = [
        r["session_started_at"]
        for r in conn.execute(
            "SELECT DISTINCT session_started_at FROM session_notes ORDER BY session_started_at DESC LIMIT ?",
            (args.sessions,),
        )
    ]
    notes = [
        dict(r)
        for r in conn.execute(
            "SELECT session_started_at, written_at, note FROM session_notes "
            "WHERE session_started_at IN (%s) ORDER BY written_at"
            % ",".join("?" * len(recent_sessions)),
            recent_sessions,
        )
    ] if recent_sessions else []
    rules = [dict(r) for r in conn.execute("SELECT * FROM rules WHERE active = 1")]
    stats_sql = "SELECT * FROM sender_stats"
    params: tuple = ()
    if args.senders:
        addrs = [a.strip().lower() for a in args.senders.split(",") if a.strip()]
        stats_sql += " WHERE sender_addr IN (%s)" % ",".join("?" * len(addrs))
        params = tuple(addrs)
    stats = [dict(r) for r in conn.execute(stats_sql, params)]
    contacts = [dict(r) for r in conn.execute("SELECT * FROM contacts")]
    json.dump(
        {"session_notes": notes, "active_rules": rules, "sender_stats": stats, "contacts": contacts},
        sys.stdout,
        indent=2,
    )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    sub = parser.add_subparsers(dest="command", required=True)

    ev = sub.add_parser("log-event", help="insert one triage decision")
    ev.add_argument("--session", required=True, help="session_started_at (UTC ISO 8601)")
    ev.add_argument("--occurred-at", help="UTC ISO 8601; default now")
    ev.add_argument("--account")
    ev.add_argument("--thread-root-id", help="root Message-ID of the thread")
    ev.add_argument("--message-id")
    ev.add_argument("--sender", required=True, help="sender email address")
    ev.add_argument("--subject")
    ev.add_argument("--thread-state", choices=THREAD_STATES)
    ev.add_argument("--category", choices=CATEGORIES)
    ev.add_argument("--inactive-days", type=int)
    ev.add_argument("--proposed", required=True, choices=ACTIONS)
    ev.add_argument("--final", required=True, choices=ACTIONS)
    ev.add_argument("--from-folder")
    ev.add_argument("--to-folder")
    ev.add_argument("--decided-by", required=True, choices=DECIDED_BY)
    ev.add_argument("--rule-id", type=int)
    ev.add_argument("--notes")
    ev.add_argument(
        "--unsubscribed",
        action="store_true",
        help="user confirmed they clicked the surfaced unsubscribe link (self-reported; see §4.4)",
    )
    ev.set_defaults(func=cmd_log_event)

    evs = sub.add_parser(
        "log-events",
        help="insert a batch of decisions from a JSON array (stdin or --file)",
        description="Reads a JSON array of event objects (same field names as "
        "log-event's flags, e.g. {\"session_started_at\": ..., \"sender_addr\": ..., "
        "\"proposed_action\": ..., \"final_action\": ..., \"decided_by\": ..., ...}). "
        "All events are validated before any are inserted (all-or-nothing).",
    )
    evs.add_argument("--file", help="read JSON array from this file instead of stdin")
    evs.set_defaults(func=cmd_log_events)

    nt = sub.add_parser("log-note", help="append a session note")
    nt.add_argument("--session", required=True, help="session_started_at (UTC ISO 8601)")
    nt.add_argument("note")
    nt.set_defaults(func=cmd_log_note)

    ar = sub.add_parser(
        "add-rule",
        help="add an active rule (action may be a plain action OR an "
             "interaction pattern like 'summarize-then-trash')")
    ar.add_argument("--predicate", required=True,
                    help="e.g. \"sender = ship-confirm@amazon.com\"")
    ar.add_argument("--action", required=True,
                    help="trash | summarize-then-trash | scan-then-trash | "
                         "surface-immediately | ... (free text)")
    ar.add_argument("--origin", required=True, choices=("user", "claude-proposed"))
    ar.set_defaults(func=cmd_add_rule)

    sr = sub.add_parser("set-rule", help="activate/deactivate a rule")
    sr.add_argument("rule_id", type=int)
    sr.add_argument("--active", required=True, type=int, choices=(0, 1))
    sr.set_defaults(func=cmd_set_rule)

    ac = sub.add_parser("add-contact",
                        help="upsert a contact (asserted facts only)")
    ac.add_argument("addr", help="email address (normalized to lowercase)")
    ac.add_argument("--display-name")
    ac.add_argument("--relation", help="concise human framing: 'dad', 'plumber', 'HOA'")
    ac.add_argument("--vip", type=int, choices=(0, 1), default=None)
    ac.add_argument("--notes")
    ac.set_defaults(func=cmd_add_contact)

    cx = sub.add_parser("context", help="print session-start context as JSON")
    cx.add_argument("--sessions", type=int, default=5, help="how many recent sessions of notes")
    cx.add_argument("--senders", help="comma-separated addresses to filter sender_stats")
    cx.set_defaults(func=cmd_context)

    bk = sub.add_parser(
        "backup",
        help="consistent dated copy of history.db (+config.yaml) into a "
             "backup dir, pruned to the newest --keep copies")
    bk.add_argument(
        "--dest",
        default=os.environ.get("EMAIL_TRIAGE_BACKUP_DIR",
                               "~/Documents/email-triage-backups"),
        help="destination directory (default: $EMAIL_TRIAGE_BACKUP_DIR or "
             "~/Documents/email-triage-backups — somewhere your cloud sync "
             "actually covers; ~/.local is not synced)")
    bk.add_argument("--keep", type=int, default=7,
                    help="dated copies to retain (default 7)")
    bk.set_defaults(func=cmd_backup)

    args = parser.parse_args()
    conn = connect(args.data_dir)
    try:
        args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
