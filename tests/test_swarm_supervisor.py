"""Unit tests for the supervisor (the swarm's liveness heartbeat).

These exercise ``supervise_once`` directly with the tmux-backed helpers
monkeypatched, so no real tmux is needed and the per-tick reconciliation logic
is fully deterministic.
"""

from pathlib import Path
from unittest import mock

import pytest
import sys

import config as cfgmod
import swarm


def _agent(name, capture="hook"):
    return cfgmod.Agent(
        name=name,
        type="claude",
        command="true",
        workdir=Path("/tmp") / name,
        session="t-" + name,
        capture=capture,
        boot_delay_ms=100,
        first_prompt="",
    )


def _cfg(tmp_path, *agents):
    root = tmp_path / "ws"
    root.mkdir()
    return cfgmod.SwarmConfig(
        path=tmp_path / "swarm.yaml",
        name="t",
        root=root,
        session_prefix="t-",
        agents=list(agents),
    )


def _write_swarm(tmp_path, *agents):
    root = tmp_path / "ws"
    root.mkdir()
    body = (
        "swarm: {name: t, root: " + str(root) + ", session_prefix: 't-'}\n"
        "agents:\n"
        + "".join(f"  - {{name: {a.name}, type: {a.type}, command: 'true'}}\n" for a in agents)
    )
    path = tmp_path / "swarm.yaml"
    path.write_text(body)
    return path


@pytest.fixture
def patched():
    """Monkeypatch every external primitive supervise_once touches."""
    rec = {
        "mark": [],        # mark_turn_finished args
        "drain": [],       # drain_queue args
        "sweep": 0,        # sweep_stale_queues call count
        "log": [],         # log_event calls
        "warn": [],        # warn calls
        "session": {},     # name -> session exists?
        "turn": {},        # name -> turn state dict
        "queue": {},       # name -> queued list (for dead-stranding warning)
    }

    def _session_exists(session):
        # Session ids are "t-NAME"; recover the agent name.
        name = session.split("t-", 1)[-1]
        return rec["session"].get(name, True)

    def _turn_state(cfg, name):
        return rec["turn"].get(name, {"delivered": 0, "completed": 0, "since": 0, "by": None})

    def _queue_read(cfg, name):
        return rec["queue"].get(name, [])

    with mock.patch.object(swarm, "session_exists", _session_exists), \
         mock.patch.object(swarm, "turn_state", _turn_state), \
         mock.patch.object(swarm, "queue_read", _queue_read), \
         mock.patch.object(swarm, "mark_turn_finished", lambda c, n: rec["mark"].append(n)), \
         mock.patch.object(swarm, "drain_queue", lambda c, a: rec["drain"].append(a.name)), \
         mock.patch.object(swarm, "sweep_stale_queues", lambda c: rec.__setitem__("sweep", rec["sweep"] + 1)), \
         mock.patch.object(swarm, "log_event", lambda c, n, k, **kw: rec["log"].append((n, k))), \
         mock.patch.object(swarm, "warn", lambda *a, **k: rec["warn"].append(a)):
        yield rec


def test_supervise_leaves_idle_agent_alone(tmp_path, patched):
    cfg = _cfg(tmp_path, _agent("A"))
    patched["session"]["A"] = True
    patched["turn"]["A"] = {"delivered": 0, "completed": 0, "since": 0, "by": None}
    swarm.supervise_once(cfg, ["A"], set())
    assert patched["mark"] == []
    assert patched["drain"] == []
    assert patched["sweep"] == 1  # sweep runs every pass regardless


def test_supervise_reconciles_stale_busy(tmp_path, patched):
    cfg = _cfg(tmp_path, _agent("A"))
    patched["session"]["A"] = True
    # delivered > completed and older than the busy_timeout.
    patched["turn"]["A"] = {"delivered": 3, "completed": 0, "since": 0, "by": "analyst"}
    cfg.busy_timeout_ms = 1  # make any non-zero age exceed it
    swarm.supervise_once(cfg, ["A"], set())
    assert patched["mark"] == ["A"]
    assert patched["drain"] == ["A"]
    assert patched["sweep"] == 1


