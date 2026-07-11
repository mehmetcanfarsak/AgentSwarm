"""Load, normalise and validate an Agentainer YAML config."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # pragma: no cover - exercised by whichever branch is installed
    import yaml as _yaml

    def _parse_yaml(text: str):
        return _yaml.safe_load(text)

except ImportError:  # pragma: no cover
    import minyaml as _yaml  # type: ignore

    def _parse_yaml(text: str):
        return _yaml.load(text)


def parse_yaml(text: str):
    """Parse YAML with PyYAML if present, otherwise the bundled subset parser."""
    return _parse_yaml(text)


class ConfigError(Exception):
    pass


# Built-in knowledge about each supported coding agent. `capture` says how we
# learn that the agent finished a turn:
#   hook  -- the CLI can call an external program on turn completion
#   pane  -- no such facility; we poll the tmux pane and diff it
#   none  -- do not capture at all
BUILTIN_AGENT_TYPES: dict[str, dict[str, Any]] = {
    "claude": {
        "command": "claude --dangerously-skip-permissions",
        "capture": "hook",
        "boot_delay_ms": 3000,
        # Appended to `command` by `up --resume`. {session_id} is the recorded id.
        "resume_args": "--resume {session_id}",
    },
    "codex": {
        "command": "codex --yolo",
        "capture": "hook",
        "boot_delay_ms": 3000,
        "resume_args": "resume {session_id}",
    },
    "gemini": {
        "command": "gemini --yolo",
        "capture": "pane",
        "boot_delay_ms": 4000,
        # No session id is recoverable from a scraped pane, so no resume recipe.
    },
    "hermes": {
        "command": "hermes",
        "capture": "pane",
        "boot_delay_ms": 3000,
    },
}

VALID_CAPTURE = ("hook", "pane", "none", "auto")

VALID_MESSAGE_FORMATS = ("tagged", "plain")

NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")

DEFAULT_COMMS_TEMPLATE = """\
---
## Inter-agent communication protocol

You are the agent **{agent}** on the **{swarm}** team.
Agents you are allowed to message: {peers}

### Receiving

Messages arrive in your prompt inside a tag, so you can always tell where one
starts and ends:

    <swarm-message from="SOMEONE" id="m-1a2b3c">
    the message body,
    which may span many lines
    </swarm-message>

Note the `id`. Quote it as `reply-to` when you answer, so the other agent knows
which of its questions you are answering. Every message is also archived under
`{inbox}` if you need to re-read one.

### Sending

Sending is a deliberate act, not something every turn needs. **Most of your turns
will contain no tag at all** -- you think, run commands, and work in your own
directory in plain text, exactly as you normally would. Write a message block ONLY
when you actually want to hand something to another agent: a question, a result, or
a handoff. When you have nothing to send, send nothing.

When you do want to send, write this block **in your reply**. It is read as soon as
your turn ends, then delivered. Do not escape or quote the body -- write it
literally, across as many lines as you need:

    <swarm-send to="AGENT_NAME" reply-to="m-1a2b3c">
    Your message. Multiple lines, code blocks and quotes are all fine.
    </swarm-send>

To reach everyone you are allowed to talk to, use `<swarm-broadcast>` with no `to`.

The `reply-to` attribute is optional; drop it when you are starting a new thread.
Quoting it also marks your message as an answer, so the other agent is not
chased for a reply to it. For an announcement that needs no answer, either use
`<swarm-broadcast>` or add `expects-reply="false"`.

You may also send from your shell, which is useful mid-task rather than at the
end of a turn (but you must then quote the text yourself):

    swarm send --to AGENT_NAME "MESSAGE TEXT"
    swarm broadcast "MESSAGE TEXT"

### When the other agent is busy

An agent already working on a task will refuse your message. That is normal. A
tagged `<swarm-send>` is queued for them automatically. From the shell you choose:

    swarm send --to AGENT_NAME --queue "MESSAGE TEXT"   # delivered when they are free
    swarm send --to AGENT_NAME --wait  "MESSAGE TEXT"   # block until they are free
    swarm queue AGENT_NAME                              # see what is waiting for them

