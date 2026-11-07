"""#4331: name patterns must not accept a trailing newline.

`re.match(r"...$", s)` lets `$` match just before a final "\\n", so a quoted
YAML system name or member key such as "w\\n" passed manifest validation and
reached the agent name. The patterns anchor with `\\Z` instead.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytest.importorskip("docker", reason="backend venv required (system_service chain)")

from services import system_service  # noqa: E402
from services import template_service  # noqa: E402
from services.agent_service import crud  # noqa: E402

pytestmark = pytest.mark.unit


def _validate(doc: str):
    manifest = system_service.parse_manifest(doc)
    return system_service.validate_manifest(manifest)


def test_member_key_with_trailing_newline_is_refused():
    with pytest.raises(ValueError):
        _validate('name: demo\nagents:\n  "w\\n":\n    template: local:x\n')


def test_system_name_with_trailing_newline_is_refused():
    with pytest.raises(ValueError):
        _validate('name: "ab\\n"\nagents:\n  w:\n    template: local:x\n')


def test_plain_names_still_accepted():
    _validate("name: demo\nagents:\n  w:\n    template: local:x\n")


@pytest.mark.parametrize(
    "regex",
    [crud._LOCAL_TEMPLATE_NAME_RE, template_service._LOCAL_TEMPLATE_NAME_RE],
)
def test_local_template_name_regex_refuses_trailing_newline(regex):
    assert regex.match("tpl")
    assert not regex.match("tpl\n")
