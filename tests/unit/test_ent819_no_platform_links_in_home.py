"""Trinity itself creates no links in an agent's home (trinity-enterprise#819).

Download and preview refuse any path that is a link, for every caller, the
platform's own reads included. That is only free while Trinity never makes one
there itself: shared folders are volume mounts, snapshot restore and skill
packaging drop links, and the Codex instruction file is a copy. This pins that
premise over the backend and the base image: a new `os.symlink`, `symlink_to`,
`hardlink_to`, `os.link` or `ln -s` fails here until it is reviewed against the
read rule and listed below with a reason.

Comments are stripped before matching (tokenize for Python, `#` for shell and
Dockerfile), so a comment naming one of these is not a finding. A Python file
that does not tokenize is scanned as raw text rather than skipped.
"""
import io
import re
import tokenize
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROOTS = (_REPO_ROOT / "src/backend", _REPO_ROOT / "docker/base-image")
# The private submodule is never checked out in CI and owns its own guards.
_EXCLUDED = (_REPO_ROOT / "src/backend/enterprise",)

_PY_PATTERNS = (
    re.compile(r"\bos\.symlink\s*\("),
    re.compile(r"\.symlink_to\s*\("),
    re.compile(r"\.hardlink_to\s*\("),
    re.compile(r"\bos\.link\s*\("),
    re.compile(r"""\bln\s+-[A-Za-z]*s"""),                       # inside a shell string
    re.compile(r"""["']ln["']\s*,\s*["']-[A-Za-z]*s"""),          # an argv list
)
_SH_PATTERNS = (re.compile(r"\bln\s+-[A-Za-z]*s"), re.compile(r"\bcp\s+-[A-Za-z]*s\b"))

# path relative to the repo -> why this link is safe under the read rule.
_ALLOWED: dict[str, str] = {}


def _strip_py_comments(source: str) -> str:
    """Blank every comment token in place; code and strings keep their text."""
    lines = source.splitlines(keepends=True)
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            (row, start), (_, end) = tok.start, tok.end
            line = lines[row - 1]
            lines[row - 1] = line[:start] + " " * (end - start) + line[end:]
    return "".join(lines)


def _strip_sh_comments(source: str) -> str:
    return "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in source.splitlines())


def _scan(roots, excluded=()):
    findings = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or any(path.is_relative_to(x) for x in excluded):
                continue
            if "__pycache__" in path.parts:
                continue
            if path.suffix == ".py":
                try:
                    text = _strip_py_comments(path.read_text())
                except (tokenize.TokenError, SyntaxError, UnicodeDecodeError):
                    # Never skip: a file that does not tokenize is scanned raw,
                    # comments included, so it can only add a finding.
                    text = path.read_text(errors="replace")
                patterns = _PY_PATTERNS
            elif path.suffix == ".sh" or path.name.startswith("Dockerfile"):
                text, patterns = _strip_sh_comments(path.read_text(errors="replace")), _SH_PATTERNS
            else:
                continue
            for pat in patterns:
                if pat.search(text):
                    findings.append(str(path))
                    break
    return findings


def test_trinity_creates_no_links_in_an_agent_home():
    findings = [f for f in _scan(_ROOTS, _EXCLUDED)
                if str(Path(f).relative_to(_REPO_ROOT)) not in _ALLOWED]
    assert findings == [], (
        "Link creation found. Download and preview refuse links for every caller, "
        "including the platform's own reads; review the new link against that rule "
        f"and list it in _ALLOWED with a reason: {findings}"
    )


def test_every_allowance_names_a_reason():
    assert all(reason.strip() for reason in _ALLOWED.values())


@pytest.mark.parametrize("planted,name", [
    ("import os\nos.symlink('/home/developer/.env', 'x')\n", "a.py"),
    ("from pathlib import Path\nPath('x').symlink_to('.env')\n", "b.py"),
    ("import subprocess\nsubprocess.run(['ln', '-sf', '.env', 'x'])\n", "c.py"),
    ("cmd = 'cd /home/developer && ln -s .env x'\n", "d.py"),
    ("#!/bin/bash\nln -sfn /data /home/developer/data\n", "e.sh"),
    ("RUN ln -s /opt/x /home/developer/x\n", "Dockerfile"),
])
def test_the_scan_has_teeth(tmp_path, planted, name):
    (tmp_path / name).write_text(planted)
    assert _scan((tmp_path,)) == [str(tmp_path / name)]


@pytest.mark.parametrize("planted", [
    b"x = '''unterminated\nos.symlink('/home/developer/.env', 'x')\n",
    b"\xff\xfe not utf-8\nimport os\nos.symlink('/home/developer/.env', 'x')\n",
], ids=["does-not-tokenize", "not-utf-8"])
def test_a_python_file_that_does_not_tokenize_is_still_scanned(tmp_path, planted):
    (tmp_path / "broken.py").write_bytes(planted)
    assert _scan((tmp_path,)) == [str(tmp_path / "broken.py")]


@pytest.mark.parametrize("text,name", [
    ("# os.symlink(a, b) is never used here\nx = 1\n", "a.py"),
    ("#!/bin/bash\n# ln -s is not used\necho ok\n", "b.sh"),
    ("x = 'ls -s'\n", "c.py"),
])
def test_comments_and_lookalikes_are_not_findings(tmp_path, text, name):
    (tmp_path / name).write_text(text)
    assert _scan((tmp_path,)) == []
