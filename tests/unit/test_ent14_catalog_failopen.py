"""THE FAIL-OPEN MATRIX, asserted at the catalog (trinity-enterprise#14).

Every failure mode is driven through the REAL parser and a REAL httpx client
over `MockTransport`, and every assertion is on the CATALOG OUTPUT — not on
whether an `except` branch ran, not on an internal call count. The property
under test is "the catalog is still served", and a test that asserts the except
branch ran would still pass if the branch returned garbage.

The structural claim being verified: `get_all_templates()` returns
`local + github`. `local` needs no network. `github` is empty by default. The
registry can only ADD to `github`, so every failure reduces `github` toward `[]`
— the already-shipping default state of the product. Nothing here can make the
catalog worse than a default install.

Sync throughout (`tests/unit/pytest.ini` overrides `asyncio_mode = auto`).
"""
import httpx
import pytest

import services.template_service as ts
import services.template_registry_service as trs

URL = "https://registry.example.com/registry.yaml"

#: A distinctive stand-in for the bundled floor. Using a sentinel rather than the
#: real bundled set makes "the floor survived" an exact equality rather than a
#: length check — and `test_the_real_bundled_floor_is_not_empty` proves the
#: sentinel is not papering over an empty reality.
FLOOR = [
    {"id": "local:sage", "display_name": "Sage", "source": "local"},
    {"id": "local:scout", "display_name": "Scout", "source": "local"},
]

GOOD_DOC = """
version: 1
templates:
  - repo: acme/one
    display_name: Curated One
    description: From the registry.
    priority: 5
"""


class _Settings:
    def __init__(self):
        self.url = URL
        self.enabled = True
        self.generation = 1
        self.lkg = None
        self.github_templates = None      # None = no admin override

    def get_template_registry_url(self):
        return self.url

    def is_template_registry_enabled(self):
        return self.enabled

    def get_template_registry_generation(self):
        return self.generation

    def get_template_registry_lkg(self):
        return self.lkg

    def set_template_registry_lkg(self, payload):
        self.lkg = payload

    def get_github_templates(self):
        return self.github_templates


@pytest.fixture
def env(monkeypatch):
    trs.invalidate_registry_cache()
    ts._metadata_cache.clear()

    settings = _Settings()
    import services.settings_service as ss
    monkeypatch.setattr(ss, "settings_service", settings, raising=True)
    monkeypatch.setattr(ss, "get_github_templates", settings.get_github_templates)
    monkeypatch.setattr(
        "utils.url_validation.validate_template_registry_url", lambda u: u
    )

    # The bundled floor, pinned.
    monkeypatch.setattr(ts, "get_local_templates", lambda: [dict(t) for t in FLOOR])
    # No default repo list — the state since #1931, and the branch the registry fills.
    monkeypatch.setattr(ts, "DEFAULT_GITHUB_TEMPLATE_REPOS", [])
    # Never dial GitHub for per-repo metadata in these tests; that hop has its
    # own coverage and would otherwise make every case network-bound.
    monkeypatch.setattr(ts, "_get_github_pat", lambda: "")
    monkeypatch.setattr(
        ts, "_fetch_template_yaml_logged", lambda repo, pat, ref=None: ({}, None)
    )

    state = {"handler": lambda r: httpx.Response(200, text=GOOD_DOC), "requests": []}

    def _client(timeout):
        def _dispatch(request):
            state["requests"].append(request)
            return state["handler"](request)

        return httpx.Client(
            timeout=timeout, follow_redirects=False,
            transport=httpx.MockTransport(_dispatch),
        )

    monkeypatch.setattr(trs, "_http_client", _client)

    class Env:
        settings = None
        def serve(self, handler):
            state["handler"] = handler
        def body(self, text):
            state["handler"] = lambda r: httpx.Response(200, text=text)
        @property
        def requests(self):
            return state["requests"]

    e = Env()
    e.settings = settings
    yield e
    trs.invalidate_registry_cache()
    ts._metadata_cache.clear()


def _ids(catalog):
    return [t["id"] for t in catalog]


def _floor_only(catalog):
    """The catalog is exactly the bundled floor — no registry contribution."""
    return [{k: t[k] for k in ("id", "display_name", "source")} for t in catalog] == FLOOR


# ---------------------------------------------------------------------------
# Baseline: the sentinel is honest, and the happy path adds
# ---------------------------------------------------------------------------

def test_the_real_bundled_floor_is_not_empty():
    """Guards the sentinel above: if the shipped catalog were empty, every
    'degrades to the floor' assertion below would be vacuously true."""
    real = ts.get_local_templates()
    assert len(real) > 0
    assert all(t["id"].startswith("local:") for t in real)


