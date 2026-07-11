#!/usr/bin/env python3
"""Agentainer -- run a swarm of coding agents in tmux and let them talk.

Invoked through ``agentainer`` (or ``./agentainer`` from a clone); see
``agentainer --help`` and README.md.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:  # POSIX only, which is fine: tmux is too.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfgmod  # noqa: E402
from config import Agent, ConfigError, SwarmConfig  # noqa: E402

SWARM_HOME = Path(os.environ.get("SWARM_HOME") or Path(__file__).resolve().parents[1])
HOOKS_DIR = SWARM_HOME / "hooks"

# Agent types whose CLI can invoke an external program when a turn completes.
HOOK_CAPABLE = ("claude", "codex")


# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------


class SwarmError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def info(msg: str) -> None:
    print(f"\033[36m::\033[0m {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"\033[33m!!\033[0m {msg}", file=sys.stderr)


def die(msg: str) -> "None":
    print(f"\033[31mxx\033[0m {msg}", file=sys.stderr)
    raise SystemExit(1)


def sleep_ms(ms: int) -> None:
    if ms > 0:
        time.sleep(ms / 1000.0)


# --------------------------------------------------------------------------
# tmux
# --------------------------------------------------------------------------


def tmux(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    if not shutil.which("tmux"):
        raise SwarmError("tmux is not installed or not on PATH")
    return subprocess.run(
        ["tmux", *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


LOCK_TIMEOUT_S = 180


@contextlib.contextmanager
def file_lock(cfg: SwarmConfig, name: str, what: str = "lock"):
    """An advisory cross-process lock, used to serialise access to one pane/queue.

    Lock ordering, to keep it deadlock-free: queue -> pane -> turn state.
    """
    if fcntl is None:  # pragma: no cover
        yield
        return

    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    handle = open(cfg.run_dir / f"{name}.{what}", "w")
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    locked = False
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() > deadline:
                    warn(f"{name}: timed out waiting for the {what}; proceeding anyway")
                    break
                time.sleep(0.05)
        yield
    finally:
        if locked:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def pane_lock(cfg: SwarmConfig, session: str):
    """Serialise everything that types into one pane.

    A paste and the Enter that submits it are two separate tmux calls. Without a
    lock, a second sender -- another agent, or one of several subagents running
    in parallel inside the same agent -- can paste in between them, so one Enter
    submits two concatenated messages and the other submits nothing.

    The busy check and the "mark this agent busy" write also happen under this
    lock, so two concurrent senders cannot both observe an idle agent and both
    deliver to it.

    The lock is per recipient, so unrelated agents are still messaged in parallel.
    """
    return file_lock(cfg, session, "pane.lock")


# --------------------------------------------------------------------------
# turn state: is an agent mid-task?
# --------------------------------------------------------------------------
#
# We know when a turn starts (we pressed Enter) and when one ends (the capture
# hook fires). Two counters rather than a boolean, because a bare flag is racy:
# the hook that ends turn N can land after a message delivered *during* turn N,
# and would then clear a "busy" that belongs to the next turn.


class BusyError(SwarmError):
    """The recipient is mid-task and is not accepting messages right now."""


def turn_state(cfg: SwarmConfig, agent: str) -> dict:
    try:
        return json.loads((cfg.run_dir / f"{agent}.turn.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {"delivered": 0, "completed": 0, "since": 0, "by": None}


def write_turn_state(cfg: SwarmConfig, agent: str, state: dict) -> None:
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    (cfg.run_dir / f"{agent}.turn.json").write_text(json.dumps(state))


def busy_info(cfg: SwarmConfig, agent: Agent) -> dict | None:
    """Return the turn state if *agent* is mid-task, else None. Not locked."""
    if not agent.busy_check:
        return None
    state = turn_state(cfg, agent.name)
    if state.get("delivered", 0) <= state.get("completed", 0):
        return None

    age_ms = (time.time() - state.get("since", 0)) * 1000
    if age_ms > cfg.busy_timeout_ms:
        # The hook never fired -- crashed agent, killed CLI, capture misconfigured.
        # Fail open rather than wedge the swarm forever.
        warn(
            f"{agent.name}: has looked busy for {int(age_ms / 1000)}s "
            f"(over busy_timeout_ms); treating it as idle"
        )
        return None
    state["age_s"] = int(age_ms / 1000)
    return state


def mark_turn_started(cfg: SwarmConfig, agent: str, sender: str) -> None:
    state = turn_state(cfg, agent)
    state["delivered"] = state.get("delivered", 0) + 1
    state["since"] = time.time()
    state["by"] = sender
    write_turn_state(cfg, agent, state)


def mark_turn_finished(cfg: SwarmConfig, agent: str) -> None:
    """The agent finished a turn: everything submitted so far is consumed.

    Clamping (rather than incrementing) keeps the counters from drifting when a
    CLI folds a queued message into the turn already running -- codex does this,
    printing "messages to be submitted after next tool call". Drift would leave
    an agent permanently "busy".
    """
    with file_lock(cfg, agent, "turn.lock"):
        state = turn_state(cfg, agent)
        state["completed"] = state.get("delivered", 0)
        write_turn_state(cfg, agent, state)


def busy_message(cfg: SwarmConfig, agent: Agent, state: dict) -> str:
    by = state.get("by") or "someone"
    return (
        f"{agent.name} is busy right now (working for {state['age_s']}s on a task "
        f"from {by}). Please try again after some time, or put your message in the "
        f"queue and wait for the answer:\n"
        f"  swarm send --to {agent.name} --queue \"...\"   "
        f"# delivered automatically when {agent.name} is free\n"
        f"  swarm send --to {agent.name} --wait \"...\"    "
        f"# block here until {agent.name} is free\n"
        f"Meanwhile you are free to do other work."
    )


# --------------------------------------------------------------------------
# per-agent message queue
# --------------------------------------------------------------------------


def queue_path(cfg: SwarmConfig, agent: str) -> Path:
    return cfg.run_dir / f"{agent}.queue.jsonl"


def queue_read(cfg: SwarmConfig, agent: str) -> list[dict]:
    try:
        return [json.loads(l) for l in queue_path(cfg, agent).read_text().splitlines() if l.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def queue_write(cfg: SwarmConfig, agent: str, items: list[dict]) -> None:
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    queue_path(cfg, agent).write_text("".join(json.dumps(i) + "\n" for i in items))


def enqueue(
    cfg: SwarmConfig,
    sender: str,
    recipient: str,
    text: str,
    hops: int,
    reply_to: str | None = None,
    expects_reply: bool = True,
) -> tuple[str, int]:
    item_id = f"{time.time():.6f}-{os.getpid()}"
    with file_lock(cfg, recipient, "queue.lock"):
        items = queue_read(cfg, recipient)
        items.append(
            {
                "id": item_id,
                "from": sender,
                "text": text,
                "hops": hops,
                "reply_to": reply_to,
                "expects_reply": expects_reply,
                "ts": now_iso(),
            }
        )
        queue_write(cfg, recipient, items)
        depth = len(items)
    log_event(cfg, recipient, "queued", **{"from": sender}, depth=depth, text=text)
    return item_id, depth


def drain_queue(cfg: SwarmConfig, agent: Agent) -> bool:
    """Deliver the next queued message, now that the agent has gone idle.

    Returns True if one was handed over -- the agent is then busy again, and the
    rest of the queue waits for its next turn to end.
    """
    with file_lock(cfg, agent.name, "queue.lock"):
        items = queue_read(cfg, agent.name)
        if not items:
            return False
        head = items[0]
        try:
            deliver(
                cfg,
                head["from"],
                agent.name,
                head["text"],
                hops=head.get("hops", 0),
                enforce_acl=False,  # it was checked when the message was queued
                reply_to=head.get("reply_to"),
                expects_reply=head.get("expects_reply", True),
            )
        except BusyError:
            return False  # somebody else got there first; try again at the next idle
        except SwarmError as exc:
            warn(f"{agent.name}: could not deliver queued message: {exc}")
            return False
        queue_write(cfg, agent.name, items[1:])
    info(f"{agent.name}: delivered queued message from {head['from']} ({len(items) - 1} left)")
    return True


def sweep_stale_queues(cfg: SwarmConfig, exclude: str | None = None) -> None:
    """Drain queued mail for any *other* agent that is no longer busy.

    A message is queued when its recipient is busy, and normally drains when that
    recipient finishes its own turn. But if the recipient's capture never fires --
    a crashed CLI, or a `type` whose hook does not match the `command` actually
    running -- that turn end never arrives and the message is stranded forever.
    Since turn completions are the only thing that wakes this process, every one is
    an opportunity to also hand over anything stuck for an agent that has since gone
    idle (busy_info fails a stale-busy agent open once busy_timeout_ms passes).
    """
    for other in cfg.agents:
        if other.name == exclude:
            continue
        if not queue_read(cfg, other.name):
            continue
        if busy_info(cfg, other) is not None:
            continue  # legitimately mid-turn; its own turn end will drain it
        try:
            drain_queue(cfg, other)
        except SwarmError as exc:
            warn(f"{other.name}: could not drain stranded queue: {exc}")


def configure_tmux(cfg: SwarmConfig) -> str | None:
    """Set the globals the swarm's panes inherit, before any agent pane is created.

    history-limit is only consulted when a pane is spawned, and the default (2000
    lines) is far too small to hold a long multi-agent conversation -- the user
    attaches, tries to scroll up, and the early messages are already gone. mouse
    mode lets the wheel scroll that backlog.

    Both are global options, but a tmux server with no sessions exits immediately,
    so `set -g` on a cold server (the normal state at `up`) does nothing. We hold
    the server up with a throwaway session while setting them; the agent panes
    created afterwards inherit the values. Returns the holder session name so the
    caller can tear it down once real sessions keep the server alive; None if there
    was nothing to configure. Best effort throughout: never block the swarm coming up.
    """
    if cfg.tmux_history_limit <= 0 and not cfg.tmux_mouse:
        return None
    holder = f"{cfg.session_prefix}swarm_setup"
    tmux("new-session", "-d", "-s", holder, "sleep 86400", check=False)
    if cfg.tmux_history_limit > 0:
        tmux("set-option", "-g", "history-limit", str(cfg.tmux_history_limit), check=False)
    if cfg.tmux_mouse:
        tmux("set-option", "-g", "mouse", "on", check=False)
    return holder


def session_exists(session: str) -> bool:
    try:
        tmux("has-session", "-t", f"={session}", capture=True)
        return True
    except subprocess.CalledProcessError:
        return False


PASTE_ATTEMPTS = 2
VERIFY_TIMEOUT_MS = 3000
VERIFY_SCROLLBACK = 200
NEEDLE_LEN = 28
# Both CLIs collapse a long paste into a chip instead of showing the text:
#   claude -> "[Pasted text #1 +36 lines]"
#   codex  -> "[Pasted Content 2580 chars]"
# Whitespace is stripped before matching, so this sees "Pastedtext" / "PastedContent".
PASTE_CHIP_RE = re.compile(r"pasted(?:text|content)", re.IGNORECASE)


def pane_text(session: str, scrollback: int = 0) -> str:
    args = ["capture-pane", "-p", "-t", session]
    if scrollback:
        args[2:2] = ["-S", f"-{scrollback}"]
    try:
        return tmux(*args, capture=True).stdout or ""
    except subprocess.CalledProcessError:
        return ""


def visible_pane(session: str) -> str:
    return pane_text(session)


def needle_for(body: str) -> str:
    """The *tail* of the text, which is what stays on screen after a paste.

    Using the head would be wrong: a long prompt pushes its own first line out
    of the pane, and the cursor -- hence the visible end of the text -- sits at
    the bottom.
    """
    return normalise(body)[-NEEDLE_LEN:]


def paste_score(session: str, needle: str) -> int:
    """How many times the text we are about to send already appears on screen."""
    pane = normalise(pane_text(session, VERIFY_SCROLLBACK))
    return pane.count(needle) + len(PASTE_CHIP_RE.findall(pane))


def send_buffer(session: str, body: str) -> None:
    buf = f"swarm-{os.getpid()}-{int(time.time() * 1000)}"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write(body)
        tmp = fh.name
    try:
        tmux("load-buffer", "-b", buf, tmp)
        tmux("paste-buffer", "-b", buf, "-d", "-p", "-t", session)
    finally:
        os.unlink(tmp)


def paste_into(
    cfg: SwarmConfig, session: str, text: str, enter: bool = True, needle: str | None = None
) -> bool:
    """Type *text* into a session's pane, confirm it arrived, then press Enter.

    Bracketed paste (``paste-buffer -p``) lets a multi-line prompt land in the
    agent's input box as one block instead of being submitted line by line.

    Confirming matters: a TUI can silently discard keystrokes while it is still
    starting up, and Claude Code does exactly that for several seconds partway
    through boot. So we compare the pane before and after the paste, retry if
    the text never showed up, and only press Enter once it has -- which also
    means a retry cannot submit a half-delivered prompt.
    """
    body = text.rstrip("\n")
    if not body:
        return False
    if not session_exists(session):
        raise SwarmError(f"tmux session {session!r} is not running")

    with pane_lock(cfg, session):
        return _paste_locked(cfg, session, body, enter, needle)


def _paste_locked(
    cfg: SwarmConfig, session: str, body: str, enter: bool, needle: str | None = None
) -> bool:
    needle = needle or needle_for(body)
    delivered = False

    for attempt in range(1, PASTE_ATTEMPTS + 1):
        if attempt > 1:
            # Best effort: clear anything a previous attempt may have left behind,
            # so a retry cannot concatenate two copies of the prompt.
            tmux("send-keys", "-t", session, "C-u", check=False)
            sleep_ms(300)

        before = paste_score(session, needle)
        sleep_ms(cfg.send_delay_ms)
        send_buffer(session, body)

        deadline = time.monotonic() + VERIFY_TIMEOUT_MS / 1000.0
        while time.monotonic() < deadline:
            sleep_ms(200)
            if paste_score(session, needle) > before:
                delivered = True
                break
        if delivered:
            break
        warn(f"{session}: pasted text never appeared (attempt {attempt}/{PASTE_ATTEMPTS})")

    if not delivered:
        # Do not press Enter: if the text did arrive and we simply failed to see
        # it, submitting now could send a mangled or duplicated prompt.
        warn(f"{session}: could not confirm the text arrived; NOT pressing Enter")
        return False

    if enter:
        sleep_ms(cfg.enter_delay_ms)
        tmux("send-keys", "-t", session, "Enter")
    return True


# --------------------------------------------------------------------------
# state / logging / inbox
# --------------------------------------------------------------------------


def write_state(cfg: SwarmConfig) -> None:
    state = {
        "swarm": cfg.name,
        "config": str(cfg.path),
        "root": str(cfg.root),
        "session_prefix": cfg.session_prefix,
        "started_at": now_iso(),
        "agents": {
            a.name: {
                "session": a.session,
                "type": a.type,
                "workdir": str(a.workdir),
                "capture": a.capture,
                "can_talk_to": a.can_talk_to,
            }
            for a in cfg.agents
        },
    }
    (cfg.runtime / "state.json").write_text(json.dumps(state, indent=2) + "\n")


# --------------------------------------------------------------------------
# sessions.yaml -- the conversation id of every agent, so `up --resume` works
# --------------------------------------------------------------------------


def yaml_scalar(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def yaml_dump(data: dict, indent: int = 0) -> str:
    """Emit the small subset we need. Written by hand so PyYAML stays optional."""
    pad = " " * indent
    out = []
    for key, value in data.items():
        if isinstance(value, dict):
            out.append(f"{pad}{key}:")
            out.append(yaml_dump(value, indent + 2) if value else f"{pad}  {{}}")
        else:
            out.append(f"{pad}{key}: {yaml_scalar(value)}")
    return "\n".join(out)


def read_sessions(cfg: SwarmConfig) -> dict:
    """The agents block of sessions.yaml, or {} if it is missing or unreadable."""
    try:
        data = cfgmod.parse_yaml(cfg.sessions_file.read_text())
    except OSError:
        return {}
    except Exception as exc:  # noqa: BLE001 - a corrupt file must not stop the swarm
        warn(f"could not parse {cfg.sessions_file}: {exc}")
        return {}
    if not isinstance(data, dict):
        return {}
    return data.get("agents") or {}


def write_sessions(cfg: SwarmConfig, agents: dict) -> None:
    cfg.runtime.mkdir(parents=True, exist_ok=True)
    header = (
        "# Agentainer session state -- written automatically as agents work.\n"
        "# `agentainer up --resume` reads this to reattach each agent to its own\n"
        "# conversation after a restart. Safe to delete; you then start fresh.\n"
    )
    body = yaml_dump(
        {
            "swarm": cfg.name,
            "config": str(cfg.path),
            "updated_at": now_iso(),
            "agents": agents or {},
        }
    )
    tmp = cfg.sessions_file.with_suffix(".yaml.tmp")
    tmp.write_text(header + body + "\n")
    os.replace(tmp, cfg.sessions_file)  # atomic: hooks write this concurrently


def record_session(cfg: SwarmConfig, agent: Agent, session_id, **fields) -> None:
    """Merge this agent's conversation id into sessions.yaml, under a lock."""
    if not session_id:
        return
    with file_lock(cfg, "sessions", "lock"):
        agents = read_sessions(cfg)
        entry = agents.get(agent.name) or {}
        if entry.get("session_id") == session_id:
            return  # unchanged: do not rewrite the file after every single turn
        entry.update({k: v for k, v in fields.items() if v})
        entry["session_id"] = session_id
        entry["type"] = agent.type
        entry["workdir"] = str(agent.workdir)
        entry["updated_at"] = now_iso()
        agents[agent.name] = entry
        write_sessions(cfg, agents)
    info(f"{agent.name}: recorded conversation {session_id}")


