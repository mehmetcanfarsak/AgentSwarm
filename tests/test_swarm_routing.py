"""Tests for swarm.py message routing, queues, and turn completion."""

import json
from unittest import mock

import pytest

import swarm
from config import Agent
from tests.support import load_swarm


def make_cfg(tmp_path, block=None, **over):
    if block is None:
        block = (
            "- {name: A, command: 'true', can_talk_to: [B]}\n"
            "- {name: B, command: 'true', can_talk_to: [A]}\n"
        )
    return load_swarm(tmp_path, block, **over)


# ------------------------------------------------------------------- deliver

def test_deliver_empty_text(tmp_path):
    cfg = make_cfg(tmp_path)
    with pytest.raises(swarm.SwarmError):
        swarm.deliver(cfg, "A", "B", "    ")


def test_deliver_acl_denied(tmp_path):
    cfg = load_swarm(
        tmp_path,
        "- {name: A, command: 'true', can_talk_to: []}\n"
        "- {name: B, command: 'true'}\n",
    )
    with pytest.raises(swarm.SwarmError):
        swarm.deliver(cfg, "A", "B", "hi")


def test_deliver_session_missing(tmp_path):
    cfg = make_cfg(tmp_path)
    with mock.patch.object(swarm, "session_exists", lambda s: False):
        with pytest.raises(swarm.SwarmError):
            swarm.deliver(cfg, "A", "B", "hi")


def test_deliver_busy(tmp_path):
    cfg = make_cfg(tmp_path)
    state = {"delivered": 1, "completed": 0, "since": 1e18, "by": "A", "age_s": 3}

    def busy(cfg, agent):
        return state

    with mock.patch.object(swarm, "session_exists", lambda s: True), mock.patch.object(
        swarm, "busy_info", busy
    ):
        with pytest.raises(swarm.BusyError):
            swarm.deliver(cfg, "A", "B", "hi")


def test_deliver_success_archives_and_logs(tmp_path):
    cfg = make_cfg(tmp_path)
    with mock.patch.object(swarm, "session_exists", lambda s: True), mock.patch.object(
        swarm, "busy_info", lambda cfg, a: None
    ), mock.patch.object(swarm, "_paste_locked", lambda *a, **k: True):
        mid = swarm.deliver(cfg, "A", "B", "hello body")
    assert mid.startswith("m-")
    # Archived into B's inbox.
    inbox = list((cfg.inbox_dir / "B").glob("*.md"))
    assert inbox and "hello body" in inbox[0].read_text()
    # Logged both as sent and received.
    sent = [json.loads(l) for l in (cfg.log_dir / "A.jsonl").read_text().splitlines()]
    recv = [json.loads(l) for l in (cfg.log_dir / "B.jsonl").read_text().splitlines()]
    assert sent[0]["kind"] == "sent" and recv[0]["kind"] == "received"
    # Turn marked started.
    ts = swarm.turn_state(cfg, "B")
    assert ts["delivered"] == 1 and ts["by"] == "A"


def test_deliver_reply_to_does_not_create_obligation(tmp_path):
    cfg = make_cfg(tmp_path)
    with mock.patch.object(swarm, "session_exists", lambda s: True), mock.patch.object(
        swarm, "busy_info", lambda cfg, a: None
    ), mock.patch.object(swarm, "_paste_locked", lambda *a, **k: True):
        swarm.deliver(cfg, "A", "B", "hi", reply_to="m-1", expects_reply=True)
    # A reply answers a question, so no pending reminder obligation.
    assert swarm.read_pending(cfg, "B") is None


def test_deliver_expects_reply_false_no_obligation(tmp_path):
    cfg = make_cfg(tmp_path)
    with mock.patch.object(swarm, "session_exists", lambda s: True), mock.patch.object(
        swarm, "busy_info", lambda cfg, a: None
    ), mock.patch.object(swarm, "_paste_locked", lambda *a, **k: True):
        swarm.deliver(cfg, "A", "B", "hi", expects_reply=False)
    assert swarm.read_pending(cfg, "B") is None


