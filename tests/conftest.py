"""Shared pytest fixtures and stdlib-only helpers for the Agentainer test suite.

The suite targets 100% line coverage of ``lib/`` (config.py, minyaml.py,
swarm.py). tmux is mocked everywhere except where an integration test opts in,
so the suite runs fast and offline.
"""

import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import config as cfgmod  # noqa: E402
import swarm  # noqa: E402

EXAMPLE_CONFIGS = sorted((REPO / "examples").glob("*.yaml")) + [REPO / "agents.example.yaml"]


@pytest.fixture(autouse=True)
def _no_real_sleep():
    """Speeds the suite up: real pauses would only slow CI without adding signal."""
    with mock.patch.object(swarm, "sleep_ms", lambda ms: None):
        yield


@pytest.fixture
def tmp_runtime(tmp_path):
    """A SwarmConfig whose runtime dirs live under a temp path (no real tmux)."""
    root = tmp_path / "ws"
    root.mkdir()
    cfg = cfgmod.SwarmConfig(
        path=tmp_path / "swarm.yaml",
        name="t",
        root=root,
        session_prefix="t-",
        agents=[],
    )
    return cfg


def load_config(text, tmp_path):
    """Write *text* to a temp YAML, resolve its root, and return the loaded config."""
    path = tmp_path / "swarm.yaml"
    path.write_text(text)
    return cfgmod.load(path)
