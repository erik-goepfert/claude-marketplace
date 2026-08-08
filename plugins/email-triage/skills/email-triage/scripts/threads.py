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
from typing import NamedTuple
from email.utils import getaddresses
from html.parser import HTMLParser
from pathlib import Path

import act
import untrusted

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


# --------------------------------------------------------------------------
# Hidden-text detection (Phase 6.4; untrusted-content-skill-design.md §4 step 3)
# --------------------------------------------------------------------------
# Declarations that hide an element's text from a human while leaving it in the
# bytes a model reads. `opacity:0` needs the lookahead so it doesn't match
# `opacity:0.5`; `mso-hide` is Outlook's preheader mechanism, the single most
# common place real mail hides text.
_HIDDEN_DECL_RE = re.compile(
    r"display\s*:\s*none"
    r"|visibility\s*:\s*hidden"
    r"|opacity\s*:\s*0(?![.\d])"
    r"|mso-hide\s*:\s*all",
    re.I,
)
_FONT_SIZE_RE = re.compile(r"font-size\s*:\s*([0-9]*\.?[0-9]+)\s*(px|pt|em|rem|%)?", re.I)
# Unit-aware, because "zero or near-zero" is a different number in each unit
# and a flat threshold would either miss `0.05em` or flag legible `10pt`.
_TINY_FONT_LIMITS = {"px": 1.0, "pt": 1.0, "em": 0.1, "rem": 0.1, "%": 10.0}

# A tiny font-size on its own is NOT concealment, and treating it as such was
# the worst false positive found when this was measured against live mail:
# `font-size:0px` on a container <td> is standard table-layout practice (MJML
# emits it on every wrapper cell to kill inline-block whitespace) and the
# children set their own size, so the text renders normally. Believing it
# dropped 100% of the body of every Amazon and Sweetgreen message.
#
# Modelling that properly means modelling inheritance -- font-size hides an
# element's own text nodes but not a descendant's that overrides it. Instead we
# require a corroborating concealment property in the same declaration, which
# is what real preheaders carry (Amazon's is `display:none;font-size:1px;
# color:#ffffff;line-height:1px;max-height:0px;opacity:0;overflow:hidden`) and
# what a layout wrapper does not.
_TINY_FONT_CORROBORATION = re.compile(
    r"max-height\s*:\s*0|max-width\s*:\s*0|height\s*:\s*0"
    r"|line-height\s*:\s*0|overflow\s*:\s*hidden"
    r"|color\s*:\s*(?:#f{3}\b|#f{6}\b|white|transparent)",
    re.I,
)
_SELECTOR_TOKEN_RE = re.compile(r"[.#][A-Za-z0-9_-]+")
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
# A real hiding rule is tiny (`.preheader{display:none}`). Anything sprawling
# is a sign the brace walk lost sync on malformed CSS, so it is not trusted to
# hide content.
_MAX_SELECTOR_CHARS = 120
_MAX_SELECTOR_TOKENS = 4


def _top_level_rules(css: str):
    """Yield (selectors, declarations) for rules at brace depth 0 only.

    Depth is the whole point: `@media (max-width:480px) { .m-hide {
    display:none } }` is RESPONSIVE design, not concealment -- that content is
    visible on desktop and hiding it would delete real text. Measured before
    this guard existed, treating nested rules as hiding dropped 100% of the
    body of every Amazon and Sweetgreen message in the mailbox. Any at-rule
    block (@media, @supports) therefore contributes nothing.

    Comments are stripped first: a stray `{` inside `/* ... */` shifts the
    depth permanently, after which real top-level rules look nested and a
    nested one's closing brace ends a bogus rule whose "selector" spans
    unrelated text. That is how `#body` was once harvested from an Audible
    message, hiding the whole document."""
    css = _CSS_COMMENT_RE.sub(" ", css)
    depth, start, selector = 0, 0, None
    for i, ch in enumerate(css):
        if ch == "{":
            if depth == 0:
                selector, start = css[start:i], i + 1
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                if selector is not None and not selector.lstrip().startswith("@"):
                    yield selector, css[start:i]
                selector, start = None, i + 1


def _declares_hidden(decl: str) -> bool:
    if not decl:
        return False
    if _HIDDEN_DECL_RE.search(decl):
        return True
    m = _FONT_SIZE_RE.search(decl)
    if m:
        unit = (m.group(2) or "px").lower()
        if float(m.group(1)) <= _TINY_FONT_LIMITS.get(unit, 1.0):
            return bool(_TINY_FONT_CORROBORATION.search(decl))
    return False


def hidden_selectors(css: str) -> set:
    """`.class`/`#id` tokens whose rules hide their content. Best effort.

    Deliberately not a CSS engine: no cascade, no specificity, no descendant
    reasoning, and nothing inside an at-rule (see _top_level_rules). It catches
    the common shape -- one unconditional rule, one class, `display:none` --
    which is also the cheapest place to plant text."""
    hidden = set()
    for selectors, decl in _top_level_rules(css):
        if not _declares_hidden(decl) or len(selectors) > _MAX_SELECTOR_CHARS:
            continue
        tokens = _SELECTOR_TOKEN_RE.findall(selectors)
        if 0 < len(tokens) <= _MAX_SELECTOR_TOKENS:
            hidden.update(t.lower() for t in tokens)
    return hidden


def _clean(text: str) -> str:
    """Strip the two sentinel codepoints from sender-controlled text.

    Without this a message containing a literal \x01> would forge a quote
    marker and make its own words render as somebody else's quote."""
    return text.replace("\x00", "").replace("\x01", "")