def codex_session(agent: Agent) -> tuple[str | None, str | None]:
    """Find the id of the codex conversation running in this agent's CODEX_HOME.

    Codex does not hand its session id to the notify program, but it writes one
    rollout file per session under CODEX_HOME/sessions, and the newest of those is
    the conversation currently in progress.
    """
    sessions = agent.workdir / ".codex" / "sessions"
    if not sessions.is_dir():
        return None, None

    rollouts = sorted(sessions.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not rollouts:
        return None, None

    newest = rollouts[-1]
    try:
        with newest.open() as fh:
            first = fh.readline()
        record = json.loads(first)
        if record.get("type") == "session_meta":
            payload = record.get("payload", {})
            return payload.get("session_id") or payload.get("id"), str(newest)
    except (OSError, json.JSONDecodeError):
        pass
    return None, str(newest)


def log_event(cfg: SwarmConfig, agent: str, kind: str, **fields) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    record = {"ts": now_iso(), "agent": agent, "kind": kind, **fields}
    with (cfg.log_dir / f"{agent}.jsonl").open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    with (cfg.log_dir / "swarm.jsonl").open("a") as fh:
        fh.write(json.dumps(record) + "\n")


def archive_message(
    cfg: SwarmConfig,
    sender: str,
    recipient: str,
    text: str,
    msg_id: str = "",
    reply_to: str | None = None,
) -> Path:
    box = cfg.inbox_dir / recipient
    box.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3]
    suffix = f"-{msg_id}" if msg_id else ""
    path = box / f"{stamp}-from-{sender}{suffix}.md"
    head = f"# message from {sender}\n\n_{now_iso()}_"
    if msg_id:
        head += f"  \nid: `{msg_id}`"
    if reply_to:
        head += f"  \nin reply to: `{reply_to}`"
    path.write_text(f"{head}\n\n{text.rstrip()}\n")
    return path


