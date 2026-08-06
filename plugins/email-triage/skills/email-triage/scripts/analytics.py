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
              DECISIVE (non-hold) events -- the "how often do I actually
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import untrusted  # noqa: E402

try:
    import duckdb
except ImportError:
    sys.exit(
        "error: analytics.py needs the optional 'duckdb' dependency "
        "(pip install duckdb / brew install duckdb-python) -- core triage "
        "works fine without it; this is the only script that needs it."
    )

DEFAULT_DATA_DIR = Path(os.environ.get("EMAIL_TRIAGE_DATA_DIR", "~/.local/share/email-triage")).expanduser()

ACTIONS = ("flag", "reply", "archive", "trash", "hold", "activate")


# Trust classification (Phase 6.2; untrusted-content-skill-design.md §5).
# Aggregates are not automatically safe: a COUNT is, but the GROUP BY key it
# came from is often sender text, and this module is UC §3's "second door" onto
# the untrusted predicate/action columns that `history.py context` also reads.
#
# Only string leaves need classifying -- every count and rate here is a number.
ANALYTICS_TRUSTED = {
    # Timestamps and week labels are our own strftime output, not header text.
    "since", "weeks.iso_week", "sessions.session_started_at",
    "domains.first_seen", "domains.last_seen",
    "rules.created_at", "rules.last_used",
    "candidates.last_arrival",
    # `origin` is CHECK-constrained to ('user','claude-proposed') in the
    # schema, so it is structurally an enum.
    "rules.origin",
    # The --category CLI argument echoed back, and the data column of the same
    # name. Both are safe for one reason: `category` is enum-validated on EVERY
    # write path (history.event_row_from_dict check_choice against CATEGORIES,
    # plus argparse choices), so it is structurally an enum and cannot carry
    # content. Invariant 7's concern is model-written PROSE derived from email
    # (`notes`, `subject`); a validated label out of our own fixed vocabulary is
    # a different thing, and declaring it untrusted would be actively
    # misleading -- the model reasons WITH these labels.
    "category", "categories.category",
}
# Handles: values that exist to be handed straight back to another command, so
# escaping them would corrupt what they are for. `$HIST add-rule` stores a
# predicate and later matches senders against it -- an entity-escaped predicate
# silently never matches anything, the same failure class as an escaped
# unsubscribe URL. Measured 2026-08-06: 0 of 17,061 indexed from_addr values
# contain `&`, `<` or `>`, so escaping would be a no-op today; but `&` is legal
# atext and `<` survives a quoted local part, which makes it sender-triggerable
# rather than impossible.
ANALYTICS_HANDLES = {"candidates.sender", "candidates.suggested_predicate"}
# Everything else is escaped and declared by default-deny. The distinction worth
# naming, since two of these are both "predicates":
#   rules.predicate / rules.action  -- coming OUT of the DB for the model to
#       read. Display text: escape it.
#   candidates.suggested_predicate  -- going INTO the DB via add-rule. Must
#       survive verbatim: don't.
# Escape on the way to the model's eyes; never escape what it must hand back.
# Also untrusted: `domains.domain`, derived from sender_addr by splitting on
# `@` -- a transformation of sender text is still sender text.


def wrap_for_model(result: dict) -> dict:
    """The single place analytics output is trust-classified.

    main() calls it and so do the trust tests, deliberately: a test that
    restated these arguments could not catch main() drifting away from them,
    and every subcommand here returns its report through one exit.
    """
    return untrusted.apply_trust(result, ANALYTICS_TRUSTED,
                                 handle_fields=ANALYTICS_HANDLES)


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
       SUM((final_action != 'hold')::INT)             AS decisive,
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