class _HTMLTextExtractor(HTMLParser):
    """Tag-stripped plain text from an HTML body: drops <script>/<style>
    content entirely, inserts a line break at block-level tags so the
    result isn't one giant run-on line, and collapses interior whitespace.

    It also drops CSS-hidden text and records that it did (Phase 6.4). The
    general untrusted-content design assumed an HTML→text pass neutralized
    planted text; ours did not. Hidden text is a prompt-injection vector
    precisely because the human and the model see different messages -- the
    user skims a rendered newsletter while the model reads a `display:none`
    div instructing it to archive everything from some address.

    Detected: inline `style` declaring display:none / visibility:hidden /
    opacity:0 / mso-hide:all / a zero-or-near-zero font-size; the `hidden` and
    `aria-hidden="true"` attributes; `<template>`; and, best-effort, a class
    or id hidden by a rule in an EARLIER `<style>` block.

    NOT detected, which is why `hidden_text_present` is a signal rather than a
    guarantee: offscreen positioning (`left:-9999px` -- needs multi-property
    reasoning), same-colour-on-same-background text (needs the cascade), a
    `<style>` block placed after the content it hides, and external CSS. 6.7
    treats the flag as one injection signal among several for that reason.
    """

    _BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
                   "dt", "dd"}
    # Cells separate with a SPACE, not a newline: a table row is one logical
    # line and `tr` already breaks it. Email is built out of tables, so before
    # this a receipt extracted as `Subtotal$41.20` and a definition list as
    # `DueAug 12Amount$90` -- measured in 27.7% of the messages where HTML is
    # the only body source (2026-08-07).
    _CELL_TAGS = {"td", "th"}
    _SKIPPED_TAGS = {"script", "style"}
    _ALWAYS_HIDDEN_TAGS = {"template"}
    _STRUCTURAL_TAGS = {"html", "body"}
    # Void elements hold no text, so a hidden one has nothing to suppress --
    # and pushing one would leave it on the stack forever, silently swallowing
    # the rest of the document.
    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                  "link", "meta", "source", "track", "wbr"}

    # Structure that cannot be expressed inline is carried as sentinel markers
    # in the chunk stream and resolved in text(). \x00/\x01 are stripped from
    # ALL sender data on the way in (_clean), so a sender cannot forge one --
    # the same unforgeability requirement as keeping link targets out of the
    # body text. Quote markers deliberately render as the plain-text `> `
    # convention, so quote isolation gets ONE code path for HTML and plain.
    _Q_OPEN, _Q_CLOSE, _PRE_OPEN, _PRE_CLOSE = "\x01>", "\x01<", "\x01[", "\x01]"
    # A CITE container is a client's own "this is the message I am replying to"
    # wrapper -- Apple Mail's `type="cite"`, Gmail's `gmail_quote` class. It
    # renders identically to a bare blockquote; the distinction exists only so
    # quote isolation can tell tier 2 (structurally marked, strip) from tier 3
    # (an unattributed quotation the author chose to include, KEEP).
    _QC_OPEN = "\x01{"

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks = []
        self._links = []
        self._skip_depth = 0
        self._in_style = False
        self._css = []
        self._hidden_selectors = set()
        self._hidden_stack = []
        self._dropped = []
        self.hidden_text_present = False

    def _is_hidden(self, tag, attrs) -> bool:
        if tag in self._ALWAYS_HIDDEN_TAGS:
            return True
        attrs = {k.lower(): (v or "") for k, v in attrs}
        if "hidden" in attrs or attrs.get("aria-hidden", "").strip().lower() == "true":
            return True
        if _declares_hidden(attrs.get("style", "")):
            return True
        # Selector matching never applies to the document root. Hiding <body>
        # would suppress everything, and a message with nothing visible is not
        # a human/model asymmetry -- it is a bug in our own CSS reading. An
        # Audible message did exactly this via a harvested `#body`.
        if self._hidden_selectors and tag not in self._STRUCTURAL_TAGS:
            tokens = {f".{c.lower()}" for c in attrs.get("class", "").split()}
            if attrs.get("id"):
                tokens.add("#" + attrs["id"].lower())
            if tokens & self._hidden_selectors:
                return True
        return False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIPPED_TAGS:
            self._skip_depth += 1
            if tag == "style":
                self._in_style = True
            return
        if tag in self._VOID_TAGS:
            # No text content to hide and no end tag to balance.
            if self._hidden_stack or self._is_hidden(tag, attrs):
                return
            if tag in self._BLOCK_TAGS:
                self._chunks.append("\n")
            elif tag == "img":
                # alt is real content in image-heavy marketing and receipt mail
                # (~27 chars/msg was being dropped). Empty alt is decorative by
                # spec -- exactly what spacer gifs carry -- so it stays dropped.
                alt = _clean({k.lower(): (v or "") for k, v in attrs}
                             .get("alt", "")).strip()
                if alt:
                    self._chunks.append(f" {alt} ")
            return
        if self._hidden_stack or self._is_hidden(tag, attrs):
            # Inside a hidden subtree, EVERY open element is tracked -- an
            # untracked inner <div> would have its </div> pop the hidden
            # wrapper instead, un-hiding the rest of it.
            self._hidden_stack.append(tag)
            return
        if tag == "a":
            href = {k.lower(): (v or "") for k, v in attrs}.get("href", "").strip()
            if href and not href.lower().startswith("javascript:"):
                self._links.append(_clean(href))
        if tag in self._BLOCK_TAGS:
            self._chunks.append("\n")
        elif tag in self._CELL_TAGS:
            self._chunks.append(" ")
        elif tag == "blockquote":
            a = {k.lower(): (v or "") for k, v in attrs}
            cite = (a.get("type", "").strip().lower() == "cite"
                    or "gmail_quote" in a.get("class", "").lower())
            self._chunks.append("\n" + (self._QC_OPEN if cite else self._Q_OPEN))
        elif tag == "pre":
            self._chunks.append("\n" + self._PRE_OPEN)
        if tag == "li":
            self._chunks.append("- ")

    def handle_endtag(self, tag):
        if tag in self._SKIPPED_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            if tag == "style":
                self._in_style = False
                self._hidden_selectors |= hidden_selectors("".join(self._css))
                self._css = []
            return
        if self._hidden_stack:
            # Pop back through the LAST matching open tag, tolerating unclosed
            # inner elements: email HTML is routinely malformed, and a strict
            # match would leave the whole remainder suppressed. Nothing is
            # emitted -- we are still inside hidden content.
            for i in range(len(self._hidden_stack) - 1, -1, -1):
                if self._hidden_stack[i] == tag:
                    del self._hidden_stack[i:]
                    break
            return
        if tag == "blockquote":
            self._chunks.append(self._Q_CLOSE + "\n")
        elif tag == "pre":
            self._chunks.append(self._PRE_CLOSE + "\n")

    def handle_data(self, data):
        if self._in_style:
            self._css.append(data)
            return
        if self._skip_depth:
            return
        if self._hidden_stack:
            # Only non-whitespace counts: indentation inside a hidden wrapper
            # is not planted text, and flagging it would make the signal
            # useless by firing on almost every HTML message.
            if data.strip():
                self.hidden_text_present = True
                self._dropped.append(data.strip())
            return
        self._chunks.append(_clean(data))

    def text(self) -> str:
        """Assemble the chunk stream, resolving quote/pre markers per line.

        One linear pass. A marker anywhere in a line applies to the whole line,
        which is why the emitters put a newline next to each one."""
        out, kinds, pre = [], [], 0
        self._cite_lines = set()
        for line in "".join(self._chunks).split("\n"):
            seg, i = [], 0
            # State is captured at the line's FIRST content character, not at
            # its end. A closing marker sits at end-of-line, so reading the
            # depth afterwards had already un-quoted the line it applied to --
            # which silently dropped the `> ` from every single-line quote and
            # re-collapsed the last line of every <pre>.
            at_kinds = at_pre = None
            while i < len(line):
                if line[i] == "\x01" and i + 1 < len(line):
                    m = line[i + 1]
                    if m == ">":
                        kinds.append("plain")
                    elif m == "{":
                        kinds.append("cite")
                    elif m == "<":
                        if kinds:
                            kinds.pop()
                    elif m == "[":
                        pre += 1
                    elif m == "]":
                        pre = max(0, pre - 1)
                    i += 2
                    continue
                if at_kinds is None:
                    at_kinds, at_pre = tuple(kinds), pre
                seg.append(line[i])
                i += 1
            body = "".join(seg)
            if at_kinds is None:        # marker-only or empty line
                at_kinds, at_pre = tuple(kinds), pre
            prefix = "> " * len(at_kinds)
            if at_pre:
                # Interior runs are the content in <pre> -- collapsing them is
                # what turned `col1  col2` into `col1 col2`. Blank lines are
                # kept here and only here.
                if "cite" in at_kinds:
                    self._cite_lines.add(len(out))
                out.append((prefix + body.rstrip()).rstrip() if not body.strip()
                           else prefix + body.rstrip())
                continue
            body = re.sub(r"[ \t]+", " ", body).strip()
            if body:
                if "cite" in at_kinds:
                    self._cite_lines.add(len(out))
                out.append(prefix + body)
        return "\n".join(out)

    def cite_lines(self) -> frozenset:
        """0-based indices of output lines that came from a client cite
        container. Only valid after text(); text() is what numbers the lines."""
        return frozenset(getattr(self, "_cite_lines", ()))

    def links(self) -> list:
        """Visible link targets in document order, deduped.

        Visible-only, matching the body: a target the user could never click is
        not part of what they read. (Links inside hidden subtrees would be the
        interesting input to a display/target mismatch check -- a separate
        concern, deliberately not folded in here.)"""
        seen, ordered = set(), []
        for url in self._links:
            if url not in seen:
                seen.add(url)
                ordered.append(url)
        return ordered

    def hidden_report(self) -> dict:
        """What was dropped, as a classification rather than as content.

        The dropped text stays in Python and NEVER reaches a model context:
        it was hidden from the user, so handing it to the model would undo the
        containment we just achieved. What crosses the boundary is our own
        judgement about it.

        `present` is a diagnostic, not a signal. Measured over the 400 most
        recent messages in the live mailbox (2026-08-06): 224 had an HTML part
        and 183 of those -- 82% -- hid text, essentially all of it legitimate
        preheader copy padded with U+034F runs. A trigger with an 82% base rate
        is an alarm that gets ignored, so 6.7 keys off `suspicious`, which
        requires injection-SHAPED content and not merely concealed content.
        """
        reasons = untrusted.injection_signals(" ".join(self._dropped))
        return {"present": self.hidden_text_present,
                "suspicious": bool(reasons),
                "reasons": reasons}


