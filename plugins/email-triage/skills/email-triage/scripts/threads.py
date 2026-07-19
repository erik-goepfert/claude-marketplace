#!/usr/bin/env python3
"""Thread assembly + state derivation over index.db (email-triage skill).

Design refs:
  - email-skill-design.md §10 (Thread assembly: link by Message-ID/In-Reply-To
    and the full References chain; guarded subject fallback; connected
    components; two entry modes by single message or by account set).
  - email-folder-taxonomy.md, "Thread-state derivation" (blocked(thread) =
    OR(\\Flagged) over the thread) and "Threads stick together" (system
    folders sit outside the tier model; this module reads Sent/Trash/Junk via
    the per-message tier, same as the indexer's message_tiers view).
  - implementation-plan.md item 2.5 and its "Revised (2026-07-09, taxonomy)"
    note: thread state is OR(\\Flagged), decoupled from recency, not the
    design doc's original "latest INBOX/Sent message" rollup.
  - scripts/indexer.py -- the envelopes DDL and message_tiers view this module
    queries. Per-message tier resolution (dedup All-Mail duplicates, prefer
    the non-allmail row) is re-implemented here in Python, matching that
    view's precedence order, rather than querying the view directly, because
    thread assembly needs the full row set (all folders) per message anyway.

Threads are assembled in Python (connected components over the envelope
graph), not as a giant recursive CTE -- see §10 point 3 ("a recursive CTE
seeded from any member, or grouping by References-chain root"); either is
valid, Python was chosen for readability of the guarded-fallback pass, which
does not express cleanly as SQL.

Revised state model actually implemented here (per the taxonomy carve-out and
implementation-plan 2.5's revision note, both more specific than design §10's
now-superseded "only INBOX and Sent drive state" table), extended by Phase 4
item 4.5 for the drafts axis:
  - Members whose per-message tier is trash or junk are history only: they
    never drive state, and flags on them are never counted in the OR.
  - Members whose per-message tier is drafts are excluded from state
    derivation too, but for a different reason (4.5): a draft isn't
    communication -- it can't move the conversation axis, it IS its own
    axis. `drafted` (see below) carries that information instead.
  - blocked(thread) = OR(is_flagged) over state-eligible members (excludes
    trash/junk/drafts) => needs-reply.
  - else if the latest state-eligible member (by date_utc) is in the sent
    tier => waiting.
  - else => holding.
  - inactive_days is always "days since that same latest state-eligible
    member's date_utc" -- in the waiting branch that member is, by
    construction, also the latest sent-tier member, so the two phrasings in
    the spec ("latest sent-tier member" for waiting, "latest state-eligible
    member" otherwise) resolve to one formula. A draft member, even if it is
    chronologically the thread's newest message, is never that "latest"
    member (4.5 point (b)/(d)).
  - `drafted`: a second, orthogonal derived status -- true iff ANY member
    (including trash/junk/drafts-tier ones; the full member list) has tier
    drafts. Independent of `state`: a thread can be needs-reply AND drafted,
    holding AND drafted, etc.

Subcommands:
  list    All threads (per account) having >=1 member whose per-message tier
          is in the given set (default: untriaged + active, the live tiers).
  show    The full thread containing a given message-id.
  read    Phase 5.1 / review §4 ("the biggest tooling gap: content access"):
          one message's headers + body, read straight off its raw Maildir
          file. Kills the himalaya id-resolution dance, lossy `--preview`
          rendering, and the \\Seen risk in one move -- a raw open() for
          reading can't touch flags, and this module never writes to the
          file it resolves. Resolution reuses this module's existing
          account+message_id -> (folder, maildir_key) precedence (see
          `resolve_message_row`), then act.py's on-disk stem-match
          (`resolve_current_file`) to find the current filename -- imported
          rather than duplicated (act.py is left unmodified). unsub.py
          shares this same read path (see its `load_message`) rather than
          re-implementing it, per implementation-plan.md 5.3.

Both print JSON to stdout (indent=2, sort_keys=True, matching indexer.py's
report style).
"""

