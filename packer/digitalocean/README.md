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
| Build | Trinity checkout at `/opt/trinity`, then `start.sh --provision --cloud digitalocean --machine-only` (Docker, pinned Caddy, ufw, the firewall unit); all five images pulled at a pinned tag; agent base retagged `trinity-agent-base:latest` |
| First boot | admin source resolved (a user-data password, or none — the first visitor claims it at `/setup`), then `start.sh --provision --cloud digitalocean --site-only --provenance do-marketplace --hosted --unattended` (`.env` including the provenance marker, Docker/ufw gap closed, a certificate for the droplet's own IP, and the install itself) |

Both phases are the **same installer** a doc-driven install runs (#2380) — the
Packer scripts contribute only what is specific to baking a snapshot. There is no
second copy of the provisioning logic to keep in step, which is how the port list
above came to be wrong in the first place.

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
3. **Have another team member QA a droplet built from that snapshot.** Nothing is
   submitted on the strength of a green build — see "Submission gate" below.
4. Submit the snapshot for review, either from the Vendor Portal (the Trinity
   listing → submit) or by letting the build do it:

   ```bash
   TRINITY_DO_APP_ID=<app_id> \
   DIGITALOCEAN_API_TOKEN=dop_v1_... \
   TRINITY_SUBMIT_REASON="Trinity v0.9.5" \
     packer build -var "image_tag=v0.9.5" -var "do_token=$DIGITALOCEAN_TOKEN" \
       trinity.pkr.hcl
   ```

   `app_id` comes from the listing URL in the Vendor Portal. The `manifest` +
   `shell-local` post-processors read the snapshot id from the build's own
   record and `PATCH` it to
   `https://api.digitalocean.com/api/v1/vendor-portal/apps/<app_id>`. Without
   `TRINITY_DO_APP_ID` the build simply does not submit.

   The payload carries `imageId` (**required**), `reasonForUpdate`, `osVersion`
   and `softwareIncluded[]`. A `null` field leaves the existing value alone; a
   blank string clears it. `.../apps/<app_id>/versions/<version>` is deprecated
   — do not use it.

5. Update the listing's version string and any changed sizing guidance.
   Catalog copy lives in [`listing.md`](listing.md), not only in the portal.

### Submission gate

**Nothing is submitted for review until another member of the team has QA'd a
droplet created from that snapshot.** Not the author, and not "the build was
green": `img_check.sh` validates DigitalOcean's *image* requirements — no baked
secrets, firewall on, no pending security updates — and knows nothing about
whether Trinity comes up, serves HTTPS, or can create an agent.

### The state that blocks a resubmission

**An app in `pending` or `in review` cannot be updated — the API answers 400.**
Once a snapshot is submitted the listing is locked until DigitalOcean finishes
reviewing it, so a release-day rebuild fails while an earlier submission is
still queued. `mp-submit.sh` names this case in its error output, because the
raw 400 reads like a bad token.

## Design notes

**Admin account — claimed in the browser (ent#580).** DigitalOcean 1-Clicks have
no vendor-defined input form at deploy time — verified against
`digitalocean/marketplace-partners`, where the only prompt is the optional
Managed Database checkbox. So first boot provisions **no** admin: `ADMIN_PASSWORD`
stays blank, `ADMIN_PASSWORD_SOURCE=browser` records that the blank is
deliberate, and the first person to open `https://<ip>` gets `/setup` — email,
password, product-updates consent — and creates the admin there. Nothing is
generated, so nothing is printed, and the customer never needs a terminal.

It used to be the other way round: a password generated at first boot and
printed in the MOTD, which forced every customer into the droplet Console or an
SSH client just to learn how to log in — and the Console cannot even accept a
login on an SSH-key droplet, whose root account DigitalOcean leaves locked. The
MOTD still exists, for whoever does SSH in: it prints the URL, the TLS state and
whether an admin exists yet — never a password.

The cost is a window between creation and the first visit in which anyone who
finds the IP can claim the droplet. That was accepted on 2026-09-10 (the
instance is empty then, and a squatted droplet can be destroyed); the practical
advice lives in `docs/DEPLOYMENT.md` → Security Recommendations.

An operator who prefers to choose the password up front can supply one through
*Additional Options → Startup scripts* on the Create page, as `#cloud-config` —
the admin is then provisioned at boot, the wizard never opens, and the MOTD says
"the password you supplied":

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
already started Trinity with no admin. `write_files` runs in the earlier
`cloud_config` stage and lands in time.

**TLS with no domain.** Let's Encrypt has issued certificates for bare IP
addresses since 2026-01-15 via the `shortlived` ACME profile (~6-day validity,
`http-01`/`tls-alpn-01` only). DigitalOcean's own 1-Click build standard mandates
Caddy with `issuer acme` + `profile shortlived` for any app with an HTTP
interface. A domain is a post-login upgrade, not a prerequisite.

**Docker publishes past ufw.** `docker-compose.hosted.yml` publishes 8000, 8080,
8686, the OTel collector ports **and the frontend's `FRONTEND_PORT` (8081 here)**
on `0.0.0.0`. Docker's iptables rules are consulted before ufw's chain, so
`ufw deny 8000` on such a droplet is silently inert. `scripts/deploy/docker-firewall.sh`
closes it in `DOCKER-USER`, the one chain Docker leaves to the operator and
evaluates first. Container ports stay reachable from the host (Caddy proxies
`127.0.0.1:8081`) and between containers, which is what the platform uses.

The rule is **inverted rather than enumerated** (#2380). It used to carry a
hand-typed port list kept in step with compose by a unit test, and that list had
already shipped wrong: 8081 was missing, and 8081 is the one that decides whether
the TLS story holds at all — it is the SPA, moved off `:80` so Caddy can own
80/443, so the login page answered plain HTTP on `http://<ip>:8081`, past the
certificate and past the `http→https` redirect. Now everything entering a
container from off-box is dropped, whatever the port, with two RETURNs ahead of
it: replies to connections a container opened (without which agents lose outbound
internet), and traffic from Docker's own bridges. Link-local (169.254.0.0/16) is
dropped outbound between the two, ahead of the bridge RETURNs — that range serves
the droplet's own user-data verbatim for the life of the machine, and an agent
container has no business reading it. Naming what is *inside* rather
than which interface is outside also covers DigitalOcean's private `eth1` for
free. `tests/unit/test_2380_provision_single_source.py` pins the ordering.

**Adding a domain is a Settings field.** The provisioned Caddyfile carries
on-demand TLS with an `ask` gate at `/api/public/tls-allowed`, so Caddy obtains a
certificate for whatever hostname an admin saves as the Public URL, on first
request, and refuses every other name. Trinity is containerised and cannot
rewrite this file or reload Caddy — inverting the direction (Caddy asks, Trinity
answers) is what removes the root shell from a non-engineer's install path
without moving any privilege into the container. The gate is not optional:
`on_demand` without it makes the droplet request certificates for any name
anyone points at its address, until the ACME account is rate-limited and the
operator's own renewals fail.

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
