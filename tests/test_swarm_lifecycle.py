"""Tests for the lifecycle + CLI command layer of lib/swarm.py.

These exercise cmd_up/down/status/queue/idle/sessions/attach/send/broadcast/
inbox/logs/validate/watch, the CLI plumbing (main/build_parser/default_config/
read_message/resolve_sender/select_agents/session_env/resume_command/write_shim/
start_agent), and send_message. tmux is fully mocked so nothing real runs; we
assert on filesystem effects and stdout.
"""

import argparse
import io
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import config
import swarm
from tests.support import agent_yaml, load_swarm, mock_tmux


# --------------------------------------------------------------------- helpers

def ns(**kw):
    return argparse.Namespace(**kw)


def two_agent_config(tmp_path):
    root = tmp_path / "ws"
    return str(agent_yaml(
        tmp_path,
        "  - {name: A, command: \"cat\", can_talk_to: [B]}\n"
        "  - {name: B, command: \"cat\", can_talk_to: [A]}\n",
        name="t",
        root=root,
        session_prefix="t-",
        ready_timeout_ms=0,
        tmux_history_limit=1000,
    ))


def pane_config(tmp_path):
    root = tmp_path / "pw"
    path = tmp_path / "pane.yaml"
    path.write_text(
        "swarm: {name: p, root: " + str(root) + ", session_prefix: 'p-', ready_timeout_ms: 0}\n"
        "agent_types:\n"
        "  pane: {command: \"cat\", capture: pane, type: claude, boot_delay_ms: 0,\n"
        "         ready_probe: false, append_agents_that_you_can_talk_to_prompt: false}\n"
        "agents:\n"
        "  - {name: P, type: pane, can_talk_to: []}\n"
    )
    return str(path)


# ------------------------------------------------------------------- select/cfg

