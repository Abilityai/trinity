# Remote Template Registry (TMPL-002, trinity-enterprise#14)

**Status**: Implemented (2026-08-04)
**Requirements**: [`requirements/core-agent.md` §4.2.2](../requirements/core-agent.md)

Where the GitHub half of the template catalog comes from at **runtime**. Curating
which starter agents an install offers becomes a vendor file edit instead of a
Trinity release.

Split out of [platform-settings.md](platform-settings.md) rather than folded into
its GitHub-Templates section: the fail-open matrix and the cache semantics alone
run past that section's length, and the story spans `template_service`,
`crud.py`, `safe_yaml` and a Settings panel.

---

## Overview

```
registry.yaml (vendor, HTTPS)
        |
        |  streaming fetch, byte-capped, follow_redirects=False
        v
template_registry_service
        |  AliasPolicy.REJECT parse -> allowlisted 4-field records
        |  own TTL cache (3600s +/- jitter, 7-day stale cap, generation-stamped)
        |  durable last-known-good in system_settings
        v
template_service.get_all_templates()   <- and get_github_template()
        |  registry entries arrive as `admin_override` dicts
        v
GET /api/templates  ->  Library page + MCP list_templates
```

**Purely additive.** `DEFAULT_GITHUB_TEMPLATE_REPOS` has been `[]` since #1931,
so this fills a branch that is empty today. On a curated install (a TMPL-001
admin list exists) the registry is not consulted at all — not even fetched.

---

## The resolution ladder

**admin DB override → remote registry → bundled `DEFAULT_GITHUB_TEMPLATE_REPOS`**

Resolved identically by **both** resolvers:

| Function | Feeds |
|----------|-------|
| `template_service.get_all_templates()` | `GET /api/templates` (Library, MCP `list_templates`) |
| `template_service.get_github_template()` | `GET /api/templates/{id}` **and agent creation** (`crud._resolve_github_repo_and_pat`) |

The second one is why this is two edits and not one. Its ladder ended in a
dynamic fallthrough calling `_build_template(repo, metadata)` with **no override
dict**, so landing the registry only in `get_all_templates()` would list a
template as *"Cornelius — your second brain"* and resolve it by id as
*"cornelius"* — and under the rate-limit scenario below, the detail view degrades
all the way to the repo basename. `learnings.md` 2026-07-10: *the create path is
never one call site.* `tests/unit/test_ent14_catalog_failopen.py` pins list,
detail and create to one ladder.

An admin who has curated a list never has it silently replaced by a vendor
registry — TMPL-001's contract survives byte-for-byte. `[]` (explicit empty)
suppresses the registry just as `[{...}]` does; only `None` (no row) falls
through.

---

## Fail-open is structural, not an `except` branch

`get_all_templates()` returns `local + github`. `local` is read from disk with no
network and no registry involvement. `github` is empty by default. The registry
can only ever **add** to `github`, so every failure mode reduces `github` toward
`[]` — which is the already-shipping default state of the product.

There is no path by which this feature can make the catalog worse than a default
install, and none by which it can make `GET /api/templates` return anything but
200.

The `except` layers are a second layer, deliberately:

- `get_registry_templates()` never raises — every internal failure returns `[]`;
- `template_service._registry_template_overrides()` fences the call **again**,
  because fail-open must not depend on the registry service being bug-free;
- each entry still passes through `_safe_build_github_template`, so #1835 /
  ent#89's *one bad entry costs itself, never the catalog* extends to registry
  entries unchanged.

### The matrix

Every row is driven end-to-end in `tests/unit/test_ent14_catalog_failopen.py`
through the real parser and a real `httpx` client over `MockTransport`, and the
assertion is on the **catalog output** — never on an internal call count.

