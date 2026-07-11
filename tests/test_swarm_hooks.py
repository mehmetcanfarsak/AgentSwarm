"""Tests for swarm.py hook installation and the hook CLI entry point."""

import io
import json
import os
from unittest import mock

import pytest

import swarm
from config import Agent
from tests.conftest import load_config
from tests.support import load_swarm


def claude_agent(workdir, **kw):
    base = dict(name="A", type="claude", command="true", workdir=workdir,
                session="t-A", capture="hook", boot_delay_ms=0, first_prompt="")
    base.update(kw)
    return Agent(**base)


# ----------------------------------------------------------- claude pretrust

def test_pretrust_no_claude_json(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "home"))
    agent = claude_agent(tmp_path / "agentA")
    # No ~/.claude.json -> should return without touching anything.
    swarm.pretrust_claude_dir(agent)
    assert not (tmp_path / "home" / ".claude.json").exists()


def test_pretrust_updates_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "home"))
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"projects": {}}))
    agent = claude_agent(tmp_path / "agentA")
    agent.workdir.mkdir(parents=True)
    swarm.pretrust_claude_dir(agent)
    data = json.loads((home / ".claude.json").read_text())
    entry = data["projects"][str(agent.workdir)]
    assert entry["hasTrustDialogAccepted"] is True
    assert entry["projectOnboardingSeenCount"] == 1


def test_pretrust_already_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "home"))
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"projects": {str(tmp_path / "agentA"): {"hasTrustDialogAccepted": True}}}))
    agent = claude_agent(tmp_path / "agentA")
    before = (home / ".claude.json").read_text()
    swarm.pretrust_claude_dir(agent)
    assert (home / ".claude.json").read_text() == before


# ----------------------------------------------------------- claude hook

def test_install_claude_hook(tmp_path):
    agent = claude_agent(tmp_path / "agentA")
    agent.workdir.mkdir(parents=True)
    # Pre-seed a settings.json with the user's own Stop hook to ensure it is kept.
    settings = agent.workdir / ".claude"
    settings.mkdir(parents=True)
    (settings / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"command": "user-hook"}]}}))
    with mock.patch.object(swarm, "pretrust_claude_dir"):
        swarm.install_claude_hook(agent)
    data = json.loads((settings / "settings.json").read_text())
    stops = data["hooks"]["Stop"]
    assert any("user-hook" in json.dumps(h) for h in stops)
    assert any(swarm.HOOKS_DIR.name in json.dumps(h) for h in stops)


def test_install_claude_hook_corrupt_json(tmp_path):
    agent = claude_agent(tmp_path / "agentA")
    agent.workdir.mkdir(parents=True)
    settings = agent.workdir / ".claude"
    settings.mkdir(parents=True)
    (settings / "settings.json").write_text("{not valid")
    with mock.patch.object(swarm, "pretrust_claude_dir"), mock.patch.object(swarm, "warn") as w:
        swarm.install_claude_hook(agent)
    assert w.called
    assert (settings / "settings.json").exists()


# ------------------------------------------------------------- codex hook

def test_install_codex_hook_basic(tmp_path):
    agent = claude_agent(tmp_path / "agentA", type="codex")
    agent.workdir.mkdir(parents=True)
    with mock.patch.object(swarm, "valid_toml", return_value=True):
        path = swarm.install_codex_hook(agent)
    toml = (path / "config.toml").read_text()
    assert "notify" in toml and "trust_level" in toml


def test_install_codex_hook_carries_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "home"))
    user_home = tmp_path / "home" / ".codex"
    user_home.mkdir(parents=True)
    (user_home / "auth.json").write_text('{"token": 1}')
    agent = claude_agent(tmp_path / "agentA", type="codex")
    agent.workdir.mkdir(parents=True)
    with mock.patch.object(swarm, "valid_toml", return_value=True):
        path = swarm.install_codex_hook(agent)
    assert (path / "auth.json").exists()


def test_install_codex_hook_merges_user_config(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "home"))
    user_home = tmp_path / "home" / ".codex"
    user_home.mkdir(parents=True)
    (user_home / "config.toml").write_text('other = 1\nnotify = ["old"]\n')
    agent = claude_agent(tmp_path / "agentA", type="codex")
    agent.workdir.mkdir(parents=True)
    with mock.patch.object(swarm, "valid_toml", return_value=True):
        path = swarm.install_codex_hook(agent)
    toml = (path / "config.toml").read_text()
    assert 'other = 1' in toml
    assert 'notify = ["old"]' not in toml  # user's notify is stripped


def test_install_codex_hook_symlink_falls_back_to_copy(tmp_path, monkeypatch):
    # When the user's auth.json cannot be symlinked (e.g. a cross-device link on
    # some filesystems), install_codex_hook must fall back to copying it.
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "home"))
    user_home = tmp_path / "home" / ".codex"
    user_home.mkdir(parents=True)
    (user_home / "auth.json").write_text('{"token": 1}')
    agent = claude_agent(tmp_path / "agentA", type="codex")
    agent.workdir.mkdir(parents=True)
    with mock.patch.object(swarm.os, "symlink", side_effect=OSError("cross-device")), \
         mock.patch.object(swarm, "valid_toml", return_value=True):
        path = swarm.install_codex_hook(agent)
    # Fell back to copy2: the file is present and is a regular file, not a link.
    dst = path / "auth.json"
    assert dst.exists()
    assert not dst.is_symlink()