MESSAGE_HEADER = "[swarm] message from"
ECHO_MEMORY = 300

# --------------------------------------------------------------------------
# tagged message envelopes
# --------------------------------------------------------------------------
#
# Inbound, an agent sees:
#
#   <swarm-message from="lead" id="m-3f9a1c" reply-to="m-1b77e0">
#   ...body, any number of lines...
#   </swarm-message>
#
# Outbound, an agent simply writes this in its reply and the capture hook routes
# it. No shell quoting, so multi-line bodies survive intact:
#
#   <swarm-send to="reviewer" reply-to="m-3f9a1c">
#   ...body...
#   </swarm-send>

INBOUND_TAG = "swarm-message"
OUTBOUND_RE = re.compile(
    r"<swarm-(?P<kind>send|broadcast)\b(?P<attrs>[^>]*)>(?P<body>.*?)</swarm-(?P=kind)\s*>",
    re.DOTALL | re.IGNORECASE,
)
ATTR_RE = re.compile(r"""([A-Za-z_-]+)\s*=\s*["']([^"']*)["']""")
# Every opening tag, capturing its attributes and whether it ends its own line.
OPENER_RE = re.compile(
    r"<swarm-(?:send|broadcast)\b(?P<attrs>[^>]*)>(?P<tail>[ \t]*)(?P<nl>\n?)",
    re.IGNORECASE,
)
ADDRESSED_RE = re.compile(r"\b(?:to|agent)\s*=", re.IGNORECASE)


def _is_block_opener(attrs: str, ends_line: bool) -> bool:
    """Does this opening tag look like the real start of a message block?

    A genuine send carries a `to="..."`, and any real block opener sits at the end
    of its line, the way the template shows it. A bare inline `<swarm-send>` with
    neither -- `Use \\`<swarm-send>\\` blocks`, `I'll <swarm-send> the result` -- is
    the agent naming the tag in prose, not attempting a send, and must not trigger a
    spurious "your send failed" nudge.
    """
    return bool(ADDRESSED_RE.search(attrs)) or ends_line


def new_message_id() -> str:
    return f"m-{uuid.uuid4().hex[:6]}"


def format_envelope(
    cfg: SwarmConfig,
    sender: str,
    recipient: str,
    text: str,
    msg_id: str,
    reply_to: str | None,
) -> str:
    if cfg.message_format == "plain":
        return f"{MESSAGE_HEADER} {sender}:\n{text}"

    attrs = f'from="{sender}" to="{recipient}" id="{msg_id}"'
    if reply_to:
        attrs += f' reply-to="{reply_to}"'
    return f"<{INBOUND_TAG} {attrs}>\n{text}\n</{INBOUND_TAG}>"


class Outbound:
    __slots__ = ("kind", "to", "reply_to", "body", "expects_reply")

    def __init__(
        self,
        kind: str,
        to: str | None,
        reply_to: str | None,
        body: str,
        expects_reply: bool = True,
    ):
        self.kind, self.to, self.reply_to, self.body = kind, to, reply_to, body
        self.expects_reply = expects_reply


def parse_outbound(text: str) -> tuple[list[Outbound], str, list[str]]:
    """Extract <swarm-send>/<swarm-broadcast> blocks.

    Returns the messages, the text with those blocks removed, and a list of
    human-readable problems -- which are fed back to the agent so it can fix its
    own syntax rather than silently losing a message.
    """
    messages: list[Outbound] = []
    problems: list[str] = []
    matched = 0

    for match in OUTBOUND_RE.finditer(text or ""):
        matched += 1
        attrs = {k.lower(): v for k, v in ATTR_RE.findall(match.group("attrs"))}
        body = match.group("body").strip()
        if not body:
            problems.append("a message block had an empty body, so there was nothing to send")
            continue
        kind = match.group("kind").lower()
        wants = (attrs.get("expects-reply") or attrs.get("expects_reply") or "true").lower()
        messages.append(
            Outbound(
                kind=kind,
                to=attrs.get("to") or attrs.get("agent"),
                reply_to=attrs.get("reply-to") or attrs.get("reply_to"),
                body=body,
                # A broadcast is an announcement, never a question.
                expects_reply=kind == "send" and wants not in ("false", "no", "0"),
            )
        )

    remainder = OUTBOUND_RE.sub("", text or "").strip()
    # A delivered block contributes one block-opener too, so more block-openers than
    # delivered blocks means one was opened and never closed. Inline prose mentions
    # of the tag are not block-openers, so naming the tag no longer looks like a
    # failed send.
    openers = sum(
        1 for m in OPENER_RE.finditer(text or "")
        if _is_block_opener(m.group("attrs"), bool(m.group("nl")))
    )
    if openers > matched:
        problems.append(
            "a <swarm-send> block was opened but never closed with </swarm-send>, "
            "so it could not be delivered"
        )
    return messages, remainder, problems


def record_echo(cfg: SwarmConfig, agent: str, text: str) -> None:
    """Remember text we typed into a pane-captured agent.

    A terminal echoes whatever is typed into it, so the pane watcher would
    otherwise see an incoming message as if the agent had said it -- and relay
    it straight back out. We keep the delivered lines so the watcher can drop
    them from its diff.
    """
    path = cfg.run_dir / f"{agent}.echo"
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    try:
        known = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        known = []
    known.extend(line.strip() for line in text.splitlines() if line.strip())
    path.write_text(json.dumps(known[-ECHO_MEMORY:]))


def read_echo(cfg: SwarmConfig, agent: str) -> set[str]:
    try:
        return set(json.loads((cfg.run_dir / f"{agent}.echo").read_text()))
    except (OSError, json.JSONDecodeError):
        return set()


def read_hops(cfg: SwarmConfig, agent: str) -> int:
    path = cfg.run_dir / f"{agent}.hop"
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def write_hops(cfg: SwarmConfig, agent: str, hops: int) -> None:
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    (cfg.run_dir / f"{agent}.hop").write_text(str(hops))


# --------------------------------------------------------------------------
# message delivery
# --------------------------------------------------------------------------


def deliver(
    cfg: SwarmConfig,
    sender: str,
    recipient: str,
    text: str,
    hops: int = 0,
    enforce_acl: bool = True,
    allow_busy: bool = False,
    reply_to: str | None = None,
    expects_reply: bool = True,
) -> str:
    text = text.strip()
    if not text:
        raise SwarmError("refusing to send an empty message")

    target = cfg.get(recipient)

    if enforce_acl and sender in cfg.names():
        source = cfg.get(sender)
        if recipient not in source.can_talk_to:
            allowed = ", ".join(source.can_talk_to) or "no one"
            raise SwarmError(
                f"permission denied: {sender!r} may not message {recipient!r} "
                f"(allowed: {allowed}). Edit can_talk_to in {cfg.path.name} to change this."
            )

    if not session_exists(target.session):
        raise SwarmError(
            f"agent {recipient!r} is not running (tmux session {target.session!r} missing). "
            "Start it with: agentainer up"
        )

    msg_id = new_message_id()
    body = format_envelope(cfg, sender, recipient, text, msg_id, reply_to)
    # The message id is unique and sits near the top of the envelope, which makes
    # a far better delivery needle than the body tail -- every tagged message ends
    # with the same closing tag.
    needle = normalise(f'id="{msg_id}"') if cfg.message_format == "tagged" else None

    # Check-and-set under one lock. Two senders -- e.g. two subagents running in
    # parallel -- must not both see an idle agent and both deliver to it, so the
    # busy check and the "now busy" write cannot be separated.
    with pane_lock(cfg, target.session):
        if not allow_busy:
            state = busy_info(cfg, target)
            if state:
                raise BusyError(busy_message(cfg, target, state))

        if target.capture == "pane":
            record_echo(cfg, recipient, body)

        if not _paste_locked(cfg, target.session, body, enter=True, needle=needle):
            raise SwarmError(
                f"could not confirm the message reached {recipient!r}; "
                f"inspect it with: agentainer attach {recipient}"
            )

        with file_lock(cfg, recipient, "turn.lock"):
            mark_turn_started(cfg, recipient, sender)

    archived = archive_message(cfg, sender, recipient, text, msg_id, reply_to)
    # A message that answers another one (reply-to), a broadcast, and an automatic
    # forward are not questions. Only an opening message earns a reply obligation --
    # otherwise an "ACK" would be nagged for an answer to an acknowledgement.
    if expects_reply and not reply_to:
        note_awaiting_reply(cfg, sender, recipient, msg_id)
    write_hops(cfg, recipient, hops)
    log_event(cfg, sender, "sent", to=recipient, hops=hops, id=msg_id, reply_to=reply_to, text=text)
    log_event(
        cfg, recipient, "received", **{"from": sender},
        hops=hops, id=msg_id, reply_to=reply_to, archived=str(archived),
    )
    return msg_id


