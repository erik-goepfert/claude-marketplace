#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["duckdb"]
# ///
"""DuckDB-over-SQLite analytics for the email-triage skill (Phase 3, optional).

Design refs:
  - email-skill-design.md §9 "Analytics access" -- the exact ATTACH
    incantation, the two verified DuckDB-over-SQLite gotchas (aggregate VIEWs
    are unreadable through the sqlite scanner; boolean predicates need
    explicit ::INT casts for SUM/AVG), the `events`/`sender_stats` schema,
    and the "facts, not aggregates" principle this script exists to serve --
    every number here is computed fresh from `events`/`envelopes`, never
    read from a stored column.
  - implementation-plan.md, Phase 3 item and its 2026-07-04 decision note:
    DuckDB is a richer *query engine* for analysis, not part of the core
    learning loop (context read, sender_stats, rule proposals stay
    SQLite-backed); this helper is where the attach quirks get encoded once,
    so SKILL.md never has to write raw DuckDB SQL.
  - scripts/history.py (events/rules/session_notes schema, ACTIONS/
    DECIDED_BY/CATEGORIES enums, CLI house style) and scripts/indexer.py
    (envelopes schema, folder_tier values).

DuckDB is an *optional* dependency (design §14: "nothing depends on it").
Everything else in this script -- and every other script in this plugin --
is stdlib. If duckdb isn't installed, this script alone is unusable; core
triage (history.py, indexer.py, threads.py, act.py) is entirely unaffected.

Subcommands (JSON to stdout, indent=2, sort_keys=True):
  categories  Per content-category action mix and unsubscribe rate, over
              DECISIVE (non-skip) events -- the "how often do I actually
              unsubscribe from marketing" metric that motivated
              events.unsubscribed (§9).
  domains     Per sender-domain (substring after '@', lowercased) action
              mix, unsubscribe total, override rate, first/last seen.
  trends      ISO-week buckets of triage volume over the last N weeks.
  rules       Per rule: events driven (via rule_id), override rate, last
              used -- includes never-used rules (LEFT JOIN from h.rules).
  sessions    Per session_started_at: events, action mix, time spread,
              session-note count.
  candidates  Rule-proposal fuel: senders you decisively trash almost every
              time, with no existing rule already covering them, who have
              mail arriving in the last N days. REQUIRES index.db.

Only `history.db` (h) is required; `index.db` (i) is attached when present
and required only by `candidates`. Every other subcommand works from
history.db alone -- matches history.py's own principle that the history DB
is self-contained (§9) and can outlive/precede the index.
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

try:
    import duckdb
except ImportError:
    sys.exit(
        "error: analytics.py needs the optional 'duckdb' dependency "
        "(pip install duckdb / brew install duckdb-python) -- core triage "
        "works fine without it; this is the only script that needs it."
    )

DEFAULT_DATA_DIR = Path(os.environ.get("EMAIL_TRIAGE_DATA_DIR", "~/.local/share/email-triage")).expanduser()

ACTIONS = ("flag", "reply", "archive", "trash", "skip", "activate")


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_str_days_ago(days: int) -> str:
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_str_weeks_ago(weeks: int) -> str:
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(weeks=weeks)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_div(numerator, denominator):
    """None (JSON null), not a ZeroDivisionError, when there's nothing to
    divide by -- "rates null when decisive=0" per the empty-data contract."""
    if not denominator:
        return None
    return numerator / denominator


def connect(data_dir: Path, *, need_index: bool = False) -> duckdb.DuckDBPyConnection:
    """Attach history.db (required) and index.db (optional unless
    need_index) read-only, per design §9's exact incantation.

    Two verified DuckDB-over-SQLite gotchas (§9), encoded here once so no
    other file in this plugin has to know about them:
      1. Query BASE TABLES only, never the SQLite views. DuckDB's sqlite
         scanner can't execute a view whose stored definition compiles to
         SQLite's internal count_star() (what COUNT(*) becomes inside a
         view) -- it fails with "no such function: count_star". h.sender_stats
         is exactly such a view and is therefore unreadable from here; every
         query below re-expresses that logic against h.events directly.
      2. Cast booleans to INT explicitly before SUM/AVG. SQLite treats
         `x = 'y'` as 0/1 so `AVG(final_action = 'reply')` "just works"
         there; DuckDB keeps comparisons BOOLEAN and rejects avg(BOOLEAN).
         Every predicate aggregated below is written `(predicate)::INT`.
    """
    history_db = data_dir / "history.db"
    if not history_db.exists():
        sys.exit(f"error: {history_db} not found -- run init_history_db.py first")
    index_db = data_dir / "index.db"
    have_index = index_db.exists()
    if need_index and not have_index:
        sys.exit(f"error: {index_db} not found -- this subcommand requires index.db (run indexer.py first)")

    con = duckdb.connect()
    con.execute("INSTALL sqlite; LOAD sqlite;")
    # ATTACH is DDL, not a regular statement -- DuckDB doesn't accept a
    # bound parameter here (`ATTACH ? AS h ...` is a parser error), so the
    # path is interpolated with SQL single-quote escaping instead.
    con.execute(f"ATTACH '{_sql_quote(history_db)}' AS h (TYPE sqlite, READ_ONLY)")
    if have_index:
        con.execute(f"ATTACH '{_sql_quote(index_db)}' AS i (TYPE sqlite, READ_ONLY)")
    return con


def _sql_quote(path: Path) -> str:
    return str(path).replace("'", "''")


# --------------------------------------------------------------------------
# categories
# --------------------------------------------------------------------------

CATEGORIES_SQL = """
SELECT category,
       COUNT(*)                                       AS events,
       SUM((final_action != 'skip')::INT)             AS decisive,
       SUM((final_action = 'trash')::INT)              AS trash_n,
       SUM((final_action = 'archive')::INT)            AS archive_n,
       SUM((final_action = 'activate')::INT)           AS activate_n,
       SUM((final_action = 'reply')::INT)              AS reply_n,
       SUM((final_action = 'flag')::INT)               AS flag_n,
       SUM(unsubscribed)                               AS unsubscribed_n
