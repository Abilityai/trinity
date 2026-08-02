# Feature: Guided Credential Setup (trinity-enterprise#127)

## Overview

Trinity agents boot with a structurally-correct but **empty** `.env` /
`.mcp.json` (CRED-002, by design). Nothing in-product told an operator *which*
keys an agent wanted: they had to already know, and hand-paste `KEY=VALUE` into
Quick Inject.

This feature answers three questions per agent, in one place:

1. **What does this agent need?** — from the ent#128 declaration
   (`credentials:` names + the `credential_setup:` overlay), read from the
   agent's **live** workspace so a forked or hand-edited agent reports its
   actual requirements.
2. **Is it set?** — from a bounded `docker exec` probe of the agent's own `.env`,
   reporting **key names with a non-empty value, never values**.
3. **Where do I get it?** — the author's `setup_url`, rendered with a canonical
   host so the link's label cannot disagree with its destination.

Writes reuse the existing owner-gated inject path: **one writer, no new backend
write surface.** No DB table, no migration, no feature flag.

This is the canonical end-to-end trace; the authoritative summaries live in
`docs/memory/architecture.md` (API Endpoints → Credentials, and the
"Per-variable credential status" note) and
`docs/memory/requirements/credentials.md` §3.6.

## User Story

As an operator who has just seeded a fleet from public templates
(trinity-enterprise#122: ent#123 tokenless clone → ent#124 first-run seed →
ent#125 resilient deploy), I want each agent to tell me which credentials it
needs, which are already set, and where to get the missing ones — so I can
finish provisioning without reading its `template.yaml`.

---

## Key Concepts

### "Set" means what the AGENT sees

The status predicate is not an independent `.env` parser. It is *defined* as
agreement with the agent server's own post-injection exporter
(`docker/base-image/agent_server/routers/credentials.py`), which does:

```python
key, _, value = line.partition("=")
key = key.strip()
value = value.strip().strip('"').strip("'")
if key:
    os.environ[key] = value
```

`credential_requirements_service._env_pairs` reproduces that byte for byte and
`tests/unit/test_ent127_predicate.py::TestExporterParity` pins it — including a
source anchor that fails if the real exporter is ever edited. Every quirk is
kept deliberately: `.strip` on a quote peels *all* layers of it, the two quote
characters are stripped independently, duplicates are last-wins, and a leading
`export ` is **not** stripped (nothing in the agent strips it, so `export KEY=v`
genuinely leaves `KEY` unavailable to the runtime — reporting it "set" would be
a false green).

Two departures sit deliberately *outside* that parse and are documented at the
code:

| Departure | Why |
|---|---|
| bytes decoded with `errors="replace"` | the exporter's strict `read_text()` raises and exports NOTHING, so one bad byte would report a fully-configured agent as empty |
| emptiness tested after `.strip()` | a whitespace-only value is a green row in front of an agent that will 401 |

### The bug this exists to fix: `KEY=` reads as "provided"

`generate_credential_files` writes `KEY=` for every declared variable when
creation passes `{}`, and `extract_credentials_from_env_example` counts any line
containing `=` as provided. Reusing that as the status predicate reports **set**
for an agent nobody has configured. Hence the non-empty predicate.

### `.env` absent is a DEFINITE answer

`_stage_config_files` guards on `template_data`, which only the `local:` arm of
`crud.py` populates — so a `github:` agent gets **no generated `.env` at all**.
The ent#123 tokenless seeded fleet (Cornelius is `github:Abilityai/cornelius`)
is exactly AC #3's audience.

> **`.env` absent ⇒ every declared variable is `missing`, never `unknown`.**
> Absence is a definite fact about a running container, not a collection
> failure. `unknown` is reserved for "we could not look".

### `degraded` dominates the empty state

`credentials:` absent / `null` / `{}` — and the all-platform-injected case —
collapse to `no_credentials_required`, a first-class "Ready" card (AC #5). But a
**degraded** lookup produces a textually identical empty requirement set, and
"Ready" is the one state a user will never investigate.

> If any of {container unreachable, template unreadable, catalog unavailable,
> label missing, label names a template not in the catalog} holds, the state is
> `degraded` — **even when the requirement list is empty.**

Reachability makes this structural rather than a rule to remember: the catalog
fallback is only *entered* from a failed live read. An **empty catalog result is
degraded data, not data** — `get_github_template` returns `_build_template(repo,
{})` (empty requirements, not `None`) when the repo is gone or the fetch failed,
and with no PAT GitHub's 60-req/hr anonymous limit makes that the *expected*
outcome for the tokenless fleet.

### `declaration_incomplete` — AC #1's other two sources

AC #1 names three sources. Using `credentials:` alone produces a
confidently-wrong green: of 25 bundled templates, 12 declare `credentials: {}`
and 13 declare nothing, so a legacy template with `${SLACK_BOT_TOKEN}` in
`.mcp.json.template` would render "Ready — this agent needs no credentials".
The codebase already treats undeclared-but-referenced as a HARD compatibility
failure (K-002 → T-015).

A fourth state therefore exists: when `credentials:` yields **zero** records and
`.mcp.json.template` / `.env.example` yield operator-suppliable `${VAR}`s, the
UI says *"this template hasn't declared its credentials"* and lists the observed
names as **advisory** rows. They are an anti-green signal only — never
`required`, never counted in `blocking`. `.mcp.json.template` does not become a
declaration authority; the schema's authoring note forbids it and K-002 polices
the divergence.

### Tri-states, preserved end to end

| Field | Values | Rule |
|---|---|---|
| `status` | `set` \| `missing` \| `unknown` | `unknown` only when the probe could not run |
| `required` | `true` \| `false` \| `"unknown"` | `"unknown"` = a bare `- FOO`, i.e. no authorial intent. Rendered as its own group ("not stated"); **never** counted in `summary.blocking` |

Platform-injected variables (`TRINITY_*`, `GITHUB_PAT`, …) are dropped from the
rows — they are never the operator's to set, and rendering one as "missing" is a
pure false-alarm class — and reported as `summary.platform_injected_excluded` so
the exclusion stays visible.

---

## Flow

```
Agent Detail → Credentials tab
      │
      ├─ CredentialsPanel.vue  (mounts the checklist UNCONDITIONALLY)
      │     └─ stores/agents.js :: getCredentialRequirements  (via api.js)
      │           └─ GET /api/agents/{name}/credential-requirements
      │
      ▼
routers/credentials.py
  get_owned_agent_by_name  ──►  reject_agent_principal  ──►  rate_limiter.enforce
      │
      ▼
services/credential_requirements_service.py
  get_report          (generation-checked cache → single-flight Redis lock)
      └─ build_report
            ├─ collect_agent_credential_facts   ── docker exec ──►  agent container
            │     (bash -c "echo <b64> | base64 -d | timeout 10 python3 - 2>/dev/null")
            │        · template.yaml / .mcp.json.template / .env.example  → text (capped)
            │        · .env                                               → KEY NAMES ONLY
            │
            ├─ _parse_template_text            (SafeLoader, aliases refused)
            ├─ normalize_credential_requirements(source_trust="deployed")   [ent#128]
            ├─ _catalog_requirements           (fallback: trinity.template label)
            └─ describe_setup_url              [services/setup_url_display.py]
      │
      ▼
CredentialSetupChecklist.vue   →  operator types values  →  emit('submit')
      │
      ▼
CredentialsPanel.saveChecklistCredentials
      readExistingEnv()  →  merge  →  formatEnvContent  →  injectCredentials
      └─ POST /api/agents/{name}/credentials/inject     (EXISTING owner-gated path)
            └─ invalidate_report_cache(agent)           (local + Redis generation)
```

---

## Step-by-step

### 1. Router — owner-only AND human-only

`GET /api/agents/{agent_name}/credential-requirements`, gated by
`get_owned_agent_by_name` + `reject_agent_principal`.

This is deliberately stricter than the `/credentials/status` route beside it.
`get_authorized_agent_by_name` resolves an agent-scoped MCP key to **the owner
user carrying the owner's role** — only *connector* principals are fenced — so
under the read gate:

| Principal | Would reach it |
|---|---|
| the agent's own injected `TRINITY_MCP_API_KEY` | ✅ for every sibling the owner can access |
| an agent key on the default **admin-owned** install | ✅ the entire fleet, including other users' agents |
| a shared (non-owner) human | ✅ |

What that discloses is a **targeting map**, not a status light: it names
`STRIPE_SECRET_KEY` per agent and says which are populated (worth stealing) and
which are empty (whose operator is about to paste one). A prompt-injected agent
gets it with one `curl` using an env var it already holds.

`/credentials/status` gets away with the read gate because it returns a *count*
and names zero variables. Every sibling route that names or writes credentials
(inject / export / import) is already owner + human-only. **The read gate must
equal the write gate it drives** — a shared user cannot submit anyway
(`injectCredentials` is owner-gated), so the looser gate would hand them a
checklist of dead inputs whose only working function is disclosing which of the
owner's secrets are missing.

`get_owned_agent_by_name` also preserves Invariant #8 self-uniformity (a uniform
404 for both an absent and an unowned agent) and closes ephemeral ghosts as a
side effect.

**Backpressure.** Every uncached call spawns a container process against the
backend's shared 4-slot Docker pool, so the route rate-limits per
`(user, agent)`; the service adds a cross-worker single-flight lock (409 on
contention) and a short cache. An audit row is written on the read — every
sibling credential route logs one, and silence on the route that enumerates a
credential inventory reads as an oversight — carrying **counts only**.

### 2. The probe, and why the exec is bounded three ways

`execute_command_in_container` **accepts** a `timeout` and then never references
it again; `container_exec_run` has no timeout parameter, docker-py's `exec_run`
has none, and docker-py's socket reader `poll()`s with no timeout before every
`recv`. The call therefore has no bound, and it runs on
`ThreadPoolExecutor(max_workers=4)` — the pool shared by **every** Docker
operation in the backend (Invariant #11). Four wedged calls stop the backend's
entire Docker layer, and it is agent-triggerable: the agent owns
`/home/developer/.env`, and `mkfifo /home/developer/.env` makes `open()` block
forever.

| Bound | What it actually fixes |
|---|---|
| container-side `timeout 10` | **load-bearing** — self-termination closes the socket, so the pool thread is reclaimed. An asyncio cancel does not do this. |
| `asyncio.wait_for` | bounds the REQUEST, so a wedged probe degrades to `agent_unreachable` instead of hanging the caller |
| `stat.S_ISREG` before `open()` | closes the FIFO vector at source |

`compatibility/collector.py`'s `_EXEC_TIMEOUT` is decorative for the same
reason; that is filed separately.

**What crosses the image boundary.** Exactly one policy — the empty-value
predicate — and it is spliced into the script from real source
(`inspect.getsource`), so the tested code and the shipped code are the same
code. Deliberately absent: no charset filter (it would be a hidden fifth member
of `services/credential_charset.py`'s explicit MEMBERS list, and *narrower* than
the runtime it audits, producing silent false-missing), no YAML parsing (alias
expansion is a measured 443 B → 52 MB amplifier — the backend parses, with
aliases refused at compose time), and no `${VAR}` detection.

**Secrets.** The probe returns key names only; `result["output"]` is never
logged or returned (on failure it holds an exception string, and on success a
blob derived from agent-controlled files) — only the exit code is. The backend
then **projects** the key list onto the declared set before it can reach a
response, so an undeclared operator-added variable (`CLIENT_ACME_PROD_TOKEN`,
which leaks a customer relationship) is never disclosed.

### 3. Why a `docker exec` probe and not an agent-server endpoint

An agent-server route would need a base-image rebuild, so every already-deployed
agent would read `unknown` until it was recreated — and AC #3 is explicitly
"works post-hoc for seeded and forked agents". Nothing is vendored, so **no
Invariant #5 parity obligation attaches**.

It is also deliberately **separate from the #668 compatibility collector**. That
snapshot treats `.env` as existence-only by security design and its payload
feeds AI checks, so adding value-derived data to a shared, LLM-bound payload is
a widening; being separate also decouples this panel from compat's cadence.
Accepted cost: two exec paths to keep aligned.

### 4. `setup_url` — mitigating the inherited IDN residual

`_setup_url_error` rejects the `user@host` form, a non-https scheme and
non-printables, and its own docstring records what it does **not** close: IDN
homograph hosts survive, so "a consumer MUST render the parsed hostname next to
the link". This feature is that consumer.

`services/setup_url_display.describe_setup_url` uses the `idna` package with
`uts46=True, transitional=False` — browser semantics. The obvious-looking
implementation (`hostname.encode("idna")`, falling back to the raw host)
manufactures the very display/resolve split it exists to close:

| Host (all pass `_setup_url_error`) | stdlib codec shows | browser resolves |
|---|---|---|
| `аpple.com` (Cyrillic) | `xn--pple-43d.com` | `xn--pple-43d.com` ✅ |
| `faß.de` | **`fass.de`** | **`xn--fa-hia.de`** 🔴 different registrable domain |
| `ς.example` | `xn--4xa.example` | `xn--3xa.example` 🔴 |
| `%D0%B0pple.com` | unchanged, unflagged | `xn--pple-43d.com` 🔴 |
| `a..b.com`, 64-char label | raises → **reported clean** | rejected 🔴 |

Hence: **fail closed.** Any failure returns `display_host is None`, and the UI
renders the raw URL as inert text with "this link's host could not be verified".
Falling back to the raw host would make a failed check byte-identical to a
passed one.

**Claim mitigation, not closure.** Punycode canonicalisation closes confusable
codepoints only. It does not catch subdomain deception
(`accounts.google.com.evil.tld` — pure ASCII, and the commonest shape),
typosquats (`0penai.com`), percent-encoded authorities, or a correct host with a
hostile destination (path/query are unvalidated anywhere). That is why the
result leads with the **registrable domain (eTLD+1)**, emphasised inside the
full host: `accounts.google.com.`**`evil.tld`**. The eTLD+1 derivation is a
documented heuristic over a small embedded suffix list (Trinity ships no Public
Suffix List); the **full host is always displayed**, so the security-load-bearing
part of the render never depends on that table being complete.

`idna` is pinned explicitly in `docker/backend/Dockerfile` even though httpx
already pulls it in — a transitive pin is one dependency resolution away from
vanishing, and that class of packaging bug (#1033) is invisible to
`/verify-local` because the source imports fine on the host.

### 5. Rendering contract (binding)

Enforced by `tests/unit/test_ent127_frontend_contract.py`, because this repo has
no component-test runner (only Playwright e2e against a live stack).

| Rule | Why |
|---|---|
| `title` / `description` / `source` / `default` / `errors[]` render via `{{ }}` **text interpolation only** | they are author-controlled text reaching an operator |
| **NOT** routed through `utils/markdown.js` | markdown is a *widening* here, not a mitigation: it hands the author an arbitrary `[label](url)` surface immediately beside a credential input, defeating the point of one validated `setup_url`. `v-html` stays banned (H-005) |
| anchor text is **always** the parsed host, never `title` | `<a href="https://evil.tld">OpenAI API keys</a>` recreates the userinfo attack in pure HTML with no validator in the way |
| `target="_blank"` only with `rel="noopener noreferrer"` + `referrerpolicy="no-referrer"`; `https:` re-checked at render | the UI must not become the second authority that forgets |
| `secret` masks on `!== false` | `secret` defaults to true; an absent or malformed value must still mask |
| `default` is a **placeholder**, suppressed unless `secret === false` | nothing enforces the schema's "NEVER put a real credential here". An author — or a prompt-injected agent rewriting its own `template.yaml` — sets `default: "sk-attacker-controlled"`, the operator clicks through, and the agent authenticates as the attacker. `secret: true` would *mask* the field, making it **less** likely the operator reads what they submit. `default` exists for `"./Brain"`-style paths, i.e. exactly the `secret: false` case |
| `format` is an **open vocabulary** | never map an unrecognised value onto a DOM attribute (`type`, `pattern`) |

The checklist renders for a **stopped** agent. `loadCredentialStatus` returns
early unless the agent is running; copying that gate onto the checklist would
have made the entire degraded design dead code. Only the *inputs* are gated on
running.

### 6. The write path, and the two defects it carried

The checklist emits `{NAME: value, …}`; the panel funnels it into the existing
read-merge-write (`downloadAgentFile` → `parseEnvText` → merge →
`formatEnvContent` → `injectCredentials`) and refetches. Both defects below were
latent while Quick Inject was a rare bulk paste; a per-row checklist makes
read-merge-write the *normal* interaction.

1. **Destructive on a transient read failure.** The old code swallowed any
   `downloadAgentFile` error as "File doesn't exist, start fresh" →
   `existingEnv = {}` → `formatEnvContent` rewrites the whole file → **every
   pre-existing credential wiped.** The merge base is now mandatory: only a
   genuine 404 (the agent server's own "File not found") is a safe empty base;
   anything else aborts with a named error and writes nothing.
2. **Quote-escape asymmetry.** `formatEnvContent` wrote `\"` for every `"`;
   `parseEnvText` stripped the surrounding quotes but never unescaped. Round
   trip: `a"b` → `a\"b` → `a\\"b` → `a\\\"b` — one backslash **per submit**, for
   **every other credential in the file**, not just the one being edited.
   `parseEnvText` now unescapes, proven by an executed node round-trip rather
   than a grep.

**Security note (not a new class).** This path pulls the full plaintext `.env`
into the operator's browser. That is today's Quick Inject behaviour; the
checklist increases its frequency. Both ends are owner-gated. Recorded rather
than silently normalised: the *endpoint* is read-only, the *feature* is not.

**Residual, unchanged deliberately:** the agent's own reader strips quote
characters and does not unescape, so a value containing `"` reaches the agent
with the backslash. Escaping for a format nobody unescapes is a pre-existing
defect in its own right, and widening the escaping would make the agent-side
mismatch worse. `formatEnvContent` also still drops author comments.

### 7. Cache invalidation

The report cache is per-worker, but a credential POST lands on whichever worker
served it — so purely local invalidation would leave the other worker reporting
"missing" for a variable the operator just set, the single most confusing
outcome this feature could produce. `invalidate_report_cache` therefore also
bumps a Redis generation counter which every read checks (one cheap GET,
fail-open). Both `.env` writers call it: inject and import.

---

## Files

| Layer | Path |
|---|---|
| Router | `src/backend/routers/credentials.py` (+1 route; cache invalidation on inject/import) |
| Models | `src/backend/models.py` (`CredentialRequirement`, `…Summary`, `…Response`) |
| Service | `src/backend/services/credential_requirements_service.py` (probe + report + admission) |
| Service | `src/backend/services/setup_url_display.py` (leaf; zero deps on `template_service`) |
| Service | `src/backend/services/template_service.py` — **read-only consumer, no edits** |
| Frontend | `src/frontend/src/components/CredentialSetupChecklist.vue` |
| Frontend | `src/frontend/src/components/CredentialsPanel.vue` (mount + write-path fixes) |
| Frontend | `src/frontend/src/stores/agents.js` (`getCredentialRequirements`, via `api.js`) |
| Infra | `docker/backend/Dockerfile` (`idna==3.10`) |
| Tests | `tests/unit/test_ent127_{setup_url,predicate,service,endpoint,frontend_contract}.py` |

**No DB table, no migration, no feature flag, no new backend write endpoint.**

---

## Out of scope / explicit non-goals

- **An MCP tool** (Invariant #13). Not because self-enumeration would be a
  widening — the agent owns its own `/home/developer/.env` and the probe runs as
  `developer` — but because the credential inventory is a **human-only operator
  surface** (the endpoint rejects agent principals), the issue does not ask for
  one, and `get_credential_status` already covers coarse file-level status. The
  three-surface cost buys nothing.
- **A persisted last-known snapshot.** It would buy one column while an agent is
  stopped and cost a durable record of which credential names each agent has
  set.
- **Placeholder detection** (`FOO=your-api-key` reads as *set*). We cannot
  verify credentials, and guessing reintroduces the cry-wolf failure the
  tri-state exists to avoid. This also pre-closes the most likely future leak
  vector — length/prefix/hash enrichment.
- Reading `.mcp.json` as a status source (`generate_credential_files`
  substitutes `""` there too, and inject regenerates it from `.env`;
  `.env` is authoritative per the Credential Pattern).
- Credential *validation* — does the key actually work.

## Follow-ups

1. **Overview discovery badge** — `summary.blocking` on the needs-attention
   idiom. Deferred until the exec and rate-limit bounds have soaked: it means N
   probes against a 4-slot pool.
2. **Create-time credential preview** — `credential_requirements` is already on
   the wire in every `/api/templates` entry and referenced by zero frontend
   files.
3. **`compatibility/collector.py` exec timeout** — the identical unbounded hole.
4. **Wire `describe_setup_url` into the normalizer** so catalog consumers
   inherit it instead of re-deriving.
5. **`formatEnvContent` ↔ agent-reader escaping mismatch** (§6 residual).