def test_select_agents_none(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n- {name: B, command: 'true'}\n")
    assert [a.name for a in swarm.select_agents(cfg, None)] == ["A", "B"]


def test_select_agents_subset(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n- {name: B, command: 'true'}\n")
    assert [a.name for a in swarm.select_agents(cfg, "A")] == ["A"]
    assert [a.name for a in swarm.select_agents(cfg, "A,B")] == ["A", "B"]


def test_default_config_from_env(monkeypatch):
    monkeypatch.setenv("SWARM_CONFIG", "/x/y.yaml")
    assert swarm.default_config() == "/x/y.yaml"


def test_default_config_no_env(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    # neither ./agents.yaml nor SWARM_HOME/agents.yaml exist -> literal fallback
    assert swarm.default_config() == "agents.yaml"


# ----------------------------------------------------------------- session_env

def test_session_env_merges(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true', env: {FOO: bar}}\n")
    agent = cfg.get("A")
    env = swarm.session_env(cfg, agent, {"EXTRA": "1"})
    assert env["SWARM_AGENT"] == "A"
    assert env["SWARM_SESSION"] == agent.session
    assert env["SWARM_PEERS"] == ",".join(agent.can_talk_to)
    assert env["FOO"] == "bar"
    assert env["EXTRA"] == "1"


# --------------------------------------------------------------- resume_command

def test_resume_command_via_resume_args(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'claude', type: claude}\n")
    agent = cfg.get("A")
    assert swarm.resume_command(cfg, agent, "sess-9") == "claude --resume sess-9"


def test_resume_command_via_resume_command(tmp_path):
    cfg = load_swarm(
        tmp_path,
        "- {name: A, command: 'claude', type: claude, resume_command: 'echo {session_id}'}\n",
    )
    agent = cfg.get("A")
    assert swarm.resume_command(cfg, agent, "sess-9") == "echo sess-9"


def test_resume_command_none_when_no_recipe(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'cat', type: gemini}\n")
    agent = cfg.get("A")
    assert swarm.resume_command(cfg, agent, "sess-9") is None


def test_resume_command_malformed_is_none(tmp_path):
    cfg = load_swarm(
        tmp_path,
        "- {name: A, command: 'claude', type: claude, resume_command: 'echo {nope}'}\n",
    )
    agent = cfg.get("A")
    assert swarm.resume_command(cfg, agent, "sess-9") is None


# -------------------------------------------------------------------- write_shim

def test_write_shim_creates_executable(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    swarm.write_shim(cfg)
    shim = cfg.bin_dir / "swarm"
    assert shim.is_file()
    assert (shim.stat().st_mode & 0o755) == 0o755
    assert "agentainer" in shim.read_text()


# ------------------------------------------------------------------- start_agent

def test_start_agent_creates_workdir_and_launches(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'cat', type: claude, can_talk_to: []}\n")
    agent = cfg.get("A")
    with mock_tmux(has_session=False) as r, mock.patch.object(swarm, "install_capture", return_value={}):
        swarm.start_agent(cfg, agent)
    assert agent.workdir.is_dir()
    assert (cfg.run_dir / "A.turn.json").is_file()
    assert any(c[1:3] == ["new-session", "-d"] for c in r.calls)


def test_start_agent_resume_cmd(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'cat', type: claude, can_talk_to: []}\n")
    agent = cfg.get("A")
    with mock_tmux(has_session=False) as r, mock.patch.object(swarm, "install_capture", return_value={}):
        swarm.start_agent(cfg, agent, resume_cmd="echo resumed")
    # The resume command must appear inside the launcher we hand tmux.
    launched = [c for c in r.calls if c[1:3] == ["new-session", "-d"]]
    assert launched and "echo resumed" in launched[0][-1]


def test_start_agent_missing_workdir_no_create(tmp_path):
    # The config validator refuses this at load time, so flip create_workdir off
    # only after load to reach start_agent's own (defensive) guard.
    cfg = load_swarm(
        tmp_path,
        "- {name: A, command: 'cat', type: claude, can_talk_to: [], create_workdir: true}\n",
    )
    agent = cfg.get("A")
    agent.create_workdir = False
    if agent.workdir.exists():
        import shutil
        shutil.rmtree(agent.workdir)
    with mock_tmux(has_session=False), mock.patch.object(swarm, "install_capture", return_value={}):
        with pytest.raises(swarm.SwarmError):
            swarm.start_agent(cfg, agent)


# ------------------------------------------------------------------------ cmd_up

def test_cmd_up_no_prompt(tmp_path):
    path = two_agent_config(tmp_path)
    with mock_tmux(has_session=False) as r:
        rc = swarm.cmd_up(ns(config=path, only=None, resume=None, no_prompt=True,
                             attach=False, restart=False))
    assert rc == 0
    # both agents launched
    new = [c for c in r.calls if c[1:3] == ["new-session", "-d"]]
    assert len(new) >= 2


def test_cmd_up_existing_session_skipped(tmp_path):
    path = two_agent_config(tmp_path)
    with mock_tmux(has_session=True):
        rc = swarm.cmd_up(ns(config=path, only=None, resume=None, no_prompt=True,
                             attach=False, restart=False))
    assert rc == 0


def test_cmd_up_restart_kills_then_starts(tmp_path):
    path = two_agent_config(tmp_path)
    with mock_tmux(has_session=True) as r:
        rc = swarm.cmd_up(ns(config=path, only=None, resume=None, no_prompt=True,
                             attach=False, restart=True))
    assert rc == 0
    assert any(c[1] == "kill-session" for c in r.calls)
    assert any(c[1:3] == ["new-session", "-d"] for c in r.calls)


def test_cmd_up_first_prompt(tmp_path):
    root = tmp_path / "ws"
    path = agent_yaml(
        tmp_path,
        "  - {name: A, command: \"cat\", can_talk_to: [], first_prompt: 'hello A'}\n",
        name="t", root=root, session_prefix="t-", ready_timeout_ms=0,
        tmux_history_limit=1000,
    )
    with mock_tmux(has_session=False), \
         mock.patch.object(swarm, "install_capture", return_value={}), \
         mock.patch.object(swarm, "wait_until_ready", return_value=True), \
         mock.patch.object(swarm, "paste_into", return_value=True):
        rc = swarm.cmd_up(ns(config=path, only=None, resume=None, no_prompt=False,
                             attach=False, restart=False))
    assert rc == 0
    lines = (root / ".swarm" / "logs" / "A.jsonl").read_text().splitlines()
    assert any('"first_prompt"' in l for l in lines)


def test_cmd_up_attach_execs(tmp_path):
    path = two_agent_config(tmp_path)
    with mock_tmux(has_session=False), \
         mock.patch.object(swarm.os, "execvp") as execvp:
        swarm.cmd_up(ns(config=path, only=None, resume=None, no_prompt=True,
                        attach=True, restart=False))
    execvp.assert_called_once()


def test_cmd_up_skips_empty_first_prompt(tmp_path):
    # An agent configured with no first prompt (and no appended notice) must be
    # launched without attempting to paste anything.
    root = tmp_path / "ws"
    path = agent_yaml(
        tmp_path,
        "  - {name: A, command: \"cat\", can_talk_to: [], "
        "append_agents_that_you_can_talk_to_prompt: false, "
        "in_first_prompt_append_your_task_will_be_sent_in_the_next_prompt: false}\n",
        name="t", root=root, session_prefix="t-", ready_timeout_ms=0,
        tmux_history_limit=1000,
    )
    cfg = config.load(path)
    assert cfg.get("A").first_prompt == ""
    with mock_tmux(has_session=False), \
         mock.patch.object(swarm, "install_capture", return_value={}), \
         mock.patch.object(swarm, "start_supervisor"), \
         mock.patch.object(swarm, "paste_into") as paste:
        rc = swarm.cmd_up(ns(config=path, only=None, resume=None, no_prompt=False,
                             attach=False, restart=False))
    assert rc == 0
    paste.assert_not_called()  # nothing to send


def test_cmd_up_first_prompt_delivery_error(tmp_path):
    # If pasting the first prompt fails, up must not crash: it warns and continues.
    root = tmp_path / "ws"
    path = agent_yaml(
        tmp_path,
        "  - {name: A, command: \"cat\", can_talk_to: [], first_prompt: 'do the thing', "
        "append_agents_that_you_can_talk_to_prompt: false, "
        "in_first_prompt_append_your_task_will_be_sent_in_the_next_prompt: false}\n",
        name="t", root=root, session_prefix="t-", ready_timeout_ms=0,
        tmux_history_limit=1000,
    )
    with mock_tmux(has_session=False), \
         mock.patch.object(swarm, "install_capture", return_value={}), \
         mock.patch.object(swarm, "start_supervisor"), \
         mock.patch.object(swarm, "paste_into", side_effect=swarm.SwarmError("pane gone")):
        rc = swarm.cmd_up(ns(config=path, only=None, resume=None, no_prompt=False,
                             attach=False, restart=False))
    assert rc == 0


def test_cmd_up_resume_reattaches(tmp_path):
    root = tmp_path / "ws"
    path = tmp_path / "resume.yaml"
    path.write_text(
        "swarm: {name: t, root: " + str(root) + ", session_prefix: 't-',\n"
        "        ready_timeout_ms: 0, tmux_history_limit: 1000, resume: true}\n"
        "defaults: {type: claude}\n"
        "agents:\n"
        "  - {name: A, command: \"claude\", type: claude, can_talk_to: []}\n"
    )
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    swarm.write_sessions(cfg, {"A": {"session_id": "sess-aaa", "type": "claude",
                                     "updated_at": "2020-01-01T00:00:00Z"}})
    with mock_tmux(has_session=False) as r, \
         mock.patch.object(swarm, "install_capture", return_value={}):
        rc = swarm.cmd_up(ns(config=str(path), only=None, resume=None, no_prompt=True,
                             attach=False, restart=False))
    assert rc == 0
    launched = [c for c in r.calls if c[1:3] == ["new-session", "-d"]]
    assert any("--resume sess-aaa" in c[-1] for c in launched)


# ---------------------------------------------------------------------- cmd_down

def test_cmd_down_kills_sessions(tmp_path):
    path = two_agent_config(tmp_path)
    with mock_tmux(has_session=True) as r:
        rc = swarm.cmd_down(ns(config=path, only=None))
    assert rc == 0
    assert any(c[1] == "kill-session" for c in r.calls)


def test_cmd_down_only_subset(tmp_path):
    path = two_agent_config(tmp_path)
    with mock_tmux(has_session=True) as r:
        rc = swarm.cmd_down(ns(config=path, only="A"))
    assert rc == 0
    killed = [c for c in r.calls if c[1] == "kill-session"]
    joined = [" ".join(c) for c in killed]
    # only A was targeted, not B
    assert any("t-A" in j for j in joined)
    assert not any("t-B" in j for j in joined)


# ------------------------------------------------------------------- cmd_restart

def test_cmd_restart(tmp_path):
    path = two_agent_config(tmp_path)
    with mock_tmux(has_session=True) as r:
        rc = swarm.cmd_restart(ns(config=path, only=None, no_prompt=True, attach=False,
                                  resume=None))
    assert rc == 0
    assert any(c[1] == "kill-session" for c in r.calls)
    assert any(c[1:3] == ["new-session", "-d"] for c in r.calls)


# -------------------------------------------------------------------- cmd_status

def test_cmd_status_up_and_idle(tmp_path, capsys):
    path = two_agent_config(tmp_path)
    with mock_tmux(has_session=True), \
         mock.patch.object(swarm, "busy_info", return_value=None), \
         mock.patch.object(swarm, "watcher_alive", return_value=False):
        rc = swarm.cmd_status(ns(config=path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "A" in out and "B" in out and "up" in out


def test_cmd_status_down_and_busy(tmp_path, capsys):
    path = two_agent_config(tmp_path)
    with mock_tmux(has_session=False), \
         mock.patch.object(swarm, "busy_info", return_value={"age_s": 3, "by": "B"}), \
         mock.patch.object(swarm, "watcher_alive", return_value=False):
        rc = swarm.cmd_status(ns(config=path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "down" in out


def test_cmd_status_pane_watching(tmp_path, capsys):
    path = pane_config(tmp_path)
    with mock_tmux(has_session=True), \
         mock.patch.object(swarm, "busy_info", return_value=None), \
         mock.patch.object(swarm, "watcher_alive", return_value=True):
        rc = swarm.cmd_status(ns(config=path))
    assert rc == 0
    assert "watching" in capsys.readouterr().out


def test_cmd_status_pane_watcher_down(tmp_path, capsys):
    path = pane_config(tmp_path)
    with mock_tmux(has_session=True), \
         mock.patch.object(swarm, "busy_info", return_value=None), \
         mock.patch.object(swarm, "watcher_alive", return_value=False):
        rc = swarm.cmd_status(ns(config=path))
    assert rc == 0
    assert "watcher down" in capsys.readouterr().out


def test_main_runs_as_script(tmp_path):
    # Exercises the `if __name__ == "__main__"` guard: running the module as a
    # script must reach `sys.exit(main())` and dispatch a real subcommand.
    root = tmp_path / "ws"
    path = agent_yaml(
        tmp_path,
        "  - {name: A, command: 'cat', can_talk_to: []}\n",
        name="t", root=root, session_prefix="t-", ready_timeout_ms=0,
        tmux_history_limit=1000,
    )
    r = subprocess.run(
        [sys.executable, swarm.__file__, "validate", "-c", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0
    assert "A" in r.stdout


# -------------------------------------------------------------------- cmd_queue

def test_cmd_queue_show(tmp_path, capsys):
    path = two_agent_config(tmp_path)
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: [B]}\n"
                     "- {name: B, command: 'true', can_talk_to: []}\n")
    swarm.enqueue(cfg, "A", "B", "hi there", hops=0)
    with mock_tmux(has_session=True), \
         mock.patch.object(swarm, "busy_info", return_value=None):
        rc = swarm.cmd_queue(ns(config=path, agent="B", clear=False))
    assert rc == 0
    assert "hi there" in capsys.readouterr().out


def test_cmd_queue_clear(tmp_path):
    path = two_agent_config(tmp_path)
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: [B]}\n"
                     "- {name: B, command: 'true', can_talk_to: []}\n")
    swarm.enqueue(cfg, "A", "B", "hi there", hops=0)
    with mock_tmux(has_session=True):
        rc = swarm.cmd_queue(ns(config=path, agent="B", clear=True))
    assert rc == 0
    assert swarm.queue_read(cfg, "B") == []


# --------------------------------------------------------------------- cmd_idle

def test_cmd_idle_marks_idle_and_drains(tmp_path):
    path = two_agent_config(tmp_path)
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: [B]}\n"
                     "- {name: B, command: 'true', can_talk_to: []}\n")
    swarm.write_turn_state(cfg, "B", {"delivered": 1, "completed": 0, "since": 0, "by": "A"})
    with mock_tmux(has_session=True), \
         mock.patch.object(swarm, "drain_queue") as drain:
        rc = swarm.cmd_idle(ns(config=path, agent="B", no_drain=False))
    assert rc == 0
    drain.assert_called_once()
    assert swarm.turn_state(cfg, "B")["completed"] == 1


def test_cmd_idle_no_drain(tmp_path):
    path = two_agent_config(tmp_path)
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: [B]}\n"
                     "- {name: B, command: 'true', can_talk_to: []}\n")
    with mock_tmux(has_session=True), \
         mock.patch.object(swarm, "drain_queue") as drain:
        swarm.cmd_idle(ns(config=path, agent="B", no_drain=True))
    drain.assert_not_called()


# ------------------------------------------------------------------ cmd_sessions

def test_cmd_sessions_empty(tmp_path, capsys):
    path = two_agent_config(tmp_path)
    with mock_tmux(has_session=True):
        rc = swarm.cmd_sessions(ns(config=path, raw=False))
    assert rc == 0
    assert "no conversations" in capsys.readouterr().out


def test_cmd_sessions_recorded(tmp_path, capsys):
    path = two_agent_config(tmp_path)
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n- {name: B, command: 'true'}\n")
    swarm.write_sessions(cfg, {"A": {"session_id": "sess-a", "type": "claude",
                                     "updated_at": "x"}})
    with mock_tmux(has_session=True):
        rc = swarm.cmd_sessions(ns(config=path, raw=False))
    assert rc == 0
    assert "sess-a" in capsys.readouterr().out


def test_cmd_sessions_raw(tmp_path, capsys):
    path = two_agent_config(tmp_path)
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n- {name: B, command: 'true'}\n")
    swarm.write_sessions(cfg, {"A": {"session_id": "sess-a", "type": "claude",
                                     "updated_at": "x"}})
    with mock_tmux(has_session=True):
        rc = swarm.cmd_sessions(ns(config=path, raw=True))
    assert rc == 0
    assert "sess-a" in capsys.readouterr().out


# -------------------------------------------------------------------- cmd_attach

def test_cmd_attach_execs(tmp_path):
    path = two_agent_config(tmp_path)
    with mock_tmux(has_session=True), \
         mock.patch.object(swarm.os, "execvp") as execvp:
        swarm.cmd_attach(ns(config=path, agent="A"))
    execvp.assert_called_once()


def test_cmd_attach_not_running_dies(tmp_path):
    path = two_agent_config(tmp_path)
    with mock_tmux(has_session=False):
        with pytest.raises(SystemExit):
            swarm.cmd_attach(ns(config=path, agent="A"))


# ------------------------------------------------------------------- read_message

def test_read_message_from_args():
    assert swarm.read_message(ns(file=None, message=["hi", "there"])) == "hi there"


def test_read_message_from_file(tmp_path):
    p = tmp_path / "msg.txt"
    p.write_text("file body")
    assert swarm.read_message(ns(file=str(p), message=[])) == "file body"


def test_read_message_from_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("stdin body\n"))
    assert swarm.read_message(ns(file=None, message=[])) == "stdin body\n"


def test_read_message_empty_on_tty_dies(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    with pytest.raises(SystemExit):
        swarm.read_message(ns(file=None, message=[]))


# ----------------------------------------------------------------- resolve_sender

def test_resolve_sender_explicit():
    assert swarm.resolve_sender(None, "X") == "X"


def test_resolve_sender_from_env(monkeypatch):
    monkeypatch.setenv("SWARM_AGENT", "envagent")
    assert swarm.resolve_sender(None, None) == "envagent"


def test_resolve_sender_default_user(monkeypatch):
    monkeypatch.delenv("SWARM_AGENT", raising=False)
    assert swarm.resolve_sender(None, None) == "user"


# --------------------------------------------------------------- wait_for_dequeue

def test_wait_for_dequeue_absent(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true', can_talk_to: []}\n")
    assert swarm.wait_for_dequeue(cfg, "A", "m-1", 0.1) is True


def test_wait_for_dequeue_timeout(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true', can_talk_to: []}\n")
    swarm.enqueue(cfg, "user", "A", "x", hops=0)
    with mock.patch.object(swarm.time, "sleep"):
        assert swarm.wait_for_dequeue(cfg, "A", swarm.queue_read(cfg, "A")[0]["id"], 0.0) is False


def test_wait_for_dequeue_removed_while_waiting(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true', can_talk_to: []}\n")
    item = swarm.enqueue(cfg, "user", "A", "x", hops=0)[0]
    calls = {"n": 0}

    def fake_queue_read(c, a):
        calls["n"] += 1
        return [] if calls["n"] > 1 else swarm.queue_read(c, a)

    with mock.patch.object(swarm, "queue_read", fake_queue_read), \
         mock.patch.object(swarm.time, "sleep"):
        assert swarm.wait_for_dequeue(cfg, "A", item, 10.0) is True


# ------------------------------------------------------------------- send_message

def test_send_message_success(tmp_path):
    path = two_agent_config(tmp_path)
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: [B]}\n"
                     "- {name: B, command: 'true', can_talk_to: []}\n")
    args = ns(to="B", force=False, ignore_busy=False, queue=False, wait=False, wait_timeout=600)
    with mock.patch.object(swarm, "deliver", return_value="m-1"):
        assert swarm.send_message(cfg, "A", args, "hi") == 0


def test_send_message_busy_queue(tmp_path):
    path = two_agent_config(tmp_path)
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: [B]}\n"
                     "- {name: B, command: 'true', can_talk_to: []}\n")
    args = ns(to="B", force=False, ignore_busy=False, queue=True, wait=False, wait_timeout=600)
    with mock.patch.object(swarm, "deliver", side_effect=swarm.BusyError("busy")):
        assert swarm.send_message(cfg, "A", args, "hi") == 0
    assert swarm.queue_read(cfg, "B")


def test_send_message_busy_queue_wait_picked_up(tmp_path):
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: [B]}\n"
                     "- {name: B, command: 'true', can_talk_to: []}\n")
    args = ns(to="B", force=False, ignore_busy=False, queue=True, wait=True, wait_timeout=600)
    # first call busy, then delivered; after enqueue the message is picked up.
    item_id = [None]

    def fake_deliver(*a, **kw):
        if item_id[0] is None:
            raise swarm.BusyError("busy")
        return "m-2"

    with mock.patch.object(swarm, "deliver", side_effect=fake_deliver), \
         mock.patch.object(swarm, "enqueue", return_value=("m-1", 1)) as enq, \
         mock.patch.object(swarm, "wait_for_dequeue", return_value=True), \
         mock.patch.object(swarm.time, "sleep"):
        enq.side_effect = lambda *a, **k: (item_id.__setitem__(0, "m-1") or ("m-1", 1))
        assert swarm.send_message(cfg, "A", args, "hi") == 0


