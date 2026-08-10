"""The registry parser — pure, tolerant, network-free (trinity-enterprise#14).

Two contracts under test, and they are deliberately different:

  * a structurally wrong DOCUMENT tells us nothing trustworthy, so nothing from
    it is used (`ok=False`, the catalog degrades to its bundled floor);
  * one malformed ENTRY costs only itself.

That split is the ent#128 / ent#89 tolerant-reader shape. The third contract is
the allowlist: a registry entry may contribute exactly four fields, and every
key it does NOT own has a creation-path consequence, so "unknown keys are
ignored" is asserted rather than assumed.

NOTE ON ASYNC: `tests/unit/pytest.ini` overrides `pyproject.toml`, so
`asyncio_mode = auto` does NOT apply in this directory — a bare `async def
test_*` is collected and silently never awaited. Everything here is sync
because everything under test is sync.
"""
import pytest

from services.template_registry_service import (
    MAX_DESCRIPTION_LEN,
    MAX_DISPLAY_NAME_LEN,
    MAX_REGISTRY_TEMPLATES,
    MAX_REPORTED_ERRORS,
    ERROR_BAD_SHAPE,
    ERROR_PARSE_REFUSED,
    ERROR_TOO_LARGE,
    ERROR_UNSUPPORTED_VERSION,
    parse_registry_document,
)


def _repos(parsed):
    return [e.repo for e in parsed.entries]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_full_entry_round_trips():
    parsed = parse_registry_document(
        """
        version: 1
        templates:
          - repo: Abilityai/cornelius
            display_name: Cornelius
            description: Your second brain.
            priority: 20
        """
    )
    assert parsed.ok
    assert parsed.error_code is None
    assert parsed.errors == ()
    (entry,) = parsed.entries
    assert entry.repo == "Abilityai/cornelius"
    assert entry.display_name == "Cornelius"
    assert entry.description == "Your second brain."
    assert entry.priority == 20


def test_absent_version_defaults_to_one():
    parsed = parse_registry_document("templates:\n  - repo: a/b\n")
    assert parsed.ok
    assert _repos(parsed) == ["a/b"]


def test_optional_fields_are_optional():
    parsed = parse_registry_document("version: 1\ntemplates:\n  - repo: a/b\n")
    (entry,) = parsed.entries
    assert (entry.display_name, entry.description, entry.priority) == ("", "", None)


def test_empty_registry_is_a_SUCCESS_not_a_failure():
    """`templates: []` is the deliberate day-one state the ship prerequisite
    publishes. Conflating it with a failure would report an operator's
    intentionally-empty catalog as an outage."""
    parsed = parse_registry_document("version: 1\ntemplates: []\n")
    assert parsed.ok is True
    assert parsed.entries == ()
    assert parsed.error_code is None


# ---------------------------------------------------------------------------
# Whole-document refusals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "document,expected",
    [
        ("- a\n- b\n", ERROR_BAD_SHAPE),                     # top-level list
        ("just a string\n", ERROR_BAD_SHAPE),                # top-level scalar
        ("", ERROR_BAD_SHAPE),                               # empty document
        ("version: 1\n", ERROR_BAD_SHAPE),                   # no `templates`
        ("version: 1\ntemplates: {}\n", ERROR_BAD_SHAPE),    # `templates` a mapping
        ("version: 1\ntemplates: nope\n", ERROR_BAD_SHAPE),  # `templates` a scalar
        ("version: 2\ntemplates: []\n", ERROR_UNSUPPORTED_VERSION),
        ("version: 99\ntemplates: []\n", ERROR_UNSUPPORTED_VERSION),
        ("version: '1'\ntemplates: []\n", ERROR_UNSUPPORTED_VERSION),
        ("version: true\ntemplates: []\n", ERROR_UNSUPPORTED_VERSION),
    ],
)
def test_structurally_wrong_documents_are_refused_whole(document, expected):
    parsed = parse_registry_document(document)
    assert parsed.ok is False
    assert parsed.error_code == expected
    assert parsed.entries == ()


def test_a_future_schema_version_refuses_rather_than_misreading():
    """Direction of failure: an unknown major version must degrade to the floor,
    never be read with v1 semantics against a shape we do not know."""
    parsed = parse_registry_document(
        "version: 2\ntemplates:\n  - repo: a/b\n    display_name: Ok\n"
    )
    assert parsed.ok is False
    assert parsed.entries == ()


