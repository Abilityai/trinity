"""The OpenCode template fixture and its docs stay true to the code (#2981, checkpoint I).

Related flow: docs/memory/feature-flows/opencode-runtime.md

What is executed, not just read:

  * ``test-opencode/template.yaml`` is parsed and its ``runtime.model`` goes
    through the backend's real ``validate_runtime_model`` — the same call
    ``crud.create_agent_internal`` makes — so the shipped fixture cannot pin a
    model string the create path would 400.
  * The COMMENTED custom-provider example in that template is extracted,
    un-commented, parsed and run through the real ``validate_runtime_provider``
    (and paired with its documented ``model:`` line through
    ``validate_runtime_model``), so the example a reader copies can never drift
    from the shape the code accepts (I1).

The remaining pins are documentation presence checks whose live consumer is a
reader, not code: the requirements headings, the feature-flow index rows, the
user-doc mentions, and a lab-identifier scrub over every file this checkpoint
added or changed.
"""

from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException

# Same stub-eviction idiom as test_2981_backend_registration.py: sibling unit
# modules may replace `services.agent_service[.helpers]` with stand-ins in
# sys.modules at collection time; evict only a stand-in so the real module binds.
_STUBBED_MODULE_NAMES = [
    "services.agent_service.helpers",
    "services.agent_service.read_only",
    "services.agent_service.file_sharing",
    "services.agent_service",
]
_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
_REAL_AGENT_SERVICE_DIR = str(_BACKEND / "services" / "agent_service")


def _is_stand_in(module) -> bool:
    path = getattr(module, "__file__", None)
    return not (path and str(Path(path).resolve()).startswith(_REAL_AGENT_SERVICE_DIR))


for _stub in _STUBBED_MODULE_NAMES:  # import-time eviction (monkeypatch can't reach)
    if _stub in sys.modules and _is_stand_in(sys.modules[_stub]):
        sys.modules.pop(_stub, None)

from services.agent_service.helpers import (  # noqa: E402  (must follow the eviction)
    validate_runtime_model,
    validate_runtime_provider,
)


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot/restore the stubbed subtree around each test (the sanctioned
    escape hatch the sys.modules lint recognizes; see test_validate_runtime)."""
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


TEMPLATE = _REPO / "config" / "agent-templates" / "test-opencode" / "template.yaml"
TEMPLATES_README = _REPO / "config" / "agent-templates" / "README.md"
RUNTIMES_REQ = _REPO / "docs" / "memory" / "requirements" / "runtimes.md"
REQ_INDEX = _REPO / "docs" / "memory" / "requirements.md"
ARCH = _REPO / "docs" / "memory" / "architecture" / "agent-runtime.md"
FLOWS_INDEX = _REPO / "docs" / "memory" / "feature-flows.md"
FLOW = _REPO / "docs" / "memory" / "feature-flows" / "opencode-runtime.md"
USER_RUNTIMES = _REPO / "docs" / "user-docs" / "agents" / "agent-runtimes.md"
USER_HOWTO = _REPO / "docs" / "user-docs" / "agents" / "opencode-runtime.md"
USER_README = _REPO / "docs" / "user-docs" / "README.md"
USER_OVERVIEW = _REPO / "docs" / "user-docs" / "getting-started" / "overview.md"
USER_FAQ_START = _REPO / "docs" / "user-docs" / "faq" / "getting-started.md"

PROVIDER_MARKER = (
    "# Custom OpenAI-compatible provider (optional) — uncomment to run on a "
    "local or self-hosted endpoint:"
)


def _template() -> dict:
    return yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))


def _commented_provider_example() -> dict:
    """Un-comment the contiguous ``# `` block under the marker and parse it."""
    lines = TEMPLATE.read_text(encoding="utf-8").splitlines()
    assert PROVIDER_MARKER in lines, "the custom-provider marker line is missing"
    start = lines.index(PROVIDER_MARKER) + 1
    block = []
    for line in lines[start:]:
        if not line.startswith("# "):
            break
        block.append(line[2:])
    assert block, "no commented example under the custom-provider marker"
    parsed = yaml.safe_load(textwrap.dedent("\n".join(block)))
    assert isinstance(parsed, dict) and "provider" in parsed
    return parsed["provider"]


# --- (a) the fixture itself --------------------------------------------------

