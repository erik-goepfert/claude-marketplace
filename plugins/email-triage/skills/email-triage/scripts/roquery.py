#!/usr/bin/env python3
"""Read-only SQL query surface, shared by `$HIST query` and `$IDX query`.

Extracted from history.py (2026-08-07) when 11.1 needed the same surface over
`index.db`. It lives in one file because **a second copy of a security boundary
is a second thing to get wrong** — the layers below were each verified
empirically against a live database, and that verification should not have to be
repeated per caller.

Enforcement is SQLite's, not ours. Five layers:

  1. `file:...?mode=ro`     -> "attempt to write a readonly database"
  2. `PRAGMA query_only=ON` -> same, belt and braces
  3. `setlimit(ATTACHED, 0)`-> closes the real hole: a read-only connection can
                               otherwise `ATTACH 'file:/anywhere?mode=rwc'` and
                               write THERE, because the attached database
                               carries its own flags. Verified: without this it
                               SUCCEEDS.
  4. progress handler       -> aborts runaway queries ("interrupted")
  5. wall-clock alarm       -> aborts what the progress handler cannot see.
                               Layer 4 counts SQLite VM steps, and no VM steps
                               occur while a Python callback is running, so a
                               catastrophically backtracking REGEXP pattern
                               would hang forever underneath it. Registering
                               REGEXP is what opened that gap, so REGEXP does
                               not ship without this.

Plus two properties of the driver: Python's `sqlite3` has no `readfile()` /
`writefile()` (those are CLI extensions, so the query language cannot touch the
filesystem), and `execute()` refuses more than one statement.

Because all of that is engine-enforced, there is deliberately **NO SQL parsing**
here. A regex blacklist over SQL is exactly the kind of control that leaks
(comments, CTEs wrapping DML, PRAGMA, stacked statements) and it would add
nothing on top of the layers above. The tests ARE the boundary.
"""

import re
import signal
import sqlite3

import untrusted

ROW_CAP = 200
MAX_PROGRESS_CALLBACKS = 50_000
PROGRESS_INTERVAL = 1_000
# Wall-clock ceiling for one statement (layer 5). Whole seconds because that is
# what signal.alarm takes; a query that legitimately needs longer than this over
# a mailbox index wants narrowing, not a bigger budget.
TIMEOUT_SECONDS = 20

_REGEX_CACHE_CAP = 64


class QueryError(Exception):
    """Raised instead of exiting, so each caller words its own error."""


# sqlite3 collapses ANY exception from a user-defined function into the opaque
# "user-defined function raised exception", so the real cause has to be carried
# out of band. Reset per run(), read only when that generic error surfaces.
_last_regex_error = []


def _regexp(pattern, value, _cache={}):
    """`col REGEXP 'pat'` — Python's sqlite3 has no regex operator.

    GLOB and LIKE are already native, so this exists only for the patterns they
    cannot express. Compiled patterns are cached because SQLite calls this once
    per row."""
    if value is None or pattern is None:
        return False
    compiled = _cache.get(pattern)
    if compiled is None:
        if len(_cache) >= _REGEX_CACHE_CAP:
            _cache.clear()
        try:
            compiled = _cache[pattern] = re.compile(pattern)
        except re.error as e:
            _last_regex_error.append(f"bad REGEXP pattern: {e}")
            raise QueryError(f"bad REGEXP pattern: {e}") from e
    return compiled.search(str(value)) is not None


def connect(db_path):
    """A connection that cannot write, cannot ATTACH, and cannot run forever."""
    if not hasattr(sqlite3.Connection, "setlimit"):
        raise QueryError(
            "needs Python 3.11+ (Connection.setlimit) to close the ATTACH "
            "escape; refusing to open a query surface without it")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    conn.create_function("regexp", 2, _regexp, deterministic=True)
    return conn


def run(conn, sql, limit):
    """Execute one statement. Returns (rows, truncated).

    Fetches limit+1 so truncation is observed rather than inferred."""
    calls = 0

    def guard():
        nonlocal calls
        calls += 1
        return 1 if calls > MAX_PROGRESS_CALLBACKS else 0

    def on_alarm(_signum, _frame):
        raise QueryError(f"query exceeded {TIMEOUT_SECONDS}s and was aborted")

    conn.set_progress_handler(guard, PROGRESS_INTERVAL)
    _last_regex_error.clear()
    previous = None
    armed = False
    try:
        try:
            previous = signal.signal(signal.SIGALRM, on_alarm)
            signal.alarm(TIMEOUT_SECONDS)
            armed = True
        except ValueError:
            # Not the main thread (a test runner, an embedded caller). Layers
            # 1-4 still hold; only the REGEXP ceiling is unavailable, so say so
            # rather than pretend it is armed.
            pass
        try:
            cur = conn.execute(sql)
            rows = [dict(r) for r in cur.fetchmany(limit + 1)]
        except (sqlite3.Error, sqlite3.Warning) as e:
            raise QueryError(_last_regex_error[0] if _last_regex_error
                             else str(e)) from e
    finally:
        if armed:
            signal.alarm(0)
            if previous is not None:
                signal.signal(signal.SIGALRM, previous)
        conn.set_progress_handler(None, 0)
    truncated = len(rows) > limit
    return rows[:limit], truncated


def bare_name_trust(trusted_columns, handle_columns, untrusted_columns):
    """(trusted, handle) bare column NAMES for an arbitrary result set.

    6.5 keys trust per (table, column) because the same name differs by table --
    `notes` is untrusted in events AND contacts, `account` is trusted only in
    events. A join has no single table, so that keying cannot apply here.
    Resolution: classify by bare name with **untrusted winning every tie**, and
    unknown names untrusted by default-deny. The cost is over-wrapping a column
    that would have been safe in its own table; the alternative cost is
    under-wrapping one that is not. Only one of those is a security failure."""
    def union(mapping):
        return set().union(*mapping.values()) if mapping else set()

    untrusted_names = union(untrusted_columns)
    handle_names = union(handle_columns) - untrusted_names
    trusted_names = union(trusted_columns) - untrusted_names - handle_names
    return trusted_names, handle_names


def payload(rows, truncated, limit, trusted_names, handle_names):
    """The `_trust`-wrapped result envelope both callers emit."""
    out = {"rows": rows, "row_count": len(rows)}
    if truncated:
        out["truncated"] = True
        out["hint"] = (f"stopped at --limit {limit}; narrow the query "
                       "or raise the limit deliberately")
    return untrusted.apply_trust(
        out,
        untrusted.under("rows", trusted_names) | {"row_count", "truncated",
                                                  "hint"},
        handle_fields=untrusted.under("rows", handle_names))
