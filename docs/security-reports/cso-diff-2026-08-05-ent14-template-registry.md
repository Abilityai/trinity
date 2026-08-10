# CSO Audit — `feature/ent14-remote-template-registry` (diff scope)

- **Mode**: `/cso --diff` (daily, 8/10 confidence gate)
- **Date**: 2026-08-05
- **Base**: merge-base `e6df5bf8` → `3900d23a` (12 commits, 28 files, +4835/-63)
- **Phases run**: 0, 1, 2, 3, 4, 5, 6, 7, 9 (A01/A02/A03/A05/A09/A10), 12, 13, 14
- **Verification note**: Phase-12 independent verification was performed **in-context**, not via
  fresh-context subagents (session policy forbids unrequested subagent dispatch). Every finding
  below instead carries an *empirical* proof — a script driving the real shipped function, or a
  direct source trace — rather than a second model's opinion.

## Feature under audit

A `registry.yaml` fetched over HTTPS from an admin-configurable URL supplies the GitHub half of
the template catalog. New trust boundary: **an unsigned, network-fetched, author-controlled
document, parsed on every worker and fanned out to every authenticated user via
`GET /api/templates`.**

---

## Findings

```
#   Sev    Conf    Status     Category   Finding                                              File:Line
──  ────   ─────   ────────   ────────   ──────────────────────────────────────────────────   ─────────────────────────────
S1  MED    10/10   FIXED      DoS/ctrl   Byte ceiling bypassed 1000:1 by Content-Encoding      template_registry_service.py:486
S2  HIGH   8/10    FIXED      AuthZ      fork_to_own gate decides from the platform-PAT        crud.py:590 / template_service.py:233
                                          catalog cache — false-pass on private templates
S3  LOW    9/10    FIXED      SSRF       RFC 6598 CGNAT range not in the deny set              url_validation.py:182
```

### S1 — The registry byte ceiling is bypassed 1000:1 by transparent decompression · MEDIUM · **FIXED 2026-08-05**

**File**: `src/backend/services/template_registry_service.py:465-494`

```python
with client.stream("GET", url, headers={"Accept": "...", "User-Agent": "..."}) as resp:
    declared = resp.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > REGISTRY_MAX_BYTES:
        return None, ERROR_TOO_LARGE
    for chunk in resp.iter_bytes():          # <-- DECODED bytes
        total += len(chunk)
        if total > REGISTRY_MAX_BYTES:
            return None, ERROR_TOO_LARGE
```

The docstring states the design goal precisely — *"`resp.text` on a 10 GB body OOMs the worker
before any parse-time cap could act; that is why the transport layer is the load-bearing one"* —
and the transport layer has the same hole one level down. `headers=` does not override httpx's
default `Accept-Encoding: gzip, deflate`, and `iter_bytes()` yields **decompressed** chunks. One
raw chunk can inflate ~1000× before the running total is consulted.

**Empirical proof** (driving the shipped `_fetch_registry_text` over `httpx.MockTransport`):

```
wire bytes: 203861   (REGISTRY_MAX_BYTES = 262144)  → under the cap
request Accept-Encoding: gzip, deflate
result: text=<None>  error_code=too_large
PEAK ALLOCATION INSIDE _fetch_registry_text: 148.6 MB from a 199 KB response
```

The function *does* fail open and return `too_large` — correctness holds. What fails is the memory
bound: a body small enough to pass the `Content-Length` early abort allocates ~149 MB, and at
gzip's 1032:1 ceiling a 256 KiB wire body reaches **~264 MB per request**.

**Exploit scenario**
1. Attacker controls the registry response — a compromised vendor host/repo at the default URL, or
   an admin persuaded to point at a hostile URL (the write is admin + human-gated, so this needs
   an admin or a host compromise).
2. Any authenticated user calling `GET /api/templates` drives the fetch. On sustained failure the
   negative cache permits one fetch per 60 s per worker; on success, one per TTL.
3. Each fetch transiently allocates up to ~264 MB **on the backend's event-loop thread**. Against a
   memory-limited backend container this is GC thrash, multi-second stalls, or an OOM kill of the
   whole backend — not just the catalog.

