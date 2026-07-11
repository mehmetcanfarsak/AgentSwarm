"""Edge-case and branch-coverage tests for lib/swarm.py.

These target the defensive and rarely-hit branches that the happy-path tests in
the other files miss: the file-lock timeout, the real ``deliver`` capture/paste
paths, reply-reminder/forward error branches, hook trust-file edge cases, the
tmux-watcher emit loop, ``config_from_state`` discovery, and a long list of
``cmd_*`` branches (warnings, resume caveats, status states, validate workdir
states, broadcast failures, etc.).

tmux is mocked; sleeps are stubbed so nothing stalls.
"""

import argparse
import json
from pathlib import Path
from unittest import mock

import pytest

import config
import swarm
from tests.support import agent_yaml, load_swarm, mock_tmux, fake_delivery

# The autouse _no_real_sleep fixture patches swarm.sleep_ms to a no-op, so the
# real body (and the `time.sleep` call inside it) never runs during tests. Grab
# the genuine function at import time (before any fixture patches it) so we can
# exercise it deliberately.
_ORIG_SLEEP_MS = swarm.sleep_ms


def ns(**kw):
    return argparse.Namespace(**kw)


# ------------------------------------------------------------------- sleep_ms

def test_sleep_ms_real_body(monkeypatch):
    # Restore the real impl, but stop it actually sleeping.
    monkeypatch.setattr(swarm, "sleep_ms", _ORIG_SLEEP_MS)
    monkeypatch.setattr(swarm.time, "sleep", lambda s: None)
    _ORIG_SLEEP_MS(5)   # ms > 0 -> time.sleep path
    _ORIG_SLEEP_MS(0)   # ms == 0 -> early return


# ------------------------------------------------------------------ file_lock

def test_file_lock_timeout_warns_and_proceeds(tmp_path, monkeypatch):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    monkeypatch.setattr(swarm.fcntl, "flock", lambda *a, **k: (_ for _ in ()).throw(OSError("busy")))
    ticks = [0.0, 0.01, 1e12]  # deadline, then still-waiting, then past deadline

    def fake_monotonic():
        return ticks.pop(0) if ticks else 1e12

    monkeypatch.setattr(swarm.time, "monotonic", fake_monotonic)
    # First iteration sleeps (line 117); the next is past the deadline -> warn + break.
    with swarm.file_lock(cfg, "A", "lock"):
        pass


# ------------------------------------------------------------------- drain_queue

def test_drain_queue_busy_returns_false(tmp_path):
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: [B]}\n"
                     "- {name: B, command: 'true', can_talk_to: []}\n")
    swarm.enqueue(cfg, "A", "B", "hi", hops=0)
    with mock.patch.object(swarm, "deliver", side_effect=swarm.BusyError("busy")):
        assert swarm.drain_queue(cfg, cfg.get("B")) is False


def test_drain_queue_swarmerror_returns_false(tmp_path):
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: [B]}\n"
                     "- {name: B, command: 'true', can_talk_to: []}\n")
    swarm.enqueue(cfg, "A", "B", "hi", hops=0)
    with mock.patch.object(swarm, "deliver", side_effect=swarm.SwarmError("nope")):
        assert swarm.drain_queue(cfg, cfg.get("B")) is False


# ----------------------------------------------------------------------------- deliver

def _pane_cfg(tmp_path):
    root = tmp_path / "pw"
    path = tmp_path / "pane.yaml"
    path.write_text(
        "swarm: {name: p, root: " + str(root) + ", session_prefix: 'p-'}\n"
        "agent_types:\n"
        "  pane: {command: \"cat\", capture: pane, type: claude, boot_delay_ms: 0,\n"
        "         ready_probe: false, append_agents_that_you_can_talk_to_prompt: false}\n"
        "agents:\n"
        "  - {name: P, type: pane, can_talk_to: [P]}\n"
    )
    return str(path)


