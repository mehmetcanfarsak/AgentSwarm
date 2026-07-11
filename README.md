# Agentainer

Run a team of coding agents — **Claude Code, Codex, Gemini CLI, Hermes** — side by
side in tmux, each in its own directory, each able to message the others only if
your YAML file says it may.

> Formerly **AgentSwarm**. Installed globally the command is `agentainer`; from a
> clone, the `./agentainer` script in the repo root is the same thing.

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
- `node` (16+) — only for the global `agentainer` command; not needed if you run `./agentainer` from a clone
- whichever agent CLIs you reference: `claude`, `codex`, `gemini`, `hermes` — install only the one(s) you actually use

## Install

Global, via npm:

```bash
npm install -g agentainer
agentainer doctor        # check tmux/python3 are present; report which agent CLIs it found
```

`agentainer doctor` verifies the required tools (`tmux`, `python3`) and reports
which agent CLIs are available — it never fails on a missing *agent* CLI, since
you may only use one of them.

Or from a clone (no npm needed):

```bash
git clone https://github.com/mehmetcanfarsak/AgentSwarm.git && cd AgentSwarm
./agentainer --help      # same commands as the global `agentainer`, straight from the repo
```

## Quickstart

```bash
cp agents.example.yaml agents.yaml

agentainer validate      # parse the config, print the resolved swarm, launch nothing
agentainer up            # create dirs, install hooks, start tmux, send first prompts
agentainer status        # who is running
agentainer attach developer
agentainer down          # stop everything
```

Give the swarm its actual work:

```bash
agentainer send --to orchestrator "Build a CLI that converts CSV to Parquet."
```

Watch the traffic between agents:

