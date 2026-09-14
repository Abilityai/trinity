"""Every `sk-` key variant is redacted, not just the ones we had heard of (#2208).

Three prefix-specific patterns meant each new variant shipped unredacted until
someone noticed: the generic `sk-[a-zA-Z0-9]{20,}` stops at the first hyphen, so
an OpenAI SERVICE-ACCOUNT key (`sk-svcacct-...`) matched none of the three and
passed through logs and error bodies in clear text. Found while verifying #2208's
AC #3 ("the key never appears in argv, logs, or an error body") with a real
service-account key.

Both copies are asserted: the backend and the agent-server each carry their own
`credential_sanitizer`, and this class of gap is exactly what a one-sided fix
leaves behind.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

KEY_VARIANTS = [
    ("openai_service_account", "sk-svcacct-" + "A" * 40),   # the miss
    ("openai_project", "sk-proj-" + "B" * 30),
    ("openai_classic", "sk-" + "C" * 30),
    ("anthropic", "sk-ant-" + "D" * 30),
    ("openai_with_underscores", "sk-svcacct-" + "E" * 10 + "_" + "F" * 20),
]


def _load(path: Path, name: str):
    """Load a copy by path WITHOUT registering it in ``sys.modules``.

    Both files are named `credential_sanitizer`, so registering them would have
    the second load shadow the first for every later test in the session — the
    cross-file pollution class of #1846/#1855. Neither copy does relative
    imports, so plain `exec_module` is enough.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sanitizers():
    return {
        "backend": _load(
            _ROOT / "src/backend/utils/credential_sanitizer.py", "_cs_backend_2208"
        ),
        "agent_server": _load(
            _ROOT / "docker/base-image/agent_server/utils/credential_sanitizer.py",
            "_cs_agent_2208",
        ),
    }


@pytest.mark.parametrize("copy_name", ["backend", "agent_server"])
@pytest.mark.parametrize("label,key", KEY_VARIANTS, ids=[v[0] for v in KEY_VARIANTS])
def test_every_sk_variant_is_redacted(sanitizers, copy_name, label, key):
    text = f"codex login failed: bad key {key} (request id req_123)"
    out = sanitizers[copy_name].sanitize_text(text)
    assert key not in out, f"{copy_name} leaked a {label} key"
    assert "req_123" in out, "sanitizer over-redacted surrounding context"


@pytest.mark.parametrize("copy_name", ["backend", "agent_server"])
def test_short_sk_tokens_are_left_alone(sanitizers, copy_name):
    """The pattern needs a length floor: `sk-` is also an ordinary prefix
    (Slovak locale tags, ticket ids), and redacting those would make real logs
    unreadable."""
    text = "locale sk-SK selected; ticket sk-42 closed"
    assert sanitizers[copy_name].sanitize_text(text) == text
