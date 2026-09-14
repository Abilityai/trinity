# Install Provenance & First-Run Hardening Guide (#2380)

> Record **how** an instance was installed, and show a first-run hardening guide
> (a real domain, then a Cloudflare Tunnel) on installs known to have landed on a
> public cloud VM at a bare IP — the marketplace images and the DigitalOcean
> install script — and nowhere else.
> Requirements: `docs/memory/requirements/infrastructure.md` §8.10 (PROV-001…015).
> Sits on §8.9 (#2280). The marker's producer is `start.sh --provision`
> (PROV-011), called by #2281's Packer image and the DigitalOcean installer; the
> host side of that path is [hosted-install.md](hosted-install.md) → *Provision Layer*.

## Why provenance, and not "is TLS configured?"

This is the whole design, so it goes first.

A provisioned droplet — a marketplace image, or one created by the DigitalOcean
install script — is the only install where Trinity can know, at boot, that there
is a public IPv4 with no domain and that the operator has done zero network
configuration.

None of that is true of the managed fleet — and critically, **the fleet is
indistinguishable from an unhardened droplet by every observable signal**.
Measured across all 16 instances: every one has `FRONTEND_PORT=80`, no `DOMAIN`,
no `HTTPS_ENABLED`, and an `SSH_HOST` on `100.x` (Tailscale CGNAT). Those
instances are already correct — HTTP over a WireGuard tunnel is encrypted
transport, and HOST-010 records it as a finished posture, not a compromise.

So a "no TLS configured → show the hardening guide" rule fires permanently on
every instance the platform has, paying clients included. There is no
environmental predicate that separates the two cases. The **install channel**
does, and it is knowable only at provisioning time — which is why it must be
recorded then and carried forward, rather than inferred later.

## The marker

```
start.sh --provision → provision_site() writes .env      [scripts/deploy/start.sh:271, :280-284]
    TRINITY_INSTALL_SOURCE=do-marketplace   ← Packer first boot passes --provenance do-marketplace
    TRINITY_INSTALL_SOURCE=do-script        ← DO installer: the --cloud digitalocean default (:181-186)
        │
        ▼  (compose forwards it — dev, prod AND hosted)
backend boot: database._record_install_source(cursor, conn)   [SQLite, database.py:697]
              database._record_install_source_engine()        [PostgreSQL, database.py:786]
        │  validate against INSTALL_SOURCE_VALUES               [config.py:561]
        │  write ONLY if no row exists
        ▼
system_settings.install_source = 'do-marketplace'
        │
        ▼  every later read — the env var is never consulted again
settings_service.get_install_source()          →  'do-marketplace' | … | 'unknown'   [settings_service.py:484]
settings_service.is_marketplace_install()      →  bool                               [settings_service.py:515]
settings_service.is_hardening_guide_eligible() →  bool  ← the guide's gate           [settings_service.py:526]
```

**Who writes it.** `start.sh --provision` is the only writer in the tree: the
`--provenance` argument if given, else the cloud's default. The script does not
validate the value — an unrecognised one is dropped at boot (below). An install
that never passes through `--provision` (a plain or `--hosted` `start.sh`)
writes nothing and reads `unknown`.

**`do-script` is not `script`.** `--cloud digitalocean` refuses to run unless
DigitalOcean's metadata service answers (`start.sh:392-404`), so the value
records a fact the machine established rather than a claim someone typed.

An **env var, not a marker file.** `/etc/trinity/install-source` was the issue's
other suggestion. `config.py` reads zero files today (it is pure `os.getenv`),
and a file needs a read-only bind mount added to all three compose files — the
packaging class this codebase has shipped repeatedly (#1039, #1056, #1707),
where the value never reaches the container and the feature is silently inert
forever. The provisioning script already writes `.env`.

**Recorded at boot, not read live**, because provenance must survive an operator
editing `.env`, a compose change, or a move to a different host.

**Not a migration**, for #2381's reason: a migration runs once and records
itself, so an instance provisioned before it — or one whose marker was corrected
afterwards — could never be answered. A boot recorder converges on next restart.

## Three properties that are security, not hygiene

**Write-once.** `_record_install_source` never overwrites an existing row; a
differing marker is logged, not applied. Provenance is a fact about an
installation *event*. If a later `.env` edit could rewrite it, it would answer
"what does this box currently claim" rather than "how was this box installed",
and the marketplace gate would be self-assertable by anyone who can edit a file.

**No env fallback on read.** `get_install_source()` reads the row only. Without
this, write-once is decorative: an unrecorded install could be talked into a
marketplace verdict just by exporting the variable.

**Blocked on PUT *and* DELETE.** `install_source` 422s on both arms of the
generic `/api/settings/{key}` catch-all, with no dedicated write route to point
at. The DELETE block is not symmetry — because the recorder is write-once, a
delete is precisely the move that *unlocks* a rewrite (delete the row, edit
`.env`, restart). Blocking the write while leaving the delete open would be no
gate at all. Without both, an admin — or, on a default admin-owned install,
anything holding an admin's credential — could summon the guide on a managed
instance or suppress it on a droplet that needs it.

## Why an unrecognised marker records nothing

Not the bogus value, and **not `unknown` either**. Recording `unknown` would
combine with write-once to freeze a typo permanently. An absent row already
reads as `unknown`, so leaving it absent costs nothing and lets a corrected
marker land on the next boot.

Absent · empty · unrecognised · unreadable → all `unknown`. Never toward a
marketplace value; the failure direction is always "hide the guide", because
showing a hardening prompt on a correctly-configured client instance is the
outcome this feature is shaped to avoid.

The marker *is* normalised before matching (`.strip().lower()`), so
`  DO-Marketplace  ` is accepted. That is not a widening: `.env` is a
script-written trusted channel, and normalisation can only land on a value
already in the closed set.

## The surface

`GET /api/settings/feature-flags` — the established home for UI-gating flags, and
an explicit AC that no new endpoint appear (`routers/settings.py:266-289`):

| field | type | notes |
|---|---|---|
| `install_source` | string | `do-marketplace` \| `vultr-marketplace` \| `do-script` \| `script` \| `unknown`. A string on a mostly-boolean surface; `platform_default_model` is the precedent |
| `marketplace_install` | bool | Did this come from a vendor listing — `MARKETPLACE_INSTALL_SOURCES` (`config.py:577`). Still served and still means only that; it is **not** the guide's gate |
| `hardening_guide_eligible` | bool | **The gate.** `HARDENING_GUIDE_INSTALL_SOURCES` (`config.py:592`) = the marketplace set ∪ `do-script`. A separate set, not a widening of `marketplace_install`, which would make a doc-driven install claim a marketplace provenance it does not have. Neither set includes `script` or `unknown` |
| `install_tls_posture` | string | `unconfigured` \| `http` \| `https-ip` \| `https-domain` |

Both booleans are resolved server-side, so the browser holds no second copy of
which provenances qualify (the ent#386 rule). The store reads only
`hardening_guide_eligible` for the card and fails closed to `false` on a failed
fetch (`stores/sessions.js:143-145`, `:170-172`).

`GET /api/version` also carries `install_source`, for operator support. It is
threaded into `_build_version_payload` as a **parameter** — that function is
exec-sliced by its own tests and must stay stdlib-only, which is exactly why
`edition` is threaded the same way (#1443).

## Honest state — what the posture field actually knows

`install_tls_posture` is derived by the pure `classify_advertised_url` from the
URL the instance is configured to hand out (`public_chat_url`, else the baked
`FRONTEND_URL`).

**Nothing probes a socket or reads a certificate.** TLS terminates outside the
backend (HOST-010) — there is no HTTPS listener in any compose file — so no
in-process check can observe the real posture. The field is named for what it
can honestly claim: what this instance *advertises*. The UI copy inherits that
constraint and never asserts "secure".

It is derived rather than returning the URL because `public_chat_url` sits behind
an admin-only settings read, while this surface serves every authenticated
principal.

## An IP certificate is a posture to upgrade, not a fault

Let's Encrypt IP-address certificates went GA 2026-01-15 — ACME `shortlived`
profile, ~6-day validity, `http-01`/`tls-alpn-01` only (no DNS-01) — and
DigitalOcean's own 1-Click authoring rules direct vendors to ship Caddy with
them. A provisioned droplet can therefore come up on genuinely browser-trusted
HTTPS at a bare IP with zero user input.

So `https-ip` is **working**. What it is not is finished: a ~6-day renewal cycle,
an unmemorable address, and an instance on the open internet. The guide is an
upgrade prompt, and must not read as a breakage warning. Renewal happens only
while the machine runs, so the copy also says that a server left off for longer
than the certificate's life comes back to a browser warning until renewal catches
up — stated as a property of the profile, never of this instance's certificate
(`hardeningGuide.js:144-150`).

## The card

`components/onboarding/HardeningGuide.vue`, first in the Dashboard onboarding
stack (`views/Dashboard.vue:258-279`), which renders at most one card with DOM
order as priority (`.onboarding-stack`, `Dashboard.vue:1145`) — a
security-posture prompt outranks a getting-started nudge. A card hidden by that
rule still mounts, which is why the Finish-setup card marks its warm telemetry
ask seen only through an `IntersectionObserver` — see
[telemetry-sharing.md](telemetry-sharing.md) → *The ask*.

Renders only when (`hardeningGuide.js:78-93`, wired at `HardeningGuide.vue:237-254`):
flags loaded **and** a verified admin **and** `hardening_guide_eligible` **and**
the current stage not dismissed. The flags-loaded term prevents a flash before
the answer arrives (the `firstRun.js` rationale). Admin is a real gate: the one
action lands in Settings → General, which is `adminOnly`, and the copy discloses
the instance's network posture.

**Two stages; the posture picks the stage, not visibility.** `hardeningStage()`
(`hardeningGuide.js:30-32`) maps `https-domain` → `tunnel` and every other
posture → `address`:

| posture | stage | the card shows |
|---|---|---|
| `unconfigured` / `http` / `https-ip` | `address` | the posture badge and headline, the **Add a domain** button (`HardeningGuide.vue:69-78`), and both steps behind "Why this matters" |
| `https-domain` | `tunnel` | "Your domain is set" and the tunnel step only (`POSTURE_COPY['https-domain']`, `hardeningGuide.js:151-158`) |

A configured domain therefore **advances** the card to the tunnel step rather
than retiring it. Retiring on step one meant step two was mentioned once and
never again, on the one surface that raises it (`hardeningGuide.js:19-23`).

Two steps that stack, never either/or (PROV-009): a real domain, then a
Cloudflare Tunnel so the server stops listening on the public internet — the
tunnel needs the name. A tunnel rather than a VPN because a VPN breaks every
inbound integration (Telegram, WhatsApp, VoIP, public agent links, webhook
triggers). The tunnel step is guidance with no button: `TUNNEL_TOKEN` lives in
`.env` and `cloudflared` starts under a compose profile, neither reachable from a
container.

**Dismissal is localStorage, per stage** (the ent#319 precedent — no new
endpoint, no server row). `dismissKeyForStage()` (`hardeningGuide.js:42-44`)
keeps `address` on the original `trinity_hardening_guide_dismissed` key, so a
dismissal made before the split still holds, and gives `tunnel` its own
`trinity_hardening_guide_tunnel_dismissed`. Waving away step one cannot
silently spend step two, and a dismissal is the **only** thing that ends the
guide — no server state retires it (`HardeningGuide.vue:230-233`, `:258-264`).

State, not verified fact — and worth being precise about, because this design
refuses exactly that shape one level up. `public_chat_url` is operator-declared,
so an admin who types any https domain advances the card to the tunnel stage
whether or not DNS resolves or a certificate exists. That is accepted here and
refused for `install_source` because the two gate different things: provenance
decides whether this surface may exist at all, while the posture only decides
which nudge to show someone who can already dismiss it outright.

**The domain field is the whole step.** Saving the Public URL used to reconfigure
nothing: the instance started advertising a name no web server answered to while
the card advanced as though step one were done. On a host provisioned by
`start.sh --provision`, the Caddyfile carries on-demand TLS behind an `ask` gate
(`scripts/deploy/start.sh:325-362`), so Caddy obtains a certificate for the saved
name on its first request:

```
browser → https://trinity.example.com
   │  Caddy: no site for that name → catch-all `https://` site, tls { on_demand }
   ▼
GET http://127.0.0.1:8000/api/public/tls-allowed?domain=trinity.example.com
   │  routers/public.py:50-102   (unauthenticated — Caddy holds no credential)
   │  requested = domain.strip().strip('.').lower()
   │  allowed   = urlparse(settings_service.get_public_chat_url()).hostname
   │              (the saved row, else PUBLIC_CHAT_URL)
   ▼  200 iff requested == allowed; 404 otherwise
Caddy obtains an ordinary Let's Encrypt certificate and serves the name
```

- **Exact host, parsed** — never a substring match, so `evil-example.com` cannot
  ride on `example.com`.
- **Fails closed** — 404 on no domain, no configured URL, a failed settings read,
  or a mismatch. `on_demand` without a working gate makes the instance request
  certificates for any name anyone points at it, until the ACME account is
  rate-limited and the operator's own renewals fail.
- **Moves no privilege** — Trinity is containerised and cannot rewrite the
  Caddyfile or reload Caddy. Caddy asking Trinity keeps the operator out of a
  root shell. The route discloses only whether a guessed hostname matches, which
  DNS answers anyway.

The copy still claims only what it can know. Trinity issues no certificate
itself; the card says the web server in front "is configured to obtain one for
the name you save" — a statement about provisioning, never a verdict on a
handshake — and tells the operator to let DNS settle first
(`HardeningGuide.vue:127-137`).

It composes `BaseCard` / `BaseButton` / `BaseBadge`. Both sibling onboarding
cards hand-roll their shell and dismiss button and predate the primitives
ratchet — they are the behavioural model, not the markup model.

## What is deliberately not here

A marker for installs that never pass through `start.sh --provision`. A plain or
`--hosted` `start.sh` writes no `TRINITY_INSTALL_SOURCE`, so provenance reads
`unknown` there and the guide renders nowhere. That is PROV-004's contract
working as specified, not a gap — it is what keeps the card off every instance
whose posture someone already chose.

A Vultr provisioner. `vultr-marketplace` is in both sets, but `--provision`
accepts only `--cloud digitalocean`, so the domain step's on-demand-TLS promise
holds only on a host whose Caddyfile `start.sh --provision` wrote.
