# Enterprise modules — local development

Clone-to-running guide for working on the closed-source enterprise
modules (`Abilityai/trinity-enterprise`) alongside the public Trinity
backend. Target: **15 minutes from `git clone` to a running
`/api/enterprise/sso/providers` endpoint.**

> **Access required**: read access to the private repo
> `Abilityai/trinity-enterprise`. Ask the project owner for an
> invite if you don't have it yet.

## TL;DR

```bash
git clone --recurse-submodules git@github.com:abilityai/trinity.git
cd trinity
./scripts/deploy/start.sh
curl http://localhost:8000/api/enterprise/sso/providers
# → []
```

That's it. The conditional loader in `src/backend/main.py` picks up
the submodule automatically.

## Step-by-step

### 1. Clone with submodules (1 min)

```bash
git clone --recurse-submodules git@github.com:abilityai/trinity.git
cd trinity
```

If you already cloned without `--recurse-submodules`, pull the
submodules in:

```bash
git submodule update --init --recursive
```

`.gitmodules` mounts two private submodules:

| Submodule | Path | Consumed by |
|---|---|---|
| `.claude` | `.claude/` | Claude Code skills (`/sprint`, `/cso`, …) |
| `src/backend/enterprise` | `src/backend/enterprise/` | Python (`main.py`) — backend logic |

**Enterprise frontend lives in the public OSS bundle** at
`src/frontend/src/views/enterprise/` and is gated server-side via the
`enterprise_features` field in `/api/settings/feature-flags`. No
frontend submodule. See `docs/planning/ENTERPRISE_ARCHITECTURE.md`
for the rationale.

The backend submodule uses SSH (`git@github.com:...`). If you only
have HTTPS auth configured, add an SSH override:

```bash
git config --global url."git@github.com:".insteadOf "https://github.com/"
git submodule sync --recursive
git submodule update --init --recursive
```

### 2. (Recommended) Auto-sync on branch switch (10 sec)

Without this, switching branches leaves the submodules stale and
imports fail silently:

```bash
git config submodule.recurse true
```

### 3. Start the stack (5 min, mostly Docker build)

```bash
./scripts/deploy/start.sh
```

This boots Redis, Vector, the backend (which conditionally loads the
enterprise submodule), MCP server, and the frontend.

### 4. Verify enterprise is wired (10 sec)

```bash
# Endpoint exists and responds
curl http://localhost:8000/api/enterprise/sso/providers
# → []

# Feature flags expose the entitled enterprise list
TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  -d 'username=admin&password='"$ADMIN_PASSWORD" | jq -r .access_token)
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/settings/feature-flags | jq .enterprise_features
# → ["sso", "scim", "siem"]
```

If you see those responses, the submodule is mounted, the conditional
import worked, and the entitlement seam reports all features enabled
(the Phase 0 stub).

## Working on the enterprise repo

```bash
cd src/backend/enterprise
git checkout main                # submodules default to detached HEAD
# … make changes (under backend/) …
git commit -m "feat(sso): ..."
git push origin main
```

Then, back in the public repo root, **bump the submodule pointer**:

```bash
cd <trinity-root>
git add src/backend/enterprise
git commit -m "chore: bump enterprise submodule"
git push
```

For **frontend** changes (Vue views, nav entries), edit the public
repo directly under `src/frontend/src/views/enterprise/`,
`src/frontend/src/stores/enterprise.js`, etc. Hot-reload picks them
up immediately.

## Forcing OSS-only mode (testing the deny path)

```bash
echo "TRINITY_OSS_ONLY=1" >> .env
docker compose up -d --force-recreate backend
# Now enterprise features are gated (403) even though the submodule
# is mounted. Useful for testing the OSS UX without unmounting.

# Revert
sed -i '/^TRINITY_OSS_ONLY=/d' .env
docker compose up -d --force-recreate backend
```

## Running Trinity OSS-only (no submodule access)

Cloning without enterprise access is the **default** OSS experience:

