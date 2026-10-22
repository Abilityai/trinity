"""Base image + startup.sh container files for the OpenCode runtime (#2981, checkpoint F).

Two halves:

* **startup.sh, executed.** The Codex ``AGENTS.md`` mirror and the OpenCode
  seeding block are cut out of the SHIPPED ``docker/base-image/startup.sh`` by
  text and run under ``bash`` against a tmp ``$HOME`` / ``$AGENT_TMPDIR`` /
  pre-seed dir — so these tests exercise the shell that ships, not a re-typed
  copy. The OpenCode block must write exactly the directory the harness reads
  (``opencode_runtime._shared_config_dir()``), and the mirror must be a regular
  file, a copy refreshed on every start, for ``codex`` only (rulings T8, R15) —
  never a link in the agent home, which the Files surface refuses (ent#819).
* **Dockerfile, static.** There is no docker on the unit island and the image
  build is the merge gate, so the Dockerfile is pinned by text — its live
  consumer is the image build itself: the OpenCode pin equals the version the
  runtime parser was measured against; the platform package is chosen per
  ``uname -m`` with a loud default; the ``opencode --version`` assertion sits in
  the SAME RUN as the install (the #2537 rule); the pre-seeded provider set is
  exactly what the runtime can ask OpenCode to load, each at an exact version;
  and the Go tarball is no longer hardcoded to linux-amd64 (ruling T1).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

# conftest wires the real docker/base-image/agent_server as `agent_server`.
from agent_server.services import opencode_runtime  # noqa: E402
from agent_server.services.opencode_provider import (  # noqa: E402
    NATIVE_KEY_ENV,
    NPM_ALLOWLIST,
)

_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _ROOT / "docker/base-image/Dockerfile"
_STARTUP = _ROOT / "docker/base-image/startup.sh"
_RUNTIME = _ROOT / "docker/base-image/agent_server/services/opencode_runtime.py"

# The driver package OpenCode v1.18.32 loads for each native provider the
# runtime pre-checks (models.dev `npm` for that provider; all three are in
# BUNDLED_PROVIDERS, provider.ts:116/122/124 at v1.18.32).
_NATIVE_DRIVER = {
    "anthropic": "@ai-sdk/anthropic",
    "openai": "@ai-sdk/openai",
    "openrouter": "@openrouter/ai-sdk-provider",
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _run_blocks(env: dict, home: Path, seed: Path) -> subprocess.CompletedProcess:
    """Run the runtime-setup section of the shipped startup.sh (from the
    lowercased-runtime derivation through the end of the OpenCode block)."""
    src = _STARTUP.read_text()
    start = src.index('_agent_runtime_lc="$(')
    end = src.index("\n# Ensure core agent-server dependencies", start)
    block = (
        src[start:end]
        .replace("/home/developer", str(home))
        .replace("/opt/trinity/opencode", str(seed))
    )
    full_env = {"PATH": os.environ["PATH"], **env}
    return subprocess.run(
        ["bash", "-c", block], capture_output=True, text=True, timeout=30, env=full_env
    )


@pytest.fixture
def box(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    tmpdir = home / ".tmp"
    tmpdir.mkdir()
    seed = tmp_path / "seed"
    (seed / "node_modules/@ai-sdk/openai-compatible").mkdir(parents=True)
    (seed / "package.json").write_text('{"name":"seed"}\n')
    (seed / "package-lock.json").write_text('{"lockfileVersion":3}\n')
    return home, tmpdir, seed


def _run_layers() -> list[str]:
    """Each Dockerfile instruction as one logical line (continuations joined)."""
    text = _DOCKERFILE.read_text()
    joined = re.sub(r"\\\n", " ", text)
    return [ln for ln in joined.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _layer_containing(needle: str) -> str:
    hits = [ln for ln in _run_layers() if ln.startswith("RUN ") and needle in ln]
    assert len(hits) == 1, f"expected exactly one RUN layer containing {needle!r}, got {len(hits)}"
    return hits[0]


# --------------------------------------------------------------------------
# startup.sh — OpenCode block, executed
# --------------------------------------------------------------------------

def test_opencode_block_writes_the_dir_the_harness_reads(box, monkeypatch):
    home, tmpdir, seed = box
    proc = _run_blocks({"AGENT_RUNTIME": "opencode", "AGENT_TMPDIR": str(tmpdir)}, home, seed)
    assert proc.returncode == 0, proc.stderr

    # The harness resolves its root from TMPDIR (AGENT_TMPDIR derives from it in
    # startup.sh) with XDG_CONFIG_HOME unset in the agent-server process.
    for var in ("OPENCODE_ROOT", "AGENT_TMPDIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    shared = Path(opencode_runtime._shared_config_dir())
    assert shared == tmpdir / "opencode/config/opencode"

    cfg = shared / "opencode.json"
    assert cfg.is_file()
    assert opencode_runtime._load_shared_config()["share"] == "disabled"
    assert opencode_runtime._load_shared_config()["autoupdate"] is False
    assert (shared / "node_modules").is_symlink()
    assert os.readlink(shared / "node_modules") == str(seed / "node_modules")
    assert (shared / "package.json").read_text() == '{"name":"seed"}\n'
    assert (shared / "package-lock.json").is_file()
    assert ((tmpdir / "opencode").stat().st_mode & 0o777) == 0o700


def test_opencode_block_matches_runtime_case_insensitively(box):
    """#1187 casing trap: a capitalised AGENT_RUNTIME still seeds the dir."""
    home, tmpdir, seed = box
    proc = _run_blocks({"AGENT_RUNTIME": "OpenCode", "AGENT_TMPDIR": str(tmpdir)}, home, seed)
    assert proc.returncode == 0, proc.stderr
    assert (tmpdir / "opencode/config/opencode/opencode.json").is_file()


