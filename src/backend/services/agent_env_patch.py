"""
Patch an agent's whole ``.env`` text: set or remove named keys, keep every
other byte (trinity-enterprise#966, plan T4).

The agent writes credentials through ``execution_env.format_env_line`` and
reads them back last-wins through ``parse_env_file``. The backend is a SECOND
writer of that file (``github_pat_propagation_service._format_pat_line`` was
the first), so the quoting rule here is the #2023 rule byte-for-byte: escape
the escape character first, then the quote, wrap in double quotes, refuse a
newline. Parity is held by ``tests/unit/test_ent966_agent_env_patch.py`` over
the shared corpus against both the PAT writer and the real reader; D3 folds
the PAT writer onto this module later.

Two differences from the PAT patcher, both deliberate:

* a key present on several lines is **collapsed** to the first one rather than
  levelled (#2016) — the reader is last-wins and every line is rewritten
  anyway, so one line is the honest result;
* ``remove_keys`` exists — a revoked Nevermined mandate must leave no
  ``NVM_DELEGATION_ID`` behind.

Unknown lines (comments, blanks, other keys, their indentation and their line
endings) are preserved byte for byte. Nothing here is logged: the text is a
credential file.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Mapping

__all__ = ["format_env_line", "set_keys", "remove_keys", "values_of"]

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: What counts as "a line for KEY" — the same shape the PAT patcher matches
#: (leading blanks, then ``KEY=``), so both writers agree on the file.
_LINE_KEY_RE = re.compile(r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)=")


def _check_key(key: str) -> str:
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise ValueError(f"invalid .env key {key!r}: must match [A-Za-z_][A-Za-z0-9_]*")
    return key


def format_env_line(key: str, value: str) -> str:
    """``KEY="value"`` exactly as the agent's own writer emits it (#2023)."""
    _check_key(key)
    # The agent reader splits with str.splitlines(), which also breaks on
    # \x0b \x0c \x1c-\x1e \x85 \u2028 \u2029 — refuse every one, anywhere.
    if value.splitlines() not in ([], [value]):
        raise ValueError(
            f"value for {key!r} contains a newline or carriage return; "
            ".env is line-oriented and such a value would define an extra key"
        )
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"'


def _line_key(line: str):
    match = _LINE_KEY_RE.match(line)
    return match.group(1) if match else None


def _ending(line: str) -> str:
    return line[len(line.rstrip("\r\n")):]


def set_keys(env_text: str, values: Mapping[str, str]) -> str:
    """Return ``env_text`` with every key in ``values`` set.

    A key already present is rewritten in place (its line ending kept; later
    duplicates dropped); an absent key is appended, in ``values`` order, after
    a newline separator if the file lacks one. Validation happens before any
    line is touched, so a bad key or value changes nothing.
    """
    lines_for: Dict[str, str] = {key: format_env_line(key, value) for key, value in values.items()}

    out = []
    seen = set()
    for line in env_text.splitlines(keepends=True):
        key = _line_key(line)
        if key in lines_for:
            if key in seen:
                continue  # collapse the duplicate; the reader is last-wins
            seen.add(key)
            out.append(lines_for[key] + _ending(line))
        else:
            out.append(line)

    result = "".join(out)
    for key, line in lines_for.items():
        if key in seen:
            continue
        if result and not result.endswith(("\n", "\r")):
            result += "\n"
        result += line + "\n"
    return result


def remove_keys(env_text: str, keys: Iterable[str]) -> str:
    """Return ``env_text`` without any line that sets one of ``keys``."""
    targets = {_check_key(k) for k in keys}
    return "".join(
        line for line in env_text.splitlines(keepends=True)
        if _line_key(line) not in targets
    )


def _unquote(value: str) -> str:
    """Mirror of the agent reader's ``execution_env.unquote_env_value`` (#2023):
    one matched quote pair is stripped; inside a DOUBLE-quoted value only, one
    scan reverses the writer's escapes (``\\"`` → ``"``, ``\\\\`` → ``\\``); a
    single-quoted value is taken verbatim; anything else is returned as is."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        inner = value[1:-1]
        if value[0] != '"':
            return inner
        out = []
        i = 0
        while i < len(inner):
            ch = inner[i]
            if ch == "\\" and i + 1 < len(inner) and inner[i + 1] in ('"', "\\"):
                out.append(inner[i + 1])
                i += 2
            else:
                out.append(ch)
                i += 1
        return "".join(out)
    return value


def values_of(env_text: str, key: str) -> List[str]:
    """Every value the file sets for ``key``, in file order, each exactly as the
    agent's own reader (``execution_env.parse_env_file``) would see it — so a
    caller comparing one knows what the agent would actually send (ent#966 R9).

    Line matching mirrors the READER, not ``_LINE_KEY_RE``: the line is
    stripped, a comment / blank / ``=``-less line is skipped, the key is the
    stripped text before the first ``=`` (so ``KEY = v`` counts) and the value
    is the stripped remainder, unquoted as above. That is a superset of the
    lines ``set_keys`` rewrites — the right direction for a refusal: a hand
    shape this module would not rewrite is still one the agent reads.
    """
    _check_key(key)
    found: List[str] = []
    for raw in env_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() != key:
            continue
        found.append(_unquote(value.strip()))
    return found