def test_deliver_pane_records_echo(tmp_path, monkeypatch):
    cfg = load_swarm(tmp_path, "- {name: P, command: 'true', type: claude, capture: pane}\n")
    with fake_delivery(session_exists=True, paste=True), \
         mock.patch.object(swarm, "record_echo") as recho:
        mid = swarm.deliver(cfg, "user", "P", "hello", enforce_acl=False)
    assert mid
    recho.assert_called_once()


def test_deliver_paste_failure_raises(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true', type: claude}\n")
    with fake_delivery(session_exists=True, paste=False):
        with pytest.raises(swarm.SwarmError):
            swarm.deliver(cfg, "user", "A", "hello", enforce_acl=False)


# ------------------------------------------------------------- note_awaiting_reply

def test_note_awaiting_reply_skips_unreachable_sender(tmp_path):
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: []}\n"
                     "- {name: B, command: 'true', can_talk_to: []}\n")
    # B cannot reach A (A talks to no one) -> no pending reminder written.
    swarm.note_awaiting_reply(cfg, "B", "A", "m-1")
    assert swarm.read_pending(cfg, "A") is None


# ----------------------------------------------------------------- forward_response

def test_forward_swarmerror_warns(tmp_path):
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: [B], forward_responses_to: [B]}\n"
                     "- {name: B, command: 'true', can_talk_to: []}\n")
    agent = cfg.get("A")
    with mock.patch.object(swarm, "deliver", side_effect=swarm.SwarmError("nope")):
        reached = swarm.forward_response(cfg, agent, "hello")
    assert reached == []


def test_forward_hop_limit(tmp_path):
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: [B], forward_responses_to: [B]}\n"
                     "- {name: B, command: 'true', can_talk_to: []}\n")
    cfg.max_forward_hops = 0
    agent = cfg.get("A")
    with mock.patch.object(swarm, "deliver") as deliver:
        assert swarm.forward_response(cfg, agent, "hello") == []
    deliver.assert_not_called()


# ------------------------------------------------------------- pretrust_claude_dir

def _patch_home(monkeypatch, tmp):
    monkeypatch.setattr(swarm.os.path, "expanduser", lambda p: str(tmp / "home"))