Prefer queueing, then get on with other work. Never spin in a retry loop.

### Rules

  * `AGENT_NAME`, `MESSAGE TEXT` and `m-1a2b3c` above are placeholders. Replace
    them with a real agent from the list and what you actually want to say. A
    `<swarm-send>` addressed to a name that is not an agent is discarded.
  * Only message the agents listed above. Messaging anyone else will be refused.
  * Keep messages self-contained -- the other agent does not see your screen,
    your files, or your reasoning.
  * Do not message an agent merely to acknowledge. Send when you have a question,
    a result, or a handoff.
  * Write a full tag block only when you actually want to send. A complete
    `<swarm-send to="...">...</swarm-send>` you write is delivered, so do not paste
    a filled-in example to illustrate a point -- it would be sent for real. Naming
    the tag in passing while you explain your plan is fine.
  * A turn with no tag simply sends nothing, which is normal and expected. The one
    exception: if another agent asked YOU something and your turn ends with no
    block, your answer reaches nobody, so you get a single reminder. Never add a
    block just to satisfy the reminder -- only to actually answer.
"""

DEFAULT_REPLY_REMINDER_TEMPLATE = """\
Your last turn sent no message to anyone, and {sender} is waiting on your answer
to message {id}.

{problems}

Anything you write outside a message block stays on your own screen. The other
agent cannot see your reasoning, your files, or your reply. To actually send it,
put your answer inside a block exactly like this:

<swarm-send to="{sender}" reply-to="{id}">
your answer here, over as many lines as you need
</swarm-send>

Agents you may message: {peers}

If you already wrote the answer, write it again inside that block. If you
deliberately have nothing to send, ignore this -- you will not be reminded again.
"""

DEFAULT_SEND_FAILED_TEMPLATE = """\
Your last turn tried to send a message, but it could not be delivered.

{problems}

Fix it and send again. The block must look exactly like this, closing tag included:

<swarm-send to="AGENT_NAME">
your message here
</swarm-send>

Agents you may message: {peers}

You will not be reminded again.
"""

DEFAULT_TASK_NOTICE_TEMPLATE = """\
---
## Standby

Do not start any work yet. Your actual task will be sent to you in the **next**
prompt. For now, reply with one plain sentence confirming you understood your role
-- no message block and no tags, this confirmation is just for your own screen and
is not sent to anyone -- then wait.
"""


@dataclass
class Agent:
    name: str
    type: str
    command: str
    workdir: Path
    session: str
    capture: str
    boot_delay_ms: int
    first_prompt: str
    can_talk_to: list[str] = field(default_factory=list)
    forward_responses_to: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    append_peers_prompt: bool = True
    append_task_notice: bool = False
    ready_probe: bool = True
    create_workdir: bool = True
    busy_check: bool = True
    parse_outbound_tags: bool = True
    reply_reminder: bool = True
    resume_args: str | None = None
    resume_command: str | None = None


@dataclass
class SwarmConfig:
    path: Path
    name: str
    root: Path
    session_prefix: str
    agents: list[Agent]
    enter_delay_ms: int = 250
    send_delay_ms: int = 150
    max_forward_hops: int = 3
    ready_timeout_ms: int = 60000
    busy_timeout_ms: int = 900000
    message_format: str = "tagged"
    max_reply_reminders: int = 1
    resume: bool = False
    reply_reminder_template: str = DEFAULT_REPLY_REMINDER_TEMPLATE
    send_failed_template: str = DEFAULT_SEND_FAILED_TEMPLATE
    pane_idle_ms: int = 2500
    pane_poll_ms: int = 700
    pane_scrollback: int = 400
    tmux_history_limit: int = 50000
    tmux_mouse: bool = True
    supervise: bool = True
    supervise_interval_ms: int = 15000
    warnings: list[str] = field(default_factory=list)

    @property
    def runtime(self) -> Path:
        return self.root / ".swarm"

    @property
    def log_dir(self) -> Path:
        return self.runtime / "logs"

    @property
    def inbox_dir(self) -> Path:
        return self.runtime / "inbox"

    @property
    def run_dir(self) -> Path:
        return self.runtime / "run"

    @property
    def bin_dir(self) -> Path:
        return self.runtime / "bin"

    @property
    def sessions_file(self) -> Path:
        """Where each agent's conversation id is recorded, so `up --resume` works."""
        return self.runtime / "sessions.yaml"

    def get(self, name: str) -> Agent:
        for agent in self.agents:
            if agent.name == name:
                return agent
        known = ", ".join(a.name for a in self.agents)
        raise ConfigError(f"unknown agent {name!r} (known agents: {known})")

    def names(self) -> list[str]:
        return [a.name for a in self.agents]