def test_send_message_busy_wait_delivered(tmp_path):
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: [B]}\n"
                     "- {name: B, command: 'true', can_talk_to: []}\n")
    args = ns(to="B", force=False, ignore_busy=False, queue=False, wait=True, wait_timeout=600)
    with mock.patch.object(swarm, "deliver", side_effect=[swarm.BusyError("busy"), "m-2"]), \
         mock.patch.object(swarm.time, "sleep"):
        assert swarm.send_message(cfg, "A", args, "hi") == 0


def test_send_message_busy_nowait_dies(tmp_path):
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: [B]}\n"
                     "- {name: B, command: 'true', can_talk_to: []}\n")
    args = ns(to="B", force=False, ignore_busy=False, queue=False, wait=False, wait_timeout=600)
    with mock.patch.object(swarm, "deliver", side_effect=swarm.BusyError("busy")):
        with pytest.raises(SystemExit):
            swarm.send_message(cfg, "A", args, "hi")


def test_send_message_acl_denied_raises(tmp_path):
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: []}\n"
                     "- {name: B, command: 'true', can_talk_to: []}\n")
    args = ns(to="B", force=False, ignore_busy=False, queue=False, wait=False, wait_timeout=600)
    with mock.patch.object(swarm, "deliver", side_effect=swarm.SwarmError("no")):
        with pytest.raises(swarm.SwarmError):
            swarm.send_message(cfg, "A", args, "hi")


