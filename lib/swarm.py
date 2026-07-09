#!/usr/bin/env python3
"""AgentSwarm -- run a swarm of coding agents in tmux and let them talk.

Invoked through ``swarm.sh``; see ``swarm.sh --help`` and README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

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
# Claude Code collapses a long paste into a "[Pasted text #1 +N lines]" chip,
# so the text itself never appears on screen. Treat the chip as proof of arrival.
PASTE_CHIP = "Pastedtext"


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
    return pane.count(needle) + pane.count(PASTE_CHIP)


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


def paste_into(cfg: SwarmConfig, session: str, text: str, enter: bool = True) -> bool:
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

    needle = needle_for(body)
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


def log_event(cfg: SwarmConfig, agent: str, kind: str, **fields) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    record = {"ts": now_iso(), "agent": agent, "kind": kind, **fields}
    with (cfg.log_dir / f"{agent}.jsonl").open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    with (cfg.log_dir / "swarm.jsonl").open("a") as fh:
        fh.write(json.dumps(record) + "\n")


def archive_message(cfg: SwarmConfig, sender: str, recipient: str, text: str) -> Path:
    box = cfg.inbox_dir / recipient
    box.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3]
    path = box / f"{stamp}-from-{sender}.md"
    path.write_text(f"# message from {sender}\n\n_{now_iso()}_\n\n{text.rstrip()}\n")
    return path


MESSAGE_HEADER = "[swarm] message from"
ECHO_MEMORY = 300


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
) -> None:
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
            "Start it with: swarm up"
        )

    archived = archive_message(cfg, sender, recipient, text)
    header = f"{MESSAGE_HEADER} {sender}:"
    body = f"{header}\n{text}"
    if target.capture == "pane":
        record_echo(cfg, recipient, body)

    if not paste_into(cfg, target.session, body):
        raise SwarmError(
            f"could not confirm the message reached {recipient!r}; "
            f"inspect it with: swarm attach {recipient}"
        )

    write_hops(cfg, recipient, hops)
    log_event(cfg, sender, "sent", to=recipient, hops=hops, text=text)
    log_event(cfg, recipient, "received", **{"from": sender}, hops=hops, archived=str(archived))


def forward_response(cfg: SwarmConfig, agent: Agent, text: str) -> None:
    """Auto-forward a captured turn to the agent's forward_responses_to list."""
    if not agent.forward_responses_to or not text.strip():
        return

    hops = read_hops(cfg, agent.name) + 1
    if hops > cfg.max_forward_hops:
        warn(
            f"{agent.name}: forward hop limit ({cfg.max_forward_hops}) reached; "
            "not forwarding further"
        )
        log_event(cfg, agent.name, "forward_suppressed", hops=hops)
        return

    for peer in agent.forward_responses_to:
        try:
            deliver(cfg, agent.name, peer, text, hops=hops)
        except SwarmError as exc:
            warn(f"forward {agent.name} -> {peer} failed: {exc}")


# --------------------------------------------------------------------------
# capture: hook installation
# --------------------------------------------------------------------------


def install_claude_hook(agent: Agent) -> None:
    settings_path = agent.workdir / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    settings: dict = {}
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            warn(f"{settings_path} is not valid JSON; overwriting")

    hook_cmd = str(HOOKS_DIR / "claude_stop.sh")
    entry = {"matcher": "*", "hooks": [{"type": "command", "command": hook_cmd}]}
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
        "# installed by AgentSwarm -- fires when codex finishes a turn.",
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
        ]

        text = "\n".join(new_lines).strip()
        if len(text) < 2:
            continue
        log_event(cfg, agent.name, "response", source="pane", text=text)
        forward_response(cfg, agent, text)


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


def extract_claude_response(payload: dict) -> str:
    """Pull the final assistant text out of a Claude Code transcript."""
    transcript = payload.get("transcript_path")
    if not transcript or not Path(transcript).is_file():
        return ""

    last = ""
    with open(transcript) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "assistant":
                continue
            content = record.get("message", {}).get("content", [])
            if isinstance(content, str):
                last = content
                continue
            chunks = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if any(c.strip() for c in chunks):
                last = "\n".join(c for c in chunks if c.strip())
    return last.strip()


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
        text = extract_claude_response(payload)
    elif args.type == "codex":
        try:
            payload = json.loads(args.payload or "{}")
        except json.JSONDecodeError:
            payload = {}
        if payload.get("type") != "agent-turn-complete":
            return 0
        text = str(payload.get("last-assistant-message") or "").strip()
    else:
        text = sys.stdin.read().strip()

    if not text:
        return 0

    log_event(cfg, agent.name, "response", source=f"hook:{args.type}", text=text)
    forward_response(cfg, agent, text)
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
        "# Generated by AgentSwarm. Lets an agent run `swarm send ...` from its shell.\n"
        f'exec {shlex.quote(str(SWARM_HOME / "swarm.sh"))} "$@"\n'
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


def start_agent(cfg: SwarmConfig, agent: Agent) -> None:
    agent.workdir.mkdir(parents=True, exist_ok=True)
    extra_env = install_capture(cfg, agent)
    env = session_env(cfg, agent, extra_env)

    exports = " ".join(f"export {k}={shlex.quote(v)};" for k, v in env.items())
    exports += f' export PATH={shlex.quote(str(cfg.bin_dir))}:"$PATH";'

    inner = (
        f"{exports} "
        f"cd {shlex.quote(str(agent.workdir))} || exit 1; "
        f"{agent.command}; "
        f'status=$?; printf "\\n[swarm] agent %s exited (status %s)\\n" '
        f"{shlex.quote(agent.name)} \"$status\"; "
        'exec "${SHELL:-bash}" -l'
    )
    launcher = f"exec bash -lc {shlex.quote(inner)}"

    tmux("new-session", "-d", "-s", agent.session, "-c", str(agent.workdir), launcher)
    info(f"started {agent.name} ({agent.type}) in tmux session {agent.session!r}")