import argparse
import datetime
import email
import email.policy
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from email.utils import getaddresses
from html.parser import HTMLParser
from pathlib import Path

import act

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# Mirrors indexer.py's message_tiers CASE precedence (lower = wins). 'allmail'
# is deliberately absent -- a pure-allmail message resolves to 'cold'
# (Gmail's virtual Cold tier), never appears as 'allmail' in output.
TIER_RANK = {
    "untriaged": 1,
    "active": 2,
    "ongoing": 3,
    "cold": 4,
    "sent": 5,
    "drafts": 6,
    "trash": 7,
    "junk": 8,
    "unrecognized": 9,
    "container": 10,
}

# Taxonomy carve-out: trash/junk members are history only -- never drive
# thread state, flags on them never count toward the OR.
HISTORY_TIERS = {"trash", "junk"}

# Drafts axis (4.5): excluded from STATE derivation for a different reason
# than trash/junk (a draft isn't communication, not that it's history), but
# mechanically it's excluded the same way -- never in the state-eligible set.
STATE_EXCLUDED_TIERS = HISTORY_TIERS | {"drafts"}

# "the live tiers" (implementation-plan 2.5 / taxonomy Active-tier model;
# tier IDs renamed per Phase 4 4.2: inbox->untriaged).
DEFAULT_LIST_TIERS = ["untriaged", "active"]

SUBJECT_PREFIX_RE = re.compile(r"^\s*(re|fwd?|aw)\s*:\s*", re.IGNORECASE)


class FatalError(Exception):
    """A condition that should abort the run with exit code 1."""


# --------------------------------------------------------------------------
# Subject / participant normalization (§10 guarded subject fallback)
# --------------------------------------------------------------------------


def normalize_subject(subject) -> str:
    """Strip Re:/Fwd:/Fw:/AW: prefixes repeatedly (case-insensitive), collapse
    whitespace. Case of the remaining text is preserved (used for display);
    callers that need match semantics casefold() the result themselves."""
    if not subject:
        return ""
    s = subject
    while True:
        stripped = SUBJECT_PREFIX_RE.sub("", s)
        if stripped == s:
            break
        s = stripped
    return re.sub(r"\s+", " ", s).strip()


def extract_participants(from_addr, to_addr) -> set:
    """Normalized (lowercased, bare-address) participant set from From/To.
    Uses email.utils.getaddresses so a multi-recipient To header is split
    correctly; from_addr is already a bare address at index time (indexer.py
    parseaddr()), but running it through the same parser is harmless."""
    participants = set()
    for field in (from_addr, to_addr):
        if not field:
            continue
        for _name, addr in getaddresses([field]):
            addr = addr.strip().lower()
            if addr:
                participants.add(addr)
    return participants


# --------------------------------------------------------------------------
# Union-Find
# --------------------------------------------------------------------------


