"""Helpers for exercising swarm.py's tmux-backed paths without a real tmux.

``mock_tmux`` patches ``swarm.subprocess.run`` (which ``tmux()`` calls) and
``shutil.which`` so every tmux command "succeeds". ``deliver`` and friends are
usually exercised through ``fake_delivery`` instead, which skips the paste layer
entirely so the routing/ACL logic is what is under test.
"""

import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import config as cfgmod
import swarm


class TmuxRunner:
    """Stand-in for ``subprocess.run`` that answers the calls ``tmux()`` makes."""

    def __init__(self, has_session=True, pane="", returncode=0):
        self.has_session = has_session
        self.pane = pane
        self.returncode = returncode
        self.calls = []

    def __call__(self, args, *a, **kw):
        cmd = list(args)
        self.calls.append(cmd)
        if "has-session" in cmd:
            if self.has_session:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            raise subprocess.CalledProcessError(1, cmd, "")
        if cmd and cmd[1] == "capture-pane":
            return subprocess.CompletedProcess(cmd, self.returncode, self.pane, "")
        return subprocess.CompletedProcess(cmd, self.returncode, "", "")


@contextmanager
def mock_tmux(has_session=True, pane="", returncode=0):
    runner = TmuxRunner(has_session=has_session, pane=pane, returncode=returncode)
    with mock.patch.object(swarm.subprocess, "run", runner), mock.patch.object(
        swarm.shutil, "which", lambda name: "/usr/bin/" + name
    ):
        yield runner


@contextmanager
def fake_delivery(session_exists=True, busy=False, paste=True):
    """Make ``deliver`` deterministic: skip the actual tmux paste.

    * ``session_exists`` -- whether the recipient session is "running".
    * ``busy``           -- if True, the recipient is reported mid-turn.
    * ``paste``          -- return value of the (mocked) paste layer.
    """
    state = {"delivered": 1, "completed": 0, "since": 0, "by": "tester"} if busy else None

    def _busy(cfg, agent):
        return state

    def _paste(cfg, session, body, enter, needle=None):
        return paste

    with mock.patch.object(swarm, "session_exists", lambda s: session_exists), mock.patch.object(
        swarm, "busy_info", _busy
    ) if busy else mock.patch.object(swarm, "busy_info", lambda cfg, a: None), mock.patch.object(
        swarm, "_paste_locked", _paste
    ):
        yield


def write_config(tmp_path, body, name="swarm.yaml"):
    path = tmp_path / name
    path.write_text(body)
    return path


def agent_yaml(tmp_path, agents_block, **swarm_over):
    """Build a minimal valid swarm config with the given agents + swarm overrides."""
    root = tmp_path / "ws"
    root.mkdir(exist_ok=True)
    swarm_bits = [f"root: {root}", 'session_prefix: "t-"']
    for k, v in swarm_over.items():
        swarm_bits.append(f"{k}: {v}")
    return write_config(
        tmp_path,
        "swarm:\n  " + "\n  ".join(swarm_bits) + "\n"
        "defaults: {type: claude}\n"
        "agents:\n" + agents_block,
    )


def load_swarm(tmp_path, agents_block, **swarm_over):
    """Write an agent_yaml and return the loaded SwarmConfig."""
    path = agent_yaml(tmp_path, agents_block, **swarm_over)
    return cfgmod.load(path)
