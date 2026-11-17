"""The agent server's dashboard refusal is a named code and a fixed sentence.

`GET /api/dashboard` on the agent server parses the agent's own
`~/dashboard.yaml` through the hardened loader (#1965). When the loader refuses
the file, the `except HardenedYamlError` arm answers

    {"has_dashboard": False, "config": None, "error": f"{e.code}: {e}"}

This file pins what that `error` string is made of, so it stays a named refusal
and never grows into internals:

* every `raise` in the loader is a bare re-raise or a call to one of its
  refusal constructors (`err`, `error_cls`, `HardenedYamlError`), so the
  source scan below sees every refusal it can raise;
* the code comes from a closed set (`<kind>_too_large`, `_too_deep`,
  `_alias_not_permitted`, `_alias_budget_exceeded`, `_duplicate_key`,
  `_yaml_invalid`), read from the loader's source;
* every message the loader can raise is built only from fixed text, the
  loader's own limits, the offending key and line, and PyYAML's description of
  where the document is malformed;
* the handler returns exactly `"<code>: <loader message>"`, nothing appended:
  no traceback, no frame, no file path;
* a valid file returns its whole parsed content to the same caller, so the
  refusal text never says more than a successful read would.

The handler under test is the real one; only its fixed file location is pointed
at a temporary file, through the handler's own `__globals__`.

abilityai/trinity-enterprise#1117
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_BASE_IMAGE = _ROOT / "docker" / "base-image"
_SAFE_YAML = _BASE_IMAGE / "agent_server" / "safe_yaml.py"
if str(_BASE_IMAGE) not in sys.path:
    sys.path.insert(0, str(_BASE_IMAGE))

pytestmark = pytest.mark.unit

KIND = "workspace_yaml"
# The names the loader raises its refusals through: the parameter and local
# alias (`err = error_cls or HardenedYamlError`) and the class itself.
REFUSAL_CONSTRUCTORS = {"err", "error_cls", "HardenedYamlError"}
REFUSAL_SUFFIXES = {
    "too_large",
    "too_deep",
    "alias_not_permitted",
    "alias_budget_exceeded",
    "duplicate_key",
    "yaml_invalid",
}
# Names a loader message may interpolate: the document kind, the loader's own
# limits, the duplicated key and its line, and the PyYAML error being wrapped.
ALLOWED_MESSAGE_VALUES = {
    "kind",
    "max_bytes",
    "max_depth",
    "max_expanded_nodes",
    "key",
    "key_node.start_mark.line + 1",
    "e",
}


@pytest.fixture(scope="module")
def dashboard_module():
    # Fails (never skips) when the agent server cannot be imported: a pin that
    # quietly stops running is not a pin.
    return importlib.import_module("agent_server.routers.dashboard")


@pytest.fixture
def serve(dashboard_module, monkeypatch, tmp_path):
    handler = dashboard_module.get_dashboard
    target = tmp_path / "dashboard.yaml"
    monkeypatch.setitem(handler.__globals__, "get_dashboard_path", lambda: target)

    def run(text: str) -> dict:
        target.write_text(text)
        return asyncio.run(handler())

    run.target = target
    run.handler = handler
    return run


def _refusal_calls():
    """Every call to a refusal constructor in the loader."""
    tree = ast.parse(_SAFE_YAML.read_text())
    calls = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in REFUSAL_CONSTRUCTORS
        ):
            calls.append(node)
    return calls


def test_every_raise_in_the_loader_is_a_refusal_constructor_or_a_re_raise():
    """Closes the scan: a refusal raised any other way (a new class, a
    prebuilt instance, a wrapped call) would be invisible to the two tests
    below."""
    raises = [n for n in ast.walk(ast.parse(_SAFE_YAML.read_text())) if isinstance(n, ast.Raise)]
    assert raises, f"no raise found in {_SAFE_YAML}"
    for node in raises:
        if node.exc is None:
            continue  # bare `raise` re-raises a refusal already built
        assert (
            node.cause is None
            and isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Name)
            and node.exc.func.id in REFUSAL_CONSTRUCTORS
        ), f"line {node.lineno}: {ast.unparse(node)}"


def _code_suffix(arg: ast.expr) -> str:
    """`f"{kind}_too_deep"` -> `too_deep`; anything else is not a fixed code."""
    assert isinstance(arg, ast.JoinedStr), ast.unparse(arg)
    head, *rest = arg.values
    assert isinstance(head, ast.FormattedValue) and ast.unparse(head.value) == "kind", ast.unparse(arg)
    assert len(rest) == 1 and isinstance(rest[0], ast.Constant), ast.unparse(arg)
    text = rest[0].value
    assert text.startswith("_"), ast.unparse(arg)
    return text[1:]


def _interpolated(arg: ast.expr) -> set[str]:
    """The expressions interpolated into a message (a str or f-string literal,
    possibly split across implicitly concatenated parts)."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return set()
    assert isinstance(arg, ast.JoinedStr), f"message is not a literal: {ast.unparse(arg)}"
    out = set()
    for part in arg.values:
        if isinstance(part, ast.FormattedValue):
            assert part.format_spec is None or all(
                isinstance(v, ast.Constant) for v in part.format_spec.values
            ), ast.unparse(part)
            out.add(ast.unparse(part.value))
    return out


