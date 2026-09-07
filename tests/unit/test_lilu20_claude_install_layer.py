"""The base image must not be able to ship a Claude Code CLI that cannot run.

Abilityai/lilu#20. `npm install -g @anthropic-ai/claude-code` exits 0 even when
the platform-native optionalDependency never landed: npm reports a failure to
fetch or unpack an *optional* dependency as a `warn` (measured: a full ENOSPC
during the 214 MB unpack produces `npm warn tar TAR_ENTRY_ERROR` and exit 0),
and the package's own postinstall `install.cjs` then `return`s -- also exit 0 --
leaving `bin/claude.exe` as the 500-byte, shebang-less placeholder stub.

The layer succeeds, the image is tagged `latest`, and the first symptom is a 503
at an agent's first turn. That is what happened on the arm64 DGX.

These tests pin the three defences the install layer now carries. They are
static: they read the Dockerfile, because the property being protected is a
property of the build, and a test that needed a real `docker build` would not
run in CI at all.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "docker" / "base-image" / "Dockerfile"

# Tokens that would make the guard arch-conditional. npm's silent-optional
# behaviour is not arch-specific -- arm64 is only where it happened to bite --
# so a check that only runs on one arch protects the wrong half of the fleet.
_ARCH_LITERALS = ("arm64", "aarch64", "amd64", "x86_64", "TARGETARCH", "TARGETPLATFORM")


def _claude_install_layer() -> str:
    """The single RUN layer that installs Claude Code, joined into one string."""
    lines = DOCKERFILE.read_text().splitlines()
    start = None
    for i, line in enumerate(lines):
        if "npm install" not in line or "@anthropic-ai/claude-code" not in line:
            continue
        # The install may sit on a continuation line rather than the RUN line.
        j = i
        while j > 0 and lines[j - 1].rstrip().endswith("\\"):
            j -= 1
        if lines[j].startswith("RUN "):
            start = j
            break
    assert start is not None, "no RUN layer installs @anthropic-ai/claude-code"
    end = start
    while lines[end].rstrip().endswith("\\"):
        end += 1
    return "\n".join(lines[start:end + 1])


@pytest.fixture(scope="module")
def layer() -> str:
    return _claude_install_layer()


class TestVerificationDefence:
    """Defence (c): the build must fail rather than tag a base with a stub."""

    def test_layer_runs_the_installed_cli(self, layer):
        assert "claude --version" in layer, (
            "the claude-code install layer must prove the CLI runs -- npm exits 0 "
            "with a placeholder stub, so an unverified install can tag a broken "
            "base image as `latest` (Abilityai/lilu#20)"
        )

    def test_verification_is_the_last_command_and_unguarded(self, layer):
        """It must be the layer's terminal, bare, unconditional command.

        A check that is `|| true`-ed, backgrounded, or wrapped in a command
        substitution (`echo "$(claude --version)"` -- the classic `set -e` mask,
        where the failure is hidden by echo's own exit 0) does not fail a build.
        """
        final = layer.rstrip().splitlines()[-1].strip()
        assert final == "claude --version", (
            f"the layer must END with a bare `claude --version`, got: {final!r}"
        )
        assert not re.search(r"\$\(\s*claude --version", layer), (
            "`$(claude --version)` masks the failure -- `set -e` sees only the "
            "exit status of the enclosing command"
        )

    def test_layer_aborts_on_any_failing_command(self, layer):
        assert re.search(r"^RUN set -eu?x?;", layer), (
            "without `set -e` an intermediate failure would not stop the layer"
        )

    def test_verification_shares_the_layer_with_the_install(self, layer):
        """A separate RUN would still cache and publish the broken install."""
        assert "npm install" in layer and "@anthropic-ai/claude-code" in layer
        assert layer.count("RUN ") == 1

    def test_layer_is_arch_neutral(self, layer):
        for token in _ARCH_LITERALS:
            assert token not in layer, (
                f"{token!r} in the install layer: the guard must not be "
                "arch-conditional -- npm's silent failure is arch-independent"
            )


class TestSuppressionDefence:
    """Defence (a): the two documented suppressors are overridden on the CLI.

    Neither was actually passed on the DGX -- nothing is COPYed into the image
    before this layer and no NPM_CONFIG_* is set -- but npm config precedence
    puts CLI flags above every npmrc and env source, so stating them explicitly
    is what makes that true for every future base image and build host as well.
    """

    def test_optional_dependencies_are_explicitly_included(self, layer):
        assert "--include=optional" in layer

    def test_install_scripts_are_explicitly_enabled(self, layer):
        # `install.cjs` runs as a postinstall; --ignore-scripts alone reproduces
        # the 500-byte stub with exit 0 (measured).
        assert "--no-ignore-scripts" in layer


class TestNativePackageDefence:
    """Defence (b): the native binary is re-installed as a DIRECT target.

    npm only swallows the failure because the dependency is *optional*. Asking
    for the same package by name makes the identical failure fatal -- measured:
    exit 0 as an optional dep, exit 1 as a direct target, same ENOSPC.
    """

    def test_recovery_installs_the_platform_native_package(self, layer):
        assert re.search(
            r"npm install -g[^;]*@anthropic-ai/claude-code-\$", layer
        ), (
            "the recovery must install `@anthropic-ai/claude-code-<platform-key>` "
            "as a direct install target, so its failure is fatal"
        )

    def test_recovery_pins_the_wrapper_version(self, layer):
        """A native binary from a different release is not interchangeable."""
        assert "CC_VER=" in layer and "$CC_VER" in layer

    def test_recovery_reruns_the_postinstall(self, layer):
        """Installing the package is not enough -- install.cjs places the binary."""
        assert "install.cjs" in layer

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
    def test_platform_key_expression_is_valid_and_well_formed(self, layer):
        """Run the layer's own detection expression; it must yield a real key.

        This is the one part of the layer that can be wrong silently: a typo
        would make `CC_KEY` empty and the recovery would try to install
        `@anthropic-ai/claude-code-@2.1.0`, failing the build for the wrong
        reason. Execute it instead of trusting it.
        """
        expr = re.search(r"node -e '([^']*)'", layer)
        assert expr, "no `node -e '...'` platform-key expression found in the layer"
        out = subprocess.run(
            ["node", "-e", expr.group(1)],
            capture_output=True, text=True, timeout=30,
        )
        assert out.returncode == 0, out.stderr
        assert re.fullmatch(r"(linux|darwin|win32)-(x64|arm64)(-musl)?", out.stdout), (
            f"platform key {out.stdout!r} is not one of the keys install.cjs "
            "maps to a native package"
        )


class TestScopeFence:
    """Abilityai/lilu#20 is the base-image half only; #19 owns the runtime guard.

    The sibling CLIs carry the same optional-dependency risk and were left
    alone deliberately -- this asserts that decision is written down where the
    next reader will find it, not merely remembered.
    """

    def test_sibling_cli_risk_is_recorded(self, layer):
        text = DOCKERFILE.read_text()
        note_at = text.index("deliberately NOT changed here")
        note = text[note_at:note_at + 400]
        assert "@google/gemini-cli" in note and "@openai/codex" in note
