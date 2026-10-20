"""Shared harness for the in-container skill-gate hook (trinity-enterprise#752).

Runs the REAL hook module (`docker/base-image/hooks/_skill_gate.py`) in a
subprocess under `-I -S`, the flags the image registers, through an
IMPORT-LEVEL seam: a wrapper imports the module, sets module constants
(`MARKER`, `PROC_ROOT`, `USER_HOME`, the deadline) and calls `main`. The
production entry point reads no test environment variable and no test argv —
the module's own environment is cleared before it decides anything, so an env
seam could not work anyway.

The platform side is a stub HTTP backend on 127.0.0.1 that records every
request and answers from a script. `/proc` is a temp tree: `<pid>/environ`
(NUL-separated) for the claude process (the wrapper's parent — this pytest
process) and for PID 1, plus `<pid>/cwd` as a symlink to the project dir.

Underscore-prefixed so pytest never collects it.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / "docker" / "base-image" / "hooks"
BOOTSTRAP = HOOKS / "skill-gate.py"
MODULE = HOOKS / "_skill_gate.py"

KEY = "trinity_mcp_agentkey_752_do_not_log"
EXECUTION_ID = "exec-row-752"

_WRAPPER = r'''
import json, os, sys
sys.path.insert(0, {hooks!r})
import lib
lib.LOG_PATH = {log!r}
import _skill_gate as g
for _k, _v in json.loads({over!r}).items():
    setattr(g, _k, _v)
code = g.main([])
sys.stdout.flush()
sys.stderr.flush()
os._exit(code)
'''


class Backend:
    """A stub platform. `script(...)` queues actions; the last one repeats.

    Actions: ("status", code, json_or_bytes) · ("close",) — read the request,
    answer nothing, drop the connection · ("hang", seconds).
    """

    def __init__(self):
        self.requests = []
        self._script = [("status", 200, {"allowed": True, "gated": False, "message": None})]
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with outer._lock:
                    outer.requests.append(SimpleNamespace(
                        path=self.path, headers={k.lower(): v for k, v in self.headers.items()},
                        body=body))
                    action = outer._script[0] if len(outer._script) == 1 else outer._script.pop(0)
                if action[0] == "close":
                    self.close_connection = True
                    return
                if action[0] == "hang":
                    time.sleep(action[1])
                    self.close_connection = True
                    return
                status, payload = action[1], action[2]
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def script(self, *actions) -> None:
        with self._lock:
            self._script = list(actions)

    def answer(self, status: int, payload: Any) -> None:
        self.script(("status", status, payload))

    def bodies(self):
        return [json.loads(r.body) for r in self.requests]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def closed_port_url() -> str:
    """A URL nothing listens on — a refused connection."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def _environ_bytes(env: Dict[str, str]) -> bytes:
    return b"".join(f"{k}={v}".encode() + b"\0" for k, v in env.items())