```bash
agentainer logs -f              # whole swarm, live
agentainer logs reviewer -n 20  # one agent
agentainer inbox developer      # messages an agent received
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
  Agentainer types a throwaway token and waits for the input box to echo it back,
  then erases it (`ready_probe`). Enter is never sent, so nothing is submitted.
- **Readiness is not monotonic**, so after pasting, Agentainer checks that the
  text actually appeared on screen before pressing Enter, and retries if it did
  not. If delivery cannot be confirmed it refuses to press Enter, rather than
  submitting a half-delivered prompt.
- **Both CLIs open a "do you trust this folder?" modal** on first run in a new
  directory, which would eat the first prompt (Enter answers the dialog). Claude
  does this even under `--dangerously-skip-permissions`. Agentainer pre-trusts each
  agent's workdir: for codex in its generated `config.toml`, for claude by adding
  `hasTrustDialogAccepted` for that path in `~/.claude.json`.
- **Both collapse a long paste into a chip** rather than showing the text —
  `[Pasted text #1 +36 lines]` for claude, `[Pasted Content 2580 chars]` for codex.
  Delivery verification recognises both.

**Agents talk in tagged messages.** A message arrives inside an envelope, so the
agent always knows where it starts, where it ends, and who sent it:

```
<swarm-message from="lead" to="reviewer" id="m-eb4105" reply-to="m-3f9a1c">
Review finding for ./parse: tokenize() mishandles a trailing backslash.

    printf 'a\' | ./parse
</swarm-message>
```

To send one, the agent simply **writes a block in its reply**. The capture hook
reads it when the turn ends and delivers it — no shell, no quoting, so multi-line
bodies, code blocks and backslashes survive intact:

```
<swarm-send to="reviewer" reply-to="m-3f9a1c">
Please review src/parse.py.

    printf 'a\' | ./parse
</swarm-send>
```

`<swarm-broadcast>` (no `to`) reaches everyone the sender may talk to. The `id`
and `reply-to` attributes let agents thread a conversation instead of guessing
which question an answer belongs to. Set `message_format: plain` to go back to the
old `[swarm] message from <sender>:` header, and `parse_outbound_tags: false` to
stop reading tags out of replies.

Agents can also send from their shell, which is useful mid-task rather than at the
end of a turn (but then they must quote the text themselves):

```bash
swarm send --to reviewer "I finished the parser, please review src/parse.py"
swarm broadcast "heads up: I renamed the config module"
```

Either way the message is permission-checked against `can_talk_to`, archived under
`.swarm/inbox/<recipient>/`, and written to the event log.

The raw tmux equivalent also works, and bypasses permissions and logging:

```bash
tmux send-keys -t reviewer -l "your message" && tmux send-keys -t reviewer Enter
```

**Agents get reminded when their answer goes nowhere.** A model that was asked a
question will often just *write the answer as prose* and end its turn — and that
prose reaches nobody, because only a `<swarm-send>` block is delivered. So when an
agent owes a reply and finishes a turn without sending one, Agentainer messages it:

```
Your last turn sent no message to anyone, and lead is waiting on your answer to
message m-c73724.
...
<swarm-send to="lead" reply-to="m-c73724">
your answer here, over as many lines as you need
</swarm-send>
```

If instead the agent *tried* to send but the block was malformed, it gets the
specific diagnosis — unclosed tag, missing `to`, unknown recipient, permission
denied — so it can correct itself rather than lose the message silently.

It is reminded at most `max_reply_reminders` times (default **1**), then Agentainer
gives up and stops nagging. An agent that auto-forwards via `forward_responses_to`
is never reminded, since its words did reach someone. Turn it off per agent with
`reply_reminder: false`.

**Conversations survive a restart.** Each time an agent finishes a turn, its
conversation id is written to `<root>/.swarm/sessions.yaml`:

```yaml
agents:
  lead:
    session_id: "0c2e47e2-5110-4e69-ae45-69d8492d2084"
    type: "claude"
    transcript: "/root/.claude/projects/.../0c2e47e2-....jsonl"
    updated_at: "2026-07-09T21:00:41+00:00"
```

If the machine dies, `swarm up --resume` reattaches every agent to its own
conversation instead of starting a fresh one — it does not re-send the first
prompt, and it keeps any mail still queued for that agent:

```bash
agentainer sessions          # what is recorded, and the command that would resume it
agentainer up --resume       # reattach; agents without a recorded id start fresh
```

Claude is resumed with `--resume <id>`, codex with `resume <id>`. Set
`swarm.resume: true` to make it the default, and `--no-resume` to override.
Gemini and hermes have no recoverable session id (their turns are scraped from
the terminal), so they always start a fresh conversation, with a warning.

If your command runs the CLI through an alias or wrapper, flags cannot simply be
appended — give the full recipe:

```yaml
- name: lead
  command: "bash -ic chy3"
  resume_command: "bash -ic 'chy3 --resume {session_id}'"
```

**Permissions are a whitelist.** An agent may only message the agents in its
`can_talk_to` list. Anything else is refused with an explanatory error that the
agent sees on its own terminal. Use `can_talk_to: "*"` for "everyone else".

---

## Capturing what an agent says

Agentainer needs to know when an agent finishes a turn — both to log it and to
support auto-forwarding. How it finds out depends on the CLI, and the two
mechanisms are **not** equally good:

| `capture` | Used by | Mechanism | Reliability |
|---|---|---|---|
| `hook` | `claude`, `codex` | The CLI runs a program when a turn completes | Exact — the model's final message |
| `pane` | `gemini`, `hermes` | Poll the tmux pane, diff it once it stops changing | Heuristic — sees rendered text |
| `none` | any | Nothing is captured | — |

- **claude** → a `Stop` hook is written into `<agent-dir>/.claude/settings.json`,
  with **no `matcher` key** (`Stop` is not a tool event, and supplying one stops the
  interactive TUI from ever running the hook). It reads the session transcript.
  Claude fires the hook *before* flushing the assistant message to that transcript,
  so Agentainer polls it briefly, and only reads text written after the last user
  message — otherwise a turn would silently capture nothing, or re-relay the
  previous turn's reply.
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

### Subagents, parallel work, and busy agents

Coding agents spawn subagents and background tasks. Four things follow from that.

**A subagent that calls `swarm send` speaks as its parent.** Subagents inherit the
agent's environment, so `SWARM_AGENT` still names the parent and `can_talk_to` is
enforced against the parent. That is almost always what you want: the swarm sees
one `developer`, not five anonymous workers.

**Parallel subagents cannot garble each other.** A paste and the Enter that submits
it are two separate tmux calls, so two senders racing on one pane used to produce
one Enter submitting two concatenated messages and another submitting nothing.
Everything that types into a pane now takes a per-recipient lock, so concurrent
sends queue up instead of interleaving. Different recipients are still messaged in
parallel.

**A message that arrives while an agent is busy is queued, not lost.** The CLIs
hold it in their input box and process it when the current tool call finishes —
codex says so out loud (`Messages to be submitted after next tool call`).

**An agent that ends its turn saying "I'll respond when the subagent finishes"
gets captured twice.** With `capture: hook`, the Stop/notify hook fires when the
*agent's* turn ends. Claude's `Task` subagents run inside the turn, so the hook
waits for them. But work the agent genuinely backgrounds lets the turn end early,
and then:

- that interim message is captured, and forwarded if `forward_responses_to` is set;
- when the agent is re-invoked and finishes for real, its answer is captured and
  forwarded too. The hop counter records the hop at which the agent *received* its
  last message, so a second response does not consume an extra hop — the real
  answer is never suppressed by the loop guard.

Subagent chatter never leaks: Claude writes subagent turns into the same transcript
marked `isSidechain: true`, and the hook skips them, so what gets relayed is the
agent's own final message rather than whatever a subagent happened to say last.

With `capture: pane` (gemini, hermes) this breaks down. A quiet pane is the only
"turn finished" signal there is, so a pause while a subagent works looks exactly
like a completed turn: the interim "I'll respond when it finishes" is captured and
forwarded, then the real answer is captured separately. Raise `pane_idle_ms` above
the longest silence you expect, or set `capture: none` and have the agent call
`swarm send` when it actually has something to say.

### Busy agents and backpressure

If `b` gives `a` a task, `a` is mid-turn. When `c` then tries to task `a` as well,
the message is refused rather than dropped into a working agent's input box:

```
$ swarm send --to a "please review my diff"
xx a is busy right now (working for 42s on a task from b). Please try again after
   some time, or put your message in the queue and wait for the answer:
     swarm send --to a --queue "..."   # delivered automatically when a is free
     swarm send --to a --wait "..."    # block here until a is free
   Meanwhile you are free to do other work.
```

So `c` chooses: come back later, `--queue` it and carry on with other work, or
`--wait` and block. A queued message is delivered by the next capture hook that
fires for `a`, the moment it goes idle. Agents are told all of this in the
communication block appended to their first prompt.

```bash
swarm status          # TURN column: idle / busy 42s / untracked, plus QUEUE depth
swarm queue a         # what is waiting for a, and who sent it
swarm queue a --clear # drop it all
swarm idle a          # force a back to idle, then drain -- if a capture never fired
```

**This is safe against parallel senders.** The busy check and the "now busy" write
happen inside the same per-recipient lock as the paste, so two subagents racing to
message an idle agent cannot both pass the check — one delivers, the other is told
it is busy. A flag checked and set separately would let both through.

Some honest limits:

- Busy tracking needs a "turn finished" signal, so it only works for agents with
  `capture: hook` or `capture: pane`. A `capture: none` agent reports `untracked`
  and always accepts mail.
- With `capture: pane`, "idle" means "the pane went quiet", which a thinking agent
  can also look like. Backpressure there is a hint, not a guarantee.
- A turn started by a human typing directly into the pane is not tracked.
- If a capture never fires (crashed CLI, misconfigured hook), the agent would look
  busy forever. After `busy_timeout_ms` (default 15 minutes) it is treated as idle
  again, with a warning. `swarm idle <agent>` clears it immediately. Any mail queued
  for such an agent is not lost either: whenever some *other* agent finishes a turn,
  Agentainer sweeps the now-idle agent's queue and delivers what was stranded — so
  one missed turn-completion cannot wedge a queue permanently.
- A capture only fires if the agent's `type` matches the CLI its `command` actually
  runs. If you point a `type: codex` agent at a `claude` command (e.g. through an
  alias), it gets codex's `notify` hook, which claude never calls — its turns are
  never detected and it looks busy forever. Set `type` to whatever the command runs.
- `--force` and `--ignore-busy` deliver anyway. The agent's CLI will queue the
  message and handle it after the current tool call, so nothing is lost — you just
  give up the backpressure.

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
| `busy_timeout_ms` | `900000` | After this, a stuck "busy" agent is treated as idle |
| `message_format` | `tagged` | `tagged` XML-ish envelopes, or `plain` text headers |
| `max_reply_reminders` | `1` | How often to remind an agent that its reply reached nobody |
| `resume` | `false` | Make `up` reattach to recorded conversations by default |
| `pane_idle_ms` | `2500` | Quiet time before a `pane` turn counts as done |
| `pane_poll_ms` | `700` | Pane sampling interval |
| `pane_scrollback` | `400` | Lines of scrollback the watcher diffs |
| `tmux_history_limit` | `50000` | Scrollback kept per agent pane so you can scroll up (`0` = tmux default) |
| `tmux_mouse` | `true` | Enable mouse-wheel scrolling in the panes |

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
| `busy_check` | `true` | Refuse incoming messages while this agent is mid-turn |
| `parse_outbound_tags` | `true` | Route `<swarm-send>` blocks the agent writes in its reply |
| `reply_reminder` | `true` | Remind it when it owes a reply but sent nothing |
| `resume_args` | from type | Appended to `command` to resume, e.g. `--resume {session_id}` |
| `resume_command` | — | Full replacement command when flags can't be appended |
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
overrides the text Agentainer generates — `comms` and `task_notice` (appended to
first prompts), plus `reply_reminder` and `send_failed` (the nudges) —
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
agentainer validate -c examples/research-swarm.yaml   # look before you leap
agentainer up       -c examples/research-swarm.yaml
agentainer send --to lead "Research the state of WebGPU compute shaders."
```

`existing-repo.yaml` intentionally refuses to start until you point `workdir` at
a repository that exists.

## Commands

| Command | Purpose |
|---|---|
| `agentainer up` | Start the swarm. `--only a,b`, `--restart`, `--resume`, `--no-prompt`, `--attach` |
| `agentainer down` | Kill sessions and watchers. `--only a,b` |
| `agentainer restart` | `down` then `up` |
| `agentainer status` | Table of agents, sessions, capture mode, permissions |
| `agentainer attach <agent>` | Attach to an agent's tmux session |
| `agentainer send --to <agent> "msg"` | Deliver a message (`--from`, `--file`, `--queue`, `--wait`, `--ignore-busy`, `--force`) |
| `agentainer broadcast "msg"` | Message everyone the sender may talk to |
| `agentainer sessions` | Show each agent's recorded conversation id (`--raw`) |
| `agentainer queue <agent>` | Show what is waiting for a busy agent (`--clear`) |
| `agentainer idle <agent>` | Force an agent back to idle, then drain its queue |
| `agentainer inbox <agent>` | Print archived messages |
| `agentainer logs [agent] [-f]` | Event log: prompts, responses, messages |
| `agentainer validate` | Parse the config. `--show-prompts` renders final prompts |

`agentainer my-swarm.yaml` is shorthand for `agentainer up -c my-swarm.yaml`.
`-c` and `$SWARM_CONFIG` both select a config; `-c` wins.

## Layout

```
AgentSwarm/
├── agentainer                # entrypoint
├── agents.example.yaml     # annotated config
├── llms.txt                # reference for agents configuring this tool
├── hooks/
│   ├── claude_stop.sh      # Claude Code Stop hook
│   └── codex_notify.sh     # Codex notify program
├── lib/
│   ├── swarm.py            # tmux orchestration, routing, capture
│   ├── config.py           # schema, defaults, validation
│   └── minyaml.py          # YAML subset parser, used when PyYAML is absent
├── tests/validate.sh       # full suite: mock agents, no model calls
├── examples/               # research swarm, software company, bug hunt, pairing
└── workspace/              # created by `up`
    ├── <agent>/            # one folder per agent
    └── .swarm/
        ├── state.json      # what `up` started
    ├── sessions.yaml   # each agent's conversation id, for `up --resume`
        ├── bin/swarm       # the `swarm` command agents call
        ├── logs/           # <agent>.jsonl + swarm.jsonl
        ├── inbox/<agent>/  # archived messages
        └── run/            # watcher pids, hop counters
```

## Tests

```bash
tests/validate.sh
```

48 checks over the real code paths — tmux, hooks, locks, queues, the tag parser,
sessions and resume — driven by mock agents, so it needs no API key and costs
nothing. It covers the awkward cases: the check-and-set race between concurrent
senders, a queued message beating a reply reminder, a subagent's sidechain turn
being skipped, and a transcript read before Claude has flushed it.

## Troubleshooting

**"could not confirm the text arrived; NOT pressing Enter".** The agent's input
box never echoed the prompt, so Agentainer refused to submit it. Attach to the
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
exists and `.swarm/logs/hooks.log` for errors. If the agent also looks busy forever,
its `type` probably does not match the CLI its `command` runs (see the capture note
under [Busy agents](#busy-agents-and-backpressure)).

**Can't scroll up in an attached session.** Agentainer raises tmux's scrollback to
`tmux_history_limit` (50000 lines) and turns on `tmux_mouse`, so the wheel scrolls
the backlog; press `q` to leave copy mode. If your terminal grabs the wheel itself,
use `Ctrl-b [` then PageUp. Both options are set on the tmux server before sessions
are created, so a server that was already running keeps its old panes' smaller
buffer — restart the swarm (or that pane) to pick up the larger one.

## A note on flags

`claude --dangerously-skip-permissions`, `codex --yolo` and `gemini --yolo` let
agents act without asking for confirmation. That is usually what you want for an
unattended swarm, and it means several models are running tools unsupervised in
these directories. Point `root` somewhere disposable, and don't run a swarm over
a directory you can't afford to lose.
