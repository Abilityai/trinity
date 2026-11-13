"""#4541 — an upgrade waits for the PROXY-protocol listener before rendering Caddy.

A recreated web container answers on the ordinary listener before its PROXY
listener does. The upgrade path gave the PROXY listener 5 x 2 s, lost that race,
and left Caddy on the plain listener, so every visitor reached Trinity as the
proxy's address. The wait now has the ordinary listener's 90 s budget.

The function is run, with the probe and `sleep` stubbed, so the test proves the
retry count and the deadline rather than the source text.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_START = Path(__file__).resolve().parents[2] / "scripts" / "deploy" / "start.sh"


def _run(ready_on_call: int, budget: str = "") -> tuple[int, int]:
    """Return (exit code, probe calls) with the probe answering on call N (0 = never)."""
    src = _START.read_text()
    start = src.index("provision_wait_for_proxy_listener() {")
    fn = src[start : src.index("\n}\n", start) + 3]
    harness = (
        fn
        + "calls=0\n"
        + 'provision_proxy_listener_ready() { calls=$((calls+1)); [ "$READY_ON" -gt 0 ] && [ "$calls" -ge "$READY_ON" ]; }\n'
        + "sleep() { :; }\n"
        + f'provision_wait_for_proxy_listener 18082 {budget}\n'
        + 'rc=$?; echo "$calls"; exit $rc\n'
    )
    proc = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "READY_ON": str(ready_on_call)},
        timeout=30,
    )
    return proc.returncode, int(proc.stdout.strip().splitlines()[-1])


def test_a_listener_that_answers_after_the_old_grace_is_still_used() -> None:
    # The old loop gave up after 5 probes; the 20th answering must still count.
    rc, calls = _run(ready_on_call=20)
    assert rc == 0
    assert calls == 20


def test_a_listener_already_answering_is_probed_once() -> None:
    assert _run(ready_on_call=1) == (0, 1)


def test_a_listener_that_never_answers_gives_up_at_the_deadline() -> None:
    rc, calls = _run(ready_on_call=0, budget="1")
    assert rc == 1
    assert calls >= 1


def test_an_invalid_port_does_not_wait() -> None:
    src = _START.read_text()
    start = src.index("provision_wait_for_proxy_listener() {")
    fn = src[start : src.index("\n}\n", start) + 3]
    proc = subprocess.run(
        ["bash", "-c", fn + "provision_proxy_listener_ready() { exit 9; }\n"
         'provision_wait_for_proxy_listener ""\n'],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 1


def test_the_upgrade_path_uses_the_bounded_wait() -> None:
    src = _START.read_text()
    block = src[src.index("Give the protected listener the same grace") :]
    block = block[: block.index("Reconfiguring Caddy for this release")]
    assert "provision_wait_for_proxy_listener" in block