def test_a_working_registry_ADDS_to_the_floor(env):
    catalog = ts.get_all_templates()
    assert _ids(catalog) == ["local:sage", "local:scout", "github:acme/one"]
    entry = catalog[-1]
    assert entry["display_name"] == "Curated One"
    assert entry["description"] == "From the registry."
    assert entry["priority"] == 5
    assert entry["source"] == "github"
    assert entry["github_repo"] == "acme/one"


# ---------------------------------------------------------------------------
# THE MATRIX — every failure mode, asserted on the catalog
# ---------------------------------------------------------------------------

def _unreachable(request):
    raise httpx.ConnectError("no route to host", request=request)


def _timeout(request):
    raise httpx.ReadTimeout("too slow", request=request)


def _huge(request):
    return httpx.Response(200, text="#" * (trs.REGISTRY_MAX_BYTES + 4096))


def _lying_length(request):
    huge = b"#" * (trs.REGISTRY_MAX_BYTES + 4096)
    return httpx.Response(
        200,
        headers=[("content-length", "9")],
        content=iter([huge[i:i + 8192] for i in range(0, len(huge), 8192)]),
    )


ALIAS_BOMB = "\n".join(
    ["version: 1", "a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    + [
        f"a{n}: &a{n} [{', '.join([f'*a{n - 1}'] * 10)}]"
        for n in range(1, 7)
    ]
    + ["templates: []"]
)


FAILURE_MODES = {
    "unreachable":         _unreachable,
    "read_timeout":        _timeout,
    "http_404":            lambda r: httpx.Response(404, text="Not Found"),
    "http_403":            lambda r: httpx.Response(403, text="Forbidden"),
    "http_500":            lambda r: httpx.Response(500, text="Server Error"),
    "http_503":            lambda r: httpx.Response(503, text="Unavailable"),
    "redirect":            lambda r: httpx.Response(302, headers={"location": "http://169.254.169.254/"}),
    "oversize_body":       _huge,
    "lying_content_length": _lying_length,
    "malformed_yaml":      lambda r: httpx.Response(200, text="version: 1\ntemplates: [\n"),
    "alias_bomb":          lambda r: httpx.Response(200, text=ALIAS_BOMB),
    "duplicate_keys":      lambda r: httpx.Response(200, text="templates: [{repo: a/b}]\ntemplates: [{repo: c/d}]\n"),
    "html_captive_portal": lambda r: httpx.Response(200, text="<html><body>Login</body></html>"),
    "binary_body":         lambda r: httpx.Response(200, content=b"\x89PNG\r\n\x1a\n\xff\xfe"),
    "top_level_list":      lambda r: httpx.Response(200, text="- a\n- b\n"),
    "future_version":      lambda r: httpx.Response(200, text="version: 99\ntemplates: [{repo: a/b}]\n"),
    "templates_not_list":  lambda r: httpx.Response(200, text="version: 1\ntemplates: nope\n"),
    "empty_body":          lambda r: httpx.Response(200, text=""),
    "empty_registry":      lambda r: httpx.Response(200, text="version: 1\ntemplates: []\n"),
}


@pytest.mark.parametrize("mode", sorted(FAILURE_MODES))
def test_every_failure_mode_degrades_to_the_bundled_floor(env, mode):
    env.serve(FAILURE_MODES[mode])
    catalog = ts.get_all_templates()
    assert _floor_only(catalog), f"{mode} did not degrade cleanly: {_ids(catalog)}"


@pytest.mark.parametrize("mode", sorted(FAILURE_MODES))
def test_every_failure_mode_still_serves_a_200(env, mode):
    """`GET /api/templates` is `get_all_templates()` + a sort. The sort is where a
    non-int `priority` used to 500 the whole endpoint, so exercise it too."""
    env.serve(FAILURE_MODES[mode])
    catalog = ts.get_all_templates()
    catalog.sort(key=lambda t: (t.get("priority", 100), t.get("display_name", "")))
    assert _floor_only(catalog)


def test_a_registry_service_that_RAISES_is_still_fenced(env, monkeypatch):
    """Fail-open must not depend on the registry service being bug-free."""
    def boom():
        raise RuntimeError("registry service exploded")

    monkeypatch.setattr(trs, "get_registry_overrides", boom)
    assert _floor_only(ts.get_all_templates())


def test_one_bad_entry_costs_itself_not_the_catalog(env, monkeypatch):
    """#1835 / ent#89's per-template fence extends to registry entries."""
    env.body(
        "version: 1\n"
        "templates:\n"
        "  - repo: bad/one\n"
        "  - repo: good/two\n    display_name: Good\n"
    )
    real_build = ts._build_template

    def explode(repo, metadata, admin_override=None, metadata_error=None):
        if repo == "bad/one":
            raise ValueError("this template blows up in the builder")
        return real_build(repo, metadata, admin_override, metadata_error)

    monkeypatch.setattr(ts, "_build_template", explode)
    assert _ids(ts.get_all_templates()) == ["local:sage", "local:scout", "github:good/two"]


