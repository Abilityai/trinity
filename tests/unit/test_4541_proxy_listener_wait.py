"""#4541 — an upgrade waits for the PROXY-protocol listener before rendering Caddy.

A recreated web container answers on the ordinary listener before its PROXY
listener does. The upgrade path gave the PROXY listener 5 x 2 s, lost that race,
and left Caddy on the plain listener, so every visitor reached Trinity as the
proxy's address. The wait now has the ordinary listener's 90 s budget.

The function is run with the probe, `sleep` and `date` stubbed (each probe
advances the clock 2 s), which pins the retry count and the 90 s default. The
upgrade path is run through the real start.sh with the ent#873 harness, so the
call site and its budget are held by behaviour.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from unit import test_ent873_caddy_upgrade as h

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
        # Each probe moves the clock 2 s, so the budget maps to a probe count.
        + 'date() { echo $((1700000000 + calls * 2)); }\n'
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


def test_a_listener_that_never_answers_gives_up_after_90_seconds() -> None:
    # 2 s per probe, 90 s default: the 45th failed probe reaches the deadline.
    assert _run(ready_on_call=0) == (1, 45)


def test_an_explicit_budget_is_honoured() -> None:
    assert _run(ready_on_call=0, budget="10") == (1, 5)


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


# --- The upgrade path, executed through the real start.sh -------------------

def _upgrade(monkeypatch, tmp_path, listener, ready_on: int):
    """Run `start.sh --hosted --unattended` with `docker port` publishing the
    PROXY listener from its Nth call (0 = never) and the clock stepping 2 s per
    read. Returns (run, listener, number of `docker port` calls)."""
    port_branch = """if args[:1] == ["port"]:
    out = os.environ.get("DOCKER_PORT_OUT", "")
    if out:"""
    assert port_branch in h._FAKE_DOCKER
    monkeypatch.setattr(h, "_FAKE_DOCKER", h._FAKE_DOCKER.replace(port_branch, """if args[:1] == ["port"]:
    cf = os.path.join(os.environ["TMPROOT"], "port_calls")
    n = int(open(cf).read()) + 1 if os.path.exists(cf) else 1
    open(cf, "w").write(str(n))
    ready_on = int(os.environ["PORT_READY_ON"])
    out = os.environ.get("DOCKER_PORT_OUT", "") if ready_on and n >= ready_on else ""
    if out:"""))
    assert "t + 30" in h._STUBS["date"]
    monkeypatch.setitem(h._STUBS, "date", h._STUBS["date"].replace("t + 30", "t + 2"))
    srv = listener("proxy-200")
    r = h._install(tmp_path, "--hosted", "--unattended",
                   env={"CADDY_PROXY_PORT": str(srv.port), "DOCKER_PORT_OUT": f"127.0.0.1:{srv.port}",
                        "PORT_READY_ON": str(ready_on)})
    calls = int((r.root / "port_calls").read_text())
    return r, srv, calls


listener = h.listener


def test_a_listener_published_late_is_used_in_one_upgrade_run(monkeypatch, tmp_path, listener):
    # The old 5 x 2 s grace gave up before the 10th check.
    r, srv, _ = _upgrade(monkeypatch, tmp_path, listener, ready_on=10)
    assert r.proc.returncode == 0, r.proc.stderr[-2000:]
    assert h._protected(r.caddyfile, srv.port), r.caddyfile


def test_a_listener_never_published_falls_back_after_the_full_wait(monkeypatch, tmp_path, listener):
    r, _, calls = _upgrade(monkeypatch, tmp_path, listener, ready_on=0)
    assert r.proc.returncode == 0, r.proc.stderr[-2000:]
    assert "reverse_proxy 127.0.0.1:8081 {" in r.caddyfile
    assert "NOT yet passed through" in r.proc.stderr
    # ~90 s at 2 s per check; a 10 s budget or no wait gives at most 6.
    assert 40 <= calls <= 50, calls
