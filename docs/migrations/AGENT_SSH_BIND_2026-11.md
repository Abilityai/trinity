# Agent SSH Port Binding Migration (trinity-enterprise#1168)

**Applies to**: existing Trinity deployments upgrading to the release that
contains trinity-enterprise#1168. Every agent container created **before** that
release publishes its SSH port on every host interface and runs an sshd from
boot, whether or not SSH access is enabled.

**Fresh installs**: skip this document. Every agent Trinity creates now
publishes SSH on `127.0.0.1` (unless `AGENT_SSH_BIND_ADDRESS` says otherwise)
and runs no sshd until an admin issues an SSH grant.

---

## What changed

Docker publishes a port with no host address on **every** interface, and it
does so by inserting its own iptables rules ahead of the host firewall — so a
ufw policy of "22, 80, 443 only" never applied to the agent SSH range
2222–2262. Three things change:

| Path | Before | After |
|---|---|---|
| Agent create (`crud.py`), config recreate and recovery rebuild (`lifecycle.py`), system agent (`system_agent_service.py`) | `ports={'22/tcp': <port>}` → `0.0.0.0` | `ports=agent_ssh_port_binding(port)` → `AGENT_SSH_BIND_ADDRESS`, `127.0.0.1` by default |
| Container env | `ENABLE_SSH=true` → sshd from boot | `ENABLE_SSH=false` → no sshd until a grant starts one |
| `POST /api/agents/{name}/ssh-access` | advertised the detected host regardless of where the port was bound | advertises the host the port is **actually** published on; `warnings[]` + `connection.bind_address` say so |
| Backend boot | — | one sweep: logs the resolved bind address, names containers still on every interface, stops idle sshd where `ssh_access_enabled` is off |
| Base image | `developer:developer` password; sshd hardened by `sed` | `developer` password locked; sshd policy is a drop-in whose effective values the image build asserts (`sshd -T`) |

