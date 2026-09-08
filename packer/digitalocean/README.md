# Trinity — DigitalOcean Marketplace 1-Click snapshot

Packer build for the Trinity 1-Click Droplet listing (#2281, epic #2332).

## What this produces

An Ubuntu 24.04 snapshot with Docker, Caddy and a pinned Trinity release already
pulled, so a droplet created from it is serving Trinity over browser-trusted
HTTPS about a minute after creation, with no input from the user.

The snapshot is built **from prebuilt GHCR images** (#2280), not from source. A
1-Click that spends 5–10 minutes compiling the agent base image on first boot
fails the one-click bar — which is why #2280 gates this work.

| Stage | What happens |
|---|---|
| Build | Docker + Caddy + ufw installed; Trinity checkout at `/opt/trinity`; all five images pulled at a pinned tag; agent base retagged `trinity-agent-base:latest` |
| First boot | admin password resolved; `.env` written (including the provenance marker); Docker/ufw gap closed; Caddy issued a certificate for the droplet's own IP; `start.sh --hosted --unattended` |

## Prerequisites

- The release's images must exist on GHCR **and be publicly pullable**. New GHCR
  packages default to private; the publish workflow's own "Verify anonymous pull"
  step fails loudly when they are, with the fix in the error message.
- `packer` ≥ 1.9, and a DigitalOcean API token with write scope.

## Build

```bash
export DIGITALOCEAN_TOKEN=dop_v1_...
packer init trinity.pkr.hcl
packer build \
  -var "do_token=$DIGITALOCEAN_TOKEN" \
  -var "image_tag=v0.9.1" \
  trinity.pkr.hcl
```

`image_tag` is required and may not be `latest` — the template rejects it. A
snapshot that resolved `latest` at first boot would serve a different Trinity on
every droplet created from one reviewed image, and Marketplace review approves a
specific artifact.

The build fails if DigitalOcean's own `99-img-check.sh` finds anything, so a
snapshot is never created from a droplet that would be rejected at review.

### If the build fails immediately with "invalid key identifiers"

```
Error creating droplet: POST .../v2/droplets: 422 ... 59080294 are invalid key
identifiers for Droplet creation.
```

Pass a pre-existing key and it cannot happen at all:

```bash
doctl compute ssh-key import trinity-packer-build \
  --public-key-file ~/.ssh/id_ed25519.pub --format ID --no-header
packer build -var "image_tag=v0.9.5-rc2" \
  -var "ssh_key_id=<that id>" \
  -var "ssh_private_key_file=$HOME/.ssh/id_ed25519" \
  trinity.pkr.hcl
```

This is a DigitalOcean API consistency window, not a fault in the template:
left to itself Packer imports a temporary SSH key and creates the droplet in the
very next call, and the new key id is not reliably resolvable that fast. It cost
**4 of 5** creates during the first real build of this bundle, each failing in
~7 seconds; the same import/create pair spaced one command apart succeeds every
time. Supplying a key removes the window rather than retrying into it — nothing
is created during the build.

Each failed attempt **leaks the temporary key**, and Packer's own cleanup then
404s on it, so reap any strays or they accumulate on the account:

```bash
doctl compute ssh-key list --format ID,Name --no-header \
  | awk '$2 ~ /^packer-/ {print $1}' \
  | xargs -r -n1 doctl compute ssh-key delete --force
```

Check `doctl compute droplet list` too — a failure later in the build can leave
the build droplet running.

## Per-release update runbook

1. Cut the Trinity release; confirm the `v*` tag published all five images and
   that each package is public.
2. `packer build` with the new `image_tag` (above).
3. Vendor Portal → the Trinity listing → submit the new snapshot for review.
   Programmatic alternative once the listing exists:
   `PATCH https://api.digitalocean.com/api/v1/vendor-portal/apps/<app_id>`
   (`app_id` comes from the listing URL in Vendor Portal).
4. Update the listing's version string and any changed sizing guidance.

## Design notes

**Admin password.** DigitalOcean 1-Clicks have no vendor-defined input form at
deploy time — verified against `digitalocean/marketplace-partners`, where the
only prompt is the optional Managed Database checkbox. So the password is
generated at first boot and printed in the MOTD, which the user reads from the
control panel's browser Console (Droplet → Console); no SSH client is needed.

An operator who prefers to choose it can supply one through *Additional Options →
Startup scripts* on the Create page, as `#cloud-config`:

```yaml
#cloud-config
write_files:
  - path: /etc/trinity/admin-password
    permissions: '0600'
    content: "your-password-here"
```

It must be `write_files` and **not** a shell script. 1-Click per-instance code
runs from cloud-init's `scripts-per-instance` module, which runs *before*
`scripts-user`, so a user-data shell script would execute after first boot had
already generated a password and started Trinity. `write_files` runs in the
earlier `cloud_config` stage and lands in time.

**TLS with no domain.** Let's Encrypt has issued certificates for bare IP
addresses since 2026-01-15 via the `shortlived` ACME profile (~6-day validity,
`http-01`/`tls-alpn-01` only). DigitalOcean's own 1-Click build standard mandates
Caddy with `issuer acme` + `profile shortlived` for any app with an HTTP
interface. A domain is a post-login upgrade, not a prerequisite.

**Docker publishes past ufw.** `docker-compose.hosted.yml` publishes 8000, 8080,
8686, the OTel collector ports **and the frontend's `FRONTEND_PORT` (8081 here)**
on `0.0.0.0`. Docker's iptables rules are consulted before ufw's chain, so
`ufw deny 8000` on such a droplet is silently inert. First boot inserts a
`DOCKER-USER` DROP rule for those ports on `eth0` — the one chain Docker leaves
to the operator and evaluates first. They remain reachable from the host (Caddy
proxies `127.0.0.1:8081`) and between containers, which is what the platform
uses.

8081 is the one that decides whether the TLS story holds at all: it is the SPA,
moved off `:80` so Caddy can own 80/443, and compose's short port syntax binds it
to every interface. Left out of the DROP list, the login page answers plain HTTP
on `http://<ip>:8081` — past the certificate and past the `http→https` redirect.
`tests/unit/test_2281_firstboot_port_exposure.py` keeps the list and the compose
file from drifting apart.

**The agent base image is not a compose service.** The backend creates agent
containers from the literal local tag `trinity-agent-base:latest`, hardcoded in
`agent_service/lifecycle.py` and allowlisted by SEC-172, and compose cannot
retag. The build pulls the GHCR copy and tags it locally; `start.sh --hosted`
does the same at run time. A bare `docker compose -f docker-compose.hosted.yml up`
would start a platform that cannot create a single agent.

**One installer, two image sources.** First boot calls the same
`scripts/deploy/start.sh` every other install uses, with `--hosted --unattended`.
A marketplace-specific copy of the installer is exactly the shape that has gone
stale in this repo before (#1039, #1056, #1707, #1871).

## Support

GitHub Issues on `abilityai/trinity`, label `do-marketplace`.
DigitalOcean does not build or support Trinity.