# -------------------------------------------------------------------- enqueue

def test_enqueue_writes_queue_and_logs(tmp_path):
    cfg = make_cfg(tmp_path)
    item_id, depth = swarm.enqueue(cfg, "A", "B", "q", 0)
    assert depth == 1 and item_id
    items = swarm.queue_read(cfg, "B")
    assert items and items[0]["text"] == "q" and items[0]["from"] == "A"


# ----------------------------------------------------------------- route_outbound

def test_route_tags_off_passthrough(tmp_path):
    cfg = make_cfg(tmp_path)
    agent = cfg.get("A")
    agent.parse_outbound_tags = False
    rem, reached, problems = swarm.route_outbound(
        cfg, agent, '<swarm-send to="B">hi</swarm-send>'
    )
    assert rem == '<swarm-send to="B">hi</swarm-send>' and reached == [] and problems == []


def test_route_empty_text_passthrough(tmp_path):
    cfg = make_cfg(tmp_path)
    rem, reached, problems = swarm.route_outbound(cfg, cfg.get("A"), "")
    assert rem == "" and reached == [] and problems == []


def test_route_sends_to_peer(tmp_path):
    cfg = make_cfg(tmp_path)
    calls = []

    def fake_deliver(c, sender, target, body, **kw):
        calls.append((sender, target, body))
        return "m-x"

    with mock.patch.object(swarm, "deliver", fake_deliver):
        rem, reached, problems = swarm.route_outbound(
            cfg, cfg.get("A"), '<swarm-send to="B">hi</swarm-send>'
        )
    assert reached == ["B"] and not problems and rem == ""
    assert calls == [("A", "B", "hi")]


def test_route_broadcast_no_peers(tmp_path):
    cfg = load_swarm(
        tmp_path,
        "- {name: A, command: 'true', can_talk_to: []}\n"
        "- {name: B, command: 'true'}\n",
    )
    with mock.patch.object(swarm, "deliver") as d:
        rem, reached, problems = swarm.route_outbound(
            cfg, cfg.get("A"), "<swarm-broadcast>x</swarm-broadcast>"
        )
    assert reached == [] and d.call_count == 0
    assert any("may not message anyone" in p for p in problems)


def test_route_send_missing_to(tmp_path):
    cfg = make_cfg(tmp_path)
    with mock.patch.object(swarm, "deliver") as d:
        rem, reached, problems = swarm.route_outbound(
            cfg, cfg.get("A"), "<swarm-send>hi</swarm-send>"
        )
    assert reached == [] and d.call_count == 0
    assert any("missing its `to`" in p for p in problems)


def test_route_send_to_unknown_agent(tmp_path):
    cfg = make_cfg(tmp_path)
    with mock.patch.object(swarm, "deliver") as d:
        rem, reached, problems = swarm.route_outbound(
            cfg, cfg.get("A"), '<swarm-send to="Z">hi</swarm-send>'
        )
    assert reached == [] and d.call_count == 0
    assert any("not an agent" in p for p in problems)


def test_route_busy_queues(tmp_path):
    cfg = make_cfg(tmp_path)

    def fake_busy(c, sender, target, body, **kw):
        raise swarm.BusyError("busy")

    with mock.patch.object(swarm, "deliver", fake_busy):
        rem, reached, problems = swarm.route_outbound(
            cfg, cfg.get("A"), '<swarm-send to="B">hi</swarm-send>'
        )
    assert reached == ["B"] and not problems
    assert swarm.queue_read(cfg, "B")


def test_route_delivery_error_reported(tmp_path):
    cfg = make_cfg(tmp_path)

    def fake_err(c, sender, target, body, **kw):
        raise swarm.SwarmError("nope")

    with mock.patch.object(swarm, "deliver", fake_err):
        rem, reached, problems = swarm.route_outbound(
            cfg, cfg.get("A"), '<swarm-send to="B">hi</swarm-send>'
        )
    assert reached == [] and any("was not delivered" in p for p in problems)


