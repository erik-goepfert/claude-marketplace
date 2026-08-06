#!/usr/bin/env python3
"""Create history.db (schema from email-skill-design.md §9) if it doesn't already exist."""

import argparse
import os
import sqlite3
from pathlib import Path

DEFAULT_DATA_DIR = Path(os.environ.get("EMAIL_TRIAGE_DATA_DIR", "~/.local/share/email-triage")).expanduser()

SCHEMA = """
-- Trust is a COLUMN property, decided at write time and applied on the
-- model-bound read path (history.py TRUSTED_COLUMNS, Phase 6.5). At rest every
-- value here is clean text with no wrapping tags, which is what lets the read
-- path wrap freshly per call and keeps a value re-readable from source. New
-- columns must be classified there -- see migrate()'s docstring.
CREATE TABLE IF NOT EXISTS events (
    id                 INTEGER PRIMARY KEY,
    session_started_at TEXT NOT NULL,
    occurred_at        TEXT NOT NULL,
    account            TEXT,
    thread_root_id     TEXT,
    message_id         TEXT,
    sender_addr        TEXT NOT NULL,
    subject            TEXT,
    thread_state       TEXT,
    category           TEXT,
    inactive_days      INTEGER,
    proposed_action    TEXT NOT NULL,
    final_action       TEXT NOT NULL,
    from_folder        TEXT,
    to_folder          TEXT,
    decided_by         TEXT NOT NULL CHECK (decided_by IN
                         ('auto','approved','override','manual')),
    rule_id            INTEGER,
    notes              TEXT,
    unsubscribed       INTEGER NOT NULL DEFAULT 0  -- user-confirmed; clicking
                                                    -- itself is never observed (§4.4)
);
CREATE INDEX IF NOT EXISTS idx_events_sender  ON events(sender_addr);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_started_at);

CREATE TABLE IF NOT EXISTS session_notes (
    id                 INTEGER PRIMARY KEY,
    session_started_at TEXT NOT NULL,
    written_at         TEXT NOT NULL,
    note               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_session ON session_notes(session_started_at);

CREATE TABLE IF NOT EXISTS contacts (
    addr         TEXT PRIMARY KEY,
    display_name TEXT,
    relation     TEXT,
    is_vip       INTEGER NOT NULL DEFAULT 0,
    notes        TEXT,
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    predicate   TEXT NOT NULL,
    action      TEXT NOT NULL,
    origin      TEXT NOT NULL CHECK (origin IN ('user','claude-proposed'))
);

-- Two different filters on purpose (2026-08-06); one used to do both jobs.
--   decisions / holds  -- "did you act at all, or leave it unchanged?"
--                         'hold' is the only no-op verb.
--   *_rate             -- "of the times you FINISHED with a thread, what did
--                         you do?" Denominator is TERMINAL actions only
--                         (reply/archive/trash) as an ALLOWLIST, so a future
--                         verb cannot silently join it. Non-terminal verbs
--                         (flag/activate/hold) leave the thread live and are
--                         not competing dispositions: counting them diluted
--                         reply_rate for any thread that was activated or
--                         held before being replied to (a draft-then-send on
--                         a one-interaction sender read as 0.50, not 1.00).
CREATE VIEW IF NOT EXISTS sender_stats AS
SELECT sender_addr,
       COUNT(*)                            AS events_total,
       SUM(final_action != 'hold')         AS decisions,
       SUM(final_action  = 'hold')         AS holds,
       AVG(final_action = 'reply')
           FILTER (WHERE final_action IN ('reply','archive','trash')) AS reply_rate,
       AVG(final_action = 'trash')
           FILTER (WHERE final_action IN ('reply','archive','trash')) AS trash_rate,
       AVG(decided_by = 'override') FILTER (WHERE decided_by != 'manual')
                                           AS override_rate,
       -- Counted from the 5.5 notes convention. Feeds the assume-unsubscribe
       -- policy (user, 2026-07-19): unsubscribable mail is noise until the
       -- user has declined that sender 2+ times; at the threshold the sender
       -- graduates to newsletter-or-above and unsubscribe stops being proposed.
       SUM(notes LIKE '%declined-unsubscribe%') AS declined_unsubs,
       MAX(occurred_at)                    AS last_seen_at
FROM events GROUP BY sender_addr;
"""


def migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for DBs created before a schema change.

    ADDING OR RENAMING A COLUMN HERE? Classify it in history.py's
    TRUSTED_COLUMNS / HANDLE_COLUMNS / UNTRUSTED_COLUMNS (Phase 6.5). This is
    the re-review trigger, placed where the schema actually changes rather than
    on a calendar. Default-deny means a new column fails SAFE at runtime
    (escaped and declared) even if you forget -- but possibly over-wrapped, and
    an unclassified column fails test_history.py's classification test on
    purpose, so the drift surfaces here instead of quietly.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    if "unsubscribed" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN unsubscribed INTEGER NOT NULL DEFAULT 0")

    # 2026-08-06 vocabulary migration: 'skip' -> 'hold' in both action
    # columns. Value rename only -- no rows added, removed, or otherwise
    # altered, and idempotent (the WHERE matches nothing on a second run).
    # 'draft' needs no data migration: it was never written (verified 0 rows
    # in both columns before removal from ACTIONS).
    for col in ("proposed_action", "final_action"):
        conn.execute(f"UPDATE events SET {col} = 'hold' WHERE {col} = 'skip'")

    view_cols = {row[1] for row in conn.execute("PRAGMA table_info(sender_stats)")}
    # Views can't ALTER; drop and let the SCHEMA block recreate it. `holds`
    # detects the pre-2026-08-06 view (which had `skips` and used one filter
    # for both the decisive count and the rate denominators).
    if view_cols and not {"declined_unsubs", "holds"} <= view_cols:
        conn.execute("DROP VIEW sender_stats")
        conn.executescript(SCHEMA)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=f"Data directory for history.db (default: {DEFAULT_DATA_DIR}, "
        "override with $EMAIL_TRIAGE_DATA_DIR)",
    )
    args = parser.parse_args()

    args.data_dir.mkdir(parents=True, exist_ok=True)
    db_path = args.data_dir / "history.db"

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        migrate(conn)
        conn.commit()
    finally:
        conn.close()

    print(f"history.db ready at {db_path}")


if __name__ == "__main__":
    main()
