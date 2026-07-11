"""A small YAML-subset parser.

Used only when PyYAML is not importable, so that Agentainer keeps working on a
bare Python 3 install. Supports the subset Agentainer configs actually need:

  * nested block mappings and block sequences
  * scalars: strings, ints, floats, booleans, null
  * single/double quoted strings
  * literal (``|``) and folded (``>``) block scalars, with ``-``/``+`` chomping
  * flow collections: ``[a, b]`` and ``{a: 1, b: 2}``
  * ``#`` comments (whole-line and trailing)

Anything fancier (anchors, aliases, tags, multi-doc, complex keys) raises
YAMLError telling the user to install PyYAML.
"""

from __future__ import annotations

import re

__all__ = ["load", "YAMLError"]


class YAMLError(Exception):
    pass


_KEY_RE = re.compile(
    r"""^(?P<key>
            "(?:[^"\\]|\\.)*"      # double quoted key
          | '(?:[^']|'')*'         # single quoted key
          | [^\s:#][^:#]*?         # plain key
        )
        \s*:(?:\s+(?P<rest>.*))?$""",
    re.VERBOSE,
)

_BLOCK_RE = re.compile(r"^([|>])([+-]?)(\d*)$")

# Only plain base-10 numbers are coerced. Leaving leading-zero ("010"), hex/octal
# ("0x1F"), bare-exponent ("1e3") and underscore ("1_000") tokens as strings keeps
# the builtin parser from silently inventing a value that PyYAML would not -- the
# ambiguous octal/exponent cases are exactly where the two would otherwise disagree.
_INT_RE = re.compile(r"^[-+]?(?:0|[1-9][0-9]*)$")
_FLOAT_RE = re.compile(r"^[-+]?(?:[0-9]*\.[0-9]+|[0-9]+\.[0-9]*)(?:[eE][-+][0-9]+)?$")

_UNSUPPORTED = ("&", "*", "!!", "---", "...")


def load(text: str):
    """Parse *text* and return the corresponding Python object."""
    lines = text.replace("\t", "    ").splitlines()
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith(("---", "...")) and stripped not in ("---", "..."):
            continue
        if stripped in ("---", "..."):
            raise YAMLError(
                "multi-document YAML is not supported by the builtin parser; "
                "install PyYAML (pip install pyyaml)"
            )
    parser = _Parser(lines)
    value = parser.parse()
    parser.expect_eof()
    return value if value is not None else {}


# --------------------------------------------------------------------------
# scalar handling
# --------------------------------------------------------------------------


def _strip_comment(s: str) -> str:
    """Remove a trailing ``# comment``, respecting quotes and brackets."""
    out = []
    quote = None
    prev = ""
    for ch in s:
        if quote:
            out.append(ch)
            if ch == quote and prev != "\\":
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and (not out or prev in " \t"):
            break
        else:
            out.append(ch)
        prev = ch
    return "".join(out).rstrip()


def _split_flow(body: str) -> list[str]:
    """Split ``a, b, [c, d]`` on top-level commas."""
    parts, depth, quote, cur = [], 0, None, []
    for ch in body:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return [p.strip() for p in parts]


def _is_balanced(s: str) -> bool:
    """True once every ``[``/``{`` opened in *s* has been closed."""
    depth, quote = 0, None
    for ch in s:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
    return depth <= 0


_DQ_ESCAPES = {
    '"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t", "r": "\r",
    "b": "\b", "f": "\f", "v": "\x0b", "0": "\x00", "a": "\x07", "e": "\x1b",
}


def _unescape_double(body: str) -> str:
    """Resolve backslash escapes in a double-quoted scalar.

    A plain ``bytes.decode("unicode_escape")`` is latin-1 based and mangles any
    non-ASCII character (``"café"`` -> ``"cafÃ©"``), so escapes are expanded by
    hand and every other character -- including multi-byte UTF-8 -- is kept as-is.
    """
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = body[i + 1]
        if nxt == "u" and i + 6 <= n:
            try:
                out.append(chr(int(body[i + 2 : i + 6], 16)))
                i += 6
                continue
            except ValueError:
                pass
        elif nxt == "x" and i + 4 <= n:
            try:
                out.append(chr(int(body[i + 2 : i + 4], 16)))
                i += 4
                continue
            except ValueError:
                pass
        out.append(_DQ_ESCAPES.get(nxt, nxt))
        i += 2
    return "".join(out)


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] == '"':
        return _unescape_double(s[1:-1])
    if len(s) >= 2 and s[0] == s[-1] == "'":
        return s[1:-1].replace("''", "'")
    return s


def _scalar(raw: str):
    s = _strip_comment(raw).strip()
    if not s:
        return None
    if s[0] in "\"'":
        return _unquote(s)
    if s.startswith("["):
        if not s.endswith("]"):
            raise YAMLError(f"unterminated flow sequence: {raw!r}")
        return [_scalar(p) for p in _split_flow(s[1:-1])]
    if s.startswith("{"):
        if not s.endswith("}"):
            raise YAMLError(f"unterminated flow mapping: {raw!r}")
        out = {}
        for part in _split_flow(s[1:-1]):
            if ":" not in part:
                raise YAMLError(f"bad flow mapping entry: {part!r}")
            k, _, v = part.partition(":")
            out[_unquote(k.strip())] = _scalar(v)
        return out
    if s.startswith(_UNSUPPORTED):
        raise YAMLError(
            f"unsupported YAML feature in {raw!r}; install PyYAML (pip install pyyaml)"
        )

    low = s.lower()
    if low in ("null", "~"):
        return None
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if _INT_RE.match(s):
        return int(s)
    if _FLOAT_RE.match(s):
        return float(s)
    return s