# --------------------------------------------------------------------- cmd_send

def test_cmd_send_success(tmp_path):
    path = two_agent_config(tmp_path)
    parser = swarm.build_parser()
    args = parser.parse_args(["send", "--to", "B", "-c", path, "hello"])
    with mock.patch.object(swarm, "deliver", return_value="m-1"):
        assert swarm.cmd_send(args) == 0


def test_cmd_send_busy_queues(tmp_path):
    path = two_agent_config(tmp_path)
    parser = swarm.build_parser()
    args = parser.parse_args(["send", "--to", "B", "--queue", "-c", path, "hello"])
    with mock.patch.object(swarm, "deliver", side_effect=swarm.BusyError("busy")):
        assert swarm.cmd_send(args) == 0


def test_cmd_send_busy_nowait_dies(tmp_path):
    path = two_agent_config(tmp_path)
    parser = swarm.build_parser()
    args = parser.parse_args(["send", "--to", "B", "-c", path, "hello"])
    with mock.patch.object(swarm, "deliver", side_effect=swarm.BusyError("busy")):
        with pytest.raises(SystemExit):
            swarm.cmd_send(args)


# ------------------------------------------------------------------ cmd_broadcast

def test_cmd_broadcast_all(tmp_path):
    path = two_agent_config(tmp_path)
    parser = swarm.build_parser()
    args = parser.parse_args(["broadcast", "-c", path, "announce"])
    with mock.patch.object(swarm, "deliver", return_value="m-1"):
        assert swarm.cmd_broadcast(args) == 0


