#!/usr/bin/env bash
# Codex `notify` program: codex invokes it with a JSON payload as $1 whenever a
# turn completes (payload type: "agent-turn-complete", with last-assistant-message).
# Wired up via <agent-workdir>/.codex/config.toml + CODEX_HOME by `swarm up`.
#
# Always exits 0 so a hook failure can never disturb the agent.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log="/dev/null"
if [[ -n "${SWARM_ROOT:-}" ]] && mkdir -p "$SWARM_ROOT/.swarm/logs" 2>/dev/null; then
  log="$SWARM_ROOT/.swarm/logs/hooks.log"
fi

"$HERE/swarm.sh" hook codex "${1:-}" >>"$log" 2>&1
exit 0
