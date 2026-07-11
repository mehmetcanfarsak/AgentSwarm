#!/usr/bin/env bash
# Claude Code `Stop` hook: fires when Claude finishes responding.
# Claude passes a JSON payload on stdin containing `transcript_path`.
# Installed automatically into <agent-workdir>/.claude/settings.json by `agentainer up`.
#
# A hook must never break the agent it is attached to, so every failure here is
# swallowed and the script always exits 0.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log="/dev/null"
if [[ -n "${SWARM_ROOT:-}" ]] && mkdir -p "$SWARM_ROOT/.swarm/logs" 2>/dev/null; then
  log="$SWARM_ROOT/.swarm/logs/hooks.log"
fi

"$HERE/agentainer" hook claude >>"$log" 2>&1
exit 0
