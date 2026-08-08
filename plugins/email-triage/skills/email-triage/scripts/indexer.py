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
  - implementation-plan.md Phase 4 ("concepts in code, names in config"):
    tier IDs (untriaged/active/ongoing/cold + system roles) are fixed
    vocabulary; every folder-name realization is per-account config
    (config.yaml's accounts.<name>.tiers map). Tier ID renames (4.2):
    inbox->untriaged, areas->ongoing, archive->cold.

Subcommands:
  init             Create index.db schema only (idempotent).
  index            Walk a Maildir root (one subdirectory per account), upsert
                    + reconcile into index.db (auto-inits schema), print a
                    JSON change report to stdout.
  priority-folders  Config-driven mbsync Stage-1 box lists per account (4.3):
                    {"<account>": {"fast": [...], "slow_hint": "full channel"}}.
                    Database-free -- reads config.yaml (or built-in defaults)
                    only.

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
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path

import roquery
import untrusted

# --------------------------------------------------------------------------
# Schema (§8, verbatim envelopes DDL + indexes; folder_tier is an additive
# column for the 2026-07-09 taxonomy scope, and message_tiers is a new view
# -- neither modifies the original columns/indexes).
# --------------------------------------------------------------------------

SCHEMA = """
-- Adding a column here? It reaches a model context through this module's own
-- change report and through threads.py's list/show/read, so classify it in
-- indexer.CHANGE_ENTRY_TRUSTED / _HANDLES and in threads.py's per-shape
-- allowlists (Phase 6.5's re-review trigger, placed where the schema actually
-- changes). Default-deny makes a forgotten column fail safe -- escaped and
-- declared untrusted -- but possibly over-wrapped: `date_utc` looks like a
-- header value and is in fact our own strftime output.
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

-- Per-message tier rollup (added scope, 2026-07-09 taxonomy; tier IDs
-- renamed per Phase 4 4.2: inbox->untriaged, areas->ongoing, archive->cold).
-- One row per (account, message_id): the highest-precedence non-allmail
-- folder_tier among its rows, or 'cold' when the message's only rows are
-- allmail (Gmail's virtual Cold tier -- taxonomy doc, Canonical locations).
CREATE VIEW IF NOT EXISTS message_tiers AS
WITH ranked AS (
    SELECT account, message_id,
        CASE folder_tier
            WHEN 'untriaged'    THEN 1
            WHEN 'active'       THEN 2
            WHEN 'ongoing'      THEN 3
            WHEN 'cold'         THEN 4
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
        WHEN 1 THEN 'untriaged' WHEN 2 THEN 'active' WHEN 3 THEN 'ongoing'
        WHEN 4 THEN 'cold' WHEN 5 THEN 'sent' WHEN 6 THEN 'drafts'
        WHEN 7 THEN 'trash' WHEN 8 THEN 'junk' WHEN 9 THEN 'unrecognized'
        WHEN 10 THEN 'container'
    END AS tier
FROM best b
UNION
SELECT DISTINCT e.account, e.message_id, 'cold' AS tier
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
# Folder -> tier classification (taxonomy doc, "Canonical locations";
# implementation-plan.md Phase 4 -- "concepts in code, names in config").
# folder is the slash-joined path relative to the account root (e.g.
# "INBOX", "Archive/Housing/2025-Colorado-Move", "[Gmail]/Sent Mail",
# "Areas/Taxes"). Folder NAMES come from the per-account tier_map (parsed
# from config.yaml's accounts.<name>.tiers block, or DEFAULT_TIER_MAPS when
# no config/account entry exists); tier IDs and the matching/derivation
# logic are fixed system vocabulary, not config.
# --------------------------------------------------------------------------

# config.yaml tier-map key -> tier id. Identical for every key except
# all_mail (config key, schema v2 4.1) -> allmail (the system-role tier id,
# unchanged since MVP) -- config and code deliberately spell it differently.
CONFIG_KEY_TO_TIER = {
    "untriaged": "untriaged",
    "active": "active",
    "ongoing": "ongoing",
    "cold": "cold",
    "sent": "sent",
    "drafts": "drafts",
    "trash": "trash",
    "junk": "junk",
    "all_mail": "allmail",
}

# Fallback per-account tier maps: current hardcoded folder names (pre-Phase-4
# behavior), used when config.yaml is absent or has no tiers map for an
# account -- keeps tests and a bare bootstrap working without config.
DEFAULT_TIER_MAPS = {
    "icloud": {
        "untriaged": "INBOX",
        "active": "Active",
        "ongoing": "Areas",
        "cold": "Archive",
        "sent": "Sent Messages",
        "drafts": "Drafts",
        "trash": "Deleted Messages",
        "junk": "Junk",
    },
    "gmail": {
        "untriaged": "INBOX",
        "active": "Active",
        "ongoing": "Areas",
        "cold": "Archive",
        "all_mail": "[Gmail]/All Mail",
        "sent": "[Gmail]/Sent Mail",
        "drafts": "[Gmail]/Drafts",
        "trash": "[Gmail]/Trash",
        "junk": "[Gmail]/Spam",
    },
}


def _matches(folder: str, name: str) -> bool:
    """4.1's matching rule: a folder belongs to a tier if it equals the
    configured name or is a descendant (name + "/...")."""
    return folder == name or folder.startswith(name + "/")


def classify_folder_tier(folder: str, tier_map: dict) -> str:
    """Folder -> tier, driven entirely by one account's tier_map (config.yaml
    accounts.<name>.tiers, or a DEFAULT_TIER_MAPS fallback -- 4.2).

    1. Direct match against every configured tier folder (untriaged, active,
       ongoing, cold, sent, drafts, trash, junk, all_mail); a null/missing
       tier never matches anything.
    2. `cold_root` (gmail-only second cold-tier root): meaningful only when
       `cold` itself is null (a virtual-only account) -- named Archive/<
       chapter> labels still exist on disk and must classify `cold`.
    3. `container`: a folder that matched no tier above but is a PROPER
       ancestor of some configured tier folder (e.g. "[Gmail]" is an
       ancestor of "[Gmail]/Sent Mail") holds no messages by convention and
       isn't audit debt.
    4. Anything else: `unrecognized`.
    """
    for config_key, tier_id in CONFIG_KEY_TO_TIER.items():
        name = tier_map.get(config_key)
        if name and _matches(folder, name):
            return tier_id

    cold_root = tier_map.get("cold_root")
    if cold_root and _matches(folder, cold_root):
        return "cold"

    configured_names = [tier_map.get(k) for k in CONFIG_KEY_TO_TIER]
    configured_names.append(cold_root)
    for name in configured_names:
        if name and name.startswith(folder + "/"):
            return "container"

    return "unrecognized"


# --------------------------------------------------------------------------
# config.yaml parsing (4.2): a restricted, stdlib-only, line-based parser for
# exactly one shape -- accounts.<name>.tiers.<key>: <value> -- tracking
# indentation to know when it has entered/left that block. Everything else
# in the file (server_rules' multiline `>-` folded block, excluded_from_sync,
# top-level maildir_path, comments anywhere) is walked over via indentation
# alone and never interpreted -- this is deliberately not a YAML parser.
# --------------------------------------------------------------------------


def _strip_comment(line: str) -> str:
    """Strip a trailing '# comment', but not a '#' that appears inside a
    double-quoted value (e.g. tier names like "[Gmail]/Trash" never contain
    one, but this stays robust regardless)."""
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "#" and not in_quotes:
            return line[:i]
    return line


def _parse_scalar(raw: str):
    """A YAML scalar restricted to what config.yaml's tiers block actually
    uses: null/~ -> None, a double- or single-quoted string -> unquoted, or
    a bare value (which may contain spaces, e.g. "Sent Messages")."""
    raw = raw.strip()
    if raw in ("", "null", "~"):
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def parse_config_tiers_text(text: str) -> dict:
    """Parse config.yaml's accounts.<name>.tiers maps out of raw text.
    Returns {account: {tier_key: value_or_None}} for every account that has
    a tiers: block; accounts without one are simply absent (caller falls
    back to DEFAULT_TIER_MAPS)."""
    accounts: dict = {}
    in_accounts = False
    accounts_indent = None
    account_indent = None
    current_account = None
    in_tiers = False
    tiers_indent = None

    for raw_line in text.splitlines():
        line = _strip_comment(raw_line)
        if not line.strip():
            continue  # blank or comment-only: structurally insignificant
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()

        if indent == 0 and content == "accounts:":
            in_accounts = True
            accounts_indent = indent
            account_indent = None
            current_account = None
            in_tiers = False
            continue

        if not in_accounts:
            continue

        if indent <= accounts_indent:
            # Back to top level (e.g. "maildir_path:") -- accounts: is done.
            in_accounts = False
            current_account = None
            in_tiers = False
            continue

        if account_indent is None:
            account_indent = indent  # first line inside accounts: sets the unit

        if indent == account_indent:
            # A new account name (bare "key:" at the account level).
            if content.endswith(":") and ":" not in content[:-1]:
                current_account = content[:-1].strip()
                accounts.setdefault(current_account, {})
                in_tiers = False
            continue

        if current_account is None:
            continue

        if in_tiers and indent <= tiers_indent:
            in_tiers = False  # left the tiers: block (sibling key or dedent)

        if not in_tiers:
            if content == "tiers:":
                in_tiers = True
                tiers_indent = indent
            continue  # ignore every other key (server_rules, email, ...)

        if ":" not in content:
            continue
        key, _, raw_value = content.partition(":")
        accounts[current_account][key.strip()] = _parse_scalar(raw_value)

    return accounts


def parse_config_tiers(config_path: Path) -> dict:
    """parse_config_tiers_text over an on-disk file; {} if it doesn't exist."""
    if not config_path.is_file():
        return {}
    return parse_config_tiers_text(config_path.read_text())