# ------------------------------------------------------------- drain / sweep

def test_drain_queue_empty(tmp_path):
    cfg = make_cfg(tmp_path)
    assert swarm.drain_queue(cfg, cfg.get("B")) is False


def test_drain_queue_delivers_head(tmp_path):
    cfg = make_cfg(tmp_path)
    swarm.enqueue(cfg, "A", "B", "q1", 0)
    swarm.enqueue(cfg, "A", "B", "q2", 0)
    with mock.patch.object(swarm, "_paste_locked", lambda *a, **k: True), mock.patch.object(
        swarm, "session_exists", lambda s: True
    ), mock.patch.object(swarm, "busy_info", lambda cfg, a: None):
        assert swarm.drain_queue(cfg, cfg.get("B")) is True
    items = swarm.queue_read(cfg, "B")
    assert len(items) == 1 and items[0]["text"] == "q2"


def test_drain_queue_busy_returns_false(tmp_path):
    cfg = make_cfg(tmp_path)
    swarm.enqueue(cfg, "A", "B", "q1", 0)

    def busy(cfg, agent):
        return {"delivered": 1, "completed": 0, "since": 1e18, "by": "A"}

    with mock.patch.object(swarm, "busy_info", busy):
        assert swarm.drain_queue(cfg, cfg.get("B")) is False


def test_drain_queue_swarmerror_warns(tmp_path):
    cfg = make_cfg(tmp_path)
    swarm.enqueue(cfg, "A", "B", "q1", 0)

    def fake_err(c, sender, target, body, **kw):
        raise swarm.SwarmError("nope")

    with mock.patch.object(swarm, "deliver", fake_err), mock.patch.object(swarm, "warn") as w:
        assert swarm.drain_queue(cfg, cfg.get("B")) is False
        assert w.called


def test_sweep_stale_queues(tmp_path):
    cfg = make_cfg(tmp_path)
    # B has a stranded queue; B is idle (no turn state) so it should be drained.
    swarm.enqueue(cfg, "A", "B", "stranded", 0)
    delivered = {"n": 0}

    def fake_deliver(c, sender, target, body, **kw):
        delivered["n"] += 1
        return "m-x"

    with mock.patch.object(swarm, "deliver", fake_deliver):
        swarm.sweep_stale_queues(cfg)
    assert delivered["n"] == 1
    assert swarm.queue_read(cfg, "B") == []


def test_sweep_stale_queues_skips_busy(tmp_path):
    cfg = make_cfg(tmp_path)
    swarm.enqueue(cfg, "A", "B", "stranded", 0)

    def busy(cfg, agent):
        return {"delivered": 1, "completed": 0, "since": 1e18, "by": "A"}

    with mock.patch.object(swarm, "busy_info", busy), mock.patch.object(
        swarm, "deliver"
    ) as d:
        swarm.sweep_stale_queues(cfg)
    assert d.call_count == 0


def test_sweep_stale_queues_excludes_self(tmp_path):
    cfg = make_cfg(tmp_path)
    # If "B" were excluded we would not try to drain it.
    with mock.patch.object(swarm, "drain_queue") as dq:
        swarm.sweep_stale_queues(cfg, exclude="B")
    # drain_queue should not have been called for B.
    assert all(c.args[1].name != "B" for c in dq.call_args_list)


def test_sweep_stale_queues_suppresses_drain_error(tmp_path):
    cfg = make_cfg(tmp_path)
    swarm.enqueue(cfg, "A", "B", "stranded", 0)
    with mock.patch.object(swarm, "busy_info", lambda c, a: None), \
         mock.patch.object(swarm, "drain_queue", side_effect=swarm.SwarmError("wedged")), \
         mock.patch.object(swarm, "warn") as warn:
        # A draining failure must not propagate out of the sweep.
        swarm.sweep_stale_queues(cfg)
    assert any("could not drain stranded queue" in str(a) for w in warn.call_args_list for a in w)


