# Install Provenance & First-Run Hardening Guide (#2380)

> Record **how** an instance was installed, and show a first-run HTTPS/VPN
> hardening guide on marketplace installs and nowhere else.
> Requirements: `docs/memory/requirements/infrastructure.md` §8.10 (PROV-001…011).
> Sits on §8.9 (#2280); the marker's producer is #2281.

## Why provenance, and not "is TLS configured?"

This is the whole design, so it goes first.

A marketplace droplet is the only install where Trinity can know, at boot, that
there is a public IPv4 with no domain, that the operator has done zero network
configuration, and that nobody has read `docs/DEPLOYMENT.md`.

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
provisioner writes .env:  TRINITY_INSTALL_SOURCE=do-marketplace
        │
        ▼  (compose forwards it — dev, prod AND hosted)
backend boot: database._record_install_source(cursor, conn)   [SQLite]
              database._record_install_source_engine()        [PostgreSQL]
        │  validate against INSTALL_SOURCE_VALUES
        │  write ONLY if no row exists
        ▼
system_settings.install_source = 'do-marketplace'
        │
        ▼  every later read — the env var is never consulted again
settings_service.get_install_source()  →  'do-marketplace' | … | 'unknown'
settings_service.is_marketplace_install()  →  bool
```

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
an explicit AC that no new endpoint appear:

| field | type | notes |
|---|---|---|
| `install_source` | string | `do-marketplace` \| `vultr-marketplace` \| `script` \| `unknown`. A string on a mostly-boolean surface; `platform_default_model` is the precedent |
| `marketplace_install` | bool | **the gate.** Resolved server-side so the browser holds no second copy of which channels count as a marketplace (the ent#386 rule) |
| `install_tls_posture` | string | `unconfigured` \| `http` \| `https-ip` \| `https-domain` |

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
them. A marketplace droplet can therefore come up on genuinely browser-trusted
HTTPS at a bare IP with zero user input.

So `https-ip` is **working**. What it is not is finished: a ~6-day renewal cycle,
an unmemorable address, and an instance on the open internet. The guide is an
upgrade prompt, and must not read as a breakage warning.

## The card

`components/onboarding/HardeningGuide.vue`, in the Dashboard onboarding stack
above `FrontDeskPanel` and `ActivationChecklist` — a security-posture prompt
outranks a getting-started nudge.

Renders only when: flags loaded **and** `marketplace_install` **and** not
dismissed **and** posture ≠ `https-domain`. The flags-loaded term prevents a
flash before the answer arrives (the `firstRun.js` rationale).

Two paths, presented as complementary rather than either/or (PROV-009): a real
domain — point an A record at the droplet, set it as the Public URL, and a normal
90-day certificate replaces the short-lived IP one — **and** private access,
putting the instance behind a VPN so the UI stops being served publicly.

Dismissal is localStorage (the ent#319 precedent — no new endpoint, no server
row). Retirement is server *state*: once the posture is `https-domain` the card
is gone with no client state involved.

State, not verified fact — and worth being precise about, because this design
refuses exactly that shape one level up. `public_chat_url` is operator-declared,
so an admin who types any https domain retires the card whether or not DNS
resolves or a certificate exists. That is accepted here and refused for
`install_source` because the two gate different things: provenance decides
whether this surface may exist at all, while the posture only decides whether a
nudge is still worth showing to someone who can already dismiss it outright.

The card is also explicit that **Trinity issues no certificate**. Nothing in the
tree reads `public_chat_url` and reconfigures a proxy or a listener; setting it
changes the name Trinity hands out, and whatever terminates TLS in front of the
backend is what turns that name into a longer-lived certificate.

It composes `BaseCard` / `BaseButton` / `BaseBadge`. Both sibling onboarding
cards hand-roll their shell and dismiss button and predate the primitives
ratchet — they are the behavioural model, not the markup model.

## What is deliberately not here

Nothing in the OSS tree writes `TRINITY_INSTALL_SOURCE`. #2281's Packer snapshot
does, and §8.9's outstanding AC4 cloud-init example
(`trinity-ops-public/provision/cloud-init.sh`) does. Until one of them lands,
provenance reads `unknown` on every install and the guide renders nowhere.

That is PROV-004's contract working as specified, not a gap — and it is what
makes this half safe to ship first.
