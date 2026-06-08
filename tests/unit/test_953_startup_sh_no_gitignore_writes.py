"""
Regression test for #953 — `M .gitignore` drift on freshly-deployed agents.

`docker/base-image/startup.sh` used to append `.local/` and `content/` to
`/home/developer/.gitignore` at every boot. Combined with the canonical
list now applied by `_build_gitignore_merge_command`
(`src/backend/services/git_service.py`), the shell-level append was
redundant — and on agents whose template `.gitignore` already shipped the
patterns correctly, false-negative greps (anchored patterns hitting CRLF
or trailing whitespace) re-appended duplicates, producing `M .gitignore`
against `origin/main` with no user action.

Fix: remove both append sites. The canonical merge in `git_service.py`
handles every git-initialized agent — `initialize_git_in_container` runs
it on first init and `_migrate_workspace_gitignore` runs it on every
push.

This test pins the fix: it fails if anyone re-introduces an
`>> .gitignore` write inside `docker/base-image/startup.sh`.
"""

from __future__ import annotations

from pathlib import Path


STARTUP = (
    Path(__file__).resolve().parents[2]
    / "docker"
    / "base-image"
    / "startup.sh"
)


def test_startup_sh_does_not_write_to_gitignore():
    """No append-or-overwrite-style write to `.gitignore` from startup.sh.

    Greps for any of the redirect operators (`> /home/developer/.gitignore`
    or `>> /home/developer/.gitignore`). Comments mentioning `.gitignore`
    are fine — only redirect syntax is forbidden.
    """
    assert STARTUP.exists(), f"startup.sh missing at {STARTUP}"
    text = STARTUP.read_text()

    forbidden_substrings = (
        "> /home/developer/.gitignore",
        ">> /home/developer/.gitignore",
        '> "/home/developer/.gitignore"',
        '>> "/home/developer/.gitignore"',
    )
    found = [s for s in forbidden_substrings if s in text]
    assert not found, (
        "startup.sh writes to /home/developer/.gitignore "
        f"(found: {found}). The canonical pattern list lives in "
        "_GITIGNORE_PATTERNS (src/backend/services/git_service.py); "
        "let _build_gitignore_merge_command apply it on init/push instead."
    )


def test_startup_sh_keeps_pointer_to_canonical_helper():
    """The replaced block keeps a comment pointing readers at git_service.py
    so the next person looking for "where do the patterns come from?"
    doesn't have to git blame their way there.
    """
    text = STARTUP.read_text()
    # Just check that the canonical helper is named somewhere — the exact
    # comment text is allowed to drift.
    assert "_build_gitignore_merge_command" in text or "_GITIGNORE_PATTERNS" in text, (
        "startup.sh should reference the canonical gitignore helper "
        "(_build_gitignore_merge_command or _GITIGNORE_PATTERNS) so "
        "future readers can find it."
    )