NO_HIDDEN_TEXT = {"present": False, "suspicious": False, "reasons": []}


# Link targets are capped for the same reason `--suggest-events` buckets are:
# a marketing message carries dozens, and an uncapped list is how output volume
# gets away from you. The count of what was dropped still travels.
LINK_CAP = 20


class Stripped(NamedTuple):
    """Named rather than a bare tuple: this return grew from 1 value to 4 across
    6.4, 11.8 and 11.2, and each widening silently broke every unpack site."""
    text: str
    hidden: dict
    links: list
    cite_lines: frozenset


def strip_html(html_text: str) -> "Stripped":
    parser = _HTMLTextExtractor()
    parser.feed(html_text)
    parser.close()
    text = parser.text()          # numbers the lines cite_lines refers to
    return Stripped(text, parser.hidden_report(), parser.links(),
                    parser.cite_lines())


# --------------------------------------------------------------------------
# Quote isolation (11.2): tell a message's own words from what it quotes
# --------------------------------------------------------------------------

# Tier 2 attribution lines. Two shapes, and they behave differently:
#  - INLINE ("On <date>, X wrote:") introduces a `>`-quoted run that follows it.
#  - HARD CUT ("Begin forwarded message:", Outlook's "-----Original Message-----")
#    introduces quoted material that is NOT `>`-prefixed -- headers and body sit
#    at the left margin, so everything from the marker to the end is chain.
_INLINE_ATTRIBUTION = re.compile(r"\bwrote:\s*$", re.I)
# The strict client-generated form: "On <date>, X wrote:". Required before an
# attribution is allowed to swallow UNINDENTED text to end-of-message, because
# that is the one removal that could eat the author's own words. A human writing
# "As Pete wrote:" mid-paragraph does not start the line with "On".
_CLIENT_ATTRIBUTION = re.compile(r"^\s*(?:>\s?)*on\b.{4,300}\bwrote:\s*$", re.I)
_HARD_CUT = re.compile(
    r"^\s*(?:-{2,}\s*original\s+message\s*-{2,}"
    r"|-{2,}\s*forwarded\s+message\s*-{2,}"
    r"|begin\s+forwarded\s+message:)\s*$", re.I)
_QUOTE_PREFIX = re.compile(r"^(?:>\s?)+")
# Below this, a quoted run is too short for an ancestor match to mean anything --
# "Thanks" appears in every thread. Unmatched means KEPT, so the floor errs
# toward tier 3.
_MIN_MATCH_WORDS = 5
ELISION = "[quoted text removed]"


def _is_attribution(line: str) -> bool:
    """Attribution introducing an already-`>`-quoted run. Looser than the strict
    form is safe here: the text it marks is quoted by the client either way."""
    return bool(_CLIENT_ATTRIBUTION.match(line)
                or (_INLINE_ATTRIBUTION.search(line)
                    and any(ch.isdigit() for ch in line)))


def _norm_for_match(text: str) -> str:
    """Punctuation- and wrap-agnostic form for comparing a quote to an ancestor.

    Quoting clients re-wrap, re-indent and re-encode, so a literal comparison
    fails on text that is plainly the same words."""
    text = _QUOTE_PREFIX.sub("", text or "", count=0)
    text = re.sub(r"^(?:>\s?)+", "", text or "", flags=re.M)
    return re.sub(r"\W+", " ", text).strip().lower()


