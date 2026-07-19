#!/usr/bin/env python3
"""Local Maildir act layer for email-triage: cross-folder moves.

Exists because himalaya's Maildir backend preserves mbsync's private
`,U=<uid>` filename token on cross-folder moves, which makes mbsync
hard-error ("UID N is beyond highest assigned UID") and strand the message
locally (verified live, 2026-07-09; see implementation-plan.md 2.6).
A move here is a plain rename with the U= token stripped, which mbsync
syncs as delete + re-append (design §5).

Only moves live here. Flag changes go through himalaya's Maildir accounts
(suffix-only rename, U= preserved in place — mbsync-safe, verified).
Reply/send stay on the IMAP accounts. See design §7.5, §12.

Resolve-before-act (§12): callers pass message-ids; the index supplies
(folder, maildir_key), and the CURRENT filename is resolved from disk at
act time — never from a cached listing, since any prior action or sync may
have renamed the file.
"""

import argparse
import json
import os
import re
import sqlite3
import sys

UID_TOKEN = re.compile(r",U=\d+")

ROW_SQL = """SELECT folder, maildir_key, maildir_file FROM envelopes
             WHERE account = ? AND message_id = ? AND folder = ?"""


class FatalError(Exception):
    pass


def resolve_current_file(maildir_root: str, folder: str, maildir_key: str) -> str:
    """Find the message's current file on disk by its stable maildir key."""
    for sub in ("cur", "new"):
        d = os.path.join(maildir_root, folder, sub)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.startswith("."):
                continue
            stem = fn.split(":2,", 1)[0]
            if stem == maildir_key:
                return os.path.join(d, fn)
    raise FatalError(
        f"message file not found on disk: folder={folder!r} key={maildir_key!r}"
        " (index stale? run the indexer and retry)"
    )


def move_one(conn, maildir_root: str, account: str, src: str, dst: str,
             message_id: str) -> dict:
    rows = conn.execute(ROW_SQL, (account, message_id, src)).fetchall()
    if not rows:
        raise FatalError(
            f"no index row for message_id={message_id!r} in folder={src!r}"
            f" (account {account!r})"
        )
    folder, key, _ = rows[0]
    src_path = resolve_current_file(maildir_root, folder, key)

    base = os.path.basename(src_path)
    clean = UID_TOKEN.sub("", base)
    dst_dir = os.path.join(maildir_root, dst, "cur")
    for sub in ("cur", "new", "tmp"):
        os.makedirs(os.path.join(maildir_root, dst, sub), exist_ok=True)
    dst_path = os.path.join(dst_dir, clean)
    if os.path.exists(dst_path):
        raise FatalError(f"target already exists, refusing to overwrite: {dst_path}")
    os.rename(src_path, dst_path)
    return {
        "message_id": message_id,
        "from_folder": src,
        "to_folder": dst,
        "file": clean,
    }


OTHER_COPY_SQL = """SELECT COUNT(*) FROM envelopes
                    WHERE account = ? AND message_id = ? AND folder != ?"""


def remove_one(conn, maildir_root: str, account: str, src: str,
               message_id: str) -> dict:
    """Delete a message's file from one folder — the Gmail virtual-Archive
    write path: removing the last tier label leaves the message natively
    archived in All Mail (mechanism verified in design §13.5).

    Guard: refuses when the index shows no copy of this message in any
    other folder — deleting the only copy would be destruction, not
    archiving. (On Gmail every labeled message also has an All Mail row,
    so a legitimate label-strip always passes this guard.)"""
    (others,) = conn.execute(OTHER_COPY_SQL, (account, message_id, src)).fetchone()
    if others == 0:
        raise FatalError(
            f"refusing to remove the ONLY copy of message_id={message_id!r}"
            f" (no row outside folder {src!r}) — this would delete mail,"
            " not archive it"
        )
    rows = conn.execute(ROW_SQL, (account, message_id, src)).fetchall()
    if not rows:
        raise FatalError(
            f"no index row for message_id={message_id!r} in folder={src!r}"
            f" (account {account!r})"
        )
    folder, key, _ = rows[0]
    src_path = resolve_current_file(maildir_root, folder, key)
    os.remove(src_path)
    return {"message_id": message_id, "removed_from": src}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    mv = sub.add_parser("move", help="move messages between local Maildir folders")
    rm = sub.add_parser("remove", help="delete a message's file from one folder "
                        "(Gmail tier-label strip; guarded against last-copy loss)")
    for p in (mv, rm):
        p.add_argument("--db", default=None, help="index.db; default <data-dir>/index.db")
        p.add_argument("--maildir", default=None,
                       help="account Maildir root; default <data-dir>/mail/<account>")
        p.add_argument("--account", required=True, help="account name in the index")
        p.add_argument("--from", dest="src", required=True, help="source folder")
        p.add_argument(
            "message_ids", nargs="+", metavar="MESSAGE-ID",
            help="message-ids; pass a single '-' to read newline-separated "
                 "ids from stdin instead (avoids shell quoting mangling ids "
                 "containing backslashes etc. — bit a live session once)")
    mv.add_argument("--to", dest="dst", required=True, help="destination folder")
    args = ap.parse_args(argv)

    data_dir = os.path.expanduser(
        os.environ.get("EMAIL_TRIAGE_DATA_DIR", "~/.local/share/email-triage"))
    if args.db is None:
        args.db = os.path.join(data_dir, "index.db")
    if args.maildir is None:
        args.maildir = os.path.join(data_dir, "mail", args.account)

    if args.message_ids == ["-"]:
        args.message_ids = [ln.strip() for ln in sys.stdin if ln.strip()]
        if not args.message_ids:
            print("fatal: no message-ids on stdin", file=sys.stderr)
            return 1

    if not os.path.isfile(args.db):
        print(f"fatal: no such db: {args.db}", file=sys.stderr)
        return 1
    if not os.path.isdir(args.maildir):
        print(f"fatal: no such maildir root: {args.maildir}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    done, failed = [], []
    try:
        for mid in args.message_ids:
            try:
                if args.cmd == "move":
                    done.append(move_one(conn, args.maildir, args.account,
                                         args.src, args.dst, mid))
                else:
                    done.append(remove_one(conn, args.maildir, args.account,
                                           args.src, mid))
            except FatalError as e:
                failed.append({"message_id": mid, "error": str(e)})
    finally:
        conn.close()

    key = "moved" if args.cmd == "move" else "removed"
    json.dump({key: done, "failed": failed}, sys.stdout,
              indent=2, sort_keys=True)
    print()
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
