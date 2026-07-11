"""Exhaustive tests for the bundled fallback YAML parser (lib/minyaml.py)."""

import pytest

import minyaml
from minyaml import YAMLError, load


# ------------------------------------------------------------------------- load

def test_empty_document_is_empty_mapping():
    assert load("") == {}
    assert load("   \n  # comment\n") == {}


def test_multidoc_raises():
    with pytest.raises(YAMLError):
        load("a: 1\n---\nb: 2")
    with pytest.raises(YAMLError):
        load("a: 1\n...\n")
    # A bare "---"/"..." line is always rejected (multi-doc unsupported).
    with pytest.raises(YAMLError):
        load("---\n  a: 1")


# ----------------------------------------------------------------------- scalars

def test_scalar_ints():
    assert load("a: 0") == {"a": 0}
    assert load("a: 42") == {"a": 42}
    assert load("a: -7") == {"a": -7}
    # Leading-zero and hex stay strings (so we never disagree with PyYAML).
    assert load("a: 010") == {"a": "010"}
    assert load("a: 0x1F") == {"a": "0x1F"}


def test_scalar_floats():
    assert load("a: 1.5") == {"a": 1.5}
    assert load("a: -0.25") == {"a": -0.25}
    assert load("a: 3.") == {"a": 3.0}
    assert load("a: .5") == {"a": 0.5}
    assert load("a: 1.0e+3") == {"a": 1000.0}
    assert load("a: 2.5e-1") == {"a": 0.25}
    # minyaml requires a sign in the exponent, so these stay strings.
    assert load("a: 1e3") == {"a": "1e3"}
    assert load("a: 2E-1") == {"a": "2E-1"}


def test_scalar_bools_and_null():
    assert load("a: true\nb: false") == {"a": True, "b": False}
    assert load("a: yes\nb: no\nc: on\nd: off") == {
        "a": True, "b": False, "c": True, "d": False
    }
    assert load("a: null\nb: ~\nc:") == {"a": None, "b": None, "c": None}


def test_scalar_strings_and_quotes():
    assert load("a: hello") == {"a": "hello"}
    assert load('a: "hi there"') == {"a": "hi there"}
    assert load("a: 'hi there'") == {"a": "hi there"}
    assert load('a: "tab\\tnl\\nend"') == {"a": "tab\tnl\nend"}
    assert load("a: 'it''s ok'") == {"a": "it's ok"}
    assert load('a: "caf\\u00e9"') == {"a": "café"}
    assert load('a: "\\x41"') == {"a": "A"}
    assert load('a: "\\u0000"') == {"a": "\x00"}


def test_scalar_double_escape_fallbacks():
    # Unknown escape and bad hex/x just pass the character through.
    assert load(r'a: "\q"') == {"a": "q"}
    assert load(r'a: "\uXYZW"') == {"a": "uXYZW"}
    assert load(r'a: "\xZZ"') == {"a": "xZZ"}


def test_scalar_flow_collections():
    assert load("a: [1, 2, 3]") == {"a": [1, 2, 3]}
    assert load('a: [x, "y z", 2.5]') == {"a": ["x", "y z", 2.5]}
    assert load("a: {b: 1, c: two}") == {"a": {"b": 1, "c": "two"}}
    assert load("a: {}") == {"a": {}}
    assert load("a: []") == {"a": []}


def test_scalar_flow_errors():
    with pytest.raises(YAMLError):
        load("a: [1, 2")
    with pytest.raises(YAMLError):
        load("a: {b: 1, c}")
    with pytest.raises(YAMLError):
        load("a: {no colon here}")


def test_scalar_unsupported_features():
    for bad in ("a: &anchor b", "a: *ref", "a: !!str x"):
        with pytest.raises(YAMLError):
            load(bad)


# --------------------------------------------------------------- comment logic

def test_comment_stripping_respects_quotes():
    assert minyaml._strip_comment('a: b # c') == "a: b"
    assert minyaml._strip_comment('a: "b # c"') == 'a: "b # c"'
    assert minyaml._strip_comment("a: 'b # c'") == "a: 'b # c'"
    assert minyaml._strip_comment("# whole line") == ""
    assert minyaml._strip_comment("a: b") == "a: b"


def test_split_flow_nested_and_quoted():
    assert minyaml._split_flow("a, b, c") == ["a", "b", "c"]
    assert minyaml._split_flow("a, [b, c], d") == ["a", "[b, c]", "d"]
    assert minyaml._split_flow("a, 'b, c', d") == ["a", "'b, c'", "d"]
    assert minyaml._split_flow("") == []


def test_is_balanced():
    assert minyaml._is_balanced("{[()]}")
    assert minyaml._is_balanced("plain")
    assert not minyaml._is_balanced("{")
    assert not minyaml._is_balanced("[")


def test_unescape_double_unit():
    fn = minyaml._unescape_double
    assert fn("a\\nb") == "a\nb"
    assert fn("\\t") == "\t"
    assert fn("plain") == "plain"
    # Trailing backslash with nothing after is kept.
    assert fn("\\") == "\\"
    # \v is a recognised named escape -> vertical tab.
    assert fn("\\v") == "\x0b"


# ----------------------------------------------------------- block structures

def test_nested_map_and_seq():
    text = """
    a:
      b: 1
      c:
        - x
        - y
    d: [1, 2]
    """
    assert load(text) == {"a": {"b": 1, "c": ["x", "y"]}, "d": [1, 2]}