def load_tier_maps(config_path: Path, accounts: list) -> dict:
    """{account: tier_map} for every requested account: the config-parsed
    map when present and non-empty, else DEFAULT_TIER_MAPS, else {} for a
    wholly unknown account (everything classifies unrecognized)."""
    parsed = parse_config_tiers(config_path) if config_path else {}
    return {
        account: parsed.get(account) or DEFAULT_TIER_MAPS.get(account, {})
        for account in accounts
    }


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


def build_snapshot(maildir_root: Path, accounts: list, tier_maps: dict):
    """Pass 1. Returns (snapshot, folders_seen):
      snapshot: {(account, folder, maildir_key): (filename, path, mtime)}
      folders_seen: {(account, folder): tier} for every folder walked, even
        empty ones -- so unrecognized folders are surfaced regardless of
        whether they currently hold any messages."""
    snapshot = {}
    folders_seen = {}
    for account in accounts:
        tier_map = tier_maps.get(account, {})
        account_root = maildir_root / account
        for folder, folder_path in find_folders(account_root):
            folders_seen[(account, folder)] = classify_folder_tier(folder, tier_map)
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


def parse_message_file(path: Path, account: str, folder: str, key: str, filename: str, tier_map: dict) -> dict:
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
        "folder_tier": classify_folder_tier(folder, tier_map),
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


# Trust classification (Phase 6.2; untrusted-content-skill-design.md §5).
# One entry shape appears at four paths in the change report, and batch.py nests
# the whole thing again under change_report.external -- so the shape is authored
# once here and composed with untrusted.under() wherever it lands. Two hand-kept
# copies of a security allowlist is how they drift.
CHANGE_REPORT_KINDS = ("new", "moved", "deleted", "flag_changed")
# Folder names come from the Maildir layout, date_utc from our own strftime, and
# the flag lists from the filename suffix -- none of it sender text.
CHANGE_ENTRY_TRUSTED = {"account", "folder", "from_folder", "to_folder",
                        "date_utc", "old_flags", "new_flags"}
# Handle: the model feeds these straight into $THR show / $ACT / $BATCH.
CHANGE_ENTRY_HANDLES = {"message_id"}
# `subject` and `from_addr` are absent from both on purpose -- sender prose,
# escaped and declared by default-deny.