FROM h.events
GROUP BY category
"""


def cmd_categories(con: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> dict:
    rows = con.execute(CATEGORIES_SQL).fetchall()
    cols = [d[0] for d in con.description]
    out = []
    for row in rows:
        r = dict(zip(cols, row))
        decisive = r["decisive"] or 0
        out.append(
            {
                "category": r["category"],
                "events": r["events"],
                "decisive": decisive,
                "action_mix": {
                    "trash_rate": safe_div(r["trash_n"], decisive),
                    "archive_rate": safe_div(r["archive_n"], decisive),
                    "activate_rate": safe_div(r["activate_n"], decisive),
                    "reply_rate": safe_div(r["reply_n"], decisive),
                    "flag_rate": safe_div(r["flag_n"], decisive),
                },
                "unsubscribed": {
                    "count": r["unsubscribed_n"] or 0,
                    "rate": safe_div(r["unsubscribed_n"], decisive),
                },
            }
        )
    out.sort(key=lambda r: (r["category"] is None, r["category"] or ""))
    return {"categories": out}


# --------------------------------------------------------------------------
# domains
# --------------------------------------------------------------------------

DOMAINS_SQL = """
SELECT lower(split_part(sender_addr, '@', 2))           AS domain,
       COUNT(*)                                         AS events,
       SUM((final_action != 'skip')::INT)                AS decisive,
       SUM((final_action = 'trash')::INT)                 AS trash_n,
       SUM((final_action = 'reply')::INT)                 AS reply_n,
       SUM((final_action = 'archive')::INT)               AS archive_n,
       SUM(unsubscribed)                                 AS unsubscribed_total,
       SUM((decided_by = 'override')::INT)                AS override_n,
       SUM((decided_by != 'manual')::INT)                 AS non_manual_n,
       MIN(occurred_at)                                  AS first_seen,
       MAX(occurred_at)                                  AS last_seen
