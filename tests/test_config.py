"""Tests for lib/config.py: loading, validation, and the dataclasses."""

import pytest

import config
from config import ConfigError, SwarmConfig, load
from tests.conftest import load_config


# ------------------------------------------------------------------ success

def test_minimal_valid_config(tmp_path):
    cfg = load_config(
        "swarm: {name: t, root: ./ws, session_prefix: 't-'}\n"
        "defaults: {type: claude}\n"
        "agents:\n  - {name: A, command: 'true'}\n",
        tmp_path,
    )
    assert cfg.name == "t"
    assert len(cfg.agents) == 1
    assert cfg.agents[0].name == "A"
    assert cfg.agents[0].capture == "hook"  # claude defaults to hook
    assert cfg.agents[0].type == "claude"


def test_defaults_apply_to_agents(tmp_path):
    cfg = load_config(
        "swarm: {root: ./ws}\n"
        "defaults: {type: claude, boot_delay_ms: 123}\n"
        "agents:\n"
        "  - {name: A, command: 'true', can_talk_to: [B]}\n"
        "  - {name: B, command: 'true'}\n",
        tmp_path,
    )
    a = cfg.get("A")
    assert a.boot_delay_ms == 123
    assert a.can_talk_to == ["B"]


def test_capture_auto_resolves_from_type(tmp_path):
    cfg = load_config(
        "swarm: {root: ./ws}\n"
        "defaults: {type: gemini}\n"  # gemini -> capture pane
        "agents:\n  - {name: A, command: 'true'}\n",
        tmp_path,
    )
    assert cfg.get("A").capture == "pane"


def test_explicit_capture_values(tmp_path):
    cfg = load_config(
        "swarm: {root: ./ws}\n"
        "agents:\n"
        "  - {name: A, command: 'true', capture: none}\n"
        "  - {name: B, command: 'true', capture: pane}\n"
        "  - {name: C, command: 'true', capture: hook}\n",
        tmp_path,
    )
    assert [a.capture for a in cfg.agents] == ["none", "pane", "hook"]


def test_custom_agent_type(tmp_path):
    cfg = load_config(
        "swarm: {root: ./ws}\n"
        "agent_types:\n"
        "  bot: {command: 'echo hi', capture: pane, boot_delay_ms: 11}\n"
        "agents:\n  - {name: A, type: bot}\n",
        tmp_path,
    )
    a = cfg.get("A")
    assert a.type == "bot"
    assert a.command == "echo hi"
    assert a.boot_delay_ms == 11


def test_forward_responses_and_wildcards(tmp_path):
    cfg = load_config(
        "swarm: {root: ./ws}\n"
        "defaults: {type: claude}\n"
        "agents:\n"
        "  - {name: A, command: 'true', can_talk_to: '*', forward_responses_to: '*'}\n"
        "  - {name: B, command: 'true'}\n"
        "  - {name: C, command: 'true'}\n",
        tmp_path,
    )
    a = cfg.get("A")
    assert set(a.can_talk_to) == {"B", "C"}
    assert set(a.forward_responses_to) == {"B", "C"}


def test_first_prompt_and_env_merge(tmp_path):
    cfg = load_config(
        "swarm: {root: ./ws}\n"
        "defaults: {type: claude, env: {A: '1'}}\n"
        "agent_types:\n"
        "  claude: {env: {B: '2'}}\n"
        "agents:\n"
        "  - {name: X, command: 'true', first_prompt: 'hello', env: {C: '3'}}\n",
        tmp_path,
    )
    x = cfg.get("X")
    assert x.first_prompt.startswith("hello")
    assert x.env == {"A": "1", "B": "2", "C": "3"}


def test_first_prompt_file(tmp_path):
    prompt = tmp_path / "p.txt"
    prompt.write_text("file prompt body")
    cfg = load_config(
        "swarm: {root: ./ws}\n"
        "defaults: {type: claude}\n"
        f"agents:\n  - {{name: X, command: 'true', first_prompt_file: '{prompt}'}}\n",
        tmp_path,
    )
    assert "file prompt body" in cfg.get("X").first_prompt


def test_first_prompt_file_relative(tmp_path):
    (tmp_path / "p.txt").write_text("relative prompt")
    cfg = load_config(
        "swarm: {root: ./ws}\n"
        "defaults: {type: claude}\n"
        "agents:\n  - {name: X, command: 'true', first_prompt_file: p.txt}\n",
        tmp_path,
    )
    assert "relative prompt" in cfg.get("X").first_prompt


def test_workdir_placeholder_and_explicit(tmp_path):
    cfg = load_config(
        "swarm: {root: ./ws, name: t}\n"
        "defaults: {type: claude, workdir: '{root}/agents/{name}'}\n"
        "agents:\n  - {name: X, command: 'true'}\n",
        tmp_path,
    )
    assert cfg.get("X").workdir == (tmp_path / "ws" / "agents" / "X")