**Impact**: the control that exists specifically to make a hostile registry survivable does not
bound what it claims to bound. Fail-open still holds; availability does not.

**Recommendation** (two lines, both wanted — the first alone does not stop a *hostile* server,
which can send `Content-Encoding: gzip` regardless of what was requested):
1. Add `"Accept-Encoding": "identity"` to the request headers. The document is ~1 KB of YAML;
   compression buys nothing.
2. Apply the ceiling to `resp.iter_raw()` (network bytes — which is what "bytes actually received"
   in the docstring means) and refuse a non-`identity` `Content-Encoding` as a fetch failure, the
   same way a redirect is refused. Measured with `iter_raw()` on the identical hostile body: peak
   **0.2 MB**.

---

### S2 — The `fork_to_own` fail-closed gate decides from the platform-PAT catalog cache · HIGH · **FIXED 2026-08-05**

**Files**: `src/backend/services/agent_service/crud.py:590` (the new gate) ·
`src/backend/services/template_service.py:233` (`_get_cached_metadata_result`) ·
`src/backend/services/agent_service/crud.py:1136` (the correctly-credentialed read that already ran)

The new gate reads `gh_template["metadata_unavailable"]`, which is derived from
`_get_cached_metadata_result(repo)` → `_get_github_pat()` — the **global platform** PAT, off the
**default branch**, through a **600 s process cache**. `crud.py:1136`, one line earlier, already
calls `fetch_template_metadata_for_create(repo, pat=github_pat_for_agent, ref=url_branch)` —
resolved per-agent→per-user→global PAT, pinned ref, cache-bypassed — and the gate ignores it.

ent#89 wrote the warning against exactly this, in `_declared_schedules_for_github`'s own docstring:

> *"Deliberately NOT the catalog's `gh_template["schedules"]` … that dict comes from
> `_get_cached_metadata`, which reads with the **global platform** PAT off the **default branch**
> through a 10-minute per-process cache. Creation resolves its PAT completely differently
> (per-agent → per-user → global, ent#162) … the exact silent-ignore class this feature exists to
> close, reintroduced one layer up."*

Two consequences, opposite directions:

**(a) False PASS — the gate does not hold for private templates.** GitHub's contents API answers
**404**, not 403, for a repo a token cannot see. `metadata_reason_is_unreadable("HTTP 404")` is
`False` by design, and the code comment concedes the conflation: *"the file (or the repo, for a
private one without a usable token) is not there"*. So for a **private** template that the platform
PAT cannot read but the creator's own per-user PAT can:
`reason="HTTP 404"` → `metadata_unavailable=False` → gate passes → `fork_to_own` never observed →
the agent is created **bound to the shared upstream template repo instead of a user-owned copy**.
That is the precise outcome F2 exists to prevent, still reachable — and ent#162 exists because
private-repo creation with a per-user PAT is a supported flow.

**(b) False REFUSE — availability.** A creator whose own PAT reads the template fine is 503'd
because the shared global-PAT cache entry says 403. On a tokenless install the catalog fetch is
anonymous (60/hr per IP, shared across all users), the poisoned reason is cached for the full 600 s,
and **every non-forking `github:` create is refused for that window** — including the plain
`github:owner/repo` escape hatch the acceptance criteria call untouched.

**Exploit scenario for (a)** — no attacker required, but an attacker helps:
1. An org publishes a private template repo declaring `fork_to_own: required` and shares access with
   its creators via per-user PATs (ent#162), not the platform PAT.
2. A creator creates from it. The catalog read (platform PAT) 404s; the gate reads that as *absent*.
3. The agent is created against the shared upstream. Its knowledge base accumulates in a repo the
   user does not own — silently, with no error. The ent#162 class, no attacker involved.

**Recommendation**: evaluate the gate from the creation-path read that already runs at
`crud.py:1136`. Add a reason-preserving sibling of `fetch_template_metadata_for_create`
(`_fetch_template_yaml_result` already returns `(metadata, reason)`; the wrapper drops it) and gate
on that. This costs **zero extra GitHub calls**, reads with the caller's own credentials, pins the
ref, and bypasses the poisoned cache — which also makes the 503 fire only on a genuine outage
rather than on a stale shared cache entry, shrinking (b) to near-nothing.

---

### S3 — RFC 6598 shared address space is not in the SSRF deny set · LOW · **FIXED 2026-08-05**

**File**: `src/backend/utils/url_validation.py:180-193`

`100.64.0.0/10` (CGNAT) is neither `is_private` nor `is_reserved` in Python's `ipaddress`, so a
registry URL resolving there is admitted. Verified:

```
100.64.0.1  -> blocked=False  (priv=False loop=False res=False ll=False)
```

Not exploitable in Trinity's own topology (both Docker networks are `172.28/16` and `172.29/16`,
which *are* `is_private`), but the range is used for internal addressing by some cloud providers.
One clause: `ip in ipaddress.ip_network("100.64.0.0/10")`.

