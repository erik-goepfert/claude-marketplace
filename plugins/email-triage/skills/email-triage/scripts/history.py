#!/usr/bin/env python3
"""CLI for the email-triage history DB: log events/notes, read session-start context.

Subcommands:
  log-event   Insert one triage decision (see design doc §9 for semantics).
  log-events  Insert a batch of decisions from a JSON array (one himalaya
              bulk action -> one bulk log call; all-or-nothing).
  log-note    Append a session note.
  context     Print session-start context (recent notes, active rules,
              sender stats) as JSON.
  add-rule / list-rules / update-rule / delete-rule / set-rule
              Full CRUD over rules (2026-08-10; before this the table was
              append-plus-a-visibility-bit, so a badly worded rule could
              only be retired and rewritten, leaving a dead row behind).
              The two MUTATING verbs refuse a rule that events cite unless
              --force: `events.rule_id` is a claim about the basis a
              decision was made on, so rewriting or deleting it rewrites
              history. `--active` alone is exempt -- retirement changes
              what is in force without touching the recorded basis.
  backup      Consistent dated copy of history.db (+config.yaml) into a
              cloud-synced folder; prunes to the newest N (2.8).
"""

import argparse
import datetime
import shutil
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import roquery
import untrusted  # noqa: E402

DEFAULT_DATA_DIR = Path(os.environ.get("EMAIL_TRIAGE_DATA_DIR", "~/.local/share/email-triage")).expanduser()

# Thread dispositions. Every verb names the condition or place it produces.
# TERMINAL (the conversation reached an end state): reply, archive, trash.
# NON-TERMINAL (thread stays live): flag, activate, hold.
#   hold  -- reviewed, left in whatever state it was already in; the event's
#            thread_state records WHICH state was held (needs-reply => reply
#            debt, holding => filing debt). Renamed from 'skip' 2026-08-06:
#            'skip' named the operator's behavior, not the thread's condition.
# 'draft' was removed 2026-08-06: saving a draft creates a DIFFERENT object,
# it is not a disposition of the thread. The thread's own verb (activate, or
# none if already live) carries the decision; the \Draft flag and the derived
# `drafted` status carry the draft itself.
ACTIONS = ("flag", "reply", "archive", "trash", "hold", "activate")
TERMINAL_ACTIONS = ("reply", "archive", "trash")
# "activate" (added Phase 2.7): moved to the live tier -- triaged, stays
# open, no flag. A real decision, so it counts toward `decisions` in
# sender_stats (unlike "hold"), but it is NOT terminal, so it stays out of
# the reply_rate/trash_rate denominators -- a thread activated before being
# replied to would otherwise halve that sender's reply_rate. to_folder
# carries the live tier's folder name.
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