def _as_list(value: Any, ctx: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise ConfigError(f"{ctx}: expected a string or a list, got {type(value).__name__}")


def _as_bool(value: Any, default: bool, ctx: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{ctx}: expected true/false, got {value!r}")


def _as_str_map(value: Any, ctx: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{ctx}: expected a mapping")
    return {str(k): str(v) for k, v in value.items()}


def load(path: str | os.PathLike) -> SwarmConfig:
    cfg_path = Path(path).expanduser().resolve()
    if not cfg_path.is_file():
        raise ConfigError(
            f"config file not found: {cfg_path}\n"
            "   Create one with:  cp agents.example.yaml agents.yaml\n"
            "   Or point at it:   agentainer -c /path/to/swarm.yaml up"
        )

    try:
        data = _parse_yaml(cfg_path.read_text())
    except Exception as exc:  # noqa: BLE001 - surface parser errors verbatim
        raise ConfigError(f"could not parse {cfg_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"{cfg_path}: top level must be a mapping")

    swarm = data.get("swarm") or {}
    if not isinstance(swarm, dict):
        raise ConfigError("`swarm:` must be a mapping")

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ConfigError("`defaults:` must be a mapping")

    templates = data.get("templates") or {}
    comms_tpl = templates.get("comms") or DEFAULT_COMMS_TEMPLATE
    notice_tpl = templates.get("task_notice") or DEFAULT_TASK_NOTICE_TEMPLATE
    reminder_tpl = templates.get("reply_reminder") or DEFAULT_REPLY_REMINDER_TEMPLATE
    failed_tpl = templates.get("send_failed") or DEFAULT_SEND_FAILED_TEMPLATE

    # Agent type registry: built-ins, overridable and extensible from YAML.
    types: dict[str, dict[str, Any]] = {
        k: dict(v) for k, v in BUILTIN_AGENT_TYPES.items()
    }
    for tname, tconf in (data.get("agent_types") or {}).items():
        if not isinstance(tconf, dict):
            raise ConfigError(f"agent_types.{tname}: must be a mapping")
        types.setdefault(tname, {}).update(tconf)

    root_raw = swarm.get("root") or "./workspace"
    root = Path(os.path.expanduser(str(root_raw)))
    if not root.is_absolute():
        root = (cfg_path.parent / root).resolve()

    prefix = str(swarm.get("session_prefix") or "")
    swarm_name = str(swarm.get("name") or cfg_path.stem)
    create_workdirs = _as_bool(swarm.get("create_workdirs"), True, "swarm.create_workdirs")

    message_format = str(swarm.get("message_format") or "tagged")
    if message_format not in VALID_MESSAGE_FORMATS:
        raise ConfigError(
            f"swarm.message_format must be one of {', '.join(VALID_MESSAGE_FORMATS)}"
        )

    raw_agents = data.get("agents")
    if not raw_agents:
        raise ConfigError("`agents:` must contain at least one agent")
    if not isinstance(raw_agents, list):
        raise ConfigError("`agents:` must be a list")

    # Pass 1: materialise agents without resolving peer references.
    agents: list[Agent] = []
    seen: set[str] = set()
    # Warnings collected while resolving each agent (e.g. capture upgrades).
    capture_warnings: list[str] = []
    for index, raw in enumerate(raw_agents):
        if not isinstance(raw, dict):
            raise ConfigError(f"agents[{index}]: must be a mapping")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ConfigError(f"agents[{index}]: missing `name`")
        if not NAME_RE.match(name):
            raise ConfigError(
                f"agent {name!r}: name must match {NAME_RE.pattern} "
                "(it is used as a tmux session name and a directory name)"
            )
        if name in seen:
            raise ConfigError(f"duplicate agent name: {name!r}")
        seen.add(name)

        atype = str(raw.get("type") or defaults.get("type") or "claude")
        if atype not in types:
            raise ConfigError(
                f"agent {name!r}: unknown type {atype!r}. "
                f"Known types: {', '.join(sorted(types))}. "
                "Define new ones under `agent_types:`."
            )
        tconf = types[atype]

        command = raw.get("command") or tconf.get("command")
        if not command:
            raise ConfigError(f"agent {name!r}: no `command` and type {atype!r} has none")

        capture = str(
            raw.get("capture") or defaults.get("capture") or "auto"
        )
        if capture not in VALID_CAPTURE:
            raise ConfigError(
                f"agent {name!r}: capture must be one of {', '.join(VALID_CAPTURE)}"
            )
        if capture == "auto":
            capture = str(tconf.get("capture") or "pane")
        # capture: none on a type that HAS a completion hook (claude/codex) removes
        # the agent's only turn-completion signal and leaves the orchestrator blind
        # to a silent turn -- which can wedge the whole swarm. Auto-upgrade to the
        # type's natural capture so the hook keeps the orchestrator informed (and
        # re-enables busy_check / tag parsing / reply reminders, force-disabled for
        # capture: none below). gemini/hermes deliberately keep capture: none.
        if capture == "none" and str(tconf.get("capture")) == "hook":
            capture = "hook"
            capture_warnings.append(
                f"agent {name!r}: capture: none on a {atype} agent gives the "
                f"orchestrator no turn-completion signal -- auto-upgraded to "
                f"capture: hook. Use capture: pane (gemini/hermes) for deliberate-send."
            )

        boot = raw.get("boot_delay_ms")
        if boot is None:
            boot = defaults.get("boot_delay_ms")
        if boot is None:
            boot = tconf.get("boot_delay_ms", 5000)

        first_prompt = raw.get("first_prompt")
        prompt_file = raw.get("first_prompt_file")
        if prompt_file:
            if first_prompt:
                raise ConfigError(
                    f"agent {name!r}: set either `first_prompt` or `first_prompt_file`, not both"
                )
            fp = Path(os.path.expanduser(str(prompt_file)))
            if not fp.is_absolute():
                fp = cfg_path.parent / fp
            if not fp.is_file():
                raise ConfigError(f"agent {name!r}: first_prompt_file not found: {fp}")
            first_prompt = fp.read_text()
        first_prompt = (first_prompt or "").strip()

        workdir_raw = raw.get("workdir") or defaults.get("workdir")
        if workdir_raw:
            # {name}, {root} and {swarm} let one `defaults.workdir` serve every agent.
            try:
                expanded = str(workdir_raw).format(
                    name=name, root=str(root), swarm=swarm_name, type=atype
                )
            except (KeyError, IndexError) as exc:
                raise ConfigError(
                    f"agent {name!r}: unknown placeholder in workdir {workdir_raw!r}: {exc}. "
                    "Available: {name} {root} {swarm} {type}"
                ) from exc
            workdir = Path(os.path.expanduser(expanded))
            if not workdir.is_absolute():
                workdir = (cfg_path.parent / workdir).resolve()
        else:
            workdir = root / name

        create_workdir = _as_bool(
            raw.get("create_workdir", defaults.get("create_workdir", create_workdirs)),
            True,
            f"agent {name}: create_workdir",
        )

        env = dict(_as_str_map(defaults.get("env"), "defaults.env"))
        env.update(_as_str_map(tconf.get("env"), f"agent_types.{atype}.env"))
        env.update(_as_str_map(raw.get("env"), f"agent {name}: env"))

        agents.append(
            Agent(
                name=name,
                type=atype,
                command=str(command),
                workdir=workdir,
                session=f"{prefix}{name}",
                capture=capture,
                boot_delay_ms=int(boot),
                first_prompt=first_prompt,
                can_talk_to=_as_list(
                    raw.get("can_talk_to", defaults.get("can_talk_to")),
                    f"agent {name}: can_talk_to",
                ),
                forward_responses_to=_as_list(
                    raw.get("forward_responses_to", defaults.get("forward_responses_to")),
                    f"agent {name}: forward_responses_to",
                ),
                env=env,
                create_workdir=create_workdir,
                # Busy tracking needs a "turn finished" signal, which only exists
                # when the agent is captured. capture: none => always accept mail.
                busy_check=_as_bool(
                    raw.get("busy_check", defaults.get("busy_check")),
                    True,
                    f"agent {name}: busy_check",
                )
                and capture != "none",
                # Routing <swarm-send> blocks means reading what the agent said,
                # which is exactly what capture provides.
                parse_outbound_tags=_as_bool(
                    raw.get("parse_outbound_tags", defaults.get("parse_outbound_tags")),
                    True,
                    f"agent {name}: parse_outbound_tags",
                )
                and capture != "none"
                and message_format == "tagged",
                # Reminding an agent to use the tags only makes sense if we read
                # them back and can see that it did not.
                reply_reminder=_as_bool(
                    raw.get("reply_reminder", defaults.get("reply_reminder")),
                    True,
                    f"agent {name}: reply_reminder",
                ),
                resume_args=raw.get("resume_args") or defaults.get("resume_args") or tconf.get("resume_args"),
                resume_command=raw.get("resume_command") or defaults.get("resume_command") or tconf.get("resume_command"),
                ready_probe=_as_bool(
                    raw.get("ready_probe", defaults.get("ready_probe")),
                    True,
                    f"agent {name}: ready_probe",
                ),
                append_peers_prompt=_as_bool(
                    raw.get(
                        "append_agents_that_you_can_talk_to_prompt",
                        defaults.get("append_agents_that_you_can_talk_to_prompt"),
                    ),
                    True,
                    f"agent {name}: append_agents_that_you_can_talk_to_prompt",
                ),
                append_task_notice=_as_bool(
                    raw.get(
                        "in_first_prompt_append_your_task_will_be_sent_in_the_next_prompt",
                        defaults.get(
                            "in_first_prompt_append_your_task_will_be_sent_in_the_next_prompt"
                        ),
                    ),
                    False,
                    f"agent {name}: in_first_prompt_append_your_task_will_be_sent_in_the_next_prompt",
                ),
            )
        )

    all_names = [a.name for a in agents]

    # Pass 2: expand wildcards and validate the communication graph.
    for agent in agents:
        if "*" in agent.can_talk_to:
            agent.can_talk_to = [n for n in all_names if n != agent.name]
        for peer in agent.can_talk_to:
            if peer not in all_names:
                raise ConfigError(
                    f"agent {agent.name!r}: can_talk_to references unknown agent {peer!r}"
                )
            if peer == agent.name:
                raise ConfigError(f"agent {agent.name!r}: cannot be in its own can_talk_to")

        if "*" in agent.forward_responses_to:
            agent.forward_responses_to = list(agent.can_talk_to)
        for peer in agent.forward_responses_to:
            if peer not in all_names:
                raise ConfigError(
                    f"agent {agent.name!r}: forward_responses_to references unknown agent {peer!r}"
                )
            if peer not in agent.can_talk_to:
                raise ConfigError(
                    f"agent {agent.name!r}: forward_responses_to includes {peer!r}, "
                    "which is not in its can_talk_to list"
                )
        if agent.forward_responses_to and agent.capture == "none":
            raise ConfigError(
                f"agent {agent.name!r}: forward_responses_to needs capture to be enabled "
                "(set capture: hook or capture: pane)"
            )

    # Pass 2b: working directories. They may be auto-created under `root`, or
    # point at an existing project -- possibly one shared by several agents.
    warnings: list[str] = []
    warnings.extend(capture_warnings)
    for agent in agents:
        # No tag parsing means no way to know whether it replied.
        if not agent.parse_outbound_tags:
            agent.reply_reminder = False

    for agent in agents:
        if agent.workdir.exists() and not agent.workdir.is_dir():
            raise ConfigError(
                f"agent {agent.name!r}: workdir is not a directory: {agent.workdir}"
            )
        if not agent.workdir.exists() and not agent.create_workdir:
            raise ConfigError(
                f"agent {agent.name!r}: workdir does not exist: {agent.workdir}\n"
                "   Create it yourself, or allow Agentainer to: create_workdir: true"
            )

    shared: dict[Path, list[str]] = {}
    for agent in agents:
        shared.setdefault(agent.workdir.resolve(), []).append(agent.name)
    for directory, names in shared.items():
        if len(names) > 1:
            warnings.append(
                f"agents {', '.join(names)} share the working directory {directory} -- "
                "they can overwrite each other's files, and a shared git checkout will "
                "interleave their commits"
            )

    cfg = SwarmConfig(
        path=cfg_path,
        name=swarm_name,
        warnings=warnings,
        root=root,
        session_prefix=prefix,
        agents=agents,
        enter_delay_ms=int(swarm.get("enter_delay_ms", 250)),
        send_delay_ms=int(swarm.get("send_delay_ms", 150)),
        max_forward_hops=int(swarm.get("max_forward_hops", 3)),
        ready_timeout_ms=int(swarm.get("ready_timeout_ms", 60000)),
        busy_timeout_ms=int(swarm.get("busy_timeout_ms", 900000)),
        message_format=message_format,
        max_reply_reminders=int(swarm.get("max_reply_reminders", 1)),
        resume=_as_bool(swarm.get("resume"), False, "swarm.resume"),
        reply_reminder_template=reminder_tpl,
        send_failed_template=failed_tpl,
        pane_idle_ms=int(swarm.get("pane_idle_ms", 2500)),
        pane_poll_ms=int(swarm.get("pane_poll_ms", 700)),
        pane_scrollback=int(swarm.get("pane_scrollback", 400)),
        tmux_history_limit=int(swarm.get("tmux_history_limit", 50000)),
        tmux_mouse=_as_bool(swarm.get("tmux_mouse"), True, "swarm.tmux_mouse"),
        supervise=_as_bool(swarm.get("supervise"), True, "swarm.supervise"),
        supervise_interval_ms=int(swarm.get("supervise_interval_ms", 15000)),
    )

    # Pass 3: build the full first prompt for each agent.
    for agent in agents:
        cfg_get = {
            "agent": agent.name,
            "swarm": cfg.name,
            "prefix": cfg.session_prefix,
            "peers": ", ".join(agent.can_talk_to) or "none (you are isolated)",
            "inbox": str(cfg.inbox_dir / agent.name),
            "workdir": str(agent.workdir),
        }
        parts = [agent.first_prompt] if agent.first_prompt else []
        try:
            if agent.append_peers_prompt:
                parts.append(comms_tpl.format(**cfg_get).strip())
            if agent.append_task_notice:
                parts.append(notice_tpl.format(**cfg_get).strip())
        except (KeyError, IndexError, ValueError) as exc:
            raise ConfigError(
                f"agent {agent.name!r}: a template placeholder is not recognised: {exc}. "
                f"Available: {', '.join(sorted(cfg_get))}"
            ) from exc
        agent.first_prompt = "\n\n".join(p.strip() for p in parts if p.strip())

    return cfg