def test_pretrust_no_file_returns(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    agent = load_swarm(tmp_path, "- {name: A, command: 'true'}\n").get("A")
    # workdir must exist for the trust target; point at tmp.
    agent.workdir = tmp_path
    swarm.pretrust_claude_dir(agent)  # no ~/.claude.json -> returns silently


def test_pretrust_bad_json_warns(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text("{ not valid")
    _patch_home(monkeypatch, tmp_path)
    agent = load_swarm(tmp_path, "- {name: A, command: 'true'}\n").get("A")
    agent.workdir = tmp_path
    swarm.pretrust_claude_dir(agent)
    assert "could not read" in capsys.readouterr().err


def test_pretrust_already_accepted_returns(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(
        json.dumps({"projects": {str(tmp_path): {"hasTrustDialogAccepted": True}}})
    )
    _patch_home(monkeypatch, tmp_path)
    agent = load_swarm(tmp_path, "- {name: A, command: 'true'}\n").get("A")
    agent.workdir = tmp_path
    # Should return without rewriting (no exception, file unchanged).
    swarm.pretrust_claude_dir(agent)


def test_pretrust_write_oserror_warns(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"projects": {}}))
    _patch_home(monkeypatch, tmp_path)
    agent = load_swarm(tmp_path, "- {name: A, command: 'true'}\n").get("A")
    agent.workdir = tmp_path
    monkeypatch.setattr(swarm.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    swarm.pretrust_claude_dir(agent)
    assert "could not pre-trust" in capsys.readouterr().err


# ----------------------------------------------------------------- install_capture

def test_install_capture_non_hook_returns_empty(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: P, command: 'true', type: claude, capture: pane}\n")
    agent = cfg.get("P")
    assert swarm.install_capture(cfg, agent) == {}


# ------------------------------------------------------- watcher pid file edges

def test_stop_watcher_bad_pid_noop(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    (cfg.run_dir / "A.watcher.pid").write_text("not-an-int")
    swarm.stop_watcher(cfg, cfg.get("A"))  # ValueError in int() is swallowed


def test_watcher_alive_bad_pid_false(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    (cfg.run_dir / "A.watcher.pid").write_text("not-an-int")
    assert swarm.watcher_alive(cfg, cfg.get("A")) is False


# --------------------------------------------------------------------- clear_token

def test_clear_token_backspaces(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    with mock_tmux(pane=swarm.READY_TOKEN) as r:
        swarm.clear_token("t-A")
    assert any("BSpace" in c for c in r.calls)


def test_visible_pane_returns_pane(tmp_path):
    with mock_tmux(pane="hello world"):
        assert swarm.visible_pane("t-A") == "hello world"


def test_clear_token_noop_when_clear(tmp_path):
    # Pane has no readiness token -> clear_token returns immediately (line 1341).
    with mock_tmux(pane="some normal text"):
        swarm.clear_token("t-A")


# ------------------------------------------------------------ config_from_state

def test_config_from_state_bad_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".swarm").mkdir()
    (tmp_path / ".swarm" / "state.json").write_text("{ bad")
    assert swarm.config_from_state() is None


def test_config_from_state_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert swarm.config_from_state() is None


# ------------------------------------------------------- extract_claude_response poll

def test_extract_claude_response_polls(tmp_path, monkeypatch):
    monkeypatch.setattr(swarm, "sleep_ms", _ORIG_SLEEP_MS)
    monkeypatch.setattr(swarm.time, "sleep", lambda s: None)
    monkeypatch.setattr(swarm, "TRANSCRIPT_WAIT_MS", 50)
    tr = tmp_path / "t.jsonl"
    tr.write_text('{"type":"user","message":{"content":"x"}}\n')  # no assistant reply yet
    assert swarm.extract_claude_response({"transcript_path": str(tr)}) == ""


# ----------------------------------------------------------------- codex_session

def test_codex_session_no_rollouts(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true', type: codex}\n")
    # sessions dir exists but holds no rollouts -> returns (None, None) at line 603.
    (cfg.get("A").workdir / ".codex" / "sessions").mkdir(parents=True)
    assert swarm.codex_session(cfg.get("A")) == (None, None)


# -------------------------------------------------------------------- read_echo

def test_read_echo_invalid_file(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    (cfg.run_dir / "A.echo").write_text("not json")
    assert swarm.read_echo(cfg, "A") == set()


# ----------------------------------------------------------------------- cmd_up

def test_cmd_up_emits_warnings(tmp_path, capsys):
    # Two agents sharing a workdir produces a cfg warning.
    root = tmp_path / "ws"
    path = tmp_path / "warn.yaml"
    path.write_text(
        "swarm: {name: w, root: " + str(root) + ", session_prefix: 'w-'}\n"
        "defaults: {type: claude}\n"
        "agents:\n"
        "  - {name: A, command: 'cat', can_talk_to: [B], workdir: " + str(root / "shared") + "}\n"
        "  - {name: B, command: 'cat', can_talk_to: [A], workdir: " + str(root / "shared") + "}\n"
    )
    with mock_tmux(has_session=False):
        swarm.cmd_up(ns(config=str(path), only=None, resume=None, no_prompt=True,
                        attach=False, restart=False))
    assert "share the working directory" in capsys.readouterr().err


def test_cmd_up_resume_no_session_id(tmp_path, capsys):
    root = tmp_path / "ws"
    path = tmp_path / "r.yaml"
    path.write_text(
        "swarm: {name: r, root: " + str(root) + ", session_prefix: 'r-', resume: true}\n"
        "defaults: {type: claude}\n"
        "agents:\n"
        "  - {name: A, command: 'claude', type: claude, can_talk_to: []}\n"
    )
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    # sessions.yaml exists but has no session_id for A
    swarm.write_sessions(cfg, {"A": {"type": "claude", "updated_at": "x"}})
    with mock_tmux(has_session=False), \
         mock.patch.object(swarm, "install_capture", return_value={}):
        swarm.cmd_up(ns(config=str(path), only=None, resume=None, no_prompt=True,
                        attach=False, restart=False))
    assert "no recorded conversation" in capsys.readouterr().err


def test_cmd_up_resume_no_recipe(tmp_path, capsys):
    root = tmp_path / "ws"
    path = tmp_path / "r.yaml"
    path.write_text(
        "swarm: {name: r, root: " + str(root) + ", session_prefix: 'r-', resume: true}\n"
        "defaults: {type: claude}\n"
        "agents:\n"
        "  - {name: A, command: 'cat', type: gemini, can_talk_to: []}\n"
    )
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    swarm.write_sessions(cfg, {"A": {"session_id": "sess-x", "type": "gemini", "updated_at": "x"}})
    with mock_tmux(has_session=False), \
         mock.patch.object(swarm, "install_capture", return_value={}):
        swarm.cmd_up(ns(config=str(path), only=None, resume=None, no_prompt=True,
                        attach=False, restart=False))
    assert "no resume recipe" in capsys.readouterr().err


def test_cmd_up_prompt_resumed_skips(tmp_path, capsys):
    # Resumed agent must not be re-prompted.
    root = tmp_path / "ws"
    path = tmp_path / "r.yaml"
    path.write_text(
        "swarm: {name: r, root: " + str(root) + ", session_prefix: 'r-', resume: true}\n"
        "defaults: {type: claude}\n"
        "agents:\n"
        "  - {name: A, command: 'claude', type: claude, can_talk_to: [], first_prompt: 'hi'}\n"
    )
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    swarm.write_sessions(cfg, {"A": {"session_id": "sess-x", "type": "claude", "updated_at": "x"}})
    with mock_tmux(has_session=False), \
         mock.patch.object(swarm, "install_capture", return_value={}), \
         mock.patch.object(swarm, "wait_until_ready", return_value=True), \
         mock.patch.object(swarm, "paste_into", return_value=True):
        swarm.cmd_up(ns(config=str(path), only=None, resume=None, no_prompt=False,
                        attach=False, restart=False))
    err = capsys.readouterr().err
    assert "resumed, not re-sending" in err


def test_cmd_up_prompt_ready_probe_failure(tmp_path, capsys):
    root = tmp_path / "ws"
    path = tmp_path / "r.yaml"
    path.write_text(
        "swarm: {name: r, root: " + str(root) + ", session_prefix: 'r-', ready_timeout_ms: 0}\n"
        "defaults: {type: claude}\n"
        "agents:\n"
        "  - {name: A, command: 'claude', type: claude, can_talk_to: [], ready_probe: true,\n"
        "     first_prompt: 'hi'}\n"
    )
    with mock_tmux(has_session=False), \
         mock.patch.object(swarm, "install_capture", return_value={}), \
         mock.patch.object(swarm, "wait_until_ready", return_value=False), \
         mock.patch.object(swarm, "paste_into", return_value=True):
        swarm.cmd_up(ns(config=str(path), only=None, resume=None, no_prompt=False,
                        attach=False, restart=False))
    assert "input box never responded" in capsys.readouterr().err


def test_cmd_up_prompt_paste_failure(tmp_path, capsys):
    root = tmp_path / "ws"
    path = tmp_path / "r.yaml"
    path.write_text(
        "swarm: {name: r, root: " + str(root) + ", session_prefix: 'r-'}\n"
        "defaults: {type: claude}\n"
        "agents:\n"
        "  - {name: A, command: 'claude', type: claude, can_talk_to: [], first_prompt: 'hi'}\n"
    )
    with mock_tmux(has_session=False), \
         mock.patch.object(swarm, "install_capture", return_value={}), \
         mock.patch.object(swarm, "wait_until_ready", return_value=True), \
         mock.patch.object(swarm, "paste_into", return_value=False):
        swarm.cmd_up(ns(config=str(path), only=None, resume=None, no_prompt=False,
                        attach=False, restart=False))
    assert "first prompt may not have been delivered" in capsys.readouterr().err


def test_cmd_up_tmux_missing_dies(tmp_path):
    path = tmp_path / "x.yaml"
    path.write_text("swarm: {name: x, root: " + str(tmp_path / "ws") + "}\n"
                    "defaults: {type: claude}\nagents:\n  - {name: A, command: 'cat'}\n")
    with mock.patch.object(swarm.shutil, "which", return_value=None):
        with pytest.raises(SystemExit):
            swarm.cmd_up(ns(config=str(path), only=None, resume=None, no_prompt=True,
                            attach=False, restart=False))


# --------------------------------------------------------------------- cmd_down

def test_cmd_down_not_running(tmp_path, capsys):
    path = str(agent_yaml(
        tmp_path,
        "  - {name: A, command: \"cat\", can_talk_to: [B]}\n"
        "  - {name: B, command: \"cat\", can_talk_to: [A]}\n",
        name="t", root=tmp_path / "ws", session_prefix="t-",
    ))
    with mock_tmux(has_session=False):
        rc = swarm.cmd_down(ns(config=path, only=None))
    assert rc == 0
    assert "not running" in capsys.readouterr().err


# ------------------------------------------------------------------- cmd_status

def test_cmd_status_not_running(tmp_path, capsys):
    path = str(agent_yaml(
        tmp_path,
        "  - {name: A, command: \"cat\", can_talk_to: [B]}\n"
        "  - {name: B, command: \"cat\", can_talk_to: [A]}\n",
        name="t", root=tmp_path / "ws", session_prefix="t-",
    ))
    with mock_tmux(has_session=False),          mock.patch.object(swarm, "busy_info", return_value=None),          mock.patch.object(swarm, "watcher_alive", return_value=False):
        rc = swarm.cmd_status(ns(config=path))
    assert rc == 0
    assert "down" in capsys.readouterr().out


def test_cmd_status_untracked_and_busy(tmp_path, capsys):
    path = str(agent_yaml(
        tmp_path,
        "  - {name: A, command: \"cat\", can_talk_to: [B]}\n"
        "  - {name: B, command: \"cat\", can_talk_to: [A]}\n",
        name="t", root=tmp_path / "ws", session_prefix="t-",
    ))
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: [B], busy_check: false}\n"
                     "- {name: B, command: 'true', can_talk_to: [A]}\n")
    with mock_tmux(has_session=True), \
         mock.patch.object(swarm, "busy_info", return_value={"age_s": 4, "by": "A"}), \
         mock.patch.object(swarm, "watcher_alive", return_value=False):
        rc = swarm.cmd_status(ns(config=path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "untracked" in out and "busy 4s" in out


# ----------------------------------------------------------------------- cmd_send

def test_send_message_queue_wait_until_picked_up_fails(tmp_path):
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: [B]}\n"
                     "- {name: B, command: 'true', can_talk_to: []}\n")
    args = ns(to="B", force=False, ignore_busy=False, queue=True, wait=True, wait_timeout=600)
    with mock.patch.object(swarm, "deliver", side_effect=swarm.BusyError("busy")), \
         mock.patch.object(swarm, "enqueue", return_value=("m-1", 1)), \
         mock.patch.object(swarm, "wait_for_dequeue", return_value=False), \
         mock.patch.object(swarm.time, "sleep"):
        assert swarm.send_message(cfg, "A", args, "hi") == 0  # "still queued" path


def test_send_message_wait_timeout_dies(tmp_path):
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: [B]}\n"
                     "- {name: B, command: 'true', can_talk_to: []}\n")
    args = ns(to="B", force=False, ignore_busy=False, queue=False, wait=True, wait_timeout=600)
    deadline = {"t": 0.0}

    def fake_monotonic():
        deadline["t"] += 1e9  # leap past the deadline immediately
        return deadline["t"]

    with mock.patch.object(swarm, "deliver", side_effect=swarm.BusyError("busy")), \
         mock.patch.object(swarm.time, "monotonic", fake_monotonic), \
         mock.patch.object(swarm.time, "sleep"):
        with pytest.raises(SystemExit):
            swarm.send_message(cfg, "A", args, "hi")


# ------------------------------------------------------------------ cmd_broadcast

def test_cmd_broadcast_no_targets_dies(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true', can_talk_to: []}\n")
    # A is in cfg.names() but talks to no one -> die
    parser = swarm.build_parser()
    args = parser.parse_args(["broadcast", "--from", "A", "-c", str(cfg.path), "hi"])
    with mock.patch.object(swarm, "deliver"):
        with pytest.raises(SystemExit):
            swarm.cmd_broadcast(args)


def test_cmd_broadcast_busy_no_queue_failure(tmp_path, capsys):
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: [B]}\n"
                     "- {name: B, command: 'true', can_talk_to: []}\n")
    parser = swarm.build_parser()
    args = parser.parse_args(["broadcast", "-c", str(cfg.path), "hi"])
    with mock.patch.object(swarm, "deliver", side_effect=swarm.BusyError("busy")):
        rc = swarm.cmd_broadcast(args)
    assert rc == 1


def test_cmd_broadcast_busy_queues(tmp_path):
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: [B]}\n"
                     "- {name: B, command: 'true', can_talk_to: []}\n")
    parser = swarm.build_parser()
    args = parser.parse_args(["broadcast", "--queue", "-c", str(cfg.path), "hi"])
    with mock.patch.object(swarm, "deliver", side_effect=swarm.BusyError("busy")):
        assert swarm.cmd_broadcast(args) == 0


