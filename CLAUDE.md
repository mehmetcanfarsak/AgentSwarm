# CLAUDE.md

Guidance for Claude Code (and other agents) working in this repository.

## What this is

**Agentainer** (formerly AgentSwarm) is a **zero-dependency** multi-agent
orchestrator. It launches coding-agent CLIs (Claude Code, Codex, Gemini, Hermes)
each in its own tmux session and working directory, defined by a single YAML
file, and lets them message each other only where the config's `can_talk_to`
ACL allows.

The whole system is stdlib-only: Python 3 + bash + tmux. **Do not add runtime
dependencies** (PyYAML is used *if present*, but a bundled parser must keep
working without it). Node is used only for the global npm launcher, never at
swarm runtime.

## Layout

| Path | Role |
|------|------|
| `agentainer` | Bash entry for local/clone use. Finds python3, sets `SWARM_HOME`, execs `lib/swarm.py`. |
| `bin/agentainer.js` | npm global launcher. Resolves the package root through the bin symlink, sets `SWARM_HOME`, execs `lib/swarm.py`. Also handles `agentainer doctor`. |
| `lib/swarm.py` | The orchestrator — all subcommands, tmux control, turn/queue state, routing, logging. The bulk of the logic. |
| `lib/config.py` | YAML config loading + validation into `SwarmConfig`. |
| `lib/minyaml.py` | Fallback YAML parser (used when PyYAML is absent). |
| `hooks/claude_stop.sh`, `hooks/codex_notify.sh` | Turn-completion hooks installed into each agent's workdir at `up`. |
| `scripts/check-deps.js` | Dependency doctor (npm `postinstall` + `agentainer doctor`). |
| `examples/*.yaml` | Runnable example swarms. |
| `tests/validate.sh` | Full mock-based validation suite (no model calls, no API keys). |

`SWARM_HOME` is the repo/package root. `lib/swarm.py` derives it from
`__file__` (`parents[1]`) or the `SWARM_HOME` env var; both launchers set it
explicitly. Hooks and the generated per-agent `swarm` shim resolve relative to
it, so it must always point at a real checkout, never a symlink dir.

## Commands

```bash
./agentainer validate -c <config>   # parse + print the resolved swarm, launch nothing
./agentainer up -c <config>         # create dirs, install hooks, start tmux, send first prompts
./agentainer status                 # who is running
./agentainer send --to <agent> "…"  # message an agent
./agentainer logs -f                # live event stream
./agentainer down                   # stop everything
tests/validate.sh                 # run the full suite (mock agents; safe, free)
```

Subcommands: `up down restart status attach send broadcast sessions queue idle
inbox logs validate hook watch`. Installed globally the command is
`agentainer`; `./agentainer` is the drop-in equivalent from a clone.

Always run `tests/validate.sh` after changing anything in `lib/` or `hooks/`.

## How turn-completion works (important footgun)

An agent's **`type`** selects how a finished turn is detected:

- `claude` → a **Stop hook** (`hooks/claude_stop.sh`) written into `<workdir>/.claude/settings.json`
- `codex` → a **`notify`** program wired into `<workdir>/.codex/config.toml`
- `gemini` / `hermes` → **pane polling** (`capture` from the terminal)

The hook wiring is baked into the workdir **at `up` time** from `type`. If an
agent's `command` launches a *different* CLI than its `type` implies (e.g.
`type: codex` but `command` runs `claude`), completion is never detected: the
agent pins as permanently "busy" (`delivered > completed`), its queue never
drains, and if all healthy agents are idle no turn-end fires to trigger
`sweep_stale_queues` — a hard deadlock. **Keep `type` consistent with what
`command` actually runs**, and prefer editing the config over trying to heal a
running swarm (its wiring can't be changed in place).

## Runtime state & logs

Per-swarm state lives under `<root>/.swarm/`:

- `.swarm/logs/<agent>.jsonl` and `swarm.jsonl` — durable, complete event log
  (`first_prompt`, `response`, `received`, `queued`, `sent`). This is the
  reliable way to read an agent's full history — tmux keeps **no scrollback**
  for fullscreen-TUI panes (alternate screen), so scrolling up in the pane
  can't show past output.
- `.swarm/run/<agent>.turn.json` — `{delivered, completed, since, by}` turn state.
- `.swarm/run/<agent>.queue.jsonl` — the agent's pending inbound turns.
- `.swarm/inbox/<agent>/` — received message files.

Never commit or ship `.swarm/`, `workspace/`, example `*-workspace/` /
`*-output/` dirs, or `__pycache__/` — they are runtime state (see `.gitignore`
/ `.npmignore`; the npm `files` allowlist ships source only).

## Conventions

- Match the existing style: stdlib only, terse `xx`/`!!`/`ok` status prefixes on
  messages, small focused functions.
- Don't print or commit secrets. Example configs may reference shell aliases
  that embed API keys — treat agent `command` strings as sensitive.
- `--dangerously-skip-permissions` / `--yolo` run agent tools unsupervised, so
  a swarm's `root` should point at a disposable directory.
