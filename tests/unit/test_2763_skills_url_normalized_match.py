"""#2763 — the legacy skills-library match compared a raw URL to a normalized one.

`validate_skills_library_url` is a validator AND a normalizer (`github.com/o/r`
-> `https://github.com/o/r`). `routers/skills.py` uses it as one when it stores
a source; `_adopt_legacy_clone` called it and threw the return away, then
compared the RAW setting value against the stored url.

So the same repository written two ways never matched, the install took the
ent#346 "already has sources" refusal branch on every sync, and filed a fresh
un-deduped high-priority alert each time (#2744 is that flood).

Both representations occur on real installs by construction:
  * the bundled default source is seeded from `config.TRINITY_DEFAULT_SKILL_SOURCE`,
    a bare literal with no scheme, which never passes through the validator;
  * a source created via `POST /api/skills/sources` is stored normalized.

Every test here drives the REAL `_adopt_legacy_clone`. The only stubs are `db`
and `library_root`.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import services.skill_service as ss
from services.skill_service import _same_skills_repo
from utils.url_validation import validate_skills_library_url

pytestmark = pytest.mark.unit

NORMALIZED = "https://github.com/abilityai/trinity-skills"
BARE = "github.com/abilityai/trinity-skills"


def _svc():
    svc = ss.SkillService.__new__(ss.SkillService)
    # No adoption path may touch the filesystem in these tests; a path that does
    # not exist makes that a hard failure rather than a silent read.
    svc.library_root = Path("/nonexistent-skills-library-2763")
    return svc


def _run(legacy_key, sources, count=None):
    """Drive the real method. Returns (result, alerts, created)."""
    alerts, created = [], []
    svc = _svc()
    with patch.object(ss, "get_skills_library_url", return_value=legacy_key), \
         patch.object(ss, "db") as db, \
         patch.object(ss.SkillService, "_record_adoption_failure",
                      lambda self, url, msg: alerts.append((url, msg))):
        db.list_skill_sources.return_value = sources
        db.count_skill_sources.return_value = len(sources) if count is None else count
        db.create_skill_source.side_effect = lambda **kw: created.append(kw) or SimpleNamespace(
            id="src_new", url=kw["url"])
        result = ss.SkillService._adopt_legacy_clone(svc)
    return result, alerts, created


# --- the regression ---------------------------------------------------------

@pytest.mark.parametrize("stored,legacy_key", [
    pytest.param(BARE, NORMALIZED, id="seeded-default-vs-https-key"),
    pytest.param(NORMALIZED, BARE, id="api-created-source-vs-bare-key"),
])
def test_the_same_repo_in_either_form_matches_and_files_no_alert(stored, legacy_key):
    """THE FIX. Before #2763 both of these took the refusal branch, forever."""
    # Precondition: the two really are the same repo, and really are != as strings.
    assert stored != legacy_key
    assert validate_skills_library_url(stored) == validate_skills_library_url(legacy_key)

    result, alerts, created = _run(legacy_key, [SimpleNamespace(id="src_x", url=stored)])

    assert result == "src_x", "should resolve to the existing source"
    assert alerts == [], "a repo already configured must not alert"
    assert created == [], "a match is the NO-OP branch — it must create nothing"


# --- what must NOT change ---------------------------------------------------

def test_a_genuinely_different_repo_still_reaches_the_ent346_refusal():
    """The detector still detects. This is the branch ent#346 exists for."""
    result, alerts, created = _run(
        "https://github.com/attacker/skills", [SimpleNamespace(id="src_x", url=NORMALIZED)])

    assert result is None
    assert len(alerts) == 1
    assert "already has" in alerts[0][1]
    assert alerts[0][0] == "https://github.com/attacker/skills"   # reaches context.url
    assert created == [], "a refusal must never register a source"


def test_the_grant_branch_is_untouched_on_a_pre_migration_install():
    """No sources at all -> still migrates, exactly as before."""
    result, alerts, created = _run(NORMALIZED, [], count=0)

    assert alerts == []
    assert len(created) == 1
    assert created[0]["url"] == NORMALIZED
    assert created[0]["created_by"] == "migration:ent237"
    assert result == "src_new"


def test_a_credential_bearing_key_is_still_refused_without_echoing_it():
    """`reject_embedded_credentials` sees the ORIGINAL, and its message must not
    carry the secret — the one case where echoing the URL would leak."""
    result, alerts, _ = _run("https://x-access-token:ghp_AAAA@github.com/o/r", [])
    assert result is None
    assert len(alerts) == 1
    assert "ghp_AAAA" not in alerts[0][1]


# --- the helper -------------------------------------------------------------

def test_same_repo_collapses_the_scheme_split_only():
    assert _same_skills_repo(BARE, NORMALIZED)
    assert _same_skills_repo(NORMALIZED, NORMALIZED)
    assert not _same_skills_repo("https://github.com/other/repo", NORMALIZED)


def test_same_repo_fails_safe_on_an_unusable_stored_value():
    """An unparseable row must NOT read as equal — that would silently adopt it."""
    for bad in ("", None, "not a url", "ftp://github.com/o/r"):
        assert not _same_skills_repo(bad, NORMALIZED)


def test_the_documented_tail_is_still_distinct():
    """Scope guard: these are deliberately NOT collapsed (see the helper docstring).
    If a later change makes one of them match, that is an ent#346 decision and
    this test is the place it gets argued."""
    for tail in (f"{NORMALIZED}.git", f"{NORMALIZED}/", "abilityai/trinity-skills"):
        assert not _same_skills_repo(tail, NORMALIZED)
