# AgentSwarm

Run a team of coding agents — **Claude Code, Codex, Gemini CLI, Hermes** — side by
side in tmux, each in its own directory, each able to message the others only if
your YAML file says it may.

```
   agents.yaml                 tmux
  ┌────────────┐        ┌──────────────────┐
  │ orchestr.  │───────▶│ session: orchestr│──┐
  │ researcher │        │ session: research│  │  swarm send --to developer "..."
  │ developer  │        │ session: develop │◀─┘
  │ reviewer   │        │ session: reviewer│
  └────────────┘        └──────────────────┘
         │                       │
         │  workspace/<agent>/   │  hooks capture each finished turn
         ▼                       ▼
   one folder per agent    messages routed + logged
```

One command starts the swarm: it creates a folder per agent, installs a
completion hook inside each folder, opens a tmux session per agent, launches
the agent's CLI, and types each agent's first prompt into it.

---

## Requirements

- `tmux` (3.0+)
- `python3` — PyYAML is used if present, otherwise a bundled parser handles the config
- whichever agent CLIs you reference: `claude`, `codex`, `gemini`, `hermes`

## Quickstart

```bash
git clone <this repo> && cd AgentSwarm
cp agents.example.yaml agents.yaml

./swarm.sh validate      # parse the config, print the resolved swarm, launch nothing
./swarm.sh up            # create dirs, install hooks, start tmux, send first prompts
./swarm.sh status        # who is running
./swarm.sh attach developer
./swarm.sh down          # stop everything
```

Give the swarm its actual work:

```bash
./swarm.sh send --to orchestrator "Build a CLI that converts CSV to Parquet."
```

Watch the traffic between agents:

```bash
./swarm.sh logs -f              # whole swarm, live
./swarm.sh logs reviewer -n 20  # one agent
./swarm.sh inbox developer      # messages an agent received
```

---

## How it works

**One folder + one tmux session per agent.** Agent `developer` gets
`workspace/developer/` and a tmux session named `developer` (plus any
`session_prefix`). The agent's CLI is launched inside that folder, so its file
operations are naturally scoped to it.

### Working directories

By default every agent gets a fresh folder under `root`, created for you. You can
override that per agent, or for the whole swarm via `defaults`:

```yaml
swarm:
  root: ./workspace
  create_workdirs: true          # auto-create missing folders (default)

agents:
  - name: developer              # -> ./workspace/developer  (created)

  - name: reviewer
    workdir: ~/projects/acme-api # -> an existing checkout
    create_workdir: false        # ...and fail loudly if it is not there

  - name: scribe
    workdir: "{root}/{name}-notes"   # {name} {root} {swarm} {type}
```

- **`workdir`** may be absolute, relative to the config file, or use `~`.
- **`create_workdir: false`** turns a missing folder into an error instead of a
  new empty directory — the right setting when you are pointing agents at real
  repositories, where a typo should not silently create `~/projcets/acme-api`.
- **`defaults.workdir`** applies to every agent that does not override it. With
  a `{name}` placeholder it lays out a folder each; without one, every agent
  shares a single directory.
- **Sharing is allowed and sometimes the point** (a driver and a navigator in one
  checkout), but agents then overwrite each other's files and interleave commits,
  so `validate` and `up` warn when it happens. See
  [`examples/existing-repo.yaml`](examples/existing-repo.yaml).

`root` is still used even when every agent lives elsewhere: it holds `.swarm/`
with the logs, inboxes and the `swarm` shim.

**Prompts are typed in, not piped.** `swarm up` drops each first prompt into the
agent's input box with a tmux bracketed paste, as one block, then presses Enter.
That is why multi-line prompts survive intact instead of being submitted line by
line.

Getting that to work reliably against a live TUI took more than a sleep:

- **Claude Code silently discards keystrokes for several seconds partway through
  startup.** Measured on v2.1.205: input at t=2s landed, t=6s and t=12s vanished,
  t=20s landed. A fixed `boot_delay_ms` is therefore a coin flip. Before typing,
  AgentSwarm types a throwaway token and waits for the input box to echo it back,
  then erases it (`ready_probe`). Enter is never sent, so nothing is submitted.
- **Readiness is not monotonic**, so after pasting, AgentSwarm checks that the
  text actually appeared on screen before pressing Enter, and retries if it did
  not. If delivery cannot be confirmed it refuses to press Enter, rather than
  submitting a half-delivered prompt.
- **Codex opens a "do you trust this directory?" modal** on first run in a new
  folder, which would eat the first prompt (Enter answers the dialog). The agent's
  generated `config.toml` pre-trusts its own working directory.

**Agents talk by messaging each other.** Every agent's session has a `swarm`
command on its `PATH` and `SWARM_AGENT` in its environment, so from inside any
agent:

```bash
swarm send --to reviewer "I finished the parser, please review src/parse.py"
swarm broadcast "heads up: I renamed the config module"
```

`swarm send` checks `can_talk_to` before delivering, archives the message under
`.swarm/inbox/<recipient>/`, appends to the event log, and pastes it into the
recipient's tmux pane prefixed with `[swarm] message from <sender>:`.