def test_cmd_broadcast_to_peers_only(tmp_path):
    path = str(agent_yaml(
        tmp_path,
        "  - {name: A, command: 'true', can_talk_to: [B]}\n"
        "  - {name: B, command: 'true', can_talk_to: []}\n"
        "  - {name: C, command: 'true', can_talk_to: []}\n",
        name="t", root=tmp_path / "ws", session_prefix="t-",
    ))
    parser = swarm.build_parser()
    args = parser.parse_args(["broadcast", "--from", "A", "-c", path, "hi"])
    delivered = []

    def fake_deliver(c, s, peer, text, **kw):
        delivered.append(peer)
        return "m-1"

    with mock.patch.object(swarm, "deliver", side_effect=fake_deliver):
        assert swarm.cmd_broadcast(args) == 0
    assert delivered == ["B"]


def test_cmd_broadcast_failure_counts(tmp_path):
    path = two_agent_config(tmp_path)
    parser = swarm.build_parser()
    args = parser.parse_args(["broadcast", "-c", path, "announce"])
    with mock.patch.object(swarm, "deliver", side_effect=swarm.SwarmError("nope")):
        assert swarm.cmd_broadcast(args) == 1


# --------------------------------------------------------------------- cmd_inbox

def test_cmd_inbox_empty(tmp_path, capsys):
    path = two_agent_config(tmp_path)
    with mock_tmux(has_session=True):
        rc = swarm.cmd_inbox(ns(config=path, agent="A", tail=5))
    assert rc == 0
    assert "empty" in capsys.readouterr().out


