#!/usr/bin/env python3
"""Maildir -> SQLite indexer for the email-triage skill (index.db).

Design refs:
  - email-skill-design.md §7.4 (three-pass reconciliation: snapshot / upsert /
    orphan reconciliation; change-report output contract; synthetic
    Message-IDs; UTC date normalization; maildir-key identity; Trash indexed).
  - email-skill-design.md §8 (the `envelopes` schema; row identity is
    (folder, maildir_key), not message_id -- Message-ID is sender-controlled
    and only an indexed join key).
  - email-folder-taxonomy.md (Canonical locations, Sync coverage) and
    implementation-plan.md item 2.4's "Added scope (2026-07-09, taxonomy)"
    for the folder->tier derivation, All-Mail dedup, and unrecognized-folder
    surfacing.

Subcommands:
  init    Create index.db schema only (idempotent).
  index   Walk a Maildir root (one subdirectory per account), upsert +
          reconcile into index.db (auto-inits schema), print a JSON change
          report to stdout.

Three passes, in this exact order (§7.4):
  1. Snapshot -- walk every folder's cur/ + new/, recording
     (account, folder, maildir_key, current_filename, mtime) for every
     message file on disk.
  2. Upsert -- for every snapshot entry: unknown (account, folder,
     maildir_key) -> parse full headers, insert; known key whose filename
     changed -> flags changed, re-derive flags from the filename suffix only
     (no re-parse).
  3. Reconcile orphans -- only after all folders are upserted: a DB row whose
     (account, folder, maildir_key) is no longer on disk is either a move
     (same message_id now lives in a different folder for the same account)
     or a genuine deletion.

Nothing here is run against real mail by this script itself -- it only acts
on whatever --maildir root it is pointed at.
"""

import argparse
import datetime
import email.policy
import hashlib
import json
import os
import sqlite3
import sys
from collections import defaultdict
from email.parser import BytesHeaderParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

# --------------------------------------------------------------------------
# Schema (§8, verbatim envelopes DDL + indexes; folder_tier is an additive
# column for the 2026-07-09 taxonomy scope, and message_tiers is a new view
# -- neither modifies the original columns/indexes).
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS envelopes (
    id               INTEGER PRIMARY KEY,       -- surrogate key
    message_id       TEXT NOT NULL,             -- Message-ID header (or synthetic-*); join key, NOT identity
    folder           TEXT NOT NULL,
    maildir_key      TEXT NOT NULL,             -- filename stem before ':2,'; stable across flag renames
    maildir_file     TEXT NOT NULL,             -- current full filename; updated on flag change
    in_reply_to      TEXT,
    references_chain TEXT,                      -- full ancestry (JSON array); robust threading
    subject          TEXT,
    from_addr        TEXT,
    to_addr          TEXT,
    date_utc         TEXT,                      -- Date header normalized to UTC ISO 8601
    flags            TEXT,                      -- raw flag set (JSON array)
    is_flagged       INTEGER NOT NULL DEFAULT 0,-- denormalized: '\\Flagged' present
    is_answered      INTEGER NOT NULL DEFAULT 0, -- denormalized: '\\Answered' present
    account          TEXT,
    indexed_at       TEXT,                      -- for incremental re-index (compare to file mtime)
    folder_tier      TEXT,                      -- added scope (2026-07-09): folder->tier classification,
                                                  -- written at index time so message_tiers stays plain SQL
    UNIQUE (folder, maildir_key)
);

CREATE INDEX IF NOT EXISTS idx_message_id  ON envelopes(message_id);
CREATE INDEX IF NOT EXISTS idx_in_reply_to ON envelopes(in_reply_to);
CREATE INDEX IF NOT EXISTS idx_folder_date ON envelopes(folder, date_utc);