| Condition | Status code | Catalog |
|---|---|---|
| host unreachable | `unreachable` | bundled floor |
| read timeout | `timeout` | bundled floor |
| 4xx / 5xx | `http_error` | bundled floor |
| 3xx redirect | `redirect` | bundled floor (never followed) |
| body over 256 KiB | `too_large` | bundled floor |
| body over cap with a **lying** `Content-Length` | `too_large` | bundled floor |
| compressed body (any `Content-Encoding`) | `encoding_refused` | bundled floor (never read) |
| malformed YAML | `parse_refused` | bundled floor |
| YAML alias / anchor (incl. a level-6 bomb) | `parse_refused` | bundled floor |
| duplicate top-level key | `parse_refused` | bundled floor |
| HTML captive-portal body | `bad_shape` / `parse_refused` | bundled floor |
| binary body | `bad_shape` | bundled floor |
| top-level list or scalar | `bad_shape` | bundled floor |
| `templates` missing / not a list | `bad_shape` | bundled floor |
| unknown `version` | `unsupported_version` | bundled floor |
| URL fails the SSRF gate | `invalid_url` | bundled floor (never dialled) |
| toggle or hard switch off | `disabled` | bundled floor (never dialled) |
| registry service **raises** | — | bundled floor (outer fence) |
| `templates: []` | `ok` | bundled floor, reported as a **success** |

That last row is not a failure. An empty registry is the deliberate day-one state
the ship prerequisite publishes, and conflating it with an outage would report an
operator's intentionally-empty catalog as broken.

---

## The document

```yaml
version: 1                      # absent => 1; unknown/greater => document REFUSED
templates:
  - repo: Abilityai/cornelius   # required, ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$, no dot segments
    display_name: Cornelius     # optional, <=200 chars (truncated, not rejected)
    description: Your second…   # optional, <=1000 chars (truncated, not rejected)
    priority: 20                # optional int, lower sorts earlier
```

