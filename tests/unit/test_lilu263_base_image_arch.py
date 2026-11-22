"""Base-image architecture resolution (Abilityai/lilu#263).

The base image pinned two x86-only artifacts by name, so it could not be built
on the arm64 DGX Spark it is built for. The two halves fail in **opposite
directions**, which is why both are pinned here:

  * ``opencode-linux-x64@<ver>`` is a DIRECT npm target, so arm64 rejects it
    LOUDLY — ``npm error code EBADPLATFORM``, 4.8 s into step 11/40. Being a
    direct target is the property #257 wanted (it defeats npm's silent-optional
    path); it just has to name the right package.
  * ``go1.23.4.linux-amd64.tar.gz`` fails SILENTLY — on arm64 that URL exists,
    downloads and untars with no error, spending 641 s installing a toolchain
    that cannot execute. Nothing fails, so nothing reports it. A static "is the
    literal gone?" check is therefore not enough for Go: the in-layer
    ``go version`` assertion is the only thing that can tell a correct install
    from this one, and it must stay in the SAME RUN, because a separate layer
    would still cache and publish the bad install.

The mapping is not re-typed here. The ``case`` blocks are EXTRACTED from the
shipped Dockerfile and EXECUTED under a stubbed ``uname``, so a future edit to
the Dockerfile is what these tests run — a re-typed copy would keep passing
against a Dockerfile that had drifted.

No Docker daemon required (``gap:engineer-no-docker-daemon``); this never builds
an image.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

DOCKERFILE = (
    Path(__file__).resolve().parents[2] / "docker" / "base-image" / "Dockerfile"
)

GO_VERSION = "1.23.4"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run_layers(text: str) -> list[tuple[int, str]]:
    """Every RUN instruction as (1-based line number, shell body)."""
    lines = text.split("\n")
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("RUN "):
            start = i
            buf = [lines[i][len("RUN ") :]]
            while buf[-1].rstrip().endswith("\\") and i + 1 < len(lines):
                i += 1
                buf.append(lines[i])
            out.append((start + 1, "\n".join(buf)))
        i += 1
    return out


def _layer_containing(text: str, needle: str) -> str:
    matches = [body for _, body in _run_layers(text) if needle in body]
    assert len(matches) == 1, f"expected exactly one RUN layer with {needle!r}, got {len(matches)}"
    return matches[0]


def _exec_under_arch(snippet: str, machine: str) -> subprocess.CompletedProcess:
    """Run a shell snippet with `uname` overridden to report `machine`.

    A POSIX shell FUNCTION shadows the external command and is inherited by the
    command substitutions in the snippet, so no executable stub file is needed
    (pytest's tmp_path lives under a noexec /tmp on some hosts).
    """
    script = f"uname() {{ echo {machine}; }}\n{snippet}\n"
    return subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, timeout=30
    )


def _case_block(body: str, var: str) -> str:
    """The `case "$(uname -m)" in ... esac` that assigns `var`, verbatim."""
    m = re.search(
        r'case\s+"\$\(uname -m\)"\s+in.*?\besac', body, re.DOTALL
    )
    assert m, f"no `case $(uname -m)` block in layer assigning {var}"
    block = m.group(0)
    assert f"{var}=" in block, f"{var} not assigned inside the extracted case block"
    # Strip Dockerfile line continuations so the block is a plain shell script.
    return re.sub(r"\\\n", "\n", block)


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE.read_text()


@pytest.fixture(scope="module")
def go_layer(dockerfile: str) -> str:
    return _layer_containing(dockerfile, "goarch")


@pytest.fixture(scope="module")
def opencode_layer(dockerfile: str) -> str:
    return _layer_containing(dockerfile, "ocarch")


# ---------------------------------------------------------------------------
# PART A — the literals are gone from executable instructions
# ---------------------------------------------------------------------------


def test_no_hardcoded_amd64_go_tarball(dockerfile: str):
    """The exact artifact that installed silently on the DGX."""
    instructions = "\n".join(
        ln for ln in dockerfile.split("\n") if not ln.lstrip().startswith("#")
    )
    assert f"go{GO_VERSION}.linux-amd64" not in instructions
    assert f"go{GO_VERSION}.linux-arm64" not in instructions, (
        "swapping one hardcoded arch for the other just moves the defect"
    )


def test_no_hardcoded_opencode_platform_package(dockerfile: str):
    """The exact package that EBADPLATFORM'd on the DGX."""
    instructions = "\n".join(
        ln for ln in dockerfile.split("\n") if not ln.lstrip().startswith("#")
    )
    assert "opencode-linux-x64@" not in instructions
    assert "opencode-linux-arm64@" not in instructions


def test_opencode_platform_package_is_still_named_explicitly(opencode_layer: str):
    """#257's fix is preserved, not traded away for `opencode-ai` alone.

    Leaving the binary to optional-dependency resolution is what installed
    NOTHING on glibc while exiting 0. The package must still be a direct,
    non-optional install target — only its name is now resolved.
    """
    assert 'opencode-linux-${ocarch}@${OPENCODE_VERSION}' in opencode_layer
    assert 'opencode-ai@${OPENCODE_VERSION}' in opencode_layer


# ---------------------------------------------------------------------------
# PART B — the shipped mapping, executed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "machine,expected_goarch,expected_ocarch",
    [("x86_64", "amd64", "x64"), ("aarch64", "arm64", "arm64")],
)
def test_arch_mapping_resolves(
    go_layer: str, opencode_layer: str, machine, expected_goarch, expected_ocarch
):
    """Both supported build hosts resolve to the artifacts verified to exist.

    npm and Go spell x86 differently (`x64` vs `amd64`) and arm64 the same, so a
    single shared mapping would be wrong for one of them; they are asserted
    independently on purpose.
    """
    for block, var, expected in (
        (_case_block(go_layer, "goarch"), "goarch", expected_goarch),
        (_case_block(opencode_layer, "ocarch"), "ocarch", expected_ocarch),
    ):
        proc = _exec_under_arch(f'set -eu\n{block}\nprintf "%s" "${var}"', machine)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == expected, (
            f"{var} on {machine}: got {proc.stdout!r}, want {expected!r}"
        )