def _matches_an_ancestor(run_text: str, ancestor_norms) -> bool:
    """True when this run is a copy of an indexed ancestor's body.

    Tier 1, and the only tier that is ground truth rather than a heuristic --
    available only because the whole thread is held locally."""
    norm = _norm_for_match(run_text)
    if len(norm.split()) < _MIN_MATCH_WORDS:
        return False
    return any(norm and norm in anc for anc in ancestor_norms)


def isolate_quotes(text: str, ancestor_bodies=(), cite_lines=frozenset()):
    """Return (kept_text, removals) where removals is a list of
    {basis, chars} — see the three tiers in implementation-plan.md 11.2.

    Polarity is the whole design: tier 3 (an unattributed quotation) is KEPT,
    because losing words the author deliberately included is a worse failure
    than leaving a stray quote in place."""
    if not text:
        return text, []
    lines = text.split("\n")
    ancestor_norms = [n for n in (_norm_for_match(b) for b in ancestor_bodies) if n]
    drop = [False] * len(lines)
    bases = {}

    # Pass 1: hard cuts swallow everything after them. Two kinds --
    #  - the dashed markers, which are unambiguous; and
    #  - "On <date>, X wrote:" when what FOLLOWS is not `>`-prefixed. Measured
    #    on 240 real replies: 30 put the chain at the left margin like this, so
    #    keying only off a following `>` run missed every one of them. The strict
    #    _CLIENT_ATTRIBUTION form is required here for exactly the reason the
    #    looser one is fine in pass 2 -- nothing else marks where this ends.
    for i, line in enumerate(lines):
        cut = _HARD_CUT.match(line)
        if not cut and _CLIENT_ATTRIBUTION.match(line):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            cut = j < len(lines) and not _QUOTE_PREFIX.match(lines[j])
        if cut:
            for j in range(i, len(lines)):
                drop[j] = True
                bases.setdefault(j, "attribution-line")
            break

    # Pass 2: `>`-prefixed runs, each classified independently.
    i = 0
    while i < len(lines):
        if drop[i] or not _QUOTE_PREFIX.match(lines[i]):
            i += 1
            continue
        start = i
        while i < len(lines) and (_QUOTE_PREFIX.match(lines[i]) or not lines[i].strip()):
            i += 1
        end = i                                    # exclusive
        while end > start and not lines[end - 1].strip():
            end -= 1                               # don't swallow trailing blanks
        run = "\n".join(lines[start:end])

        # An attribution line immediately above (skipping blanks) marks the run.
        attributed_at = None
        k = start - 1
        while k >= 0 and not lines[k].strip():
            k -= 1
        if k >= 0 and _is_attribution(lines[k]):
            attributed_at = k
        # ...or INSIDE it. In a nested reply the attribution is itself quoted
        # (`> On Mon, Pete wrote:`), so it becomes the run's own first line and
        # a look-back finds nothing. 40 of 44 missed detections on real mail
        # were this shape. Only the run's opening lines count -- an attribution
        # deep inside is a quoted quote, already covered by the enclosing run.
        marked_inside = any(
            _is_attribution(_QUOTE_PREFIX.sub("", lines[n]).strip())
            for n in range(start, min(start + 3, end)))

        if _matches_an_ancestor(run, ancestor_norms):
            basis = "matched-ancestor"
        elif attributed_at is not None or marked_inside:
            basis = "attribution-line"
        elif any(n in cite_lines for n in range(start, end)):
            basis = "client-container"
        else:
            basis = None                           # tier 3 -- KEEP

        if basis:
            if attributed_at is not None:
                drop[attributed_at] = True
                bases.setdefault(attributed_at, basis)
            for j in range(start, end):
                drop[j] = True
                bases.setdefault(j, basis)

    # Assemble: keep order, and label a gap when kept text FOLLOWS it, so the
    # following text does not read as the start of the message. A gap is marked
    # on what comes after it, not before -- marking on "something preceded it"
    # put an elision at the end of every ordinary reply-above-quote message,
    # where the report already says everything useful.
    kept, removals, run_basis, run_chars = [], [], None, 0

    def flush():
        nonlocal run_basis, run_chars
        if run_basis is None:
            return
        removals.append({"basis": run_basis, "chars": run_chars})
        kept.append(ELISION)
        run_basis, run_chars = None, 0

    for i, line in enumerate(lines):
        if drop[i]:
            if run_basis is not None and bases[i] != run_basis:
                flush()
            run_basis = bases[i]
            run_chars += len(line) + 1
        else:
            flush()
            kept.append(line)
    flush()
    # Trailing: counted, not marked. Blank lines are popped alongside, because a
    # body ending in a newline leaves an empty line AFTER the elision, which was
    # enough to stop the marker being recognised as trailing at all.
    while kept and (kept[-1] == ELISION or not kept[-1].strip()):
        kept.pop()

    out = "\n".join(kept).strip("\n")
    return out, removals


class Body(NamedTuple):
    text: str
    source_part: str
    hidden: dict
    links: list
    cite_lines: frozenset


def ancestor_bodies(conn, maildir_root, account: str, message_id: str) -> list:
    """Bodies of every INDEXED ancestor of this message, for tier-1 matching.

    All ancestors, not just the `In-Reply-To` parent: people quote earlier
    messages in a thread, not only the one they are answering. Unreadable or
    unindexed ancestors are skipped rather than raised -- a missing ancestor
    means tier 1 cannot fire, which degrades to KEEP, the safe direction."""
    row = resolve_message_row(conn, account, message_id)
    if row is None:
        return []
    members = load_account_members(conn, account)
    me = members.get(message_id)
    if me is None:
        return []
    wanted = [r for r in me["references_chain"] if r != message_id]
    if me["in_reply_to"] and me["in_reply_to"] not in wanted:
        wanted.append(me["in_reply_to"])
    bodies = []
    for mid in wanted:
        if mid not in members:
            continue
        try:
            path = resolve_message_file(maildir_root, account, mid, conn)
            body = extract_body(load_raw_message(path), force_html=False).text
        except Exception:
            continue                      # see docstring: skip, never raise
        if body:
            bodies.append(body)
    return bodies


