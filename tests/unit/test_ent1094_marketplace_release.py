"""Marketplace release scripts (abilityai/trinity-enterprise#1094).

Runs the real scripts under `packer/digitalocean/scripts/` against fake `doctl`,
`curl`, `ssh`, `packer` and `git` binaries. The fakes share one JSON world file,
so a droplet the script creates stays until the script deletes it.
Related flow: docs/memory/feature-flows/hosted-install.md
"""
from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PACKER = _ROOT / "packer" / "digitalocean"
_SCRIPTS = _PACKER / "scripts"
_TOKEN = "dop_v1_testtoken"
_TAG = "v1.0.0"
_SNAPSHOT = "1000001"
_APP = "app123"
_PAGE = "https://203.0.113.7/"

_FAKE = r'''#!__PY__
import json, os, re, sys
tool = os.path.basename(sys.argv[0])
args = sys.argv[1:]
base = os.environ["FAKE_WORLD"]
world = json.load(open(base))
stdin = sys.stdin.read() if not sys.stdin.isatty() and tool in ("curl", "ssh") else ""
sent = ""
if "--data-binary" in args and args[args.index("--data-binary") + 1].startswith("@/"):
    sent = open(args[args.index("--data-binary") + 1][1:]).read()
with open(base + ".calls", "a") as fh:
    fh.write(json.dumps({"tool": tool, "args": args, "stdin": stdin, "sent": sent}) + "\n")
def save():
    json.dump(world, open(base, "w"))
def finish(out="", rc=0):
    sys.stdout.write(out)
    sys.stdout.flush()
    sys.exit(rc)
joined = " ".join(args)
def maybe_hang(text):
    if world.get("hang_on") and world["hang_on"] in text:
        import time
        time.sleep(world.get("hang_secs", 30))
if tool == "doctl":
    maybe_hang(joined)
    if "snapshot get" in joined:
        finish(world["snapshot"] + "\n", 0 if world["snapshot"] else 1)
    if "droplet create" in joined:
        if "PublicIPv4" in joined:
            if world.get("prober_fails"):
                finish("", 1)
            world["droplets"].append("222")
            save()
            finish("198.51.100.9\n")
        world["droplets"].append("111")
        save()
        finish("111\n")
    if "droplet list" in joined:
        if world.get("list_fails") and world["droplets"] == ["gone-from-view"]:
            finish("", 1)
        if "--tag-name" not in joined:
            finish("".join(f"{d} {world['ip']} {world['stray']}\n" for d in world["droplets"]))
        if "PublicIPv4" in joined:
            finish(f"{world['ip']}\n" if world["droplets"] else "")
        finish("".join(d + "\n" for d in world["droplets"]))
    if "droplet delete" in joined:
        world.setdefault("deleted_tags", []).append(args[args.index("--tag-name") + 1])
        if world.get("list_fails"):
            world["droplets"] = ["gone-from-view"]
        elif not world["delete_fails"]:
            world["droplets"] = []
        save()
        finish()
    finish()
if tool == "ssh":
    command = " ".join(args[next(i for i, a in enumerate(args) if a.startswith("root@")) + 1:])
    maybe_hang(command)
    probed = re.search(r"http://[0-9.]+:(\d+)", command)
    if probed:
        port = probed.group(1)
        if port in world.get("silent_ports", []):
            finish("0.012000 28\n")  # accepted the connection, then said nothing
        if port in world.get("dropped_ports", []):
            finish("0.000000 28\n")
        finish("0.012000 0\n" if port in world["open_ports"] else "0.000000 7\n")
    for needle, out, rc in world["ssh"]:
        if needle in command:
            finish(out, rc)
    finish("", 0)
if tool == "curl":
    url = next((a for a in reversed(args) if a.startswith("http")), "")
    method = args[args.index("-X") + 1] if "-X" in args else "GET"
    def matches(needle):
        # A full URL means that URL only, so the page rule cannot answer for the API.
        return needle == url if needle.startswith("https://") else needle in url
    # A rule that survives a set number of answers, then fails: [needle, method, answers left, code, rc].
    for rule in world.get("then_fail", []):
        if matches(rule[0]) and rule[1] == method:
            if rule[2] == 0:
                if "-o" in args:
                    open(args[args.index("-o") + 1], "w").write(world["curl_failure_body"])
                finish(str(rule[3]) if "-w" in args else "", rule[4])
            rule[2] -= 1
            save()
    for needle, body, code, rc in world["curl"]:
        if matches(needle):
            if needle == "vendor-portal" and method == "PATCH":
                body, code = world["patch"]
            if "-o" in args:
                open(args[args.index("-o") + 1], "w").write(body)
                body = ""
            if "-w" in args:
                body += str(code)
            if "-f" in "".join(a for a in args if a.startswith("-") and not a.startswith("--")) and code >= 400:
                finish("", 22)
            finish(body, rc)
    finish("", 7)
if tool == "packer":
    if args[0] == "build":
        if world.get("packer_hang"):
            import time
            open(base + ".pid", "w").write(str(os.getpid()))
            time.sleep(20)
        if world["packer_rc"]:
            finish("", world["packer_rc"])
        json.dump({"builds": [{"artifact_id": f"sfo2:{world['built_id']}"}]}, open("manifest.json", "w"))
    finish()
if tool == "git":
    if "diff" in args:
        finish("", world["git_diff_rc"])
    if "rev-parse" in args and "--show-toplevel" in args:
        finish(world["repo_root"] + "\n")
    finish("abc\n", world["git_tag_rc"])
'''