def test_cmd_broadcast_swarmerror_failure(tmp_path):
    cfg = load_swarm(tmp_path,
                     "- {name: A, command: 'true', can_talk_to: [B]}\n"
                     "- {name: B, command: 'true', can_talk_to: []}\n")
    parser = swarm.build_parser()
    args = parser.parse_args(["broadcast", "-c", str(cfg.path), "hi"])
    with mock.patch.object(swarm, "deliver", side_effect=swarm.SwarmError("nope")):
        assert swarm.cmd_broadcast(args) == 1


# --------------------------------------------------------------------- cmd_logs

def test_cmd_logs_unknown_agent_dies(tmp_path):
    path = str(agent_yaml(
        tmp_path,
        "  - {name: A, command: \"cat\", can_talk_to: [B]}\n"
        "  - {name: B, command: \"cat\", can_talk_to: [A]}\n",
        name="t", root=tmp_path / "ws", session_prefix="t-",
    ))
    with mock_tmux(has_session=True):
        with pytest.raises(config.ConfigError):
            swarm.cmd_logs(ns(config=path, agent="ghost", tail=20, follow=False))


def test_cmd_logs_skips_bad_json(tmp_path, capsys):
    path = str(agent_yaml(
        tmp_path,
        "  - {name: A, command: \"cat\", can_talk_to: [B]}\n"
        "  - {name: B, command: \"cat\", can_talk_to: [A]}\n",
        name="t", root=tmp_path / "ws", session_prefix="t-",
    ))
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n- {name: B, command: 'true'}\n")
    log = cfg.log_dir / "A.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('this is not json\n{"ts":"t","agent":"A","kind":"response","text":"ok"}\n')
    with mock_tmux(has_session=True):
        rc = swarm.cmd_logs(ns(config=path, agent="A", tail=20, follow=False))
    assert rc == 0
    assert "ok" in capsys.readouterr().out