def extract_body(msg, force_html: bool) -> "Body":
    """Returns (body, source_part, hidden_report). Default is the text/plain
    part; falls back to tag-stripped text/html when there is no text/plain
    part OR --html-text was given -- design review §4's two trigger
    conditions ("when no text/plain, or flag given") are ORed, not
    exclusive alternatives, so a plain-text message is unaffected by
    --html-text. If --html-text is given but the message has no text/html
    part at all, falls back to text/plain rather than returning nothing
    (an ambiguity the spec didn't cover; returning no body when a
    perfectly readable plain part exists would be a worse default)."""
    plain_part = find_part(msg, "plain")
    plain_text = decode_text_part(plain_part) if plain_part is not None else None

    # An EMPTY text/plain part must not win (found live 2026-08-07, reviewing a
    # real draft). Apple Mail writes multipart/alternative drafts with a BLANK
    # text/plain beside the genuine text/html, so preferring text/plain whenever
    # it merely EXISTS reported a fully-written draft as empty -- and a session
    # following the documented read path would have told the user their draft
    # was blank, then offered to rewrite it over the top of real content.
    # No link list for text/plain: a plain-text body already shows its URLs
    # inline, so there is nothing to recover and nothing to separate.
    if plain_text is not None and plain_text.strip() and not force_html:
        return Body(plain_text, "text/plain", NO_HIDDEN_TEXT, [], frozenset())

    html_part = find_part(msg, "html")
    if html_part is not None:
        st = strip_html(decode_text_part(html_part))
        return Body(st.text, "text/html-stripped", st.hidden, st.links,
                    st.cite_lines)

    # Blank plain and no html at all: the message really is empty. Return it as
    # text/plain rather than None so the caller can tell "empty" from "absent".
    if plain_text is not None:
        return Body(plain_text, "text/plain", NO_HIDDEN_TEXT, [], frozenset())

    return Body(None, None, NO_HIDDEN_TEXT, [], frozenset())


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


def _attach_injection_signals(obj: dict) -> None:
    """Flag injection-shaped SUBJECTS at scan time (Phase 6.7).

    The priority scan runs over `list --compact`, which carries no body -- so
    the subject is the only prose available before anyone reads a message, and
    catching it here means the model is warned BEFORE it opens the thing.

    Set only when non-empty: an empty array on every one of 200 threads is
    noise, and a field that is almost always `[]` stops being read. Measured
    over the live index (2026-08-06): 0 of 16,981 subjects and 0 of 17,061
    from_addr values match, so a hit here is genuinely unusual rather than
    routine -- unlike `hidden_text_present`, which fires on 78% of HTML mail.
    """
    signals = untrusted.injection_signals(obj.get("subject") or "")
    if signals:
        obj["injection_signals"] = signals


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
    _attach_injection_signals(thread_obj)
    return thread_obj, (newest.get("date_utc") or "")


# --------------------------------------------------------------------------
# Trust classification (Phase 6.2; untrusted-content-skill-design.md §5)
# --------------------------------------------------------------------------
# The authored artifact is the TRUSTED allowlist; `untrusted.apply_trust`
# derives `_trust.untrusted_fields` by subtraction, so a field added below
# without a matching allowlist entry goes out escaped and declared rather
# than bare and silent. The test is "can this value contain sender bytes,"
# NOT "did our code emit this field" -- `root_message_id` is computed by
# `thread_root_id` and is still untrusted, because a sender chose the
# Message-ID it selects.
#
# Paths are absolute and never matched by leaf name; `untrusted.under()`
# composes the same shape at the several paths it appears at.

# Members: folder/folder_tier come from the Maildir layout, flags from the
# filename suffix, date_utc from our own strftime (parse_date_utc yields
# None rather than passing a bad Date header through). message_id is a
# HANDLE -- sender-valued, but the model feeds it back to `show`/`act`/
# `batch`, so it is declared untrusted and deliberately not escaped.
MEMBER_TRUSTED = {"folder", "folder_tier", "date_utc", "flags"}
# injection_signals holds OUR labels, never sender text (6.7) -- the detector
# returns pipeline-authored strings and never the matched span, precisely so
# that reporting a signal cannot smuggle the sender's words along with it.
SIGNAL_FIELDS = {"injection_signals"}
MEMBER_HANDLES = {"message_id"}

THREAD_TRUSTED = ({"account", "state"} | SIGNAL_FIELDS
                  | untrusted.under("members", MEMBER_TRUSTED))
THREAD_HANDLES = {"root_message_id"} | untrusted.under("members", MEMBER_HANDLES)

# --compact swaps the members array for a single `latest` object and adds
# tiers_present (pipeline tier names, not sender text).
COMPACT_TRUSTED = ({"account", "state", "tiers_present"} | SIGNAL_FIELDS
                   | untrusted.under("latest", MEMBER_TRUSTED))
COMPACT_HANDLES = {"root_message_id"} | untrusted.under("latest", MEMBER_HANDLES)

# `list` nests either shape under `threads`; `show` emits one bare.
LIST_TRUSTED = (untrusted.under("threads", THREAD_TRUSTED)
                | untrusted.under("threads", COMPACT_TRUSTED))
LIST_HANDLES = (untrusted.under("threads", THREAD_HANDLES)
                | untrusted.under("threads", COMPACT_HANDLES))

RESIDUE_TRUSTED = untrusted.under(
    "residue", {"account", "state", "date_utc", "folders"})
RESIDUE_HANDLES = untrusted.under("residue", {"root_message_id"})

# flag's actions are entirely our own bookkeeping; only the message_id it
# echoes back came from a sender.
FLAG_TRUSTED = {"account", "mode"} | untrusted.under(
    "actions", {"folder", "himalaya_id", "op"})
FLAG_HANDLES = untrusted.under("actions", {"message_id"})

# read: NO `headers.*` path is ever allowlisted. Under --headers-only the key
# space itself is sender-chosen (full_message_headers builds its dict from
# msg.keys()), and even the curated SELECTED_HEADERS are all sender-written.
# The two message-id headers are handles: the reply flow copies them into a
# References chain, and `&lt;id@host&gt;` is a corrupt References value.
# `source_part` is our own content-type label; `file` is the Maildir filename,
# generated by mbsync rather than by any sender.
# hidden_text_reasons is a list of OUR OWN labels, not sender text -- the one
# place in this script where a string array is genuinely pipeline-generated.
# `quoted_removed.basis` is our own three-value vocabulary, not sender text.
READ_TRUSTED = ({"source_part", "file", "hidden_text_reasons",
                 "quoted_removed.basis"} | SIGNAL_FIELDS)
# `links.url` is sender-chosen and must stay unescaped to remain a usable key,
# which is exactly what a handle is. `links.n` and `links_truncated` are ints,
# and the manifest only classifies strings, so they need no entry.
READ_HANDLES = {"headers.Message-ID", "headers.In-Reply-To", "links.url"}
# The one full nonce envelope in this script: the body is long free text with
# room for an injection to work, and it is where recency-bias framing earns
# its keep (UC §5a, invariant 2).
READ_ENVELOPES = {"body": "email body"}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


SUMMARY_TRUSTED = {"totals", "by_account", "by_state", "by_tier", "top_senders",
                   "signals", "tiers"}