_LISTING = {
    "apps": [{
        "current": {
            "appId": _APP,
            "status": {"value": "approved"},
            "customData": {
                "version": "0.9.5",
                "softwareIncluded": [
                    {"name": "Trinity", "version": "0.9.5", "website": "x", "licenseType": "Apache-2.0"},
                    {"name": "Caddy", "version": "2.11.4", "website": "c", "licenseType": "Apache-2.0"},
                    {"name": "Redis", "version": "7", "website": "r", "licenseType": "RSALv2/SSPLv1"},
                ],
            },
        },
        "unpublished": None,
    }]
}


def _ssh_defaults() -> list:
    pinned = "ghcr.io/abilityai/trinity-{}:" + _TAG
    return [
        ["test ! -e /etc/trinity/firstboot-failed", "", 0],
        ["firstboot-failed", "", 1],
        ["localhost:8000/health", "ok", 0],
        ["setup/status", '{"setup_completed": false}', 0],
        ["setup/admin-password", "200", 0],
        ["cloud-init status", "status: done\n", 0],
        ["tls-status", "ok\n", 0],
        ["trinity.db", "", 0],
        ["printenv DATABASE_URL", "", 0],
        ["TRINITY_INSTALL_SOURCE", "", 0],
        *[[f"Config.Image}}}}' trinity-{s}", pinned.format(s) + "\n", 0]
          for s in ("backend", "frontend", "scheduler", "mcp-server")],
        ["image inspect", "sha256:same\n", 0],
        ["docker ps", "0.0.0.0:8000->8000/tcp\n0.0.0.0:4317-4318->4317-4318/tcp, 0.0.0.0:8080->8080/tcp\n", 0],
        ["iptables", "", 0],
        ["caddy version", "v2.11.8 h1:abc\n", 0],
        ["State.Status", "running 0\n", 0],
    ]