# ---------------------------------------------------------------------------
# ent#314 hardening reaches this document
# ---------------------------------------------------------------------------

def test_any_alias_is_refused():
    parsed = parse_registry_document(
        "version: 1\n_x: &anchor {repo: a/b}\ntemplates:\n  - *anchor\n"
    )
    assert parsed.ok is False
    assert parsed.error_code == ERROR_PARSE_REFUSED


def test_a_level_six_alias_bomb_is_refused():
    """The measured 416 B -> 110 MB shape. It must never reach `json.dumps`."""
    lines = ["version: 1", "a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    for level in range(1, 7):
        prev = f"*a{level - 1}"
        lines.append(f"a{level}: &a{level} [{', '.join([prev] * 10)}]")
    lines.append("templates: []")
    parsed = parse_registry_document("\n".join(lines))
    assert parsed.ok is False
    assert parsed.error_code == ERROR_PARSE_REFUSED


def test_duplicate_keys_are_refused_not_last_wins():
    """Two `templates:` keys would silently last-wins under bare safe_load —
    one catalog shown to the human editing the file, another served to Trinity."""
    parsed = parse_registry_document(
        "version: 1\ntemplates:\n  - repo: good/one\ntemplates:\n  - repo: evil/two\n"
    )
    assert parsed.ok is False
    assert parsed.error_code == ERROR_PARSE_REFUSED


def test_an_oversize_document_is_refused_at_the_parser_too():
    """The transport ceiling is load-bearing; this is the belt that survives a
    future refactor of the fetch layer."""
    parsed = parse_registry_document("version: 1\ntemplates: []\n" + "#" * (300 * 1024))
    assert parsed.ok is False
    assert parsed.error_code == ERROR_TOO_LARGE


def test_a_refusal_never_echoes_the_document_body():
    """The parser's errors ride into an operator's settings panel."""
    secret = "SUPER-SECRET-MARKER"
    parsed = parse_registry_document(f"version: 1\ntemplates: []\ntemplates: [{secret}]\n")
    assert parsed.ok is False
    assert secret not in " ".join(parsed.errors)


# ---------------------------------------------------------------------------
# Per-entry drops
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "entry_yaml",
    [
        "- not-a-mapping",
        "- 42",
        "- []",
        "- {display_name: no repo here}",
        "- {repo: 12345}",
        "- {repo: [a/b]}",
        "- {repo: single-segment}",
        "- {repo: a/b/c}",
        "- {repo: 'a/b c'}",
        "- {repo: 'a/b@main'}",
        "- {repo: ''}",
    ],
)
def test_a_bad_entry_costs_only_itself(entry_yaml):
    parsed = parse_registry_document(
        f"version: 1\ntemplates:\n  {entry_yaml}\n  - repo: good/one\n"
    )
    assert parsed.ok is True
    assert _repos(parsed) == ["good/one"]
    assert parsed.errors  # named, so an operator can debug their registry


@pytest.mark.parametrize("repo", ["../evil", "a/..", "./x", "..", "../.."])
def test_dot_segments_are_refused(repo):
    """`.` is inside the shared owner/repo character class, so `../evil` MATCHES
    the regex. That would render "../evil" on the card while both
    `api.github.com/repos/../evil/...` and `github.com/../evil.git` normalize to
    a DIFFERENT repo — the card advertising one path and cloning another."""
    parsed = parse_registry_document(
        f"version: 1\ntemplates:\n  - repo: '{repo}'\n  - repo: good/one\n"
    )
    assert _repos(parsed) == ["good/one"]


def test_a_control_byte_in_repo_is_refused():
    parsed = parse_registry_document(
        'version: 1\ntemplates:\n  - repo: "a/b\\u0000c"\n  - repo: good/one\n'
    )
    assert _repos(parsed) == ["good/one"]


def test_duplicate_repo_keeps_the_first_and_is_case_insensitive():
    """One GitHub repo but two template ids and two metadata-cache keys — so
    admitting both spellings would render one repo as two cards."""
    parsed = parse_registry_document(
        """
        version: 1
        templates:
          - repo: Acme/Widget
            display_name: First
          - repo: acme/widget
            display_name: Second
        """
    )
    assert _repos(parsed) == ["Acme/Widget"]
    assert parsed.entries[0].display_name == "First"
    assert any("duplicate" in e for e in parsed.errors)