# `--scan`: one line per thread carrying ONLY what the priority scan judges on,
# plus the message_id needed to act. Added 2026-08-07: the scan is judgement over
# subjects, and there was no way to see subjects at mailbox scale -- `--compact`
# emits the full object (member counts, tiers_present, folders, root ids) and
# overflowed at ~140 threads, so the scan could not actually be performed.
SCAN_TRUSTED = {"scan.account", "scan.state", "scan.seen", "scan.inactive_days",
                "scan.drafted", "scan.injection_signals", "scan.folder_tier"}
SCAN_HANDLES = {"scan.message_id"}


def scan_line(obj: dict) -> dict:
    """The scan's judgement fields. Deliberately omits everything the scan does
    not read -- that omission is the point, not an oversight."""
    latest = obj.get("latest") or {}
    line = {
        "account": obj["account"],
        "message_id": latest.get("message_id"),
        "from_addr": latest.get("from_addr"),
        "subject": obj.get("subject"),
        "inactive_days": obj.get("inactive_days"),
        # `seen` is the USER's read state, protected by rule 5 -- our own reads
        # never set it. True means they have actually had this in front of them
        # (or been shown a summary of it), which is what makes it a legitimate
        # novelty filter and NOT a reason to drop a deadline.
        "seen": "Seen" in (latest.get("flags") or []),
        "state": obj.get("state"),
        "folder_tier": latest.get("folder_tier"),
    }
    if obj.get("drafted"):
        line["drafted"] = True
    if obj.get("injection_signals"):
        line["injection_signals"] = obj["injection_signals"]
    return line


def summarize(objs: list) -> dict:
    """Counts instead of thread objects (added 2026-08-07 after the first live
    session: `--compact` over a busy mailbox still emitted 105KB / ~141 threads,
    and the SKILL prescribes --compact as the remedy for oversized output, so the
    remedy was what overflowed).

    Carries exactly what the breakdown step needs to plan the session and
    nothing per-thread: totals, the lifecycle split, the priority-scan scope
    (unseen), drafted and reconciliation counts, and the heaviest senders --
    which is what turns a 141-thread inbox into a handful of category batches
    worked through `--sender`."""
    by_account: dict = {}
    by_state: dict = {}
    by_tier: dict = {}
    senders: dict = {}
    unseen = drafted = reconcile = signals = 0
    for obj in objs:
        by_account[obj["account"]] = by_account.get(obj["account"], 0) + 1
        by_state[obj["state"]] = by_state.get(obj["state"], 0) + 1
        latest = obj.get("latest") or {}
        tier = latest.get("folder_tier")
        if tier:
            by_tier[tier] = by_tier.get(tier, 0) + 1
        if "Seen" not in (latest.get("flags") or []):
            unseen += 1
        if obj.get("drafted"):
            drafted += 1
        present = set(obj.get("tiers_present") or [])
        if "untriaged" in present and present & {"ongoing", "cold"}:
            reconcile += 1
        if obj.get("injection_signals"):
            signals += 1
        addr = (latest.get("from_addr") or "").lower()
        if addr:
            senders[addr] = senders.get(addr, 0) + 1
    top = sorted(senders.items(), key=lambda kv: (-kv[1], kv[0]))[:15]
    return {
        "totals": {"threads": len(objs), "unseen": unseen, "drafted": drafted,
                   "reconciliation_candidates": reconcile,
                   "injection_signals": signals},
        "by_account": by_account,
        "by_state": by_state,
        "by_tier": by_tier,
        # sender is a HANDLE: the model feeds it straight back into
        # `list --sender <addr>` to build each category batch.
        "top_senders": [{"sender": a, "threads": n} for a, n in top],
    }


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
                if args.compact or args.summary or args.scan:
                    # --summary derives from the compact shape deliberately, so
                    # `latest` / `tiers_present` / the priority-scan unseen rule
                    # come from ONE implementation rather than a second copy.
                    thread_obj, sort_date = compact_thread_object(account, mlist)
                else:
                    thread_obj, sort_date = build_thread_object(account, mlist)
                if args.unseen:
                    latest = thread_obj.get("latest") or {}
                    if "Seen" in (latest.get("flags") or []):
                        continue
                result.append((thread_obj, sort_date))

        # (account asc, newest date desc): stable-sort by date desc first,
        # then by account asc, so the date ordering survives within account.
        result.sort(key=lambda pair: pair[1], reverse=True)
        result.sort(key=lambda pair: pair[0]["account"])

        objs = [t for t, _ in result]
        if args.scan:
            # One entry per LINE, not indent=2. Every other emitter here
            # pretty-prints, but this one is a dense listing whose whole purpose
            # is fitting a whole mailbox in a readable budget -- indent=2 spent
            # ~60% of the payload on padding around six short fields.
            payload = untrusted.apply_trust(
                {"scan": [scan_line(o) for o in objs]},
                SCAN_TRUSTED, handle_fields=SCAN_HANDLES)
            lines = ",\n    ".join(
                json.dumps(e, sort_keys=True, separators=(",", ":"))
                for e in payload["scan"])
            print('{\n  "_trust": %s,\n  "scan": [\n    %s\n  ]\n}' % (
                json.dumps(payload["_trust"], sort_keys=True), lines))
            return
        if args.summary:
            print(json.dumps(
                untrusted.apply_trust(
                    summarize(objs), SUMMARY_TRUSTED,
                    handle_fields={"top_senders.sender"}),
                indent=2, sort_keys=True))
            return
        # An object, not the bare array this printed before 6.2: an array has
        # nowhere to carry the `_trust` manifest.
        print(json.dumps(
            untrusted.apply_trust({"threads": objs},
                                  LIST_TRUSTED, handle_fields=LIST_HANDLES),
            indent=2, sort_keys=True))
    finally:
        conn.close()