def test_opencode_json_is_written_only_if_absent(box):
    home, tmpdir, seed = box
    cfg = tmpdir / "opencode/config/opencode/opencode.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('{"share": "manual", "operator": true}\n')
    proc = _run_blocks({"AGENT_RUNTIME": "opencode", "AGENT_TMPDIR": str(tmpdir)}, home, seed)
    assert proc.returncode == 0, proc.stderr
    assert cfg.read_text() == '{"share": "manual", "operator": true}\n'


def test_opencode_block_exports_nothing_into_the_shell():
    """Ruling T3: the XDG relocation and the OPENCODE_DISABLE_* switches are
    per-turn, on the subprocess env only. A global export would leak them into
    the agent server and every other process the container starts."""
    src = _STARTUP.read_text()
    start = src.index('if [ "$_agent_runtime_lc" = "opencode" ]; then')
    end = src.index("\n# Ensure core agent-server dependencies", start)
    block = src[start:end]
    assert not re.search(r"^\s*export\s+XDG_", block, re.M)
    assert not re.search(r"^\s*export\s+OPENCODE_", block, re.M)
    assert '[ ! -f "${OPENCODE_CONFIG_DIR}/opencode.json" ]' in block
    assert "chmod 700" in block


def test_harness_sets_every_disable_switch_per_turn():
    """Because startup.sh exports none, the harness must set all of them."""
    src = _RUNTIME.read_text()
    for key in (
        "OPENCODE_DISABLE_AUTOUPDATE",
        "OPENCODE_DISABLE_LSP_DOWNLOAD",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
    ):
        assert f'"{key}":' in src, key


def test_opencode_block_skipped_for_other_runtimes(box):
    home, tmpdir, seed = box
    for runtime in ("claude-code", "codex", "gemini-cli", ""):
        proc = _run_blocks({"AGENT_RUNTIME": runtime, "AGENT_TMPDIR": str(tmpdir)}, home, seed)
        assert proc.returncode == 0, proc.stderr
        assert not (tmpdir / "opencode").exists(), runtime


# --------------------------------------------------------------------------
# startup.sh — AGENTS.md mirror, codex only (ruling T8), executed
# --------------------------------------------------------------------------

def _mirror(box, runtime="codex"):
    home, tmpdir, seed = box
    proc = _run_blocks({"AGENT_RUNTIME": runtime, "AGENT_TMPDIR": str(tmpdir)}, home, seed)
    assert proc.returncode == 0, proc.stderr
    return home, proc.stdout


def _stamp(box) -> Path:
    return box[1] / ".agents-md-mirror.last"


def test_mirror_creates_a_regular_file_and_stamps_it(box):
    home = box[0]
    (home / "CLAUDE.md").write_text("v1\n")
    _, out = _mirror(box)
    mirror = home / "AGENTS.md"
    assert not mirror.is_symlink()  # Files never follows a link (ent#819)
    assert mirror.is_file()
    assert mirror.read_text() == "v1\n"
    assert _stamp(box).read_text() == "v1\n"
    assert "Mirrored CLAUDE.md -> AGENTS.md" in out


