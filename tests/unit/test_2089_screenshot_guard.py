"""Unit tests for the screenshot store guard (#2089).

Trinity keeps every UI screenshot in ONE place, `docs/screenshots/`, with one
`MANIFEST.yaml` entry each. Three stores existed before #2089 and drifted because the
capture tooling wrote to the directory no page embedded — the freshest set was the one
nobody rendered, while the rendered ones aged three months.

`scripts/ci/check_screenshots.py` is what stops that recurring, so it needs the same
kind of coverage as the other `scripts/ci/` guards (#2068, alembic parity): a guard
verified only by ad-hoc shell fixtures at authoring time is unverified the moment
someone edits it.

Coverage mirrors the acceptance criteria:

  (a) the REAL store passes integrity — a regression lock on the live tree, not a
      fixture, so a future PR that breaks an embed or orphans an image fails here too;
  (b) a manifest entry with no file FAILS;
  (c) a store image with no manifest entry FAILS — this is the direction that lets an
      unowned screenshot quietly accumulate;
  (d) a broken doc image reference FAILS — silent on GitHub until a reader hits it;
  (e) an image referenced from OUTSIDE the canonical store FAILS — the store drift the
      guard exists to prevent;
  (f) a `sources:` path that no longer exists FAILS. This is the subtle one: freshness
      over a nonexistent path finds no commits, i.e. the entry becomes immortally
      "fresh". A guard whose own inputs can rot without complaint is worse than none;
  (g) empty `sources` without `external: true` FAILS, for the same reason;
  (h) `external: true` with empty `sources` PASSES and is excluded from freshness —
      third-party screens (GitHub's own token page) never go stale from our releases;
  (i) a non-flat `file` value FAILS (the store is flat, so `file` is a bare filename);
  (j) a malformed `captured_at` FAILS;
  (k) a duplicate manifest entry FAILS;
  (l) a MISSING or unparseable manifest FAILS rather than reporting green — the
      fail-closed rule the #2068 guard learned the hard way;
  (m) staleness is ADVISORY by default and BLOCKING under --strict. This asymmetry is
      deliberate and load-bearing: a frontend PR legitimately makes a screenshot stale
      in the same commit that changes the view, so blocking on PRs would force a
      recapture into every UI PR and get routed around.

Two pins guard the mechanism AROUND the script, each of which fails only after merge
if left untested:

  (n) the workflow reads the result via `--json-out`, never a pipe. Under GitHub's
      default non-pipefail shell `python … | tee` reports tee's status, so a failing
      guard would pass — reintroducing the pipe silently disables the whole job;
  (o) the workflow checks out with `fetch-depth: 0`. Staleness compares against git
      history; a shallow clone makes the freshness half report "not evaluated".
"""

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "check_screenshots.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "screenshot-guard.yml"