def test_install_codex_hook_trust_already_present(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "home"))
    agent = claude_agent(tmp_path / "agentA", type="codex")
    agent.workdir.mkdir(parents=True)
    user_home = tmp_path / "home" / ".codex"
    user_home.mkdir(parents=True)
    (user_home / "config.toml").write_text(
        f'[projects.{json.dumps(str(agent.workdir))}]\ntrust_level = "trusted"\n'
    )
    with mock.patch.object(swarm, "valid_toml", return_value=True):
        path = swarm.install_codex_hook(agent)
    toml = (path / "config.toml").read_text()
    # The user's trust header is kept; no second trust_level is appended.
    assert toml.count("trust_level") == 1


def test_install_codex_hook_invalid_toml_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "home"))
    user_home = tmp_path / "home" / ".codex"
    user_home.mkdir(parents=True)
    (user_home / "config.toml").write_text("this = is = not = valid\n")
    agent = claude_agent(tmp_path / "agentA", type="codex")
    agent.workdir.mkdir(parents=True)
    with mock.patch.object(swarm, "valid_toml", return_value=False), mock.patch.object(swarm, "warn") as w:
        path = swarm.install_codex_hook(agent)
    assert w.called
    toml = (path / "config.toml").read_text()
    assert "trust_level" in toml


# ----------------------------------------------------------- install_capture

def test_install_capture_claude(tmp_path):
    agent = claude_agent(tmp_path / "agentA")
    agent.workdir.mkdir(parents=True)
    with mock.patch.object(swarm, "install_claude_hook") as h:
        env = swarm.install_capture(_cfg(tmp_path), agent)
    assert env == {}
    assert h.called


def _cfg(tmp_path):
    return load_swarm(tmp_path, "- {name: A, command: 'true'}\n")


def test_install_capture_codex(tmp_path):
    agent = claude_agent(tmp_path / "agentA", type="codex")
    agent.workdir.mkdir(parents=True)
    env = swarm.install_capture(_cfg(tmp_path), agent)
    assert "CODEX_HOME" in env


def test_install_capture_unknown_type_falls_back(tmp_path):
    agent = Agent(name="A", type="weird", command="true", workdir=tmp_path / "agentA",
                  session="t-A", capture="hook", boot_delay_ms=0, first_prompt="")
    agent.workdir.mkdir(parents=True)
    with mock.patch.object(swarm, "warn") as w:
        env = swarm.install_capture(_cfg(tmp_path), agent)
    assert env == {}
    assert agent.capture == "pane"
    assert w.called


# ------------------------------------------------------------- cmd_hook CLI

def _hook_args(type_, payload=None, config=None, agent=None):
    ns = mock.Mock()
    ns.type = type_
    ns.payload = payload
    ns.config = config
    ns.agent = agent
    return ns


def test_cmd_hook_claude(tmp_path, monkeypatch):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    monkeypatch.setenv("SWARM_CONFIG", str(cfg.path))
    monkeypatch.setenv("SWARM_AGENT", "A")
    payload = json.dumps({"session_id": "s1", "transcript_path": None})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    with mock.patch.object(swarm, "on_turn_finished") as otf, mock.patch.object(
        swarm, "record_session"
    ) as rs:
        rc = swarm.cmd_hook(_hook_args("claude"))
    assert rc == 0
    rs.assert_called_once()
    otf.assert_called_once()


def test_cmd_hook_claude_stop_hook_active(tmp_path, monkeypatch):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    monkeypatch.setenv("SWARM_CONFIG", str(cfg.path))
    monkeypatch.setenv("SWARM_AGENT", "A")
    payload = json.dumps({"stop_hook_active": True})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    with mock.patch.object(swarm, "on_turn_finished") as otf, mock.patch.object(
        swarm, "record_session"
    ) as rs:
        rc = swarm.cmd_hook(_hook_args("claude"))
    assert rc == 0
    otf.assert_not_called()
    rs.assert_not_called()


def test_cmd_hook_codex(tmp_path, monkeypatch):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    monkeypatch.setenv("SWARM_CONFIG", str(cfg.path))
    monkeypatch.setenv("SWARM_AGENT", "A")
    payload = json.dumps({"type": "agent-turn-complete", "last-assistant-message": "hi"})
    with mock.patch.object(swarm, "on_turn_finished") as otf, mock.patch.object(
        swarm, "record_session"
    ):
        rc = swarm.cmd_hook(_hook_args("codex", payload=payload))
    assert rc == 0
    otf.assert_called_once_with(cfg, cfg.get("A"), "hi")


def test_cmd_hook_codex_ignores_other_types(tmp_path, monkeypatch):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    monkeypatch.setenv("SWARM_CONFIG", str(cfg.path))
    monkeypatch.setenv("SWARM_AGENT", "A")
    payload = json.dumps({"type": "something-else"})
    with mock.patch.object(swarm, "on_turn_finished") as otf:
        rc = swarm.cmd_hook(_hook_args("codex", payload=payload))
    assert rc == 0
    otf.assert_not_called()


def test_cmd_hook_generic(tmp_path, monkeypatch):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    monkeypatch.setenv("SWARM_CONFIG", str(cfg.path))
    monkeypatch.setenv("SWARM_AGENT", "A")
    monkeypatch.setattr("sys.stdin", io.StringIO("plain text reply\n"))
    with mock.patch.object(swarm, "on_turn_finished") as otf:
        rc = swarm.cmd_hook(_hook_args("generic"))
    assert rc == 0
    otf.assert_called_once_with(cfg, cfg.get("A"), "plain text reply")


def test_cmd_hook_claude_bad_payload(tmp_path, monkeypatch):
    cfg = load_swarm(tmp_path, "- {name: A, command: 'true'}\n")
    monkeypatch.setenv("SWARM_CONFIG", str(cfg.path))
    monkeypatch.setenv("SWARM_AGENT", "A")
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    # Bad payload -> empty payload dict, no crash, returns 0.
    with mock.patch.object(swarm, "on_turn_finished"):
        assert swarm.cmd_hook(_hook_args("claude")) == 0