def route_outbound(cfg: SwarmConfig, agent: Agent, text: str) -> tuple[str, list[str], list[str]]:
    """Deliver any <swarm-send> blocks the agent wrote.

    This is the point of tagged messages: the agent writes a multi-line block in
    its reply and never has to shell-quote anything.

    Returns the leftover text, who was successfully messaged, and what went wrong.
    """
    if not agent.parse_outbound_tags or not text:
        return text, [], []

    messages, remainder, problems = parse_outbound(text)
    reached: list[str] = []

    for message in messages:
        if message.kind == "broadcast":
            targets = agent.can_talk_to
            if not targets:
                problems.append("you used <swarm-broadcast> but you may not message anyone")
                continue
        elif not message.to:
            problems.append(
                'a <swarm-send> was missing its `to` attribute, e.g. <swarm-send to="NAME">'
            )
            continue
        elif message.to not in cfg.names():
            # Most often the agent echoed the AGENT_NAME placeholder from its prompt.
            allowed = ", ".join(agent.can_talk_to) or "no one"
            problems.append(
                f'you addressed <swarm-send to="{message.to}">, which is not an agent. '
                f"You may message: {allowed}"
            )
            continue
        else:
            targets = [message.to]

        for target in targets:
            try:
                msg_id = deliver(
                    cfg, agent.name, target, message.body,
                    reply_to=message.reply_to,
                    expects_reply=message.expects_reply,
                )
                reached.append(target)
                info(f"{agent.name} -> {target}: routed tagged message {msg_id}")
            except BusyError:
                _, depth = enqueue(
                    cfg, agent.name, target, message.body, hops=0,
                    reply_to=message.reply_to, expects_reply=message.expects_reply,
                )
                reached.append(target)
                info(f"{agent.name} -> {target}: busy, queued tagged message (depth {depth})")
            except SwarmError as exc:
                reason = str(exc).splitlines()[0]
                problems.append(f"your message to {target} was not delivered: {reason}")
                warn(f"{agent.name} -> {target}: {reason}")

    return remainder, reached, problems


# --------------------------------------------------------------------------
# reply reminders
# --------------------------------------------------------------------------
#
# An agent can finish a turn having written its answer as ordinary prose. That
# prose goes nowhere: only a <swarm-send> block is delivered. The sender would
# wait forever. So when an agent owes a reply and ends a turn without sending
# one -- or writes a block we could not deliver -- we tell it, once.

SYSTEM_SENDER = "swarm"


def pending_path(cfg: SwarmConfig, agent: str) -> Path:
    return cfg.run_dir / f"{agent}.pending.json"


def read_pending(cfg: SwarmConfig, agent: str) -> dict | None:
    try:
        return json.loads(pending_path(cfg, agent).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_pending(cfg: SwarmConfig, agent: str, state: dict | None) -> None:
    path = pending_path(cfg, agent)
    if state is None:
        path.unlink(missing_ok=True)
        return
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


def note_awaiting_reply(cfg: SwarmConfig, sender: str, recipient: str, msg_id: str) -> None:
    """Remember that *recipient* owes *sender* an answer."""
    target = cfg.get(recipient)
    if not target.reply_reminder or sender not in cfg.names():
        return
    if sender not in target.can_talk_to:
        return  # it has no way to answer, so do not badger it
    write_pending(cfg, recipient, {"from": sender, "id": msg_id, "reminders": 0})


def handle_reply_reminder(
    cfg: SwarmConfig, agent: Agent, reached: list[str], problems: list[str]
) -> None:
    if not agent.reply_reminder:
        return

    pending = read_pending(cfg, agent.name)
    # `from` is absent when the state was written by a send_failed nudge: nobody is
    # waiting then, so this must not turn into "someone is waiting for your answer".
    owes_reply = bool(pending and pending.get("from")) and not reached

    if not problems and not owes_reply:
        write_pending(cfg, agent.name, None)  # it answered; nothing to chase
        return

    reminders = (pending or {}).get("reminders", 0)
    if reminders >= cfg.max_reply_reminders:
        warn(
            f"{agent.name}: still no valid message after {reminders} reminder(s); "
            "giving up on this one"
        )
        write_pending(cfg, agent.name, None)
        return

    detail = (
        "What went wrong:\n" + "\n".join(f"  - {p}" for p in problems)
        if problems
        else "Your last turn contained no message block at all, so nothing was sent."
    )
    peers = ", ".join(agent.can_talk_to) or "no one"

    if owes_reply:
        template = cfg.reply_reminder_template
    else:
        # It tried to send something and we could not deliver it. Correct the syntax
        # rather than telling it that somebody is waiting -- nobody may be.
        template = cfg.send_failed_template

    fields = {
        "agent": agent.name,
        "sender": (pending or {}).get("from") or "another agent",
        "id": (pending or {}).get("id") or "",
        "peers": peers,
        "problems": detail,
    }
    try:
        text = template.format(**fields).strip()
    except (KeyError, IndexError, ValueError) as exc:
        warn(f"{agent.name}: reminder template is malformed ({exc}); using the built-in one")
        default = (
            cfgmod.DEFAULT_REPLY_REMINDER_TEMPLATE
            if owes_reply
            else cfgmod.DEFAULT_SEND_FAILED_TEMPLATE
        )
        text = default.format(**fields).strip()

    try:
        deliver(cfg, SYSTEM_SENDER, agent.name, text)
        info(f"{agent.name}: reminded how to send its reply")
    except BusyError:
        enqueue(cfg, SYSTEM_SENDER, agent.name, text, hops=0)
    except SwarmError as exc:
        warn(f"{agent.name}: could not send the reply reminder: {exc}")
        return

    state = pending or {"from": None, "id": None}
    state["reminders"] = reminders + 1
    write_pending(cfg, agent.name, state)


def on_turn_finished(cfg: SwarmConfig, agent: Agent, text: str) -> None:
    """Called whenever a capture tells us the agent completed a turn."""
    mark_turn_finished(cfg, agent.name)
    # Route explicit tagged sends first, then auto-forward only what is left, so a
    # message the agent addressed to one peer is not also broadcast to everybody.
    remainder, reached, problems = route_outbound(cfg, agent, text)
    reached += forward_response(cfg, agent, remainder)

    # Hand over waiting mail before nudging. A reminder is itself a message: sending
    # it first would mark the agent busy and leave the real message stuck in the
    # queue. If something was delivered, the agent gets another turn, and we
    # reconsider the reminder when that one ends.
    if not drain_queue(cfg, agent):
        handle_reply_reminder(cfg, agent, reached, problems)

    # Any turn end is a chance to rescue mail stranded on an agent whose own capture
    # never fired -- otherwise a single missed turn-completion wedges its queue.
    sweep_stale_queues(cfg, exclude=agent.name)


def forward_response(cfg: SwarmConfig, agent: Agent, text: str) -> list[str]:
    """Auto-forward a captured turn to the agent's forward_responses_to list.

    Returns who it reached, so an agent that forwards automatically is not then
    nagged for writing no <swarm-send> block: its words did get delivered.
    """
    if not agent.forward_responses_to or not text.strip():
        return []

    hops = read_hops(cfg, agent.name) + 1
    if hops > cfg.max_forward_hops:
        warn(
            f"{agent.name}: forward hop limit ({cfg.max_forward_hops}) reached; "
            "not forwarding further"
        )
        log_event(cfg, agent.name, "forward_suppressed", hops=hops)
        return []

    reached: list[str] = []
    for peer in agent.forward_responses_to:
        try:
            deliver(cfg, agent.name, peer, text, hops=hops, expects_reply=False)
            reached.append(peer)
        except BusyError:
            # An auto-forward must not be dropped just because the peer is mid-task.
            _, depth = enqueue(cfg, agent.name, peer, text, hops, expects_reply=False)
            reached.append(peer)
            info(f"forward {agent.name} -> {peer}: {peer} is busy, queued (depth {depth})")
        except SwarmError as exc:
            warn(f"forward {agent.name} -> {peer} failed: {exc}")
    return reached


# --------------------------------------------------------------------------
# capture: hook installation
# --------------------------------------------------------------------------


def pretrust_claude_dir(agent: Agent) -> None:
    """Mark the agent's workdir as trusted in ~/.claude.json.

    Claude Code asks "Do you trust the files in this folder?" the first time it
    runs anywhere new -- even under --dangerously-skip-permissions -- and that
    modal swallows the first prompt (Enter answers the dialog). Codex gets the
    same treatment via its config.toml; this is the claude equivalent.
    """
    path = Path(os.path.expanduser("~")) / ".claude.json"
    if not path.is_file():
        return  # claude has never run here; it will create the file itself

    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        warn(f"{agent.name}: could not read ~/.claude.json; the trust dialog may appear")
        return

    projects = data.setdefault("projects", {})
    entry = projects.setdefault(str(agent.workdir), {})
    if entry.get("hasTrustDialogAccepted"):
        return

    entry["hasTrustDialogAccepted"] = True
    entry.setdefault("projectOnboardingSeenCount", 1)

    # Write atomically: a running claude may be reading this file.
    tmp = path.with_suffix(".json.swarm-tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)
    except OSError as exc:
        warn(f"{agent.name}: could not pre-trust {agent.workdir}: {exc}")
        tmp.unlink(missing_ok=True)


def install_claude_hook(agent: Agent) -> None:
    pretrust_claude_dir(agent)
    settings_path = agent.workdir / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    settings: dict = {}
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            warn(f"{settings_path} is not valid JSON; overwriting")

    hook_cmd = str(HOOKS_DIR / "claude_stop.sh")
    # No "matcher" key: Stop is not a tool event, and supplying one can stop the
    # interactive TUI from ever running the hook.
    entry = {"hooks": [{"type": "command", "command": hook_cmd}]}
    hooks = settings.setdefault("hooks", {})
    stop_hooks = [
        h
        for h in hooks.get("Stop", [])
        if hook_cmd not in json.dumps(h)  # drop our own stale entry, keep the user's
    ]
    stop_hooks.append(entry)
    hooks["Stop"] = stop_hooks
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")


def valid_toml(text: str) -> bool:
    try:
        import tomllib
    except ImportError:  # Python < 3.11: cannot check, assume the caller is right
        return True
    try:
        tomllib.loads(text)
        return True
    except tomllib.TOMLDecodeError:
        return False


def install_codex_hook(agent: Agent) -> Path:
    """Give codex a private CODEX_HOME with a `notify` program wired up."""
    codex_home = agent.workdir / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)

    # Carry over the user's real credentials + settings, so the agent is logged in.
    user_home = Path(os.path.expanduser("~")) / ".codex"
    base = ""
    if user_home.is_dir() and user_home.resolve() != codex_home.resolve():
        for name in ("auth.json",):
            src, dst = user_home / name, codex_home / name
            if src.is_file() and not dst.exists():
                try:
                    dst.symlink_to(src)
                except OSError:
                    shutil.copy2(src, dst)
        user_cfg = user_home / "config.toml"
        if user_cfg.is_file():
            base = "\n".join(
                line
                for line in user_cfg.read_text().splitlines()
                if not re.match(r"\s*notify\s*=", line)
            ).strip()

    notify = json.dumps(str(HOOKS_DIR / "codex_notify.sh"))
    # Without this table codex opens a "do you trust this directory?" modal on
    # first run in a fresh folder, and that modal swallows the first prompt.
    trust = f"[projects.{json.dumps(str(agent.workdir))}]"

    # TOML is order-sensitive: a bare key written after a [table] header belongs
    # to that table. `notify` must therefore come before anything else, or codex
    # reads it as projects.<dir>.notify and never calls it.
    chunks = [
        "# installed by Agentainer -- fires when codex finishes a turn.",
        "# Keep `notify` above every [table] header: TOML is order-sensitive.",
        f"notify = [{notify}]",
        "",
    ]
    if base:
        chunks += [base, ""]
    if trust not in base:  # the user's config may already trust this directory
        chunks += ["# pre-trust the workdir so no modal eats the first prompt", trust,
                   'trust_level = "trusted"', ""]

    body = "\n".join(chunks)
    if not valid_toml(body):
        warn(
            f"{agent.name}: ~/.codex/config.toml could not be merged cleanly "
            "(invalid TOML); writing a minimal config instead"
        )
        body = "\n".join(
            [f"notify = [{notify}]", "", trust, 'trust_level = "trusted"', ""]
        )

    (codex_home / "config.toml").write_text(body)
    return codex_home


def install_capture(cfg: SwarmConfig, agent: Agent) -> dict[str, str]:
    """Install turn-completion capture. Returns extra env vars for the session."""
    env: dict[str, str] = {}
    if agent.capture != "hook":
        return env

    if agent.type == "claude":
        install_claude_hook(agent)
    elif agent.type == "codex":
        env["CODEX_HOME"] = str(install_codex_hook(agent))
    else:
        warn(
            f"agent {agent.name!r}: type {agent.type!r} has no known completion hook "
            f"(only {', '.join(HOOK_CAPABLE)} do); falling back to capture: pane"
        )
        agent.capture = "pane"
    return env


# --------------------------------------------------------------------------
# capture: pane watcher
# --------------------------------------------------------------------------


def start_watcher(cfg: SwarmConfig, agent: Agent) -> None:
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    logfile = (cfg.log_dir / f"{agent.name}.watcher.log").open("a")
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "watch", agent.name, "-c", str(cfg.path)],
        stdout=logfile,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    (cfg.run_dir / f"{agent.name}.watcher.pid").write_text(str(proc.pid))