def test_template_is_a_hidden_opencode_fixture_on_openrouter():
    data = _template()
    assert data["name"] == "test-opencode"
    assert data["hidden"] is True
    runtime = data["runtime"]
    assert runtime["type"] == "opencode"
    model = runtime["model"]
    assert model.startswith("openrouter/")
    # The create path's own check; raises HTTPException(400) on a bad shape.
    validate_runtime_model(runtime["type"], model)
    # No live provider block: OpenRouter is a models.dev provider, rendered natively.
    assert "provider" not in runtime
    validate_runtime_provider(runtime["type"], runtime.get("provider"))


def test_template_declares_the_openrouter_key_it_needs():
    data = _template()
    assert "OPENROUTER_API_KEY" in data["credentials"]["env_file"]
    names = [entry["name"] for entry in data["credential_setup"]]
    assert "OPENROUTER_API_KEY" in names
    entry = next(e for e in data["credential_setup"] if e["name"] == "OPENROUTER_API_KEY")
    assert entry["setup_url"] == "https://openrouter.ai/keys"


# --- (b) the commented example is executable-true ----------------------------

def test_commented_provider_example_passes_the_backend_validator():
    block = _commented_provider_example()
    validate_runtime_provider("opencode", block)
    assert block["base_url"] == "http://host.docker.internal:11434/v1"
    # The model line the comment tells the reader to set must pass too.
    model_id = next(iter(block["models"]))
    validate_runtime_model("opencode", f"{block['id']}/{model_id}")


def test_validator_rejects_a_drifted_example():
    """Guard the guard: the validator the example runs through is not a no-op."""
    block = dict(_commented_provider_example(), npm="some-unlisted-driver")
    with pytest.raises(HTTPException) as exc:
        validate_runtime_provider("opencode", block)
    assert exc.value.status_code == 400 and "npm" in str(exc.value.detail)


# --- (c)–(f) the documentation surfaces ---------------------------------------

def test_templates_readme_lists_the_fixture():
    fixtures = next(
        line for line in TEMPLATES_README.read_text(encoding="utf-8").splitlines()
        if line.startswith("- **Test/canary fixtures**")
    )
    assert "`test-opencode`" in fixtures


def test_requirements_section_42_has_all_six_subsections():
    text = RUNTIMES_REQ.read_text(encoding="utf-8")
    assert re.search(r"^## 42\. Multi-Runtime Harnesses — OpenCode \(#2981\)$", text, re.M)
    for n in range(1, 7):
        assert re.search(rf"^### 42\.{n} ", text, re.M), f"### 42.{n} heading missing"


def test_feature_flow_is_indexed_and_has_a_verification_status():
    index = FLOWS_INDEX.read_text(encoding="utf-8")
    assert index.count("feature-flows/opencode-runtime.md") >= 2
    assert FLOW.exists()
    assert re.search(r"^## Verification status$", FLOW.read_text(encoding="utf-8"), re.M)


def test_user_docs_name_opencode():
    for path in (USER_RUNTIMES, USER_README):
        assert "OpenCode" in path.read_text(encoding="utf-8"), path
    assert USER_HOWTO.exists()
    assert "agents/opencode-runtime.md" in USER_README.read_text(encoding="utf-8")


# --- (g) lab-identifier scrub -------------------------------------------------

CHECKPOINT_FILES = [
    TEMPLATE, TEMPLATES_README, RUNTIMES_REQ, REQ_INDEX, ARCH, FLOWS_INDEX, FLOW,
    USER_RUNTIMES, USER_HOWTO, USER_README, USER_OVERVIEW, USER_FAQ_START,
    Path(__file__),
]
_LAB = re.compile(r"(?i)lilu|dgx|tailscale|172\.28\.")
# Pre-existing PUBLIC text a scanned file already carried before #2981: the
# documented agent-network subnet (also in architecture.md's Network Topology).
# Removed verbatim before scanning, so only a NEW occurrence is a hit.
_PUBLIC_PREEXISTING = {
    USER_OVERVIEW: ["| Agent Network | Isolated bridge | 172.28.0.0/16 |"],
}


@pytest.mark.parametrize("path", CHECKPOINT_FILES, ids=lambda p: p.name)
def test_no_lab_identifiers(path):
    text = path.read_text(encoding="utf-8")
    if path == Path(__file__):
        text = text.replace(_LAB.pattern, "")
        for exempt in _PUBLIC_PREEXISTING.values():
            for known in exempt:
                text = text.replace(known, "")
    for known in _PUBLIC_PREEXISTING.get(path, []):
        assert known in text, f"stale exemption for {path.name}: {known!r}"
        text = text.replace(known, "", 1)
    hits = sorted({m.group(0) for m in _LAB.finditer(text)})
    assert not hits, f"{path.relative_to(_REPO)} carries lab identifiers: {hits}"