The raw tmux equivalent also works, and bypasses permissions and logging:

```bash
tmux send-keys -t reviewer -l "your message" && tmux send-keys -t reviewer Enter
```

**Permissions are a whitelist.** An agent may only message the agents in its
`can_talk_to` list. Anything else is refused with an explanatory error that the
agent sees on its own terminal. Use `can_talk_to: "*"` for "everyone else".

---

## Capturing what an agent says

AgentSwarm needs to know when an agent finishes a turn — both to log it and to
support auto-forwarding. How it finds out depends on the CLI, and the two
mechanisms are **not** equally good:

| `capture` | Used by | Mechanism | Reliability |
|---|---|---|---|
| `hook` | `claude`, `codex` | The CLI runs a program when a turn completes | Exact — the model's final message |
| `pane` | `gemini`, `hermes` | Poll the tmux pane, diff it once it stops changing | Heuristic — sees rendered text |
| `none` | any | Nothing is captured | — |

- **claude** → a `Stop` hook is written into `<agent-dir>/.claude/settings.json`.
  It reads the session transcript and extracts the last assistant message.
- **codex** → the agent gets a private `CODEX_HOME` at `<agent-dir>/.codex/`
  with a `notify` program wired up (your `~/.codex/auth.json` is symlinked in, so
  it stays logged in, and your existing `config.toml` is carried over). The
  generated file keeps `notify` above every `[table]` header -- TOML is
  order-sensitive, and a `notify` written after one silently becomes
  `projects.<dir>.notify`, which codex never calls.
- **gemini / hermes** → no turn-completion hook exists, so a background watcher
  samples the pane and emits the new text once it has been quiet for
  `pane_idle_ms`. It filters out the terminal's echo of incoming messages, but
  it is still terminal scraping: spinners and redraws can leak in. Prefer having
  these agents call `swarm send` explicitly.

Set `capture:` per agent to override the default for its type.

### Auto-forwarding

`forward_responses_to` relays an agent's finished turn to other agents without
it having to ask:

```yaml
- name: researcher
  can_talk_to: [orchestrator, developer]
  forward_responses_to: [orchestrator]   # must be a subset of can_talk_to
```

Two agents forwarding to each other would ping-pong forever, so every forwarded
message carries a hop count, and forwarding stops at `max_forward_hops`
(default 3). A fresh message from you resets the count. Auto-forwarding is
powerful but chatty — for most swarms it is better to let agents decide when to
speak, and leave `forward_responses_to` unset.

---

## Configuration reference

Full annotated example: [`agents.example.yaml`](agents.example.yaml).
Machine-readable summary for agents: [`llms.txt`](llms.txt).

### `swarm:`

| Key | Default | Meaning |
|---|---|---|
| `name` | config filename | Label used in prompts and logs |
| `root` | `./workspace` | Where per-agent folders are created |
| `create_workdirs` | `true` | Auto-create missing agent folders |
| `session_prefix` | `""` | Prepended to every tmux session name |
| `send_delay_ms` | `150` | Pause before pasting into a pane |
| `enter_delay_ms` | `250` | Pause between pasting and pressing Enter |
| `max_forward_hops` | `3` | Auto-forward loop guard |
| `ready_timeout_ms` | `60000` | How long to wait for an agent's input box to respond |
| `pane_idle_ms` | `2500` | Quiet time before a `pane` turn counts as done |
| `pane_poll_ms` | `700` | Pane sampling interval |
| `pane_scrollback` | `400` | Lines of scrollback the watcher diffs |

### `agents:`

| Key | Default | Meaning |
|---|---|---|
| `name` | *required* | Folder name **and** tmux session name |
| `type` | `claude` | `claude`, `codex`, `gemini`, `hermes`, or one you define |
| `command` | from type | Exact CLI to run, e.g. `claude --dangerously-skip-permissions` |
| `can_talk_to` | `[]` | Whitelist of agents it may message; `"*"` for all others |
| `first_prompt` | `""` | Prompt typed in after the CLI boots |
| `first_prompt_file` | — | Read the prompt from a file instead |
| `append_agents_that_you_can_talk_to_prompt` | `true` | Append the "here's who you can message and how" block |
| `in_first_prompt_append_your_task_will_be_sent_in_the_next_prompt` | `false` | Append "stand by, your task is coming next" |
| `forward_responses_to` | `[]` | Auto-relay finished turns to these agents |
| `capture` | from type | `hook`, `pane`, `none`, or `auto` |
| `boot_delay_ms` | from type | Grace period before probing the input box (not a delivery guarantee) |
| `ready_probe` | `true` | Wait for the input box to echo a token before typing |
| `workdir` | `<root>/<name>` | Override the agent's directory (`~`, `{name}`, `{root}`, `{swarm}`, `{type}`) |
| `create_workdir` | from `create_workdirs` | Create the folder if missing, else error |
| `env` | `{}` | Extra environment variables for its tmux session |

### `agent_types:`