```bash
git clone https://github.com/abilityai/trinity.git
cd trinity
git submodule update --init .claude    # explicit — skip enterprise
./scripts/deploy/start.sh
```

The conditional `try: from enterprise import register_enterprise` in
`main.py` raises `ImportError`, the `except` branch logs an
informational message, and Trinity runs as OSS. Confirm:

```bash
docker logs trinity-backend 2>&1 | grep -i enterprise
# → INFO  Trinity Enterprise submodule not present — OSS-only build
```

## Dev VM deploy (production-style, with enterprise)

The `Deploy to Dev` workflow (`.github/workflows/deploy-dev.yml`) runs
on every push to `dev` and SSHes into the shared dev VM to rebuild.
On each deploy it attempts to init the `src/backend/enterprise/`
submodule and layers the enterprise compose overlay on top of
`docker-compose.prod.yml`:

```bash
git submodule update --init --recursive src/backend/enterprise   # soft-fail
docker compose -f docker-compose.prod.yml \
               -f docker-compose.prod.enterprise.yml \
               build ...
```

If the submodule init succeeds, the dev VM boots with
`enterprise_features: ["audit", ...]` and `/enterprise/audit` is live.
If it fails (no GitHub access to the private repo, network hiccup, …)
the workflow logs a `::warning::` annotation, **does not fail the
deploy**, and the backend's conditional `from enterprise.backend
import register_enterprise` falls back to OSS-only — enterprise UI
surfaces stay hidden until the next deploy succeeds.

The overlay (`docker-compose.prod.enterprise.yml`) is a single read-only
bind-mount of `./src/backend/enterprise` into `/app/enterprise`. The
base prod image stays bit-identical to OSS — no Dockerfile change.

### Enabling enterprise on the dev VM

The dev VM needs **read access to `Abilityai/trinity-enterprise`** for
the submodule init to succeed. Any of these works — pick whatever fits
how the VM already authenticates to GitHub:

- **Org membership**: if the VM clones via an identity that's a member
  of the Abilityai org, grant that identity read on `trinity-enterprise`.
- **Deploy key**: add a read-only deploy key on `trinity-enterprise`
  whose private half lives on the VM.
- **PAT in a credential helper**: a fine-grained PAT with read on
  `trinity-enterprise`, registered via `git config --global
  credential.helper`.

After the access is in place, push to `dev` (or trigger the workflow
manually). The workflow log should show
`ENTERPRISE: initialized at <sha>` instead of the warning.

If the dev VM never gains private-repo access, the workflow keeps
deploying OSS-only indefinitely — no action required.

## CI: testing without the submodule

`.github/workflows/build-without-submodule.yml` boots the backend with
the submodule absent and asserts the conditional import doesn't break
the boot. Run it manually with:

```bash
gh workflow run build-without-submodule.yml
```

The job exists specifically so a forgotten access revocation
(submodule URL no longer reachable) is caught in CI rather than at a
customer's first OSS-only clone.

## Troubleshooting

**Submodule clone fails with "Permission denied (publickey)"** — your
GitHub SSH key isn't loaded. Run `ssh-add ~/.ssh/id_ed25519` or
configure the URL rewrite shown in Step 1.

**`curl /api/enterprise/sso/providers` returns 404** — the submodule
didn't init. Run `git submodule status` — the line for
`src/backend/enterprise` should show a real SHA, not `-XXXX`. If it
shows `-XXXX`, run `git submodule update --init --recursive`.

**Enterprise endpoint returns 403 "not licensed"** — `TRINITY_OSS_ONLY=1`
is set in your `.env`. Either remove it or change to `0`, then
`docker compose up -d --force-recreate backend`.

## See also

- `docs/planning/ENTERPRISE_ARCHITECTURE.md` — decision record
- `docs/planning/OSS_ENTERPRISE_SPLIT_RESEARCH.md` — full research
- `Abilityai/trinity-enterprise/README.md` — private repo overview
- Issue #847 — the spike that produced this scaffolding