def test_plain_message_format(tmp_path):
    cfg = load_config(
        "swarm: {root: ./ws, message_format: plain}\n"
        "defaults: {type: claude}\n"
        "agents:\n  - {name: A, command: 'true'}\n",
        tmp_path,
    )
    assert cfg.message_format == "plain"
    # parse_outbound_tags is forced off for plain messages.
    assert cfg.get("A").parse_outbound_tags is False


def test_reply_reminder_off_when_tags_off(tmp_path):
    cfg = load_config(
        "swarm: {root: ./ws, message_format: plain}\n"
        "defaults: {type: claude, reply_reminder: true}\n"
        "agents:\n  - {name: A, command: 'true'}\n",
        tmp_path,
    )
    assert cfg.get("A").reply_reminder is False


def test_swarmconfig_properties(tmp_path):
    cfg = load_config(
        "swarm: {root: ./ws}\n"
        "defaults: {type: claude}\n"
        "agents:\n  - {name: A, command: 'true'}\n",
        tmp_path,
    )
    assert cfg.runtime == cfg.root / ".swarm"
    assert cfg.log_dir == cfg.runtime / "logs"
    assert cfg.inbox_dir == cfg.runtime / "inbox"
    assert cfg.run_dir == cfg.runtime / "run"
    assert cfg.bin_dir == cfg.runtime / "bin"
    assert cfg.sessions_file == cfg.runtime / "sessions.yaml"
    assert cfg.get("A").name == "A"
    assert cfg.names() == ["A"]


def test_get_unknown_agent_raises(tmp_path):
    cfg = load_config(
        "swarm: {root: ./ws}\n"
        "defaults: {type: claude}\n"
        "agents:\n  - {name: A, command: 'true'}\n",
        tmp_path,
    )
    with pytest.raises(ConfigError):
        cfg.get("Z")


def test_shared_workdir_warns(tmp_path):
    shared = tmp_path / "ws" / "shared"
    shared.mkdir(parents=True)
    cfg = load_config(
        "swarm: {root: ./ws}\n"
        "defaults: {type: claude}\n"
        "agents:\n"
        "  - {name: A, command: 'true', workdir: ./shared}\n"
        "  - {name: B, command: 'true', workdir: ./shared}\n",
        tmp_path,
    )
    assert any("share the working directory" in w for w in cfg.warnings)


# ------------------------------------------------------------------- errors

def test_missing_file_raises():
    with pytest.raises(ConfigError):
        load("/nonexistent/path/to/swarm.yaml")


def test_parse_error_raises(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("agents: [unclosed\n")  # invalid YAML
    with pytest.raises(ConfigError):
        load(path)


def test_top_level_not_mapping(tmp_path):
    with pytest.raises(ConfigError):
        load_config("- just\n- a\n- list\n", tmp_path)


def test_swarm_not_mapping(tmp_path):
    with pytest.raises(ConfigError):
        load_config("swarm: notamap\nagents: []\n", tmp_path)


def test_defaults_not_mapping(tmp_path):
    with pytest.raises(ConfigError):
        load_config("defaults: 5\nagents: []\n", tmp_path)


def test_agent_types_not_mapping(tmp_path):
    with pytest.raises(ConfigError):
        load_config("agent_types: {t: 'notmap'}\nagents: []\n", tmp_path)


def test_no_agents_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config("swarm: {root: ./ws}\nagents: []\n", tmp_path)


def test_agents_not_list(tmp_path):
    with pytest.raises(ConfigError):
        load_config("swarm: {root: ./ws}\nagents: {name: A}\n", tmp_path)


def test_agent_not_mapping(tmp_path):
    with pytest.raises(ConfigError):
        load_config("swarm: {root: ./ws}\nagents:\n  - 'notamap'\n", tmp_path)


def test_agent_missing_name(tmp_path):
    with pytest.raises(ConfigError):
        load_config("swarm: {root: ./ws}\nagents:\n  - {type: claude}\n", tmp_path)


def test_agent_bad_name(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            "swarm: {root: ./ws}\nagents:\n  - {name: 'a b', command: 'true'}\n", tmp_path
        )


def test_agent_duplicate_name(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            "swarm: {root: ./ws}\n"
            "defaults: {type: claude}\n"
            "agents:\n  - {name: A, command: 'true'}\n  - {name: A, command: 'true'}\n",
            tmp_path,
        )


def test_agent_unknown_type(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            "swarm: {root: ./ws}\nagents:\n  - {name: A, type: nope, command: 'true'}\n",
            tmp_path,
        )


def test_agent_no_command(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            "swarm: {root: ./ws}\n"
            "agent_types:\n"
            "  x: {capture: pane}\n"
            "agents:\n  - {name: A, type: x}\n",
            tmp_path,
        )


def test_agent_bad_capture(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            "swarm: {root: ./ws}\nagents:\n  - {name: A, command: 'true', capture: weird}\n",
            tmp_path,
        )


def test_bad_message_format(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            "swarm: {root: ./ws, message_format: sideways}\nagents: []\n", tmp_path
        )


def test_can_talk_to_unknown_peer(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            "swarm: {root: ./ws}\n"
            "defaults: {type: claude}\n"
            "agents:\n  - {name: A, command: 'true', can_talk_to: [ghost]}\n",
            tmp_path,
        )


def test_can_talk_to_self(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            "swarm: {root: ./ws}\n"
            "defaults: {type: claude}\n"
            "agents:\n  - {name: A, command: 'true', can_talk_to: [A]}\n",
            tmp_path,
        )


def test_forward_unknown_peer(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            "swarm: {root: ./ws}\n"
            "defaults: {type: claude}\n"
            "agents:\n"
            "  - {name: A, command: 'true', can_talk_to: [B], forward_responses_to: [ghost]}\n"
            "  - {name: B, command: 'true'}\n",
            tmp_path,
        )


def test_forward_not_subset_of_can_talk_to(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            "swarm: {root: ./ws}\n"
            "defaults: {type: claude}\n"
            "agents:\n"
            "  - {name: A, command: 'true', can_talk_to: [B], forward_responses_to: [C]}\n"
            "  - {name: B, command: 'true'}\n"
            "  - {name: C, command: 'true'}\n",
            tmp_path,
        )


def test_forward_with_capture_none(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            "swarm: {root: ./ws}\n"
            "defaults: {type: claude}\n"
            "agents:\n"
            "  - {name: A, command: 'true', capture: none, can_talk_to: [B], forward_responses_to: [B]}\n"
            "  - {name: B, command: 'true'}\n",
            tmp_path,
        )


def test_first_prompt_both_set_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            "swarm: {root: ./ws}\n"
            "defaults: {type: claude}\n"
            "agents:\n"
            "  - {name: A, command: 'true', first_prompt: hi, first_prompt_file: x}\n",
            tmp_path,
        )


