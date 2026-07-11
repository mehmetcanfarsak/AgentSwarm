"""Pure / filesystem-only unit tests for lib/swarm.py (no tmux)."""

import json
from pathlib import Path
from unittest import mock

import pytest

import swarm
from config import Agent, SwarmConfig
from tests.conftest import load_config
from tests.support import load_swarm


# --------------------------------------------------------------- tiny helpers

def test_now_iso_is_utc():
    assert swarm.now_iso().endswith("+00:00")


def test_normalise_strips_whitespace():
    assert swarm.normalise("  a\n\t b  c ") == "abc"


def test_new_message_id_format():
    mid = swarm.new_message_id()
    assert mid.startswith("m-")
    assert len(mid) > 4


def test_outbound_dataclass():
    o = swarm.Outbound("send", "B", None, "hi", expects_reply=False)
    assert o.kind == "send" and o.to == "B" and o.body == "hi"
    assert o.expects_reply is False


def test_constants_exist():
    assert swarm.MESSAGE_HEADER == "[swarm] message from"
    assert swarm.ECHO_MEMORY == 300


# ------------------------------------------------------------- yaml emitter

def test_yaml_scalar():
    assert swarm.yaml_scalar(None) == "null"
    assert swarm.yaml_scalar(True) == "true"
    assert swarm.yaml_scalar(False) == "false"
    assert swarm.yaml_scalar(3) == "3"
    assert swarm.yaml_scalar(2.5) == "2.5"
    assert swarm.yaml_scalar("plain") == '"plain"'
    assert swarm.yaml_scalar('has "quote') == '"has \\"quote"'
    assert swarm.yaml_scalar("back\\slash") == '"back\\\\slash"'


def test_yaml_dump_roundtrip():
    import yaml

    data = {"a": 'q" \\ b', "b": None, "c": {"x": 1}, "d": {}}
    text = swarm.yaml_dump(data)
    assert yaml.safe_load(text) == data


def test_yaml_dump_nested_and_empty():
    assert swarm.yaml_dump({"a": {}}) == "a:\n  {}"
    assert "b:" in swarm.yaml_dump({"a": 1, "b": 2})
    nested = swarm.yaml_dump({"top": {"inner": {"x": 1}}})
    assert "inner:" in nested


# ------------------------------------------------------------ envelope + tags

def test_format_envelope_tagged(tmp_path):
    cfg = load_swarm(
        tmp_path,
        "- {name: A, command: 'true', can_talk_to: [B]}\n"
        "- {name: B, command: 'true'}\n",
    )
    env = swarm.format_envelope(cfg, "A", "B", "hi there", "m-1", None)
    assert env.startswith("<swarm-message")
    assert 'from="A"' in env and 'to="B"' in env and 'id="m-1"' in env
    assert "hi there" in env


def test_format_envelope_plain(tmp_path):
    cfg = load_swarm(
        tmp_path,
        "- {name: A, command: 'true', can_talk_to: [B]}\n"
        "- {name: B, command: 'true'}\n",
    )
    cfg.message_format = "plain"
    env = swarm.format_envelope(cfg, "A", "B", "hi there", "m-1", "m-0")
    assert env == "[swarm] message from A:\nhi there"
    # reply_to is ignored in plain format.


def test_parse_outbound_send_and_broadcast():
    msgs, remainder, problems = swarm.parse_outbound(
        'x<swarm-send to="a">hi</swarm-send>y'
    )
    assert len(msgs) == 1 and msgs[0].to == "a" and msgs[0].expects_reply
    assert remainder == "xy" and not problems

    msgs, _, _ = swarm.parse_outbound('<swarm-broadcast>yo</swarm-broadcast>')
    assert msgs[0].kind == "broadcast" and not msgs[0].expects_reply


def test_parse_outbound_expects_reply_false_and_reply_to():
    msgs, _, _ = swarm.parse_outbound(
        '<swarm-send to="a" expects-reply="false">fyi</swarm-send>'
    )
    assert not msgs[0].expects_reply

    msgs, _, _ = swarm.parse_outbound(
        "<swarm-send to='a' reply-to='m-1'>b</swarm-send>"
    )
    assert msgs[0].reply_to == "m-1"