`version:` is the **only** forward-compat mechanism — deliberately not a
versioned URL path (unlike `OPERATOR_INTAKE_URL`'s `/v1/`), because the URL
default is baked into `config.py` and bumping it would cost a release, which is
the cost this feature exists to remove.

### Tolerant reader

| Condition | Behaviour |
|---|---|
| top-level not a mapping | whole document refused → floor |
| `version` unknown / non-int / bool | whole document refused → floor |
| `templates` missing / not a list | whole document refused → floor |
| `templates: []` | **success**, zero entries, status `ok` |
| more than `MAX_REGISTRY_TEMPLATES` (25) | truncated to the cap + named error |
| entry not a mapping | that entry dropped + named error |
| `repo` missing / non-string / pattern fail / dot segment | that entry dropped + named error |
| duplicate `repo` (case-insensitive) | later occurrence dropped + named error |
| `display_name` / `description` non-string | field ignored (falls through to `template.yaml`) |
| `display_name` / `description` oversize | truncated to the field cap |
| `priority` non-int or `bool` | field ignored |

Errors surface on the **settings status** endpoint, never on the catalog: an
operator debugging their registry needs them, a user browsing templates does not.
The error list itself is capped (50 + an "and N more" line) so a hostile registry
cannot make the admin payload enormous, and any interpolated foreign string is
snipped to 80 characters.

### The allowlist is the blast-radius bound

A registry entry is parsed into a frozen four-field record and **never splatted**
into the template dict. Unknown keys are ignored, not merged, so a registry
cannot assert `fork_to_own`, `credentials`, `credential_setup`, `schedules`,
`data_paths`, `persistent_state`, `resources`, `skills`, `hidden` or `id`. Every
one is a claim about a repo the registry does not own and every one has a
creation-path consequence. `github_repo` and `id` are both computed by
`_build_template` from the same `repo`, so a card can never display a repo path
different from the one it would clone.

**`repo` is a capability pointer — say it plainly.** It is *literally* true that
the four allowlisted fields only change which repos are listed and how they are
labelled and ordered. As a security statement that is materially misleading: by
choosing `repo`, the registry chooses **which `template.yaml` Trinity fetches and
trusts**, and that document declares everything the allowlist just refused. The
registry does not set those fields; it **selects the document that does**. The
allowlist bounds the *direct* blast radius, not the indirect one. (Same
distinction ent#123 draws for tokenless public clones: the platform trusts a repo
it did not author.)

**What a hostile registry can do**: cause `evil/repo` to appear with a
trustworthy-looking name. Any `creator`-role user can already create from
`github:evil/repo` by typing it, so the registry grants **no new permission** — it
grants **persuasion**. Mitigations: the default URL is vendor-controlled HTTPS,
changeable only by an admin **human**; the card always renders `github_repo` under
the display name; two independent off-switches. **Not** shipped in v1: signature
verification of the document, and any allowlist on which repo owners a registry
may list. Both belong to the private/per-customer-catalog phase, where the trust
model is genuinely different.

---

## Security envelope

**YAML — `AliasPolicy.REJECT`** via the named
`utils.safe_yaml.load_template_registry_yaml()` helper, so the policy is pinned at
the `utils/` layer and never relitigated at a call site. A four-scalar-field
schema has no legitimate anchor, and this is the most exposed document in the
system: network-fetched, unsigned, process-cached, fanned out to `/api/templates`
for every authenticated user. `template.yaml` gets BUDGET while being *less*
exposed. The `safe_yaml` docstring was amended in the same change, because its
"a template catalog entry may legitimately anchor a repeated block" clause reads
as an argument for the opposite choice — it means the rich per-repo payload, not
the remote index.

**Byte cap, two layers, transport load-bearing.** The fetch streams and aborts
the moment a running ceiling is crossed. A `Content-Length` check is *not*
sufficient — absent on chunked responses, trivially lied about — and `resp.text`
on a 10 GB body OOMs the worker before any parse-time cap could act. The declared
length is checked only as an early abort. `max_bytes` is passed to the parser as a
belt so the cap survives a future refactor of the fetch layer.

**The ceiling counts WIRE bytes** (`iter_raw()`), and a compressed body is
refused outright (`encoding_refused`) before it is read. The first cut counted
`iter_bytes()` — *decoded* chunks — and httpx sends `Accept-Encoding: gzip,
deflate` unless told otherwise, so a body whose wire size passed the
`Content-Length` abort inflated ~1030:1 before the running total was ever
consulted: **458 MB of transient allocation, on the event-loop thread, from a
199 KiB response** (ent#14 S1). The refusal returned `too_large` correctly, so
this was never a correctness bug — it was the memory bound failing to bound the
resource actually under attack, which is the peak and not the decoded total.
`Accept-Encoding: identity` is sent too, but only as the polite half: it asks a
cooperative server not to compress ~1 KB of YAML and cannot bind a hostile one.
The same body over `iter_raw()` peaks at 0.2 MB.

The harness carried the same blind spot and is worth knowing about before
writing a transport test here: `httpx.Response(text=…)`/`(content=…)` decodes
and buffers in the CONSTRUCTOR, so a buffered mock can only ever exercise
`iter_bytes()` and the ceiling tests were green against a shape that could not
express the failure. Both fixtures now convert every mock response to a real
stream (`_as_streaming`), and a compressed body must be built as
`stream=httpx.ByteStream(raw)` — passing compressed bytes as `content=` is
refused loudly rather than silently decoded before the code under test sees it.

**SSRF gate** (`utils.url_validation.validate_template_registry_url`): HTTPS only;
**no userinfo** (rejected outright, never stripped, so a credential cannot be
persisted into a settings row or echoed through a status payload); resolve-and-
reject private / loopback / link-local / reserved / multicast destinations, plus
**RFC 6598 shared address space (`100.64.0.0/10`)**, which Python's `ipaddress`
reports as neither `is_private` nor `is_reserved` and which several cloud
providers use for internal endpoints (ent#14 S3 — not reachable in Trinity's own
`172.28/16` + `172.29/16` topology, a hole the shape of `10.0.0.0/8` anywhere
that does use it);
**`follow_redirects=False`**, because a URL that passed the gate and then
redirects is a bypass and `raw.githubusercontent.com` does not redirect for a
valid path. Named residual: pre-resolution does not close DNS rebinding (a TOCTOU
between validate and connect), accepted for v1 because the URL is
admin-and-human-set, the response becomes a display-only allowlisted record, and
the body never reaches a deserialize sink.

**`repo` charset.** A fourth copy of a pattern that already exists three times
(`routers/settings._REPO_PATTERN`, `crud._GITHUB_REPO_PATH_RE`,
`Settings.vue REPO_PATTERN`), duplicated rather than imported per the
`_LOCAL_TEMPLATE_NAME_RE` convention — and carrying that convention's obligation:
a **behavioural** parity test over a fixture corpus
(`tests/unit/test_ent14_repo_pattern_parity.py`), because the existing copies
already differ in character-class ordering while denoting the same set.

> **Deliberate divergence, pinned by test.** `.` is inside the character class, so
> `../evil` **matches all three older copies**. The registry gate refuses dot
> segments explicitly: `github:../evil` would render "../evil" on the card while
> `api.github.com/repos/../evil/…` and `github.com/../evil.git` both normalize to
> a *different* repo — the card advertising one path and cloning another. The
> older copies share the hole on paths reachable only by an authenticated
> `creator` typing an id by hand; that is filed, not fixed here. The parity test
> asserts the containment that matters: everything the registry **accepts**, the
> other three accept. Strictly stricter, never differently strict.

**Status vocabulary.** `last_error_code` is a fixed lowercase set (`unreachable`,
`timeout`, `http_error`, `redirect`, `too_large`, `encoding_refused`,
`parse_refused`, `unsupported_version`, `bad_shape`, `invalid_url`, `disabled`)
— never a raw
exception string, so a hostile server's response text cannot reach the operator's
panel. The panel translates the code into prose locally.

---

## Caching

Its own cache — never a share of `template_service._metadata_cache`, whose value
type is a raw metadata dict with no status, no staleness and no invalidation hook.

| Property | Value | Why |
|---|---|---|
| TTL | 3600 s ± up to 300 s jitter | **Deliberately unaligned** with the 600 s per-repo TTL. Aligning is a *correlated thundering herd*, not a shared rhythm: one expiry fires the registry fetch **and** N per-repo GitHub fetches in the same instant, and `--workers 2` drift into phase. A registry changes on a human's git commit. The longer TTL is also the cheapest lever on the GitHub budget below — it is doing security work, not tidiness. |
| Serve-stale | on failure, capped at 7 days | Unbounded stale keeps a de-curated, renamed or compromised repo listed indefinitely while the operator sees a catalog that still renders — no signal at all. Past the cap: degrade to the floor. |
| Negative cache | 60 s | A dead URL costs one bounded request per minute per worker, not one per catalog load. |
| Invalidation | **generation counter** (`template_registry_generation`), bumped on every settings write, read at cache-hit time | A per-process `invalidate_registry_cache()` clears only the calling worker, so under `--workers 2` an admin who repoints the URL sees it apply on roughly half their page loads — a *nondeterministic* setting, which is worse than a slow one. **Coupled to the TTL raise: the two must not be split.** |
| Durable LKG | `template_registry_lkg`, sanitized **parsed** JSON | Written only when the normalized content changes, so a steady-state install writes no rows. Invalidated by a URL change, either off-switch, a parser-version bump, or the same stale cap. Entries are re-validated on read. Ships because the registry is default-ON and primary: without it a first boot during an outage shows a fresh operator the bundled floor — the exact first-screen problem this feature exists to fix, now with a network dependency in front of it. |

A `threading.RLock` guards the cache: `get_all_templates()` is sync, so FastAPI
runs it in a threadpool and several threads of one worker reach this
concurrently. The lock is held across the fetch on purpose — blocking a sibling
thread for one bounded 5 s fetch beats N simultaneous fetches of one document.

---

## Two off-switches, deliberately asymmetric

| Switch | Where | Semantics |
|---|---|---|
| `TEMPLATE_REGISTRY_ENABLED` | env → `config.py` | **Hard** kill switch. `false` ⇒ never fetch, and no DB row can turn it back on. The air-gap / policy answer. |
| `template_registry_enabled` | `system_settings` row | Admin toggle, **default true when absent**. Composed with the hard switch at the consumer. |

Deliberately **not** `settings_service._resolve_bool_flag`: its env leg is
*opt-in only* (`"true"/"1"/"yes"` → True, anything else falls through to
`default`), so with `default=True` it would silently swallow
`TEMPLATE_REGISTRY_ENABLED=false` and ship an inert kill switch — the #1039
"inert by obscurity" class, caught before a line of it was written. Uses the
`OPERATOR_INTAKE_ENABLED` / `TELEMETRY_SHARING_ENABLED` shape instead.

### …and then it shipped inert anyway, one layer down

Both vars must be injected into `backend.environment:` in **`docker-compose.yml`
*and* `docker-compose.prod.yml`**, and documented in `.env.example`. The first
version of this feature wired neither. Prod compose launches **standalone** — no
base-compose merge, no `env_file:` on any service — and it is what every deploy
path uses (`.github/workflows/deploy-dev.yml`, `scripts/deploy/gcp-deploy.sh`),
so the explicit `environment:` list is the only path into the container. The
result was the precise outcome the paragraph above congratulates itself for
avoiding: a hard kill switch that does nothing, on a feature that is default-ON
and makes outbound requests. It worked on a laptop, where the shell environment
reaches the process directly.

The lesson is worth more than the fix: **avoiding a known failure mode in the
layer you are looking at does not avoid it in the layer you are not.** The
`_resolve_bool_flag` analysis was correct and load-bearing, and it was reasoning
about the *code path* while the flag died in *packaging*. This is the
#1039/#1056 class for the sixth time (#1056 `VOIP_*`, ent#31 `LOG_*`, #1039,
#1871 `AGENT_LOG_*`, #411 `CANARY_*`, this), which is why it is now pinned by
`tests/unit/test_ent14_registry_env_packaging.py` rather than trusted:

- injection present in **both** composes (drift between them *is* the bug);
- `TEMPLATE_REGISTRY_ENABLED` uses the `${VAR:-true}` form — a hardcoded `- TEMPLATE_REGISTRY_ENABLED=true`
  satisfies a presence check while re-breaking the switch, so presence is not
  the property worth asserting;
- `TEMPLATE_REGISTRY_URL` carries its **full non-empty** default — a bare `:-`
  arrives set-but-empty, `os.getenv` returns `""` instead of falling back, and
  the registry points at nothing (#1076 class). Strictly worse than an unwired
  var, because it looks wired;
- the compose default and the `config.py` default are asserted equal, since two
  spellings of one default drift invisibly (the container always wins, so the
  code default becomes decorative while still reading as authoritative);
- a meta-test proves the matcher goes red on pre-fix content *and* on the
  hardcoded-value revert.

One note on verifying a guard like this, because the first attempt produced a
false green: proving it by deleting the real compose line and re-running is the
right instinct, but the deletion was done with
`grep -v 'VAR=${VAR'` — where `$` is a **regex end-of-line anchor**, so the
pattern matched nothing, the line was never removed, and the suite passed
against an unmodified tree. Use `grep -vF`, and confirm the file actually
changed before believing the run.

Deliberately **not** coupled to `DO_NOT_TRACK`: those two honour it because they
*send* data about the operator. A registry fetch sends nothing — it is a
package-index read, and npm and Homebrew do not disable their default registries
under DNT. It is still outbound egress on a default install, which is a real
behavioural change and carries a release note.

---

## Admin surface

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/settings/template-registry` | `assert_admin`. `{source, url, default_url, effective_url, enabled, hard_disabled, suppressed_by_github_templates, status}` |
| `PUT` | `/api/settings/template-registry` | `assert_admin` **+ `reject_agent_principal`**. Partial `{url?, enabled?}`; URL SSRF-validated (400), hard-disabled + `enabled: true` → 409; bumps the generation; audit `template_registry_config_change` |
| `DELETE` | `/api/settings/template-registry` | `assert_admin` **+ `reject_agent_principal`**. Clears both rows **and the durable LKG** (captured under the old URL); audit `template_registry_config_reset`, unconditionally |

Registered **before** the `/{key}` catch-all (Invariant #4), like
`/skills-library` and `/brain-orb` — asserted by test, because if `/{key}` won,
`template-registry` would be swallowed as a setting *name* and every other test
would be exercising the wrong handler.

**`reject_agent_principal` on the writes is not optional.** `assert_admin`
answers *what role*, never *is this a human*: `get_current_user` resolves an
agent-scoped MCP key to its owner **carrying the owner's role**, so on a default
admin-owned install any agent's injected `TRINITY_MCP_API_KEY` satisfies a bare
admin gate (trinity-ops-agent#232, Invariant #8). The consequence here is direct
and total — an agent could repoint the platform's template registry at a URL it
controls. `GET` stays admin-only without the human gate: it reads an operator-set
URL, same as TMPL-001's GET.

**Catch-all blocklist.** All four keys 422 on the generic `PUT /api/settings/{key}`
(which takes an unvalidated `Dict[str, str]` — without this the SSRF gate is one
request away from bypass) **and** on `DELETE /api/settings/{key}`. The DELETE half
differs from the #1644 retention acks on purpose: deleting an ack re-arms a guard
and fails safe, whereas deleting `template_registry_enabled` reverts it to its
default of **ON**, re-enabling egress an operator deliberately switched off —
through a route that carries no human gate. `generation` and `lkg` are blocked
because they *are* the cache, and a writable cache is a poisonable one.

**Status is part of the contract.** Fail-open makes every registry failure
invisible in the catalog by design, so the panel is the only place an operator can
see that their registry 404s (ent#236's *"the panel must be able to show a
failing auto-sync"*). Resolving the status goes through the same cache the catalog
uses, so it fetches only when a fetch was already due.

---

## Frontend

`components/settings/TemplateRegistryPanel.vue`, mounted in `Settings.vue`'s
`agents` tab with one import and one tag.

A **new file** specifically so `Settings.vue`'s raw-color counts cannot move (the
ratchet only lets per-file counts shrink and that view sits at 63 non-gray); the
tag carries no class. The panel is 0 non-gray / 0 hex / 32 semantic tokens.

- URL field + Save + Reset (Reset disabled while `source === 'default'`).
- Toggle, rendered inert with an explanation when `hard_disabled` — a toggle that
  silently does nothing is worse than one that says why.
- A notice when an admin GitHub-Templates list suppresses the registry, so the
  panel does not report a healthy registry that is contributing nothing.
- Status chip (shape **and** hue, principle 24), template count, last-read time,
  a stale marker, a prose explanation keyed by `last_error_code`, and the
  per-entry document problems behind a disclosure.
- `LoadFailed` for a failed fetch, `InlineError` for a failed verb: "couldn't load
  the settings" and "couldn't save the URL" point at different remedies, and a
  failed verb persists next to its control rather than becoming a toast
  (principle 18).

**`Library.vue` is not touched.** Registry entries are `source: "github"` and land
in the existing GitHub Templates grid with zero markup change — which is also why
the ent#126 `Library.vue` collision is designed away rather than merged around.

**MCP**: no functional change (Invariant #13 satisfied without code, because the
catalog payload shape is unchanged). One stale string corrected in
`create_agent`'s `template` description, which claimed `list_templates` "returns
local templates only until an admin curates GitHub repos in Settings".

---

## The honest cost: GitHub calls return

#1931's side-effect was *"`GET /api/templates` makes zero outbound GitHub calls on
a cold metadata cache"*. A non-empty registry re-introduces one `template.yaml`
fetch per listed repo per cold per-repo cache: `workers × windows/hr × entries`,
i.e. **`12 × entries` per hour** at the 600 s per-repo TTL with `--workers 2`.

On an install with **no platform PAT**, GitHub's anonymous limit is 60 req/hr per
IP — so above roughly **5 entries** the metadata fetch is rate-limited some of the
time. Three things make that acceptable, and all three are in the requirements
entry rather than left for an operator to discover:

1. **It degrades gracefully by design.** A 403 returns `{}` and `_build_template`
   falls back to the **registry-supplied** `display_name` / `description`, so the
   card still renders and only the derived chips (MCP servers, credential count)
   go empty. That is the concrete payoff of reusing the `admin_override` shape.
2. **It is not new** — an admin curating 25 repos via TMPL-001 hits the same wall
   today.
3. **A platform PAT raises it to 5000/hr** and is already a first-class settings
   surface.

`MAX_REGISTRY_TEMPLATES = 25` is sized against this, and the 3600 s registry TTL
keeps the registry's *own* fetch off that budget entirely.

---

## `fork_to_own` fails closed (the fix that gates default-ON)

Graceful degradation is true for display fields and **false** for `fork_to_own`.

```
_fetch_template_yaml_result()  -> ({}, "HTTP 403") on a rate limit
_get_cached_metadata_result()  -> caches the {} for a full 600s TTL
_build_template()              -> "fork_to_own": None
crud._apply_fork_to_own()      -> `None == "required"` is False
=> the gate never fires; the agent is created bound to the SHARED UPSTREAM
   TEMPLATE REPO instead of a user-owned copy.
```

`fork_to_own: required` exists precisely to prevent that, and the failure is
silent — the user's knowledge base ends up in the wrong place with no error. It is
the ent#162 class ("a private KB could reach the shared public upstream"), reached
without any attacker.

**Pre-existing.** This feature converts it from unreachable to *expected*: it
re-introduces per-repo fetches on a **default** install, ships default-ON, and its
own arithmetic puts a PAT-less install over the anonymous budget above ~5 entries
— while the curated fleet the registry exists to serve very likely includes the
`fork_to_own: required` agent.

**The fix.** `_fetch_template_yaml_result` already distinguished "no
`template.yaml`" from "could not read it"; the wrapper threw the reason away one
frame below the code that computed it. Creation now reads it and **refuses**
(`503`, `TEMPLATE_METADATA_UNAVAILABLE`, retryable, naming the platform-PAT
remedy) rather than guessing.

**Which read decides** (ent#14 S2 — the first cut got this wrong). Both the
availability verdict *and* the `fork_to_own` value come from
`_read_source_template`, the **creation-path** read: `template.yaml` fetched with
the PAT that will actually clone (per-agent → per-user → global, ent#162), at the
requested ref, cache-bypassed. The first cut read the CATALOG dict
(`gh_template["metadata_unavailable"]` / `["fork_to_own"]`), which comes from
`_get_cached_metadata_result` — global platform PAT, default branch, 600 s cache
— one call *after* `_resolve_template` had already made the correct read for
ent#89's `schedules:`. That was wrong in both directions at once:

- **False pass.** GitHub answers **404, not 403**, for a repo a token cannot see.
  A *private* `fork_to_own: required` template readable only by the creator's own
  per-user PAT therefore classified as ABSENT (`metadata_unavailable` False,
  `fork_to_own` None) — the gate passed and the agent bound to the shared
  upstream. The exact outcome the gate exists to prevent, no attacker involved.
  Note that fixing only the availability half would NOT have closed this: the
  *value* has to come from the correctly-credentialed read too.
- **False refuse.** A creator whose own PAT read the template fine was 503'd
  because the shared cache entry said 403 — for the full 600 s TTL, on every
  non-forking `github:` create, including the plain `github:owner/repo` escape
  hatch the acceptance criteria call untouched.

Costs **zero extra GitHub calls** — it consumes the read ent#89 already made. The
structural argument is that the read which DECIDES is now made with the same
credentials as the clone that follows, so "we could not see it" and "the agent
could not have cloned it" can no longer disagree. `fork_to_own: required` is
enforced if **either** read declares it (a union, not a precedence), so the
change can only ever remove a false pass, never add one.

- A clean **HTTP 404 stays "absent"**, so a repo that genuinely ships no
  `template.yaml` creates exactly as before.
- Scoped to the **non-forking** branch: a caller who supplies a fork destination
  ends up with a user-owned repo whatever the template declares, so an outage must
  not block them.
- The trade is deliberate: creation now depends on GitHub API reachability where
  it previously depended only on `git clone`. A loud, retryable refusal beats a
  silent wrong-repo binding — the `learnings.md` 2026-07-15 direction-of-failure
  rule applied to a gate instead of a retention window.

**Known adjacent defect, tracked separately.** `_get_cached_metadata_result` /
`_fetch_all_metadata_results` write the cache **unconditionally**, so a `{}` from
a transient 403 overwrites a previously-good entry and is served for a full 600 s.
That is what turns the window from one request into ten minutes. It is strictly
wider than the registry — that cache serves every template path — so it is filed
rather than folded in. Caching the *reason* alongside the empty metadata is what
keeps the gate correct for the whole stale window regardless.

**`mcp_servers` and `resources` degrade the same way** on an unreadable
`template.yaml`. Only `fork_to_own` has a security consequence, so only it fails
closed — but the pattern is named here so the next reader does not rediscover it.

---

## `hidden` is inert on the GitHub half

`_build_local_template` sets it and `get_local_templates` filters on it, but
`_build_template` never emits it and `_safe_build_github_template` never filters
on it. So a registry cannot mark an entry hidden — *and* a listed repo whose own
`template.yaml` says `hidden: true` is still shown. Neither is exploitable; the
local/github divergence is real and recorded so it is not rediscovered as a bug.

---

## Ship prerequisite (not code)

A valid document — possibly empty (`version: 1` / `templates: []`) — must exist at
the default URL before or with the release. Then day-one behaviour is "fetch
succeeds, zero entries, catalog unchanged, no warnings", and publishing the
curated fleet (ent#137) becomes a pure content edit, which is the whole point.
Owner: @vybe. Blocking for the release cut, not for the PR.

---

## Testing

| File | Covers |
|------|--------|
| `tests/unit/test_ent14_registry_document.py` | The pure parser: happy path, `templates: []` as a success, every document-level refusal, aliases + a level-6 bomb + duplicate keys + oversize, per-entry drops, dot segments, case-insensitive duplicate repos, field caps, `priority: true`, the error-list cap, and the **allowlist** (16 forbidden keys, none reaching the record) |
| `tests/unit/test_ent14_registry_fetch.py` | Transport + cache: the byte ceiling, a **lying `Content-Length`** (re-probed to prove the lie was told), an early abort on a declared oversize length, connect/timeout/HTTP/redirect, HTML and binary bodies, URL-validation refusal, TTL + jitter, URL change, **generation bump (cross-worker)**, serve-stale, the 7-day cap, negative caching, and the durable LKG (persist-on-change-only, cold-start restore, and rejection of a wrong-URL / older-parser / too-old / tampered row) |
| `tests/unit/test_ent14_catalog_failopen.py` | **The matrix at the seam** — 19 failure modes × 2 (catalog equality and through the router's sort), a raising registry service, per-entry fencing, the full precedence ladder incl. zero-requests-when-curated, the config hard switch beating the DB row, **list/detail/create sharing one ladder**, payload key-set equality, and a hostile registry failing to inject `fork_to_own` / `credentials` / `schedules` / `hidden` / `id` |
| `tests/unit/test_ent14_registry_settings_api.py` | GET/PUT/DELETE, the principal matrix (**an admin-owned agent key is refused on both writes**), audit rows on write *and* on a no-op reset, partial update, the hard-switch 409, and the catch-all 422 on PUT **and** DELETE for all four keys |
| `tests/unit/test_ent14_repo_pattern_parity.py` | All four `owner/repo` gates agree on a corpus; the registry gate is stricter on dot segments; registry acceptance is a **subset** of every other gate |
| `tests/unit/test_ent14_fork_to_own_failclosed.py` | The F2 regression — verified red without the fix — plus the **S2** half: the private-404 false pass and the poisoned-cache false refuse, both driven through the real `_resolve_template` with a GitHub whose answer depends on WHICH token asks (3 of those fail against the catalog-reading gate) |
| `tests/unit/test_ent14_registry_url_ssrf.py` | The SSRF gate itself, DNS stubbed: RFC 6598 refused with both `/10` boundaries held public, one CGNAT record among public ones enough to refuse, the neighbouring internal ranges pinned, scheme + userinfo + unresolvable-host |
| `tests/unit/test_ent14_registry_env_packaging.py` | **Packaging** — both knobs injected in both composes, `${VAR:-true}` rather than a hardcoded `true`, the URL's full non-empty default, compose↔`config.py` default agreement, dev/prod parity, `.env.example` coverage, and a meta-test that goes red on pre-fix content *and* on the hardcoded-value revert (see [the section above](#and-then-it-shipped-inert-anyway-one-layer-down)) |

`tests/unit/pytest.ini` overrides `pyproject.toml`, so `asyncio_mode = auto` does
**not** apply in that directory: a bare `async def test_*` is collected and
silently never awaited. Every test here is sync; the one async call under test is
driven through an explicit `asyncio.run`.

---

## Related Flows

- **Upstream**: [platform-settings.md](platform-settings.md) — the TMPL-001 seam this extends
- **Downstream**: [library-page.md](library-page.md) — where registry entries render
- **Downstream**: [template-processing.md](template-processing.md) — per-repo `template.yaml` reading
- **Related**: [agent-repo-binding.md](agent-repo-binding.md) — the `fork_to_own` ownership story

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-08-04 | **ent#14 — initial implementation.** Runtime registry fetch + cache, admin URL/toggle surface, the fail-open matrix, `AliasPolicy.REJECT` + the named `safe_yaml` helper, the SSRF gate, the ladder in both resolvers (F1), and the `fork_to_own` fail-closed fix (F2). |
| 2026-08-05 | **CSO diff findings closed.** S2 — the `fork_to_own` gate now decides from the creation-path read (right credentials, pinned ref, no cache) instead of the platform-PAT catalog cache, closing a false pass on private templates and a 600 s false refuse. S1 — the byte ceiling counts wire bytes (`iter_raw()`) and any `Content-Encoding` is refused, closing a ~1030:1 decompression bypass measured at 458 MB from a 199 KiB body; both transport fixtures now build real streams, since the buffered mock shape could not express the failure. S3 — RFC 6598 (`100.64.0.0/10`) added to the SSRF deny set. |