def test_first_prompt_file_missing(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            "swarm: {root: ./ws}\n"
            "defaults: {type: claude}\n"
            "agents:\n  - {name: A, command: 'true', first_prompt_file: /nope/x}\n",
            tmp_path,
        )


def test_workdir_placeholder_unknown(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            "swarm: {root: ./ws}\n"
            "defaults: {type: claude}\n"
            "agents:\n  - {name: A, command: 'true', workdir: '{nope}/x'}\n",
            tmp_path,
        )


def test_workdir_not_a_directory(tmp_path):
    f = tmp_path / "ws" / "afile"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    with pytest.raises(ConfigError):
        load_config(
            f"swarm: {{root: {tmp_path!r}}}\n"
            "defaults: {type: claude}\n"
            f"agents:\n  - {{name: A, command: 'true', workdir: {str(f)!r}}}\n",
            tmp_path,
        )


def test_workdir_missing_and_not_created(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            "swarm: {root: ./ws, create_workdirs: false}\n"
            "defaults: {type: claude}\n"
            "agents:\n  - {name: A, command: 'true', workdir: ./missing}\n",
            tmp_path,
        )


def test_bad_template_placeholder_comms(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            "swarm: {root: ./ws}\n"
            "templates: {comms: 'hi {nonsense}'}\n"
            "defaults: {type: claude}\n"
            "agents:\n  - {name: A, command: 'true'}\n",
            tmp_path,
        )


def test_bad_template_placeholder_task_notice(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            "swarm: {root: ./ws}\n"
            "templates: {task_notice: 'hi {nonsense}'}\n"
            "defaults: {type: claude}\n"
            "agents:\n"
            "  - {name: A, command: 'true', in_first_prompt_append_your_task_will_be_sent_in_the_next_prompt: true}\n",
            tmp_path,
        )


def test_custom_reply_templates(tmp_path):
    cfg = load_config(
        "swarm: {root: ./ws}\n"
        "templates:\n"
        "  reply_reminder: 'remind {agent}'\n"
        "  send_failed: 'failed {agent}'\n"
        "defaults: {type: claude}\n"
        "agents:\n  - {name: A, command: 'true'}\n",
        tmp_path,
    )
    assert cfg.reply_reminder_template == "remind {agent}"
    assert cfg.send_failed_template == "failed {agent}"


# --------------------------------------------------------------- helpers

def test_as_list():
    assert config._as_list(None, "x") == []
    assert config._as_list("a", "x") == ["a"]
    assert config._as_list(["a", 1], "x") == ["a", "1"]
    with pytest.raises(ConfigError):
        config._as_list({"a": 1}, "x")
    with pytest.raises(ConfigError):
        config._as_list(5, "x")


def test_as_bool():
    assert config._as_bool(None, True, "x") is True
    assert config._as_bool(True, False, "x") is True
    assert config._as_bool(False, True, "x") is False
    with pytest.raises(ConfigError):
        config._as_bool("yes", True, "x")


def test_as_str_map():
    assert config._as_str_map(None, "x") == {}
    assert config._as_str_map({"a": 1}, "x") == {"a": "1"}
    with pytest.raises(ConfigError):
        config._as_str_map([1], "x")


def test_parse_yaml_uses_installed_parser():
    assert config.parse_yaml("a: 1\n") == {"a": 1}