Override a built-in launch command, or define a new agent type:

```yaml
agent_types:
  claude:
    command: "claude --dangerously-skip-permissions --model opus"
  aider:                       # a type of your own
    command: "aider --yes"
    capture: pane              # only claude/codex support `hook`
    boot_delay_ms: 4000
```

### `defaults:` and `templates:`

`defaults:` supplies any agent key for agents that don't set it, including
`workdir` — useful for putting a whole swarm in one repository. `templates:`
overrides the two blocks appended to first prompts — `comms` and `task_notice` —
with `{agent} {swarm} {peers} {prefix} {inbox} {workdir}` available as
placeholders.

---

## Examples

Ready-to-run swarms in [`examples/`](examples/):

| File | Shape | Shows off |
|---|---|---|
| [`research-swarm.yaml`](examples/research-swarm.yaml) | Hub and spoke | A lead delegating to a scout, an analyst and a writer; a custom output folder |
| [`software-company.yaml`](examples/software-company.yaml) | Org chart | Six agents across all four CLIs, with a deliberately restricted comms graph |
| [`bug-hunt.yaml`](examples/bug-hunt.yaml) | Pipeline | `forward_responses_to` chaining reproduce → diagnose → fix → verify, hands-free |
| [`existing-repo.yaml`](examples/existing-repo.yaml) | Pairing | Two agents in one **existing** checkout, with `create_workdirs: false` |

```bash
./swarm.sh validate -c examples/research-swarm.yaml   # look before you leap
./swarm.sh up       -c examples/research-swarm.yaml
./swarm.sh send --to lead "Research the state of WebGPU compute shaders."
```

`existing-repo.yaml` intentionally refuses to start until you point `workdir` at
a repository that exists.

## Commands

| Command | Purpose |
|---|---|
| `swarm.sh up` | Start the swarm. `--only a,b`, `--restart`, `--no-prompt`, `--attach` |
| `swarm.sh down` | Kill sessions and watchers. `--only a,b` |
| `swarm.sh restart` | `down` then `up` |
| `swarm.sh status` | Table of agents, sessions, capture mode, permissions |
| `swarm.sh attach <agent>` | Attach to an agent's tmux session |
| `swarm.sh send --to <agent> "msg"` | Deliver a message (`--from`, `--file`, `--force`) |
| `swarm.sh broadcast "msg"` | Message everyone the sender may talk to |
| `swarm.sh inbox <agent>` | Print archived messages |
| `swarm.sh logs [agent] [-f]` | Event log: prompts, responses, messages |
| `swarm.sh validate` | Parse the config. `--show-prompts` renders final prompts |

`./swarm.sh my-swarm.yaml` is shorthand for `./swarm.sh up -c my-swarm.yaml`.
`-c` and `$SWARM_CONFIG` both select a config; `-c` wins.

## Layout

```
AgentSwarm/
├── swarm.sh                # entrypoint
├── agents.example.yaml     # annotated config
├── llms.txt                # reference for agents configuring this tool
├── hooks/
│   ├── claude_stop.sh      # Claude Code Stop hook
│   └── codex_notify.sh     # Codex notify program
├── lib/
│   ├── swarm.py            # tmux orchestration, routing, capture
│   ├── config.py           # schema, defaults, validation
│   └── minyaml.py          # YAML subset parser, used when PyYAML is absent
├── examples/               # research swarm, software company, bug hunt, pairing
└── workspace/              # created by `up`
    ├── <agent>/            # one folder per agent
    └── .swarm/
        ├── state.json      # what `up` started
        ├── bin/swarm       # the `swarm` command agents call
        ├── logs/           # <agent>.jsonl + swarm.jsonl
        ├── inbox/<agent>/  # archived messages
        └── run/            # watcher pids, hop counters
```

## Troubleshooting

**"could not confirm the text arrived; NOT pressing Enter".** The agent's input
box never echoed the prompt, so AgentSwarm refused to submit it. Attach to the
session to see what state the CLI is in -- usually a modal (login, trust,
onboarding) is holding focus. Raise `ready_timeout_ms` if the CLI is merely slow.

**An agent says it "cannot message" another.** That is the permission check
doing its job — add the recipient to the sender's `can_talk_to`.

**A `pane` agent forwards garbage.** Terminal scraping picked up a redraw. Raise
`pane_idle_ms`, or set `capture: none` and instruct the agent to call
`swarm send` itself.

**Forwarding stopped with a hop-limit warning.** Two agents were relaying to each
other. Raise `max_forward_hops`, or break the cycle in `forward_responses_to`.

**Nothing captured from a claude agent.** Check `<agent-dir>/.claude/settings.json`
exists and `.swarm/logs/hooks.log` for errors.

## A note on flags

`claude --dangerously-skip-permissions`, `codex --yolo` and `gemini --yolo` let
agents act without asking for confirmation. That is usually what you want for an
unattended swarm, and it means several models are running tools unsupervised in
these directories. Point `root` somewhere disposable, and don't run a swarm over
a directory you can't afford to lose.