# ------------------------------------------------------------------ cmd_validate

def test_cmd_validate_workdir_states(tmp_path, capsys):
    root = tmp_path / "ws"
    path = tmp_path / "v.yaml"
    (root / "exists").mkdir(parents=True)
    # B's workdir is missing but create_workdir defaults true -> "will be created".
    path.write_text(
        "swarm: {name: v, root: " + str(root) + ", session_prefix: 'v-'}\n"
        "defaults: {type: claude}\n"
        "agents:\n"
        "  - {name: A, command: 'cat', can_talk_to: [B], workdir: " + str(root / "exists") + "}\n"
        "  - {name: B, command: 'cat', can_talk_to: []}\n"
    )
    with mock_tmux(has_session=True):
        rc = swarm.cmd_validate(ns(config=str(path), show_prompts=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "exists" in out and "will be created" in out


def test_cmd_validate_emits_warnings(tmp_path, capsys):
    root = tmp_path / "ws"
    path = tmp_path / "v.yaml"
    path.write_text(
        "swarm: {name: v, root: " + str(root) + ", session_prefix: 'v-'}\n"
        "defaults: {type: claude}\n"
        "agents:\n"
        "  - {name: A, command: 'cat', can_talk_to: [B], workdir: " + str(root / "shared") + "}\n"
        "  - {name: B, command: 'cat', can_talk_to: [A], workdir: " + str(root / "shared") + "}\n"
    )
    with mock_tmux(has_session=True):
        rc = swarm.cmd_validate(ns(config=str(path), show_prompts=False))
    assert rc == 0
    assert "share the working directory" in capsys.readouterr().err


def test_cmd_validate_will_be_created(tmp_path, capsys):
    root = tmp_path / "ws"
    path = tmp_path / "v.yaml"
    # B's workdir does not exist but create_workdir defaults true -> will be created
    path.write_text(
        "swarm: {name: v, root: " + str(root) + ", session_prefix: 'v-'}\n"
        "defaults: {type: claude}\n"
        "agents:\n"
        "  - {name: B, command: 'cat', can_talk_to: [], workdir: " + str(root / "newdir") + "}\n"
    )
    with mock_tmux(has_session=True):
        rc = swarm.cmd_validate(ns(config=str(path), show_prompts=False))
    assert rc == 0
    assert "will be created" in capsys.readouterr().out


def test_cmd_validate_prints_forward_responses(tmp_path, capsys):
    root = tmp_path / "ws"
    path = tmp_path / "v.yaml"
    path.write_text(
        "swarm: {name: v, root: " + str(root) + ", session_prefix: 'v-'}\n"
        "defaults: {type: claude}\n"
        "agents:\n"
        "  - {name: A, command: 'cat', can_talk_to: [B], forward_responses_to: [B]}\n"
        "  - {name: B, command: 'cat', can_talk_to: [A]}\n"
    )
    with mock_tmux(has_session=True):
        rc = swarm.cmd_validate(ns(config=str(path), show_prompts=False))
    assert rc == 0
    assert "auto-forwards responses to: B" in capsys.readouterr().out


# ------------------------------------------------------------------ cmd_hook codex

def test_cmd_hook_codex_bad_payload(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true', type: codex}\n")
    # invalid JSON -> payload becomes {} -> not agent-turn-complete -> returns 0
    args = ns(config=str(cfg.path), agent="A", type="codex", payload="{bad")
    assert swarm.cmd_hook(args) == 0


# ------------------------------------------------------------- default_config

def test_default_config_candidate_found(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agents.yaml").write_text("agents:\n  - {name: A, command: 'cat'}\n")
    assert swarm.default_config() == str(tmp_path / "agents.yaml")


# ----------------------------------------------------------------- wait_for_dequeue

def test_wait_for_dequeue_timeout_sleeps(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true', can_talk_to: []}\n")
    item = swarm.enqueue(cfg, "user", "A", "x", hops=0)[0]
    with mock.patch.object(swarm.time, "sleep"):  # line runs (call), no real wait
        assert swarm.wait_for_dequeue(cfg, "A", item, 0.02) is False


# ----------------------------------------------------------------- helpers