# ---------------------------------------------------------------------------
# Precedence — the ladder, and that a curated install never dials out
# ---------------------------------------------------------------------------

def test_an_admin_override_wins_AND_makes_zero_registry_requests(env):
    """TMPL-001's contract survives byte-for-byte, and a curated install pays
    nothing for a feature it is not using."""
    env.settings.github_templates = [
        {"github_repo": "admin/curated", "display_name": "Admin Pick", "description": "Mine"}
    ]
    catalog = ts.get_all_templates()
    assert _ids(catalog) == ["local:sage", "local:scout", "github:admin/curated"]
    assert catalog[-1]["display_name"] == "Admin Pick"
    assert env.requests == []


def test_an_EMPTY_admin_override_still_suppresses_the_registry(env):
    """`[]` means "I want no GitHub templates" — not "fall through to defaults"."""
    env.settings.github_templates = []
    assert _floor_only(ts.get_all_templates())
    assert env.requests == []


def test_the_registry_wins_over_the_bundled_default_list(env, monkeypatch):
    monkeypatch.setattr(ts, "DEFAULT_GITHUB_TEMPLATE_REPOS", ["bundled/fallback"])
    assert _ids(ts.get_all_templates()) == ["local:sage", "local:scout", "github:acme/one"]


def test_a_failed_registry_falls_through_to_the_bundled_default_list(env, monkeypatch):
    monkeypatch.setattr(ts, "DEFAULT_GITHUB_TEMPLATE_REPOS", ["bundled/fallback"])
    env.serve(_unreachable)
    assert _ids(ts.get_all_templates()) == [
        "local:sage", "local:scout", "github:bundled/fallback",
    ]


def test_the_disable_toggle_makes_zero_requests(env):
    env.settings.enabled = False
    assert _floor_only(ts.get_all_templates())
    assert env.requests == []


def test_the_config_hard_switch_beats_the_db_row(monkeypatch, env):
    """`TEMPLATE_REGISTRY_ENABLED=false` is the air-gap answer: no settings row
    may turn it back on. Resolved through the real accessor, because the whole
    point is that this must NOT be `_resolve_bool_flag` (whose env leg is
    opt-in only, so `default=True` would swallow the `false`)."""
    import services.settings_service as ss
    import config as cfg

    real = ss.SettingsService()
    monkeypatch.setattr(real, "get_setting", lambda key, default=None: "true")
    monkeypatch.setattr(cfg, "TEMPLATE_REGISTRY_ENABLED", False)
    assert real.is_template_registry_enabled() is False

    monkeypatch.setattr(cfg, "TEMPLATE_REGISTRY_ENABLED", True)
    assert real.is_template_registry_enabled() is True


# ---------------------------------------------------------------------------
# F1 — list, detail and CREATE resolve through one ladder
# ---------------------------------------------------------------------------

def test_list_detail_and_create_share_one_precedence_ladder(env):
    """`get_github_template()` is the second resolver, and it feeds
    `GET /api/templates/{id}` AND `crud._resolve_github_repo_and_pat`. Its
    dynamic fallthrough passed NO override dict, so a registry-sourced template
    listed as "Curated One" and resolved by id as "one".
    learnings.md 2026-07-10: the create path is never one call site."""
    listed = next(t for t in ts.get_all_templates() if t["id"] == "github:acme/one")
    detail = ts.get_github_template("github:acme/one")     # GET /api/templates/{id}
    created = ts.get_github_template("github:acme/one")    # the creation path

    for surface in (detail, created):
        assert surface["display_name"] == listed["display_name"] == "Curated One"
        assert surface["description"] == listed["description"]
        assert surface["priority"] == listed["priority"] == 5
        assert surface["github_repo"] == "acme/one"


def test_the_detail_path_keeps_registry_fields_when_github_metadata_is_missing(env, monkeypatch):
    """The concrete payoff of reusing the admin_override shape: a rate-limited
    per-repo fetch blanks the derived chips, not the card."""
    monkeypatch.setattr(
        ts, "_fetch_template_yaml_logged",
        lambda repo, pat, ref=None: ({}, "HTTP 403"),
    )
    ts._metadata_cache.clear()
    detail = ts.get_github_template("github:acme/one")
    assert detail["display_name"] == "Curated One"
    assert detail["description"] == "From the registry."
    assert detail["metadata_unavailable"] is True


