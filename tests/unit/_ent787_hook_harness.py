"""Shared harness for the GUARD-002 hooks under their registered interpreter
flags (trinity-enterprise#787).

Runs the REAL hook module (`docker/base-image/hooks/<hook>.py`) in a
subprocess through an import-level seam: a `-c` wrapper puts the repo's hooks
directory on `sys.path`, points `lib`'s config and log paths at test files,
loads the hook as a module, applies module-constant overrides and calls
`lib.run_hook(main)` — the same fail-closed wrapper the hook's `__main__` uses.
The hooks hard-code `/opt/trinity/...`, which does not exist on a runner, so
an env seam would not reach them (and `env -i` empties the env anyway).

The interpreter is the BASE interpreter (`sys._base_executable`), never the
venv shim: inside a venv the user site is disabled, so a user-site
`usercustomize.py` would be inert under the OLD flags too and the hostile-env
tests would pass for the wrong reason.

Underscore-prefixed so pytest never collects it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

REPO = Path(__file__).resolve().parents[2]
BASE = REPO / "docker" / "base-image"
HOOKS = BASE / "hooks"
MANAGED = HOOKS / "managed-settings.json"
BASELINE = HOOKS / "guardrails-baseline.json"

GUARD_HOOKS = ("bash-guardrail.py", "file-guardrail.py", "read-only-guard.py", "output-scanner.py")

PYTHON = getattr(sys, "_base_executable", None) or sys.executable

_WRAPPER = r'''
import importlib.util, json, sys
sys.path.insert(0, {hooks!r})
import lib
lib.RUNTIME_CONFIG_PATH = {runtime!r}
lib.BASELINE_CONFIG_PATH = {baseline!r}
lib.LOG_PATH = {log!r}
spec = importlib.util.spec_from_file_location("guard_hook", {script!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
for _k, _v in json.loads({over!r}).items():
    setattr(mod, _k, _v)
lib.run_hook(mod.main)
'''


def registration() -> dict:
    return json.loads(MANAGED.read_text())


def registered_entries() -> List[dict]:
    """Every hook entry in the managed file, flattened."""
    return [h for phase in registration()["hooks"].values() for e in phase for h in e["hooks"]]


def registered_flags(hook: str) -> List[str]:
    """The interpreter flags the managed file runs ``hook`` with: everything in
    `args` between the interpreter and the script."""
    for entry in registered_entries():
        args = entry.get("args") or []
        if args and args[-1].endswith("/" + hook):
            interp = next(i for i, a in enumerate(args) if a.endswith("python3"))
            return args[interp + 1:-1]
    raise AssertionError(f"{hook} is not registered in exec form")


def registered_env(hook: str) -> Dict[str, str]:
    """The environment `env -i NAME=value ...` hands the hook — nothing else."""
    for entry in registered_entries():
        args = entry.get("args") or []
        if args and args[-1].endswith("/" + hook):
            assert entry["command"] == "/usr/bin/env" and args[0] == "-i"
            return dict(a.split("=", 1) for a in args[1:] if "=" in a and not a.startswith("-"))
    raise AssertionError(f"{hook} is not registered in exec form")


def run_hook(hook: str, payload: dict, *, flags: List[str], env: Dict[str, str],
             tmp: Path, overrides: Optional[dict] = None) -> subprocess.CompletedProcess:
    log = tmp / "guardrails.jsonl"
    wrapper = _WRAPPER.format(
        hooks=str(HOOKS), runtime=str(tmp / "no-runtime.json"), baseline=str(BASELINE),
        log=str(log), script=str(HOOKS / hook), over=json.dumps(overrides or {}))
    return subprocess.run(
        [PYTHON, *flags, "-c", wrapper], input=json.dumps(payload), env=env,
        capture_output=True, text=True, timeout=30)


def log_events(tmp: Path) -> List[dict]:
    path = tmp / "guardrails.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def plant_usercustomize(home: Path, body: str) -> Path:
    """Write `usercustomize.py` into the user site `PYTHON` derives from ``home``."""
    site_dir = subprocess.run(
        [PYTHON, "-c", "import site; print(site.getusersitepackages())"],
        env={"HOME": str(home), "PATH": os.environ.get("PATH", "")},
        capture_output=True, text=True, check=True).stdout.strip()
    target = Path(site_dir)
    target.mkdir(parents=True, exist_ok=True)
    (target / "usercustomize.py").write_text(body)
    return target