def test_mirror_tracks_later_edits(box):
    """The defect: a copy froze at first boot. The copy is refreshed every
    start while it still equals the stamp of the last copy written."""
    home = box[0]
    (home / "CLAUDE.md").write_text("v1\n")
    _mirror(box)
    (home / "CLAUDE.md").write_text("v2 — edited by a git sync\n")
    _mirror(box)
    assert not (home / "AGENTS.md").is_symlink()
    assert (home / "AGENTS.md").read_text() == "v2 — edited by a git sync\n"
    assert _stamp(box).read_text() == "v2 — edited by a git sync\n"


def test_mirror_matches_codex_case_insensitively(box):
    home = box[0]
    (home / "CLAUDE.md").write_text("v1\n")
    _mirror(box, runtime="Codex")
    assert (home / "AGENTS.md").is_file()
    assert (home / "AGENTS.md").read_text() == "v1\n"


@pytest.mark.parametrize("target", ["CLAUDE.md", "SOMEWHERE_ELSE.md"])
def test_mirror_replaces_a_symlink_with_a_regular_file(box, target):
    """Includes this branch's own earlier image, which linked to CLAUDE.md."""
    home = box[0]
    (home / "CLAUDE.md").write_text("v1\n")
    (home / "AGENTS.md").symlink_to(target)
    _mirror(box)
    assert not (home / "AGENTS.md").is_symlink()
    assert (home / "AGENTS.md").read_text() == "v1\n"
    assert (home / "CLAUDE.md").read_text() == "v1\n"  # not written through


def test_mirror_adopts_a_stale_identical_copy(box):
    home = box[0]
    (home / "CLAUDE.md").write_text("v1\n")
    (home / "AGENTS.md").write_text("v1\n")
    _mirror(box)
    assert not (home / "AGENTS.md").is_symlink()
    assert (home / "AGENTS.md").read_text() == "v1\n"
    assert _stamp(box).read_text() == "v1\n"
    # adopted: a later CLAUDE.md edit now reaches it
    (home / "CLAUDE.md").write_text("v2\n")
    _mirror(box)
    assert (home / "AGENTS.md").read_text() == "v2\n"


def test_mirror_leaves_an_authored_agents_md_alone_but_says_so(box):
    home = box[0]
    (home / "CLAUDE.md").write_text("claude instructions\n")
    (home / "AGENTS.md").write_text("hand-written codex instructions\n")
    _stamp(box).write_text("an older mirrored copy\n")
    _, out = _mirror(box)
    assert not (home / "AGENTS.md").is_symlink()
    assert (home / "AGENTS.md").read_text() == "hand-written codex instructions\n"
    assert "differs" in out.lower()


def test_mirror_is_idempotent(box):
    home = box[0]
    (home / "CLAUDE.md").write_text("v1\n")
    _, first = _mirror(box)
    _, second = _mirror(box)
    assert "Mirrored CLAUDE.md" in first
    assert "Mirrored CLAUDE.md" not in second
    assert "Warning" not in second
    assert (home / "AGENTS.md").read_text() == "v1\n"


def test_mirror_noops_without_claude_md(box):
    home = box[0]
    _mirror(box)
    assert not (home / "AGENTS.md").exists()


def test_startup_sh_creates_no_link_in_the_agent_home():
    """R16: the ent819 allowance for startup.sh is per file, so pin what it
    admits — exactly one ``ln``, the OpenCode ``node_modules`` link under the
    ``${OPENCODE_CONFIG_DIR}`` scratch root, and no link named under
    ``/home/developer/`` outside a ``${...}`` scratch variable."""
    ln_lines = [
        ln for ln in _STARTUP.read_text().splitlines() if re.match(r"^\s*ln\s", ln)
    ]
    for ln in ln_lines:
        args = []
        for tok in ln.split()[1:]:
            if tok in ("&&", "||", ";", "|", "\\") or re.match(r"^\d*>", tok):
                break  # end of the ln command itself
            if not tok.startswith("-"):
                args.append(tok.strip("'\""))
        link_name = args[-1]
        if not link_name.startswith("${"):  # a scratch variable, e.g. OPENCODE_CONFIG_DIR
            assert not link_name.startswith(("/home/developer", "~", "$HOME")), ln
            assert "/" in link_name, f"relative link name in the home cwd: {ln}"
    assert len(ln_lines) == 1, ln_lines
    assert "node_modules" in ln_lines[0]
    assert "${OPENCODE_CONFIG_DIR}" in ln_lines[0]