# ---------------------------------------------------------------------------
# Field handling
# ---------------------------------------------------------------------------

def test_oversize_display_fields_are_truncated_not_dropped():
    parsed = parse_registry_document(
        "version: 1\ntemplates:\n"
        f"  - repo: a/b\n    display_name: {'N' * 5000}\n    description: {'D' * 50000}\n"
    )
    (entry,) = parsed.entries
    assert len(entry.display_name) == MAX_DISPLAY_NAME_LEN
    assert len(entry.description) == MAX_DESCRIPTION_LEN


@pytest.mark.parametrize("bad", ["[1, 2]", "{a: b}", "12", "true"])
def test_a_non_string_display_name_falls_through_rather_than_coercing(bad):
    """No `str()` on a container from untrusted YAML — that walks the graph and
    pays the amplification cost before any cap can act."""
    parsed = parse_registry_document(
        f"version: 1\ntemplates:\n  - repo: a/b\n    display_name: {bad}\n"
    )
    (entry,) = parsed.entries
    assert entry.display_name == ""


@pytest.mark.parametrize("bad,expected", [("high", None), ("true", None), ("1.5", None), ("7", 7), ("-3", -3)])
def test_priority_accepts_only_real_ints(bad, expected):
    parsed = parse_registry_document(
        f"version: 1\ntemplates:\n  - repo: a/b\n    priority: {bad}\n"
    )
    (entry,) = parsed.entries
    assert entry.priority == expected


def test_priority_true_does_not_become_one():
    """`isinstance(True, int)` is True in Python — `_coerce_priority`'s own reason."""
    parsed = parse_registry_document(
        "version: 1\ntemplates:\n  - repo: a/b\n    priority: true\n"
    )
    assert parsed.entries[0].priority is None


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------

def test_over_cap_truncates_with_a_named_error():
    body = "\n".join(f"  - repo: acme/t{i}" for i in range(MAX_REGISTRY_TEMPLATES + 10))
    parsed = parse_registry_document(f"version: 1\ntemplates:\n{body}\n")
    assert parsed.ok is True
    assert len(parsed.entries) == MAX_REGISTRY_TEMPLATES
    assert any(str(MAX_REGISTRY_TEMPLATES) in e for e in parsed.errors)


def test_the_error_list_itself_is_bounded():
    """A hostile registry must not be able to make the admin status payload huge."""
    body = "\n".join(f"  - bad-entry-{i}" for i in range(500))
    parsed = parse_registry_document(f"version: 1\ntemplates:\n{body}\n")
    assert len(parsed.errors) <= MAX_REPORTED_ERRORS + 1  # +1 for the "and N more" line


def test_a_hostile_repo_string_is_snipped_in_the_error():
    parsed = parse_registry_document(
        f"version: 1\ntemplates:\n  - repo: '{'Z' * 4000}'\n"
    )
    assert parsed.entries == ()
    assert max(len(e) for e in parsed.errors) < 300


# ---------------------------------------------------------------------------
# THE ALLOWLIST — the blast-radius bound
# ---------------------------------------------------------------------------

FORBIDDEN_KEYS = [
    "fork_to_own", "credentials", "credential_setup", "schedules", "data_paths",
    "persistent_state", "resources", "skills", "hidden", "id", "github_repo",
    "source", "mcp_servers", "required_credentials", "tagline",
    "metadata_unavailable",
]


@pytest.mark.parametrize("key", FORBIDDEN_KEYS)
def test_a_registry_cannot_assert_any_field_it_does_not_own(key):
    """Every one of these is a claim about a repo the registry does not own, and
    every one has a creation-path consequence. Unknown keys are IGNORED, never
    merged — this is what bounds a hostile registry to display and order."""
    parsed = parse_registry_document(
        f"version: 1\ntemplates:\n  - repo: a/b\n    {key}: required\n"
    )
    (entry,) = parsed.entries
    assert set(vars(entry)) == {"repo", "display_name", "description", "priority"}
    assert entry.as_override() == {
        "github_repo": "a/b",
        "display_name": "",
        "description": "",
        "priority": None,
    }


def test_the_record_is_frozen():
    """A consumer must not be able to smuggle a fifth field back in by mutating
    a shared instance out of the process-wide cache."""
    parsed = parse_registry_document("version: 1\ntemplates:\n  - repo: a/b\n")
    with pytest.raises(Exception):
        parsed.entries[0].repo = "evil/repo"
