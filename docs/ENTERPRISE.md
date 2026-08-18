# Trinity Enterprise Modules — Installation & Verification

Trinity is open-core. The open-source platform in this repository is complete
and fully functional on its own. Customers with an enterprise agreement
additionally receive access to a **private companion repository**
(`Abilityai/trinity-enterprise`) that mounts into this repo as an **optional
git submodule** at `src/backend/enterprise/`.

This document covers the *generic installation mechanism only*: how the
optional submodule mounts, how the backend detects it, and how to verify which
edition an instance is running.

**Why there's no feature catalog here.** By a standing open-core rule
(trinity-enterprise#45, enforced by
`.github/workflows/enterprise-docs-guard.yml`), this public repo documents the
*seam* — never the *catalog* of which capabilities are paid, their private
`enterprise_*` schema, or the monetization rationale. Entitled customers and
core-team members find the per-module catalog and designs in the private
enterprise repository (`docs/memory/ENTERPRISE_DOCS.md` there). For a live,
edition-specific answer to "what's actually enabled on this instance," ask the
running backend: `GET /api/version` and `GET /api/settings/feature-flags` both
report the enterprise feature-ids *your* build has registered (empty in an
OSS-only build).

## How the seam works

- `src/backend/main.py` wraps the enterprise loader in a conditional import:
  if the submodule is present, `register_enterprise(app)` runs and each
  enterprise module registers itself with the entitlement registry
  (`services/entitlement_service.py`); if the submodule is absent, the
  `ImportError` is caught and the platform runs **OSS-only** — this is normal,
  not an error.