def compact_thread_object(account: str, mlist: list):
    """Classification-sized thread summary (5.11): everything the triage
    breakdown, priority scan, and reconciliation check need, WITHOUT the
    full members array. A full `list` over a busy mailbox overflows the
    tool-result budget and the dump gets poked at with ad-hoc shell — the
    exact anti-pattern the member dump was already known for. Full member
    lists come from `show` (or non-compact `list`), needed only when
    acting on a multi-message thread or building reply References."""
    state, inactive_days = compute_state(mlist)
    # Priority-scan scope: the chronologically latest non-trash/junk
    # member (same rule the SKILL states); its flags carry Seen.
    eligible = [m for m in mlist if m["tier"] not in HISTORY_TIERS]
    newest = latest_dated(eligible) or latest_dated(mlist)
    if newest is None:
        newest = sorted(mlist, key=lambda m: m["message_id"])[0]
    obj = {
        "account": account,
        "root_message_id": thread_root_id(mlist),
        "state": state,
        "drafted": compute_drafted(mlist),
        "inactive_days": inactive_days,
        "member_count": len(mlist),
        "subject": normalize_subject(newest["subject"]),
        # spanning untriaged + ongoing/cold here IS the reconciliation
        # signal; trash/junk presence answers the never-count-as-resolved
        # rule without a second query.
        "tiers_present": sorted({m["tier"] for m in mlist}),
        "latest": {
            "message_id": newest["message_id"],
            "folder": newest["folder"],
            "folder_tier": newest["tier"],
            "date_utc": newest["date_utc"],
            "from_addr": newest["from_addr"],
            "flags": newest["flags"],
        },
    }
    _attach_injection_signals(obj)
    return obj, (newest.get("date_utc") or "")


# --------------------------------------------------------------------------
# flag: mechanized himalaya-local flag ops (session-review R2, 2026-07-19).
# The id-resolution dance (resolve FRESH, match by from_addr + date -- ids
# are an unstable id-mapper; envelope JSON has NO message_id field) was the
# last himalaya interaction done by hand, and it misfired in a live session
# (an ad-hoc filter on the nonexistent message_id field). Here it's code.
# --------------------------------------------------------------------------


def _parse_himalaya_date(s: str):
    """himalaya envelope date: '2026-07-19 17:40+00:00' (no seconds)."""
    try:
        return datetime.datetime.strptime(s.strip(), "%Y-%m-%d %H:%M%z")
    except (ValueError, AttributeError):
        return None