# --------------------------------------------------------------------------
# Per-column trust for index.db (11.1). Phase 6.5 built this for history.db and
# stopped there; `$IDX query` cannot classify a result set without it. Keyed per
# table like 6.5's, even though `envelopes` is currently the only writable one,
# so a second table does not have to reshape the map.
# --------------------------------------------------------------------------

TRUSTED_COLUMNS = {
    # Ours or the filesystem's, none of it sender text: folder names come from
    # config.yaml, maildir_key/maildir_file are the filenames mbsync generated,
    # date_utc is a normalized header, flags are the letters in the filename
    # suffix, folder_tier is our own vocabulary, indexed_at is our clock.
    "envelopes": {"id", "folder", "maildir_key", "maildir_file", "date_utc",
                  "flags", "is_flagged", "is_answered", "account", "indexed_at",
                  "folder_tier"},
    # The view derives account/message_id from envelopes and tier from our own
    # CASE expression.
    "message_tiers": {"account", "tier"},
}

# Handles: sender-valued, but the model feeds them straight back into $THR show /
# $ACT / $BATCH, which match by exact string equality. Escaping would break the
# lookup, which is the whole reason this category exists.
HANDLE_COLUMNS = {
    "envelopes": {"message_id", "in_reply_to", "references_chain"},
    "message_tiers": {"message_id"},
}

# Deliberately untrusted. Runtime does not consult this set -- default-deny
# already covers anything unlisted. It exists so the test can tell "we looked at
# this column and it holds sender-derived prose" apart from "nobody has looked at
# this column yet", and so a NEW column fails the suite instead of drifting in
# silently as merely-wrapped.
UNTRUSTED_COLUMNS = {
    # subject is a raw header. to_addr is the raw To header -- unparsed, so
    # display names and multiple recipients ride along. from_addr is
    # parseaddr()'d to a bare address, which makes escaping a NO-OP in the normal
    # case (an addr-spec contains no <, > or &) and correct in the abnormal one:
    # parseaddr falls back to the raw header string when it cannot parse, and
    # that fallback is arbitrary sender prose. Same call the change report
    # already makes for these two columns.
    "envelopes": {"subject", "from_addr", "to_addr"},
    "message_tiers": set(),
}


def query_column_trust():
    """(trusted, handle) bare column names -- see roquery.bare_name_trust."""
    return roquery.bare_name_trust(TRUSTED_COLUMNS, HANDLE_COLUMNS,
                                   UNTRUSTED_COLUMNS)


def cmd_query(args) -> int:
    """Read-only SQL over index.db. Hardening lives in roquery (11.1)."""
    db_path = args.db
    if not os.path.isfile(db_path):
        print(f"fatal: no such db: {db_path}", file=sys.stderr)
        return 1
    try:
        conn = roquery.connect(db_path)
        try:
            rows, truncated = roquery.run(conn, args.sql, args.limit)
        finally:
            conn.close()
    except roquery.QueryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    trusted_names, handle_names = query_column_trust()
    json.dump(roquery.payload(rows, truncated, args.limit, trusted_names,
                              handle_names),
              sys.stdout, indent=2, sort_keys=True)
    print()
    return 0


def change_report_trusted(prefix: str = "") -> set:
    """`CHANGE_ENTRY_TRUSTED` qualified onto all four report paths."""
    base = f"{prefix}." if prefix else ""
    return set().union(*(untrusted.under(base + kind, CHANGE_ENTRY_TRUSTED)
                         for kind in CHANGE_REPORT_KINDS))


def change_report_handles(prefix: str = "") -> set:
    base = f"{prefix}." if prefix else ""
    return set().union(*(untrusted.under(base + kind, CHANGE_ENTRY_HANDLES)
                         for kind in CHANGE_REPORT_KINDS))


# Destination tier -> logged action. Lives here rather than in batch.py so the
# batch executor and `--suggest-events` derive an action the SAME way (6.9's
# "extract that map so both share one copy, not two"): two copies of a
# tier->action mapping is two chances to disagree about what an archive is.
TIER_TO_ACTION = {
    "trash": "trash",
    "junk": "trash",
    "active": "activate",
    "ongoing": "archive",
    "cold": "archive",
}

# unrecognized_folders holds folder names, which no sender can choose.
INDEX_TRUSTED = change_report_trusted() | {"unrecognized_folders", "note", "change_counts"}
INDEX_HANDLES = change_report_handles()

# Per-bucket cap on the suggestion lists (2026-08-07, from the first live run:
# a six-week backlog produced 772KB of JSON, which no session can read). Counts
# are always exact; the full `suggested_events` set is always in log_file, so
# capping loses nothing actionable.
SUGGEST_LIST_CAP = 20

# 6.9's suggestion layer. sender_addr is a HANDLE, matching history.py's column
# classification -- it is the key `--senders` and rule predicates match on, so
# an escaped copy in a preview is a copy the model must not reuse.
SUGGEST_EVENT_TRUSTED = {
    "session_started_at", "account", "proposed_action", "final_action",
    "decided_by", "from_folder", "to_folder", "thread_state", "category",
}
SUGGEST_EVENT_HANDLES = {"message_id", "thread_root_id", "sender_addr"}
_SUG = "suggestions"
SUGGEST_TRUSTED = (
    {f"{_SUG}.log_file", f"{_SUG}.note"}
    | untrusted.under(_SUG, {"counts", "by_action"})
    | untrusted.under(f"{_SUG}.suggested_events", SUGGEST_EVENT_TRUSTED)
    | untrusted.under(f"{_SUG}.needs_confirmation",
                      {"account", "folder", "reason", "folder_tier", "date_utc"})
    | untrusted.under(f"{_SUG}.unresolved",
                      {"account", "folder", "reason", "date_utc"})
    | untrusted.under(f"{_SUG}.self_echo_absorbed",
                      {"account", "logged_action", "date_utc"})
)
SUGGEST_HANDLES = (
    untrusted.under(f"{_SUG}.suggested_events", SUGGEST_EVENT_HANDLES)
    | untrusted.under(f"{_SUG}.needs_confirmation", {"message_id"})
    | untrusted.under(f"{_SUG}.unresolved", {"message_id"})
    | untrusted.under(f"{_SUG}.self_echo_absorbed", {"message_id"})
)


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