def test_supervise_ignores_freshly_busy(tmp_path, patched):
    cfg = _cfg(tmp_path, _agent("A"))
    patched["session"]["A"] = True
    patched["turn"]["A"] = {
        "delivered": 1,
        "completed": 0,
        "since": __import__("time").time(),  # brand new turn, under timeout
        "by": "lead",
    }
    cfg.busy_timeout_ms = 900000
    swarm.supervise_once(cfg, ["A"], set())
    assert patched["mark"] == []
    assert patched["drain"] == []


def test_supervise_handles_dead_session_and_logs_once(tmp_path, patched):
    cfg = _cfg(tmp_path, _agent("A"))
    patched["session"]["A"] = False  # session gone
    seen: set[str] = set()
    swarm.supervise_once(cfg, ["A"], seen)
    # Logged + reconciled the turn, but did NOT try to deliver into a dead pane.
    assert patched["log"] == [("A", "dead")]
    assert patched["mark"] == ["A"]
    assert patched["drain"] == []
    # Second tick: already seen dead -> no duplicate log / reconcile.
    swarm.supervise_once(cfg, ["A"], seen)
    assert patched["log"] == [("A", "dead")]
    assert patched["mark"] == ["A"]


def test_supervise_warns_on_stranded_queue_when_dead(tmp_path, patched):
    cfg = _cfg(tmp_path, _agent("A"))
    patched["session"]["A"] = False
    patched["queue"]["A"] = [{"from": "lead", "text": "still waiting"}]
    swarm.supervise_once(cfg, ["A"], set())
    joined = " ".join(str(w) for w in patched["warn"])
    assert "stranded" in joined
    assert "A" in joined


def test_supervise_resurrected_agent_clears_seen_dead(tmp_path, patched):
    cfg = _cfg(tmp_path, _agent("A"))
    patched["session"]["A"] = False
    seen: set[str] = set()
    swarm.supervise_once(cfg, ["A"], seen)
    assert "A" in seen
    # Agent comes back to life: seen_dead is cleared, and a later death re-logs.
    patched["session"]["A"] = True
    patched["turn"]["A"] = {"delivered": 0, "completed": 0, "since": 0, "by": None}
    swarm.supervise_once(cfg, ["A"], seen)
    assert "A" not in seen
    patched["session"]["A"] = False
    swarm.supervise_once(cfg, ["A"], seen)
    assert patched["log"].count(("A", "dead")) == 2


def test_supervise_suppresses_drain_error(tmp_path, patched):
    # If draining the stranded queue raises, supervise_once must swallow it and
    # still complete the pass (so one bad agent can't wedge the whole supervisor).
    cfg = _cfg(tmp_path, _agent("A"))
    patched["session"]["A"] = True
    patched["turn"]["A"] = {"delivered": 3, "completed": 0, "since": 0, "by": "lead"}
    cfg.busy_timeout_ms = 1

    def _drain_raises(cfg, agent):
        raise swarm.SwarmError("queue wedged")

    with mock.patch.object(swarm, "drain_queue", _drain_raises), \
         mock.patch.object(swarm, "warn") as warn:
        swarm.supervise_once(cfg, ["A"], set())
    assert patched["mark"] == ["A"]
    assert any("could not drain queue" in str(a) for w in warn.call_args_list for a in w)


def test_start_supervisor_launches_and_records_pid(tmp_path):
    path = _write_swarm(tmp_path, _agent("A"))
    cfg = cfgmod.load(path)
    fake = mock.Mock()
    fake.pid = 2468
    captured = {}

    class _Popen:
        def __init__(self, args, **kw):
            captured["args"] = args
            captured["env"] = kw.get("env", {})
            self.pid = fake.pid

    with mock.patch.object(swarm.subprocess, "Popen", _Popen):
        swarm.start_supervisor(cfg, ["A"])
    # It launches the supervise subcommand, passing the config via SWARM_CONFIG.
    args = captured["args"]
    assert "supervise" in args
    assert args[0] == sys.executable
    assert captured["env"]["SWARM_CONFIG"] == str(cfg.path)
    # The pid file records the launched process id.
    assert (cfg.run_dir / "supervisor.pid").read_text() == "2468"


