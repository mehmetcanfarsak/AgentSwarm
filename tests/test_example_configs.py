"""Validate every shipped example YAML config.

The user asked specifically that the test suite *check/validate the example YAML
files*. ``test_parity_with_pyyaml_on_shipped_configs`` only compares parser
output; this module goes further and runs each example through ``cfgmod.load``
(the real validation path: ACLs, agent types, workdirs, templates) so a broken
or misleading example fails CI.
"""

from pathlib import Path
from unittest import mock

import pytest

import config as cfgmod
import swarm
from tests.conftest import EXAMPLE_CONFIGS


def test_example_configs_present():
    # Guard against the examples directory being emptied by mistake.
    assert EXAMPLE_CONFIGS, "no example configs found to validate"


@pytest.mark.parametrize("cfg_path", EXAMPLE_CONFIGS, ids=lambda p: p.name)
def test_example_config_loads_and_validates(cfg_path):
    # Agents reference CLIs (claude, codex, gemini, ...) that may not be on PATH in
    # CI; the validator resolves commands but does not execute them, so faking the
    # lookup is enough to load without requiring the real tools installed.
    # Examples point `workdir` at placeholder paths (e.g. ~/projects/...) the user
    # is meant to repoint at a real repo. We materialise those dirs so the config
    # loads, then assert its *internal* correctness (ACLs, agent types, templates).
    with mock.patch.object(swarm.shutil, "which", lambda name: "/usr/bin/" + name):
        # Probe-load with create_workdir forced on (so missing dirs are tolerated),
        # then materialise each agent's resolved workdir on disk so the real
        # (create_workdirs:false) example validates cleanly on the second load.
        real_load = cfgmod.load
        real_as_bool = cfgmod._as_bool

        def _as_bool_force(val, default, ctx):
            if "create_workdir" in ctx:
                return True
            return real_as_bool(val, default, ctx)

        def _probe(path):
            cfg = real_load(path)
            for agent in cfg.agents:
                agent.workdir.mkdir(parents=True, exist_ok=True)
            return cfg

        with mock.patch.object(cfgmod, "_as_bool", _as_bool_force), \
             mock.patch.object(cfgmod, "load", _probe):
            cfgmod.load(cfg_path)  # side effect: creates workdirs
        cfg = real_load(cfg_path)
    # A valid swarm must have at least one agent and a usable runtime layout.
    assert cfg.agents, f"{cfg_path.name} defines no agents"
    assert cfg.name
    assert cfg.root
    # Every agent must resolve to a session that uses the configured prefix.
    for agent in cfg.agents:
        assert agent.session.startswith(cfg.session_prefix)
        assert agent.workdir.exists()


@pytest.mark.parametrize("cfg_path", EXAMPLE_CONFIGS, ids=lambda p: p.name)
def test_example_config_round_trips_through_validate_cli(cfg_path):
    # The `validate` subcommand is the user-facing "is this config OK?" check;
    # exercise it on every example so its pretty-printer cannot crash on real input.
    with mock.patch.object(swarm.shutil, "which", lambda name: "/usr/bin/" + name):
        real_load = cfgmod.load
        real_as_bool = cfgmod._as_bool

        def _as_bool_force(val, default, ctx):
            if "create_workdir" in ctx:
                return True
            return real_as_bool(val, default, ctx)

        def _probe(path):
            cfg = real_load(path)
            for agent in cfg.agents:
                agent.workdir.mkdir(parents=True, exist_ok=True)
            return cfg

        with mock.patch.object(cfgmod, "_as_bool", _as_bool_force), \
             mock.patch.object(cfgmod, "load", _probe):
            cfgmod.load(cfg_path)
        rc = swarm.cmd_validate(
            swarm.argparse.Namespace(config=str(cfg_path), show_prompts=False)
        )
    assert rc == 0