@pytest.mark.parametrize("runtime", ["opencode", "OpenCode", "claude-code", "gemini-cli"])
def test_mirror_is_codex_only(box, runtime):
    """Ruling T8: OpenCode reads CLAUDE.md natively — no AGENTS.md for it."""
    home = box[0]
    (home / "CLAUDE.md").write_text("v1\n")
    _mirror(box, runtime=runtime)
    assert not (home / "AGENTS.md").exists()


def test_startup_sh_parses():
    proc = subprocess.run(["bash", "-n", str(_STARTUP)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------------------
# Dockerfile — OpenCode install layer
# --------------------------------------------------------------------------

def _opencode_pin() -> str:
    m = re.search(r"^ARG OPENCODE_VERSION=(\S+)$", _DOCKERFILE.read_text(), re.M)
    assert m, "ARG OPENCODE_VERSION missing from the base image"
    return m.group(1)


def test_opencode_pin_equals_the_version_the_parser_was_measured_against():
    """The runtime carries no version constant; its parser cites the version it
    was measured against (`v1.18.32`) throughout. Every cited version must be
    the Dockerfile pin — bumping one without the other fails here."""
    pin = _opencode_pin()
    assert pin == "1.18.32"
    cited = set(re.findall(r"\bv(\d+\.\d+\.\d+)\b", _RUNTIME.read_text()))
    assert cited == {pin}


def test_opencode_layer_maps_both_arches_and_fails_loudly_otherwise():
    layer = _layer_containing('"opencode-ai@${OPENCODE_VERSION}"')
    assert layer.startswith("RUN set -eu;")
    assert "x86_64)  ocarch=x64 ;;" in layer
    assert "aarch64) ocarch=arm64 ;;" in layer
    assert re.search(r"\*\) echo [^;]*FATAL[^;]*;\s*exit 1 ;;", layer)
    assert '"opencode-linux-${ocarch}@${OPENCODE_VERSION}"' in layer
    assert "--no-ignore-scripts" in layer


def test_opencode_version_assertion_is_in_the_install_layer():
    """#2537 rule: a separate layer would still cache and publish a broken install."""
    layer = _layer_containing('"opencode-ai@${OPENCODE_VERSION}"')
    install = layer.index("npm install -g")
    check = layer.index("opencode --version")
    assert check > install
    assert '*"${OPENCODE_VERSION}"*) : ;;' in layer[check:]
    assert "exit 1" in layer[check:]


def test_opencode_installs_before_the_developer_user_exists():
    text = _DOCKERFILE.read_text()
    assert text.index("ARG OPENCODE_VERSION=") < text.index("useradd -m -s /bin/bash -u 1000 developer")


# --------------------------------------------------------------------------
# Dockerfile — pre-seeded provider packages
# --------------------------------------------------------------------------

def _seeded_packages() -> dict[str, str]:
    layer = _layer_containing("/opt/trinity/opencode")
    m = re.search(r"npm install (.*?);", layer)
    assert m
    specs = [t for t in m.group(1).split() if not t.startswith("--")]
    out = {}
    for spec in specs:
        name, _, version = spec.rpartition("@")
        assert name, f"unpinned provider package: {spec}"
        out[name] = version
    return out


def test_seeded_set_is_exactly_what_the_runtime_can_load():
    expected = set(NPM_ALLOWLIST) | {_NATIVE_DRIVER[p] for p in NATIVE_KEY_ENV}
    assert set(_seeded_packages()) == expected


def test_seeded_packages_are_exact_versions():
    for name, version in _seeded_packages().items():
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"{name}@{version} is not an exact pin"
    layer = _layer_containing("/opt/trinity/opencode")
    assert "--save-exact" in layer
    for name in _seeded_packages():
        assert f"test -d node_modules/{name};" in layer
    assert "chmod -R a+rX /opt/trinity/opencode" in layer


# --------------------------------------------------------------------------
# Dockerfile — Go tarball per arch (ruling T1)
# --------------------------------------------------------------------------

def test_go_tarball_is_resolved_per_arch():
    assert not any("linux-amd64.tar.gz" in ln for ln in _run_layers())
    layer = _layer_containing("go.dev/dl/")
    assert layer.startswith("RUN set -eu;")
    assert "x86_64)  goarch=amd64 ;;" in layer
    assert "aarch64) goarch=arm64 ;;" in layer
    assert re.search(r"\*\) echo [^;]*FATAL[^;]*;\s*exit 1 ;;", layer)
    assert "go1.23.4.linux-${goarch}.tar.gz" in layer
    # Verified by running the binary in the same layer.
    assert layer.index("go version") > layer.index("tar -C /usr/local")
    assert '*"linux/${goarch}"*) : ;;' in layer
