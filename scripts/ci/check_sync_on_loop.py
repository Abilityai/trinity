#!/usr/bin/env python3
"""Sync-on-loop ratchet: blocking calls inside `async def` may only shrink (#4674).

A synchronous database, Docker, HTTP or sleep call made directly inside an
`async def` runs ON the asyncio event loop. While it runs, the whole uvicorn
worker serves nothing — every other request, every WebSocket, `/health`. A
hosted instance showed it as `/health` taking up to 1.5 s (#4671).

There are several hundred such call sites today. This guard does not ask for
them to be fixed at once; it stops the number GROWING, per file, and makes
every paydown stick:

  * a file's count may not exceed its baseline entry;
  * a file with no entry starts at zero — new code does not add to the debt;
  * an entry that sits above reality fails too, so a fix has to lower the
    baseline (regenerate it) or the ratchet stops biting on that file.

What is counted — CANDIDATES, by reading the code, never by running it:

  A call, written directly in the body of an `async def` (not inside a nested
  `def` / `lambda`, which may well be handed to a thread), to one of:

    * the sync db layer:       `db.<x>(…)`, `core_db.<x>(…)`, `_db.<x>(…)`,
                               `deps.db.<x>(…)`
    * Docker helpers:          `get_agent_container(…)`, `list_all_agents_fast(…)`,
                               `agent_container_states(…)`
    * blocking stdlib / HTTP:  `time.sleep(…)`, `subprocess.run(…)`,
                               `requests.<verb>(…)`

  …and a call to one of the SAME module's plain `def`s that makes such a call
  (directly or through another of them): `await`-free code in an `async def`
  blocks exactly the same when the blocking line is one function down.

  NOT counted, because the work does not run on the loop:

    * anything inside the arguments of `asyncio.to_thread(…)`,
      `loop.run_in_executor(…)`, `run_in_threadpool(…)`,
      `anyio.to_thread.run_sync(…)` — the thread hops;
    * anything in a plain `def` handler — FastAPI runs those in its threadpool.

  A count is a place to look, not a verdict: a sub-millisecond indexed read is
  counted the same as a fleet-wide Docker listing. The event-loop probe
  (`services/loop_health.py`) says which ones actually stall a worker.

The helper names live in ONE place (`BLOCKING` below); anything else that wants
the list should import it from here rather than keep a second copy.

Usage:
    python scripts/ci/check_sync_on_loop.py                 # check against the baseline
    python scripts/ci/check_sync_on_loop.py --write         # regenerate after a paydown
    python scripts/ci/check_sync_on_loop.py --write --allow-growth   # accept named increases
    python scripts/ci/check_sync_on_loop.py --report        # counts + top files, no check

Pure stdlib; parses, never imports.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "src" / "backend"
BASELINE = Path(__file__).resolve().parent / "sync_on_loop_baseline.json"

# Not Trinity's own request path: the private submodule has its own guard, and
# Alembic revisions run at boot, before the loop serves anything.
SKIP_DIRS = ("enterprise", "migrations", "__pycache__")

# The one list of blocking callables.
BLOCKING = {
    # receiver names whose every method call is a sync db read/write
    "db_receivers": ("db", "core_db", "_db"),
    # `<x>.db.<method>(…)` — e.g. `deps.db.get_setting_value(…)`
    "db_attribute": "db",
    # bare function calls
    "functions": ("get_agent_container", "list_all_agents_fast", "agent_container_states"),
    # `<module>.<attr>(…)`; `None` means any attribute of that module
    "module_calls": {"time": ("sleep",), "subprocess": ("run", "check_output", "check_call", "call"),
                     "requests": None},
}

# Calls whose ARGUMENTS run off the loop.
THREAD_HOPS = ("to_thread", "run_in_executor", "run_in_threadpool", "run_sync")


def _dotted(node: ast.AST) -> Optional[str]:
    """`a.b.c` for a Name/Attribute chain, else None."""
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def blocking_kind(call: ast.Call) -> Optional[str]:
    """Which blocking family `call` belongs to, or None."""
    func = call.func
    if isinstance(func, ast.Name):
        return "docker" if func.id in BLOCKING["functions"] else None
    if not isinstance(func, ast.Attribute):
        return None
    receiver = _dotted(func.value)
    if receiver is None:
        return None
    last = receiver.split(".")[-1]
    if receiver in BLOCKING["db_receivers"] or (
            "." in receiver and last == BLOCKING["db_attribute"]):
        return "db"
    if func.attr in BLOCKING["functions"]:
        return "docker"
    allowed = BLOCKING["module_calls"].get(receiver, ())
    if receiver in BLOCKING["module_calls"] and (allowed is None or func.attr in allowed):
        return receiver
    return None


def _is_thread_hop(call: ast.Call) -> bool:
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    return name in THREAD_HOPS


def _direct_calls(node: ast.AST) -> Iterator[ast.Call]:
    """Every Call that runs in `node`'s own frame — stops at nested function
    and lambda bodies, and at the arguments of a thread hop."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(child, ast.Call):
            yield child
            if _is_thread_hop(child):
                # The callee expression still runs here; what is passed to the
                # thread does not.
                yield from _direct_calls(child.func)
                continue
        yield from _direct_calls(child)


