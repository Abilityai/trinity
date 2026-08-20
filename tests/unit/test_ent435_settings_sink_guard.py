"""ent#435 — anti-recurrence guard: every ``system_settings`` writer is gated.

The value of ent#435 is not that six rows got encrypted once; it is that a live
credential **cannot quietly go back** into a cleartext settings row. The reason
that could happen at all is that `system_settings` has more than one writer:
``db/settings.py:set_setting`` is the canonical sink, but ``client_portal/db.py``
and two enterprise modules each carry their own near-verbatim upsert, and the
generic ``PUT /api/settings/{key}`` catch-all can address ANY key. A guard on
one route, or even on one function, is one copy-paste away from irrelevance —
which is precisely how the same catch-all door was found standing open by #506,
#1609, ent#12, #1644, ent#14 and ent#346 in turn.

So this pins the sink set itself. Two invariants:

1. **Every upsert into ``system_settings`` is accounted for** — it either calls
   ``assert_plaintext_write_allowed`` first, or it is named below with the
   reason it cannot carry a credential. A NEW unlisted writer fails here,
   forcing the author to gate it or justify the exemption in one place.

2. **The known-secret key set is never silently shrunk.** Dropping a key from
   ``SECRET_SETTING_KEYS`` un-gates it AND strands whatever the migration
   already moved to ``<key>_encrypted`` (the reader would stop looking there),
   so the removal must be deliberate enough to edit this list too.

OSS tree only. The two enterprise sinks live in the private submodule, which
public CI never checks out; that repo owns its own twin of this guard — the
#1677 caller-parity convention.

Mirrors the repo's static-guard convention (#1560 keyspace parity, #1677 alert
emitters, #1920 single-flight, #293 admin-gate spelling).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"

_GUARD_CALL = "assert_plaintext_write_allowed"

#: Files that write ``system_settings`` without the guard, and why that is safe.
#: Adding a new general-purpose settings writer means CALLING the guard — not
#: extending this map.
_UNGATED_WRITERS = {
    "database.py": (
        "#1638 fresh-install retention seed — writes only the fixed "
        "COMMUNITY_FRESH_INSTALL_SEED integers, and runs inside init_database() "
        "at import where a raise would crash-loop boot (the seed is explicitly "
        "fail-safe). Not a general-purpose sink: it cannot be handed a key."
    ),
    "db/migrations.py": (
        "the ent#435 sweep itself — it WRITES the encrypted rows, so gating it "
        "on the guard would be circular. It also runs on the raw sqlite3 cursor, "
        "below the SQLAlchemy sink entirely."
    ),
    "migrations/versions/0041_secret_settings_encryption.py": (
        "the PostgreSQL half of the same sweep, same reasoning."
    ),
}


def _py_files():
    for path in _BACKEND.rglob("*.py"):
        rel = path.relative_to(_BACKEND).as_posix()
        if rel.startswith(("enterprise/", "tests/", "__pycache__/")):
            continue
        yield rel, path


def _writes_system_settings(tree: ast.AST) -> bool:
    """True if the module upserts into ``system_settings``.

    Matches both shapes in the tree: the SQLAlchemy ``make_insert(system_settings)``
    call and a raw ``INSERT INTO system_settings`` string constant.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "make_insert"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "system_settings"
        ):
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "INSERT INTO system_settings" in node.value:
                return True
    return False


def _calls_guard(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == _GUARD_CALL:
                return True
    return False


def test_every_system_settings_writer_is_gated_or_listed():
    offenders = []
    listed_but_absent = set(_UNGATED_WRITERS)

    for rel, path in _py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        if not _writes_system_settings(tree):
            continue
        listed_but_absent.discard(rel)
        if rel in _UNGATED_WRITERS or _calls_guard(tree):
            continue
        offenders.append(rel)

    assert not offenders, (
        "New `system_settings` writer(s) with no cleartext-credential guard: "
        f"{sorted(offenders)}. Call "
        "`services.secret_settings.assert_plaintext_write_allowed(key)` before "
        "the upsert, or add the file to _UNGATED_WRITERS with the reason it "
        "cannot carry a credential (ent#435)."
    )
    assert not listed_but_absent, (
        "_UNGATED_WRITERS names file(s) that no longer write system_settings: "
        f"{sorted(listed_but_absent)}. Drop the stale entry so the exemption "
        "list stays an accurate inventory."
    )


def test_canonical_and_clone_sinks_are_gated():
    """The two OSS general-purpose sinks specifically — the ones a caller can
    hand an arbitrary key. Pinned by name so a refactor that drops the guard
    from either fails with a message naming it, not a generic count."""
    for rel in ("db/settings.py", "client_portal/db.py"):
        tree = ast.parse((_BACKEND / rel).read_text(encoding="utf-8"))
        assert _calls_guard(tree), (
            f"{rel} upserts system_settings for a caller-supplied key and must "
            f"call {_GUARD_CALL} (ent#435)."
        )


def test_secret_key_set_is_not_silently_shrunk():
    import sys

    sys.path.insert(0, str(_BACKEND))
    from services.secret_settings import SECRET_SETTING_KEYS

    # The six rows ent#435 found in cleartext. Removing one un-gates it AND
    # strands the already-migrated `<key>_encrypted` row, so it must be a
    # deliberate edit here too.
    required = {
        "anthropic_api_key",
        "github_pat",
        "google_api_key",
        "slack_app_token",
        "slack_client_secret",
        "slack_signing_secret",
    }
    missing = required - set(SECRET_SETTING_KEYS)
    assert not missing, (
        f"SECRET_SETTING_KEYS no longer covers {sorted(missing)} — these were "
        "reported as cleartext credentials in ent#435 and their encrypted rows "
        "already exist on deployed installs."
    )


def test_slack_client_id_is_a_documented_exemption_not_an_omission():
    """`slack_client_id` is credential-SHAPED but public (it goes verbatim into
    the browser-visible OAuth authorize URL). It must be an explicit, reasoned
    exemption rather than something that merely happens not to be in the set —
    otherwise a later reader cannot tell "reviewed and safe" from "overlooked"."""
    import sys

    sys.path.insert(0, str(_BACKEND))
    from services.secret_settings import (
        PUBLIC_CREDENTIAL_SHAPED_KEYS,
        SECRET_SETTING_KEYS,
    )

    assert "slack_client_id" not in SECRET_SETTING_KEYS
    assert PUBLIC_CREDENTIAL_SHAPED_KEYS.get("slack_client_id"), (
        "slack_client_id must carry a written reason in "
        "PUBLIC_CREDENTIAL_SHAPED_KEYS."
    )
