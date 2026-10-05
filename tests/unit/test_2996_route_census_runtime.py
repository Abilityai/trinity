"""#2996 — the route census, checked against the LIVE route table.

The AST census (`test_2996_human_only_routes.py`) is import-free and keys each
route by `<definition relpath>::<qualname>`. This file imports the real app — in
a SUBPROCESS, so no module state leaks into the unit island — and checks:

* every registered non-enterprise `APIRoute` maps to a key the AST walker found
  (a registration shape the walker cannot see fails here), and
* every stored `"METHOD /full/path"` — in the baseline and in the reviewed
  tables — is exactly what the app registers for that key (ratchet rule 7), so
  an exemption cannot silently follow a function onto another path or method.

It FAILS, never skips, when the subprocess cannot import `main`, prints nothing
parseable, or finds zero routes: `test_1483_route_order.py`'s module-level skip
hid its check for months (#2080), and it lives in its own file so a failure here
cannot take the AST tests with it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _route_census as rc  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def live_paths():
    return rc.runtime_paths(rc.runtime_routes())


def _stored() -> dict:
    stored = dict(rc.load_baseline())
    for table in (rc.AGENT_CALLABLE, rc.OWN_AUTH):
        stored.update({k: v[0] for k, v in table.items()})
    stored.update({k: v[0] for k, v in rc.ADMIN_WIDENED.items()})
    stored.update({k: v[0] for k, v in rc.DELEGATED.items()})
    return stored


def test_every_live_route_is_in_the_census_and_every_stored_path_matches(live_paths):
    failures = rc.path_failures(rc.walk().routes, live_paths, _stored())
    assert not failures, "\n".join(msg for _, msg in failures)


def test_the_live_table_is_not_vacuous(live_paths):
    assert len(live_paths) > 600
    assert live_paths["routers/agent_config.py::set_agent_autonomy_status"] == [
        "PUT /api/agents/{agent_name}/autonomy"
    ]
    # add_api_route: the key is the DEFINITION file, as the AST side computes it.
    assert live_paths["routers/settings/generic.py::get_all_settings"] == [
        "GET /api/settings"
    ]


def test_a_failed_import_fails_loudly(tmp_path):
    """No `main` to import: the dump raises — it never returns an empty table
    a caller could read as 'nothing to check'."""
    with pytest.raises(RuntimeError, match="import main"):
        rc.runtime_routes(backend=tmp_path, timeout=120)