FROM h.events
WHERE (? IS NULL OR category = ?)
GROUP BY domain
HAVING COUNT(*) >= ?
ORDER BY events DESC, domain
"""


def cmd_domains(con: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> dict:
    rows = con.execute(DOMAINS_SQL, [args.category, args.category, args.min_events]).fetchall()
    cols = [d[0] for d in con.description]
    out = []
    for row in rows:
        r = dict(zip(cols, row))
        decisive = r["decisive"] or 0
        out.append(
            {
                "domain": r["domain"],
                "events": r["events"],
                "decisive": decisive,
                "trash_rate": safe_div(r["trash_n"], decisive),
                "reply_rate": safe_div(r["reply_n"], decisive),
                "archive_rate": safe_div(r["archive_n"], decisive),
                "unsubscribed_total": r["unsubscribed_total"] or 0,
                "override_rate": safe_div(r["override_n"], r["non_manual_n"]),
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
            }
        )
    return {"domains": out, "min_events": args.min_events, "category": args.category}


# --------------------------------------------------------------------------
# trends
# --------------------------------------------------------------------------

TRENDS_SQL = """
SELECT strftime(CAST(occurred_at AS TIMESTAMP), '%G-W%V')  AS iso_week,
       COUNT(*)                                            AS events,
       SUM((final_action = 'trash')::INT)                   AS trash_n,
       SUM((final_action = 'archive')::INT)                 AS archive_n,
       SUM((final_action = 'activate')::INT)                AS activate_n,
       SUM((final_action = 'reply')::INT)                   AS reply_n,
       SUM((final_action = 'flag')::INT)                    AS flag_n,
       SUM((final_action = 'skip')::INT)                    AS skip_n,
       COUNT(DISTINCT sender_addr)                          AS distinct_senders,
       COUNT(DISTINCT session_started_at)                   AS distinct_sessions
FROM h.events
WHERE occurred_at >= ?
GROUP BY iso_week
ORDER BY iso_week
"""


def cmd_trends(con: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> dict:
    cutoff = iso_str_weeks_ago(args.weeks)
    rows = con.execute(TRENDS_SQL, [cutoff]).fetchall()
    cols = [d[0] for d in con.description]
    weeks = []
    for row in rows:
        r = dict(zip(cols, row))
        weeks.append(
            {
                "iso_week": r["iso_week"],
                "events": r["events"],
                "action_counts": {a: r.get(f"{a}_n", 0) or 0 for a in ACTIONS},
                "distinct_senders": r["distinct_senders"],
                "distinct_sessions": r["distinct_sessions"],
            }
        )
    return {"weeks": weeks, "weeks_requested": args.weeks, "since": cutoff}


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------

RULES_SQL = """
SELECT r.id                                          AS rule_id,
       r.predicate                                   AS predicate,
       r.action                                       AS action,
       r.origin                                       AS origin,
       r.active                                       AS active,
       r.created_at                                   AS created_at,
       COUNT(e.id)                                    AS events_driven,
       SUM((e.decided_by = 'override')::INT)          AS override_n,
       MAX(e.occurred_at)                             AS last_used
FROM h.rules r
LEFT JOIN h.events e ON e.rule_id = r.id
GROUP BY r.id, r.predicate, r.action, r.origin, r.active, r.created_at
ORDER BY r.id
"""


def cmd_rules(con: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> dict:
    rows = con.execute(RULES_SQL).fetchall()
    cols = [d[0] for d in con.description]
    out = []
    for row in rows:
        r = dict(zip(cols, row))
        events_driven = r["events_driven"] or 0
        out.append(
            {
                "rule_id": r["rule_id"],
                "predicate": r["predicate"],
                "action": r["action"],
                "origin": r["origin"],
                "active": bool(r["active"]),
                "created_at": r["created_at"],
                "events_driven": events_driven,
                "override_rate": safe_div(r["override_n"], events_driven),
                "last_used": r["last_used"],
            }
        )
    return {"rules": out}


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------

SESSIONS_SQL = """
SELECT session_started_at,
       COUNT(*)                                                            AS events,
       SUM((final_action = 'trash')::INT)                                   AS trash_n,
       SUM((final_action = 'archive')::INT)                                 AS archive_n,
       SUM((final_action = 'activate')::INT)                                AS activate_n,
       SUM((final_action = 'reply')::INT)                                   AS reply_n,
       SUM((final_action = 'flag')::INT)                                    AS flag_n,
       SUM((final_action = 'skip')::INT)                                    AS skip_n,
       DATEDIFF('minute', MIN(CAST(occurred_at AS TIMESTAMP)),
                          MAX(CAST(occurred_at AS TIMESTAMP)))              AS spread_minutes