# --------------------------------------------------------- forward_response

def test_forward_none_configured(tmp_path):
    cfg = make_cfg(tmp_path)
    assert swarm.forward_response(cfg, cfg.get("B"), "text") == []


def test_forward_hop_limit(tmp_path):
    cfg = make_cfg(tmp_path)
    agent = cfg.get("B")
    agent.forward_responses_to = ["A"]
    swarm.write_hops(cfg, "B", cfg.max_forward_hops)
    with mock.patch.object(swarm, "warn") as w:
        assert swarm.forward_response(cfg, agent, "text") == []
        assert w.called


def test_forward_success(tmp_path):
    cfg = make_cfg(tmp_path)
    agent = cfg.get("B")
    agent.forward_responses_to = ["A"]
    calls = []

    def fake_deliver(c, sender, target, body, **kw):
        calls.append(target)
        return "m-x"

    with mock.patch.object(swarm, "deliver", fake_deliver):
        reached = swarm.forward_response(cfg, agent, "text")
    assert reached == ["A"] and calls == ["A"]


def test_forward_busy_queues(tmp_path):
    cfg = make_cfg(tmp_path)
    agent = cfg.get("B")
    agent.forward_responses_to = ["A"]

    def fake_busy(c, sender, target, body, **kw):
        raise swarm.BusyError("busy")

    with mock.patch.object(swarm, "deliver", fake_busy):
        reached = swarm.forward_response(cfg, agent, "text")
    assert reached == ["A"] and swarm.queue_read(cfg, "A")


# --------------------------------------------------------- reply reminders

def test_handle_reply_reminder_disabled(tmp_path):
    cfg = make_cfg(tmp_path)
    agent = cfg.get("B")
    agent.reply_reminder = False
    # Should return without delivering or writing anything.
    with mock.patch.object(swarm, "deliver") as d:
        swarm.handle_reply_reminder(cfg, agent, [], [])
    assert d.call_count == 0
    assert swarm.read_pending(cfg, "B") is None


def test_handle_reply_reminder_answered(tmp_path):
    cfg = make_cfg(tmp_path)
    agent = cfg.get("B")
    swarm.write_pending(cfg, "B", {"from": "A", "id": "m-1", "reminders": 0})
    with mock.patch.object(swarm, "deliver") as d:
        swarm.handle_reply_reminder(cfg, agent, ["A"], [])
    assert d.call_count == 0
    assert swarm.read_pending(cfg, "B") is None


def test_handle_reply_reminder_reminds(tmp_path):
    cfg = make_cfg(tmp_path)
    agent = cfg.get("B")
    agent.can_talk_to = ["A"]
    swarm.write_pending(cfg, "B", {"from": "A", "id": "m-1", "reminders": 0})
    with mock.patch.object(swarm, "_paste_locked", lambda *a, **k: True), mock.patch.object(
        swarm, "session_exists", lambda s: True
    ), mock.patch.object(swarm, "busy_info", lambda cfg, a: None):
        swarm.handle_reply_reminder(cfg, agent, [], [])
    pend = swarm.read_pending(cfg, "B")
    assert pend and pend["reminders"] == 1


def test_handle_reply_reminder_gives_up(tmp_path):
    cfg = make_cfg(tmp_path)
    agent = cfg.get("B")
    agent.can_talk_to = ["A"]
    swarm.write_pending(cfg, "B", {"from": "A", "id": "m-1", "reminders": 99})
    with mock.patch.object(swarm, "warn") as w, mock.patch.object(swarm, "deliver") as d:
        swarm.handle_reply_reminder(cfg, agent, [], [])
    assert w.called and d.call_count == 0
    assert swarm.read_pending(cfg, "B") is None