---

## Verified safe (checked, not assumed)

The prompt asked specifically about SSRF, response-size bounds, YAML policy, and hostile-registry
blast radius. Everything below was traced or executed, not inferred.

**YAML parser policy** — `AliasPolicy.REJECT` via a named `utils/` helper
(`load_template_registry_yaml`), so the policy is pinned at the utility layer and cannot be
relitigated at a call site. Alias bombs, any single alias, duplicate keys and a 256 KiB parse cap
all refuse the whole document → floor. The `safe_yaml` docstring clause that read as an argument
*for* BUDGET has been amended in both copies. Mirrors are **byte-identical** (`diff` clean) and
guarded by `tests/unit/test_1965_agent_server_safe_yaml.py::test_loader_copies_are_byte_identical`
(passing) — Invariant #5 intact.

**SSRF gate** — https-only; userinfo refused outright (not stripped, so a credential cannot reach a
`system_settings` row or the status payload); DNS pre-resolution rejecting
private/loopback/reserved/link-local/multicast/unspecified across **all** returned records.
Empirically confirmed blocked: `::ffff:127.0.0.1`, `::ffff:10.0.0.1`, `::ffff:169.254.169.254`,
`169.254.169.254`, `::1`, `fc00::1`, `0.0.0.0`, `::`, `64:ff9b::7f00:1` (NAT64). `follow_redirects=False`
with a 3xx classified as a failure **before any body is read**. Re-validated on **every** refetch,
so a `TEMPLATE_REGISTRY_URL` set in env cannot bypass the endpoint's validation. DNS-rebinding
TOCTOU is a documented, accepted residual (requirements §4.2.2).

**Blast radius beyond "which templates are LISTED"** — the direct answer:
- *Directly*: nothing. Entries parse into a frozen four-field record and are never splatted;
  `fork_to_own`, `credentials`, `schedules`, `data_paths`, `persistent_state`, `resources`,
  `skills`, `hidden`, `id` are all unreachable (pinned by
  `test_a_hostile_registry_cannot_inject_fork_to_own_or_credentials`). `id` and `github_repo` are
  both derived from the same `repo`, so a card cannot advertise one path and clone another
  (pinned by `test_the_id_always_matches_the_repo_that_would_be_cloned`). `.`/`..` path segments
  are refused — a stricter gate than its three sibling `owner/repo` regexes, which accept them.