def test_cmd_inbox_with_message(tmp_path, capsys):
    path = two_agent_config(tmp_path)
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n- {name: B, command: 'true'}\n")
    box = cfg.inbox_dir / "A"
    box.mkdir(parents=True)
    (box / "from-swarm-1.md").write_text("hello inbox")
    with mock_tmux(has_session=True):
        rc = swarm.cmd_inbox(ns(config=path, agent="A", tail=5))
    assert rc == 0
    assert "hello inbox" in capsys.readouterr().out


def test_cmd_inbox_no_agent_dies(tmp_path):
    path = two_agent_config(tmp_path)
    with mock_tmux(has_session=True), mock.patch.object(swarm.os, "environ", {}):
        with pytest.raises(SystemExit):
            swarm.cmd_inbox(ns(config=path, agent=None, tail=5))


# ---------------------------------------------------------------------- cmd_logs

def test_cmd_logs_empty(tmp_path, capsys):
    path = two_agent_config(tmp_path)
    with mock_tmux(has_session=True):
        rc = swarm.cmd_logs(ns(config=path, agent="swarm", tail=20, follow=False))
    assert rc == 0
    assert "no log" in capsys.readouterr().out


def test_cmd_logs_prints(tmp_path, capsys):
    path = two_agent_config(tmp_path)
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n- {name: B, command: 'true'}\n")
    log = cfg.log_dir / "swarm.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"ts":"t","agent":"swarm","kind":"response","text":"line"}\n')
    with mock_tmux(has_session=True):
        rc = swarm.cmd_logs(ns(config=path, agent="swarm", tail=20, follow=False))
    assert rc == 0
    assert "line" in capsys.readouterr().out