def test_parse_outbound_empty_body_flagged():
    _, _, problems = swarm.parse_outbound('<swarm-send to="a"></swarm-send>')
    assert problems and any("empty body" in p for p in problems)


def test_parse_outbound_unclosed_flagged():
    msgs, _, problems = swarm.parse_outbound('<swarm-send to="a">body with no closing tag')
    assert not msgs and problems


def test_parse_outbound_prose_not_flagged():
    for prose in (
        "I'll use <swarm-send> blocks to answer.",
        "Use `<swarm-send>` blocks.",
        "I will <swarm-broadcast> the result later.",
    ):
        msgs, _, problems = swarm.parse_outbound(prose)
        assert not msgs and not problems, prose


def test_parse_outbound_real_unclosed_flagged():
    msgs, _, problems = swarm.parse_outbound('<swarm-send to="a">body no close')
    assert not msgs and problems


# ------------------------------------------------------- queue / turn / hops

def test_queue_read_write(tmp_runtime):
    assert swarm.queue_read(tmp_runtime, "X") == []
    items = [{"id": "1", "from": "A", "text": "hi"}]
    swarm.queue_write(tmp_runtime, "X", items)
    assert swarm.queue_read(tmp_runtime, "X") == items


def test_turn_state_default_and_write(tmp_runtime):
    assert swarm.turn_state(tmp_runtime, "X") == {
        "delivered": 0, "completed": 0, "since": 0, "by": None
    }
    state = {"delivered": 2, "completed": 1, "since": 5.0, "by": "A"}
    swarm.write_turn_state(tmp_runtime, "X", state)
    assert swarm.turn_state(tmp_runtime, "X") == state


def test_mark_turn_started_and_finished(tmp_runtime):
    cfg = tmp_runtime
    swarm.mark_turn_started(cfg, "X", "A")
    st = swarm.turn_state(cfg, "X")
    assert st["delivered"] == 1 and st["by"] == "A" and st["since"] > 0
    swarm.mark_turn_finished(cfg, "X")
    st = swarm.turn_state(cfg, "X")
    assert st["completed"] == 1


def test_busy_info_states(tmp_runtime):
    cfg = tmp_runtime
    agent = Agent(
        name="X", type="claude", command="true", workdir=cfg.root,
        session="t-X", capture="hook", boot_delay_ms=0, first_prompt="",
    )
    # Not busy: delivered <= completed.
    swarm.write_turn_state(cfg, "X", {"delivered": 0, "completed": 0, "since": 0, "by": None})
    assert swarm.busy_info(cfg, agent) is None

    # Busy: delivered > completed, recently.
    swarm.write_turn_state(cfg, "X", {"delivered": 1, "completed": 0, "since": 1e18, "by": "A"})
    state = swarm.busy_info(cfg, agent)
    assert state is not None and state["by"] == "A" and "age_s" in state

    # Stale-busy: beyond busy_timeout -> treated idle (None) and warns.
    cfg.busy_timeout_ms = 1
    swarm.write_turn_state(cfg, "X", {"delivered": 1, "completed": 0, "since": 0, "by": "A"})
    with mock.patch.object(swarm, "warn") as w:
        assert swarm.busy_info(cfg, agent) is None
        assert w.called


def test_busy_info_disabled(tmp_runtime):
    agent = Agent(
        name="X", type="claude", command="true", workdir=tmp_runtime.root,
        session="t-X", capture="none", boot_delay_ms=0, first_prompt="",
        busy_check=False,
    )
    swarm.write_turn_state(tmp_runtime, "X", {"delivered": 5, "completed": 0})
    assert swarm.busy_info(tmp_runtime, agent) is None


def test_busy_message():
    agent = Agent(
        name="X", type="claude", command="true", workdir=Path("/x"),
        session="t-X", capture="hook", boot_delay_ms=0, first_prompt="",
    )
    state = {"by": "A", "age_s": 3}
    msg = swarm.busy_message(None, agent, state)
    assert "busy" in msg and "A" in msg and "--queue" in msg


