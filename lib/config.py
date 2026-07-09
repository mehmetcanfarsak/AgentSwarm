"""Load, normalise and validate an AgentSwarm YAML config."""

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
        "boot_delay_ms": 5000,
    },
    "codex": {
        "command": "codex --yolo",
        "capture": "hook",
        "boot_delay_ms": 5000,
    },
    "gemini": {
        "command": "gemini --yolo",
        "capture": "pane",
        "boot_delay_ms": 6000,
    },
    "hermes": {
        "command": "hermes",
        "capture": "pane",
        "boot_delay_ms": 5000,
    },
}

VALID_CAPTURE = ("hook", "pane", "none", "auto")

NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")

DEFAULT_COMMS_TEMPLATE = """\
---
## Swarm communication protocol

You are the agent **{agent}** in the "{swarm}" swarm.
Agents you are allowed to message: {peers}

To send a message (preferred -- it is permission-checked and logged), run in your shell:

    swarm send --to <agent> "your message here"

To message everyone you are allowed to talk to:

    swarm broadcast "your message here"

Raw tmux equivalent (works, but bypasses permission checks and logging):

    tmux send-keys -t {prefix}<agent> -l "your message here" && tmux send-keys -t {prefix}<agent> Enter

Incoming messages are typed straight into your prompt, prefixed with
`[swarm] message from <agent>`. Every message is also archived under
`{inbox}` if you need to re-read one.

Rules:
  * Only message the agents listed above. Messaging anyone else will be refused.
  * State clearly who you are replying to, and keep messages self-contained --
    the other agent does not see your screen or your files.
  * Do not message an agent just to acknowledge; only send a message when you
    have a question, a result, or a handoff.
"""

DEFAULT_TASK_NOTICE_TEMPLATE = """\
---
## Standby

Do not start any work yet. Your actual task will be sent to you in the **next**
prompt. For now, briefly confirm that you have understood your role and are
ready, then wait.
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
    pane_idle_ms: int = 2500
    pane_poll_ms: int = 700
    pane_scrollback: int = 400

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
            "   Or point at it:   swarm.sh -c /path/to/swarm.yaml up"
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

    raw_agents = data.get("agents")
    if not raw_agents:
        raise ConfigError("`agents:` must contain at least one agent")
    if not isinstance(raw_agents, list):
        raise ConfigError("`agents:` must be a list")

    # Pass 1: materialise agents without resolving peer references.
    agents: list[Agent] = []
    seen: set[str] = set()
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

        workdir_raw = raw.get("workdir")
        if workdir_raw:
            workdir = Path(os.path.expanduser(str(workdir_raw)))
            if not workdir.is_absolute():
                workdir = (cfg_path.parent / workdir).resolve()
        else:
            workdir = root / name

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

    cfg = SwarmConfig(
        path=cfg_path,
        name=swarm_name,
        root=root,
        session_prefix=prefix,
        agents=agents,
        enter_delay_ms=int(swarm.get("enter_delay_ms", 250)),
        send_delay_ms=int(swarm.get("send_delay_ms", 150)),
        max_forward_hops=int(swarm.get("max_forward_hops", 3)),
        ready_timeout_ms=int(swarm.get("ready_timeout_ms", 60000)),
        pane_idle_ms=int(swarm.get("pane_idle_ms", 2500)),
        pane_poll_ms=int(swarm.get("pane_poll_ms", 700)),
        pane_scrollback=int(swarm.get("pane_scrollback", 400)),
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
        if agent.append_peers_prompt:
            parts.append(comms_tpl.format(**cfg_get).strip())
        if agent.append_task_notice:
            parts.append(notice_tpl.format(**cfg_get).strip())
        agent.first_prompt = "\n\n".join(p.strip() for p in parts if p.strip())

    return cfg
