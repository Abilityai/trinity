"""#1028 — `services/git_service` is a package, and the split changed no behaviour.

2,322 lines split into six responsibility modules. The properties pinned here
are the ones a regrouping has to keep:

  * every module stays under the 800-line critical threshold;
  * the import surface other backend modules depend on still resolves;
  * cross-module calls go THROUGH the sibling module object, never a
    from-import of the function — a from-import freezes the binding, so a test
    patching the owning module would silently stop reaching the caller. That
    is not a style rule: it is what happened to test_2069's readiness probes
    mid-split, and this guard is how it stays fixed.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PKG = Path(__file__).resolve().parents[2] / "src" / "backend" / "services" / "git_service"


def test_every_module_is_under_the_critical_threshold():
    oversized = {p.name: len(p.read_text().splitlines())
                 for p in _PKG.glob("*.py")
                 if len(p.read_text().splitlines()) > 800}
    assert oversized == {}, f"over the 800-line threshold: {oversized}"


def test_the_import_surface_still_resolves():
    """Names other src/backend modules import from `services.git_service`."""
    import services.git_service as g

    for name in (
        "sync_to_github", "pull_from_github", "get_git_status", "get_git_log",
        "reset_to_main_preserve_state", "classify_conflict", "ConflictClass",
        "initialize_git_in_container", "check_git_initialized",
        "check_remote_branch_exists", "probe_anonymous_repo_access",
        "reserve_and_generate_instance_id", "create_git_config_for_agent",
        "rebind_origin_and_push", "inspect_container_git", "update_remote_pat",
        "materialize_persistent_state", "materialize_data_paths",
        "materialize_plugins", "materialize_trinity_yaml_list",
        "merge_gitignore_after_clone", "spawn_gitignore_merge_after_clone",
        "DEFAULT_PERSISTENT_STATE", "DEFAULT_DATA_PATHS",
        "_GITIGNORE_PATTERNS", "_TRINITY_AUTHORED_PATHS",
        "_detect_git_dir", "_git_auto_sync_baked", "_git_remote_url",
        "NO_WRITE_CREDENTIALS_MESSAGE",
    ):
        assert hasattr(g, name), f"services.git_service.{name} no longer resolves"


def test_no_function_from_import_between_package_modules():
    """Cross-module calls must go through the sibling module object.

    `from .gitignore import _detect_git_dir` freezes the function into the
    importer's namespace: a test that patches
    `git_service.gitignore._detect_git_dir` then no longer reaches that
    caller, and nothing fails — the patch applies cleanly to a name nobody
    reads. `from . import gitignore` + `gitignore._detect_git_dir(...)`
    resolves at call time, so the patch lands for every caller.
    """
    offenders = []
    for path in sorted(_PKG.glob("*.py")):
        if path.name == "__init__.py":
            continue  # the facade's re-exports are the documented exception
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is None and node.level == 1:
                # `from . import x` — importing the MODULE is the sanctioned form
                continue
            if isinstance(node, ast.ImportFrom) and node.level >= 1:
                offenders.append(
                    f"{path.name}: from .{node.module or ''} import "
                    + ", ".join(a.name for a in node.names)
                )
    assert offenders == [], (
        "function-level from-imports between package modules freeze bindings "
        "and detach monkeypatches silently: " + "; ".join(offenders)
    )