def _local_blocking_functions(tree: ast.Module) -> Dict[str, str]:
    """`{name: kind}` for the module's own plain `def`s that block — directly,
    or by calling another one that does (to a fixed point).

    One hop of indirection is how the hottest sites are written: the heartbeat
    loop awaits nothing blocking itself, it calls `process_watch_tick()`, a
    sync function in the same file that lists every container. Following calls
    INTO the module's own sync functions catches that; following them across
    modules would need imports resolved, and this guard parses, never imports.
    """
    defs = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    blocking: Dict[str, str] = {}
    for name, node in defs.items():
        for call in _direct_calls(node):
            kind = blocking_kind(call)
            if kind:
                blocking[name] = kind
                break
    changed = True
    while changed:
        changed = False
        for name, node in defs.items():
            if name in blocking:
                continue
            for call in _direct_calls(node):
                callee = call.func.id if isinstance(call.func, ast.Name) else None
                if callee in blocking:
                    blocking[name] = blocking[callee]
                    changed = True
                    break
    return blocking


def scan_source(source: str) -> List[Tuple[int, str, str]]:
    """`[(line, function, kind)]` for every candidate in `source`.

    `kind` is the blocking family, or `via:<family>` when the async function
    reaches it through one of the module's own sync functions."""
    found: List[Tuple[int, str, str]] = []
    tree = ast.parse(source)
    local = _local_blocking_functions(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for call in _direct_calls(node):
            kind = blocking_kind(call)
            if not kind and isinstance(call.func, ast.Name) and call.func.id in local:
                kind = f"via:{local[call.func.id]}"
            if kind:
                found.append((call.lineno, node.name, kind))
    return sorted(found)


def scan_tree(root: Path = BACKEND) -> Dict[str, List[Tuple[int, str, str]]]:
    """`{path relative to the repo: candidates}` for every file that has any."""
    out: Dict[str, List[Tuple[int, str, str]]] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        try:
            found = scan_source(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # a file that does not parse is not ours to count
            print(f"skipped (does not parse): {rel}: {exc}", file=sys.stderr)
            continue
        if found:
            out[str(path.relative_to(REPO))] = found
    return out


def counts(scan: Dict[str, list]) -> Dict[str, int]:
    return {path: len(found) for path, found in scan.items()}


def compare(current: Dict[str, int], baseline: Dict[str, int]) -> List[str]:
    """Human-readable problems; empty means the ratchet holds."""
    problems: List[str] = []
    for path in sorted(set(current) | set(baseline)):
        now, allowed = current.get(path, 0), baseline.get(path, 0)
        if now > allowed:
            where = "no baseline entry — new code starts at zero" if path not in baseline \
                else f"baseline {allowed}"
            problems.append(f"GREW  {path}: {now} blocking call(s) in async functions ({where})")
        elif now < allowed:
            problems.append(f"STALE {path}: baseline says {allowed}, the code has {now} — "
                            f"regenerate so the ratchet keeps biting")
    return problems


def load_baseline(path: Path = BASELINE) -> Dict[str, int]:
    return dict(json.loads(path.read_text(encoding="utf-8"))["files"])


def write_baseline(current: Dict[str, int], path: Path = BASELINE) -> None:
    doc = {
        "_comment": "Blocking calls written directly inside `async def`, per file (#4674). "
                    "May only shrink. Regenerate: python scripts/ci/check_sync_on_loop.py --write",
        "total": sum(current.values()),
        "files": dict(sorted(current.items())),
    }
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def report(scan: Dict[str, list], top: int = 10) -> str:
    per_file = counts(scan)
    by_kind: Dict[str, int] = {}
    for found in scan.values():
        for _, _, kind in found:
            by_kind[kind] = by_kind.get(kind, 0) + 1
    functions = sum(len({name for _, name, _ in found}) for found in scan.values())
    lines = [f"sync-on-loop candidates: {sum(per_file.values())} call(s) in {functions} async "
             f"function(s), {len(per_file)} files",
             "by kind: " + ", ".join(f"{k}={n}" for k, n in sorted(by_kind.items(), key=lambda kv: -kv[1]))]
    for path, n in sorted(per_file.items(), key=lambda kv: (-kv[1], kv[0]))[:top]:
        lines.append(f"  {n:4d}  {path}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write", action="store_true",
                        help="regenerate the baseline (refuses if any file's count grew)")
    parser.add_argument("--allow-growth", action="store_true",
                        help="with --write: accept the increases, each printed by name")
    parser.add_argument("--report", action="store_true", help="print counts and the top files")
    parser.add_argument("--json", action="store_true", help="with --report: machine-readable")
    args = parser.parse_args(argv)

    scan = scan_tree()
    current = counts(scan)
    if args.report:
        if args.json:
            print(json.dumps({"total": sum(current.values()), "files": current}, indent=2))
        else:
            print(report(scan))
        return 0
    if args.write:
        # A regeneration must not be a way to wave new blocking calls through:
        # it lowers entries freely, and raises one only when asked to, by name.
        grew = [p for p in compare(current, load_baseline(BASELINE)) if p.startswith("GREW")] \
            if BASELINE.exists() else []
        if grew:
            print("\n".join(grew))
            if not args.allow_growth:
                print("\nThe baseline was NOT written: the counts above grew. Remove the "
                      "calls, or accept them by name with --write --allow-growth.")
                return 1
        write_baseline(current, BASELINE)
        print(f"baseline written: {sum(current.values())} candidates in {len(current)} files")
        return 0

    problems = compare(current, load_baseline(BASELINE))
    if problems:
        print("\n".join(problems))
        print("\nA sync db / Docker / HTTP / sleep call inside `async def` blocks the whole "
              "worker while it runs (#4674).\nHop to a thread (`await asyncio.to_thread(...)`), "
              "make the handler a plain `def`, or use the async client.\nIf you REMOVED calls, "
              "regenerate: python scripts/ci/check_sync_on_loop.py --write")
        return 1
    print(f"sync-on-loop ratchet holds: {sum(current.values())} candidates in {len(current)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
