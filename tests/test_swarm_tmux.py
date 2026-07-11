"""Tests for swarm.py tmux/pane interaction and the pane watcher."""

import subprocess
from unittest import mock

import pytest

import swarm
from tests.support import load_swarm, mock_tmux


# ---------------------------------------------------------------------- tmux()

def test_tmux_not_installed():
    with mock.patch.object(swarm.shutil, "which", return_value=None):
        with pytest.raises(swarm.SwarmError):
            swarm.tmux("has-session", "-t", "x")


def test_tmux_runs():
    with mock_tmux() as r:
        rc = swarm.tmux("new-session", "-s", "x")
    assert rc.returncode == 0


# ---------------------------------------------------------------- configure

def test_configure_tmux_nothing_to_do(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    cfg.tmux_history_limit = 0
    cfg.tmux_mouse = False
    assert swarm.configure_tmux(cfg) is None


def test_configure_tmux_sets_options(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    cfg.tmux_history_limit = 1000
    cfg.tmux_mouse = True
    with mock_tmux() as r:
        holder = swarm.configure_tmux(cfg)
    assert holder == "t-swarm_setup"
    assert any("new-session" in c for c in r.calls)
    assert any("history-limit" in c for c in r.calls)
    assert any("mouse" in c for c in r.calls)


# -------------------------------------------------------------- session/pane

def test_session_exists(tmp_path):
    with mock_tmux(has_session=True):
        assert swarm.session_exists("t-A") is True
    with mock_tmux(has_session=False):
        assert swarm.session_exists("t-A") is False


def test_pane_text_and_visible_pane(tmp_path):
    with mock_tmux(pane="hello"):
        assert swarm.pane_text("t-A") == "hello"
        assert swarm.visible_pane("t-A") == "hello"


def test_visible_pane_error_returns_empty(tmp_path):
    with mock.patch.object(
        swarm, "tmux", side_effect=subprocess.CalledProcessError(1, ["tmux"])
    ):
        assert swarm.visible_pane("t-A") == ""
        assert swarm.pane_text("t-A", 50) == ""


# ------------------------------------------------------------- small helpers

def test_needle_for_short():
    assert swarm.needle_for("a") == "a"
    assert swarm.needle_for("abc\n  def") == "abcdef"[-swarm.NEEDLE_LEN:]


def test_paste_score_basic():
    with mock.patch.object(swarm, "pane_text", return_value="hello world"):
        assert swarm.paste_score("sess", "hello") == 1
    with mock.patch.object(swarm, "pane_text", return_value="Pasted text here"):
        # The paste-chip regex matches "pasted" (case-insensitive) after
        # whitespace is stripped, independent of the needle we searched for.
        assert swarm.paste_score("sess", "zzz") == 1


def test_send_buffer_writes_and_loads(tmp_path):
    with mock_tmux() as r:
        swarm.send_buffer("sess", "my body")
    assert any("load-buffer" in c for c in r.calls)
    assert any("paste-buffer" in c for c in r.calls)


# ---------------------------------------------------------------- paste_into

def test_paste_into_session_missing(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    with mock_tmux(has_session=False):
        with pytest.raises(swarm.SwarmError):
            swarm.paste_into(cfg, "t-A", "body")


def test_paste_into_success(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")

    state = {"n": 0}

    def fake_pane(session, scrollback=0):
        state["n"] += 1
        return "body" if state["n"] > 1 else ""

    with mock.patch.object(swarm, "pane_text", fake_pane):
        with mock_tmux():
            assert swarm.paste_into(cfg, "t-A", "body") is True


def test_paste_into_failure(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    with mock.patch.object(swarm, "pane_text", return_value=""):
        with mock_tmux():
            assert swarm.paste_into(cfg, "t-A", "body") is False


def test_paste_into_empty_body():
    with mock_tmux():
        assert swarm.paste_into(None, "t-A", "") is False


# -------------------------------------------------------------- _paste_locked

def test_paste_locked_failure(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    with mock.patch.object(swarm, "pane_text", return_value=""):
        with mock_tmux():
            assert swarm._paste_locked(cfg, "t-A", "body", True) is False


def test_paste_locked_success_enters(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    state = {"n": 0}

    def fake_pane(session, scrollback=0):
        state["n"] += 1
        return "body" if state["n"] > 1 else ""

    with mock.patch.object(swarm, "pane_text", fake_pane):
        with mock_tmux() as r:
            assert swarm._paste_locked(cfg, "t-A", "body", True) is True
            assert any("Enter" in c for c in r.calls)


def test_paste_locked_no_enter(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    state = {"n": 0}

    def fake_pane(session, scrollback=0):
        state["n"] += 1
        return "body" if state["n"] > 1 else ""

    with mock.patch.object(swarm, "pane_text", fake_pane):
        with mock_tmux() as r:
            assert swarm._paste_locked(cfg, "t-A", "body", False) is True
            assert not any("Enter" in c for c in r.calls)


# ---------------------------------------------------------------- clear_token

def test_clear_token_backspaces(tmp_path):
    with mock_tmux(pane=swarm.READY_TOKEN) as r:
        swarm.clear_token("t-A")
    assert any("BSpace" in c for c in r.calls)


# -------------------------------------------------------------- wait_until_ready

def test_wait_until_ready_token_echoed(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    agent = cfg.get("A")
    with mock_tmux(pane=swarm.READY_TOKEN):
        assert swarm.wait_until_ready(cfg, agent) is True


def test_wait_until_ready_session_gone(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    agent = cfg.get("A")
    with mock_tmux(has_session=False):
        assert swarm.wait_until_ready(cfg, agent) is False


def test_wait_until_ready_timeout(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    agent = cfg.get("A")
    cfg.ready_timeout_ms = 0
    with mock_tmux(has_session=True, pane=""):
        assert swarm.wait_until_ready(cfg, agent) is False


# ---------------------------------------------------------------- capture_pane

def test_capture_pane(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    with mock_tmux(pane="pane text") as r:
        assert swarm.capture_pane(cfg, cfg.get("A")) == "pane text"
    with mock.patch.object(
        swarm, "tmux", side_effect=subprocess.CalledProcessError(1, ["tmux"])
    ):
        assert swarm.capture_pane(cfg, cfg.get("A")) == ""


# ---------------------------------------------------------------- watchers

def test_start_stop_watcher(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    agent = cfg.get("A")
    fake_proc = mock.Mock()
    fake_proc.pid = 9999
    with mock.patch.object(swarm.subprocess, "Popen", return_value=fake_proc), mock.patch.object(
        swarm.os, "kill"
    ) as kill:
        swarm.start_watcher(cfg, agent)
        assert (cfg.run_dir / "A.watcher.pid").read_text() == "9999"
        assert swarm.watcher_alive(cfg, agent) is True
        swarm.stop_watcher(cfg, agent)
        kill.assert_called()
    assert not (cfg.run_dir / "A.watcher.pid").exists()


def test_watcher_alive_no_pid_file(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    agent = cfg.get("A")
    assert swarm.watcher_alive(cfg, agent) is False


def test_stop_watcher_no_pid_file(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    agent = cfg.get("A")
    # Should be a no-op (no pid file, no exception).
    swarm.stop_watcher(cfg, agent)


def test_run_watcher_emits_on_change(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    agent = cfg.get("A")
    cfg.pane_idle_ms = 0
    calls = {"cap": 0, "sess": 0}

    def cap(c, a):
        calls["cap"] += 1
        if calls["cap"] == 1:
            return "line1"
        return "line1\nline2 new"

    def sess(a):
        calls["sess"] += 1
        return calls["sess"] < 4

    with mock.patch.object(swarm, "capture_pane", cap), mock.patch.object(
        swarm, "session_exists", sess
    ), mock.patch.object(swarm, "read_echo", lambda c, a: set()), mock.patch.object(
        swarm, "on_turn_finished"
    ) as otf, mock.patch.object(swarm, "log_event"):
        swarm.run_watcher(cfg, agent)
    assert otf.called


def test_run_watcher_exits_on_session_gone(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    agent = cfg.get("A")
    with mock.patch.object(swarm, "capture_pane", lambda c, a: "x"), mock.patch.object(
        swarm, "session_exists", lambda a: False
    ), mock.patch.object(swarm, "on_turn_finished") as otf:
        swarm.run_watcher(cfg, agent)
    assert not otf.called


def test_run_watcher_skips_tiny_text(tmp_path):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    agent = cfg.get("A")
    cfg.pane_idle_ms = 0
    calls = {"cap": 0, "sess": 0}

    def cap(c, a):
        calls["cap"] += 1
        if calls["cap"] == 1:
            return "line1"
        return "line1\nx"  # "x" is < 2 chars -> skipped

    def sess(a):
        calls["sess"] += 1
        return calls["sess"] < 4

    with mock.patch.object(swarm, "capture_pane", cap), mock.patch.object(
        swarm, "session_exists", sess
    ), mock.patch.object(swarm, "read_echo", lambda c, a: set()), mock.patch.object(
        swarm, "on_turn_finished"
    ) as otf, mock.patch.object(swarm, "log_event"):
        swarm.run_watcher(cfg, agent)
    assert not otf.called


def test_run_watcher_emits_partial_change(tmp_path):
    # When the pane changes to text that shares a prefix with what was already
    # emitted, the common-prefix loop must break early (line 1536). "line1\nlineA"
    # and "line1\nlineB" share "line1" but differ at the tail.
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    agent = cfg.get("A")
    cfg.pane_idle_ms = 0
    calls = {"cap": 0, "sess": 0}

    def cap(c, a):
        calls["cap"] += 1
        if calls["cap"] == 1:
            return "line1"
        if calls["cap"] == 2:
            return "line1\nlineA"  # change -> dirty
        if calls["cap"] == 3:
            return "line1\nlineA"  # idle -> emit (tail empty, skipped)
        return "line1\nlineB"      # change -> shares prefix, tail differs -> break

    def sess(a):
        calls["sess"] += 1
        return calls["sess"] < 5

    with mock.patch.object(swarm, "capture_pane", cap), mock.patch.object(
        swarm, "session_exists", sess
    ), mock.patch.object(swarm, "read_echo", lambda c, a: set()), mock.patch.object(
        swarm, "on_turn_finished"
    ) as otf, mock.patch.object(swarm, "log_event"):
        swarm.run_watcher(cfg, agent)
    assert otf.called
    # The emitted text should be the differing tail, not the shared prefix.
    text = otf.call_args[0][2]
    assert "lineB" in text and "line1" not in text