def load_guard():
    """Import the guard as a module (it is a script, not an installed package)."""
    spec = importlib.util.spec_from_file_location("check_screenshots", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def guard():
    return load_guard()


# --------------------------------------------------------------------------- fixtures


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def build_tree(tmp_path, *, entries, store_files=None, docs=None, sources=()):
    """Lay out a miniature repo the guard can be pointed at.

    Returns the repo root. `entries` is the manifest's `screenshots:` list, `store_files`
    the filenames actually written into docs/screenshots/ (defaults to the entry files),
    `docs` a {relative md path: text} mapping, and `sources` extra paths to create so
    `sources:` references resolve.
    """
    store = tmp_path / "docs" / "screenshots"
    store.mkdir(parents=True)

    if store_files is None:
        store_files = [e["file"] for e in entries if "file" in e]
    for name in store_files:
        (store / name).write_bytes(PNG)

    for source in sources:
        target = tmp_path / source
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("// source\n", encoding="utf-8")

    manifest = {"version": 1, "viewport": {"width": 1440}, "screenshots": entries}
    (store / "MANIFEST.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    for relpath, text in (docs or {}).items():
        doc = tmp_path / relpath
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text(text, encoding="utf-8")

    return tmp_path


def point_at(guard, root, *, doc_files=()):
    """Repoint the guard's module-level paths at a fixture tree."""
    guard.REPO = root
    guard.STORE = root / "docs" / "screenshots"
    guard.MANIFEST = guard.STORE / "MANIFEST.yaml"
    guard.DOC_ROOTS = [root / "docs"]
    guard.DOC_FILES = [root / f for f in doc_files]


def entry(**overrides):
    base = {
        "file": "dashboard-grid.png",
        "title": "Dashboard — Grid view",
        "route": "/?view=grid",
        "shows": "The fleet as tiles.",
        "sources": ["src/frontend/src/views/Dashboard.vue"],
        "captured_at": "2026-08-09",
        "captured_commit": "abc1234",
    }
    base.update(overrides)
    return base


def run_integrity(guard):
    """The blocking half: manifest/file/reference integrity, no git involved."""
    findings = guard.Findings()
    entries = guard.load_manifest(findings)
    by_file = guard.check_entries(entries, findings)
    guard.check_store_files(by_file, findings)
    guard.check_references(findings)
    return findings, by_file


def messages(findings):
    return " | ".join(m for _, m in findings.errors)


# ------------------------------------------------------------- (a) real-tree lock


def test_real_store_passes_integrity(guard):
    """The live tree must be clean — this is a lock, not a fixture."""
    findings, by_file = run_integrity(guard)
    assert not findings.errors, messages(findings)
    assert by_file, "guard found no screenshots at all — it is pointed at nothing"


def test_real_store_every_image_is_claimed(guard):
    """No unowned images, in either direction."""
    _, by_file = run_integrity(guard)
    on_disk = {
        p.name
        for p in guard.STORE.iterdir()
        if p.is_file() and p.suffix.lower() in guard.IMAGE_SUFFIXES
    }
    assert on_disk == set(by_file), (
        f"manifest-only: {sorted(set(by_file) - on_disk)}, "
        f"disk-only: {sorted(on_disk - set(by_file))}"
    )


def test_real_manifest_sources_all_exist(guard):
    """A rotted `sources` path makes an entry immortally fresh — see (f)."""
    _, by_file = run_integrity(guard)
    missing = [
        (name, source)
        for name, spec in by_file.items()
        for source in spec.get("sources", [])
        if not (guard.REPO / str(source)).exists()
    ]
    assert not missing, f"stale source paths: {missing}"


# ------------------------------------------------------------- (b)-(k) failure modes


def test_manifest_entry_without_file_fails(guard, tmp_path):
    root = build_tree(
        tmp_path,
        entries=[entry(file="ghost.png")],
        store_files=[],
        sources=["src/frontend/src/views/Dashboard.vue"],
    )
    point_at(guard, root)
    findings, _ = run_integrity(guard)
    assert any("no file in docs/screenshots" in m for _, m in findings.errors), messages(
        findings
    )


def test_store_image_without_manifest_entry_fails(guard, tmp_path):
    root = build_tree(
        tmp_path,
        entries=[entry()],
        store_files=["dashboard-grid.png", "unclaimed.png"],
        sources=["src/frontend/src/views/Dashboard.vue"],
    )
    point_at(guard, root)
    findings, _ = run_integrity(guard)
    assert any("no MANIFEST.yaml entry" in m for _, m in findings.errors), messages(
        findings
    )


def test_broken_doc_reference_fails(guard, tmp_path):
    root = build_tree(
        tmp_path,
        entries=[entry()],
        sources=["src/frontend/src/views/Dashboard.vue"],
        docs={"docs/user-docs/page.md": "![x](../screenshots/does-not-exist.png)\n"},
    )
    point_at(guard, root)
    findings, _ = run_integrity(guard)
    assert any("broken image reference" in m for _, m in findings.errors), messages(
        findings
    )


def test_image_referenced_from_outside_the_store_fails(guard, tmp_path):
    """The drift this guard exists to prevent: a second store growing elsewhere."""
    root = build_tree(
        tmp_path,
        entries=[entry()],
        sources=["src/frontend/src/views/Dashboard.vue"],
        docs={"docs/user-docs/page.md": "![x](../rogue/shot.png)\n"},
    )
    rogue = root / "docs" / "rogue"
    rogue.mkdir(parents=True)
    (rogue / "shot.png").write_bytes(PNG)
    point_at(guard, root)
    findings, _ = run_integrity(guard)
    assert any("outside the canonical store" in m for _, m in findings.errors), messages(
        findings
    )


def test_frozen_whats_new_and_brand_assets_are_allowed(guard, tmp_path):
    """The two deliberate exceptions must NOT be reported as drift."""
    root = build_tree(
        tmp_path,
        entries=[entry()],
        sources=["src/frontend/src/views/Dashboard.vue"],
        docs={
            "docs/user-docs/whats-new/v0.8.0.md": (
                "![a](../../assets/screenshots/whats-new/v0.8.0/orb.png)\n"
                "![b](../../assets/trinity-hero.webp)\n"
            )
        },
    )
    frozen = root / "docs" / "assets" / "screenshots" / "whats-new" / "v0.8.0"
    frozen.mkdir(parents=True)
    (frozen / "orb.png").write_bytes(PNG)
    (root / "docs" / "assets" / "trinity-hero.webp").write_bytes(PNG)
    point_at(guard, root)
    findings, _ = run_integrity(guard)
    assert not findings.errors, messages(findings)


def test_nonexistent_source_path_fails(guard, tmp_path):
    """(f) The subtle case — freshness over a missing path is always 'fresh'."""
    root = build_tree(
        tmp_path,
        entries=[entry(sources=["src/frontend/src/components/Gone.vue"])],
    )
    point_at(guard, root)
    findings, _ = run_integrity(guard)
    assert any("source path does not exist" in m for _, m in findings.errors), messages(
        findings
    )


def test_empty_sources_without_external_fails(guard, tmp_path):
    root = build_tree(tmp_path, entries=[entry(sources=[])])
    point_at(guard, root)
    findings, _ = run_integrity(guard)
    assert any("empty `sources`" in m for _, m in findings.errors), messages(findings)


def test_external_entry_with_empty_sources_passes(guard, tmp_path):
    """(h) A third-party screen never goes stale from our releases."""
    root = build_tree(
        tmp_path,
        entries=[entry(file="external-github-pat-scopes.png", sources=[], external=True)],
    )
    point_at(guard, root)
    findings, _ = run_integrity(guard)
    assert not findings.errors, messages(findings)


def test_non_flat_file_value_fails(guard, tmp_path):
    root = build_tree(tmp_path, entries=[entry(file="nested/shot.png")], store_files=[])
    point_at(guard, root)
    findings, _ = run_integrity(guard)
    assert any("bare filename" in m for _, m in findings.errors), messages(findings)


def test_malformed_captured_at_fails(guard, tmp_path):
    root = build_tree(
        tmp_path,
        entries=[entry(captured_at="last tuesday")],
        sources=["src/frontend/src/views/Dashboard.vue"],
    )
    point_at(guard, root)
    findings, _ = run_integrity(guard)
    assert any("ISO date" in m for _, m in findings.errors), messages(findings)


def test_missing_required_key_fails(guard, tmp_path):
    incomplete = entry()
    del incomplete["shows"]
    root = build_tree(
        tmp_path, entries=[incomplete], sources=["src/frontend/src/views/Dashboard.vue"]
    )
    point_at(guard, root)
    findings, _ = run_integrity(guard)
    assert any("missing required key" in m for _, m in findings.errors), messages(findings)


def test_duplicate_entry_fails(guard, tmp_path):
    root = build_tree(
        tmp_path,
        entries=[entry(), entry()],
        sources=["src/frontend/src/views/Dashboard.vue"],
    )
    point_at(guard, root)
    findings, _ = run_integrity(guard)
    assert any("duplicate manifest entry" in m for _, m in findings.errors), messages(
        findings
    )


# ------------------------------------------------------------- (l) fail-closed


def test_missing_manifest_fails_closed(guard, tmp_path):
    """A guard that reports green when it read nothing is worse than no guard."""
    root = build_tree(tmp_path, entries=[entry()], sources=["src/x.vue"])
    guard_manifest = root / "docs" / "screenshots" / "MANIFEST.yaml"
    guard_manifest.unlink()
    point_at(guard, root)
    findings, _ = run_integrity(guard)
    assert any("manifest is missing" in m for _, m in findings.errors), messages(findings)


def test_unparseable_manifest_fails_closed(guard, tmp_path):
    root = build_tree(tmp_path, entries=[entry()], sources=["src/x.vue"])
    (root / "docs" / "screenshots" / "MANIFEST.yaml").write_text(
        "screenshots: [\n  unclosed", encoding="utf-8"
    )
    point_at(guard, root)
    findings, _ = run_integrity(guard)
    assert any("does not parse" in m for _, m in findings.errors), messages(findings)


def test_manifest_without_screenshots_list_fails_closed(guard, tmp_path):
    root = build_tree(tmp_path, entries=[entry()], sources=["src/x.vue"])
    (root / "docs" / "screenshots" / "MANIFEST.yaml").write_text(
        "version: 1\n", encoding="utf-8"
    )
    point_at(guard, root)
    findings, _ = run_integrity(guard)
    assert any("`screenshots:` list" in m for _, m in findings.errors), messages(findings)


# ------------------------------------------------------------- (m) staleness weighting


@pytest.fixture
def stale_tree(guard, tmp_path, monkeypatch):
    root = build_tree(
        tmp_path,
        entries=[entry(captured_at="2026-06-01")],
        sources=["src/frontend/src/views/Dashboard.vue"],
    )
    point_at(guard, root)
    monkeypatch.setattr(guard, "git_history_is_shallow", lambda: False)
    monkeypatch.setattr(guard, "newest_source_date", lambda sources: "2026-08-09")
    return root


def test_staleness_is_advisory_by_default(guard, stale_tree):
    """A UI PR makes a screenshot stale in the same commit that changes the view."""
    findings = guard.Findings()
    stale = guard.check_freshness(
        guard.check_entries(guard.load_manifest(findings), findings), findings, False
    )
    assert len(stale) == 1
    assert not findings.errors, messages(findings)
    assert any("stale" in m for _, m in findings.warnings)


def test_staleness_blocks_under_strict(guard, stale_tree):
    findings = guard.Findings()
    stale = guard.check_freshness(
        guard.check_entries(guard.load_manifest(findings), findings), findings, True
    )
    assert len(stale) == 1
    assert any("stale" in m for _, m in findings.errors), messages(findings)


def test_recaptured_entry_is_not_stale(guard, tmp_path, monkeypatch):
    root = build_tree(
        tmp_path,
        entries=[entry(captured_at="2026-08-10")],
        sources=["src/frontend/src/views/Dashboard.vue"],
    )
    point_at(guard, root)
    monkeypatch.setattr(guard, "git_history_is_shallow", lambda: False)
    monkeypatch.setattr(guard, "newest_source_date", lambda sources: "2026-08-09")
    findings = guard.Findings()
    stale = guard.check_freshness(
        guard.check_entries(guard.load_manifest(findings), findings), findings, True
    )
    assert stale == []


def test_shallow_clone_warns_instead_of_passing_silently(guard, tmp_path, monkeypatch):
    """(o)'s failure mode: without history the freshness half buys nothing — say so."""
    root = build_tree(
        tmp_path,
        entries=[entry(captured_at="2026-06-01")],
        sources=["src/frontend/src/views/Dashboard.vue"],
    )
    point_at(guard, root)
    monkeypatch.setattr(guard, "git_history_is_shallow", lambda: True)
    findings = guard.Findings()
    stale = guard.check_freshness(
        guard.check_entries(guard.load_manifest(findings), findings), findings, True
    )
    assert stale == []
    assert any("shallow clone" in m for _, m in findings.warnings)


# ------------------------------------------------------------- exit codes


def test_main_exit_codes(guard, tmp_path, monkeypatch, capsys):
    """Advisory run exits 0 with a stale entry; --strict exits 1."""
    root = build_tree(
        tmp_path,
        entries=[entry(captured_at="2026-06-01")],
        sources=["src/frontend/src/views/Dashboard.vue"],
    )
    point_at(guard, root)
    monkeypatch.setattr(guard, "git_history_is_shallow", lambda: False)
    monkeypatch.setattr(guard, "newest_source_date", lambda sources: "2026-08-09")

    monkeypatch.setattr(sys, "argv", ["check_screenshots.py"])
    assert guard.main() == 0

    monkeypatch.setattr(sys, "argv", ["check_screenshots.py", "--strict"])
    assert guard.main() == 1
    assert "FAIL" in capsys.readouterr().out


def test_json_out_writes_a_summary(guard, tmp_path, monkeypatch):
    import json

    root = build_tree(
        tmp_path, entries=[entry()], sources=["src/frontend/src/views/Dashboard.vue"]
    )
    point_at(guard, root)
    monkeypatch.setattr(guard, "git_history_is_shallow", lambda: True)
    out = tmp_path / "summary.json"
    monkeypatch.setattr(
        sys, "argv", ["check_screenshots.py", "--json-out", str(out)]
    )
    assert guard.main() == 0
    payload = json.loads(out.read_text())
    assert payload["screenshots"] == 1
    assert payload["errors"] == []


# ------------------------------------------------------------- (n)(o) workflow wiring


def test_workflow_reads_result_without_a_pipe():
    """`python … | tee` reports tee's status under GitHub's default non-pipefail shell,
    so piping the guard silently disables it. The workflow must use --json-out."""
    text = WORKFLOW.read_text(encoding="utf-8")
    invocation = [
        line for line in text.splitlines() if "check_screenshots.py" in line and "run:" in line
    ]
    assert invocation, "workflow no longer invokes the guard"
    assert "--json-out" in text
    for line in invocation:
        assert "|" not in line.split("check_screenshots.py")[1], (
            f"guard result is piped, which swallows its exit code: {line.strip()}"
        )


def test_workflow_checks_out_full_history():
    """Staleness compares against git history; a shallow clone evaluates nothing."""
    config = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = config["jobs"]["guard"]["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout.get("with", {}).get("fetch-depth") == 0


def test_workflow_is_strict_only_off_pull_requests():
    """Strict on the scheduled sweep and manual dispatch; advisory on PRs."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "schedule" in text and "--strict" in text
    config = yaml.safe_load(text)
    triggers = config.get("on") or config.get(True)
    assert "pull_request" in triggers and "schedule" in triggers