def test_map_with_sequence_at_same_indent():
    text = """
    tasks:
    - one
    - two
    """
    assert load(text) == {"tasks": ["one", "two"]}


def test_sequence_of_maps():
    text = """
    - name: a
      type: x
    - name: b
      type: y
    """
    assert load(text) == [
        {"name": "a", "type": "x"},
        {"name": "b", "type": "y"},
    ]


def test_sequence_with_multiline_item_errors():
    # minyaml does not fold multi-line plain scalars inside a sequence item.
    with pytest.raises(YAMLError):
        load("- line1\n  line2\n- other")


def test_empty_sequence_item():
    text = """
    - a
    -
    - c
    """
    assert load(text) == ["a", None, "c"]


def test_key_with_flow_value_on_next_line():
    text = """
    mapping:
      a: 1
      b: 2
    list: [x, y]
    """
    assert load(text) == {"mapping": {"a": 1, "b": 2}, "list": ["x", "y"]}


def test_indentation_errors():
    with pytest.raises(YAMLError):
        load("a:\n  b: 1\n   c: 2")  # over-indented sibling
    with pytest.raises(YAMLError):
        load("  a: 1\nb: 2")  # less than current on first key


def test_trailing_content_raises():
    with pytest.raises(YAMLError):
        load("a: 1\nb: 2\n  leftover: 3")


# ------------------------------------------------------------- block scalars

def test_literal_block_scalar():
    text = "a: |\n  line1\n  line2\n"
    assert load(text) == {"a": "line1\nline2\n"}


def test_folded_block_scalar():
    text = "a: >\n  line1\n  line2\n"
    assert load(text) == {"a": "line1 line2\n"}


def test_block_chomp_strip():
    assert load("a: |-\n  x\n\n") == {"a": "x"}
    assert load("a: >-\n  x\n  y\n") == {"a": "x y"}


def test_block_chomp_keep():
    assert load("a: |+\n  x\n") == {"a": "x\n"}
    assert load("a: |+\n  x\n\n\n") == {"a": "x\n"}


def test_block_explicit_indent():
    text = "a: |2\n    indented by four\n"
    assert load(text) == {"a": "  indented by four\n"}


def test_block_empty():
    assert load("a: |\n") == {"a": ""}
    assert load("a: |-\n   \n") == {"a": ""}


def test_folded_blank_separates_paragraphs():
    text = "a: >\n  para one\n  para two\n\n  para three\n"
    assert load(text) == {"a": "para one para two\n\npara three\n"}


def test_folded_deeper_line_flushes_buffer():
    text = "a: >\n  para one\n    deep line\n"
    assert load(text) == {"a": "para one\n  deep line\n"}


# --------------------------------------------------------- parser edge cases

def test_top_level_flow_scalar():
    assert load("[1, 2, 3]") == [1, 2, 3]
    assert load("{a: 1}") == {"a": 1}


def test_lone_scalar_value():
    assert load("just a string") == "just a string"
    assert load("123") == 123


def test_key_regex_variants():
    assert load('"quoted key": 1') == {"quoted key": 1}
    assert load("'q k': 2") == {"q k": 2}
    assert load("plain: 3") == {"plain": 3}


def test_unterminated_flow_raises():
    p = minyaml._Parser(["a: [1, 2"])
    with pytest.raises(YAMLError):
        p.parse()


def test_expect_eof_raises_on_trailing():
    p = minyaml._Parser(["a: 1", "hello"])
    p.parse()
    with pytest.raises(YAMLError):
        p.expect_eof()


# --------------------------------------------------------------- parser parity

def test_parity_with_pyyaml_on_shipped_configs():
    import yaml
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    configs = sorted((repo / "examples").glob("*.yaml")) + [repo / "agents.example.yaml"]
    bad = []
    for cfg in configs:
        text = cfg.read_text()
        try:
            min_parsed = minyaml.load(text)
        except minyaml.YAMLError as exc:
            # minyaml intentionally does not support multi-document YAML (a
            # documented limitation, not a bug); such files are still valid
            # and load fine via PyYAML at runtime. Skip them here.
            if "multi-document" in str(exc):
                continue
            bad.append(f"{cfg.name}:{exc!r}")
            continue
        if yaml.safe_load(text) != min_parsed:
            bad.append(cfg.name)
    assert not bad, f"parser mismatch: {bad}"


# ----------------------------------------------------- defensive / deep branches

def test_doc_separator_prefix_is_ignored():
    # "---foo" starts with "---" but is not the separator -> treated as a key.
    assert load("---foo: 1") == {"---foo": 1}


def test_flow_value_with_empty_scalar():
    assert load("a: {b:}") == {"a": {"b": None}}


def test_scalar_direct_unterminated_flow():
    with pytest.raises(YAMLError):
        minyaml._scalar("[1, 2")
    with pytest.raises(YAMLError):
        minyaml._scalar("{a: 1")


def test_multiline_flow_with_blank_line():
    assert load("a: [1,\n\n  2]") == {"a": [1, 2]}
    assert load("a: {b: 1,\n\n  c: 2}") == {"a": {"b": 1, "c": 2}}


def test_parse_with_larger_indent_returns_none():
    p = minyaml._Parser(["a: 1"])
    assert p.parse(indent=10) is None