def test_cmd_logs_follow_execs(tmp_path):
    path = two_agent_config(tmp_path)
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n- {name: B, command: 'true'}\n")
    log = cfg.log_dir / "swarm.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"ts":"t","agent":"swarm","kind":"response","text":"line"}\n')
    with mock_tmux(has_session=True), \
         mock.patch.object(swarm.os, "execvp") as execvp:
        swarm.cmd_logs(ns(config=path, agent="swarm", tail=20, follow=True))
    execvp.assert_called_once()


# ------------------------------------------------------------------ cmd_validate

def test_cmd_validate(tmp_path, capsys):
    path = two_agent_config(tmp_path)
    with mock_tmux(has_session=True):
        rc = swarm.cmd_validate(ns(config=path, show_prompts=False))
    assert rc == 0
    assert "config ok" in capsys.readouterr().out


def test_cmd_validate_show_prompts(tmp_path, capsys):
    root = tmp_path / "ws"
    path = agent_yaml(
        tmp_path,
        "  - {name: A, command: \"cat\", can_talk_to: [], first_prompt: 'p'}\n",
        name="t", root=root, session_prefix="t-",
    )
    with mock_tmux(has_session=True):
        rc = swarm.cmd_validate(ns(config=path, show_prompts=True))
    assert rc == 0
    assert "first prompt" in capsys.readouterr().out.lower()


