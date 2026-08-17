#!/usr/bin/env python3
"""Regenerate the frontend selectable-model catalog from the Python source (#2086).

Thin wrapper: the emitter lives in ``services.model_catalog.render_js`` so the
CLI and the parity test (``tests/unit/test_2086_model_catalog_parity.py``) share
one importable renderer. CI/dev-only — never read at runtime, never shipped in
any image.

    python scripts/gen_model_catalog.py

Then commit ``src/frontend/src/constants/modelCatalog.js``. Run it whenever you
edit ``src/backend/services/model_catalog.py`` (e.g. when Anthropic ships a new
selectable model — the single-file edit the docs point at).

``model_catalog`` is loaded directly from its file (not ``from services...``) so
this stays a stdlib-only leaf load: the ``services`` package ``__init__`` eagerly
imports Docker/pydantic modules, none of which codegen needs.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SOURCE = _ROOT / "src" / "backend" / "services" / "model_catalog.py"
_OUTPUT = _ROOT / "src" / "frontend" / "src" / "constants" / "modelCatalog.js"


def _load_render_js():
    spec = importlib.util.spec_from_file_location("_model_catalog_leaf", _SOURCE)
    module = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass on Python 3.12+ resolves __module__ via
    # sys.modules, which raises if the dynamically-loaded module isn't present.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.render_js


def main() -> None:
    _OUTPUT.write_bytes(_load_render_js()().encode("utf-8"))
    print(f"wrote {_OUTPUT.relative_to(_ROOT)}")


if __name__ == "__main__":
    main()
