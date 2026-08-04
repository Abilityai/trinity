"""`fork_to_own: required` must fail CLOSED on unreadable metadata (ent#14 F2).

The bug, every link source-verified:

    _fetch_template_yaml_result()  -> ({}, "HTTP 403") on a GitHub rate limit
    _get_cached_metadata_result()  -> caches the {} for a full 600s TTL
    _build_template()              -> "fork_to_own": None
    _apply_fork_to_own()           -> `None == "required"` is False
    => the gate never fires, and the agent is created bound to the SHARED
       UPSTREAM TEMPLATE REPO instead of a user-owned copy.

`fork_to_own: required` exists precisely to prevent that, and the failure is
silent: the user's knowledge base ends up in the wrong place with no error. It
is the ent#162 class ("a private KB could reach the shared public upstream")
reached without any attacker.

Pre-existing — but the remote registry converts it from unreachable to expected:
per-repo fetches return on a DEFAULT install, the registry ships default-on, and
`workers x windows/hr x entries` exceeds GitHub's 60/hr ANONYMOUS limit above ~5
listed repos while the curated fleet very likely includes the fork_to_own
template.

Sync throughout (`tests/unit/pytest.ini` overrides `asyncio_mode = auto`; a bare
`async def test_*` here is collected and never awaited, so the async call under
test is driven with an explicit `asyncio.run`).
"""
import asyncio
import types

import pytest
from fastapi import HTTPException

import services.agent_service.crud as crud
import services.template_service as ts


def _config(**over):
    base = dict(template="github:acme/second-brain", fork_to_own=None, source_branch=None)
    base.update(over)
    return types.SimpleNamespace(**base)


def _user():
    return types.SimpleNamespace(id=1, username="owner", email="o@example.com")


def _apply(gh_template, config):
    """Drive the real async gate from a sync test."""
    return asyncio.run(
        crud._apply_fork_to_own(
            config=config,
            current_user=_user(),
            gh_template=gh_template,
            github_repo_for_agent="acme/second-brain",
            github_pat_for_agent="pat",
            github_pat_tier="global",
            url_branch=None,
        )
    )


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------

def test_unreadable_metadata_REFUSES_creation():
    template = ts._build_template("acme/second-brain", {}, None, "HTTP 403")
    assert template["fork_to_own"] is None       # the value that used to sail through
    assert template["metadata_unavailable"] is True

    with pytest.raises(HTTPException) as exc:
        _apply(template, _config())
    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "TEMPLATE_METADATA_UNAVAILABLE"


@pytest.mark.parametrize(
    "reason",
    [
        "HTTP 403",                                  # the rate limit
        "HTTP 401",
        "HTTP 500",
        "HTTP 502",
        "ConnectError: no route to host",
        "ReadTimeout: timed out",
        "template_alias_not_permitted: refused",     # an ent#314 parse refusal
        "template_duplicate_key: refused",
    ],
)
def test_every_unreadable_reason_refuses(reason):
    template = ts._build_template("acme/second-brain", {}, None, reason)
    with pytest.raises(HTTPException) as exc:
        _apply(template, _config())
    assert exc.value.detail["code"] == "TEMPLATE_METADATA_UNAVAILABLE"


def test_the_refusal_is_retryable_and_names_the_remedy():
    """503, not 400: this is a transient upstream condition, and the message has
    to tell the operator that a platform PAT raises 60/hr to 5000/hr."""
    template = ts._build_template("acme/second-brain", {}, None, "HTTP 403")
    with pytest.raises(HTTPException) as exc:
        _apply(template, _config())
    assert exc.value.status_code == 503
    assert "retry" in exc.value.detail["error"].lower()
    assert "token" in exc.value.detail["error"].lower()


# ---------------------------------------------------------------------------
# A clean 404 is ABSENCE, not unreadability
# ---------------------------------------------------------------------------

def test_a_404_still_creates_because_the_repo_declares_nothing():
    """Most repos ship no template.yaml at all. If 404 were treated as unknown,
    every dynamic `github:owner/repo` creation would break."""
    template = ts._build_template("someone/plain-repo", {}, None, "HTTP 404")
    assert template["metadata_unavailable"] is False
    repo, pat, tier, upstream = _apply(template, _config(template="github:someone/plain-repo"))
    assert (repo, pat, tier, upstream) == ("acme/second-brain", "pat", "global", None)


def test_a_successful_read_with_no_fork_declaration_creates():
    template = ts._build_template("acme/ordinary", {"display_name": "Ordinary"}, None, None)
    assert template["metadata_unavailable"] is False
    assert _apply(template, _config())[0] == "acme/second-brain"


def test_a_successful_read_of_a_required_template_still_refuses_without_a_fork_block():
    """The original ent#93 gate, unchanged."""
    template = ts._build_template("acme/second-brain", {"fork_to_own": "required"}, None, None)
    with pytest.raises(HTTPException) as exc:
        _apply(template, _config())
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "FORK_TO_OWN_REQUIRED"


# ---------------------------------------------------------------------------
# Scope of the refusal
# ---------------------------------------------------------------------------

def test_a_caller_who_IS_forking_is_not_blocked_by_an_outage():
    """The unsafe branch is the non-forking one. A caller who supplies a
    destination ends up with a user-owned repo whatever the template declares,
    so a GitHub outage must not block them — it would be friction with no
    safety benefit."""
    template = ts._build_template("acme/second-brain", {}, None, "HTTP 403")
    fork_block = types.SimpleNamespace(
        destination_repo="user/mine",
        github_pat=types.SimpleNamespace(get_secret_value=lambda: "user-pat"),
        private=True,
    )
    config = _config(fork_to_own=fork_block)

    # Stop before the network: the assertion is that the 503 gate did NOT fire.
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            crud._apply_fork_to_own(
                config=config, current_user=_user(), gh_template=template,
                github_repo_for_agent="acme/second-brain", github_pat_for_agent="pat",
                github_pat_tier="global", url_branch="feature-branch",
            )
        )
    assert exc.value.detail["code"] == "FORK_BRANCH_UNSUPPORTED"


# ---------------------------------------------------------------------------
# The classifier itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "reason,unreadable",
    [
        (None, False),
        ("", False),
        ("HTTP 404", False),
        ("HTTP 403", True),
        ("HTTP 429", True),
        ("HTTP 500", True),
        ("HTTP 404 something else", False),
        ("ConnectError: boom", True),
        ("template_too_large: refused", True),
    ],
)
def test_metadata_reason_classifier(reason, unreadable):
    assert ts.metadata_reason_is_unreadable(reason) is unreadable


def test_the_reason_survives_the_metadata_cache_for_the_whole_stale_window(monkeypatch):
    """The adjacent defect (`{}` overwriting last-good, tracked separately) makes
    the empty metadata stick for a full TTL. Caching the REASON alongside it is
    what keeps the gate correct for that entire window rather than for one
    request."""
    ts._metadata_cache.clear()
    calls = []

    def fake(repo, pat, ref=None):
        calls.append(repo)
        return {}, "HTTP 403"

    monkeypatch.setattr(ts, "_fetch_template_yaml_logged", fake)
    monkeypatch.setattr(ts, "_get_github_pat", lambda: "")

    first = ts._get_cached_metadata_result("acme/second-brain")
    second = ts._get_cached_metadata_result("acme/second-brain")
    assert first == second == ({}, "HTTP 403")
    assert len(calls) == 1, "the second read came from the cache, as designed"
    assert ts.metadata_reason_is_unreadable(second[1]) is True
    ts._metadata_cache.clear()