# --------------------------------------------------------------------- cmd_watch

def test_cmd_watch_pane(tmp_path):
    path = pane_config(tmp_path)
    with mock_tmux(has_session=True), \
         mock.patch.object(swarm, "run_watcher") as watcher:
        rc = swarm.cmd_watch(ns(config=path, agent="P"))
    assert rc == 0
    watcher.assert_called_once()


def test_cmd_watch_non_pane_dies(tmp_path):
    path = two_agent_config(tmp_path)
    with mock_tmux(has_session=True):
        with pytest.raises(SystemExit):
            swarm.cmd_watch(ns(config=path, agent="A"))


def test_cmd_watch_session_gone_dies(tmp_path):
    path = pane_config(tmp_path)
    with mock_tmux(has_session=False):
        with pytest.raises(SystemExit):
            swarm.cmd_watch(ns(config=path, agent="P"))


# ------------------------------------------------------------------------ main

def test_main_status_returns_zero(tmp_path):
    path = two_agent_config(tmp_path)
    with mock_tmux(has_session=True), \
         mock.patch.object(swarm, "busy_info", return_value=None), \
         mock.patch.object(swarm, "watcher_alive", return_value=False):
        assert swarm.main(["status", "-c", path]) == 0


def test_main_empty_argv_exits():
    with pytest.raises(SystemExit):
        swarm.main([])


def test_main_config_error_exits(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("not: a: valid: mapping:\n  - x\n")
    with pytest.raises(SystemExit):
        swarm.main(["validate", "-c", str(bad)])


def test_main_swarm_error_exits(tmp_path):
    path = two_agent_config(tmp_path)
    with mock.patch.object(swarm, "deliver", side_effect=swarm.SwarmError("denied")):
        with pytest.raises(SystemExit):
            swarm.main(["send", "--to", "B", "-c", path, "hi"])


def test_main_called_process_error_exits(tmp_path):
    path = two_agent_config(tmp_path)
    with mock_tmux(has_session=False), \
         mock.patch.object(swarm, "tmux", side_effect=subprocess.CalledProcessError(1, ["tmux"])):
        with pytest.raises(SystemExit):
            swarm.main(["up", "-c", path, "--no-prompt"])


def test_main_keyboard_interrupt_returns_130(tmp_path):
    path = two_agent_config(tmp_path)
    with mock_tmux(has_session=False), \
         mock.patch.object(swarm, "select_agents", side_effect=KeyboardInterrupt):
        assert swarm.main(["up", "-c", path, "--no-prompt"]) == 130


def test_main_shorthand_config_only(tmp_path):
    path = two_agent_config(tmp_path)
    with mock.patch.object(swarm, "cmd_up") as up, \
         mock.patch.object(swarm, "shutil") as sh:
        sh.which.return_value = "/usr/bin/tmux"
        swarm.main([path])
    assert up.called


def test_main_shorthand_config_with_command(tmp_path):
    path = two_agent_config(tmp_path)
    with mock.patch.object(swarm, "cmd_status") as status, \
         mock.patch.object(swarm, "busy_info", return_value=None), \
         mock.patch.object(swarm, "watcher_alive", return_value=False), \
         mock_tmux(has_session=True):
        swarm.main([path, "status"])
    assert status.called


def test_build_parser_has_all_subcommands():
    sub = swarm.build_parser()._subparsers._group_actions[0].choices
    for name in ("up", "down", "restart", "status", "attach", "send", "broadcast",
                 "sessions", "queue", "idle", "inbox", "logs", "validate", "hook", "watch"):
        assert name in sub