def test_handle_reply_reminder_send_failed_template(tmp_path):
    cfg = make_cfg(tmp_path)
    agent = cfg.get("B")
    agent.can_talk_to = ["A"]
    # No `from` => a syntax-correction nudge, not "someone is waiting".
    with mock.patch.object(swarm, "_paste_locked", lambda *a, **k: True), mock.patch.object(
        swarm, "session_exists", lambda s: True
    ), mock.patch.object(swarm, "busy_info", lambda cfg, a: None):
        swarm.handle_reply_reminder(cfg, agent, [], ["you had a problem"])
    pend = swarm.read_pending(cfg, "B")
    assert pend and pend["reminders"] == 1


def test_handle_reply_reminder_malformed_template(tmp_path):
    cfg = make_cfg(tmp_path)
    agent = cfg.get("B")
    agent.can_talk_to = ["A"]
    cfg.reply_reminder_template = "hi {nope} {missing}"
    swarm.write_pending(cfg, "B", {"from": "A", "id": "m-1", "reminders": 0})
    with mock.patch.object(swarm, "_paste_locked", lambda *a, **k: True), mock.patch.object(
        swarm, "session_exists", lambda s: True
    ), mock.patch.object(swarm, "busy_info", lambda cfg, a: None), mock.patch.object(
        swarm, "warn"
    ) as w:
        swarm.handle_reply_reminder(cfg, agent, [], [])
    assert w.called
    assert swarm.read_pending(cfg, "B")["reminders"] == 1


def test_handle_reply_reminder_deliver_busy_queues(tmp_path):
    cfg = make_cfg(tmp_path)
    agent = cfg.get("B")
    agent.can_talk_to = ["A"]
    swarm.write_pending(cfg, "B", {"from": "A", "id": "m-1", "reminders": 0})

    def fake_busy(c, sender, target, body, **kw):
        raise swarm.BusyError("busy")

    with mock.patch.object(swarm, "deliver", fake_busy):
        swarm.handle_reply_reminder(cfg, agent, [], [])
    assert swarm.queue_read(cfg, "B")


def test_handle_reply_reminder_deliver_swarmerror(tmp_path):
    cfg = make_cfg(tmp_path)
    agent = cfg.get("B")
    agent.can_talk_to = ["A"]
    swarm.write_pending(cfg, "B", {"from": "A", "id": "m-1", "reminders": 0})

    def fake_err(c, sender, target, body, **kw):
        raise swarm.SwarmError("nope")

    with mock.patch.object(swarm, "deliver", fake_err), mock.patch.object(swarm, "warn") as w:
        swarm.handle_reply_reminder(cfg, agent, [], [])
    assert w.called


# ------------------------------------------------------------ on_turn_finished

def test_on_turn_finished_simple(tmp_path):
    cfg = make_cfg(tmp_path)
    swarm.write_turn_state(cfg, "B", {"delivered": 1, "completed": 0, "since": 0, "by": "A"})
    swarm.on_turn_finished(cfg, cfg.get("B"), "just prose, no tags")
    # Turn marked finished.
    assert swarm.turn_state(cfg, "B")["completed"] == 1


def test_on_turn_finished_drains_queued(tmp_path):
    cfg = make_cfg(tmp_path)
    swarm.write_turn_state(cfg, "B", {"delivered": 1, "completed": 0, "since": 0, "by": "A"})
    swarm.enqueue(cfg, "A", "B", "queued msg", 0)
    with mock.patch.object(swarm, "_paste_locked", lambda *a, **k: True), mock.patch.object(
        swarm, "session_exists", lambda s: True
    ), mock.patch.object(swarm, "busy_info", lambda cfg, a: None):
        # The queued message is delivered, so no reminder is needed.
        with mock.patch.object(swarm, "handle_reply_reminder") as hr:
            swarm.on_turn_finished(cfg, cfg.get("B"), "")
    assert swarm.queue_read(cfg, "B") == []
    assert hr.call_count == 0