def cmd_set_unsubscribed(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """Record a manual unsubscribe confirmed AFTER its event was logged.

    SKILL step 5 assumes confirmation precedes logging, which holds for the
    automated path -- an RFC 8058 2xx IS the confirmation, so `unsubscribed` can
    be set at log time. Manual links break that assumption: the user clicks them
    later, sometimes after the session has closed. Without a way to amend,
    `events.unsubscribed` permanently understates reality and every unsubscribe
    rate derived from it is wrong (added 2026-08-07, after exactly that).

    Deliberately narrow. It sets ONE boolean column, scoped to one session and an
    explicit sender list, and reports how many rows changed. It is not a general
    UPDATE surface -- `query` is read-only precisely so that arbitrary mutation
    of an irreplaceable DB is never one typo away."""
    addrs = [a.strip().lower() for a in args.senders.split(",") if a.strip()]
    if not addrs:
        sys.exit("error: --senders is empty")
    placeholders = ",".join("?" * len(addrs))
    cur = conn.execute(
        f"UPDATE events SET unsubscribed = 1 WHERE session_started_at = ? "
        f"AND sender_addr IN ({placeholders}) AND unsubscribed = 0",
        (args.session, *addrs))
    conn.commit()
    matched = conn.execute(
        f"SELECT sender_addr, COUNT(*) AS n FROM events "
        f"WHERE session_started_at = ? AND sender_addr IN ({placeholders}) "
        f"GROUP BY sender_addr",
        (args.session, *addrs)).fetchall()
    print(json.dumps(untrusted.apply_trust(
        {"updated": cur.rowcount,
         "per_sender": [{"sender_addr": r["sender_addr"], "events": r["n"]}
                        for r in matched]},
        {"updated"} | untrusted.under("per_sender", {"events"}),
        handle_fields=untrusted.under("per_sender", {"sender_addr"})),
        indent=2, sort_keys=True))


# A rule id is a caller-chosen slug (2026-08-10). Numbers collided with the
# SKILL's own engine rules 1-6 -- "rule 4" read as either one, and two rules
# ended up citing an engine rule by bare number while themselves sitting under
# that number.
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SLUG_LEN = (3, 32)


def _valid_slug_or_exit(slug: str) -> str:
    lo, hi = SLUG_LEN
    if not SLUG_RE.match(slug) or not (lo <= len(slug) <= hi):
        sys.exit(f"error: rule id must be kebab-case (lowercase, digits, "
                 f"single hyphens) and {lo}-{hi} characters: {slug!r}")
    return slug


def _refuse_if_slug_taken(conn: sqlite3.Connection, slug: str,
                          predicate=None, action=None) -> None:
    """A slug collision is information, so the two cases get different errors.

    Identical text means the caller is re-adding what already exists and should
    update instead. Different text means two distinct rules want one name, and
    the fix is a different name -- never an auto-suffix, because
    `receipt-filing` beside `receipt-filing-2` is the "which of these is live?"
    problem the slugs were meant to end."""
    row = conn.execute("SELECT * FROM rules WHERE id = ?", (slug,)).fetchone()
    if row is not None:
        state = "active" if row["active"] else "retired"
        if predicate == row["predicate"] and action == row["action"]:
            sys.exit(f"error: rule {slug!r} already exists with an IDENTICAL "
                     f"predicate and action (created {row['created_at']}, "
                     f"{state}). Nothing to add — did you mean "
                     f"`update-rule {slug}`?")
        sys.exit(
            f"error: rule {slug!r} already exists ({state}) and DIFFERS from "
            f"what you passed. Retry with a different id, or update that rule.\n"
            f"  existing predicate: {row['predicate'][:100]}\n"
            f"  existing action:    {row['action'][:100]}")

    # Free slug, but a decision still points at it: reusing it would re-point
    # those events at a different rule. An orphaned reference is honest; a
    # re-bound one reads correct forever and is not.
    cited = _citing_events(conn, slug)
    if cited:
        sys.exit(f"error: slug {slug!r} is still cited by {cited} event(s) "
                 f"whose rule is gone. Reusing it would silently re-point "
                 f"those decisions at a different rule. Retry with a "
                 f"different id.")


def cmd_add_rule(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """Rules are history turned into behavior. Since 5.5 the action
    vocabulary is wider than the event enum: alongside plain actions
    ('trash') an action may name an INTERACTION PATTERN — how to handle the
    matching mail, e.g. 'summarize-then-trash', 'scan-then-trash',
    'surface-immediately'. Free text by design; the enum lives on events,
    not rules.

    `--id` is required and never derived from the predicate: predicates here are
    long prose ("filing a kept transactional or home-related message: receipt,
    statement, ..."), so derivation would mint `filing-a-kept-transactional` and
    that would stick forever."""
    slug = _valid_slug_or_exit(args.id)
    _refuse_if_slug_taken(conn, slug, args.predicate, args.action)
    conn.execute(
        "INSERT INTO rules (id, created_at, active, predicate, action, origin) "
        "VALUES (?, ?, 1, ?, ?, ?)",
        (slug, utc_now(), args.predicate, args.action, args.origin),
    )
    conn.commit()
    print(f"rule {slug} added")


def cmd_set_rule(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """Kept as its own verb: `set-rule <id> --active 0` is the retire idiom the
    SKILL and reference name, and `update-rule --active 0` now does the same."""
    cur = conn.execute(
        "UPDATE rules SET active = ? WHERE id = ?",
        (int(args.active), args.rule_id),
    )
    if cur.rowcount == 0:
        sys.exit(f"error: no rule with id {args.rule_id}")
    conn.commit()
    print(f"rule {args.rule_id} active={int(args.active)}")


RULE_COLUMNS = ("id", "created_at", "updated_at", "active", "predicate",
                "action", "origin")


def cmd_list_rules(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """Every rule, not just the active ones -- `context` deliberately shows only
    active rules because a session should not be steered by retired ones, which
    left no way to SEE a retired rule at all. `cited_by` counts events naming
    each rule, so 0 across the board is visible rather than assumed."""
    where = {"active": " WHERE active = 1", "inactive": " WHERE active = 0",
             "all": ""}[args.filter]
    rows = [dict(r) for r in conn.execute(
        f"SELECT {', '.join(RULE_COLUMNS)} FROM rules{where} ORDER BY id")]
    cited = dict(conn.execute(
        "SELECT rule_id, COUNT(*) FROM events WHERE rule_id IS NOT NULL "
        "GROUP BY rule_id").fetchall())
    for row in rows:
        row["cited_by"] = cited.get(row["id"], 0)
    trusted, handles = column_trust({"rules": "rules"})
    json.dump(untrusted.apply_trust(
        {"rules": rows, "count": len(rows), "filter": args.filter},
        trusted | {"count", "filter", "rules.cited_by"},
        handle_fields=handles), sys.stdout, indent=2)
    print()


def _citing_events(conn: sqlite3.Connection, rule_id: str) -> int:
    (n,) = conn.execute("SELECT COUNT(*) FROM events WHERE rule_id = ?",
                        (rule_id,)).fetchone()
    return n


def _rule_or_exit(conn: sqlite3.Connection, rule_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        sys.exit(f"error: no rule with id {rule_id}")
    return row


# Both mutating verbs stop when events cite the rule, and say the count. An
# event's `rule_id` is a claim about the basis a decision was made on, so
# rewriting or deleting that basis silently rewrites history -- `--force` exists
# because it is the user's log and their call, not because it is free.
CITED_HINT = ("{n} event(s) cite rule {id}: {what} would change the recorded "
              "basis for decisions already made. Re-run with --force if that "
              "is what you want, or add a new rule and retire this one "
              "(`set-rule {id} --active 0`), which preserves both.")


def cmd_update_rule(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    _rule_or_exit(conn, args.rule_id)
    sets, params, changed = [], [], []
    if args.new_id is not None:
        # Renaming is a rewrite of identity: events citing the old slug become
        # orphans, so it is gated exactly like editing the text.
        new_slug = _valid_slug_or_exit(args.new_id)
        _refuse_if_slug_taken(conn, new_slug)
        sets.append("id = ?")
        params.append(new_slug)
        changed.append("id")
    for col, val in (("predicate", args.predicate), ("action", args.action),
                     ("origin", args.origin)):
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val)
            changed.append(col)
    if args.active is not None:
        sets.append("active = ?")
        params.append(int(args.active))
        changed.append("active")
    if not sets:
        sys.exit("error: nothing to update — give at least one of "
                 "--predicate / --action / --origin / --active")

    # Toggling `active` alone is retirement, not rewriting: it changes what is
    # in force from now on and leaves the recorded basis intact, so it is never
    # gated and does not stamp updated_at.
    rewriting = [c for c in changed if c != "active"]
    if rewriting:
        n = _citing_events(conn, args.rule_id)
        if n and not args.force:
            sys.exit("error: " + CITED_HINT.format(
                n=n, id=args.rule_id, what="editing " + "/".join(rewriting)))
        sets.append("updated_at = ?")
        params.append(utc_now())

    conn.execute(f"UPDATE rules SET {', '.join(sets)} WHERE id = ?",
                 (*params, args.rule_id))
    conn.commit()
    now = args.new_id or args.rule_id
    renamed = f" (now {now})" if args.new_id else ""
    print(f"rule {args.rule_id} updated: {', '.join(changed)}{renamed}")


def cmd_delete_rule(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """Deletion is for mistakes -- a rule written wrong, or one that never
    should have existed. To end a rule that DID govern real decisions, retire it
    instead: `set-rule <id> --active 0` keeps the row those events point at."""
    _rule_or_exit(conn, args.rule_id)
    n = _citing_events(conn, args.rule_id)
    if n and not args.force:
        sys.exit("error: " + CITED_HINT.format(n=n, id=args.rule_id,
                                               what="deleting it"))
    conn.execute("DELETE FROM rules WHERE id = ?", (args.rule_id,))
    conn.commit()
    print(f"rule {args.rule_id} deleted"
          + (f" (was cited by {n} event(s), forced)" if n else ""))


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
    # No sender data here at all -- paths and filenames we generated -- but it
    # still carries a manifest, so "every model-bound emitter declares its
    # trust" holds without exception for the reader to check.
    print(json.dumps(untrusted.apply_trust(
        {"backed_up": str(target), "copies": len(copies) - len(pruned),
         "pruned": pruned},
        {"backed_up", "pruned"})))


# --------------------------------------------------------------------------
# Trust as a column property (Phase 6.5; untrusted-content-skill-design.md §6)
# --------------------------------------------------------------------------
# Keyed PER TABLE, which is not decoration: `notes` is untrusted in `events`
# and `contacts`, while `account` is trusted in `events`. One flat set of column
# names would have to pick a single answer for each name and would be wrong
# somewhere. `cmd_context` uses `SELECT *` -- a feature here, since a new column
# flows out and default-deny classifies it untrusted automatically, but only
# because matching is per (table, column) rather than by bare name.
#
# Trust is decided at WRITE time (which column is it?) and applied on the
# model-bound READ path. At-rest text stays clean: nothing in the DB carries
# tags, so the read path can wrap freshly every time and a value is always
# re-readable from source. That is what makes the confinement rule affordable.
TRUSTED_COLUMNS = {
    "events": {
        "id", "session_started_at", "occurred_at", "account", "thread_state",
        "inactive_days", "from_folder", "to_folder", "rule_id", "unsubscribed",
        # Enum-constrained on every write path, so structurally safe:
        # proposed/final_action and category via event_row_from_dict's
        # check_choice, decided_by by both that and a schema CHECK.
        "proposed_action", "final_action", "decided_by", "category",
    },
    "session_notes": {"id", "session_started_at", "written_at"},
    "contacts": {"is_vip", "updated_at"},
    "rules": {"id", "created_at", "updated_at", "active", "origin"},
    # Enumerated rather than described as "all counts, rates" (UC §6 was loose
    # here). The view drifts: it gained `holds` and lost `skips` on 2026-08-06,
    # and a description cannot fail a test.
    "sender_stats": {
        "events_total", "decisions", "holds", "reply_rate", "trash_rate",
        "override_rate", "declined_unsubs", "last_seen_at",
    },
}

# Untrusted AND unescaped: values the model reads and then passes back as a
# lookup key. `$HIST context --senders a@b,c@d`, `$HIST add-rule --predicate
# "sender = a@b"`, `$THR show --message-id ...` all match the raw value, so an
# entity-escaped address silently matches nothing. Note these addresses are
# sender-valued even when the USER asked to store them: the string still came
# out of a From header, which is the `root_message_id` trap again.
HANDLE_COLUMNS = {
    "events": {"message_id", "thread_root_id", "sender_addr"},
    "contacts": {"addr"},
    "sender_stats": {"sender_addr"},
}

# Deliberately untrusted. Runtime does not consult this set -- default-deny
# already covers anything unlisted. It exists so the test can tell "we looked
# at this column and it holds sender-derived prose" apart from "nobody has
# looked at this column yet", and so a NEW column fails the suite instead of
# drifting in silently as merely-wrapped.
UNTRUSTED_COLUMNS = {
    # subject is a header; notes is ours or the model's, and invariant 7 makes
    # model-written derivatives of email content untrusted too -- a summary of
    # an injection still carries its words. session_notes.note is the
    # cross-session laundering vector the confinement rule exists for.
    "events": {"subject", "notes"},
    "session_notes": {"note"},
    # display_name is untrusted by invariant 10: identity matches on the real
    # address, never the name a sender chose for itself. relation/notes are free
    # text about a correspondent.
    "contacts": {"display_name", "relation", "notes"},
    # UC §3's shared door: analytics.py `rules` reads these same two columns.
    "rules": {"predicate", "action"},
    "sender_stats": set(),
}


def column_trust(paths_to_tables: dict):
    """(trusted_paths, handle_paths) for an output nesting rows under keys.

    The per-table sets above are authored once and qualified onto whatever path
    each table's rows land at, so a second emitter over the same table cannot
    disagree with this one."""
    trusted, handles = set(), set()
    for path, table in paths_to_tables.items():
        trusted |= untrusted.under(path, TRUSTED_COLUMNS[table])
        handles |= untrusted.under(path, HANDLE_COLUMNS.get(table, ()))
    return trusted, handles


# Trusted scalars cmd_context adds alongside the table rows: our own count and
# our own prose. Shared with the completeness test so neither can drift.
CONTEXT_EXTRA_TRUSTED = {"sender_stats_available", "sender_stats_hint"}

CONTEXT_TABLES = {
    "session_notes": "session_notes",
    "active_rules": "rules",
    "sender_stats": "sender_stats",
    "contacts": "contacts",
}


# Read-only query surface. The five hardening layers, the row cap and the
# no-SQL-parsing decision all live in roquery.py -- extracted 2026-08-07 when
# 11.1 needed the same surface over index.db, because a second copy of a
# security boundary is a second thing to get wrong.
QUERY_ROW_CAP = roquery.ROW_CAP


def query_column_trust():
    """(trusted, handle) bare column names -- see roquery.bare_name_trust."""
    return roquery.bare_name_trust(TRUSTED_COLUMNS, HANDLE_COLUMNS,
                                   UNTRUSTED_COLUMNS)


def cmd_query(args: argparse.Namespace) -> None:
    db_path = args.data_dir / "history.db"
    if not db_path.exists():
        sys.exit(f"error: {db_path} not found — run init_history_db.py first")
    try:
        conn = roquery.connect(db_path)
        try:
            rows, truncated = roquery.run(conn, args.sql, args.limit)
        finally:
            conn.close()
    except roquery.QueryError as e:
        sys.exit(f"error: {e}")
    trusted_names, handle_names = query_column_trust()
    json.dump(roquery.payload(rows, truncated, args.limit, trusted_names,
                              handle_names), sys.stdout, indent=2)
    print()


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
    contacts = [dict(r) for r in conn.execute("SELECT * FROM contacts")]

    # sender_stats is NO LONGER prefetched (2026-08-07). Notes, rules and
    # contacts are free-form judgement material: you cannot know which sentence
    # in a note will matter until you meet the thread it is about, so they have
    # to arrive up front. sender_stats is the opposite -- a keyed lookup whose
    # keys come from `$THR list --summary`'s top_senders, i.e. AFTER this call.
    # Dumping all 129 rows was answering questions nobody had asked, and it was
    # ~35KB of the 40.9KB this command used to cost every single session.
    # Ask for what you need: `--senders a,b,c`, or `$HIST query` for anything
    # else. `--all-senders` restores the old dump.
    stats = None
    if args.all_senders:
        stats = [dict(r) for r in conn.execute("SELECT * FROM sender_stats")]
    elif args.senders:
        addrs = [a.strip().lower() for a in args.senders.split(",") if a.strip()]
        stats = [dict(r) for r in conn.execute(
            "SELECT * FROM sender_stats WHERE sender_addr IN (%s)"
            % ",".join("?" * len(addrs)), tuple(addrs))]

    trusted, handles = column_trust(CONTEXT_TABLES)
    # Escape + manifest, NOT a nonce envelope per value: an envelope around each
    # of ~129 sender_addr strings would bury the structure it is meant to guard.
    # Envelopes are for the body (6.3); scalars get the manifest (6.2).
    payload = {"session_notes": notes, "active_rules": rules,
               "contacts": contacts}
    if stats is None:
        (n,) = conn.execute("SELECT COUNT(*) FROM sender_stats").fetchone()
        payload["sender_stats_available"] = n
        payload["sender_stats_hint"] = (
            "Not prefetched. Get the senders you actually need from `$THR list "
            "--summary` (top_senders), then `$HIST context --senders a,b,c` -- "
            "or `$HIST query` for anything a keyed lookup cannot answer.")
    else:
        payload["sender_stats"] = stats
    json.dump(
        untrusted.apply_trust(payload, trusted | CONTEXT_EXTRA_TRUSTED,
                              handle_fields=handles),
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
    ev.add_argument("--rule-id",
                    help="slug of the rule that justified this decision; "
                         "without it the rule cannot be measured")
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
    ar.add_argument("--id", required=True,
                    help="kebab-case slug naming the rule, 3-32 chars, e.g. "
                         "'credit-karma' or 'receipt-filing'. Refused if taken "
                         "(never auto-suffixed) — retry with another")
    ar.add_argument("--predicate", required=True,
                    help="e.g. \"sender = ship-confirm@amazon.com\"")
    ar.add_argument("--action", required=True,
                    help="trash | summarize-then-trash | scan-then-trash | "
                         "surface-immediately | ... (free text)")
    ar.add_argument("--origin", required=True, choices=("user", "claude-proposed"))
    ar.set_defaults(func=cmd_add_rule)

    sr = sub.add_parser("set-rule", help="activate/deactivate a rule")
    sr.add_argument("rule_id")
    sr.add_argument("--active", required=True, type=int, choices=(0, 1))
    sr.set_defaults(func=cmd_set_rule)

    lr = sub.add_parser("list-rules",
                        help="list rules (all by default, incl. retired) with "
                             "how many events cite each")
    lr.add_argument("--filter", choices=("all", "active", "inactive"),
                    default="all")
    lr.set_defaults(func=cmd_list_rules)

    ur = sub.add_parser("update-rule",
                        help="edit a rule in place; refuses to rewrite one that "
                             "events cite unless --force")
    ur.add_argument("rule_id")
    ur.add_argument("--new-id", dest="new_id",
                    help="rename the rule; gated like a text edit, since events "
                         "citing the old slug become orphans")
    ur.add_argument("--predicate")
    ur.add_argument("--action")
    ur.add_argument("--origin", choices=("user", "claude-proposed"))
    ur.add_argument("--active", type=int, choices=(0, 1), default=None)
    ur.add_argument("--force", action="store_true",
                    help="rewrite even though events cite this rule")
    ur.set_defaults(func=cmd_update_rule)

    dr = sub.add_parser("delete-rule",
                        help="delete a rule written by mistake; to end one that "
                             "governed real decisions, retire it instead")
    dr.add_argument("rule_id")
    dr.add_argument("--force", action="store_true",
                    help="delete even though events cite this rule")
    dr.set_defaults(func=cmd_delete_rule)

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
    cx.add_argument("--senders", help="comma-separated addresses whose sender_stats "
                                      "to include (not prefetched by default)")
    cx.add_argument("--all-senders", action="store_true", dest="all_senders",
                    help="restore the old behaviour: dump every sender_stats row. "
                         "~35KB on a real history; prefer --senders or `query`.")

    su = sub.add_parser(
        "set-unsubscribed",
        help="mark already-logged events as unsubscribed, for a MANUAL link the "
             "user confirmed clicking after the fact")
    su.add_argument("--session", required=True,
                    help="session_started_at the events were logged under")
    su.add_argument("--senders", required=True,
                    help="comma-separated sender addresses to amend")
    su.set_defaults(func=cmd_set_unsubscribed)

    q = sub.add_parser(
        "query",
        help="read-only SQL over history.db -- for decisions a keyed lookup "
             "cannot answer (sample queries: reference.md §History queries)")
    q.add_argument("--sql", required=True, help="a single SELECT/WITH statement")
    q.add_argument("--limit", type=int, default=QUERY_ROW_CAP,
                   help=f"max rows returned (default {QUERY_ROW_CAP}); "
                        "truncation is reported, never silent")
    q.set_defaults(func=cmd_query, needs_conn=False)
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
    # `query` opens its own hardened read-only connection (see cmd_query) rather
    # than borrowing the read-write one every other subcommand uses.
    if getattr(args, "needs_conn", True) is False:
        args.func(args)
        return
    conn = connect(args.data_dir)
    try:
        args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