@pytest.mark.parametrize("var,layer_key", [("goarch", "go_layer"), ("ocarch", "opencode_layer")])
def test_unmapped_arch_fails_loudly(request, var, layer_key):
    """An unknown machine must STOP the build, not fall through to a default.

    A default is how #263 happened: a silently-chosen wrong artifact. The error
    must name the architecture it could not map, so the next person adding a
    host knows what to add (quality bar: named validation).
    """
    block = _case_block(request.getfixturevalue(layer_key), var)
    proc = _exec_under_arch(f"set -eu\n{block}", "riscv64")
    assert proc.returncode != 0, "unmapped architecture silently succeeded"
    assert "riscv64" in proc.stderr
    assert "FATAL" in proc.stderr


# ---------------------------------------------------------------------------
# PART C — the in-layer assertions (the half a static check cannot cover)
# ---------------------------------------------------------------------------


def test_go_layer_asserts_the_resolved_arch_in_the_same_run(go_layer: str):
    """A wrong-arch tarball untars happily; only running `go` tells them apart.

    The assertion must live in the SAME RUN as the download — a separate layer
    would still cache and publish the bad install.
    """
    assert "/usr/local/go/bin/go version" in go_layer
    assert 'linux/${goarch}' in go_layer, (
        "the assertion must compare against the RESOLVED arch, not merely run `go version`"
    )
    # the download it is guarding is in this same layer
    assert f"go.dev/dl/go{GO_VERSION}.linux-${{goarch}}.tar.gz" in go_layer


def test_opencode_layer_keeps_its_version_assertion(opencode_layer: str):
    """#257's guard survives; it is also what proves the arch.

    `opencode --version` EXECUTES the platform binary, so a mismatched arch
    could not run at all (ENOEXEC -> 126, aborted by `set -eu`). No second
    arch check is expected here, and one should not be added: it would restate
    the mapping the layer just computed and prove nothing more.
    """
    assert "opencode --version" in opencode_layer
    assert '${OPENCODE_VERSION}' in opencode_layer
    assert "exit 1" in opencode_layer


@pytest.mark.parametrize("layer_key", ["go_layer", "opencode_layer"])
def test_layers_are_strict_mode(request, layer_key):
    """`set -eu` is what turns every assertion above into a build failure."""
    assert request.getfixturevalue(layer_key).lstrip().startswith("set -eu")


# ---------------------------------------------------------------------------
# PART D — the source of truth for the arch
# ---------------------------------------------------------------------------


def test_arch_comes_from_uname_not_an_undeclared_build_arg(dockerfile: str):
    """`TARGETARCH` is empty under the classic builder — the same silent class.

    It also needs an in-stage `ARG TARGETARCH` redeclaration, and that ARG would
    have to sit above the Go layer, busting the cached upper layers that make a
    DGX retry cheap. If a future change does move to TARGETARCH, it must declare
    the ARG and guard the empty case; this test is the prompt to do so.
    """
    assert "TARGETARCH" not in "\n".join(
        ln for ln in dockerfile.split("\n") if not ln.lstrip().startswith("#")
    )
