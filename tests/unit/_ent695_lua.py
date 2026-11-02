"""Execute the production lease scripts' Lua TEXT in unit tests (trinity-enterprise#695).

The suite's fakeredis has no Lua (`tests/requirements-test.txt`), and the
earlier emulators re-implemented the compare-and-set in Python while ignoring
the script body: a mutated script (the token compare dropped, `==` flipped,
two ARGV indices swapped) stayed green. This evaluator runs the script text
itself, for the small Lua subset those scripts use, and REFUSES anything
outside it, so a script cannot drift into a construct the tests silently
skip:

    if <expr> then <block> [else <block>] end
    return <expr>
    <expr> := redis.call('<cmd>', <expr>, ...) | ARGV[n] | KEYS[n]
              | <integer> | '<string>' | <expr> == <expr> | <expr> ~= <expr>

Redis semantics that matter here: a missing key's GET is false (never equal
to a token), and every argument reaches the script as a string. Not a test
module (no `test_` prefix).
"""
from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"\s*(redis\.call|ARGV|KEYS|~=|==|\d+|'[^']*'|[\[\](),])")


class UnsupportedLua(ValueError):
    """The script uses a construct this evaluator does not implement."""


def _expr(text: str) -> str:
    """One Lua expression -> the Python expression with the same meaning."""
    out, pos = [], 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise UnsupportedLua(f"unsupported Lua in expression: {text[pos:]!r}")
        tok = m.group(1)
        out.append({"redis.call": "_call", "~=": "!="}.get(tok, tok))
        pos = m.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return " ".join(out)


def transpile(src: str) -> str:
    lines, depth = [], 1
    for raw in src.strip().splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("if ") and line.endswith(" then"):
            lines.append("    " * depth + f"if {_expr(line[3:-5])}:")
            depth += 1
        elif line == "else":
            lines.append("    " * (depth - 1) + "else:")
        elif line == "end":
            depth -= 1
            if depth < 1:
                raise UnsupportedLua("unbalanced 'end'")
        elif line.startswith("return "):
            lines.append("    " * depth + f"return {_expr(line[7:])}")
        else:
            raise UnsupportedLua(f"unsupported Lua statement: {line!r}")
    if depth != 1:
        raise UnsupportedLua("unterminated block")
    return "def _script(_call, KEYS, ARGV):\n" + "\n".join(lines) + "\n    return None\n"


def run(src: str, store: dict, ttls: dict, keys=None, args=None):
    """Run `src` against a plain key/value `store` (+ `ttls`), Redis-style."""
    def _call(cmd, *a):
        cmd = cmd.lower()
        if cmd == "get":
            return store.get(a[0], False)
        if cmd == "expire":
            if a[0] not in store:
                return 0
            ttls[a[0]] = int(a[1])
            return 1
        if cmd == "del":
            ttls.pop(a[0], None)
            return 1 if store.pop(a[0], None) is not None else 0
        raise UnsupportedLua(f"unsupported redis.call command: {cmd!r}")

    namespace: dict = {}
    exec(transpile(src), {"__builtins__": {}}, namespace)  # noqa: S102 — our own test transpiler
    as_strings = [None, *(str(v) for v in (args or []))]
    return namespace["_script"](_call, [None, *(keys or [])], as_strings)
