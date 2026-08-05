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

WHICH READ DECIDES (ent#14 S2). The first cut of this gate read the CATALOG's
`metadata_unavailable` — computed from `_get_cached_metadata_result`: the global
platform PAT, off the default branch, through a 600s cache — while the correctly
credentialed creation-path read had already happened one call earlier. That was
wrong in both directions at once, so the tests below drive the gate from
`source_metadata`/`source_metadata_reason` (the creation read) and pin the
catalog dict as the SECONDARY source it now is.

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


def _apply(gh_template, config, *, source_metadata=None, source_reason=None):
    """Drive the real async gate from a sync test.

    `source_metadata`/`source_reason` are the CREATION-path read — what
    `_resolve_template` passes in after reading `template.yaml` with the PAT
    that will actually clone. `gh_template` is the catalog dict, now secondary.
    Defaulting the creation read to "clean, declares nothing" keeps each test
    stating only the input it is about.
    """
    return asyncio.run(
        crud._apply_fork_to_own(
            config=config,
            current_user=_user(),
            gh_template=gh_template,
            github_repo_for_agent="acme/second-brain",
            github_pat_for_agent="pat",
            github_pat_tier="global",
            url_branch=None,
            source_metadata={} if source_metadata is None else source_metadata,
            source_metadata_reason=source_reason,
        )
    )


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------

def test_unreadable_metadata_REFUSES_creation():
    template = ts._build_template("acme/second-brain", {}, None, "HTTP 403")
    assert template["fork_to_own"] is None       # the value that used to sail through

    with pytest.raises(HTTPException) as exc:
        _apply(template, _config(), source_reason="HTTP 403")
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
        _apply(template, _config(), source_reason=reason)
    assert exc.value.detail["code"] == "TEMPLATE_METADATA_UNAVAILABLE"


def test_the_refusal_is_retryable_and_names_the_remedy():
    """503, not 400: this is a transient upstream condition, and the message has
    to tell the operator that a platform PAT raises 60/hr to 5000/hr."""
    template = ts._build_template("acme/second-brain", {}, None, "HTTP 403")
    with pytest.raises(HTTPException) as exc:
        _apply(template, _config(), source_reason="HTTP 403")
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
    repo, pat, tier, upstream = _apply(
        template, _config(template="github:someone/plain-repo"), source_reason="HTTP 404")
    assert (repo, pat, tier, upstream) == ("acme/second-brain", "pat", "global", None)


def test_a_successful_read_with_no_fork_declaration_creates():
    template = ts._build_template("acme/ordinary", {"display_name": "Ordinary"}, None, None)
    assert template["metadata_unavailable"] is False
    assert _apply(template, _config())[0] == "acme/second-brain"


def test_a_successful_read_of_a_required_template_still_refuses_without_a_fork_block():
    """The original ent#93 gate, unchanged."""
    template = ts._build_template("acme/second-brain", {"fork_to_own": "required"}, None, None)
    with pytest.raises(HTTPException) as exc:
        _apply(template, _config(),
               source_metadata={"fork_to_own": "required"}, source_reason=None)
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
                source_metadata={}, source_metadata_reason="HTTP 403",
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


# ---------------------------------------------------------------------------
# WHICH READ DECIDES (ent#14 S2)
#
# Driven through the real `_resolve_template`, not the gate in isolation: the
# defect was not in the gate's logic but in WHICH dict was handed to it, and a
# test that passes its own inputs to `_apply_fork_to_own` cannot see that. The
# fake GitHub below is keyed on the PAT, which is the entire point — the catalog
# reads with the global platform token, creation reads with the creator's.
# ---------------------------------------------------------------------------

PLATFORM_PAT = "platform-pat"
CREATOR_PAT = "creator-pat"


@pytest.fixture
def github(monkeypatch):
    """A GitHub whose answer depends on WHICH token asks."""
    state = {"answers": {}, "calls": []}

    def _fetch(repo, pat, ref=None):
        state["calls"].append({"repo": repo, "pat": pat, "ref": ref})
        return state["answers"].get(pat or "", ({}, "HTTP 404"))

    monkeypatch.setattr(ts, "_fetch_template_yaml_result", _fetch)
    monkeypatch.setattr(ts, "_get_github_pat", lambda: PLATFORM_PAT)
    ts._metadata_cache.clear()

    async def _ok(*a, **k):
        return None

    async def _instance(*a, **k):
        return None, None

    monkeypatch.setattr(crud, "_validate_github_access", _ok)
    monkeypatch.setattr(crud, "_reserve_git_instance", _instance)

    class GitHub:
        calls = state["calls"]

        def answers(self, **by_pat):
            """`answers(**{PLATFORM_PAT: ({}, "HTTP 404"), ...})`"""
            state["answers"].update(by_pat)

        def catalog_for(self, repo="acme/second-brain"):
            """The catalog dict, built the way `GET /api/templates` builds it —
            global platform PAT, default branch, cached."""
            return ts.get_github_template("github:" + repo)

        def create(self, repo="acme/second-brain", pat=CREATOR_PAT, branch=None):
            """Run the real github branch of `_resolve_template`.

            `branch` goes into the TEMPLATE STRING, because that is where
            `_parse_github_ref` reads it from — `config.source_branch` is an
            output of that parse, not an input to it.
            """
            template = "github:" + repo + ("@" + branch if branch else "")
            catalog = self.catalog_for(repo)
            monkeypatch.setattr(
                crud, "_resolve_github_repo_and_pat",
                lambda *a, **k: (catalog, repo, pat, "per_user"),
            )
            config = types.SimpleNamespace(
                template=template, fork_to_own=None,
                source_branch=None, source_mode=True,
            )
            return catalog, asyncio.run(crud._resolve_template(config, _user()))

    yield GitHub()
    ts._metadata_cache.clear()


def test_a_PRIVATE_required_template_the_platform_pat_cannot_see_still_refuses(github):
    """The false PASS, and the reason the availability half alone was not a fix.

    GitHub answers 404 — not 403 — for a repo a token cannot see, so the catalog
    classifies a private template as ABSENT: `metadata_unavailable` is False and
    `fork_to_own` is None. Both of the old gate's inputs said "nothing to
    enforce" while the creator's own token could read `required` perfectly well.
    """
    github.answers(**{
        PLATFORM_PAT: ({}, "HTTP 404"),
        CREATOR_PAT: ({"fork_to_own": "required"}, None),
    })

    catalog = github.catalog_for()
    assert catalog["metadata_unavailable"] is False, "404 is absence, by design"
    assert catalog["fork_to_own"] is None, "the catalog cannot see the declaration"


def test_the_private_required_template_refuses_end_to_end(github):
    github.answers(**{
        PLATFORM_PAT: ({}, "HTTP 404"),
        CREATOR_PAT: ({"fork_to_own": "required"}, None),
    })
    with pytest.raises(HTTPException) as exc:
        github.create()
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "FORK_TO_OWN_REQUIRED"


def test_a_poisoned_catalog_cache_does_not_block_a_creator_who_can_read(github):
    """The false REFUSE. A shared 403 (anonymous rate limit, another user's
    listing) used to 503 every non-forking `github:` create for the full 600s
    TTL — including the plain `github:owner/repo` escape hatch — even though
    this creator's own token reads the template fine."""
    github.answers(**{
        PLATFORM_PAT: ({}, "HTTP 403"),
        CREATOR_PAT: ({"display_name": "Ordinary"}, None),
    })

    catalog, tr = github.create(repo="acme/ordinary")
    assert catalog["metadata_unavailable"] is True, "the catalog IS poisoned"
    assert tr.github_repo_for_agent == "acme/ordinary", "creation proceeded"


def test_an_outage_on_the_CREATION_read_still_refuses(github):
    """The fail-closed property itself, unchanged — only its input moved."""
    github.answers(**{
        PLATFORM_PAT: ({"display_name": "Fine"}, None),   # catalog looks healthy
        CREATOR_PAT: ({}, "HTTP 403"),                     # the read that matters
    })
    with pytest.raises(HTTPException) as exc:
        github.create()
    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "TEMPLATE_METADATA_UNAVAILABLE"


def test_the_gate_costs_no_extra_github_calls(github):
    """The creation read is the one ent#89 already made for `schedules:`. If
    this ever grows a second fetch, the gate has been re-wired to a source of
    its own and the two can disagree again."""
    github.answers(**{
        PLATFORM_PAT: ({}, "HTTP 404"),
        CREATOR_PAT: ({"schedules": []}, None),
    })
    github.create()
    creation_reads = [c for c in github.calls if c["pat"] == CREATOR_PAT]
    assert len(creation_reads) == 1, creation_reads


def test_the_creation_read_is_pinned_to_the_requested_ref(github):
    """`@branch` reads that branch, not the default one — the ent#89 contract,
    now load-bearing for a security decision as well."""
    github.answers(**{
        PLATFORM_PAT: ({}, "HTTP 404"),
        CREATOR_PAT: ({"fork_to_own": "optional"}, None),
    })
    github.create(branch="feature-x")
    assert [c["ref"] for c in github.calls if c["pat"] == CREATOR_PAT] == ["feature-x"]


def test_required_declared_by_EITHER_read_refuses(github):
    """The union. The creation read is the better one, but taking it ALONE would
    let a repo whose default branch declares `required` be created from a branch
    that drops the line. This fix may only ever remove false passes."""
    github.answers(**{
        PLATFORM_PAT: ({"fork_to_own": "required"}, None),  # catalog says required
        CREATOR_PAT: ({}, None),                             # this ref does not
    })
    with pytest.raises(HTTPException) as exc:
        github.create(branch="feature-x")
    assert exc.value.detail["code"] == "FORK_TO_OWN_REQUIRED"