class DSU:
    def __init__(self, items):
        self.parent = {x: x for x in items}
        self.rank = {x: 0 for x in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return ra
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1
        return ra


# --------------------------------------------------------------------------
# DB loading: per-account, deduped by message_id (All-Mail duplicates
# collapse to one member, preferring the non-allmail row for display fields
# -- same precedence as indexer.py's message_tiers view).
# --------------------------------------------------------------------------


def connect_db(db_path: Path) -> sqlite3.Connection:
    if not Path(db_path).is_file():
        raise FatalError(f"database not found: {db_path}")
    return sqlite3.connect(str(db_path))


def all_accounts(conn: sqlite3.Connection) -> list:
    rows = conn.execute("SELECT DISTINCT account FROM envelopes ORDER BY account").fetchall()
    return [r[0] for r in rows]


def resolve_message_row(conn: sqlite3.Connection, account: str, message_id: str):
    """Return (folder, maildir_key) for one message, using the SAME
    non-allmail-preferred / TIER_RANK precedence as `load_account_members`
    (module docstring; matches indexer.py's message_tiers view). A message
    with rows in more than one folder (e.g. a Gmail All-Mail duplicate)
    resolves to its single highest-precedence row -- there is exactly one
    "the message" for read/unsub purposes, same as everywhere else in this
    module."""
    rows = conn.execute(
        "SELECT folder, maildir_key, folder_tier FROM envelopes WHERE account = ? AND message_id = ?",
        (account, message_id),
    ).fetchall()
    if not rows:
        raise FatalError(f"no index row for account={account!r} message_id={message_id!r}")

    non_allmail = [r for r in rows if r[2] != "allmail"]
    if non_allmail:
        folder, maildir_key, _tier = min(non_allmail, key=lambda r: TIER_RANK.get(r[2], 999))
    else:
        # Every row is All-Mail-only: no better copy to prefer, take the first.
        folder, maildir_key, _tier = rows[0]
    return folder, maildir_key


def resolve_message_file(maildir_root, account: str, message_id: str, conn: sqlite3.Connection) -> str:
    """Full current path to one message's raw file. Wraps act.py's
    resolve_current_file (disk stem-match, tolerant of renames since the
    last index run) so callers get one FatalError type regardless of
    whether the failure was a missing index row or a missing file."""
    folder, maildir_key = resolve_message_row(conn, account, message_id)
    try:
        return act.resolve_current_file(str(maildir_root), folder, maildir_key)
    except act.FatalError as e:
        raise FatalError(str(e)) from e


# --------------------------------------------------------------------------
# Raw-file content access (Phase 5.1 / review §4): headers + body straight
# off the Maildir file, via the stdlib email module (policy=default).
# --------------------------------------------------------------------------

# The `read` default's header selection -- deliberately narrow (review §4's
# complaint was too much noise from himalaya's own dumps, not too little).
SELECTED_HEADERS = ("From", "To", "Cc", "Date", "Subject", "Message-ID", "In-Reply-To")


def load_raw_message(path: str):
    """Read + parse a message's raw file. Plain read-only open() -- no
    rename, no flag touch, and (unlike a flag-changing tool) no reason to
    ever call os.utime either, so the file's mtime is untouched by
    construction."""
    with open(path, "rb") as f:
        raw = f.read()
    return email.message_from_bytes(raw, policy=email.policy.default)


def selected_message_headers(msg) -> dict:
    return {name: (str(msg[name]) if msg[name] is not None else None) for name in SELECTED_HEADERS}


def full_message_headers(msg) -> dict:
    """Every header in the raw file, in first-seen order; a header repeated
    (e.g. Received) collects into a list rather than silently keeping only
    the last occurrence."""
    headers = {}
    for name in dict.fromkeys(msg.keys()):
        values = [str(v) for v in msg.get_all(name, [])]
        headers[name] = values[0] if len(values) == 1 else values
    return headers


def find_part(msg, subtype: str):
    """First non-multipart part of content type text/<subtype> that isn't
    an attachment (walks in document order, so multipart/alternative's
    preferred ordering is respected for the common plain-then-html case)."""
    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_type() != f"text/{subtype}":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        return part
    return None


def decode_text_part(part) -> str:
    """Charset-aware decode of a text part's payload: get_payload(decode=True)
    undoes the transfer encoding (base64/quoted-printable) to raw bytes,
    then the part's own declared charset (falling back to utf-8) decodes
    those bytes to str."""
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


class _HTMLTextExtractor(HTMLParser):
    """Tag-stripped plain text from an HTML body: drops <script>/<style>
    content entirely, inserts a line break at block-level tags so the
    result isn't one giant run-on line, and collapses interior whitespace."""

    _BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"}
    _SKIPPED_TAGS = {"script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIPPED_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = (re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.splitlines())
        return "\n".join(ln for ln in lines if ln)


def strip_html(html_text: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html_text)
    parser.close()
    return parser.text()


def extract_body(msg, force_html: bool):
    """Returns (body, source_part). Default is the message's text/plain
    part; falls back to tag-stripped text/html when there is no text/plain
    part OR --html-text was given -- design review §4's two trigger
    conditions ("when no text/plain, or flag given") are ORed, not
    exclusive alternatives, so a plain-text message is unaffected by
    --html-text. If --html-text is given but the message has no text/html
    part at all, falls back to text/plain rather than returning nothing
    (an ambiguity the spec didn't cover; returning no body when a
    perfectly readable plain part exists would be a worse default)."""
    plain_part = find_part(msg, "plain")
    if plain_part is not None and not force_html:
        return decode_text_part(plain_part), "text/plain"

    html_part = find_part(msg, "html")
    if html_part is not None:
        return strip_html(decode_text_part(html_part)), "text/html-stripped"

    if plain_part is not None:
        return decode_text_part(plain_part), "text/plain"

    return None, None


def load_account_members(conn: sqlite3.Connection, account: str) -> dict:
    """Return {message_id: member_dict} for one account, deduped across
    All-Mail/label duplicate rows. member_dict carries everything thread
    assembly + state derivation need: header links, resolved tier, display
    fields, and the precomputed participant set."""
    rows = conn.execute(
        """SELECT message_id, folder, folder_tier, in_reply_to, references_chain,
                  subject, from_addr, to_addr, date_utc, flags, is_flagged
           FROM envelopes WHERE account = ? ORDER BY id""",
        (account,),
    ).fetchall()

    grouped = defaultdict(list)
    for row in rows:
        grouped[row[0]].append(row)

    members = {}
    for message_id, group in grouped.items():
        parsed = []
        for (mid, folder, folder_tier, in_reply_to, references_chain_json, subject,
             from_addr, to_addr, date_utc, flags_json, is_flagged) in group:
            parsed.append({
                "message_id": mid,
                "folder": folder,
                "folder_tier": folder_tier,
                "in_reply_to": in_reply_to,
                "references_chain": json.loads(references_chain_json) if references_chain_json else [],
                "subject": subject,
                "from_addr": from_addr,
                "to_addr": to_addr,
                "date_utc": date_utc,
                "flags": json.loads(flags_json) if flags_json else [],
                "is_flagged": is_flagged,
            })

        non_allmail = [r for r in parsed if r["folder_tier"] != "allmail"]
        if non_allmail:
            winner = min(non_allmail, key=lambda r: TIER_RANK.get(r["folder_tier"], 999))
            tier = winner["folder_tier"]
        else:
            # Every row for this message is All-Mail-only -> Gmail's virtual
            # Cold tier (message_tiers view semantics), not literal 'allmail'.
            winner = parsed[0]
            tier = "cold"

        members[message_id] = {
            "message_id": message_id,
            "folder": winner["folder"],
            "tier": tier,
            "in_reply_to": winner["in_reply_to"],
            "references_chain": winner["references_chain"],
            "subject": winner["subject"],
            "from_addr": winner["from_addr"],
            "date_utc": winner["date_utc"],
            "flags": winner["flags"],
            "is_flagged": winner["is_flagged"],
            "participants": extract_participants(winner["from_addr"], winner["to_addr"]),
        }
    return members


# --------------------------------------------------------------------------
# Thread assembly (§10: link, guarded subject fallback, connected components)
# --------------------------------------------------------------------------


def assemble_account_threads(members: dict) -> list:
    """Connected components over one account's message graph. Returns a list
    of sets of message_id.

    Pass 1: union message_id <-> in_reply_to and message_id <-> every
    references_chain entry, but only when the target message_id is itself a
    known node in this account -- a header value pointing at a message we
    never indexed is not a "usable" link.

    Pass 2 (guarded subject fallback, §10 point 2): a message is "link-less"
    iff, after pass 1, it sits alone in a singleton component (by
    construction, any component with >1 member got that way through a real
    header edge, so singleton == no usable header link at all -- incoming or
    outgoing). Only such link-less messages may attempt to merge into another
    component (of any size), and only when (a) their normalized subject
    matches a normalized subject already present in that other component and
    (b) the two share >=1 participant address. A component that already has
    internal header links is never itself the *source* of a fallback merge --
    only linkless singletons drive merges; any component may be a target.
    """
    ids = list(members.keys())
    if not ids:
        return []

    dsu = DSU(ids)
    id_set = set(ids)

    for mid, m in members.items():
        irt = m["in_reply_to"]
        if irt and irt in id_set and irt != mid:
            dsu.union(mid, irt)
        for ref in m["references_chain"]:
            if ref in id_set and ref != mid:
                dsu.union(mid, ref)

    comps = defaultdict(set)
    for mid in ids:
        comps[dsu.find(mid)].add(mid)

    linkless = sorted(mid for root, group in comps.items() if len(group) == 1 for mid in group)

    comp_subjects = {
        root: {normalize_subject(members[m]["subject"]).casefold() for m in group}
        for root, group in comps.items()
    }
    comp_participants = {
        root: set().union(*(members[m]["participants"] for m in group))
        for root, group in comps.items()
    }

    for mid in linkless:
        root_l = dsu.find(mid)
        if root_l not in comp_subjects:
            continue  # already absorbed into another component earlier in this pass

        subj_l = normalize_subject(members[mid]["subject"]).casefold()
        parts_l = members[mid]["participants"]
        if not subj_l or not parts_l:
            # Nothing to guard against for a blank subject or address-less
            # message -- never a fallback candidate (would otherwise merge
            # every blank-subject message in the mailbox into one thread).
            continue

        match_root = None
        for other_root in sorted(comp_subjects.keys()):
            if other_root == root_l:
                continue
            if subj_l in comp_subjects[other_root] and (parts_l & comp_participants[other_root]):
                match_root = other_root
                break

        if match_root is None:
            continue

        merged_subjects = comp_subjects.pop(root_l) | comp_subjects.pop(match_root)
        merged_participants = comp_participants.pop(root_l) | comp_participants.pop(match_root)
        new_root = dsu.union(root_l, match_root)
        comp_subjects[new_root] = merged_subjects
        comp_participants[new_root] = merged_participants

    final = defaultdict(set)
    for mid in ids:
        final[dsu.find(mid)].add(mid)
    return list(final.values())


# --------------------------------------------------------------------------
# Thread state derivation (taxonomy doc, "Thread-state derivation")
# --------------------------------------------------------------------------


def latest_dated(members: list):
    """Latest member by date_utc (None dates excluded); deterministic
    message_id tie-break. None if no member has a parseable date."""
    dated = [m for m in members if m.get("date_utc")]
    if not dated:
        return None
    return max(dated, key=lambda m: (m["date_utc"], m["message_id"]))


def days_since(date_str, now: datetime.datetime = None):
    if not date_str:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return max(0, (now - dt).days)


def compute_state(members: list):
    """Returns (state, inactive_days). See module docstring for the exact
    (revised) derivation. Excludes trash/junk (history) AND drafts (4.5's
    independent axis) from every part of this computation."""
    state_eligible = [m for m in members if m["tier"] not in STATE_EXCLUDED_TIERS]
    latest = latest_dated(state_eligible)

    blocked = any(m["is_flagged"] for m in state_eligible)
    if blocked:
        state = "needs-reply"
    elif latest is not None and latest["tier"] == "sent":
        state = "waiting"
    else:
        # Covers both genuine 'holding' and the degenerate case where every
        # member is trash/junk/drafts (state_eligible empty, latest is None)
        # -- there is no server-side state left to read in that case, so it
        # falls through to 'holding' with a null inactive_days below.
        state = "holding"

    inactive_days = days_since(latest["date_utc"]) if latest is not None else None
    return state, inactive_days


def compute_drafted(members: list) -> bool:
    """4.5: true iff ANY member (the full list -- trash/junk/drafts-tier
    included) has tier drafts. Orthogonal to `state`."""
    return any(m["tier"] == "drafts" for m in members)


def thread_root_id(members: list) -> str:
    """References-root (first entry of the longest References chain among
    the thread's members) or, failing that, the oldest member by date_utc."""
    longest_chain = []
    for m in members:
        if len(m["references_chain"]) > len(longest_chain):
            longest_chain = m["references_chain"]
    if longest_chain:
        return longest_chain[0]

    dated = [m for m in members if m["date_utc"]]
    if dated:
        return min(dated, key=lambda m: (m["date_utc"], m["message_id"]))["message_id"]
    return sorted(members, key=lambda m: m["message_id"])[0]["message_id"]


def member_output(m: dict) -> dict:
    return {
        "message_id": m["message_id"],
        "folder": m["folder"],
        "folder_tier": m["tier"],
        "date_utc": m["date_utc"],
        "from_addr": m["from_addr"],
        "subject": m["subject"],
        "flags": m["flags"],
        "is_flagged": bool(m["is_flagged"]),
    }


def build_thread_object(account: str, mlist: list):
    """Returns (thread_dict, sort_date) -- sort_date is the newest member's
    date_utc (or '' if none), used only by `list`'s ordering and dropped
    otherwise."""
    state, inactive_days = compute_state(mlist)
    drafted = compute_drafted(mlist)
    newest = latest_dated(mlist)
    if newest is None:
        newest = sorted(mlist, key=lambda m: m["message_id"])[0]

    sorted_members = sorted(mlist, key=lambda m: (m["date_utc"] or "", m["message_id"]))
    thread_obj = {
        "account": account,
        "root_message_id": thread_root_id(mlist),
        "state": state,
        "drafted": drafted,
        "inactive_days": inactive_days,
        "member_count": len(mlist),
        "subject": normalize_subject(newest["subject"]),
        "members": [member_output(m) for m in sorted_members],
    }
    return thread_obj, (newest.get("date_utc") or "")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> None:
    conn = connect_db(args.db)
    try:
        accounts = args.account or all_accounts(conn)
        tiers = set(args.tier) if args.tier else set(DEFAULT_LIST_TIERS)

        sender_q = (args.sender or "").lower()
        subject_q = (args.subject_contains or "").lower()

        result = []
        for account in accounts:
            members = load_account_members(conn, account)
            for group in assemble_account_threads(members):
                mlist = [members[mid] for mid in group]
                if not any(m["tier"] in tiers for m in mlist):
                    continue
                # Cheap in-process filters (added after the first live
                # session spent 27 ad-hoc python -c calls re-filtering
                # dumped JSON): substring match, case-insensitive, against
                # ANY member's from_addr / subject.
                if sender_q and not any(
                    sender_q in (m["from_addr"] or "").lower() for m in mlist
                ):
                    continue
                if subject_q and not any(
                    subject_q in (m["subject"] or "").lower() for m in mlist
                ):
                    continue
                thread_obj, sort_date = build_thread_object(account, mlist)
                result.append((thread_obj, sort_date))

        # (account asc, newest date desc): stable-sort by date desc first,
        # then by account asc, so the date ordering survives within account.
        result.sort(key=lambda pair: pair[1], reverse=True)
        result.sort(key=lambda pair: pair[0]["account"])

        print(json.dumps([t for t, _ in result], indent=2, sort_keys=True))
    finally:
        conn.close()


def cmd_residue(args: argparse.Namespace) -> None:
    """End-of-session residue check (5.7): everything still sitting in the
    untriaged tier, compact -- one entry per thread, not full member dumps.
    An empty list is the definition of a clean close; anything listed must
    be either deliberate (waiting thread, user said leave it) or unfinished
    work for the session note."""
    conn = connect_db(args.db)
    try:
        accounts = args.account or all_accounts(conn)
        tiers = set(args.tier) if args.tier else {"untriaged"}
        residue = []
        total = 0
        for account in accounts:
            members = load_account_members(conn, account)
            for group in assemble_account_threads(members):
                mlist = [members[mid] for mid in group]
                hits = [m for m in mlist if m["tier"] in tiers]
                if not hits:
                    continue
                state, _inactive = compute_state(mlist)
                newest = latest_dated(hits) or hits[0]
                total += len(hits)
                residue.append({
                    "account": account,
                    "root_message_id": thread_root_id(mlist),
                    "state": state,
                    "drafted": compute_drafted(mlist),
                    "subject": normalize_subject(newest["subject"]),
                    "from_addr": newest["from_addr"],
                    "date_utc": newest["date_utc"],
                    "residual_count": len(hits),
                    "folders": sorted({m["folder"] for m in hits}),
                })
        residue.sort(key=lambda r: (r["account"], r["date_utc"] or ""), reverse=False)
        print(json.dumps({"residue": residue, "threads": len(residue),
                          "messages": total}, indent=2, sort_keys=True))
    finally:
        conn.close()


def cmd_show(args: argparse.Namespace) -> None:
    conn = connect_db(args.db)
    try:
        members = load_account_members(conn, args.account)
        if args.message_id not in members:
            print(
                f"error: unknown message-id for account {args.account!r}: {args.message_id}",
                file=sys.stderr,
            )
            sys.exit(1)

        for group in assemble_account_threads(members):
            if args.message_id in group:
                mlist = [members[mid] for mid in group]
                thread_obj, _sort_date = build_thread_object(args.account, mlist)
                print(json.dumps(thread_obj, indent=2, sort_keys=True))
                return

        # Unreachable: every known message_id belongs to exactly one
        # assembled component. Kept as a defensive, clearly-labeled error
        # rather than an uncaught KeyError/None if that ever changes.
        print(f"error: message-id not assembled into any thread: {args.message_id}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


def cmd_read(args: argparse.Namespace) -> None:
    conn = connect_db(args.db)
    try:
        path = resolve_message_file(args.maildir, args.account, args.message_id, conn)
    finally:
        conn.close()

    msg = load_raw_message(path)
    rel_file = os.path.relpath(path, str(args.maildir))

    if args.headers_only:
        result = {
            "headers": full_message_headers(msg),
            "body": None,
            "source_part": None,
            "file": rel_file,
        }
    else:
        body, source_part = extract_body(msg, force_html=args.html_text)
        result = {
            "headers": selected_message_headers(msg),
            "body": body,
            "source_part": source_part,
            "file": rel_file,
        }
    print(json.dumps(result, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list", help="list all threads with >=1 member in the given tier set")
    list_p.add_argument("--db", required=True, type=Path)
    list_p.add_argument("--account", action="append", help="limit to this account (repeatable); default: all accounts")
    list_p.add_argument(
        "--tier",
        action="append",
        help=f"include threads with >=1 member in this tier (repeatable); default: {DEFAULT_LIST_TIERS}",
    )
    list_p.add_argument(
        "--sender",
        help="only threads where some member's from_addr contains this "
             "(case-insensitive substring)",
    )
    list_p.add_argument(
        "--subject-contains",
        dest="subject_contains",
        help="only threads where some member's subject contains this "
             "(case-insensitive substring)",
    )
    list_p.set_defaults(func=cmd_list)

    res_p = sub.add_parser(
        "residue",
        help="end-of-session check: compact list of threads still holding "
             "untriaged-tier messages (empty = clean close)",
    )
    res_p.add_argument("--db", required=True, type=Path)
    res_p.add_argument("--account", action="append", help="limit to this account (repeatable); default: all accounts")
    res_p.add_argument(
        "--tier", action="append",
        help="count residue in this tier (repeatable); default: untriaged only",
    )
    res_p.set_defaults(func=cmd_residue)

    show_p = sub.add_parser("show", help="show the full thread containing a message-id")
    show_p.add_argument("--db", required=True, type=Path)
    show_p.add_argument("--account", required=True)
    show_p.add_argument("--message-id", required=True, dest="message_id")
    show_p.set_defaults(func=cmd_show)

    read_p = sub.add_parser(
        "read",
        help="raw-file-backed content read for one message: headers + body "
             "(Phase 5.1 / review §4 -- replaces the himalaya read path)",
    )
    read_p.add_argument("--db", required=True, type=Path)
    read_p.add_argument("--account", required=True)
    read_p.add_argument("--message-id", required=True, dest="message_id")
    read_p.add_argument(
        "--maildir", required=True, type=Path,
        help="account Maildir root (e.g. .../mail/icloud) -- same convention as act.py's --maildir",
    )
    read_p.add_argument(
        "--html-text", action="store_true", dest="html_text",
        help="prefer tag-stripped text/html over text/plain; also the automatic "
             "fallback when there is no text/plain part",
    )
    read_p.add_argument(
        "--headers-only", action="store_true", dest="headers_only",
        help="full header block only, no body",
    )
    read_p.set_defaults(func=cmd_read)

    return parser


def main() -> None:
    parser = build_parser()
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