def test_hops_read_write(tmp_runtime):
    assert swarm.read_hops(tmp_runtime, "X") == 0
    swarm.write_hops(tmp_runtime, "X", 4)
    assert swarm.read_hops(tmp_runtime, "X") == 4


# ------------------------------------------------------------- echo / pending

def test_echo_record_and_read(tmp_runtime):
    swarm.record_echo(tmp_runtime, "X", "line one\nline two\n")
    assert "line one" in swarm.read_echo(tmp_runtime, "X")
    assert "line two" in swarm.read_echo(tmp_runtime, "X")
    # Memory is capped.
    swarm.record_echo(tmp_runtime, "X", "\n".join(f"l{i}" for i in range(500)))
    assert len(swarm.read_echo(tmp_runtime, "X")) <= swarm.ECHO_MEMORY


def test_pending_path_read_write(tmp_runtime):
    assert swarm.pending_path(tmp_runtime, "X").name == "X.pending.json"
    assert swarm.read_pending(tmp_runtime, "X") is None
    swarm.write_pending(tmp_runtime, "X", {"from": "A", "id": "m-1", "reminders": 0})
    assert swarm.read_pending(tmp_runtime, "X")["from"] == "A"
    swarm.write_pending(tmp_runtime, "X", None)
    assert swarm.read_pending(tmp_runtime, "X") is None


def test_note_awaiting_reply(tmp_runtime, tmp_path):
    cfg = load_swarm(
        tmp_path,
        "- {name: A, command: 'true', can_talk_to: [B]}\n"
        "- {name: B, command: 'true', can_talk_to: [A]}\n",
    )
    # B owes A a reply (and can message A back).
    swarm.note_awaiting_reply(cfg, "A", "B", "m-1")
    pend = swarm.read_pending(cfg, "B")
    assert pend == {"from": "A", "id": "m-1", "reminders": 0}


def test_note_awaiting_reply_skipped(tmp_runtime, tmp_path):
    cfg = load_swarm(
        tmp_path,
        "- {name: A, command: 'true', can_talk_to: [B]}\n"
        "- {name: B, command: 'true'}\n",
    )
    # sender not in cfg -> ignored.
    swarm.note_awaiting_reply(cfg, "ghost", "B", "m-1")
    assert swarm.read_pending(cfg, "B") is None


# ----------------------------------------------------- transcript extraction

TRANSCRIPT = """\
{"type":"user","isSidechain":false,"message":{"content":"one"}}
{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"text","text":"TURN1"}]}}
{"type":"user","isSidechain":false,"message":{"content":"two"}}
{"type":"assistant","isSidechain":true,"message":{"content":[{"type":"text","text":"SUBAGENT"}]}}
{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"text","text":"TURN2"}]}}
"""


