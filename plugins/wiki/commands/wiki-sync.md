---
description: Run a wiki sync cycle now (commit, merge through main, push)
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/sync/sync.sh)
---

Run `${CLAUDE_PLUGIN_ROOT}/sync/sync.sh` and report the result in one or two
lines. If it printed nothing and exited 0, either there was nothing to sync or
the cycle completed; check the end of `~/Library/Logs/wiki-sync.log` for this
run's summary and relay it.