def stop_watcher(cfg: SwarmConfig, agent: Agent) -> None:
    pid_file = cfg.run_dir / f"{agent.name}.watcher.pid"
    if not pid_file.is_file():
        return
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 15)
    except (OSError, ValueError):
        pass
    pid_file.unlink(missing_ok=True)


def watcher_alive(cfg: SwarmConfig, agent: Agent) -> bool:
    pid_file = cfg.run_dir / f"{agent.name}.watcher.pid"
    if not pid_file.is_file():
        return False
    try:
        os.kill(int(pid_file.read_text().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


READY_TOKEN = "zqxswarmready"


def normalise(text: str) -> str:
    """Strip all whitespace, so a TUI's line-wrapping cannot hide a needle."""
    return re.sub(r"\s+", "", text)


def visible_pane(session: str) -> str:
    try:
        return tmux("capture-pane", "-p", "-t", session, capture=True).stdout or ""
    except subprocess.CalledProcessError:
        return ""


def clear_token(session: str) -> None:
    """Backspace the readiness token back out of the composer."""
    for _ in range(6):
        count = normalise(visible_pane(session)).count(READY_TOKEN)
        if not count:
            return
        tmux("send-keys", "-t", session, *(["BSpace"] * (count * len(READY_TOKEN))))
        sleep_ms(300)


def wait_until_ready(cfg: SwarmConfig, agent: Agent) -> bool:
    """Block until the agent's TUI is actually accepting keystrokes.

    A fixed sleep is not enough: Claude Code, for instance, discards input for
    several seconds partway through its startup, and a prompt typed into that
    window is silently lost. So we type a throwaway token until the TUI echoes
    it back -- proof that its input box is live -- then erase it. Nothing is
    ever submitted, because Enter is never sent.
    """
    deadline = time.monotonic() + cfg.ready_timeout_ms / 1000.0
    with pane_lock(cfg, agent.session):
        while time.monotonic() < deadline:
            tmux("send-keys", "-t", agent.session, "-l", READY_TOKEN, check=False)
            sleep_ms(600)
            if READY_TOKEN in normalise(visible_pane(agent.session)):
                clear_token(agent.session)
                sleep_ms(cfg.send_delay_ms)
                return True
            if not session_exists(agent.session):
                return False
    return False


def capture_pane(cfg: SwarmConfig, agent: Agent) -> str:
    try:
        result = tmux(
            "capture-pane", "-p", "-J", "-S", f"-{cfg.pane_scrollback}", "-t", agent.session,
            capture=True,
        )
    except subprocess.CalledProcessError:
        return ""
    return result.stdout or ""


def run_watcher(cfg: SwarmConfig, agent: Agent) -> None:
    """Poll the pane; when it stops changing, emit whatever text is new.

    This is the fallback for agents whose CLI cannot run a program on turn
    completion. It is heuristic: it sees rendered terminal output, so spinners
    and redraws can produce noise.
    """
    info(f"watcher started for {agent.name} (session {agent.session})")
    emitted: list[str] = capture_pane(cfg, agent).splitlines()
    previous = list(emitted)
    last_change = time.monotonic()
    dirty = False

    while True:
        sleep_ms(cfg.pane_poll_ms)
        if not session_exists(agent.session):
            info(f"watcher for {agent.name}: session gone, exiting")
            return

        current = capture_pane(cfg, agent).splitlines()
        if current != previous:
            previous = current
            last_change = time.monotonic()
            dirty = True
            continue

        idle_for = (time.monotonic() - last_change) * 1000
        if not dirty or idle_for < cfg.pane_idle_ms:
            continue
        dirty = False

        # Emit the tail that appeared since the last quiet moment.
        common = 0
        for old, new in zip(emitted, current):
            if old != new:
                break
            common += 1
        emitted = current

        # Drop the terminal's echo of messages we delivered, so the agent does
        # not appear to have "said" its own incoming mail.
        echoed = read_echo(cfg, agent.name)
        new_lines = [
            line.rstrip()
            for line in current[common:]
            if line.strip()
            and line.strip() not in echoed
            and not line.strip().startswith(MESSAGE_HEADER)
            and not line.strip().startswith((f"<{INBOUND_TAG}", f"</{INBOUND_TAG}"))
        ]

        text = "\n".join(new_lines).strip()
        if len(text) < 2:
            continue
        log_event(cfg, agent.name, "response", source="pane", text=text)
        on_turn_finished(cfg, agent, text)


# --------------------------------------------------------------------------
# hook entry points (called by hooks/*.sh from inside an agent's process)
# --------------------------------------------------------------------------


def config_from_state() -> str | None:
    """Walk up from the cwd looking for the .swarm/state.json written by `up`."""
    probe = Path.cwd().resolve()
    for candidate in [probe, *probe.parents]:
        state = candidate / ".swarm" / "state.json"
        if state.is_file():
            try:
                return json.loads(state.read_text()).get("config")
            except (OSError, json.JSONDecodeError):
                return None
    return None


def agent_from_cwd(cfg: SwarmConfig) -> str | None:
    cwd = Path.cwd().resolve()
    for agent in cfg.agents:
        workdir = agent.workdir.resolve()
        if cwd == workdir or workdir in cwd.parents:
            return agent.name
    return None


def discover_context(explicit_config: str | None, explicit_agent: str | None):
    """Figure out which swarm + agent we are, from argv, env, or the filesystem.

    A hook runs inside the agent's own process, so it may inherit a SWARM_CONFIG
    (or fall back to an unrelated ./agents.yaml) that does not describe it. Each
    candidate config is therefore only accepted if it actually contains the
    calling agent.
    """
    candidates = [explicit_config, os.environ.get("SWARM_CONFIG"), config_from_state()]
    seen: list[str] = []

    for path in candidates:
        if not path or not Path(path).is_file() or path in seen:
            continue
        seen.append(path)
        try:
            cfg = cfgmod.load(path)
        except ConfigError:
            continue
        name = explicit_agent or os.environ.get("SWARM_AGENT") or agent_from_cwd(cfg)
        if name and name in cfg.names():
            return cfg, cfg.get(name)

    raise SwarmError(
        "cannot work out which swarm/agent is calling. Set SWARM_AGENT and SWARM_CONFIG, "
        "or run from inside an agent's working directory. "
        f"Configs tried: {', '.join(seen) or 'none'}"
    )


TRANSCRIPT_WAIT_MS = 5000
TRANSCRIPT_POLL_MS = 150


def read_transcript_reply(transcript: str) -> str:
    """The agent's reply to the newest user message, or "" if not written yet.

    Two things to be careful about:

    * Subagents (the Task tool) write into the *same* transcript, marked
      `isSidechain: true`. Their turns are not the agent's answer.
    * Only text that comes *after* the last user message belongs to this turn.
      Scanning the whole file would happily return the previous turn's reply.
    """
    records = []
    try:
        with open(transcript) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a partially flushed final line
    except OSError:
        return ""

    last_user = -1
    for index, record in enumerate(records):
        if record.get("type") == "user" and not record.get("isSidechain"):
            last_user = index

    reply = ""
    for record in records[last_user + 1 :]:
        if record.get("type") != "assistant" or record.get("isSidechain"):
            continue
        content = record.get("message", {}).get("content", [])
        if isinstance(content, str):
            reply = content
            continue
        chunks = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if any(c.strip() for c in chunks):
            reply = "\n".join(c for c in chunks if c.strip())
    return reply.strip()


def extract_claude_response(payload: dict) -> str:
    """Pull the agent's reply for this turn out of a Claude Code transcript.

    The Stop hook can fire before Claude has flushed the assistant message to
    disk, so the transcript is polled briefly rather than read once. Without this
    the hook silently captures nothing -- or, worse, re-reads the previous turn.
    """
    transcript = payload.get("transcript_path")
    if not transcript or not Path(transcript).is_file():
        return ""

    deadline = time.monotonic() + TRANSCRIPT_WAIT_MS / 1000.0
    while True:
        reply = read_transcript_reply(transcript)
        if reply or time.monotonic() >= deadline:
            return reply
        sleep_ms(TRANSCRIPT_POLL_MS)


def cmd_hook(args) -> int:
    cfg, agent = discover_context(args.config, args.agent)

    if args.type == "claude":
        try:
            payload = json.load(sys.stdin)
        except json.JSONDecodeError:
            payload = {}
        # Claude sets this when a Stop hook already caused a continuation.
        if payload.get("stop_hook_active"):
            return 0
        # Claude hands us its session id on every turn; that is what --resume wants.
        record_session(
            cfg, agent, payload.get("session_id"), transcript=payload.get("transcript_path")
        )
        text = extract_claude_response(payload)
    elif args.type == "codex":
        try:
            payload = json.loads(args.payload or "{}")
        except json.JSONDecodeError:
            payload = {}
        if payload.get("type") != "agent-turn-complete":
            return 0
        session_id, rollout = codex_session(agent)
        record_session(cfg, agent, session_id, transcript=rollout)
        text = str(payload.get("last-assistant-message") or "").strip()
    else:
        text = sys.stdin.read().strip()

    # An empty turn (tool-only) still ends the turn: mark the agent idle so it can
    # receive again, even when there is nothing worth logging or relaying.
    if text:
        log_event(cfg, agent.name, "response", source=f"hook:{args.type}", text=text)
    on_turn_finished(cfg, agent, text)
    return 0


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------


def write_shim(cfg: SwarmConfig) -> None:
    """A `swarm` executable the agents themselves can call."""
    cfg.bin_dir.mkdir(parents=True, exist_ok=True)
    shim = cfg.bin_dir / "swarm"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        "# Generated by Agentainer. Lets an agent run `swarm send ...` from its shell.\n"
        f'exec {shlex.quote(str(SWARM_HOME / "agentainer"))} "$@"\n'
    )
    shim.chmod(0o755)


def session_env(cfg: SwarmConfig, agent: Agent, extra: dict[str, str]) -> dict[str, str]:
    env = {
        "SWARM_HOME": str(SWARM_HOME),
        "SWARM_CONFIG": str(cfg.path),
        "SWARM_ROOT": str(cfg.root),
        "SWARM_NAME": cfg.name,
        "SWARM_AGENT": agent.name,
        "SWARM_SESSION": agent.session,
        "SWARM_PEERS": ",".join(agent.can_talk_to),
    }
    env.update(agent.env)
    env.update(extra)
    return env


def resume_command(cfg: SwarmConfig, agent: Agent, session_id: str) -> str | None:
    """The command that reattaches *agent* to conversation *session_id*.

    `resume_command` wins, because a command like `bash -ic chy3` invokes the CLI
    through an alias and flags cannot simply be appended to it.
    """
    try:
        if agent.resume_command:
            return agent.resume_command.format(session_id=session_id, command=agent.command)
        if agent.resume_args:
            return f"{agent.command} {agent.resume_args.format(session_id=session_id)}"
    except (KeyError, IndexError, ValueError) as exc:
        warn(f"{agent.name}: resume recipe is malformed ({exc}); starting a fresh conversation")
    return None


def start_agent(cfg: SwarmConfig, agent: Agent, resume_cmd: str | None = None) -> None:
    """Launch the agent. Pass resume_cmd to reattach it to an existing conversation."""
    resume = resume_cmd is not None
    if not agent.workdir.is_dir():
        if not agent.create_workdir:
            raise SwarmError(
                f"{agent.name}: workdir does not exist: {agent.workdir} "
                "(create_workdir is false)"
            )
        agent.workdir.mkdir(parents=True, exist_ok=True)
        info(f"{agent.name}: created {agent.workdir}")

    # No turn is in flight in a newly launched CLI, whether resumed or not. A
    # resumed agent keeps its unread mail and any reply it still owes.
    write_turn_state(cfg, agent.name, {"delivered": 0, "completed": 0, "since": 0, "by": None})
    if not resume:
        queue_write(cfg, agent.name, [])
        write_pending(cfg, agent.name, None)

    extra_env = install_capture(cfg, agent)
    env = session_env(cfg, agent, extra_env)

    exports = " ".join(f"export {k}={shlex.quote(v)};" for k, v in env.items())
    exports += f' export PATH={shlex.quote(str(cfg.bin_dir))}:"$PATH";'

    command = resume_cmd or agent.command

    inner = (
        f"{exports} "
        f"cd {shlex.quote(str(agent.workdir))} || exit 1; "
        f"{command}; "
        f'status=$?; printf "\\n[swarm] agent %s exited (status %s)\\n" '
        f"{shlex.quote(agent.name)} \"$status\"; "
        'exec "${SHELL:-bash}" -l'
    )
    launcher = f"exec bash -lc {shlex.quote(inner)}"

    # -x/-y give the detached pane a real size, so a long turn's output does not
    # wrap to an 80x24 default and lose lines. history-limit and mouse are read
    # from the globals configure_tmux() set *before* this call, so the pane
    # inherits the large scrollback the user needs to scroll back through.
    tmux("new-session", "-d", "-s", agent.session, "-x", "220", "-y", "50",
         "-c", str(agent.workdir), launcher)
    info(f"started {agent.name} ({agent.type}) in tmux session {agent.session!r}")


def cmd_up(args) -> int:
    cfg = cfgmod.load(args.config)
    if not shutil.which("tmux"):
        die("tmux is required but was not found on PATH")

    selected = select_agents(cfg, args.only)
    for message in cfg.warnings:
        warn(message)

    for directory in (cfg.runtime, cfg.log_dir, cfg.inbox_dir, cfg.run_dir, cfg.bin_dir):
        directory.mkdir(parents=True, exist_ok=True)
    write_shim(cfg)
    write_state(cfg)
    setup_holder = configure_tmux(cfg)

    resume = cfg.resume if args.resume is None else args.resume
    recorded = read_sessions(cfg) if resume else {}

    started: list[Agent] = []
    resumed: set[str] = set()
    for agent in selected:
        if session_exists(agent.session):
            if not args.restart:
                warn(f"{agent.name}: session {agent.session!r} already exists, skipping")
                continue
            info(f"{agent.name}: restarting")
            stop_watcher(cfg, agent)
            tmux("kill-session", "-t", f"={agent.session}", check=False, capture=True)

        resume_cmd = None
        if resume:
            session_id = (recorded.get(agent.name) or {}).get("session_id")
            if not session_id:
                warn(f"{agent.name}: no recorded conversation; starting a fresh one")
            else:
                resume_cmd = resume_command(cfg, agent, session_id)
                if resume_cmd:
                    resumed.add(agent.name)
                    info(f"{agent.name}: resuming conversation {session_id}")
                else:
                    warn(
                        f"{agent.name}: type {agent.type!r} has no resume recipe "
                        "(set resume_args or resume_command); starting a fresh conversation"
                    )

        start_agent(cfg, agent, resume_cmd)
        started.append(agent)

    # The real agent sessions now keep the server alive, so the throwaway holder
    # that let us set the global scrollback has done its job.
    if setup_holder:
        tmux("kill-session", "-t", f"={setup_holder}", check=False, capture=True)

    if not started:
        info("nothing to start")
        return 0

    # An agent launched into a brand-new conversation must not keep claiming the
    # old one, or a later `up --resume` would reattach to a session it never ran.
    fresh = [a.name for a in started if a.name not in resumed]
    if fresh:
        with file_lock(cfg, "sessions", "lock"):
            recorded_now = read_sessions(cfg)
            if any(name in recorded_now for name in fresh):
                for name in fresh:
                    recorded_now.pop(name, None)
                write_sessions(cfg, recorded_now)

    if args.no_prompt:
        info("skipping first prompts (--no-prompt)")
    else:
        # Give the CLIs a moment to draw their splash, then wait for each one's
        # input box to actually respond before typing a prompt into it.
        boot = max(a.boot_delay_ms for a in started)
        info(f"waiting {boot}ms for agents to boot...")
        sleep_ms(boot)

        for agent in started:
            if agent.name in resumed:
                # It already has the prompt, and the whole conversation after it.
                info(f"{agent.name}: resumed, not re-sending the first prompt")
                continue
            if not agent.first_prompt:
                continue
            try:
                if agent.ready_probe and not wait_until_ready(cfg, agent):
                    warn(
                        f"{agent.name}: input box never responded within "
                        f"{cfg.ready_timeout_ms}ms; sending the prompt anyway"
                    )
                if paste_into(cfg, agent.session, agent.first_prompt):
                    # The agent is now working on it, so it counts as a started turn.
                    with file_lock(cfg, agent.name, "turn.lock"):
                        mark_turn_started(cfg, agent.name, "user")
                    info(f"sent first prompt to {agent.name}")
                else:
                    warn(f"{agent.name}: first prompt may not have been delivered")
                log_event(cfg, agent.name, "first_prompt", text=agent.first_prompt)
            except SwarmError as exc:
                warn(f"{agent.name}: could not send first prompt: {exc}")
            sleep_ms(cfg.send_delay_ms)

    # Watchers start last, so they do not mistake a boot banner or the readiness
    # probe for something the agent said.
    for agent in started:
        if agent.capture == "pane":
            start_watcher(cfg, agent)

    print()
    info(f"swarm {cfg.name!r} is up with {len(started)} agent(s)")
    info(f"attach with:  tmux attach -t {started[0].session}")
    info(f"or:           {SWARM_HOME / 'agentainer'} attach {started[0].name}")

    if args.attach:
        os.execvp("tmux", ["tmux", "attach", "-t", started[0].session])
    return 0


def cmd_down(args) -> int:
    cfg = cfgmod.load(args.config)
    for agent in select_agents(cfg, args.only):
        stop_watcher(cfg, agent)
        if session_exists(agent.session):
            tmux("kill-session", "-t", f"={agent.session}", check=False, capture=True)
            info(f"stopped {agent.name}")
        else:
            info(f"{agent.name}: not running")
    return 0


def cmd_restart(args) -> int:
    cmd_down(args)
    args.restart = True
    return cmd_up(args)


def cmd_status(args) -> int:
    cfg = cfgmod.load(args.config)
    rows = []
    for agent in cfg.agents:
        running = session_exists(agent.session)
        capture = agent.capture
        if capture == "pane":
            capture += " (watching)" if watcher_alive(cfg, agent) else " (watcher down)"

        if not running:
            turn = "-"
        elif not agent.busy_check:
            turn = "untracked"
        else:
            state = busy_info(cfg, agent)
            turn = f"busy {state['age_s']}s" if state else "idle"

        depth = len(queue_read(cfg, agent.name))
        rows.append(
            (
                agent.name,
                agent.type,
                "up" if running else "down",
                turn,
                str(depth) if depth else "-",
                capture,
                ", ".join(agent.can_talk_to) or "-",
            )
        )

    headers = ("AGENT", "TYPE", "STATE", "TURN", "QUEUE", "CAPTURE", "CAN TALK TO")
    widths = [max(len(str(r[i])) for r in (*rows, headers)) for i in range(len(headers))]
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(f"swarm: {cfg.name}   root: {cfg.root}")
    print(line)
    print("  ".join("-" * w for w in widths))
    for row in rows:
        colour = "\033[32m" if row[2] == "up" else "\033[31m"
        cells = [str(c).ljust(widths[i]) for i, c in enumerate(row)]
        cells[2] = f"{colour}{cells[2]}\033[0m"
        print("  ".join(cells))
    return 0


def cmd_queue(args) -> int:
    cfg = cfgmod.load(args.config)
    agent = cfg.get(args.agent)

    if args.clear:
        with file_lock(cfg, agent.name, "queue.lock"):
            dropped = len(queue_read(cfg, agent.name))
            queue_write(cfg, agent.name, [])
        info(f"{agent.name}: dropped {dropped} queued message(s)")
        return 0

    items = queue_read(cfg, agent.name)
    state = busy_info(cfg, agent)
    status = f"busy for {state['age_s']}s (task from {state['by']})" if state else "idle"
    print(f"{agent.name}: {status}, {len(items)} message(s) queued")
    for index, item in enumerate(items, 1):
        first = item["text"].strip().splitlines()[0][:70]
        print(f"  {index}. from {item['from']} at {item['ts']}: {first}")
    return 0


def cmd_idle(args) -> int:
    """Force an agent back to idle -- the escape hatch when a capture never fired."""
    cfg = cfgmod.load(args.config)
    agent = cfg.get(args.agent)
    mark_turn_finished(cfg, agent.name)
    info(f"{agent.name}: marked idle")
    if not args.no_drain:
        drain_queue(cfg, agent)
    return 0


def cmd_sessions(args) -> int:
    cfg = cfgmod.load(args.config)
    agents = read_sessions(cfg)
    if not agents:
        print(f"no conversations recorded yet ({cfg.sessions_file})")
        print("They are written as each agent finishes its first turn.")
        return 0

    if args.raw:
        print(cfg.sessions_file.read_text().rstrip())
        return 0

    print(f"{cfg.sessions_file}\n")
    for name in cfg.names():
        entry = agents.get(name)
        if not entry:
            print(f"  {name}: -")
            continue
        print(f"  {name} ({entry.get('type')})")
        print(f"      conversation: {entry.get('session_id')}")
        print(f"      last seen:    {entry.get('updated_at')}")
        resumable = resume_command(cfg, cfg.get(name), str(entry.get("session_id")))
        print(f"      resume with:  {resumable or '(no resume recipe for this type)'}")
    return 0


def cmd_attach(args) -> int:
    cfg = cfgmod.load(args.config)
    agent = cfg.get(args.agent)
    if not session_exists(agent.session):
        die(f"{agent.name} is not running")
    os.execvp("tmux", ["tmux", "attach", "-t", agent.session])
    return 0


def read_message(args) -> str:
    if args.file:
        return Path(args.file).read_text()
    if args.message == ["-"] or not args.message:
        if sys.stdin.isatty():
            die("no message given (pass it as an argument, with --file, or on stdin)")
        return sys.stdin.read()
    return " ".join(args.message)


def resolve_sender(cfg: SwarmConfig, explicit: str | None) -> str:
    return explicit or os.environ.get("SWARM_AGENT") or "user"


def wait_for_dequeue(cfg: SwarmConfig, recipient: str, item_id: str, timeout_s: float) -> bool:
    """Block until a queued message has been handed to the agent."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not any(i["id"] == item_id for i in queue_read(cfg, recipient)):
            return True
        time.sleep(2)
    return False


def send_message(cfg: SwarmConfig, sender: str, args, text: str) -> int:
    recipient = args.to
    allow_busy = args.force or args.ignore_busy
    deadline = time.monotonic() + args.wait_timeout

    while True:
        try:
            deliver(cfg, sender, recipient, text, enforce_acl=not args.force, allow_busy=allow_busy)
            info(f"{sender} -> {recipient}: delivered")
            return 0
        except BusyError as exc:
            if args.queue:
                item_id, depth = enqueue(cfg, sender, recipient, text, hops=0)
                info(
                    f"{recipient} is busy; queued at position {depth}. "
                    f"It will be delivered as soon as {recipient} is free."
                )
                if not args.wait:
                    return 0
                info(f"waiting for {recipient} to pick it up...")
                if wait_for_dequeue(cfg, recipient, item_id, deadline - time.monotonic()):
                    info(f"{recipient} received your queued message")
                    return 0
                warn(f"still queued after {args.wait_timeout}s; it stays in the queue")
                return 0

            if args.wait:
                if time.monotonic() >= deadline:
                    die(f"{recipient} was still busy after {args.wait_timeout}s")
                time.sleep(3)
                continue

            die(str(exc))


def cmd_send(args) -> int:
    cfg = cfgmod.load(args.config)
    sender = resolve_sender(cfg, args.sender)
    return send_message(cfg, sender, args, read_message(args))


def cmd_broadcast(args) -> int:
    cfg = cfgmod.load(args.config)
    sender = resolve_sender(cfg, args.sender)
    text = read_message(args)

    if sender in cfg.names():
        targets = cfg.get(sender).can_talk_to
    else:
        targets = [a.name for a in cfg.agents]
    if not targets:
        die(f"{sender} has no agents it may talk to")

    failures = 0
    for peer in targets:
        try:
            deliver(
                cfg, sender, peer, text,
                enforce_acl=not args.force,
                allow_busy=args.force or args.ignore_busy,
                # A broadcast is an announcement, never a question -- so it must not
                # saddle every recipient with a reply obligation (and a later nag).
                # Mirrors the tagged <swarm-broadcast> path in parse_outbound().
                expects_reply=False,
            )
            info(f"{sender} -> {peer}: delivered")
        except BusyError as exc:
            if args.queue:
                _, depth = enqueue(cfg, sender, peer, text, hops=0, expects_reply=False)
                info(f"{sender} -> {peer}: busy, queued at position {depth}")
            else:
                warn(f"{sender} -> {peer}: {exc.args[0].splitlines()[0]}")
                failures += 1
        except SwarmError as exc:
            warn(f"{sender} -> {peer}: {exc}")
            failures += 1
    return 1 if failures else 0


def cmd_inbox(args) -> int:
    cfg = cfgmod.load(args.config)
    name = args.agent or os.environ.get("SWARM_AGENT")
    if not name:
        die("specify an agent: agentainer inbox <agent>")
    cfg.get(name)

    box = cfg.inbox_dir / name
    messages = sorted(box.glob("*.md")) if box.is_dir() else []
    if not messages:
        print(f"{name}: inbox is empty")
        return 0
    for path in messages[-args.tail :]:
        print(f"\n\033[1m--- {path.name} ---\033[0m")
        print(path.read_text().rstrip())
    return 0


def cmd_logs(args) -> int:
    cfg = cfgmod.load(args.config)
    name = args.agent or "swarm"
    if name != "swarm":
        cfg.get(name)
    path = cfg.log_dir / f"{name}.jsonl"
    if not path.is_file():
        print(f"no log yet at {path}")
        return 0
    if args.follow:
        os.execvp("tail", ["tail", "-f", str(path)])

    lines = path.read_text().splitlines()[-args.tail :]
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = (rec.get("text") or "").strip().replace("\n", "\n    ")
        detail = rec.get("to") or rec.get("from") or rec.get("source") or ""
        print(f"\033[2m{rec['ts']}\033[0m \033[1m{rec['agent']}\033[0m {rec['kind']} {detail}")
        if text:
            print(f"    {text}")
    return 0


def cmd_validate(args) -> int:
    cfg = cfgmod.load(args.config)
    for message in cfg.warnings:
        warn(message)
    print(f"config ok: {cfg.path}")
    print(f"  swarm:  {cfg.name}")
    print(f"  root:   {cfg.root}")
    print(f"  agents: {len(cfg.agents)}")
    for agent in cfg.agents:
        peers = ", ".join(agent.can_talk_to) or "none"
        fwd = ", ".join(agent.forward_responses_to)
        if agent.workdir.is_dir():
            state = "exists"
        else:
            state = "will be created" if agent.create_workdir else "MISSING"
        print(f"\n  - {agent.name} ({agent.type}, capture={agent.capture})")
        print(f"      command:  {agent.command}")
        print(f"      workdir:  {agent.workdir}  [{state}]")
        print(f"      session:  {agent.session}")
        print(f"      talks to: {peers}")
        if fwd:
            print(f"      auto-forwards responses to: {fwd}")
        if args.show_prompts and agent.first_prompt:
            body = "\n".join(f"      | {l}" for l in agent.first_prompt.splitlines())
            print(f"      first prompt:\n{body}")
    return 0


def cmd_watch(args) -> int:
    cfg = cfgmod.load(args.config)
    run_watcher(cfg, cfg.get(args.agent))
    return 0


def select_agents(cfg: SwarmConfig, only: str | None) -> list[Agent]:
    if not only:
        return list(cfg.agents)
    names = [n.strip() for n in only.split(",") if n.strip()]
    return [cfg.get(n) for n in names]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def default_config() -> str:
    """SWARM_CONFIG, else ./agents.yaml, else the agents.yaml beside agentainer."""
    from_env = os.environ.get("SWARM_CONFIG")
    if from_env:
        return from_env
    for candidate in (Path.cwd() / "agents.yaml", SWARM_HOME / "agents.yaml"):
        if candidate.is_file():
            return str(candidate)
    return "agents.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=os.environ.get("SWARM_PROG", "agentainer"),
        description="Run a swarm of coding agents (claude, codex, gemini, hermes) in tmux.",
    )
    parser.add_argument(
        "-c", "--config", default=default_config(), help="path to the swarm YAML (default: agents.yaml)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_text):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=func)
        # SUPPRESS (not a real default) so that omitting -c here leaves the
        # value parsed from the top-level -c intact instead of overwriting it.
        p.add_argument("-c", "--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        return p

    p_up = add("up", cmd_up, "create agent dirs, install hooks, start tmux sessions, send prompts")
    p_up.add_argument("--only", help="comma-separated subset of agents to start")
    p_up.add_argument(
        "--resume", dest="resume", action="store_true", default=None,
        help="reattach each agent to the conversation recorded in sessions.yaml",
    )
    p_up.add_argument(
        "--no-resume", dest="resume", action="store_false",
        help="start fresh conversations even if swarm.resume is true",
    )
    p_up.add_argument("--restart", action="store_true", help="kill and recreate existing sessions")
    p_up.add_argument("--no-prompt", action="store_true", help="start agents without sending first prompts")
    p_up.add_argument("--attach", action="store_true", help="attach to the first agent once started")

    p_down = add("down", cmd_down, "kill agent tmux sessions and watchers")
    p_down.add_argument("--only", help="comma-separated subset of agents to stop")

    p_restart = add("restart", cmd_restart, "down + up")
    p_restart.add_argument("--only", help="comma-separated subset of agents")
    p_restart.add_argument("--no-prompt", action="store_true")
    p_restart.add_argument("--attach", action="store_true")
    p_restart.add_argument("--resume", dest="resume", action="store_true", default=None)
    p_restart.add_argument("--no-resume", dest="resume", action="store_false")

    add("status", cmd_status, "show which agents are running")

    p_attach = add("attach", cmd_attach, "attach to an agent's tmux session")
    p_attach.add_argument("agent")

    def add_busy_flags(p):
        p.add_argument(
            "--queue", action="store_true",
            help="if the recipient is busy, queue the message instead of failing",
        )
        p.add_argument(
            "--wait", action="store_true",
            help="block until the recipient is free (or, with --queue, until it is picked up)",
        )
        p.add_argument(
            "--wait-timeout", type=float, default=600,
            help="seconds to keep waiting (default: 600)",
        )
        p.add_argument(
            "--ignore-busy", action="store_true",
            help="deliver even if the recipient is mid-task",
        )

    p_send = add("send", cmd_send, "send a message to an agent (permission-checked)")
    p_send.add_argument("--to", required=True, help="recipient agent name")
    p_send.add_argument("--from", dest="sender", help="sender name (default: $SWARM_AGENT or 'user')")
    p_send.add_argument("--file", help="read the message body from a file")
    p_send.add_argument("--force", action="store_true", help="bypass the can_talk_to and busy checks")
    add_busy_flags(p_send)
    p_send.add_argument("message", nargs="*", help="message text, or '-' to read stdin")

    p_bcast = add("broadcast", cmd_broadcast, "send a message to every agent you may talk to")
    p_bcast.add_argument("--from", dest="sender")
    p_bcast.add_argument("--file")
    p_bcast.add_argument("--force", action="store_true")
    add_busy_flags(p_bcast)
    p_bcast.add_argument("message", nargs="*")

    p_sessions = add("sessions", cmd_sessions, "show each agent's recorded conversation id")
    p_sessions.add_argument("--raw", action="store_true", help="print sessions.yaml verbatim")

    p_queue = add("queue", cmd_queue, "show (or clear) the messages waiting for a busy agent")
    p_queue.add_argument("agent")
    p_queue.add_argument("--clear", action="store_true", help="discard everything queued")

    p_idle = add("idle", cmd_idle, "force an agent back to idle if a capture never fired")
    p_idle.add_argument("agent")
    p_idle.add_argument("--no-drain", action="store_true", help="do not deliver queued messages")

    p_inbox = add("inbox", cmd_inbox, "print archived messages received by an agent")
    p_inbox.add_argument("agent", nargs="?")
    p_inbox.add_argument("-n", "--tail", type=int, default=5)

    p_logs = add("logs", cmd_logs, "print the swarm event log")
    p_logs.add_argument("agent", nargs="?", help="agent name, or omit for the whole swarm")
    p_logs.add_argument("-n", "--tail", type=int, default=20)
    p_logs.add_argument("-f", "--follow", action="store_true")

    p_val = add("validate", cmd_validate, "parse the config and print the resolved swarm")
    p_val.add_argument("--show-prompts", action="store_true", help="also print each agent's first prompt")

    p_hook = add("hook", cmd_hook, "internal: called by an agent's completion hook")
    p_hook.add_argument("type", choices=["claude", "codex", "generic"])
    p_hook.add_argument("payload", nargs="?", help="JSON payload (codex passes it as argv)")
    p_hook.add_argument("--agent", help="override the detected agent name")

    p_watch = add("watch", cmd_watch, "internal: poll an agent's tmux pane for completed turns")
    p_watch.add_argument("agent")

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # `swarm agents.yaml` and `swarm ./x.yaml up` both mean "up with this config".
    if argv and not argv[0].startswith("-") and argv[0].endswith((".yaml", ".yml")):
        path = argv.pop(0)
        argv = [*(argv or ["up"]), "-c", path]

    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, SwarmError) as exc:
        die(str(exc))
    except subprocess.CalledProcessError as exc:
        die(f"command failed: {exc}")
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