- Registration state drives two API surfaces (same source, never divergent):
  - `GET /api/settings/feature-flags` → `enterprise_features: [...]` (empty
    list in OSS-only builds)
  - `GET /api/version` → `edition: "oss" | "enterprise"` plus the same
    `enterprise_features` list (#1443)
- A **bug** during enterprise registration never takes down the platform: the
  backend logs `Trinity Enterprise registration FAILED — continuing OSS-only`
  and boots with whatever registered before the failure (possibly nothing).

## OSS installs: nothing to do

The submodule entry in `.gitmodules` sets `update = none`, so:

```bash
git clone --recurse-submodules https://github.com/abilityai/trinity.git   # OK
# or
git clone https://github.com/abilityai/trinity.git
cd trinity
git submodule update --init --recursive                                   # OK — prints "Skipping submodule"
```

both complete **without credentials**. The `src/backend/enterprise/` directory
stays empty and the backend boots OSS-only, logging:

```
Trinity Enterprise submodule not present — OSS-only build (this is normal; enterprise modules are an optional private submodule)
```

## Mounting the enterprise submodule (entitled customers)

> `update = none` means git skips this submodule on *every* plain
> `git submodule update`, including `--init` — and **any** init path (plain
> `--init`, one-shot `--init --checkout`, `clone --recurse-submodules`) copies
> `none` into your local config, so later updates keep skipping. Set the local
> override **first**; it is durable and wins over `.gitmodules`.

### 1. Set the durable local override

```bash
git config submodule.src/backend/enterprise.update checkout
```

### 2. Choose an auth transport

**Option A — SSH (default URL in `.gitmodules`):** ensure your SSH key has
read access to the private repository, then initialize:

```bash
git submodule update --init src/backend/enterprise
```

**Option B — HTTPS with a personal access token** (CI hosts, servers without
SSH keys): override the submodule URL locally, then initialize:

```bash
git config submodule.src/backend/enterprise.url \
  "https://x-access-token:<YOUR_GITHUB_PAT>@github.com/Abilityai/trinity-enterprise.git"
git submodule update --init src/backend/enterprise
```

The URL override lives only in your clone's `.git/config` — never commit a
token anywhere. Prefer a credential helper over embedding the token in the URL
where possible.

> **Option A fails one layer earlier than you expect (#2246).** SSH verifies the
> *server's* identity before it offers your key, so a host with no `github.com`
> entry in its `~/.ssh/known_hosts` dies at:
>
> ```
> Host key verification failed.
> fatal: Could not read from remote repository.
> ```
>
> That message is **not** about access — authentication was never attempted — but
> git helpfully appends "Please make sure you have the correct access rights",
> which sends most people after credentials they already have. It is also
> asymmetric: `.gitmodules` uses HTTPS for `.claude` and SSH only for
> `src/backend/enterprise`, so this is the one submodule that can fail this way.
> The dev VM shipped OSS-only for weeks on exactly this. Either trust the key:
>
> ```bash
> ssh-keyscan github.com >> ~/.ssh/known_hosts
> ```
>
> …or take **Option B**, which needs no `known_hosts` entry and clears the
> authorization layer in the same step. On an unattended host, prefer B.

### Unattended hosts: dev VM / CI (#2246)

A deploy host is the case where "it silently degraded" costs the most, because
nobody is watching the log. Two things make it durable:

**Pin the transport in the host's clone, once.** Option B's `git config` above
survives pulls, rebuilds of the containers, and every future
`git submodule update` — but **not** a rebuild of the VM itself. Re-applying it
belongs in whatever provisions the host, next to the checkout step; otherwise the
next rebuild silently reintroduces the OSS-only deploy.

**Or hand the token to the workflow instead of the host.** `deploy-dev.yml` reads
an optional `ENT_SUBMODULE_PAT` repo secret and, when present, rewrites the SSH URL
to HTTPS **for the duration of that one command**:

```bash
git -c "url.https://x-access-token:${PAT}@github.com/.insteadOf=git@github.com:" \
    submodule update --init --recursive src/backend/enterprise
```

`git -c` exports `GIT_CONFIG_PARAMETERS`, which the submodule's own clone process
inherits (verified — with the rewrite in place the child clones the rewritten
URL), so the token never lands in the host's `.git/config`. With the secret
unset the command is byte-identical to the plain form, so this is additive: the
host-side override above remains a valid, independent fix.

Whichever route you take, the deploy now **fails** rather than warning when the
submodule is not mounted — an unentitled dev instance means the recorded gitlink
is never exercised before prod. Set the repo variable `DEPLOY_ALLOW_OSS_ONLY=true`
if a particular host legitimately has no enterprise access.

### 3. Get the code into the container

The enterprise tree reaches the backend via a **bind-mount**, never the image —
the backend Dockerfile copies an explicit allowlist that excludes
`enterprise/`, keeping the published image bit-identical to the OSS build.
What to do depends on your stack:

**Dev stack (`docker-compose.yml`)** — it already bind-mounts `./src/backend`
into `/app`, so the freshly-initialized submodule is inside the mount. Just
restart the backend:

```bash
docker compose restart backend
```

**Prod stack (`docker-compose.prod.yml`)** — there is no source mount; add the
enterprise overlay (which bind-mounts `./src/backend/enterprise` read-only
into `/app/enterprise`) to **every** compose invocation:

```bash
docker compose -f docker-compose.prod.yml -f docker-compose.prod.enterprise.yml up -d backend
```

See the comments in `docker-compose.prod.enterprise.yml` for the full
build/update recipe.

### 4. Verify

```bash
# Boot log — one line per outcome:
docker logs trinity-backend 2>&1 | grep "Trinity Enterprise"
#   "Trinity Enterprise modules registered"            → enterprise active
#   "Trinity Enterprise submodule not present …"       → OSS-only
#   "Trinity Enterprise registration FAILED …"         → degraded to OSS-only (see traceback above it)

# API (any authenticated user):
curl -s -H "Authorization: Bearer <token>" http://localhost:8000/api/version
#   → { "edition": "enterprise", "enterprise_features": ["..."], ... }
```

### Keeping it updated

With the step-1 override in place, routine updates just work:

```bash
git pull
git submodule update --init --recursive
```

**Existing clones (mounted before #1443):** the `update = none` default
applies to your clone too — plain `git submodule update` starts *skipping*
the enterprise submodule (exit 0, working tree left stale). Run the step-1
`git config` line once to restore normal syncing.

#### Before advancing the recorded submodule pointer (#2068)

Advancing the pointer is the one change public CI cannot check: it never
mounts the submodule, so nothing in this repo's build sees what the new
pointer contains. Run the migration-graph guard locally first — you already
have the submodule checked out, which is why this is a procedure rather than
a CI job:

```bash
python3 scripts/ci/check_alembic_heads.py \
  src/backend/enterprise/backend/migrations/versions
#   → "… 1 head (…) — PASS."   ship it
#   → "FAIL — … resolves to 2 heads"   do NOT advance the pointer
```

Two migration revisions that branch off the same parent leave the version
line with two heads. The upgrade run is `head`, singular: it resolves its
target *before* applying anything, so it applies **nothing** and raises —
and that error is caught and downgraded to a warning, so the platform boots
happily with every entitled feature switched off. Git shows no conflict for
this, because each revision file is valid on its own.

After the deploy, confirm the outcome rather than assuming it — the boot log
in [4. Verify](#4-verify) must show `Trinity Enterprise modules registered`
and no `registration FAILED` line, and `GET /api/version` must report
`"edition": "enterprise"`. A green `/health` and green containers prove
nothing here: a failed registration leaves both untouched.

## Forcing OSS-only mode

`TRINITY_OSS_ONLY=1` (backend environment) empties the entitlement registry
even when the submodule is mounted — every enterprise feature reports
not-entitled, `enterprise_features` returns `[]`, and `edition` reports
`"oss"`. Useful for compliance lockdowns and for CI that exercises the
OSS-only path (`.github/workflows/build-without-submodule.yml`).

## For maintainers: the `.claude` submodule

The dev-tooling submodule at `.claude/` (skills, methodology guides) is also
private and also `update = none`, so OSS clones skip it too. Core-team setup
is documented in `CLAUDE.md` → "Development Skills". External contributors
don't need it — the public
[abilities](https://github.com/abilityai/abilities) marketplace ships the
`dev-methodology` plugin with the equivalent workflow skills.