class Rig:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.home = tmp / "home"
        self.home.mkdir()
        self.world_path = tmp / "world.json"
        self.work = tmp / "work"
        self.work.mkdir()
        fake = self.bin / "_fake"
        fake.write_text(_FAKE.replace("__PY__", sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        for tool in ("doctl", "curl", "ssh", "packer", "git"):
            (self.bin / tool).symlink_to(fake)
        self.key = tmp / "key"
        self.key.write_text("k")
        self.world = {
            "snapshot": f"trinity-v1-0-0-20261104-0954 [sfo2]",
            "droplets": [],
            "ip": "203.0.113.7",
            "stray": "",
            "delete_fails": False,
            "ssh": _ssh_defaults(),
            "open_ports": [],
            "curl": [
                ["vendor-portal", json.dumps(_LISTING), 200, 0],
                ["/api/setup/status", '{"setup_completed": true}', 200, 0],
                ["/api/token", '{"access_token": "jwt123"}', 200, 0],
                ["/api/agents/boot-check-agent", '{"status": "running"}', 200, 0],
                ["/api/agents", "{}", 200, 0],
                [_PAGE, "HTTP/2 200\nx-do-marketplace: trinity\n\n<title>Trinity</title>", 200, 0],
            ],
            "curl_failure_body": json.dumps(_LISTING),
            "patch": ['{"status": {"value": "pending"}}', 200],
            "packer_rc": 0,
            "built_id": "999",
            "git_diff_rc": 0,
            "git_tag_rc": 0,
            "repo_root": str(self.work),
        }
        self.save()

    def save(self) -> None:
        self.world_path.write_text(json.dumps(self.world))

    def set_ssh(self, needle: str, out: str, rc: int = 0) -> None:
        self.world["ssh"].insert(0, [needle, out, rc])
        self.save()

    def set_curl(self, needle: str, body: str, code: int = 200, rc: int = 0) -> None:
        self.world["curl"].insert(0, [needle, body, code, rc])
        self.save()

    def listing(self, **fields) -> None:
        data = json.loads(json.dumps(_LISTING))
        data["apps"][0].update(fields)
        self.world["curl"] = [c for c in self.world["curl"] if c[0] != "vendor-portal"]
        self.set_curl("vendor-portal", json.dumps(data))

    def state(self) -> dict:
        return json.loads(self.world_path.read_text())

    def calls(self, tool: str | None = None) -> list:
        path = Path(str(self.world_path) + ".calls")
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        return [r for r in rows if tool is None or r["tool"] == tool]

    def env(self, **extra) -> dict:
        env = {
            "PATH": f"{self.bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.home),
            "FAKE_WORLD": str(self.world_path),
            "DIGITALOCEAN_TOKEN": _TOKEN,
            "TRINITY_DO_APP_ID": _APP,
            "TRINITY_SSH_KEY_ID": "4242",
            "TRINITY_SSH_KEY_FILE": str(self.key),
            "TRINITY_BOOT_CHECK_TIMEOUT": "1",
            "TRINITY_BOOT_CHECK_POLL": "0",
        }
        env.update(extra)
        return env

    def run(self, script: str, *args, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(_SCRIPTS / script), *args],
            cwd=self.work,
            env=env or self.env(),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def pass_record(self, snapshot: str = _SNAPSHOT, tag: str = _TAG) -> Path:
        record = self.home / ".trinity-marketplace" / f"{snapshot}.pass"
        record.parent.mkdir(exist_ok=True)
        record.write_text(f"tag={tag}\ncaddy=2.11.8\n")
        return record


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def _assert_no_secret_in_argv(rig: Rig, secret: str) -> None:
    for call in rig.calls():
        assert not any(secret in a for a in call["args"]), f"{secret} in argv: {call}"


class TestStatus:
    def test_clear_listing_exits_zero_and_names_the_version(self, rig: Rig) -> None:
        result = rig.run("mp-status.sh")
        assert result.returncode == 0, result.stderr
        assert "live_version=0.9.5" in result.stdout

    @pytest.mark.parametrize("value", ["pending", "in review", "inReview"])
    def test_a_queued_submission_exits_three(self, rig: Rig, value: str) -> None:
        rig.listing(unpublished={"status": {"value": value}})
        assert rig.run("mp-status.sh").returncode == 3

    @pytest.mark.parametrize("unpublished", [
        {"status": {"value": "rejected"}},
        {"status": {"value": "some-new-state"}},
        {"no": "status"},
    ])
    def test_rejected_or_unknown_exits_four(self, rig: Rig, unpublished: dict) -> None:
        rig.listing(unpublished=unpublished)
        assert rig.run("mp-status.sh").returncode == 4

    def test_current_listing_not_approved_exits_four(self, rig: Rig) -> None:
        data = json.loads(json.dumps(_LISTING))
        data["apps"][0]["current"]["status"]["value"] = "rejected"
        rig.world["curl"] = [c for c in rig.world["curl"] if c[0] != "vendor-portal"]
        rig.set_curl("vendor-portal", json.dumps(data))
        assert rig.run("mp-status.sh").returncode == 4

    def test_api_failure_and_unknown_app_exit_one(self, rig: Rig) -> None:
        assert rig.run("mp-status.sh", env=rig.env(TRINITY_DO_APP_ID="other")).returncode == 1
        rig.world["curl"] = [c for c in rig.world["curl"] if c[0] != "vendor-portal"]
        rig.set_curl("vendor-portal", '{"id":"Unauthorized"}', 401)
        assert rig.run("mp-status.sh").returncode == 1

    def test_a_stalled_portal_times_out_as_an_api_failure(self, rig: Rig) -> None:
        rig.set_curl("vendor-portal", "", 0, 28)
        assert rig.run("mp-status.sh").returncode == 1
        assert all("--max-time" in c["args"] for c in rig.calls("curl"))

    def test_token_stays_out_of_argv(self, rig: Rig) -> None:
        rig.run("mp-status.sh")
        assert rig.calls("curl")
        _assert_no_secret_in_argv(rig, _TOKEN)


class TestSubmit:
    def _patches(self, rig: Rig) -> list:
        return [c for c in rig.calls("curl") if "PATCH" in c["args"]]

    def test_requires_a_snapshot_argument(self, rig: Rig) -> None:
        rig.pass_record()
        result = rig.run("mp-submit.sh")
        assert result.returncode != 0
        assert not self._patches(rig)

    def test_missing_app_id_fails_and_names_it(self, rig: Rig) -> None:
        env = rig.env()
        del env["TRINITY_DO_APP_ID"]
        result = rig.run("mp-submit.sh", _SNAPSHOT, env=env)
        assert result.returncode != 0
        assert "TRINITY_DO_APP_ID" in result.stderr

    def test_refuses_a_snapshot_without_a_pass_record(self, rig: Rig) -> None:
        result = rig.run("mp-submit.sh", _SNAPSHOT)
        assert result.returncode != 0
        assert "boot-check" in result.stderr
        assert not self._patches(rig)

    def test_refuses_while_a_submission_is_queued(self, rig: Rig) -> None:
        rig.pass_record()
        rig.listing(unpublished={"status": {"value": "pending"}})
        assert rig.run("mp-submit.sh", _SNAPSHOT).returncode == 3
        assert not self._patches(rig)

    def test_submits_the_named_snapshot_and_keeps_other_software(self, rig: Rig) -> None:
        record = rig.pass_record()
        result = rig.run("mp-submit.sh", _SNAPSHOT)
        assert result.returncode == 0, result.stderr
        patch = self._patches(rig)
        assert len(patch) == 1 and patch[0]["args"][-1].endswith(f"/{_APP}")
        body = json.loads(patch[0]["sent"])
        assert body["imageId"] == int(_SNAPSHOT)
        assert body["version"] == "1.0.0", "the listing's own version moves with the release"
        versions = {s["name"]: s["version"] for s in body["softwareIncluded"]}
        assert versions == {"Trinity": "1.0.0", "Caddy": "2.11.8", "Redis": "7", "PostgreSQL": "16"}
        assert not record.exists(), "a consumed snapshot keeps no pass record"
        _assert_no_secret_in_argv(rig, _TOKEN)

    def test_an_existing_postgresql_entry_moves_and_stays_single(self, rig: Rig) -> None:
        data = json.loads(json.dumps(_LISTING))
        data["apps"][0]["current"]["customData"]["softwareIncluded"].append(
            {"name": "PostgreSQL", "version": "99", "website": "p", "licenseType": "PostgreSQL"}
        )
        rig.world["curl"] = [c for c in rig.world["curl"] if c[0] != "vendor-portal"]
        rig.set_curl("vendor-portal", json.dumps(data))
        rig.pass_record()
        assert rig.run("mp-submit.sh", _SNAPSHOT).returncode == 0
        body = json.loads(self._patches(rig)[0]["sent"])
        assert [s["version"] for s in body["softwareIncluded"] if s["name"] == "PostgreSQL"] == ["16"]

    def test_a_record_with_no_caddy_version_sends_nothing(self, rig: Rig) -> None:
        record = rig.pass_record()
        record.write_text(f"tag={_TAG}\n")
        result = rig.run("mp-submit.sh", _SNAPSHOT)
        assert result.returncode != 0
        assert "no Caddy version" in result.stderr, result.stderr
        assert self._patches(rig) == []

    def test_a_failed_listing_read_sends_nothing(self, rig: Rig) -> None:
        """mp-status reads the listing first, so only the second read fails here."""
        rig.world["then_fail"] = [["vendor-portal", "GET", 1, 500, 0]]
        rig.save()
        record = rig.pass_record()
        result = rig.run("mp-submit.sh", _SNAPSHOT)
        assert result.returncode != 0
        assert "listing read returned 500" in result.stderr, result.stderr
        assert self._patches(rig) == []
        assert record.exists()

    def test_a_compose_file_without_postgres_sends_nothing(self, rig: Rig, tmp_path: Path) -> None:
        """A blank version in a PATCH clears the field, so it must never be sent."""
        repo = tmp_path / "repo"
        scripts = repo / "packer" / "digitalocean" / "scripts"
        scripts.mkdir(parents=True)
        for name in ("mp-submit.sh", "mp-status.sh", "mp-lib.sh"):
            (scripts / name).write_text((_SCRIPTS / name).read_text())
        (repo / "docker-compose.hosted.yml").write_text("services:\n  redis:\n    image: redis:7\n")
        record = rig.pass_record()
        result = subprocess.run(
            ["bash", str(scripts / "mp-submit.sh"), _SNAPSHOT],
            cwd=rig.work, env=rig.env(), capture_output=True, text=True, timeout=60,
        )
        assert result.returncode != 0
        assert "no postgres image" in result.stderr, result.stderr
        assert self._patches(rig) == []
        assert record.exists()

    def test_a_vendor_400_fails_and_keeps_the_pass_record(self, rig: Rig) -> None:
        record = rig.pass_record()
        rig.world["patch"] = ['{"message": "locked"}', 400]
        rig.save()
        result = rig.run("mp-submit.sh", _SNAPSHOT)
        assert result.returncode != 0
        assert "in review" in result.stderr
        assert record.exists()


class TestBootCheck:
    def _run(self, rig: Rig, **env) -> subprocess.CompletedProcess:
        return rig.run("boot-check.sh", _SNAPSHOT, _TAG, env=rig.env(**env))

    def _pass_file(self, rig: Rig) -> Path:
        return rig.home / ".trinity-marketplace" / f"{_SNAPSHOT}.pass"

    def test_pass_writes_the_record_and_destroys_the_droplet(self, rig: Rig) -> None:
        result = self._run(rig)
        assert result.returncode == 0, result.stdout + result.stderr
        assert self._pass_file(rig).read_text().splitlines() == [f"tag={_TAG}", "caddy=2.11.8"]
        assert rig.state()["droplets"] == []

    def test_droplet_shuts_itself_down_and_uses_the_snapshot_region(self, rig: Rig) -> None:
        self._run(rig)
        create = next(c for c in rig.calls("doctl") if "create" in c["args"])
        args = create["args"]
        user_data = args[args.index("--user-data") + 1]
        assert "shutdown -P +45" in user_data
        assert "bootcmd" not in user_data, "systemd-run in bootcmd wedged a real boot"
        assert args[args.index("--region") + 1] == "sfo2"
        assert args[args.index("--size") + 1] == "s-4vcpu-8gb"
        assert args[args.index("--image") + 1] == _SNAPSHOT

    def test_password_and_jwt_stay_out_of_argv(self, rig: Rig) -> None:
        self._run(rig)
        claim = next(c for c in rig.calls("ssh") if "admin-password" in " ".join(c["args"]))
        password = json.loads(claim["stdin"])["password"]
        assert len(password) >= 20
        _assert_no_secret_in_argv(rig, password)
        _assert_no_secret_in_argv(rig, "jwt123")

    def test_a_failed_recheck_removes_the_earlier_pass(self, rig: Rig) -> None:
        """The newest result decides, so an old pass can't carry a snapshot through submit."""
        record = rig.pass_record()
        rig.set_ssh("tls-status", "failed\n")
        assert self._run(rig).returncode != 0
        assert not record.exists()
        assert rig.run("mp-submit.sh", _SNAPSHOT).returncode != 0
        assert [c for c in rig.calls("curl") if "PATCH" in c["args"]] == []

    @pytest.mark.parametrize("service", ["backend", "frontend", "scheduler", "mcp-server"])
    @pytest.mark.parametrize("state", ["exited 0", "running 3", "restarting 1"])
    def test_a_container_that_stopped_or_restarted_fails(self, rig: Rig, service, state) -> None:
        rig.world["ssh"].insert(0, [f"{{{{.RestartCount}}}}' trinity-{service}", f"{state}\n", 0])
        rig.save()
        result = self._run(rig)
        assert result.returncode != 0
        assert f"trinity-{service} is {state}" in result.stderr, result.stderr
        assert not self._pass_file(rig).exists()

    def test_remote_calls_that_can_hang_carry_a_time_limit(self, rig: Rig) -> None:
        """DEADLINE covers only wait_for, so a hung backend must not stall the run."""
        assert self._run(rig).returncode == 0
        commands = [" ".join(c["args"]) for c in rig.calls("ssh")]
        claim = next(c for c in commands if "setup/admin-password" in c)
        assert " -m " in claim
        docker = [c for c in commands if " docker " in c]
        assert docker
        assert all(" timeout 30 docker " in c for c in docker), docker

    @pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
    def test_a_signal_during_cleanup_still_destroys_the_droplet(self, rig: Rig, sig) -> None:
        """Cleanup holds the only copy of the job of deleting a billing droplet."""
        rig.set_ssh("tls-status", "failed\n")
        rig.world["hang_on"] = "trinity-firstboot.log"
        rig.world["hang_secs"] = 3
        rig.save()
        proc = subprocess.Popen(
            ["bash", str(_SCRIPTS / "boot-check.sh"), _SNAPSHOT, _TAG],
            cwd=rig.work, env=rig.env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.time() + 20
        while time.time() < deadline and not any(
            "trinity-firstboot.log" in " ".join(c["args"]) for c in rig.calls("ssh")
        ):
            time.sleep(0.05)
        proc.send_signal(sig)
        assert proc.wait(timeout=30) == 1, "the check's own failure decides the exit code"
        assert rig.state()["droplets"] == []

    def test_a_snapshot_for_another_tag_never_creates_a_droplet(self, rig: Rig) -> None:
        rig.world["snapshot"] = "trinity-v1-0-0-rc1-20261104-0954 [sfo2]"
        rig.save()
        assert self._run(rig).returncode != 0
        assert not [c for c in rig.calls("doctl") if "create" in c["args"]]

    @pytest.mark.parametrize("needle,out,rc", [
        ("firstboot-failed", "", 0),
        ("test ! -e /etc/trinity/firstboot-failed", "", 1),
        ("localhost:8000/health", "", 7),
        ("setup/status", '{"setup_completed": true}', 0),
        ("setup/admin-password", "403", 0),
        ("cloud-init status", "status: error\n", 1),
        ("tls-status", "failed\n", 0),
        ("trinity.db", "", 1),
        ("printenv DATABASE_URL", "", 1),
        ("TRINITY_INSTALL_SOURCE", "", 1),
        ("Config.Image}}' trinity-scheduler", "ghcr.io/abilityai/trinity-scheduler:v0.9.5\n", 0),
        ("image inspect", "", 1),
        ("docker ps", "", 0),
        ("iptables", "", 1),
    ])
    def test_each_droplet_check_failure_fails_and_destroys(self, rig: Rig, needle, out, rc) -> None:
        rig.set_ssh(needle, out, rc)
        result = self._run(rig)
        assert result.returncode != 0, f"{needle} did not fail the check\n{result.stdout}"
        assert not self._pass_file(rig).exists()
        assert rig.state()["droplets"] == []

    def test_image_id_mismatch_on_the_agent_base_fails(self, rig: Rig) -> None:
        rig.set_ssh("trinity-agent-base:latest", "sha256:other\n")
        assert self._run(rig).returncode != 0

    @pytest.mark.parametrize("needle,body,code,rc,reason", [
        (_PAGE, "", 0, 60, "trusted certificate"),
        (_PAGE, "HTTP/2 200\n\n<title>Trinity</title>", 200, 0, "X-DO-MARKETPLACE"),
        (_PAGE, "HTTP/2 200\nx-do-marketplace: trinity\n\n<title>nginx</title>", 200, 0, "does not serve Trinity"),
        ("/api/setup/status", '{"setup_completed": false}', 200, 0, "disagree on setup state"),
        ("/api/token", "{}", 401, 22, "sign-in"),
        ("/api/agents/boot-check-agent", '{"status": "starting"}', 200, 0, "the agent to run"),
        ("/api/agents", "{}", 500, 22, "creating an agent failed"),
    ])
    def test_each_off_box_failure_fails_and_destroys(self, rig: Rig, needle, body, code, rc, reason) -> None:
        rig.set_curl(needle, body, code, rc)
        result = self._run(rig)
        assert result.returncode != 0, result.stdout
        assert reason in result.stderr, result.stderr
        assert not self._pass_file(rig).exists()
        assert rig.state()["droplets"] == []

    def test_a_token_that_could_rewrite_the_curl_config_is_refused(self, rig: Rig) -> None:
        """The JWT goes into a curl config on stdin, so a newline there would add options."""
        rig.set_curl("/api/token", json.dumps({"access_token": 'x"\noutput = "/tmp/owned'}))
        result = self._run(rig)
        assert result.returncode != 0
        assert not [c for c in rig.calls("curl") if "/api/agents" in " ".join(c["args"])]

    def test_an_open_container_port_fails(self, rig: Rig) -> None:
        rig.world["open_ports"] = ["8000"]
        rig.save()
        assert self._run(rig).returncode != 0

    def test_every_port_of_a_published_range_is_probed(self, rig: Rig) -> None:
        rig.world["open_ports"] = ["4318"]
        rig.save()
        result = self._run(rig)
        assert result.returncode != 0
        assert "port 4318 is reachable" in result.stderr, result.stderr

    def test_8081_is_probed_when_docker_ps_does_not_list_it(self, rig: Rig) -> None:
        rig.world["open_ports"] = ["8081"]
        rig.save()
        assert self._run(rig).returncode != 0

    def test_a_dead_network_never_reads_as_closed_ports(self, rig: Rig) -> None:
        """Closed ports and no network look alike, so 443 is the positive control."""
        rig.set_ssh(f"--max-time 15 {_PAGE}", "", 7)
        result = self._run(rig)
        assert result.returncode != 0
        assert "443 does not answer the prober" in result.stderr, result.stderr
        assert not self._pass_file(rig).exists()

    def test_a_network_that_answers_every_port_is_refused(self, rig: Rig) -> None:
        rig.world["open_ports"] = ["19999"]
        rig.save()
        result = self._run(rig)
        assert result.returncode != 0
        assert "no listener" in result.stderr, result.stderr
        assert not self._pass_file(rig).exists()

    def test_the_prober_is_destroyed_with_the_droplet(self, rig: Rig) -> None:
        assert self._run(rig).returncode == 0
        assert rig.state()["droplets"] == []
        creates = [c["args"] for c in rig.calls("doctl") if "create" in c["args"]]
        tags = {a[a.index("--tag-names") + 1] for a in creates}
        assert len(creates) == 2 and len(tags) == 1, "one run tag reaps both droplets"
        assert all("shutdown -P +45" in a[a.index("--user-data") + 1] for a in creates)

    def test_a_failed_prober_create_fails(self, rig: Rig) -> None:
        rig.world["prober_fails"] = True
        rig.save()
        result = self._run(rig)
        assert result.returncode != 0
        assert "prober create failed" in result.stderr, result.stderr
        assert rig.state()["droplets"] == []

    def test_the_pass_record_carries_the_images_caddy_version(self, rig: Rig) -> None:
        assert self._run(rig).returncode == 0
        assert "caddy=2.11.8" in self._pass_file(rig).read_text().splitlines()

    def test_an_unreadable_caddy_version_fails(self, rig: Rig) -> None:
        rig.set_ssh("caddy version", "", 127)
        result = self._run(rig)
        assert result.returncode != 0
        assert "Caddy version" in result.stderr, result.stderr
        assert not self._pass_file(rig).exists()

    def test_a_port_that_accepts_and_stays_silent_fails(self, rig: Rig) -> None:
        """A timeout after the connection opened is an open port."""
        rig.world["silent_ports"] = ["8000"]
        rig.save()
        result = self._run(rig)
        assert result.returncode != 0
        assert "port 8000 is reachable" in result.stderr, result.stderr
        assert not self._pass_file(rig).exists()

    def test_a_port_the_firewall_drops_counts_as_closed(self, rig: Rig) -> None:
        rig.world["dropped_ports"] = ["8000"]
        rig.save()
        result = self._run(rig)
        assert result.returncode == 0, result.stderr
        assert self._pass_file(rig).exists()

    @pytest.mark.parametrize("sig,code", [
        (signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129),
    ])
    def test_a_signal_mid_run_destroys_the_droplet(self, rig: Rig, sig, code) -> None:
        rig.world["hang_on"] = "cloud-init status"
        rig.save()
        proc = subprocess.Popen(
            ["bash", str(_SCRIPTS / "boot-check.sh"), _SNAPSHOT, _TAG],
            cwd=rig.work, env=rig.env(TRINITY_BOOT_CHECK_TIMEOUT="60"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.time() + 20
        while time.time() < deadline and not any(
            "cloud-init status" in " ".join(c["args"]) for c in rig.calls("ssh")
        ):
            time.sleep(0.1)
        assert rig.state()["droplets"], "droplet was never created"
        os.killpg(proc.pid, sig)
        assert proc.wait(timeout=30) == code
        assert rig.state()["droplets"] == []

    def test_a_failed_delete_exits_nonzero_and_names_the_droplet(self, rig: Rig) -> None:
        rig.world["delete_fails"] = True
        rig.save()
        result = self._run(rig)
        assert result.returncode != 0
        assert "111" in result.stderr


    def test_an_unreadable_droplet_list_never_reads_as_destroyed(self, rig: Rig) -> None:
        """A dead network answers the list with nothing; that is no proof of deletion."""
        rig.world["list_fails"] = True
        rig.save()
        result = self._run(rig)
        assert result.returncode != 0
        assert "may still exist" in result.stderr


class TestTemplate:
    def test_default_region_offers_the_default_build_size(self) -> None:
        """Pin: `s-1vcpu-1gb` is no longer offered in nyc3, so a default build failed."""
        template = (_PACKER / "trinity.pkr.hcl").read_text()
        region = template[template.index('variable "region"'):]
        assert 'default = "sfo2"' in region.split("}", 1)[0]


class TestBuild:
    def _run(self, rig: Rig, tag: str = _TAG, **env) -> subprocess.CompletedProcess:
        return rig.run("mp-build.sh", tag, env=rig.env(**env))

    def test_stdout_is_only_the_snapshot_id(self, rig: Rig) -> None:
        result = self._run(rig)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "999"

    def test_a_manifest_without_a_snapshot_id_fails(self, rig: Rig) -> None:
        rig.world["built_id"] = ""
        rig.save()
        result = self._run(rig)
        assert result.returncode != 0
        assert "no snapshot ID" in result.stderr, result.stderr
        assert result.stdout.strip() == ""

    def test_a_tag_missing_locally_builds_nothing(self, rig: Rig) -> None:
        rig.world["git_tag_rc"] = 1
        rig.save()
        result = self._run(rig)
        assert result.returncode != 0
        assert "git fetch" in result.stderr
        assert not rig.calls("packer")

    def test_token_reaches_packer_outside_argv(self, rig: Rig) -> None:
        self._run(rig)
        build = next(c for c in rig.calls("packer") if c["args"][0] == "build")
        assert "image_tag=v1.0.0" in build["args"]
        assert "ssh_key_id=4242" in build["args"]
        _assert_no_secret_in_argv(rig, _TOKEN)

    @pytest.mark.parametrize("tag", ["latest", "v1.0.0; rm -rf /", "1.0.0", ""])
    def test_a_bad_tag_builds_nothing(self, rig: Rig, tag: str) -> None:
        assert self._run(rig, tag=tag).returncode != 0
        assert not rig.calls("packer")

    def test_missing_ssh_key_builds_nothing_and_names_it(self, rig: Rig) -> None:
        env = rig.env()
        del env["TRINITY_SSH_KEY_ID"]
        result = rig.run("mp-build.sh", _TAG, env=env)
        assert result.returncode != 0
        assert "TRINITY_SSH_KEY_ID" in result.stderr
        assert not rig.calls("packer")

    def test_a_checkout_that_differs_from_the_tag_builds_nothing(self, rig: Rig) -> None:
        rig.world["git_diff_rc"] = 1
        rig.save()
        result = self._run(rig)
        assert result.returncode != 0
        assert not rig.calls("packer")

    @pytest.mark.parametrize("sig,code", [
        (signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129),
    ])
    def test_a_signal_mid_build_stops_packer_and_reaps(self, rig: Rig, sig, code) -> None:
        rig.world["packer_hang"] = True
        rig.world["droplets"] = ["222"]
        rig.save()
        proc = subprocess.Popen(
            ["bash", str(_SCRIPTS / "mp-build.sh"), _TAG],
            cwd=rig.work, env=rig.env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        pid_file = Path(str(rig.world_path) + ".pid")
        deadline = time.time() + 20
        while time.time() < deadline and not pid_file.exists():
            time.sleep(0.05)
        time.sleep(0.2)
        proc.send_signal(sig)
        assert proc.wait(timeout=30) == code
        assert rig.state()["droplets"] == []
        packer = int(pid_file.read_text())
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(packer, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(packer, signal.SIGKILL)
            pytest.fail("packer outlived the script")

    def test_a_signal_during_the_reap_does_not_stop_it(self, rig: Rig) -> None:
        rig.world["packer_rc"] = 1
        rig.world["droplets"] = ["222"]
        rig.world["hang_on"] = "droplet list"
        rig.world["hang_secs"] = 2
        rig.save()
        proc = subprocess.Popen(
            ["bash", str(_SCRIPTS / "mp-build.sh"), _TAG],
            cwd=rig.work, env=rig.env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.time() + 20
        while time.time() < deadline and not any(
            "droplet list" in " ".join(c["args"]) for c in rig.calls("doctl")
        ):
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 1
        assert rig.state()["droplets"] == []

    def test_a_failed_build_deletes_only_its_own_tag(self, rig: Rig) -> None:
        rig.world["packer_rc"] = 1
        rig.world["droplets"] = ["111"]
        rig.save()
        assert self._run(rig).returncode != 0
        build = next(c for c in rig.calls("packer") if c["args"][0] == "build")
        run_tag = next(a.split("=", 1)[1] for a in build["args"] if a.startswith("run_tag="))
        assert run_tag.startswith("trinity-build-")
        assert rig.state()["deleted_tags"] == [run_tag]


def test_a_build_can_no_longer_submit() -> None:
    """Pin on the template: packer has no runnable harness in the unit island."""
    template = (_PACKER / "trinity.pkr.hcl").read_text()
    assert "shell-local" not in template
    assert "mp-submit" not in template.split("post-processor", 1)[1]


def test_checked_containers_exist_in_the_hosted_compose() -> None:
    """boot-check.sh addresses containers by name; compose owns those names."""
    compose = (_ROOT / "docker-compose.hosted.yml").read_text()
    script = (_SCRIPTS / "boot-check.sh").read_text()
    for service in ("backend", "frontend", "scheduler", "mcp-server"):
        assert f"container_name: trinity-{service}" in compose
        assert service in script