def run_index(conn: sqlite3.Connection, maildir_root: Path, accounts: list,
              tier_maps: dict, index_bodies: bool = True) -> dict:
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

    snapshot, folders_seen = build_snapshot(maildir_root, accounts, tier_maps)

    cur = conn.cursor()
    new_entries = {}  # (account, folder, message_id) -> report entry
    flag_changed_entries = []
    parse_errors = 0
    bodies_indexed = 0
    index_bodies = index_bodies and fts_available()

    # Pass 2: upsert (all folders, before any deletions are processed).
    for (account, folder, key), (filename, path, _mtime) in snapshot.items():
        existing = existing_map.get((account, folder, key))
        if existing is None:
            try:
                fields = parse_message_file(path, account, folder, key, filename, tier_maps.get(account, {}))
            except Exception:
                parse_errors += 1
                continue
            new_id = insert_row(cur, fields)
            if index_bodies:
                # Only NEW rows decode a body. A flag change re-derives from the
                # filename suffix and never re-reads the file, so steady-state
                # sessions pay for arrivals only -- the minutes-long cost is the
                # one-off backfill, not every index.
                if index_body(cur, new_id, path):
                    bodies_indexed += 1
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
            _, old_suffix = split_maildir_filename(existing["maildir_file"])
            _, suffix = split_maildir_filename(filename)
            flags = derive_flags(suffix)
            is_flagged = int("Flagged" in flags)
            is_answered = int("Answered" in flags)
            cur.execute(UPDATE_FLAGS_SQL, (filename, json.dumps(flags), is_flagged, is_answered, utc_now(), existing["id"]))
            existing["maildir_file"] = filename
            entry = envelope_report_entry(account, folder, existing)
            entry["old_flags"] = derive_flags(old_suffix)
            entry["new_flags"] = flags
            flag_changed_entries.append(entry)
        # else: filename unchanged -> nothing to do (this is what makes
        # unchanged runs idempotent and report-silent).

    # Pass 3: reconcile orphans (only after all folders are upserted).
    moved_entries = []
    deleted_entries = []
    orphan_keys = existing_map.keys() - snapshot.keys()
    for k in sorted(orphan_keys):
        row = existing_map[k]
        account, folder = row["account"], row["folder"]
        same_folder = [
            r for r in current_by_msgid.get((account, row["message_id"]), [])
            if r["folder"] == folder
            and r["maildir_key"] != row["maildir_key"]
            and (account, folder, r["maildir_key"]) in snapshot
        ]
        if same_folder:
            # Same message_id reappeared in the SAME folder under a new
            # maildir key: an in-place rename that changed the key's stem.
            # In practice this is mbsync re-stamping ",U=<uid>" after
            # uploading a file the act layer wrote (act.py strips the
            # token on move). Nothing user-visible happened -- absorb
            # silently: drop the provisional "new" entry and the stale row.
            new_entries.pop((account, folder, row["message_id"]), None)
            cur.execute("DELETE FROM envelopes WHERE id = ?", (row["id"],))
            if index_bodies:
                unindex_body(cur, row["id"])
            continue
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
        if index_bodies:
            unindex_body(cur, row["id"])

    unrecognized_folders = sorted({folder for (_account, folder), tier in folders_seen.items() if tier == "unrecognized"})

    return {
        "new": sorted(new_entries.values(), key=_sort_key_simple),
        "moved": sorted(moved_entries, key=_sort_key_moved),
        "deleted": sorted(deleted_entries, key=_sort_key_simple),
        "flag_changed": sorted(flag_changed_entries, key=_sort_key_simple),
        "parse_errors": parse_errors,
        "bodies_indexed": bodies_indexed,
        "unrecognized_folders": unrecognized_folders,
    }


# --------------------------------------------------------------------------
# Suggested events (Phase 6.9): the change report, mechanized into proposed
# event rows. PROPOSES, NEVER LOGS -- see suggest_events().
# --------------------------------------------------------------------------


def parse_config_emails_text(text: str) -> dict:
    """{account: own email address} from config.yaml's accounts.<name>.email.

    Needed to subtract the user's own address from a sent message's recipients:
    a reply-all includes them, and they are not their own counterparty."""
    emails: dict = {}
    in_accounts = False
    accounts_indent = None
    current = None
    current_indent = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        content = raw.strip()
        if indent == 0 and content == "accounts:":
            in_accounts, accounts_indent = True, 0
            continue
        if not in_accounts:
            continue
        if indent == 0 and content != "accounts:":
            break
        if current_indent is not None and indent <= current_indent and content.endswith(":"):
            current = None
        if content.endswith(":") and (accounts_indent is None or indent > accounts_indent):
            if current is None or indent <= current_indent:
                current, current_indent = content[:-1].strip(), indent
                continue
        if current and content.startswith("email:"):
            emails[current] = content.split(":", 1)[1].strip().strip("'\"")
    return emails


def parse_config_emails(config_path) -> dict:
    path = Path(config_path)
    return parse_config_emails_text(path.read_text()) if path.is_file() else {}


