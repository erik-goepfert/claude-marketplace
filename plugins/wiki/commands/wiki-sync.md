---
description: Run a wiki sync cycle now (commit, merge through main, push)
allowed-tools: Bash(/usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/sync/sync.py)
---

Run `/usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/sync/sync.py` and report the result in one or two
lines. If it printed nothing and exited 0, either there was nothing to sync or
the cycle completed; check the end of `~/Library/Logs/wiki-sync.log` for this
run's summary and relay it.