Nothing changes for a running container until it is **recreated**: the binding
is creation-time, exactly like the restart policy (#2541) and the log cap
(#1871). A plain restart keeps the old binding and the old `ENABLE_SSH=true`.

---

## Why an upgrade step is required

After deploying the release:

- **New and recreated agents** are correct automatically.
- **Existing agents** keep publishing on every interface. Their running sshd is
  stopped at the next backend boot **only if** `ssh_access_enabled` is off (the
  default); with the setting on, the listener stays up because a grant may be
  active. Either way the *port* stays published on `0.0.0.0` until a recreate.
- The SSH grant response names each such agent ("created before
  `AGENT_SSH_BIND_ADDRESS=…` applied — recreate it"), and the boot log lists them
  in one WARNING.

A recreate happens on a config change, a base-image adoption,
`POST /api/ops/fleet/restart`, or a #1559 recovery rebuild — so an active fleet
converges eventually. This runbook is "don't wait for one". The fix **closes the
population**: after this release the set of every-interface agent containers is
finite and can never grow.

---

## Upgrade procedure

### 0. Decide the binding

| Posture | `.env` | Who can connect |
|---|---|---|
| **Default** | leave `AGENT_SSH_BIND_ADDRESS` unset (or `127.0.0.1`) | from the Trinity host, or through it: `ssh -J you@<host> -p <port> developer@127.0.0.1` |
| VPN interface | `AGENT_SSH_BIND_ADDRESS=<tailscale ip>` and `SSH_HOST=<the same>` | anyone on the VPN, directly. Every agent start then needs that interface up — after a reboot, agents restart-loop until `tailscaled` is. Trinity's `DOCKER-USER` rules (if installed) still drop the traffic unless you allow `tailscale0` there |
| Every interface | `AGENT_SSH_BIND_ADDRESS=0.0.0.0` | the pre-#1168 behaviour. Pair it with `scripts/deploy/docker-firewall.sh`, which is the actual control; the binding alone closes nothing here |

The value must be an IP literal. Anything else (a hostname, `tailscale0`,
`[::1]`, `ip:port`) falls back to `127.0.0.1` and logs one ERROR at the first
create. The backend reads it at **container create**, so restart the backend
after changing it (`./scripts/deploy/start.sh`, or `start.sh --hosted`).

### 1. Firewall first — this is the control, the binding is defense in depth

If the host was not provisioned with `start.sh --provision`:

```bash
sudo ./scripts/deploy/docker-firewall.sh
```

Loopback publication is not a firewall: Docker engines before the moby
loopback-DNAT fix could reach a `127.0.0.1` publication from off-box via
`route_localnet`, and a specific interface IP is only as closed as the
interface. `docker-firewall.sh` fills `DOCKER-USER` without a per-port list.

**Interim only** — if you cannot run the script yet and need the agent SSH
range closed on the public interface right now:

```bash
# Interim workaround: drops inbound to the agent SSH range on the public NIC.
# Replace eth0. This is the port-list shape docker-firewall.sh exists to avoid;
# replace it with the script when you can.
sudo iptables -I DOCKER-USER -i eth0 -p tcp --dport 2222:2262 -j DROP
```

### 2. Census — what still publishes on every interface

```bash
for c in $(docker ps -a --format '{{.Names}}' | grep '^agent-'); do
  printf '%s %s\n' "$c" "$(docker inspect -f '{{(index (index .HostConfig.PortBindings "22/tcp") 0).HostIp}}' "$c")"
done
```

An empty second column is Docker's spelling of `0.0.0.0`. The backend logs the
same list at boot:
`N agent container(s) still publish SSH on every host interface … Recreate them`.

### 3. Rebuild the base image

```bash
./scripts/deploy/build-base-image.sh
```

The new image locks the `developer` password and installs the sshd drop-in; the
build **fails** (rather than shipping) if the effective sshd config does not
show `passwordauthentication no`, `kbdinteractiveauthentication no`,
`permitrootlogin no`, `pubkeyauthentication yes` and `usepam yes`. Old images
keep the `sed`-hardened sshd, which is correct today — this step closes the
"silent no-op the day Debian changes the text" class, not a live hole.

It also does the work for step 4: a new image is *image drift*, and image drift
is what makes an agent's next start a recreate.

### 4. Recreate

The binding cannot be changed on an existing container (`docker update` has no
port option); the container must be recreated. Trinity's recreate keeps the
workspace volume (`agent-<name>-workspace`) — the agent's data is untouched.

The drift ladder does **not** read the port binding (deliberately — a predicate
on it would recreate running agents, the #1560/#1557 class), so a stop/start of
an otherwise-converged agent keeps the old binding. After step 3 every agent is
image-drifted, so a restart recreates it: per agent from the UI, or all at once
with the fleet restart:

```bash
# all agents, one call (admin token); recreates every agent whose image drifted
curl -s -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/ops/fleet/restart
```

`trinity-system` adopts the new image on its next cold start the same way
(#1816). Without a rebuild, the only other recreate triggers are a config
change that the ladder already checks (resources, capabilities, auth mode,
shared folders) and a #1559 recovery rebuild.

### 5. Verify

```bash
# every agent-* should show 127.0.0.1 (or the IP you configured)
for c in $(docker ps -a --format '{{.Names}}' | grep '^agent-'); do
  printf '%s %s\n' "$c" "$(docker inspect -f '{{(index (index .HostConfig.PortBindings "22/tcp") 0).HostIp}}' "$c")"
done

# with ssh_access_enabled off: no sshd in a running agent
docker exec agent-<name> pgrep -xa sshd || echo "no listener (expected)"

# from OFF the host, the public interface must not answer (replace the IP)
nc -vz -w 3 <public-ip> 2222   # expect: connection refused / timed out
```

Then issue an SSH grant (`get_agent_ssh_access`, or the API) and read its
`warnings` — they are empty once the binding and the advertised host agree.

---

## Operational consequences

### The grant starts sshd; nothing else does

`ENABLE_SSH=false` at create means a freshly created or recreated agent runs
**no** sshd until `POST /api/agents/{name}/ssh-access` starts one (root exec,
idempotent). A listener stays up after a grant expires (the key is removed; it
answers nobody) until the next backend boot stops it — only when
`ssh_access_enabled` is off. Toggling the setting on later needs no restart:
the next grant starts sshd on demand.

### Specific interface IP = reboot ordering

With `AGENT_SSH_BIND_ADDRESS=<tailscale ip>`, Docker must bind that address at
every container start. After a host reboot the agents (`unless-stopped`) start
before `tailscaled` has the address, fail with `cannot assign requested address`,
and restart-loop until it does; the `High restart count` alert will fire. The
create API returns a 400 naming `AGENT_SSH_BIND_ADDRESS` when the address is
missing at create. If this bites, prefer the default plus `-J`, or `0.0.0.0`
plus `docker-firewall.sh`.

### macOS / Docker Desktop

Host detection falls back to `host.docker.internal`, which is **not** dialable
from the Mac itself; with the loopback default the grant response simply says
`127.0.0.1`, which is correct, and offers no jump hint. This is a latent bug the
default fixes rather than a new behaviour.

---

## Reference

- Requirements: `docs/memory/requirements/roadmap.md` → **16.2 SSH Access → SSH-BIND-001..005**
- Architecture: `docs/memory/architecture/agent-lifecycle.md` → **Agent SSH port binding**; `security.md` → Container Security
- Flow: `docs/memory/feature-flows/ssh-access.md`
- Firewall script: `scripts/deploy/docker-firewall.sh` (#2380)
- Precedent: `docs/migrations/AGENT_RESTART_POLICY_2026-09.md` (same shape — a creation-time property that existing containers must be swept for once)
