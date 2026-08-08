#!/usr/bin/env python3
"""One search across all three stores (Phase 11.6).

Answers the plain question — *"search everywhere for raimund"* — over:

  * `index.db` envelopes  : subject / from_addr / to_addr
  * `index.db` body_fts   : decoded message bodies, ranked (11.6)
  * `history.db`          : session notes, rules, contacts, event subjects/notes

Every section returns the id or key needed to go read the thing. Bodies return
ranked message-ids and no snippets — see indexer.py's FTS_SCHEMA for why that is
deliberate rather than a limitation.

**The term is matched literally.** `$SEARCH` takes a phrase, not a query
language: it is the "look everywhere" operation. `$IDX search --term` is the
precise one, where FTS5 syntax (prefixes, boolean operators, NEAR) applies.
Keeping them separate means a term containing a stray quote or asterisk cannot
turn into a syntax error here.

Rule 6, and it is the whole reason this note exists: a search term the USER
supplies is fine. A term lifted out of an email is email content choosing what
runs, and is not.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import history
import indexer
import roquery
import untrusted

DEFAULT_DATA_DIR = Path(
    os.path.expanduser(os.environ.get("EMAIL_TRIAGE_DATA_DIR",
                                      "~/.local/share/email-triage")))
SECTION_LIMIT = 20

ENVELOPE_SQL = """SELECT account, message_id, folder, folder_tier, date_utc,
                         subject, from_addr, to_addr
                  FROM envelopes
                  WHERE subject REGEXP ? OR from_addr REGEXP ?
                     OR to_addr REGEXP ?
                  ORDER BY date_utc DESC LIMIT ?"""

BODY_SQL = """SELECT e.account, e.message_id, e.folder_tier, e.date_utc,
                     e.subject, e.from_addr, bm25(body_fts) AS rank
              FROM body_fts JOIN envelopes e ON e.id = body_fts.rowid
              WHERE body_fts MATCH ? ORDER BY rank LIMIT ?"""

# Only columns that can hold the searched text. Each carries enough identity to
# go read the row it came from.
HISTORY_SQL = {
    "session_notes": """SELECT session_started_at, written_at, note
                        FROM session_notes WHERE note REGEXP ?
                        ORDER BY written_at DESC LIMIT ?""",
    "rules": """SELECT id, predicate, action, created_at
                FROM rules WHERE predicate REGEXP ? OR action REGEXP ?
                ORDER BY id LIMIT ?""",
    "contacts": """SELECT addr, display_name, relation, notes
                   FROM contacts
                   WHERE addr REGEXP ? OR display_name REGEXP ?
                      OR relation REGEXP ? OR notes REGEXP ?
                   ORDER BY addr LIMIT ?""",
    "events": """SELECT id, occurred_at, message_id, sender_addr, subject,
                        final_action, notes
                 FROM events WHERE subject REGEXP ? OR notes REGEXP ?
                    OR sender_addr REGEXP ?
                 ORDER BY occurred_at DESC LIMIT ?""",
}
HISTORY_PARAM_COUNT = {"session_notes": 1, "rules": 2, "contacts": 4,
                       "events": 3}


def literal_regex(term: str) -> str:
    """Case-insensitive literal match.

    `re.escape` first: a term is a phrase, so `.` and `*` in it are characters,
    not operators. `LIKE` was the obvious alternative and is worse -- a term
    containing `%` or `_` would silently become a wildcard."""
    return f"(?i){re.escape(term)}"


def fts_phrase(term: str) -> str:
    """The term as an FTS5 phrase. Internal double quotes are doubled, which is
    how FTS5 escapes them; without this a quote in the term is a syntax error."""
    return '"' + term.replace('"', '""') + '"'


def _rows(conn, sql, params):
    return [dict(r) for r in conn.execute(sql, params)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--term", required=True,
                    help="literal phrase to look for, case-insensitive")
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--limit", type=int, default=SECTION_LIMIT,
                    help=f"max rows per section (default {SECTION_LIMIT})")
    args = ap.parse_args(argv)

    index_db = args.data_dir / "index.db"
    history_db = args.data_dir / "history.db"
    rx = literal_regex(args.term)
    result = {"term": args.term, "envelopes": [], "bodies": [], "history": {},
              "unavailable": []}

    if index_db.exists():
        conn = roquery.connect(index_db)
        try:
            result["envelopes"] = _rows(conn, ENVELOPE_SQL,
                                       (rx, rx, rx, args.limit))
            try:
                result["bodies"] = _rows(
                    conn, BODY_SQL, (fts_phrase(args.term), args.limit))
            except sqlite3.OperationalError as e:
                # No FTS table yet is the expected case before backfill runs.
                # Reported, never silent: "no body hits" and "body search does
                # not exist yet" must not look the same.
                result["unavailable"].append(
                    "bodies: run `indexer.py backfill-bodies` once"
                    if "no such table" in str(e) else f"bodies: {e}")
        finally:
            conn.close()
    else:
        result["unavailable"].append(f"envelopes+bodies: no {index_db}")

    if history_db.exists():
        conn = roquery.connect(history_db)
        try:
            for table, sql in HISTORY_SQL.items():
                params = (rx,) * HISTORY_PARAM_COUNT[table] + (args.limit,)
                try:
                    result["history"][table] = _rows(conn, sql, params)
                except sqlite3.OperationalError as e:
                    result["unavailable"].append(f"history.{table}: {e}")
        finally:
            conn.close()
    else:
        result["unavailable"].append(f"history: no {history_db}")

    result["counts"] = {
        "envelopes": len(result["envelopes"]),
        "bodies": len(result["bodies"]),
        **{f"history.{t}": len(v) for t, v in result["history"].items()},
    }
    result["counts"]["total"] = sum(
        v for k, v in result["counts"].items() if k != "total")

    # Trust: each section is classified by ITS OWN database's column map, since
    # the same bare name can differ between them. `term` is the user's own text,
    # echoed back so a saved result says what produced it.
    idx_trusted, idx_handles = indexer.query_column_trust()
    hist_trusted, hist_handles = history.query_column_trust()
    trusted = ({"term", "unavailable"}
               | {f"counts.{k}" for k in result["counts"]}
               | untrusted.under("envelopes", idx_trusted)
               | untrusted.under("bodies", idx_trusted | {"rank"}))
    handles = (untrusted.under("envelopes", idx_handles)
               | untrusted.under("bodies", idx_handles))
    for table in result["history"]:
        trusted |= untrusted.under(f"history.{table}", hist_trusted)
        handles |= untrusted.under(f"history.{table}", hist_handles)

    json.dump(untrusted.apply_trust(result, trusted, handle_fields=handles),
              sys.stdout, indent=2, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
