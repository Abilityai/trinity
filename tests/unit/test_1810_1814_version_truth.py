"""
Regression for #1810 + #1814 — `/api/version` must never name a version that
isn't running.

Two distinct ways the old resolver got it wrong, both observed on a real
v0.8.0 → v0.8.5 upgrade:

  #1810  `ARG VERSION=unknown` + an unconditional `ENV` means the env var is
         ALWAYS set — to the literal string "unknown" when no build arg was
         passed. That sentinel is truthy, so `os.getenv("VERSION") or None`
         never fell through and the VERSION-file tier was unreachable for
         exactly the operator it was added for (#993).

  #1814  `start.sh` never rebuilds platform images, so after an in-place
         upgrade the baked env still names the PREVIOUS build while the
         bind-mounted source runs the new code. Observed: `version` reported
         0.8.0+gb3b7078b while migrations 83 → 99 had applied and 0.8.5-only
         routes answered 200.

The resolver is exercised through the module-level payload builder rather than
the route so no app/auth wiring is needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _payload(monkeypatch, tmp_path, *, env_version, file_version):
    """Build the version payload with a controlled env var and VERSION file."""
    import main

    if env_version is None:
        monkeypatch.delenv("VERSION", raising=False)
    else:
        monkeypatch.setenv("VERSION", env_version)

    # Point the first candidate path at a temp file (or nowhere).
    version_file = tmp_path / "VERSION"
    if file_version is not None:
        version_file.write_text(file_version + "\n")

    real_exists = Path.exists
    real_read = Path.read_text

    def fake_exists(self):
        # NB: must call the CAPTURED original on the temp file — calling
        # `version_file.exists()` here would re-enter this patch forever.
        if self.name == "VERSION":
            return real_exists(version_file)
        return real_exists(self)

    def fake_read_text(self, *a, **kw):
        if self.name == "VERSION":
            return real_read(version_file, *a, **kw)
        return real_read(self, *a, **kw)

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setattr(Path, "read_text", fake_read_text)

    return main._build_version_payload(edition="oss", enterprise_features=[], voice_enabled=False)


def test_1810_unknown_sentinel_falls_through_to_the_file(monkeypatch, tmp_path):
    """The bug: a sentinel env var hid a perfectly good baked VERSION file."""
    out = _payload(monkeypatch, tmp_path, env_version="unknown", file_version="0.8.5")
    assert out["version"] == "0.8.5"
    assert out["components"]["backend"] == "0.8.5"


def test_1810_absent_env_uses_the_file(monkeypatch, tmp_path):
    out = _payload(monkeypatch, tmp_path, env_version=None, file_version="0.8.5")
    assert out["version"] == "0.8.5"


def test_1814_stale_image_reports_the_running_code(monkeypatch, tmp_path):
    """In-place upgrade: file (live bind mount) wins over the stale baked env."""
    out = _payload(
        monkeypatch, tmp_path, env_version="0.8.0+gb3b7078b", file_version="0.8.5"
    )
    assert out["version"] == "0.8.5", "must report the code that is running"
    assert out["image_version"] == "0.8.0+gb3b7078b", "drift must stay visible"


def test_matching_build_keeps_the_git_suffix(monkeypatch, tmp_path):
    """A normally-built stack must keep the richer git-stamped value."""
    out = _payload(
        monkeypatch, tmp_path, env_version="0.8.5+gb8cf94ca", file_version="0.8.5"
    )
    assert out["version"] == "0.8.5+gb8cf94ca"
    assert out["image_version"] == "0.8.5+gb8cf94ca"


def test_no_stamp_and_no_file_is_still_unknown(monkeypatch, tmp_path):
    """The honest fallback survives — this must not become a crash or a lie."""
    out = _payload(monkeypatch, tmp_path, env_version="unknown", file_version=None)
    assert out["version"] == "unknown"
    assert out["image_version"] is None


def test_env_only_still_works(monkeypatch, tmp_path):
    """Prod-style: env stamped, no readable file — unchanged behaviour."""
    out = _payload(
        monkeypatch, tmp_path, env_version="0.8.5+gb8cf94ca", file_version=None
    )
    assert out["version"] == "0.8.5+gb8cf94ca"
