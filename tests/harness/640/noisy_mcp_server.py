#!/usr/bin/env python3
"""Noisy stdio MCP server — repro harness for #640.

A minimal MCP server that speaks just enough of the protocol for Claude Code
to register it (`initialize`, `notifications/initialized`, `tools/list`,
`tools/call`) and then deliberately leaks output via one of several
hypothesised pollution paths.

Usage:
    noisy_mcp_server.py --leak <variant> [--rate HZ] [--burst N]

Variants:
    none           — clean baseline (no leak; control)
    stderr-flood   — flood stderr with text outside MCP protocol
    setsid-child   — fork a setsid grandchild that writes to stdout
                     periodically (escapes claude pgid kill)
    proc-fd-write  — open /proc/self/fd/1 from this process and
                     interleave raw text with MCP responses
    delayed-stdout — write a partial JSON line to stdout, sleep mid-line,
                     finish — races claude's reader at line boundary
    npm-wrapper    — simulate `npx` style: print informational text to
                     stdout BEFORE protocol begins (deprecation warnings,
                     "added N packages" — known npm bootstrap behaviour)

Each variant tests one of the leak hypotheses listed in #640.

The server logs every protocol action to a sidecar file (specified via
--log-file) so we can correlate observed wire corruption with what this
process actually wrote.

Designed to be self-contained — no external deps beyond the Python stdlib —
so it can be dropped into any agent template's `.mcp.json` without
needing pip install in the container.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Logging — write to file, never to stdout/stderr (those are the wire).
# ---------------------------------------------------------------------------

_LOG_FH = None


def log(msg: str) -> None:
    if _LOG_FH is not None:
        _LOG_FH.write(f"[{time.time():.3f}] [pid={os.getpid()}] {msg}\n")
        _LOG_FH.flush()


# ---------------------------------------------------------------------------
# Leak variants — each spawned (or activated) at server start.
# ---------------------------------------------------------------------------


def leak_stderr_flood(rate_hz: float, burst: int) -> None:
    """Periodically write garbage to stderr.

    Hypothesis: claude reads child stderr as line-delimited diagnostic
    output. If it gets dup'd onto stdout somewhere, parse failures occur.
    """

    def _worker():
        log("stderr-flood worker started")
        while True:
            for _ in range(burst):
                sys.stderr.write(f"NOISY MCP STDERR LINE pid={os.getpid()} t={time.time():.3f}\n")
            sys.stderr.flush()
            time.sleep(1.0 / rate_hz)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def leak_setsid_child(rate_hz: float, burst: int) -> None:
    """Fork a grandchild via setsid that writes to fd 1 (the MCP-protocol
    pipe write end inherited from this process).

    Hypothesis: a setsid grandchild escapes claude's pgid kill (the #618
    failure mode) AND retains an open write handle to the protocol pipe,
    so its writes interleave with this server's MCP responses, corrupting
    the JSON stream Claude is parsing as MCP frames. If those corrupted
    frames bleed back into Claude's own stdout stream toward agent-server,
    that's the #640 wire corruption.
    """
    pid = os.fork()
    if pid == 0:
        # In grandchild — detach from parent's session.
        os.setsid()
        log(f"setsid-child started pgid={os.getpgid(0)}")
        # Inherit fd 1 = MCP protocol pipe write end.
        try:
            while True:
                for _ in range(burst):
                    os.write(1, f"GRANDCHILD STDOUT POLLUTION pid={os.getpid()} t={time.time():.3f}\n".encode())
                time.sleep(1.0 / rate_hz)
        except BrokenPipeError:
            log("setsid-child broken pipe — parent died")
            os._exit(0)
    else:
        log(f"forked setsid-child pid={pid}")


def leak_proc_fd_write(rate_hz: float, burst: int) -> None:
    """From this process, open /proc/self/fd/1 and write raw text
    interleaved with MCP responses.

    Hypothesis: even when fd 1 is the MCP protocol pipe, writing raw text
    via /proc/self/fd/1 (which dups the descriptor) corrupts the protocol
    stream Claude is parsing.
    """

    def _worker():
        try:
            with open("/proc/self/fd/1", "wb", buffering=0) as f:
                log("proc-fd-write opened /proc/self/fd/1")
                while True:
                    for _ in range(burst):
                        f.write(f"PROC_FD_WRITE_POLLUTION pid={os.getpid()} t={time.time():.3f}\n".encode())
                    time.sleep(1.0 / rate_hz)
        except (BrokenPipeError, OSError) as e:
            log(f"proc-fd-write ended: {e}")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def leak_delayed_stdout(rate_hz: float, burst: int) -> None:
    """Write a partial line to stdout (no newline), sleep, then finish.

    Hypothesis: if Claude's reader is reading at line granularity and the
    line is mid-flight when it expects a JSON message, the partial reads
    cause buffering issues / split frames.
    """

    def _worker():
        while True:
            for _ in range(burst):
                # Write half a line, no newline — sit on the pipe.
                os.write(1, b"PARTIAL_LINE_NO_NEWLINE_")
                time.sleep(0.05)
                os.write(1, f"finish_pid={os.getpid()}\n".encode())
            time.sleep(1.0 / rate_hz)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def leak_npm_wrapper() -> None:
    """Print informational text to stdout BEFORE protocol begins.

    This is the most likely real-world culprit: `npx -y some-pkg` prints
    "npm warn deprecated foo" / "added 5 packages, audited 23 packages"
    on stdout during package install. If Claude attempts to start protocol
    before that boilerplate has cleared, the first MCP frames are
    interleaved with package-manager output.
    """
    sys.stdout.write("npm warn deprecated some-pkg@1.0.0: use other-pkg instead\n")
    sys.stdout.write("npm warn deprecated another-pkg@2.0.0: this package is no longer maintained\n")
    sys.stdout.write("\n")
    sys.stdout.write("added 5 packages, and audited 23 packages in 1s\n")
    sys.stdout.write("\n")
    sys.stdout.write("3 packages are looking for funding\n")
    sys.stdout.write("  run `npm fund` for details\n")
    sys.stdout.write("\n")
    sys.stdout.flush()
    log("npm-wrapper bootstrap noise emitted")


# ---------------------------------------------------------------------------
# Minimal MCP protocol — just enough to register and respond to tool calls.
# ---------------------------------------------------------------------------


def write_response(msg_id: Any, result: Dict[str, Any]) -> None:
    msg = {"jsonrpc": "2.0", "id": msg_id, "result": result}
    line = json.dumps(msg) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    log(f"RESPONSE id={msg_id} bytes={len(line)}")


def write_error(msg_id: Any, code: int, message: str) -> None:
    msg = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()
    log(f"ERROR id={msg_id} code={code}")


def handle(msg: Dict[str, Any]) -> None:
    method = msg.get("method")
    msg_id = msg.get("id")
    log(f"REQUEST method={method} id={msg_id}")

    if method == "initialize":
        write_response(msg_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "noisy-mcp-server", "version": "0.1.0"},
        })
    elif method == "notifications/initialized":
        # No response on notifications.
        pass
    elif method == "tools/list":
        write_response(msg_id, {"tools": [
            {
                "name": "echo",
                "description": "Echo a string back. Cheap. Use for repro stress.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
        ]})
    elif method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name")
        if name == "echo":
            text = params.get("arguments", {}).get("text", "")
            write_response(msg_id, {"content": [{"type": "text", "text": text}]})
        else:
            write_error(msg_id, -32601, f"Unknown tool: {name}")
    elif method is None:
        # Probably a response to a server-initiated request — ignore.
        pass
    else:
        write_error(msg_id, -32601, f"Method not found: {method}")


def main() -> None:
    global _LOG_FH

    p = argparse.ArgumentParser()
    p.add_argument(
        "--leak",
        choices=["none", "stderr-flood", "setsid-child", "proc-fd-write", "delayed-stdout", "npm-wrapper"],
        default="none",
    )
    p.add_argument("--rate", type=float, default=10.0, help="Leak rate in Hz")
    p.add_argument("--burst", type=int, default=5, help="Lines per burst")
    p.add_argument(
        "--log-file",
        default="/tmp/noisy-mcp-server.log",
        help="Sidecar log file (NEVER write to stdout/stderr).",
    )
    args = p.parse_args()

    _LOG_FH = open(args.log_file, "a")
    log(f"=== START variant={args.leak} rate={args.rate} burst={args.burst} ===")

    # npm-wrapper variant runs once at startup BEFORE protocol begins.
    if args.leak == "npm-wrapper":
        leak_npm_wrapper()
    elif args.leak == "stderr-flood":
        leak_stderr_flood(args.rate, args.burst)
    elif args.leak == "setsid-child":
        leak_setsid_child(args.rate, args.burst)
    elif args.leak == "proc-fd-write":
        leak_proc_fd_write(args.rate, args.burst)
    elif args.leak == "delayed-stdout":
        leak_delayed_stdout(args.rate, args.burst)

    # Read MCP frames from stdin, write responses to stdout.
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            log(f"malformed input: {e}; line={line[:200]!r}")
            continue
        try:
            handle(msg)
        except Exception as e:
            log(f"handler error: {e}")

    log("=== STDIN CLOSED — server exiting ===")


if __name__ == "__main__":
    main()