def test_read_transcript_reply_latest(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(TRANSCRIPT)
    assert swarm.read_transcript_reply(str(p)) == "TURN2"


def test_read_transcript_reply_unflushed_empty(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(TRANSCRIPT + '{"type":"user","isSidechain":false,"message":{"content":"three"}}\n')
    assert swarm.read_transcript_reply(str(p)) == ""


def test_read_transcript_reply_string_content(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        '{"type":"user","message":{"content":"q"}}\n'
        '{"type":"assistant","message":{"content":"STRING REPLY"}}\n'
    )
    assert swarm.read_transcript_reply(str(p)) == "STRING REPLY"


def test_read_transcript_reply_partial_line_skipped(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        '{"type":"user","message":{"content":"q"}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"OK"}]}}\n'
        '{"type":"assistant", this is not valid json\n'
    )
    assert swarm.read_transcript_reply(str(p)) == "OK"


def test_read_transcript_reply_missing(tmp_path):
    assert swarm.read_transcript_reply(str(tmp_path / "nope.jsonl")) == ""


# ------------------------------------------------------- session persistence

def test_write_and_read_sessions(tmp_runtime):
    swarm.write_sessions(tmp_runtime, {"A": {"session_id": "s1", "type": "claude"}})
    assert swarm.read_sessions(tmp_runtime) == {"A": {"session_id": "s1", "type": "claude"}}


def test_read_sessions_missing_and_corrupt(tmp_runtime):
    assert swarm.read_sessions(tmp_runtime) == {}
    tmp_runtime.runtime.mkdir(parents=True, exist_ok=True)
    tmp_runtime.sessions_file.write_text("not: [valid json\n")
    with mock.patch.object(swarm, "warn") as w:
        assert swarm.read_sessions(tmp_runtime) == {}
        assert w.called
    tmp_runtime.sessions_file.write_text("- just a list\n")
    assert swarm.read_sessions(tmp_runtime) == {}


def test_record_session_merges(tmp_runtime):
    agent = Agent(
        name="A", type="claude", command="true", workdir=tmp_runtime.root,
        session="t-A", capture="hook", boot_delay_ms=0, first_prompt="",
    )
    swarm.record_session(tmp_runtime, agent, "sess-1", transcript="/x")
    rec = swarm.read_sessions(tmp_runtime)["A"]
    assert rec["session_id"] == "sess-1" and rec["transcript"] == "/x" and rec["type"] == "claude"
    # Same id is not rewritten (no change).
    before = tmp_runtime.sessions_file.read_text()
    swarm.record_session(tmp_runtime, agent, "sess-1", transcript="/x")
    assert tmp_runtime.sessions_file.read_text() == before
    # New id updates.
    swarm.record_session(tmp_runtime, agent, "sess-2")
    assert swarm.read_sessions(tmp_runtime)["A"]["session_id"] == "sess-2"
    # Empty id is ignored.
    swarm.record_session(tmp_runtime, agent, "")
    assert swarm.read_sessions(tmp_runtime)["A"]["session_id"] == "sess-2"


def test_record_session_no_id(tmp_runtime):
    agent = Agent(
        name="A", type="claude", command="true", workdir=tmp_runtime.root,
        session="t-A", capture="hook", boot_delay_ms=0, first_prompt="",
    )
    swarm.record_session(tmp_runtime, agent, None)
    assert "A" not in swarm.read_sessions(tmp_runtime)


# ------------------------------------------------------------- codex session

def test_codex_session_missing(tmp_runtime):
    agent = Agent(
        name="A", type="codex", command="true", workdir=tmp_runtime.root,
        session="t-A", capture="hook", boot_delay_ms=0, first_prompt="",
    )
    assert swarm.codex_session(agent) == (None, None)


def test_codex_session_from_rollout(tmp_runtime):
    agent = Agent(
        name="A", type="codex", command="true", workdir=tmp_runtime.root,
        session="t-A", capture="hook", boot_delay_ms=0, first_prompt="",
    )
    rollout = agent.workdir / ".codex" / "sessions" / "rollout-1.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {"session_id": "abc"}}))
    sid, path = swarm.codex_session(agent)
    assert sid == "abc" and path == str(rollout)


def test_codex_session_no_meta(tmp_runtime):
    agent = Agent(
        name="A", type="codex", command="true", workdir=tmp_runtime.root,
        session="t-A", capture="hook", boot_delay_ms=0, first_prompt="",
    )
    rollout = agent.workdir / ".codex" / "sessions" / "rollout-1.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(json.dumps({"type": "other"}))
    sid, path = swarm.codex_session(agent)
    assert sid is None and path == str(rollout)


def test_codex_session_bad_json(tmp_runtime):
    agent = Agent(
        name="A", type="codex", command="true", workdir=tmp_runtime.root,
        session="t-A", capture="hook", boot_delay_ms=0, first_prompt="",
    )
    rollout = agent.workdir / ".codex" / "sessions" / "rollout-1.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("not json\n")
    sid, path = swarm.codex_session(agent)
    assert sid is None and path == str(rollout)


# ------------------------------------------------------------- archive / log

def test_archive_message(tmp_runtime):
    p = swarm.archive_message(tmp_runtime, "A", "B", "hello body", "m-1", "m-0")
    assert p.exists() and "hello body" in p.read_text()
    assert "from-A" in p.name and "-m-1" in p.name
    text = p.read_text()
    assert "m-1" in text and "m-0" in text


def test_log_event(tmp_runtime):
    swarm.log_event(tmp_runtime, "A", "sent", to="B", text="hi")
    agent_log = (tmp_runtime.log_dir / "A.jsonl").read_text().strip().splitlines()
    swarm_log = (tmp_runtime.log_dir / "swarm.jsonl").read_text().strip().splitlines()
    assert len(agent_log) == 1 and len(swarm_log) == 1
    rec = json.loads(agent_log[0])
    assert rec["kind"] == "sent" and rec["to"] == "B" and rec["text"] == "hi"


# ------------------------------------------------------- claude transcript

def test_extract_claude_response_found(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        '{"type":"user","message":{"content":"q"}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"REPLY"}]}}\n'
    )
    payload = {"transcript_path": str(p)}
    assert swarm.extract_claude_response(payload) == "REPLY"


def test_extract_claude_response_missing_file(tmp_path):
    assert swarm.extract_claude_response({"transcript_path": str(tmp_path / "nope.jsonl")}) == ""


def test_extract_claude_response_polls_then_gives_up(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"type":"user","message":{"content":"q"}}\n')
    with mock.patch.object(swarm, "TRANSCRIPT_WAIT_MS", 0):
        assert swarm.extract_claude_response({"transcript_path": str(p)}) == ""


# --------------------------------------------------------------- context disc

def test_config_from_state(tmp_path, monkeypatch):
    work = tmp_path / "work" / "agentA"
    work.mkdir(parents=True)
    (tmp_path / ".swarm").mkdir()
    (tmp_path / ".swarm" / "state.json").write_text(json.dumps({"config": str(tmp_path / "swarm.yaml")}))
    monkeypatch.chdir(work)
    # Walk up to tmp_path/.swarm/state.json.
    assert swarm.config_from_state() == str(tmp_path / "swarm.yaml")


def test_config_from_state_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert swarm.config_from_state() is None


def test_agent_from_cwd(tmp_runtime, tmp_path):
    cfg = load_swarm(
        tmp_path,
        "- {name: A, command: 'true', can_talk_to: [B]}\n"
        "- {name: B, command: 'true'}\n",
    )
    import os
    with mock.patch.object(os, "getcwd", return_value=str(cfg.get("A").workdir)):
        # workdir may not exist; agent_from_cwd uses resolve() on the configured path.
        cfg.get("A").workdir.mkdir(parents=True, exist_ok=True)
        assert swarm.agent_from_cwd(cfg) == "A"


def test_discover_context_explicit(tmp_path, monkeypatch):
    cfg = load_swarm(
        tmp_path,
        "- {name: A, command: 'true', can_talk_to: [B]}\n"
        "- {name: B, command: 'true'}\n",
    )
    monkeypatch.setenv("SWARM_CONFIG", str(cfg.path))
    monkeypatch.setenv("SWARM_AGENT", "A")
    got_cfg, agent = swarm.discover_context(None, None)
    assert agent.name == "A"


def test_discover_context_not_found(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_CONFIG", raising=False)
    monkeypatch.delenv("SWARM_AGENT", raising=False)
    with mock.patch.object(swarm, "config_from_state", return_value=None):
        with pytest.raises(swarm.SwarmError):
            swarm.discover_context(None, None)


def test_discover_context_skips_bad_config(tmp_path, monkeypatch):
    bad = tmp_path / "bad.yaml"
    bad.write_text("not valid: [")
    monkeypatch.setenv("SWARM_CONFIG", str(bad))
    with mock.patch.object(swarm, "config_from_state", return_value=None):
        with pytest.raises(swarm.SwarmError):
            swarm.discover_context(None, None)


# ----------------------------------------------------------------- valid_toml

def test_valid_toml():
    assert swarm.valid_toml("a = 1\n[b]\nc = 2") is True
    assert swarm.valid_toml("a = = =") is False


def test_valid_toml_without_tomllib(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "tomllib", None)
    assert swarm.valid_toml("anything") is True