# --------------------------------------------------------------------------
# block parser
# --------------------------------------------------------------------------


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_blank(line: str) -> bool:
    s = line.strip()
    return not s or s.startswith("#")


class _Parser:
    def __init__(self, lines: list[str]):
        self.lines = lines
        self.i = 0

    # -- cursor helpers ----------------------------------------------------

    def peek(self):
        """Return (index, indent, stripped) of the next meaningful line."""
        while self.i < len(self.lines) and _is_blank(self.lines[self.i]):
            self.i += 1
        if self.i >= len(self.lines):
            return None
        line = self.lines[self.i]
        return self.i, _indent_of(line), line.strip()

    def expect_eof(self):
        if self.peek() is not None:
            _, indent, text = self.peek()
            raise YAMLError(f"unexpected content at indent {indent}: {text!r}")

    def finish_flow(self, first: str):
        """Parse a flow collection, consuming continuation lines until balanced.

        The cursor must already sit *after* the line ``first`` came from.
        """
        buf = _strip_comment(first)
        while not _is_balanced(buf):
            if self.i >= len(self.lines):
                raise YAMLError(f"unterminated flow collection: {first!r}")
            line = self.lines[self.i]
            self.i += 1
            if _is_blank(line):
                continue
            buf += " " + _strip_comment(line.strip())
        return _scalar(buf)

    # -- grammar -----------------------------------------------------------

    def parse(self, indent: int | None = None):
        head = self.peek()
        if head is None:
            return None
        _, cur_indent, text = head
        if indent is None:
            indent = cur_indent
        if cur_indent < indent:
            return None
        if text == "-" or text.startswith("- "):
            return self.parse_seq(cur_indent)
        # Check flow collections before the key regex: `{name: a, type: b}` would
        # otherwise look like a block mapping with the key `{name`.
        if text.startswith(("{", "[")):
            self.i += 1
            return self.finish_flow(text)
        if _KEY_RE.match(text):
            return self.parse_map(cur_indent)
        # A lone scalar (only reachable for sequence item bodies).
        self.i += 1
        return _scalar(text)

    def parse_map(self, indent: int) -> dict:
        out: dict = {}
        while True:
            head = self.peek()
            if head is None:
                break
            _, cur_indent, text = head
            if cur_indent < indent:
                break
            if cur_indent > indent:
                raise YAMLError(f"unexpected indentation before {text!r}")
            match = _KEY_RE.match(text)
            if not match:
                break
            key = _unquote(match.group("key").strip())
            rest = (match.group("rest") or "").strip()
            self.i += 1

            block = _BLOCK_RE.match(_strip_comment(rest)) if rest else None
            if block:
                out[key] = self.parse_block_scalar(indent, block)
            elif rest.startswith(("{", "[")) and not _is_balanced(_strip_comment(rest)):
                out[key] = self.finish_flow(rest)
            elif rest:
                out[key] = _scalar(rest)
            else:
                nxt = self.peek()
                if nxt and nxt[1] > indent:
                    out[key] = self.parse(nxt[1])
                elif nxt and nxt[1] == indent and nxt[2].startswith(("-", "- ")):
                    # Sequences may sit at the same indent as their key.
                    out[key] = self.parse_seq(indent)
                else:
                    out[key] = None
        return out

    def parse_seq(self, indent: int) -> list:
        out: list = []
        while True:
            head = self.peek()
            if head is None:
                break
            idx, cur_indent, text = head
            if cur_indent != indent or not (text == "-" or text.startswith("- ")):
                break

            line = self.lines[idx]
            # Rewrite "- foo" into "  foo" so the item body is a normal block
            # that starts at the column right after the dash.
            body_first = line[:indent] + " " + line[indent + 1 :]
            self.i += 1

            if not body_first.strip():
                nxt = self.peek()
                out.append(self.parse(nxt[1]) if nxt and nxt[1] > indent else None)
                continue

            body = [body_first]
            while self.i < len(self.lines):
                nxt_line = self.lines[self.i]
                if _is_blank(nxt_line):
                    body.append(nxt_line)
                    self.i += 1
                    continue
                if _indent_of(nxt_line) > indent:
                    body.append(nxt_line)
                    self.i += 1
                    continue
                break

            sub = _Parser(body)
            out.append(sub.parse())
            sub.expect_eof()
        return out

    def parse_block_scalar(self, indent: int, match: re.Match) -> str:
        style, chomp, explicit = match.group(1), match.group(2), match.group(3)

        raw: list[str] = []
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.strip() and _indent_of(line) <= indent:
                break
            raw.append(line)
            self.i += 1

        while raw and not raw[-1].strip():
            raw.pop()
        if not raw:
            return ""

        if explicit:
            block_indent = indent + int(explicit)
        else:
            block_indent = min(_indent_of(l) for l in raw if l.strip())

        body = [l[block_indent:] if len(l) > block_indent else "" for l in raw]

        if style == "|":
            text = "\n".join(body)
        else:
            folded, buf = [], []
            for line in body:
                if not line.strip():
                    folded.append(" ".join(buf))
                    buf = []
                    folded.append("")
                elif line.startswith(" "):  # more-indented lines keep their breaks
                    if buf:
                        folded.append(" ".join(buf))
                        buf = []
                    folded.append(line)
                else:
                    buf.append(line.strip())
            if buf:
                folded.append(" ".join(buf))
            text = "\n".join(folded)

        if chomp == "-":
            return text.rstrip("\n")
        if chomp == "+":
            return text + "\n"
        return text.rstrip("\n") + "\n"