def cmd_up(args) -> int:
    cfg = cfgmod.load(args.config)
    if not shutil.which("tmux"):
        die("tmux is required but was not found on PATH")

    selected = select_agents(cfg, args.only)

    for directory in (cfg.runtime, cfg.log_dir, cfg.inbox_dir, cfg.run_dir, cfg.bin_dir):
        directory.mkdir(parents=True, exist_ok=True)
    write_shim(cfg)
    write_state(cfg)

    started: list[Agent] = []
    for agent in selected:
        if session_exists(agent.session):
            if not args.restart:
                warn(f"{agent.name}: session {agent.session!r} already exists, skipping")
                continue
            info(f"{agent.name}: restarting")
            stop_watcher(cfg, agent)
            tmux("kill-session", "-t", f"={agent.session}", check=False, capture=True)
        start_agent(cfg, agent)
        started.append(agent)

    if not started:
        info("nothing to start")
        return 0

    if args.no_prompt:
        info("skipping first prompts (--no-prompt)")
    else:
        # Give the CLIs a moment to draw their splash, then wait for each one's
        # input box to actually respond before typing a prompt into it.
        boot = max(a.boot_delay_ms for a in started)
        info(f"waiting {boot}ms for agents to boot...")
        sleep_ms(boot)

        for agent in started:
            if not agent.first_prompt:
                continue
            try:
                if agent.ready_probe and not wait_until_ready(cfg, agent):
                    warn(
                        f"{agent.name}: input box never responded within "
                        f"{cfg.ready_timeout_ms}ms; sending the prompt anyway"
                    )
                if paste_into(cfg, agent.session, agent.first_prompt):
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
    info(f"or:           {SWARM_HOME / 'swarm.sh'} attach {started[0].name}")

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
        rows.append(
            (
                agent.name,
                agent.type,
                "up" if running else "down",
                agent.session,
                capture,
                ", ".join(agent.can_talk_to) or "-",
            )
        )

    headers = ("AGENT", "TYPE", "STATE", "SESSION", "CAPTURE", "CAN TALK TO")
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


def cmd_send(args) -> int:
    cfg = cfgmod.load(args.config)
    sender = resolve_sender(cfg, args.sender)
    deliver(cfg, sender, args.to, read_message(args), enforce_acl=not args.force)
    info(f"{sender} -> {args.to}: delivered")
    return 0


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
            deliver(cfg, sender, peer, text, enforce_acl=not args.force)
            info(f"{sender} -> {peer}: delivered")
        except SwarmError as exc:
            warn(f"{sender} -> {peer}: {exc}")
            failures += 1
    return 1 if failures else 0


def cmd_inbox(args) -> int:
    cfg = cfgmod.load(args.config)
    name = args.agent or os.environ.get("SWARM_AGENT")
    if not name:
        die("specify an agent: swarm inbox <agent>")
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
    print(f"config ok: {cfg.path}")
    print(f"  swarm:  {cfg.name}")
    print(f"  root:   {cfg.root}")
    print(f"  agents: {len(cfg.agents)}")
    for agent in cfg.agents:
        peers = ", ".join(agent.can_talk_to) or "none"
        fwd = ", ".join(agent.forward_responses_to)
        print(f"\n  - {agent.name} ({agent.type}, capture={agent.capture})")
        print(f"      command:  {agent.command}")
        print(f"      workdir:  {agent.workdir}")
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
    """SWARM_CONFIG, else ./agents.yaml, else the agents.yaml beside swarm.sh."""
    from_env = os.environ.get("SWARM_CONFIG")
    if from_env:
        return from_env
    for candidate in (Path.cwd() / "agents.yaml", SWARM_HOME / "agents.yaml"):
        if candidate.is_file():
            return str(candidate)
    return "agents.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swarm",
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
    p_up.add_argument("--restart", action="store_true", help="kill and recreate existing sessions")
    p_up.add_argument("--no-prompt", action="store_true", help="start agents without sending first prompts")
    p_up.add_argument("--attach", action="store_true", help="attach to the first agent once started")

    p_down = add("down", cmd_down, "kill agent tmux sessions and watchers")
    p_down.add_argument("--only", help="comma-separated subset of agents to stop")

    p_restart = add("restart", cmd_restart, "down + up")
    p_restart.add_argument("--only", help="comma-separated subset of agents")
    p_restart.add_argument("--no-prompt", action="store_true")
    p_restart.add_argument("--attach", action="store_true")

    add("status", cmd_status, "show which agents are running")

    p_attach = add("attach", cmd_attach, "attach to an agent's tmux session")
    p_attach.add_argument("agent")

    p_send = add("send", cmd_send, "send a message to an agent (permission-checked)")
    p_send.add_argument("--to", required=True, help="recipient agent name")
    p_send.add_argument("--from", dest="sender", help="sender name (default: $SWARM_AGENT or 'user')")
    p_send.add_argument("--file", help="read the message body from a file")
    p_send.add_argument("--force", action="store_true", help="bypass the can_talk_to check")
    p_send.add_argument("message", nargs="*", help="message text, or '-' to read stdin")

    p_bcast = add("broadcast", cmd_broadcast, "send a message to every agent you may talk to")
    p_bcast.add_argument("--from", dest="sender")
    p_bcast.add_argument("--file")
    p_bcast.add_argument("--force", action="store_true")
    p_bcast.add_argument("message", nargs="*")

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