def test_stop_supervisor_kills_and_clears_pid(tmp_path):
    cfg = _cfg(tmp_path, _agent("A"))
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    (cfg.run_dir / "supervisor.pid").write_text("4321")
    with mock.patch.object(swarm.os, "kill") as kill:
        swarm.stop_supervisor(cfg)
    kill.assert_called_once_with(4321, 15)
    assert not (cfg.run_dir / "supervisor.pid").exists()


def test_stop_supervisor_no_pid_is_noop(tmp_path):
    cfg = _cfg(tmp_path, _agent("A"))
    with mock.patch.object(swarm.os, "kill") as kill:
        swarm.stop_supervisor(cfg)
    kill.assert_not_called()


def test_stop_supervisor_survives_kill_error(tmp_path):
    cfg = _cfg(tmp_path, _agent("A"))
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    (cfg.run_dir / "supervisor.pid").write_text("4321")
    with mock.patch.object(swarm.os, "kill", side_effect=OSError("esrch")):
        # Must not raise even if the process is already gone; pid file cleared.
        swarm.stop_supervisor(cfg)
    assert not (cfg.run_dir / "supervisor.pid").exists()


def test_supervisor_alive_kill_error_means_dead(tmp_path):
    cfg = _cfg(tmp_path, _agent("A"))
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    (cfg.run_dir / "supervisor.pid").write_text("777")
    with mock.patch.object(swarm.os, "kill", side_effect=OSError("esrch")):
        # A kill probe that fails means the pid is not running.
        assert swarm.supervisor_alive(cfg) is False


def test_supervisor_alive_bad_pid_file(tmp_path):
    cfg = _cfg(tmp_path, _agent("A"))
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    (cfg.run_dir / "supervisor.pid").write_text("not-a-number")
    # A non-integer pid must not raise; treated as not alive.
    assert swarm.supervisor_alive(cfg) is False


def test_supervisor_alive_true_and_false(tmp_path):
    cfg = _cfg(tmp_path, _agent("A"))
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    (cfg.run_dir / "supervisor.pid").write_text("777")
    with mock.patch.object(swarm.os, "kill") as kill:
        assert swarm.supervisor_alive(cfg) is True
    kill.assert_called_once_with(777, 0)
    # No pid file -> not alive.
    (cfg.run_dir / "supervisor.pid").unlink()
    assert swarm.supervisor_alive(cfg) is False


def test_run_supervisor_exits_when_no_sessions(tmp_path):
    cfg = _cfg(tmp_path, _agent("A"))
    with mock.patch.object(swarm, "session_exists", lambda s: False), \
         mock.patch.object(swarm, "supervise_once") as so, \
         mock.patch.object(swarm, "info") as info:
        swarm.run_supervisor(cfg, ["A"])
    assert not so.called  # nothing left to watch -> exits without reconciling
    assert info.call_count >= 2  # "started" + "no watched sessions remain"


def test_run_supervisor_reconciles_then_exits(tmp_path):
    cfg = _cfg(tmp_path, _agent("A"))
    calls = {"n": 0}

    def _sess(_s):
        calls["n"] += 1
        return calls["n"] == 1  # alive the first tick, gone after

    with mock.patch.object(swarm, "session_exists", _sess), \
         mock.patch.object(swarm, "supervise_once") as so:
        swarm.run_supervisor(cfg, ["A"])
    assert so.call_count == 1


def test_cmd_supervise_defaults_to_all_agents(tmp_path):
    path = _write_swarm(tmp_path, _agent("A"), _agent("B"))
    with mock.patch.object(swarm, "session_exists", lambda s: False), \
         mock.patch.object(swarm, "run_supervisor") as rs:
        rc = swarm.cmd_supervise(
            mock.Mock(config=str(path), names=[])
        )
    assert rc == 0
    rs.assert_called_once()
    assert [a.name for a in rs.call_args[0][0].agents] == ["A", "B"]


def test_cmd_supervise_explicit_names(tmp_path):
    path = _write_swarm(tmp_path, _agent("A"), _agent("B"))
    with mock.patch.object(swarm, "session_exists", lambda s: False), \
         mock.patch.object(swarm, "run_supervisor") as rs:
        rc = swarm.cmd_supervise(
            mock.Mock(config=str(path), names=["A"])
        )
    assert rc == 0
    rs.assert_called_once()
    assert [a.name for a in rs.call_args[0][0].agents] == ["A", "B"]