class HookWorld:
    """One agent home, one fake `/proc`, one stub backend."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        (self.home / ".claude" / "skills").mkdir(parents=True)
        (self.home / ".claude" / "agents").mkdir(parents=True)
        self.project = self.home            # a Trinity agent's cwd IS its home
        self.proc = tmp / "proc"
        self.marker = tmp / "opt" / "skill-gates-active"
        self.marker.parent.mkdir(parents=True)
        self.log = tmp / "logs" / "guardrails.jsonl"
        self.log.parent.mkdir(parents=True)
        self.backend = Backend()
        self.pid1_env: Dict[str, Optional[str]] = {
            "TRINITY_BACKEND_URL": self.backend.url,
            "TRINITY_MCP_API_KEY": KEY,
            "AGENT_NAME": "a-stale-name-the-hook-must-not-use",
        }
        self.parent_env: Dict[str, Optional[str]] = {
            "TRINITY_EXECUTION_ID": EXECUTION_ID,
            "HOME": str(self.home),
        }

    # -- fixtures -------------------------------------------------------------
    def skill(self, dirname: str, frontmatter: Optional[str] = None, *, root: Optional[Path] = None,
              raw: Optional[bytes] = None) -> Path:
        d = (root or self.home / ".claude" / "skills") / dirname
        d.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            (d / "SKILL.md").write_bytes(raw)
        else:
            head = f"---\n{frontmatter}\n---\n" if frontmatter is not None else ""
            (d / "SKILL.md").write_text(head + "# do the thing\n")
        return d

    def subagent(self, relpath: str, frontmatter: str, *, root: Optional[Path] = None) -> Path:
        p = (root or self.home / ".claude" / "agents") / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\n{frontmatter}\n---\nYou are a helper.\n")
        return p

    def set_marker(self, present: bool = True) -> None:
        if present:
            self.marker.write_text("1\n")
        elif self.marker.exists():
            self.marker.unlink()

    def log_events(self):
        if not self.log.exists() or not self.log.is_file():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def _write_proc(self) -> None:
        me = self.proc / str(os.getpid())
        one = self.proc / "1"
        for d in (me, one):
            d.mkdir(parents=True, exist_ok=True)
        for d, env in ((one, self.pid1_env), (me, self.parent_env)):
            f = d / "environ"
            if env is None:
                if f.exists():
                    f.unlink()
                continue
            f.write_bytes(_environ_bytes({k: v for k, v in env.items() if v is not None}))
        cwd = me / "cwd"
        if cwd.is_symlink() or cwd.exists():
            cwd.unlink()
        cwd.symlink_to(self.project)

    def overrides(self, **extra) -> Dict[str, Any]:
        base = {
            "MARKER": str(self.marker),
            "PROC_ROOT": str(self.proc),
            "USER_HOME": str(self.home),
        }
        base.update(extra)
        return base

    # -- runs -----------------------------------------------------------------
    def run(self, payload: Any, *, env: Optional[Dict[str, str]] = None, timeout: float = 40,
            raw_stdin: Optional[str] = None, **over) -> subprocess.CompletedProcess:
        """Run `_skill_gate.main` the way the bootstrap does, in its own process."""
        self._write_proc()
        wrapper = self.tmp / "run_hook.py"
        wrapper.write_text(_WRAPPER.format(hooks=str(HOOKS), log=str(self.log),
                                           over=json.dumps(self.overrides(**over))))
        stdin = raw_stdin if raw_stdin is not None else json.dumps(payload)
        return subprocess.run([sys.executable, "-I", "-S", str(wrapper)], input=stdin,
                              capture_output=True, text=True, timeout=timeout,
                              env=env if env is not None else {"PATH": os.environ.get("PATH", "")})

    def close(self) -> None:
        self.backend.close()


def skill_payload(skill: Any = "pay-invoice", **tool_input) -> Dict[str, Any]:
    ti: Dict[str, Any] = {"skill": skill, "args": "100 EUR to ACME"} if skill is not None else {}
    ti.update(tool_input)
    return {"session_id": "sess-752", "transcript_path": "/tmp/t.jsonl", "cwd": "/home/developer",
            "permission_mode": "bypassPermissions", "hook_event_name": "PreToolUse",
            "tool_name": "Skill", "tool_input": ti, "tool_use_id": "toolu_752"}


def agent_payload(subagent_type: Any = "finance", tool_name: str = "Agent") -> Dict[str, Any]:
    ti: Dict[str, Any] = {"description": "pay it", "prompt": "Pay the invoice."}
    if subagent_type is not None:
        ti["subagent_type"] = subagent_type
    return {"session_id": "sess-752", "transcript_path": "/tmp/t.jsonl", "cwd": "/home/developer",
            "hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": ti,
            "tool_use_id": "toolu_752b"}


def load_module():
    """The hook module imported IN-PROCESS (import is side-effect free: the
    environment is cleared only inside `main`)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_skill_gate_752_inproc", str(MODULE))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