FROM h.events
GROUP BY session_started_at
ORDER BY session_started_at DESC
LIMIT ?
"""

SESSION_NOTES_SQL = """
SELECT session_started_at, COUNT(*) AS notes
FROM h.session_notes
GROUP BY session_started_at
"""


def cmd_sessions(con: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> dict:
    rows = con.execute(SESSIONS_SQL, [args.limit]).fetchall()
    cols = [d[0] for d in con.description]
    notes_by_session = dict(con.execute(SESSION_NOTES_SQL).fetchall())
    out = []
    for row in rows:
        r = dict(zip(cols, row))
        out.append(
            {
                "session_started_at": r["session_started_at"],
                "events": r["events"],
                "action_counts": {a: r.get(f"{a}_n", 0) or 0 for a in ACTIONS},
                "spread_minutes": r["spread_minutes"],
                "notes": notes_by_session.get(r["session_started_at"], 0),
            }
        )
    return {"sessions": out, "limit": args.limit}


# --------------------------------------------------------------------------
# candidates (requires index.db)
# --------------------------------------------------------------------------

CANDIDATES_SQL = """
WITH sender_agg AS (
    SELECT lower(sender_addr)                                           AS sender,
           COUNT(*) FILTER (WHERE final_action != 'skip')                AS decisive,
           AVG((final_action = 'trash')::INT)
               FILTER (WHERE final_action != 'skip')                     AS trash_rate
    FROM h.events
    GROUP BY 1
    HAVING COUNT(*) FILTER (WHERE final_action != 'skip') >= ?
       AND AVG((final_action = 'trash')::INT)
               FILTER (WHERE final_action != 'skip') >= 0.8
),
recent_arrivals AS (
    SELECT lower(from_addr)   AS sender,
           MAX(date_utc)      AS last_arrival
    FROM i.envelopes
    WHERE folder_tier IN ('untriaged', 'allmail')
      AND date_utc >= ?
    GROUP BY 1
)
SELECT s.sender, s.decisive, s.trash_rate, r.last_arrival
FROM sender_agg s
JOIN recent_arrivals r ON r.sender = s.sender
WHERE NOT EXISTS (
    SELECT 1 FROM h.rules ru
    WHERE ru.active = 1 AND ru.predicate LIKE '%' || s.sender || '%'
)
ORDER BY s.decisive DESC, s.sender
"""


def cmd_candidates(con: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> dict:
    cutoff = iso_str_days_ago(args.days)
    rows = con.execute(CANDIDATES_SQL, [args.min_events, cutoff]).fetchall()
    out = [
        {
            "sender": sender,
            "decisive": decisive,
            "trash_rate": trash_rate,
            "last_arrival": last_arrival,
            "suggested_predicate": f"sender = {sender}",
        }
        for sender, decisive, trash_rate, last_arrival in rows
    ]
    return {"candidates": out, "min_events": args.min_events, "days": args.days, "since": cutoff}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    sub = parser.add_subparsers(dest="command", required=True)

    cat = sub.add_parser("categories", help="per-category action mix + unsubscribe rate")
    cat.set_defaults(func=cmd_categories, need_index=False)

    dom = sub.add_parser("domains", help="per-sender-domain action mix + override rate")
    dom.add_argument("--min-events", type=int, default=3)
    dom.add_argument("--category")
    dom.set_defaults(func=cmd_domains, need_index=False)

    tr = sub.add_parser("trends", help="ISO-week triage volume trends")
    tr.add_argument("--weeks", type=int, default=12)
    tr.set_defaults(func=cmd_trends, need_index=False)

    ru = sub.add_parser("rules", help="per-rule effectiveness, incl. zero-event rules")
    ru.set_defaults(func=cmd_rules, need_index=False)

    se = sub.add_parser("sessions", help="per-session action mix + time spread")
    se.add_argument("--limit", type=int, default=10)
    se.set_defaults(func=cmd_sessions, need_index=False)

    ca = sub.add_parser(
        "candidates",
        help="rule-proposal fuel: chronically-trashed senders still arriving (requires index.db)",
    )
    ca.add_argument("--min-events", type=int, default=5)
    ca.add_argument("--days", type=int, default=30)
    ca.set_defaults(func=cmd_candidates, need_index=True)

    args = parser.parse_args()
    con = connect(args.data_dir, need_index=args.need_index)
    try:
        result = args.func(con, args)
    finally:
        con.close()
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    print()


if __name__ == "__main__":
    main()