- *Indirectly*: **yes, and the code says so plainly.** Choosing `repo` chooses which `template.yaml`
  Trinity fetches and trusts, and that document declares everything the allowlist just refused —
  materialized at creation (schedules ent#89, `data_paths` #1169, credential staging ent#128).
  Correctly documented as a capability pointer in requirements §4.2.2 and the module docstring; the
  escalation it grants is **persuasion, not permission** (any `creator` can already type
  `github:evil/repo`). No signature/provenance verification in v1 — named residual.
- *Side effect of listing*: up to 25 attacker-chosen repos are fetched per cache window using the
  platform PAT, at `api.github.com/repos/{repo}/contents/template.yaml`. Host is fixed and the path
  charset is gated, so this is not a usable SSRF primitive; the cost is GitHub rate budget only.

**Status/error surface** — fixed lowercase vocabulary, never a raw exception string; foreign text
in per-entry errors is bounded to 80 chars, newline-stripped and `repr`-escaped, admin-only, and
rendered through Vue interpolation (no `v-html`) — no XSS path.

**AuthZ** — `GET` is `assert_admin`; `PUT`/`DELETE` are `assert_admin` **+ `reject_agent_principal`**
(correct: an agent-scoped key resolves to its owner carrying the owner's role, so a bare admin gate
would let any agent repoint the platform registry — trinity-ops-agent#232). All four registry keys
are 422-blocked on **both** generic catch-alls (`PUT`/`DELETE /api/settings/{key}`), so the SSRF gate
has no second, unvalidated write path. Both writes are audit-logged (`template_registry_config_change`
/ `_reset`). Route order vs the `/{key}` catch-all is pinned by test (Invariant #4).

**Phase 2 / 3 / 4** — no secrets in the diff (the only matches are the literal test fixture
`github_pat_for_agent="pat"`). No dependency, lockfile, workflow, Dockerfile or compose change, so
supply-chain and CI/CD surfaces are unmoved.

---

## Totals

| Severity | Count |
|---|---|
| CRITICAL | 0 |
| HIGH | 1 (S2) |
| MEDIUM | 1 (S1) |
| LOW | 1 (S3) |

**Trend**: prior diff report `cso-diff-2026-08-02-ent89-template-schedules` — no shared
fingerprints; all three findings are NEW to this branch. S2 is a *sharpening* of the pre-existing
F2 class this branch set out to fix, not a regression it introduced.

---

## Resolution (2026-08-05)

All three findings are closed. Each fix was first proven **red** against the
pre-fix code — a regression test that cannot fail proves nothing.

**S2 — the gate now reads what creation read.** Both inputs moved to
`crud._read_source_template` (the creation-path read `_resolve_template` already
makes for ent#89's `schedules:` — **zero extra GitHub calls**): the availability
verdict *and* the `fork_to_own` value. Moving only the verdict would have left
the false pass open, because a private template's catalog *value* is `None` for
the same reason its verdict is wrong — a half-fix that tests green.
`required` declared by **either** read enforces (a union), so the change can only
remove a false pass, never add one. `template_service.fetch_template_metadata_result_for_create`
is the new reason-preserving sibling; the old wrapper stays for callers whose
decision is not security-relevant.
Proof: `tests/unit/test_ent14_fork_to_own_failclosed.py` drives the private-404
false pass, the poisoned-cache false refuse and a creation-read outage through
the **real** `_resolve_template`, against a GitHub whose answer depends on which
token asks. 3 of them fail against the catalog-reading gate.

**S1 — the ceiling counts wire bytes.** `resp.iter_raw()`, plus an outright
refusal of any non-`identity` `Content-Encoding` before the body is read (new
fixed-vocabulary code `encoding_refused`, degrading to the bundled floor like
every other failure), plus `Accept-Encoding: identity` as the polite half.
Proof: a gzip bomb whose wire size passes the `Content-Length` abort is refused
with **zero body bytes read** (counting stream), and peak allocation is asserted
under 8 MB against 64 MB of decoded payload.

> **Correction to this report's measurement.** The 148.6 MB figure was taken
> through a *buffered* mock (`httpx.Response(content=…)`), which decodes in the
> constructor — so it partly measured the harness, not the shipped function. On a
> true streaming response (the production shape) the pre-fix peak is **458 MB**
> from a 199 KiB body; post-fix, **0.01 MB**. The same artifact is why the
> existing byte-ceiling tests were green: a buffered response marks its stream
> consumed, so it can only ever exercise `iter_bytes()` and `iter_raw()` raises
> `StreamConsumed` against it. Both transport fixtures now convert every mock
> response to a real stream, and a compressed body must be built as
> `stream=httpx.ByteStream(raw)` — passing compressed bytes as `content=` is
> refused loudly rather than silently decoded before the code under test sees it.

**S3 — CGNAT refused.** `100.64.0.0/10` added to the registry validator's deny
predicate (parsed once at import). The **skills-library** validator is
deliberately left alone: its host is allowlisted to `github.com`, so an operator
cannot choose the destination and the same clause would buy nothing.
Proof: new `tests/unit/test_ent14_registry_url_ssrf.py` — the range refused, both
`/10` boundaries held public (a `/8` would blackhole real public space), one
CGNAT record among public ones enough to refuse, and the neighbouring internal
ranges pinned so a future refactor of the predicate stack cannot quietly drop one.