def logged_this_session(data_dir, session: str) -> dict:
    """{(account, message_id): final_action} already logged for `session`.

    The authoritative self-echo record, and the actual reason 6.9 is worth
    building. The prose rule it replaces ("drop entries matching what you
    executed and logged this session") depends on model recall across a long,
    possibly compacted session; this is a SELECT and cannot forget. batch.py
    already absorbs its own echo in-memory, but hand-done flags and replies --
    and anything past a compaction boundary -- were model memory alone."""
    db = Path(data_dir) / "history.db"
    if not session or not db.is_file():
        return {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {
            (account, message_id): action
            for account, message_id, action in conn.execute(
                "SELECT account, message_id, final_action FROM events "
                "WHERE session_started_at = ?", (session,))
        }
    finally:
        conn.close()


def _recipient_counterparty(conn, entry: dict, own_email: str):
    """(addr, None) or (None, reason) for a message WE sent.

    The counterparty of a reply is its recipient. The plan reached for thread
    members' from_addr, but the To header of the sent message is the same answer
    without pulling thread assembly into the indexer -- and it fails the same
    way (a group thread has several recipients), which is the case that must
    land in `unresolved` rather than guess."""
    row = conn.execute(
        "SELECT to_addr FROM envelopes WHERE account = ? AND message_id = ? "
        "AND folder = ?",
        (entry["account"], entry["message_id"], entry["folder"]),
    ).fetchone()
    if row is None or not row[0]:
        return None, "sent message has no To header to derive a counterparty from"
    mine = (own_email or "").strip().lower()
    addrs = sorted({
        addr.strip().lower()
        for _name, addr in getaddresses([row[0]])
        if addr.strip() and addr.strip().lower() != mine
    })
    if not addrs:
        return None, "every recipient is the account's own address"
    if len(addrs) > 1:
        return None, f"{len(addrs)} recipients -- counterparty is ambiguous"
    return addrs[0], None


def suggest_events(conn, report: dict, tier_maps: dict, account_emails: dict,
                   logged: dict, session: str) -> dict:
    """Turn a change report into PROPOSED event rows. Never logs anything.

    `batch.py` is safe to let act because it acts only on explicit instruction.
    This infers intent from filesystem deltas -- one step further removed -- so
    auto-logging would make a derivation bug both invisible and scaled. The
    output is a proposal a human confirms.

    Four buckets:
      suggested_events    ready for `$HIST log-events`
      needs_confirmation  mechanically classified, but the call is the user's
      unresolved          could not be derived; reason attached, never a guess
      self_echo_absorbed  a VISIBLE LIST, not a count -- `decided_by=manual` is
                          a factual claim that the user acted outside a session,
                          so an echo-subtraction gap would relabel OUR action as
                          theirs. The list is what makes that mismatch
                          surfaceable instead of silent.

    Takes the report BEFORE it is trust-wrapped, deliberately: the rows it
    builds are written to history.db, and 6.5 established that at-rest text is
    clean. Deriving them from escaped output would store `&lt;b&gt;` as a
    subject, which then escapes again on the next read.
    """
    suggested, needs_confirmation, unresolved, absorbed = [], [], [], []

    def ref(entry, **extra):
        return {k: entry.get(k) for k in
                ("account", "message_id", "subject", "from_addr", "date_utc")} | extra

    def absorbed_echo(entry) -> bool:
        key = (entry.get("account"), entry.get("message_id"))
        if key in logged:
            absorbed.append(ref(entry, logged_action=logged[key]))
            return True
        return False

    def tier_of(account, folder):
        return classify_folder_tier(folder, tier_maps.get(account, {}))

    def event(entry, final, *, from_folder, to_folder, decided_by="manual"):
        return {
            "session_started_at": session,
            "account": entry["account"],
            "message_id": entry["message_id"],
            "sender_addr": entry.get("from_addr") or "unknown@invalid",
            "subject": entry.get("subject"),
            "proposed_action": final,
            "final_action": final,
            "from_folder": from_folder,
            "to_folder": to_folder,
            "decided_by": decided_by,
        }

    for entry in report["moved"]:
        if absorbed_echo(entry):
            continue
        src_tier = tier_of(entry["account"], entry["from_folder"])
        # A message whose only home was All Mail was never in a triage tier, so
        # it was never in front of the user in a session. Calling a label change
        # on it a *decision* attributes to them what was almost certainly a
        # server-side filter or a client's own cleanup -- and `decided_by=manual`
        # is a factual claim that they acted. Route it for review instead.
        if src_tier == "allmail":
            needs_confirmation.append(ref(
                entry, folder=entry["from_folder"], folder_tier=src_tier,
                reason="label churn on an All-Mail-only message: never in a "
                       "triage tier, so not a session decision (server-side "
                       "filter or client cleanup) -- confirm before logging"))
            continue
        tier = tier_of(entry["account"], entry["to_folder"])
        action = TIER_TO_ACTION.get(tier)
        if action is None:
            unresolved.append(ref(entry, folder=entry["to_folder"],
                                  reason=f"destination tier {tier!r} maps to no "
                                         "triage action"))
            continue
        suggested.append(event(entry, action, from_folder=entry["from_folder"],
                               to_folder=entry["to_folder"]))

    for entry in report["flag_changed"]:
        if absorbed_echo(entry):
            continue
        old, new = set(entry.get("old_flags") or []), set(entry.get("new_flags") or [])
        # \Seen churn is never an event: rule 5 makes \Seen the user's own
        # signal, and it changes constantly for reasons that are not decisions.
        if ("Flagged" in new) == ("Flagged" in old):
            continue
        if "Flagged" in new:
            suggested.append(event(entry, "flag", from_folder=entry["folder"],
                                   to_folder=None))
        else:
            # Clearing \Flagged retires a reply debt, but not via any verb in
            # ACTIONS -- they either replied (there would be a sent message) or
            # decided not to. Genuinely ambiguous, so ask.
            needs_confirmation.append(ref(
                entry, folder=entry["folder"],
                reason="\\Flagged cleared without a move: reply debt retired, "
                       "but by replying or by deciding not to?"))

    for entry in report["new"]:
        if tier_of(entry["account"], entry["folder"]) != "sent":
            # A new arrival is not a decision, and a new file in a filed folder
            # is covered by `moved` when we can see where it came from.
            continue
        if absorbed_echo(entry):
            continue
        addr, reason = _recipient_counterparty(
            conn, entry, account_emails.get(entry["account"], ""))
        if addr is None:
            unresolved.append(ref(entry, folder=entry["folder"], reason=reason))
            continue
        row = event(entry, "reply", from_folder=None, to_folder=entry["folder"])
        row["sender_addr"] = addr
        suggested.append(row)

    expunged = 0
    for entry in report["deleted"]:
        if absorbed_echo(entry):
            continue
        tier = tier_of(entry["account"], entry["folder"])
        if tier in ("trash", "junk"):
            # COUNTED, never listed. Provider auto-purge (~30 days on both
            # accounts) is a non-event: no log, no mention. Listing them as
            # things to confirm turned 822 auto-purges into 822 questions in a
            # live session -- the pre-6.9 prose rule had this right and the
            # first mechanized version lost it.
            expunged += 1
            continue
        needs_confirmation.append(ref(
            entry, folder=entry["folder"], folder_tier=tier,
            reason=f"file vanished from a {tier!r} folder without reappearing "
                   "elsewhere -- confirm this was intended"))

    return {
        "suggested_events": suggested,
        "needs_confirmation": needs_confirmation,
        "unresolved": unresolved,
        "self_echo_absorbed": absorbed,
        "expunged_from_trash": expunged,
    }


# --------------------------------------------------------------------------
# DB setup / CLI
# --------------------------------------------------------------------------

DEFAULT_DATA_DIR = Path(os.environ.get("EMAIL_TRIAGE_DATA_DIR", "~/.local/share/email-triage")).expanduser()


def default_config_path() -> Path:
    return DEFAULT_DATA_DIR / "config.yaml"


# Body search (11.6). CONTENTLESS FTS5: the term dictionary and posting lists
# are kept, the copy of the original text is not (`content=''`). Matching only
# ever consults the dictionary and postings, so search works fine without it --
# measured on 389 real messages, identical hit sets to a text-storing index for a
# term, a phrase and a prefix, and 0.55 MB against 1.45 MB.
#
# Deliberately no snippet column. It would be a SECOND door for sender prose to
# reach the model, needing its own escape/manifest path independent of
# `$THR read`, and a second at-rest copy of body text to keep consistent with the
# Maildir. Search returns ranked message-ids; you read the ones you want.
#
# `contentless_delete=1` needs SQLite 3.43+. Without it, deleting a row from a
# contentless index requires re-supplying the original text, which by
# construction we do not have -- and mail is moved and expunged constantly, so
# the index would drift permanently out of sync. rowid is `envelopes.id`.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS body_fts USING fts5(
    body,
    content='',
    contentless_delete=1,
    tokenize='unicode61'
);
"""
FTS_MIN_SQLITE = (3, 43)


def fts_available() -> bool:
    v = tuple(int(x) for x in sqlite3.sqlite_version.split(".")[:2])
    return v >= FTS_MIN_SQLITE


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    if fts_available():
        conn.executescript(FTS_SCHEMA)
    conn.commit()
    return conn


def extract_body_text(path: Path):
    """The exact text a `$THR read` would show, or None.

    Reuses threads.extract_body rather than re-deriving, so search can never
    match on text a read would not display. Imported here rather than at module
    scope to keep `indexer.py` usable when only headers are wanted."""
    import threads
    try:
        msg = threads.load_raw_message(str(path))
        return threads.extract_body(msg, force_html=False).text
    except Exception:
        return None


def index_body(cur: sqlite3.Cursor, rowid: int, path: Path) -> bool:
    """Add one message's decoded body to the FTS index. Returns whether it did.

    A body that will not decode is skipped rather than raised: a message missing
    from body search is a degraded search, while an aborted index run is a broken
    session."""
    text = extract_body_text(path)
    if not text or not text.strip():
        return False
    cur.execute("INSERT INTO body_fts (rowid, body) VALUES (?, ?)",
                (rowid, text))
    return True


def unindex_body(cur: sqlite3.Cursor, rowid: int) -> None:
    """Drop a message from the FTS index. Safe when it was never added."""
    cur.execute("DELETE FROM body_fts WHERE rowid = ?", (rowid,))


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


BACKFILL_SQL = """SELECT e.id, e.account, e.folder, e.maildir_key, e.maildir_file
                  FROM envelopes e
                  LEFT JOIN body_fts f ON f.rowid = e.id
                  WHERE f.rowid IS NULL"""


def cmd_backfill_bodies(args: argparse.Namespace) -> int:
    """One-off: add bodies for rows indexed before body search existed.

    Separate from `index` because it is genuinely slow -- it decodes every
    message rather than the arrivals -- and because a session should never
    silently pay for it. `index` keeps the FTS current from here on."""
    if not fts_available():
        print(f"error: body search needs SQLite {'.'.join(map(str, FTS_MIN_SQLITE))}+"
              f" for contentless_delete; this is {sqlite3.sqlite_version}",
              file=sys.stderr)
        return 1
    if not args.db.exists():
        print(f"fatal: no such db: {args.db}", file=sys.stderr)
        return 1
    conn = init_db(args.db)
    cur = conn.cursor()
    rows = cur.execute(BACKFILL_SQL).fetchall()
    done = skipped = 0
    for i, (rowid, account, folder, _key, filename) in enumerate(rows, 1):
        path = args.maildir / account / folder / "cur" / filename
        if not path.exists():
            path = args.maildir / account / folder / "new" / filename
        if not path.exists():
            skipped += 1
            continue
        if index_body(cur, rowid, path):
            done += 1
        else:
            skipped += 1
        if i % 500 == 0:
            conn.commit()
            print(f"  {i}/{len(rows)} ...", file=sys.stderr)
    conn.commit()
    conn.close()
    json.dump({"candidates": len(rows), "bodies_indexed": done,
               "skipped": skipped,
               "note": "skipped = no body, undecodable, or file gone since "
                       "indexing; a missing body degrades search, never the index"},
              sys.stdout, indent=2, sort_keys=True)
    print()
    return 0


SEARCH_SQL = """SELECT e.account, e.message_id, e.folder, e.folder_tier,
                       e.date_utc, e.subject, e.from_addr,
                       bm25(body_fts) AS rank
                FROM body_fts
                JOIN envelopes e ON e.id = body_fts.rowid
                WHERE body_fts MATCH ?
                ORDER BY rank
                LIMIT ?"""


def cmd_search(args: argparse.Namespace) -> int:
    """Ranked body search. Returns message-ids to go read, never snippets."""
    if not args.db.exists():
        print(f"fatal: no such db: {args.db}", file=sys.stderr)
        return 1
    try:
        conn = roquery.connect(args.db)
    except roquery.QueryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        try:
            rows = [dict(r) for r in conn.execute(
                SEARCH_SQL, (args.term, args.limit + 1))]
        except sqlite3.OperationalError as e:
            msg = str(e)
            if "no such table" in msg:
                print("error: no body index yet — run "
                      "`indexer.py backfill-bodies` once", file=sys.stderr)
            else:
                # FTS5 has its own query syntax; an unbalanced quote or a bare
                # operator is a user error, not a crash.
                print(f"error: bad search query: {msg}", file=sys.stderr)
            return 1
    finally:
        conn.close()
    truncated = len(rows) > args.limit
    rows = rows[:args.limit]
    trusted_names, handle_names = query_column_trust()
    payload = {"hits": rows, "hit_count": len(rows)}
    if truncated:
        payload["truncated"] = True
        payload["hint"] = (f"stopped at --limit {args.limit}; narrow the term "
                           "or raise the limit deliberately")
    json.dump(untrusted.apply_trust(
        payload,
        untrusted.under("hits", trusted_names | {"rank"})
        | {"hit_count", "truncated", "hint"},
        handle_fields=untrusted.under("hits", handle_names)),
        sys.stdout, indent=2, sort_keys=True)
    print()
    return 0


def cmd_init(args: argparse.Namespace) -> None:
    conn = init_db(args.db)
    conn.close()
    print(f"index.db schema ready at {args.db}")


def cmd_index(args: argparse.Namespace) -> None:
    accounts = resolve_accounts(args.maildir, args.account)
    config_path = args.config or default_config_path()
    tier_maps = load_tier_maps(config_path, accounts)
    conn = init_db(args.db)
    try:
        try:
            report = run_index(conn, args.maildir, accounts, tier_maps,
                               index_bodies=not getattr(args, "no_bodies", False))
        except sqlite3.Error as e:
            conn.rollback()
            raise FatalError(f"database error: {e}") from e
        conn.commit()
    finally:
        conn.close()
    trusted, handles = INDEX_TRUSTED, INDEX_HANDLES
    if args.suggest_events:
        if not args.session:
            raise FatalError("--suggest-events requires --session "
                             "(the echo query and every event row need it)")
        conn = sqlite3.connect(args.db)
        try:
            suggestions = suggest_events(
                conn, report, tier_maps,
                parse_config_emails(config_path),
                logged_this_session(args.data_dir, args.session),
                args.session)
        finally:
            conn.close()
        # The CLEAN rows go to a file for `$HIST log-events --file`; only an
        # ESCAPED copy goes to the model. That split is not ceremony: 6.5
        # established that at-rest text in history.db is clean, so a row the
        # model retyped out of escaped output would store `&lt;b&gt;` as a
        # subject and escape again on the next read. This way no sender text has
        # to survive a trip through the model at all -- the same move as
        # `$UNSUB exec --link N`.
        log_file = Path(args.data_dir) / "suggested-events.json"
        log_file.write_text(json.dumps(suggestions["suggested_events"], indent=2))

        counts = {k: len(v) if isinstance(v, list) else v
                  for k, v in suggestions.items()}
        by_action: dict = {}
        for row in suggestions["suggested_events"]:
            by_action[row["final_action"]] = by_action.get(row["final_action"], 0) + 1

        capped = {}
        for key, value in suggestions.items():
            if not isinstance(value, list):
                continue
            capped[key] = value[:SUGGEST_LIST_CAP]
            if len(value) > SUGGEST_LIST_CAP:
                capped[f"{key}_truncated"] = len(value) - SUGGEST_LIST_CAP

        report["suggestions"] = capped | {
            "counts": counts,
            "by_action": by_action,
            "log_file": str(log_file),
            "note": ("PROPOSALS, nothing was logged. `counts` is exact; lists "
                     f"are capped at {SUGGEST_LIST_CAP} per bucket and the "
                     "COMPLETE suggested_events set is in log_file. Log what "
                     f"you accept with `$HIST log-events --file {log_file}` -- "
                     "pass the FILE, never retype the rows: the copy here is "
                     "entity-escaped for display, and that file is deliberately "
                     "NOT escaped, so do not read it either. Edit it to drop "
                     "any you reject. A large suggested_events count after a "
                     "long gap is mailbox drift, not a session's worth of "
                     "decisions -- confirm the volume with the user before "
                     "logging it as theirs."),
        }
        # With --suggest-events the per-entry arrays are what the suggestions
        # were DERIVED FROM, so shipping both doubles the payload for no gain --
        # that redundancy is most of what made the first live run 772KB. Counts
        # keep the scale visible; run without the flag to see raw entries.
        report["change_counts"] = {kind: len(report.pop(kind))
                                   for kind in CHANGE_REPORT_KINDS}
        trusted = (trusted - change_report_trusted()) | SUGGEST_TRUSTED
        handles = (handles - change_report_handles()) | SUGGEST_HANDLES
    if not args.suggest_events:
        # Running plain `index` consumes the deltas and discards the one chance
        # to derive events from them -- the report is not replayable. Say so
        # rather than letting a silent omission look like a clean run.
        report["note"] = ("No --suggest-events: these deltas were consumed "
                          "WITHOUT deriving proposed events, and a second index "
                          "run will not show them again. Intentional only for "
                          "diagnostics; the session flow passes "
                          "--suggest-events --session <ts> on every index.")
    print(json.dumps(untrusted.apply_trust(report, trusted,
                                           handle_fields=handles),
                     indent=2, sort_keys=True))


# Fast-lane tier keys for priority-folders (4.3): untriaged + active + sent +
# drafts, in this exact order -- archiving activity must never promote cold
# storage into the fast lane, and under inbox-only delivery no other folder
# can originate liveness (Phase 4 design note).
PRIORITY_FAST_TIER_KEYS = ("untriaged", "active", "sent", "drafts")


def priority_fast_map(config_path: Path) -> dict:
    """Per-account Stage-1 fast set. Also imported by session.py so `start`
    can hand the model everything it needs in one call."""
    parsed = parse_config_tiers(config_path)
    account_names = sorted(parsed.keys()) if parsed else sorted(DEFAULT_TIER_MAPS.keys())

    result = {}
    for account in account_names:
        tier_map = parsed.get(account) or DEFAULT_TIER_MAPS.get(account, {})
        fast = [tier_map[k] for k in PRIORITY_FAST_TIER_KEYS if tier_map.get(k)]
        result[account] = {
            "fast": fast,
            # Ready-made Stage-1 argument (5.7): pass verbatim as ONE
            # argument -- mbsync -c "$MBSYNCRC" '<mbsync_fast>' -- instead
            # of hand-assembling the box list (a quoting slip in a live
            # session cost a retry cycle).
            "mbsync_fast": f"{account}:{','.join(fast)}",
            "slow_hint": "full channel",
        }
    return result


def cmd_priority_folders(args: argparse.Namespace) -> None:
    # Wrapped under a key (was the bare map) so it can carry `_trust`, and so
    # it matches the field name `session.py start` already uses for the same
    # data. Keys are account names from config, values are folder names --
    # a dynamic key space, but ours, which is the one case a trusted subtree
    # is legitimate.
    print(json.dumps(
        untrusted.apply_trust(
            {"priority_folders": priority_fast_map(args.config or default_config_path())},
            (), trusted_subtrees={"priority_folders"}),
        indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="create index.db schema only")
    init_p.add_argument("--db", type=Path, default=DEFAULT_DATA_DIR / "index.db")
    init_p.set_defaults(func=cmd_init)

    idx_p = sub.add_parser("index", help="index a Maildir root into index.db (auto-inits schema)")
    idx_p.add_argument("--maildir", type=Path, default=DEFAULT_DATA_DIR / "mail",
                       help="root containing one subdirectory per account; default <data-dir>/mail")
    idx_p.add_argument("--db", type=Path, default=DEFAULT_DATA_DIR / "index.db")
    idx_p.add_argument(
        "--account",
        action="append",
        help="limit indexing to this account (repeatable); default: every subdirectory of --maildir",
    )
    idx_p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="config.yaml path; default: <data-dir>/config.yaml via $EMAIL_TRIAGE_DATA_DIR "
             "(falls back to built-in tier-name defaults if absent)",
    )
    idx_p.add_argument(
        "--suggest-events", action="store_true", dest="suggest_events",
        help="also derive PROPOSED event rows from the change report (6.9). A "
             "flag on `index`, not its own subcommand, because the report is "
             "ephemeral -- a second index run reports nothing, so the "
             "suggestions have to be produced in the same pass that consumed "
             "the deltas.",
    )
    idx_p.add_argument("--session", default=None,
                       help="session_started_at; required with --suggest-events")
    idx_p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                       help="holds history.db (for self-echo) and receives "
                            "suggested-events.json")
    idx_p.add_argument(
        "--no-bodies", action="store_true", dest="no_bodies",
        help="skip body-search indexing for this run. Arrivals normally cost "
             "one body decode each, which is seconds; this exists for a fast "
             "header-only rebuild, and leaves body search stale until the next "
             "`backfill-bodies`")
    idx_p.set_defaults(func=cmd_index)

    pf_p = sub.add_parser(
        "priority-folders",
        help="config-driven mbsync Stage-1 box lists per account (fast = untriaged+active+sent+drafts)",
    )
    pf_p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="config.yaml path; default: <data-dir>/config.yaml via $EMAIL_TRIAGE_DATA_DIR "
             "(falls back to built-in tier-name defaults if absent)",
    )
    pf_p.set_defaults(func=cmd_priority_folders)

    q_p = sub.add_parser(
        "query",
        help="read-only SQL over index.db (message-level search: recipient, "
             "sender, subject, date range, folder, flags, and combinations)",
        description="Read-only SQL over index.db. The connection cannot write, "
                    "cannot ATTACH, and is aborted if it runs away. GLOB and "
                    "LIKE are native; REGEXP is registered for patterns they "
                    "cannot express, e.g. "
                    "WHERE to_addr REGEXP '(?i)raimund'.",
    )
    # --sql, not positional: `$HIST query` already spells it this way, and two
    # copies of the same surface with different invocations is how one gets
    # called wrong.
    q_p.add_argument("--sql", required=True, help="a single SELECT statement")
    q_p.add_argument("--db", type=Path, default=DEFAULT_DATA_DIR / "index.db")
    q_p.add_argument("--limit", type=int, default=roquery.ROW_CAP,
                     help=f"max rows returned (default {roquery.ROW_CAP}); "
                          "truncation is reported, never silent")
    q_p.set_defaults(func=cmd_query)

    bf_p = sub.add_parser(
        "backfill-bodies",
        help="one-off: populate the body search index for already-indexed mail "
             "(slow — decodes every message; `index` keeps it current after)")
    bf_p.add_argument("--db", type=Path, default=DEFAULT_DATA_DIR / "index.db")
    bf_p.add_argument("--maildir", type=Path, default=DEFAULT_DATA_DIR / "mail")
    bf_p.set_defaults(func=cmd_backfill_bodies)

    s_p = sub.add_parser(
        "search",
        help="ranked full-text search over decoded message BODIES",
        description="Contentless FTS5, so results are ranked message-ids to go "
                    "read -- there are no snippets by design. FTS5 query "
                    "syntax: bare terms AND together, \"quoted phrase\" is a "
                    "phrase, trailing * is a prefix.")
    s_p.add_argument("--term", required=True, help="FTS5 query")
    s_p.add_argument("--db", type=Path, default=DEFAULT_DATA_DIR / "index.db")
    s_p.add_argument("--limit", type=int, default=50)
    s_p.set_defaults(func=cmd_search)

    args = parser.parse_args()
    try:
        # Subcommands that report failure return a code; the rest return None.
        sys.exit(args.func(args) or 0)
    except FatalError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except (OSError, sqlite3.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