# NOTE (2026-08-06): the three rates here are over `terminal`, not
# `decisive`. This query emits only terminal rates (trash/reply/archive) with
# no activate_rate/flag_rate, so a non-hold denominator left the non-terminal
# mass silently unaccounted and depressed every rate for a domain whose
# threads get activated or flagged before being resolved. Contrast
# `categories`, which deliberately KEEPS the `decisive` denominator because
# its action_mix does include activate_rate and flag_rate and is a complete
# distribution -- don't "fix" that one to match this one.
DOMAINS_SQL = """
SELECT lower(split_part(sender_addr, '@', 2))           AS domain,
       COUNT(*)                                         AS events,
       SUM((final_action != 'hold')::INT)                AS decisive,
       SUM((final_action IN ('reply','archive','trash'))::INT) AS terminal,
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
        terminal = r["terminal"] or 0
        out.append(
            {
                "domain": r["domain"],
                "events": r["events"],
                "decisive": decisive,
                "terminal": terminal,
                # Rates divide by `terminal` -- "of the threads you FINISHED
                # with this domain, what did you do?" safe_div returns None on
                # a 0 denominator, so a domain with no terminal actions
                # reports null rather than a fabricated 0.0.
                "trash_rate": safe_div(r["trash_n"], terminal),
                "reply_rate": safe_div(r["reply_n"], terminal),
                "archive_rate": safe_div(r["archive_n"], terminal),
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
       SUM((final_action = 'hold')::INT)                    AS hold_n,
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
       SUM((final_action = 'hold')::INT)                                    AS hold_n,
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
-- trash_rate is over TERMINAL actions only (allowlist, matching
-- sender_stats): a sender you activate or hold before eventually trashing
-- would otherwise look less decisively-trashed than they are, weakening
-- exactly the rule proposals this query exists to fuel. `decisive` keeps the
-- broader non-hold sense -- it answers "did you act at all", not "how did
-- you finish". (2026-08-06)
WITH sender_agg AS (
    SELECT lower(sender_addr)                                           AS sender,
           COUNT(*) FILTER (WHERE final_action != 'hold')                AS decisive,
           AVG((final_action = 'trash')::INT)
               FILTER (WHERE final_action IN ('reply','archive','trash')) AS trash_rate
    FROM h.events
    GROUP BY 1
    HAVING COUNT(*) FILTER (WHERE final_action != 'hold') >= ?
       AND AVG((final_action = 'trash')::INT)
               FILTER (WHERE final_action IN ('reply','archive','trash')) >= 0.8
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
-- Rule-coverage check. Interprets the two predicate FORMS actually in use
-- rather than substring-matching the whole predicate text (fixed 2026-08-06;
-- see design §15.4). The old `predicate LIKE '%' || sender || '%'` only ever
-- matched a predicate that spelled out the full address, so
-- `sender ends-with creditkarma.com` failed to cover
-- notifications@mail.creditkarma.com -- and Credit Karma was proposed as an
-- auto-trash candidate despite an active rule saying scan-then-trash and
-- NEVER propose unsubscribe.
--
-- Each branch mirrors the SEMANTICS of the rule it reads, so coverage is
-- neither stricter nor looser than the rule itself. The substring form is
-- KEPT as a final fallback: any predicate shape not parsed here still gets
-- the old behavior, so this is purely additive.
--
-- regexp_extract returns '' on no match, hence the <> '' guard on the
-- suffix branch: without it an unmatched extract would make the comparison
-- `sender LIKE '%'`, which matches EVERY sender and would silently hide all
-- candidates.
WHERE NOT EXISTS (
    SELECT 1 FROM h.rules ru
    WHERE ru.active = 1
      AND (
            -- 'sender = <addr>'  (exact)
            s.sender = regexp_extract(lower(ru.predicate),
                                      '^ *sender *= *([^ ]+) *$', 1)
            -- 'sender ends-with <suffix>'  (suffix, as the rule means it)
         OR (regexp_extract(lower(ru.predicate),
                            '^ *sender +ends-with +([^ ]+) *$', 1) <> ''
             AND s.sender LIKE '%' || regexp_extract(lower(ru.predicate),
                            '^ *sender +ends-with +([^ ]+) *$', 1))
            -- fallback: a predicate that literally names the address
         OR lower(ru.predicate) LIKE '%' || s.sender || '%'
          )
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
    json.dump(wrap_for_model(result), sys.stdout, indent=2, sort_keys=True)
    print()


if __name__ == "__main__":
    main()
