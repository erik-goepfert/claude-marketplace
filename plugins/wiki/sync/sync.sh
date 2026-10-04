#!/bin/bash
# sync.sh -- the single entry point for a wiki sync cycle.
#
# Called by the launchd timer (--timer), by the plugin's hooks, and by
# /wiki-sync. Reconciles the `icloud` and `claude` worktrees through `main`
# and pushes to GitHub (design: erik-goepfert-private projects/wiki/README.md).
#
# Order of operations:
#   1. Lock     -- only one cycle at a time, however it was started.
#   2. Precheck -- plain git, no model: is there anything to do at all?
#   3. Headless `claude -p` does the actual commits/merges/push. It runs in
#      dontAsk mode with only `Bash(git *)` pre-approved, so any other tool
#      call is denied outright -- note content can't steer it into anything
#      beyond git.
#
# Modes:
#   --timer    launchd backstop. Skips if another cycle holds the lock, and
#              defers while phone edits are still arriving (quiet window).
#   (default)  on demand. Waits for a running cycle to finish, ignores the
#              quiet window -- the caller wants the latest right now.

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PLUGIN_ROOT/config.sh"

MODE=ondemand
[[ "$1" == "--timer" ]] && MODE=timer

QUIET_SECONDS=120       # timer mode: phone edits must be this old to sync
LOCK_WAIT_SECONDS=180   # on-demand mode: how long to wait for a running cycle

mkdir -p "$WIKI_STATE_DIR"
LOCK_FILE="$WIKI_STATE_DIR/sync.lock"
STAMP_FILE="$WIKI_STATE_DIR/last-check"

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S%z') [$MODE] $*" >> "$WIKI_LOG"; }

# --- 1. Lock ---------------------------------------------------------------
# Re-exec under lockf(1) so the lock is released however we exit.
if [[ -z "$WIKI_SYNC_LOCKED" ]]; then
  export WIKI_SYNC_LOCKED=1
  wait=$LOCK_WAIT_SECONDS
  [[ $MODE == timer ]] && wait=0
  /usr/bin/lockf -s -t "$wait" "$LOCK_FILE" "$0" "$@"
  rc=$?
  if [[ $rc == 75 ]]; then   # EX_TEMPFAIL: lock not acquired in time
    log "skipped: another sync cycle is running"
    exit 0
  fi
  exit $rc
fi

# --- 2. Precheck -------------------------------------------------------------
g() { git -C "$WIKI_MAIN" "$@"; }

# iCloud evicts files under "Optimize Mac Storage", leaving `.Name.md.icloud`
# placeholders. `git add -A` would commit that as the note being deleted, and
# the deletion would propagate everywhere. Refuse to run until it's resolved.
evicted=$(find "$WIKI_ICLOUD" -name '.*.icloud' -not -path '*/.git/*' 2>/dev/null | head -5)
if [[ -n "$evicted" ]]; then
  log "ABORTED: evicted iCloud placeholders in vault (set the folder to Keep Downloaded): $evicted"
  echo "wiki sync aborted: iCloud has evicted files in the vault, see $WIKI_LOG" >&2
  exit 1
fi

icloud_dirty=$(git -C "$WIKI_ICLOUD" status --porcelain --untracked-files=all)
claude_dirty=$(git -C "$WIKI_CLAUDE" status --porcelain --untracked-files=all)
unmerged=$(g rev-list main..icloud main..claude icloud..main claude..main | head -1)
unpushed=""
for b in main icloud claude; do
  [[ "$(g rev-parse "$b")" != "$(g rev-parse "origin/$b" 2>/dev/null)" ]] && unpushed=1
done

touch "$STAMP_FILE"

if [[ -z "$icloud_dirty$claude_dirty$unmerged$unpushed" ]]; then
  exit 0   # nothing to do; deliberately not logged (every 15 min + every hook)
fi

# Quiet window: on the timer, don't commit a note while it's still being
# typed on the phone. Only looks at files git sees as changed, so Obsidian's
# ignored UI-state churn doesn't hold the cycle off forever.
if [[ $MODE == timer && -n "$icloud_dirty" ]]; then
  now=$(date +%s)
  while IFS= read -r -d '' entry; do
    path="${entry:3}"
    [[ -e "$WIKI_ICLOUD/$path" ]] || continue
    age=$(( now - $(stat -f %m "$WIKI_ICLOUD/$path") ))
    if (( age < QUIET_SECONDS )); then
      log "deferred: '$path' changed ${age}s ago, waiting for edits to settle"
      exit 0
    fi
  done < <(git -C "$WIKI_ICLOUD" status --porcelain -z --untracked-files=all)
fi

# --- 3. Headless sync -------------------------------------------------------
# WIKI_SYNC_HEADLESS tells this plugin's own hooks to stand down inside the
# headless run (otherwise they'd try to sync from within the sync).
export WIKI_SYNC_HEADLESS=1
prompt="$(cat "$PLUGIN_ROOT/sync/prompt.md")

$(cat "$PLUGIN_ROOT/skills/wiki/git-conventions.md")"

{
  echo "=== $(date '+%Y-%m-%dT%H:%M:%S%z') [$MODE] sync start ==="
  cd "$WIKI_MAIN" || exit 1
  "$HOME/.local/bin/claude" -p "$prompt" \
    --permission-mode dontAsk \
    --allowedTools "Bash(git *)" \
    --add-dir "$WIKI_CLAUDE" \
    --add-dir "$WIKI_ICLOUD" < /dev/null
  rc=$?
  echo "=== $(date '+%Y-%m-%dT%H:%M:%S%z') [$MODE] sync end (exit $rc) ==="
  exit $rc
} >> "$WIKI_LOG" 2>&1
