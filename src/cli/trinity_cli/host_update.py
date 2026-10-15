"""Locate a Trinity checkout's update script for `trinity update` (#3007).

`trinity update` is a front door, not a second implementation: the update itself
(shape detection, preflight, backup, rebuild, published-URL verification,
rollback) lives in the checkout's `scripts/deploy/update.sh`, because it has to
run ON the host, against that checkout's compose files and git history. This
module only decides which script to run. Stdlib only, so it is testable without
the CLI's own dependencies.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional, Sequence

UPDATE_SCRIPT = Path("scripts") / "deploy" / "update.sh"


class NotACheckout(Exception):
    """The directory holds no Trinity update script."""


def find_update_script(directory: Optional[str], env: Mapping[str, str] = os.environ) -> Path:
    """`--dir`, else `TRINITY_HOME`, else the current directory."""
    base = Path(directory or env.get("TRINITY_HOME") or os.getcwd()).expanduser()
    script = base / UPDATE_SCRIPT
    if not script.is_file():
        raise NotACheckout(
            f"{base} is not a Trinity checkout (no {UPDATE_SCRIPT.as_posix()} there). "
            "Run `trinity update` from the directory Trinity is installed in, or pass "
            "--dir /path/to/trinity (or set TRINITY_HOME)."
        )
    return script


def build_command(script: Path, args: Sequence[str]) -> list:
    return ["bash", str(script), *args]