def _parse_index_date(s: str):
    try:
        return datetime.datetime.strptime(s.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except (ValueError, AttributeError):
        return None


def _himalaya(himalaya_bin, *cli_args) -> str:
    import subprocess
    proc = subprocess.run([himalaya_bin, *cli_args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise FatalError(f"himalaya {' '.join(cli_args[:3])}... failed: "
                         f"{(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.stdout


def resolve_himalaya_id(himalaya_bin: str, hima_account: str, member: dict) -> str:
    """Current himalaya-local id for one member: envelope list in the
    member's folder, matched by from_addr + date (minute precision -- the
    envelope JSON carries no seconds and NO message_id field)."""
    out = _himalaya(himalaya_bin, "envelope", "list", "-a", hima_account,
                    "-f", member["folder"], "-s", "500", "-o", "json")
    envelopes = json.loads(out)
    want_addr = (member["from_addr"] or "").lower()
    want_dt = _parse_index_date(member["date_utc"])
    matches = []
    for env in envelopes:
        addr = ((env.get("from") or {}).get("addr") or "").lower()
        if addr != want_addr:
            continue
        edt = _parse_himalaya_date(env.get("date") or "")
        if want_dt is None or edt is None:
            continue
        if edt.astimezone(datetime.timezone.utc).replace(second=0) == want_dt.replace(second=0):
            matches.append(env["id"])
    if len(matches) == 1:
        return matches[0]
    raise FatalError(
        f"himalaya id resolution for {member['message_id']!r} in "
        f"{member['folder']!r}: {len(matches)} matches by from+date "
        f"({want_addr}, {member['date_utc']}) -- "
        + ("ambiguous; act on it manually" if matches else
           "not found; run the indexer / sync and retry"))


def cmd_flag(args: argparse.Namespace) -> None:
    """--seen: mark ONE message read (the moment its content is shown).
    --block / --unblock: rule 2's thread-wide clear-then-set, mechanized —
    clear \\Flagged from every currently-flagged member, then (--block)
    set it on the thread's latest state-eligible member only."""
    conn = connect_db(args.db)
    try:
        members = load_account_members(conn, args.account)
    finally:
        conn.close()
    if args.message_id not in members:
        raise FatalError(f"unknown message-id for account {args.account!r}: {args.message_id}")
    hima_account = args.himalaya_account or f"{args.account}-local"
    actions = []

    def op(member, verb, flag):
        hid = resolve_himalaya_id(args.himalaya, hima_account, member)
        _himalaya(args.himalaya, "flag", verb, "-a", hima_account,
                  "-f", member["folder"], hid, flag)
        actions.append({"message_id": member["message_id"], "folder": member["folder"],
                        "himalaya_id": hid, "op": f"{verb} {flag}"})

    if args.mode == "seen":
        op(members[args.message_id], "add", "seen")
    else:
        group = next(g for g in assemble_account_threads(members) if args.message_id in g)
        mlist = [members[m] for m in group]
        for m in mlist:
            if m["is_flagged"] and m["tier"] not in HISTORY_TIERS:
                op(m, "remove", "flagged")
        if args.mode == "block":
            eligible = [m for m in mlist if m["tier"] not in STATE_EXCLUDED_TIERS]
            target = latest_dated(eligible)
            if target is None:
                raise FatalError("thread has no state-eligible member to flag")
            op(target, "add", "flagged")
    print(json.dumps(
        untrusted.apply_trust({"account": args.account, "mode": args.mode,
                               "actions": actions},
                              FLAG_TRUSTED, handle_fields=FLAG_HANDLES),
        indent=2, sort_keys=True))


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
        print(json.dumps(
            untrusted.apply_trust({"residue": residue, "threads": len(residue),
                                   "messages": total},
                                  RESIDUE_TRUSTED,
                                  handle_fields=RESIDUE_HANDLES),
            indent=2, sort_keys=True))
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
                print(json.dumps(
                    untrusted.apply_trust(thread_obj, THREAD_TRUSTED,
                                          handle_fields=THREAD_HANDLES),
                    indent=2, sort_keys=True))
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
            "hidden_text_present": False,
            "hidden_text_suspicious": False,
            "hidden_text_reasons": [],
            "injection_signals": [],
            "links": [],
            "links_truncated": 0,
            "quoted_removed": [],
            "file": rel_file,
        }
    else:
        extracted = extract_body(msg, force_html=args.html_text)
        body, source_part = extracted.text, extracted.source_part
        hidden, links = extracted.hidden, extracted.links
        # 11.2: opt-in. The FULL body stays available without the flag -- a
        # "show me less" mode must never become the only way to read something.
        quoted_removed = []
        if args.no_quotes and body:
            conn2 = connect_db(args.db)
            try:
                ancestors = ancestor_bodies(conn2, args.maildir, args.account,
                                            args.message_id)
            finally:
                conn2.close()
            body, quoted_removed = isolate_quotes(body, ancestors,
                                                  extracted.cite_lines)
        result = {
            "headers": selected_message_headers(msg),
            "body": body,
            "source_part": source_part,
            # 6.4. `present` is a diagnostic: 82% of HTML mail in this
            # mailbox hides text, nearly all of it legitimate preheader copy.
            # `suspicious` is the signal 6.7 acts on -- it means the dropped
            # text was injection-SHAPED. The dropped text itself is never
            # emitted; only our classification of it crosses into context.
            "hidden_text_present": hidden["present"],
            "hidden_text_suspicious": hidden["suspicious"],
            "hidden_text_reasons": hidden["reasons"],
            # 6.7: the same check over the VISIBLE body -- "a body addressing
            # the assistant directly". Separate from the hidden-text reasons
            # because where the text was hidden is itself part of the signal.
            "injection_signals": untrusted.injection_signals(body or ""),
            # 11.8: link targets as their OWN field, never appended to the body.
            # A reference list rendered into the body would put our numbering
            # inside sender-controlled text, where a sender can forge extra
            # entries that read as ours. A sibling field cannot be reached from
            # inside the body at all, and it lets the URLs be classified as
            # handles -- unescaped so they stay usable as keys, and still never
            # fetched (rules 4 and 6 are unchanged by having them visible).
            "links": [{"n": i, "url": u}
                      for i, u in enumerate(links[:LINK_CAP], start=1)],
            "links_truncated": max(0, len(links) - LINK_CAP),
            # Report, never silently transform: same contract as
            # injection_signals and hidden_text_reasons.
            "quoted_removed": quoted_removed,
            "file": rel_file,
        }
    # 6.3: the body gets the full nonce envelope, built inside apply_trust so
    # it shares the manifest's nonce and cannot be double-escaped. One message
    # per context is already structural here -- `read` takes a single
    # message_id and there is no batch form (invariant 8).
    print(json.dumps(
        untrusted.apply_trust(result, READ_TRUSTED, handle_fields=READ_HANDLES,
                              envelope_fields=READ_ENVELOPES),
        indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list", help="list all threads with >=1 member in the given tier set")
    list_p.add_argument("--db", type=Path, default=None, help="index.db; default <data-dir>/index.db")
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
    list_p.add_argument(
        "--scan", action="store_true",
        help="one line per thread with only the priority scan's judgement "
             "fields (subject, sender, age, seen, state) plus message_id -- far "
             "smaller than --compact, which carries per-thread structure the "
             "scan never reads.")
    list_p.add_argument(
        "--unseen", action="store_true",
        help="only threads whose latest non-trash/junk member lacks \\Seen -- "
             "the security pass's scope. Combines with --scan/--compact/"
             "--summary and the other filters.")
    list_p.add_argument(
        "--summary", action="store_true",
        help="counts only, no thread objects: totals (threads / unseen / drafted "
             "/ reconciliation candidates / injection signals), by account, by "
             "state, by tier, and the 15 heaviest senders. Use this for the "
             "session-start breakdown on a busy mailbox -- --compact still "
             "emits one object per thread and overflows past ~100 threads.")
    list_p.add_argument(
        "--compact", action="store_true",
        help="classification-sized summaries (no members array): state, "
             "drafted, tiers_present, latest member. The default for "
             "session-start assembly; full output only when member-level "
             "detail is needed",
    )
    list_p.set_defaults(func=cmd_list)

    flag_p = sub.add_parser(
        "flag",
        help="mechanized himalaya-local flag ops: --seen one message, or "
             "--block/--unblock = rule 2's thread-wide clear-then-set",
    )
    flag_p.add_argument("--db", type=Path, default=None, help="index.db; default <data-dir>/index.db")
    flag_p.add_argument("--account", required=True)
    flag_p.add_argument("--message-id", required=True, dest="message_id",
                        help="--seen: the message to mark read; --block/--unblock: any member of the thread")
    mode = flag_p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--seen", dest="mode", action="store_const", const="seen",
                      help="mark this one message \\Seen (content was shown to the user)")
    mode.add_argument("--block", dest="mode", action="store_const", const="block",
                      help="thread needs the user's reply: clear \\Flagged everywhere, set it on the latest state-eligible member")
    mode.add_argument("--unblock", dest="mode", action="store_const", const="unblock",
                      help="clear \\Flagged from every member")
    flag_p.add_argument("--himalaya", default="himalaya", help="himalaya binary (tests override)")
    flag_p.add_argument("--himalaya-account", default=None,
                        help="himalaya account name; default <account>-local")
    flag_p.set_defaults(func=cmd_flag)

    res_p = sub.add_parser(
        "residue",
        help="end-of-session check: compact list of threads still holding "
             "untriaged-tier messages (empty = clean close)",
    )
    res_p.add_argument("--db", type=Path, default=None, help="index.db; default <data-dir>/index.db")
    res_p.add_argument("--account", action="append", help="limit to this account (repeatable); default: all accounts")
    res_p.add_argument(
        "--tier", action="append",
        help="count residue in this tier (repeatable); default: untriaged only",
    )
    res_p.set_defaults(func=cmd_residue)

    show_p = sub.add_parser("show", help="show the full thread containing a message-id")
    show_p.add_argument("--db", type=Path, default=None, help="index.db; default <data-dir>/index.db")
    show_p.add_argument("--account", required=True)
    show_p.add_argument("--message-id", required=True, dest="message_id")
    show_p.set_defaults(func=cmd_show)

    read_p = sub.add_parser(
        "read",
        help="raw-file-backed content read for one message: headers + body "
             "(Phase 5.1 / review §4 -- replaces the himalaya read path)",
    )
    read_p.add_argument("--db", type=Path, default=None, help="index.db; default <data-dir>/index.db")
    read_p.add_argument("--account", required=True)
    read_p.add_argument("--message-id", required=True, dest="message_id")
    read_p.add_argument(
        "--maildir", type=Path, default=None,
        help="account Maildir root; default <data-dir>/mail/<account>",
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
    read_p.add_argument(
        "--no-quotes", action="store_true", dest="no_quotes",
        help="drop quoted CHAIN content -- text matching an indexed ancestor, "
             "or marked by an attribution line or a client cite container. An "
             "unattributed quotation is KEPT as the author's own content. "
             "Removals are reported in `quoted_removed`; omit the flag for the "
             "full body",
    )
    read_p.set_defaults(func=cmd_read)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    data_dir = Path(os.environ.get("EMAIL_TRIAGE_DATA_DIR", "~/.local/share/email-triage")).expanduser()
    if getattr(args, "db", None) is None:
        args.db = data_dir / "index.db"
    if hasattr(args, "maildir") and args.maildir is None:
        args.maildir = data_dir / "mail" / args.account
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
