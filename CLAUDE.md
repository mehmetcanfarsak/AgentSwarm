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

`capture: none` *removes* this detection. For `claude`/`codex` (which have a hook)
that leaves the orchestrator blind to a silent turn — exactly the footgun below —
so `capture: none` on those types is **auto-upgraded to `hook` at load time** (with
a `validate`/`up` warning). Use `gemini`/`hermes` when you genuinely want
deliberate, hook-free sending. Note that a `gemini`/`hermes` agent left on
`capture: none` keeps no completion signal and the supervisor (below) can only
catch its *dead* session, not an "alive but silent" one.

A **supervisor** background process (started at `up`, `supervise_interval_ms`,
default 15s; `swarm.supervise: false` disables it) is the heartbeat the
event-driven design otherwise lacks. Each tick it reconciles stale-busy agents
(`delivered > completed` past `busy_timeout_ms` → marked idle + queue drained),
logs and reconciles dead sessions, and re-runs `sweep_stale_queues` — so one
silent agent cannot wedge the whole swarm. `status` reports whether it is alive.

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

## README & marketing assets

The `README.md` doubles as the project's landing page, so it is written for
**discovery** (SEO + LLM answer engines) as well as for readers. Keep these
conventions when editing it:

- **Structure that ranks.** Keyword-rich H1 ("Multi-Agent Orchestrator for AI
  Coding Agents"), a plain-language one-paragraph definition directly under it
  (models can quote it verbatim), a **Table of contents**, and an **FAQ**
  section of natural-question Q&A (the format answer engines extract). Preserve
  these when restructuring — don't collapse the FAQ or drop the intro paragraph.
- **Never change the technical substance for style.** All config tables
  (`swarm:`/`agents:`/`agent_types:`/`defaults:`), capture mechanics, footguns,
  the examples matrix, and troubleshooting are load-bearing. Marketing edits are
  additive framing (headings, emoji section markers, badges, images) only.

Visual assets live in **`assets/`** (committed to git; *not* shipped to npm —
the `files` allowlist is source-only, which is fine because the README
references them by absolute URL). They are **hand-authored SVGs**, consistent
with the zero-dependency ethos — no PNG/GIF pipeline, no asciinema/vhs/agg:

| File | Purpose |
|------|---------|
| `assets/banner.svg` | Top-of-README brand banner (logo mark + wordmark + tagline + feature chips) |
| `assets/demo.svg` | Looping terminal "cast" of the quickstart (`up`→`status`→`send`→`logs`), SMIL-animated |
| `assets/architecture.svg` | The "how it fits together" diagram (YAML → tmux sessions → core routing) |
| `assets/screenshot-status.svg` | Terminal-styled `status`/`queue` output |

Rules for these assets:

- **Reference them with absolute `raw.githubusercontent.com/.../main/assets/...`
  URLs**, never relative paths — relative paths break on the npmjs.com package
  page. Every `<img>` needs descriptive `alt` text (accessibility + indexing).
- **Terminal SVGs must show real output.** The demo/screenshot text was captured
  by running an actual mock swarm (`./agentainer up/status/send/logs` against a
  throwaway config with `command: "bash -c '…read…'"` agents — no API keys). Do
  not invent output; re-capture if the CLI's format changes.
- **Animations degrade gracefully.** `demo.svg` uses SMIL with base
  `opacity="1"` so that if a renderer strips animation, the full transcript is
  still visible. GitHub renders animated SVGs via its camo proxy.
- **Text must fit its box.** SVG has no auto-layout — a label wider than its
  pill/rect silently overflows into neighbors. When editing, sanity-check widths
  (mono ≈ 0.6em/char) and that XML is well-formed
  (`python3 -c "import xml.dom.minidom; xml.dom.minidom.parse('assets/x.svg')"`).
- Prefer SVG over binary images for anything that is essentially text
  (diagrams, terminals): crisp at any DPI, tiny, and the text stays indexable.
- Keep a plain-text fallback for diagrams (e.g. the ASCII architecture art in a
  `<details>` block) for terminals and screen readers.

### How these were produced (reproducible recipe)

No design tools were used — the SVGs were written by hand and the terminal text
was captured from the real CLI. To reproduce or refresh them:

1. **Capture real CLI output** (no API keys — mock agents are just bash loops).
   Run a throwaway swarm and copy the actual `up`/`status`/`send`/`logs` text
   into the terminal SVGs:
   ```bash
   cat > /tmp/demo-swarm.yaml <<'Y'
   swarm: {name: demo, root: /tmp/demo-workspace}
   agents:
     - {name: orchestrator, type: claude, capture: none, can_talk_to: "*",
        command: "bash -c 'while true; do read -r l || sleep 1; done'"}
     - {name: researcher, type: claude, capture: none, can_talk_to: [orchestrator, developer],
        command: "bash -c 'while true; do read -r l || sleep 1; done'"}
     - {name: developer, type: claude, capture: none, can_talk_to: [orchestrator, reviewer],
        command: "bash -c 'while true; do read -r l || sleep 1; done'"}
     - {name: reviewer, type: claude, capture: none, can_talk_to: [developer],
        command: "bash -c 'while true; do read -r l || sleep 1; done'"}
   Y
   ./agentainer up      -c /tmp/demo-swarm.yaml --no-prompt
   ./agentainer status  -c /tmp/demo-swarm.yaml
   ./agentainer send    -c /tmp/demo-swarm.yaml --to orchestrator "Build a CLI that converts CSV to Parquet."
   ./agentainer logs    -c /tmp/demo-swarm.yaml -n 12
   ./agentainer down    -c /tmp/demo-swarm.yaml
   ```
   (`--help` and `validate -c examples/research-swarm.yaml` give the other real
   strings.) The config lives in `/tmp`, so nothing lands in the repo.

2. **Hand-author the SVGs** into `assets/` with the `Write` tool — no libraries.
   Shared visual language: dark terminal bg (`#0c1220`/`#0a0f1c`), grid overlay
   (`<pattern>`), accent gradient cyan→indigo (`#22d3ee`→`#818cf8`), traffic-light
   window dots (`#ff5f56`/`#ffbd2e`/`#27c93f`), monospace stack `ui-monospace,
   SFMono-Regular, Menlo, Consolas, monospace`. Give each `<svg>` a `role="img"`
   and `aria-label`. Colour terminal text with `<tspan fill=…>`.

3. **Animate with SMIL, not CSS/JS.** In `demo.svg` each command block is a `<g>`
   with base `opacity="1"` plus an `<animate attributeName="opacity"
   repeatCount="indefinite" values="0;…;1;…;0" keyTimes=…>` — staggered
   `keyTimes` reveal blocks in sequence and the loop resets together. Base
   opacity 1 is the graceful-degradation fallback.

4. **Verify before wiring in.** SVG has no auto-layout, so check both:
   ```bash
   for f in assets/*.svg; do python3 -c "import xml.dom.minidom; xml.dom.minidom.parse('$f')" \
     && echo "OK $f" || echo "BAD $f"; done
   ```
   and eyeball that every text label fits its box (mono ≈ 0.6em/char; a label
   wider than its pill/rect overflows silently into its neighbours — the common
   bug). Then reference each from `README.md` by absolute `raw.githubusercontent`
   URL with `alt` text.
