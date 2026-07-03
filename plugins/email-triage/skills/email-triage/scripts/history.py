#!/usr/bin/env python3
"""CLI for the email-triage history DB: log events/notes, read session-start context.

Subcommands:
  log-event   Insert one triage decision (see design doc §9 for semantics).
  log-note    Append a session note.
  context     Print session-start context (recent notes, active rules,
              sender stats) as JSON.
"""

import argparse
import datetime
import json
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_DATA_DIR = Path(os.environ.get("EMAIL_TRIAGE_DATA_DIR", "~/.local/share/email-triage")).expanduser()

ACTIONS = ("flag", "reply", "archive", "trash", "skip")
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


def cmd_log_event(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    conn.execute(
        """INSERT INTO events (session_started_at, occurred_at, account,
               thread_root_id, message_id, sender_addr, subject,
               thread_state, category, inactive_days,
               proposed_action, final_action, from_folder, to_folder,
               decided_by, rule_id, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            args.session,
            args.occurred_at or utc_now(),
            args.account,
            args.thread_root_id,
            args.message_id,
            args.sender.lower(),
            args.subject,
            args.thread_state,
            args.category,
            args.inactive_days,
            args.proposed,
            args.final,
            args.from_folder,
            args.to_folder,
            args.decided_by,
            args.rule_id,
            args.notes,
        ),
    )
    conn.commit()
    print("event logged")


def cmd_log_note(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    conn.execute(
        "INSERT INTO session_notes (session_started_at, written_at, note) VALUES (?, ?, ?)",
        (args.session, utc_now(), args.note),
    )
    conn.commit()
    print("note logged")


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
    ev.set_defaults(func=cmd_log_event)

    nt = sub.add_parser("log-note", help="append a session note")
    nt.add_argument("--session", required=True, help="session_started_at (UTC ISO 8601)")
    nt.add_argument("note")
    nt.set_defaults(func=cmd_log_note)

    cx = sub.add_parser("context", help="print session-start context as JSON")
    cx.add_argument("--sessions", type=int, default=5, help="how many recent sessions of notes")
    cx.add_argument("--senders", help="comma-separated addresses to filter sender_stats")
    cx.set_defaults(func=cmd_context)

    args = parser.parse_args()
    conn = connect(args.data_dir)
    try:
        args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
