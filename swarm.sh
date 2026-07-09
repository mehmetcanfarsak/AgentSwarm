#!/usr/bin/env bash
#
# AgentSwarm -- launch a configurable swarm of coding agents in tmux.
#
#   ./swarm.sh up                    start every agent in agents.yaml
#   ./swarm.sh up -c my-swarm.yaml   ...from a different config
#   ./swarm.sh agents.yaml           shorthand for `up -c agents.yaml`
#   ./swarm.sh status                see who is running
#   ./swarm.sh send --to dev "hi"    message an agent
#   ./swarm.sh attach dev            jump into an agent's tmux session
#   ./swarm.sh down                  stop everything
#
# See README.md for the full reference, or llms.txt if you are an LLM.
set -euo pipefail

SWARM_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SWARM_HOME

PYTHON="${SWARM_PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON="$candidate"
      break
    fi
  done
fi

if [[ -z "$PYTHON" ]]; then
  echo "xx AgentSwarm needs python3 on PATH (or set SWARM_PYTHON)" >&2
  exit 1
fi

if ! command -v tmux >/dev/null 2>&1; then
  echo "!! tmux was not found on PATH; every command except 'validate' will fail" >&2
fi

# The config is resolved in lib/swarm.py: -c, then $SWARM_CONFIG, then
# ./agents.yaml, then the agents.yaml next to this script. Deliberately NOT
# exported here -- a stale SWARM_CONFIG would shadow the config that a hook
# discovers from the agent's working directory.

exec "$PYTHON" "$SWARM_HOME/lib/swarm.py" "$@"
