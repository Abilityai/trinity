"""Workspace Build: the company profile, kept once per instance (core-agent AC-14).

The first person's guide researches the company; the page keeps the result here so a
colleague's guide starts from it. Only people who can build read or write it, never an
agent key (the routes take ``require_person`` and the creator role). These are the pure
rules of the stored document.
"""

import importlib.util
import json
from pathlib import Path

# Loaded by path: importing it through the `services` package would pull in the whole
# backend, and these rules need nothing but the standard library.
_PATH = Path(__file__).resolve().parents[2] / "src" / "backend" / "services" / "workspace_company.py"
_spec = importlib.util.spec_from_file_location("workspace_company", _PATH)
wc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wc)


def test_keeps_only_the_four_texts_trimmed():
    out = wc.normalize_company({"name": "  Acme Freight ", "website": "acme.example", "research_md": "# Acme", "context_md": "ctx", "owner": "x"})
    assert out == {"name": "Acme Freight", "website": "acme.example", "research_md": "# Acme", "context_md": "ctx"}


def test_cuts_each_text_at_its_bound_and_drops_non_text():
    out = wc.normalize_company({"name": "n" * 600, "website": 5, "research_md": "r" * 250_000, "context_md": ["x"]})
    assert len(out["name"]) == 500
    assert out["website"] == ""
    assert len(out["research_md"]) == 200_000
    assert out["context_md"] == ""
    assert wc.normalize_company(None) == {"name": "", "website": "", "research_md": "", "context_md": ""}


def test_reads_a_stored_profile_and_says_who_kept_it():
    stored = wc.stored_document({"name": "Acme"}, "person@example.com", "2026-11-03T12:00:00Z")
    back = wc.parse_stored(json.dumps(stored))
    assert back["name"] == "Acme"
    assert back["updated_by"] == "person@example.com"
    assert back["updated_at"] == "2026-11-03T12:00:00Z"


def test_an_unreadable_or_empty_value_is_no_profile():
    for raw in (None, "", "not json", "[]", json.dumps({"name": ""})):
        assert wc.parse_stored(raw) is None, raw


def test_an_empty_text_never_blanks_what_a_colleague_kept():
    old = wc.stored_document({"name": "Acme", "research_md": "# Acme", "context_md": "ctx"}, "first@example.com", "t1")
    new = wc.merged_with(old, {"name": "Acme", "research_md": "", "context_md": "new ctx"})
    assert new == {"name": "Acme", "website": "", "research_md": "# Acme", "context_md": "new ctx"}
    assert wc.merged_with(None, {"name": "Acme"})["name"] == "Acme"