def test_an_admin_override_still_wins_on_the_detail_path(env):
    env.settings.github_templates = [
        {"github_repo": "admin/curated", "display_name": "Admin Pick", "description": "Mine"}
    ]
    detail = ts.get_github_template("github:admin/curated")
    assert detail["display_name"] == "Admin Pick"
    assert env.requests == []


def test_an_unlisted_repo_still_resolves_dynamically(env):
    """The OSS escape hatch. `github:owner/repo` must work whether or not the
    registry lists it — that is the acceptance criterion, not a nicety."""
    detail = ts.get_github_template("github:someone/unlisted")
    assert detail is not None
    assert detail["id"] == "github:someone/unlisted"
    assert detail["github_repo"] == "someone/unlisted"


# ---------------------------------------------------------------------------
# Payload shape — what buys "no MCP change" and "no Library.vue change"
# ---------------------------------------------------------------------------

def test_a_registry_entry_and_an_admin_entry_have_an_IDENTICAL_key_set(env):
    from_registry = next(
        t for t in ts.get_all_templates() if t["id"] == "github:acme/one"
    )
    env.settings.github_templates = [
        {"github_repo": "admin/curated", "display_name": "A", "description": "B"}
    ]
    trs.invalidate_registry_cache()
    from_admin = next(
        t for t in ts.get_all_templates() if t["id"] == "github:admin/curated"
    )
    assert set(from_registry) == set(from_admin)
    assert from_registry["source"] == from_admin["source"] == "github"


def test_a_registry_entry_carries_no_provenance_marker(env):
    """Registry provenance lives on the settings surface, not the catalog.

    Asserted on the KEY SET, not on the values: a registry-supplied
    `description` is free to say the word "registry", and a value scan would
    make this test fail on its own fixture data rather than on a regression.
    """
    entry = next(t for t in ts.get_all_templates() if t["id"] == "github:acme/one")
    assert not [k for k in entry if "registry" in k.lower()]
    assert entry["source"] == "github"


def test_the_id_always_matches_the_repo_that_would_be_cloned(env):
    """A card can never advertise one path and clone another — both are computed
    by `_build_template` from the same `repo` argument."""
    env.body(
        "version: 1\ntemplates:\n"
        "  - repo: acme/one\n    display_name: Something Trustworthy\n"
    )
    entry = next(t for t in ts.get_all_templates() if t["source"] == "github")
    assert entry["id"] == f"github:{entry['github_repo']}"


# ---------------------------------------------------------------------------
# A hostile registry cannot assert fields it does not own
# ---------------------------------------------------------------------------

def test_a_hostile_registry_cannot_inject_fork_to_own_or_credentials(env):
    env.body(
        """
        version: 1
        templates:
          - repo: acme/one
            display_name: Innocent
            fork_to_own: required
            credentials: {env_file: [STOLEN_TOKEN]}
            data_paths: ['/etc']
            persistent_state: ['/etc/shadow']
            schedules: [{name: exfil, cron: '* * * * *', message: leak}]
            resources: {cpu: '64', memory: '512g'}
            skills: [evil]
            hidden: true
            id: 'local:sage'
        """
    )
    entry = next(t for t in ts.get_all_templates() if t["source"] == "github")
    assert entry["id"] == "github:acme/one"        # not the injected local: id
    assert entry["fork_to_own"] is None
    assert entry["required_credentials"] == []
    assert entry["data_paths"] == []
    assert entry["schedules"] == []
    assert entry["skills"] == []
    assert entry["resources"] == {"cpu": "2", "memory": "4g"}
    assert entry["display_name"] == "Innocent"     # display IS in the allowlist


def test_ordering_is_curatable_but_admin_entries_are_unaffected(env):
    """`priority` is the one dimension of 'curate freely — feature, ORDER, add,
    deprecate' that previously had no mechanism."""
    env.body(
        "version: 1\ntemplates:\n"
        "  - repo: acme/late\n    priority: 90\n"
        "  - repo: acme/early\n    priority: 1\n"
    )
    catalog = ts.get_all_templates()
    catalog.sort(key=lambda t: (t.get("priority", 100), t.get("display_name", "")))
    github = [t["id"] for t in catalog if t["source"] == "github"]
    assert github == ["github:acme/early", "github:acme/late"]

    # An admin entry has no `priority` field at all, so it must resolve exactly
    # as it did before this change: the template.yaml value, else the default.
    env.settings.github_templates = [{"github_repo": "admin/x", "display_name": "X", "description": ""}]
    trs.invalidate_registry_cache()
    admin_entry = next(t for t in ts.get_all_templates() if t["id"] == "github:admin/x")
    assert admin_entry["priority"] == ts._DEFAULT_TEMPLATE_PRIORITY