def test_refusal_codes_are_a_closed_set():
    calls = _refusal_calls()
    assert calls, f"no refusal raised in {_SAFE_YAML}"
    suffixes = {_code_suffix(c.args[0]) for c in calls}
    assert suffixes == REFUSAL_SUFFIXES


def test_refusal_messages_are_fixed_text_and_named_values_only():
    for call in _refusal_calls():
        assert len(call.args) == 2 and not call.keywords, ast.unparse(call)
        used = _interpolated(call.args[1])
        assert used <= ALLOWED_MESSAGE_VALUES, (ast.unparse(call), used - ALLOWED_MESSAGE_VALUES)


# Documents the handler refuses, with the code each must produce and, where the
# loader's text is fixed, the exact message.
REFUSED = {
    "alias": ("a: &x 1\nb: *x\n", "alias_not_permitted", f"YAML aliases are not permitted in {KIND}."),
    "anchor_bomb": (
        "a: &a [x,x,x,x,x,x,x,x,x]\nb: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]\nc: [*b,*b,*b,*b,*b,*b,*b,*b,*b]\n",
        "alias_not_permitted",
        f"YAML aliases are not permitted in {KIND}.",
    ),
    "too_deep": ("a: " + "[" * 400 + "]" * 400 + "\n", "too_deep", f"{KIND} nests deeper than 64 levels."),
    "too_large": (
        "title: x\n# " + "x" * (3 * 1024 * 1024) + "\n",
        "too_large",
        f"{KIND} exceeds the 262144-byte parse cap.",
    ),
    "duplicate_key": (
        "title: a\ntitle: b\n",
        "duplicate_key",
        "Duplicate key 'title' at line 2. YAML would silently keep only the last one, "
        "so the file would not mean what it appears to say.",
    ),
    "unclosed_flow": ("title: [unclosed\n", "yaml_invalid", None),
    "mapping_in_scalar": ("a: b: c\n", "yaml_invalid", None),
    "nul_byte": ("title: a\x00b\n", "yaml_invalid", None),
    "unterminated_quote": ("title: 'abc\n", "yaml_invalid", None),
}

_ABSOLUTE_PATH = re.compile(r"(?:^|[\s\"'(=])/[A-Za-z0-9_.-]+/")


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_handler_returns_exactly_the_named_refusal(serve, case):
    text, suffix, fixed_message = REFUSED[case]
    out = serve(text)
    code = f"{KIND}_{suffix}"

    # The loader's own refusal for the same document, computed independently.
    load = serve.handler.__globals__["load_hardened_yaml"]
    refusal_cls = serve.handler.__globals__["HardenedYamlError"]
    policy = serve.handler.__globals__["AliasPolicy"].REJECT
    with pytest.raises(refusal_cls) as raised:
        load(text, kind=KIND, alias_policy=policy)
    assert raised.value.code == code
    message = str(raised.value)

    assert out == {
        "has_dashboard": False,
        "config": None,
        "last_modified": None,
        "error": f"{code}: {message}",
    }
    if fixed_message is not None:
        assert message == fixed_message
    else:
        assert message.startswith("YAML parse error: "), message
        # PyYAML names the parsed text, never a file on disk.
        assert 'in "<unicode string>", ' in message, message
    error = out["error"]
    assert "Traceback" not in error and 'File "' not in error and ".py" not in error, error
    assert str(serve.target.parent) not in error and "dashboard.yaml" not in error, error
    assert not _ABSOLUTE_PATH.search(error), error


def test_valid_file_returns_its_whole_content_to_the_same_caller(serve):
    out = serve(
        "title: My board\n"
        "refresh: 15\n"
        "sections:\n"
        "  - title: s\n"
        "    widgets:\n"
        "      - type: metric\n"
        "        label: x\n"
        "        value: 1\n"
    )
    assert out["has_dashboard"] is True and out["error"] is None
    assert out["config"] == {
        "title": "My board",
        "refresh": 15,
        "sections": [{"title": "s", "widgets": [{"type": "metric", "label": "x", "value": 1}]}],
    }