-- Per-message tier rollup (added scope, 2026-07-09 taxonomy). One row per
-- (account, message_id): the highest-precedence non-allmail folder_tier
-- among its rows, or 'archive' when the message's only rows are allmail
-- (Gmail's virtual Archive -- taxonomy doc, Canonical locations).
CREATE VIEW IF NOT EXISTS message_tiers AS
WITH ranked AS (
    SELECT account, message_id,
        CASE folder_tier
            WHEN 'inbox'        THEN 1
            WHEN 'active'       THEN 2
            WHEN 'areas'        THEN 3
            WHEN 'archive'      THEN 4
            WHEN 'sent'         THEN 5
            WHEN 'drafts'       THEN 6
            WHEN 'trash'        THEN 7
            WHEN 'junk'         THEN 8
            WHEN 'unrecognized' THEN 9
            WHEN 'container'    THEN 10
        END AS tier_rank
    FROM envelopes
    WHERE folder_tier != 'allmail'
),
best AS (
    SELECT account, message_id, MIN(tier_rank) AS best_rank
    FROM ranked
    GROUP BY account, message_id
)
SELECT b.account, b.message_id,
    CASE b.best_rank
        WHEN 1 THEN 'inbox' WHEN 2 THEN 'active' WHEN 3 THEN 'areas'
        WHEN 4 THEN 'archive' WHEN 5 THEN 'sent' WHEN 6 THEN 'drafts'
        WHEN 7 THEN 'trash' WHEN 8 THEN 'junk' WHEN 9 THEN 'unrecognized'
        WHEN 10 THEN 'container'
    END AS tier
FROM best b
UNION
SELECT DISTINCT e.account, e.message_id, 'archive' AS tier
FROM envelopes e
WHERE e.folder_tier = 'allmail'
  AND NOT EXISTS (
      SELECT 1 FROM envelopes e2
      WHERE e2.account = e.account AND e2.message_id = e.message_id AND e2.folder_tier != 'allmail'
  );
"""

INSERT_SQL = """INSERT INTO envelopes
    (message_id, folder, maildir_key, maildir_file, in_reply_to, references_chain,
     subject, from_addr, to_addr, date_utc, flags, is_flagged, is_answered,
     account, indexed_at, folder_tier)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""

UPDATE_FLAGS_SQL = """UPDATE envelopes
    SET maildir_file = ?, flags = ?, is_flagged = ?, is_answered = ?, indexed_at = ?
    WHERE id = ?"""


class FatalError(Exception):
    """A condition that should abort the run with exit code 1 (bad paths, db errors)."""


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Folder -> tier classification (taxonomy doc, "Canonical locations").
# Convention-based, no config file: folder is the slash-joined path relative
# to the account root (e.g. "INBOX", "Archive/Housing/2025-Colorado-Move",
# "[Gmail]/Sent Mail", "Areas/Taxes").
# --------------------------------------------------------------------------

SYSTEM_FOLDER_TIERS = {
    "[Gmail]/Sent Mail": "sent",
    "Sent Messages": "sent",
    "[Gmail]/Trash": "trash",
    "Deleted Messages": "trash",
    "[Gmail]/Spam": "junk",
    "Junk": "junk",
    "[Gmail]/Drafts": "drafts",
    "Drafts": "drafts",
    "[Gmail]/All Mail": "allmail",
}


def classify_folder_tier(folder: str) -> str:
    if folder == "INBOX":
        return "inbox"
    if folder == "Active":
        return "active"
    if folder in SYSTEM_FOLDER_TIERS:
        return SYSTEM_FOLDER_TIERS[folder]
    if folder == "Archive" or folder.startswith("Archive/"):
        return "archive"
    if folder.startswith("Areas/"):
        return "areas"
    if folder in ("[Gmail]", "Areas", "Archive/Career", "Archive/Housing"):
        # Pure container directories (Gmail's bracket namespace, family
        # folders, the Areas parent): they hold no messages by convention
        # and aren't audit debt. Messages found in one anyway still get
        # surfaced, via the container tier's lowest precedence.
        return "container"
    return "unrecognized"


# --------------------------------------------------------------------------
# Maildir walking (§7.4 pass 1: snapshot).
# --------------------------------------------------------------------------

_SKIP_DIRS = {"cur", "new", "tmp"}


def find_folders(account_root: Path):
    """Yield (folder_name, folder_path) for every Maildir folder under
    account_root. A folder is any directory containing both cur/ and new/.
    folder_name is the slash-joined path relative to account_root ("INBOX"
    for account_root itself); cur/new/tmp are never descended into, and
    dotfiles/dotdirs (.mbsyncstate, .uidvalidity, ...) are skipped."""
    for dirpath, dirnames, _filenames in os.walk(account_root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        p = Path(dirpath)
        if (p / "cur").is_dir() and (p / "new").is_dir():
            rel = p.relative_to(account_root)
            folder = "INBOX" if str(rel) == "." else str(rel).replace(os.sep, "/")
            yield folder, p


def scan_folder_messages(folder_path: Path):
    """Yield (filename, path, mtime) for every message file in a folder's
    cur/ and new/ subdirectories. Dotfiles are skipped."""
    for sub in ("cur", "new"):
        d = folder_path / sub
        if not d.is_dir():
            continue
        for entry in sorted(os.scandir(d), key=lambda e: e.name):
            if entry.name.startswith(".") or not entry.is_file():
                continue
            yield entry.name, Path(entry.path), entry.stat().st_mtime


def split_maildir_filename(filename: str):
    """Return (maildir_key, flags_suffix). The key is the stem before ':2,'
    -- stable across flag renames (§7.4/§8)."""
    if ":2," in filename:
        stem, _, suffix = filename.partition(":2,")
        return stem, suffix
    return filename, ""


FLAG_CHARS = {"D": "Draft", "F": "Flagged", "P": "Passed", "R": "Answered", "S": "Seen", "T": "Trashed"}


def derive_flags(suffix: str) -> list:
    return sorted({FLAG_CHARS[c] for c in suffix if c in FLAG_CHARS})


def build_snapshot(maildir_root: Path, accounts: list):
    """Pass 1. Returns (snapshot, folders_seen):
      snapshot: {(account, folder, maildir_key): (filename, path, mtime)}
      folders_seen: {(account, folder): tier} for every folder walked, even
        empty ones -- so unrecognized folders are surfaced regardless of
        whether they currently hold any messages."""
    snapshot = {}
    folders_seen = {}
    for account in accounts:
        account_root = maildir_root / account
        for folder, folder_path in find_folders(account_root):
            folders_seen[(account, folder)] = classify_folder_tier(folder)
            for filename, path, mtime in scan_folder_messages(folder_path):
                key, _suffix = split_maildir_filename(filename)
                snapshot[(account, folder, key)] = (filename, path, mtime)
    return snapshot, folders_seen


# --------------------------------------------------------------------------
# Header parsing (§7.4 pass 2, new keys only).
# --------------------------------------------------------------------------


def normalize_msgid(raw: str):
    if raw is None:
        return None
    val = raw.strip().strip("<>").strip()
    return val or None


def synthetic_message_id(date_str, from_str, subject_str) -> str:
    """Deterministic synthetic Message-ID for messages missing the header
    (§7.4): synthetic-<sha256 hex of Date+From+Subject>, first 16 hex chars."""
    basis = "\x00".join([date_str or "", from_str or "", subject_str or ""])
    digest = hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()
    return f"synthetic-{digest[:16]}"


def parse_date_utc(date_str):
    """Date header -> UTC ISO 8601 ('YYYY-MM-DDTHH:MM:SSZ'); unparseable/missing -> None."""
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    else:
        dt = dt.astimezone(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_message_file(path: Path, account: str, folder: str, key: str, filename: str) -> dict:
    """Parse headers only (BytesHeaderParser -- never the body) and return
    the field dict for INSERT_SQL. Raises on any malformed input; the caller
    counts these as parse_errors and skips the file."""
    with open(path, "rb") as f:
        msg = BytesHeaderParser(policy=email.policy.default).parse(f)

    raw_date = msg.get("Date")
    raw_from = msg.get("From")
    raw_subject = msg.get("Subject")
    raw_to = msg.get("To")
    raw_in_reply_to = msg.get("In-Reply-To")
    raw_message_id = msg.get("Message-ID")

    date_str = str(raw_date) if raw_date is not None else None
    from_str = str(raw_from) if raw_from is not None else None
    subject_str = str(raw_subject) if raw_subject is not None else None
    to_str = str(raw_to) if raw_to is not None else None
    in_reply_to = normalize_msgid(str(raw_in_reply_to)) if raw_in_reply_to is not None else None

    message_id = normalize_msgid(str(raw_message_id)) if raw_message_id is not None else None
    if not message_id:
        message_id = synthetic_message_id(date_str, from_str, subject_str)

    references_chain = []
    for value in msg.get_all("References") or []:
        for token in str(value).split():
            mid = normalize_msgid(token)
            if mid:
                references_chain.append(mid)

    from_addr = parseaddr(from_str or "")[1] or from_str
    date_utc = parse_date_utc(date_str)

    _, suffix = split_maildir_filename(filename)
    flags = derive_flags(suffix)

    return {
        "message_id": message_id,
        "folder": folder,
        "maildir_key": key,
        "maildir_file": filename,
        "in_reply_to": in_reply_to,
        "references_chain": references_chain,
        "subject": subject_str,
        "from_addr": from_addr,
        "to_addr": to_str,
        "date_utc": date_utc,
        "flags": flags,
        "is_flagged": int("Flagged" in flags),
        "is_answered": int("Answered" in flags),
        "account": account,
        "indexed_at": utc_now(),
        "folder_tier": classify_folder_tier(folder),
    }


def insert_row(cur: sqlite3.Cursor, fields: dict) -> int:
    cur.execute(
        INSERT_SQL,
        (
            fields["message_id"],
            fields["folder"],
            fields["maildir_key"],
            fields["maildir_file"],
            fields["in_reply_to"],
            json.dumps(fields["references_chain"]),
            fields["subject"],
            fields["from_addr"],
            fields["to_addr"],
            fields["date_utc"],
            json.dumps(fields["flags"]),
            fields["is_flagged"],
            fields["is_answered"],
            fields["account"],
            fields["indexed_at"],
            fields["folder_tier"],
        ),
    )
    return cur.lastrowid


# --------------------------------------------------------------------------
# Change report entries
# --------------------------------------------------------------------------


def envelope_report_entry(account, folder, fields: dict) -> dict:
    return {
        "account": account,
        "folder": folder,
        "message_id": fields.get("message_id"),
        "subject": fields.get("subject"),
        "from_addr": fields.get("from_addr"),
        "date_utc": fields.get("date_utc"),
    }


def _sort_key_simple(e: dict):
    return (e.get("account") or "", e.get("folder") or "", e.get("message_id") or "")


def _sort_key_moved(e: dict):
    return (e.get("account") or "", e.get("from_folder") or "", e.get("to_folder") or "", e.get("message_id") or "")


# --------------------------------------------------------------------------
# Core three-pass run
# --------------------------------------------------------------------------


def run_index(conn: sqlite3.Connection, maildir_root: Path, accounts: list) -> dict:
    placeholders = ",".join("?" * len(accounts))
    existing_rows = conn.execute(
        f"SELECT id, account, folder, maildir_key, maildir_file, message_id, subject, from_addr, date_utc "
        f"FROM envelopes WHERE account IN ({placeholders})",
        accounts,
    ).fetchall()

    existing_map = {}  # (account, folder, maildir_key) -> row dict
    current_by_msgid = defaultdict(list)  # (account, message_id) -> [row dict, ...]
    for row in existing_rows:
        id_, account, folder, key, maildir_file, message_id, subject, from_addr, date_utc = row
        rec = {
            "id": id_,
            "account": account,
            "folder": folder,
            "maildir_key": key,
            "maildir_file": maildir_file,
            "message_id": message_id,
            "subject": subject,
            "from_addr": from_addr,
            "date_utc": date_utc,
        }
        existing_map[(account, folder, key)] = rec
        current_by_msgid[(account, message_id)].append(rec)

    snapshot, folders_seen = build_snapshot(maildir_root, accounts)

    cur = conn.cursor()
    new_entries = {}  # (account, folder, message_id) -> report entry
    flag_changed_entries = []
    parse_errors = 0

    # Pass 2: upsert (all folders, before any deletions are processed).
    for (account, folder, key), (filename, path, _mtime) in snapshot.items():
        existing = existing_map.get((account, folder, key))
        if existing is None:
            try:
                fields = parse_message_file(path, account, folder, key, filename)
            except Exception:
                parse_errors += 1
                continue
            new_id = insert_row(cur, fields)
            rec = {
                "id": new_id,
                "account": account,
                "folder": folder,
                "maildir_key": key,
                "maildir_file": filename,
                "message_id": fields["message_id"],
                "subject": fields["subject"],
                "from_addr": fields["from_addr"],
                "date_utc": fields["date_utc"],
            }
            current_by_msgid[(account, fields["message_id"])].append(rec)
            new_entries[(account, folder, fields["message_id"])] = envelope_report_entry(account, folder, fields)
        elif existing["maildir_file"] != filename:
            # Known key, changed filename: flags changed. Re-derive from the
            # filename suffix only -- no full re-parse.
            _, suffix = split_maildir_filename(filename)
            flags = derive_flags(suffix)
            is_flagged = int("Flagged" in flags)
            is_answered = int("Answered" in flags)
            cur.execute(UPDATE_FLAGS_SQL, (filename, json.dumps(flags), is_flagged, is_answered, utc_now(), existing["id"]))
            existing["maildir_file"] = filename
            flag_changed_entries.append(envelope_report_entry(account, folder, existing))
        # else: filename unchanged -> nothing to do (this is what makes
        # unchanged runs idempotent and report-silent).

    # Pass 3: reconcile orphans (only after all folders are upserted).
    moved_entries = []
    deleted_entries = []
    orphan_keys = existing_map.keys() - snapshot.keys()
    for k in sorted(orphan_keys):
        row = existing_map[k]
        account, folder = row["account"], row["folder"]
        candidates = [r for r in current_by_msgid.get((account, row["message_id"]), []) if r["folder"] != folder]
        if candidates:
            target = min(candidates, key=lambda r: r["folder"])
            new_entries.pop((account, target["folder"], row["message_id"]), None)
            moved_entries.append(
                {
                    "account": account,
                    "from_folder": folder,
                    "to_folder": target["folder"],
                    "message_id": row["message_id"],
                    "subject": row["subject"],
                    "from_addr": row["from_addr"],
                    "date_utc": row["date_utc"],
                }
            )
        else:
            deleted_entries.append(envelope_report_entry(account, folder, row))
        cur.execute("DELETE FROM envelopes WHERE id = ?", (row["id"],))

    unrecognized_folders = sorted({folder for (_account, folder), tier in folders_seen.items() if tier == "unrecognized"})

    return {
        "new": sorted(new_entries.values(), key=_sort_key_simple),
        "moved": sorted(moved_entries, key=_sort_key_moved),
        "deleted": sorted(deleted_entries, key=_sort_key_simple),
        "flag_changed": sorted(flag_changed_entries, key=_sort_key_simple),
        "parse_errors": parse_errors,
        "unrecognized_folders": unrecognized_folders,
    }


# --------------------------------------------------------------------------
# DB setup / CLI
# --------------------------------------------------------------------------


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def resolve_accounts(maildir_root: Path, requested) -> list:
    if not maildir_root.is_dir():
        raise FatalError(f"maildir root not found or not a directory: {maildir_root}")
    all_accounts = sorted(d.name for d in maildir_root.iterdir() if d.is_dir() and not d.name.startswith("."))
    if requested:
        missing = [a for a in requested if a not in all_accounts]
        if missing:
            raise FatalError(f"account(s) not found under {maildir_root}: {', '.join(missing)}")
        accounts = sorted(set(requested))
    else:
        accounts = all_accounts
    if not accounts:
        raise FatalError(f"no account subdirectories found under {maildir_root}")
    return accounts


def cmd_init(args: argparse.Namespace) -> None:
    conn = init_db(args.db)
    conn.close()
    print(f"index.db schema ready at {args.db}")


def cmd_index(args: argparse.Namespace) -> None:
    accounts = resolve_accounts(args.maildir, args.account)
    conn = init_db(args.db)
    try:
        try:
            report = run_index(conn, args.maildir, accounts)
        except sqlite3.Error as e:
            conn.rollback()
            raise FatalError(f"database error: {e}") from e
        conn.commit()
    finally:
        conn.close()
    print(json.dumps(report, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="create index.db schema only")
    init_p.add_argument("--db", required=True, type=Path)
    init_p.set_defaults(func=cmd_init)

    idx_p = sub.add_parser("index", help="index a Maildir root into index.db (auto-inits schema)")
    idx_p.add_argument("--maildir", required=True, type=Path, help="root containing one subdirectory per account")
    idx_p.add_argument("--db", required=True, type=Path)
    idx_p.add_argument(
        "--account",
        action="append",
        help="limit indexing to this account (repeatable); default: every subdirectory of --maildir",
    )
    idx_p.set_defaults(func=cmd_index)

    args = parser.parse_args()
    try:
        args.func(args)
    except FatalError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except (OSError, sqlite3.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
