# CSO Diff Audit — 2026-11-17 — platform JWTs signed and verified with PyJWT instead of python-jose (abilityai/trinity-enterprise#1124)

**Mode**: `--diff` (branch `feature/ent1124-pyjwt` vs merge-base `73d75e881` on `dev`), daily gate (8/10), cso v1.2.3
**Scope**: 46 files — `dependencies.py`, `routers/auth.py`, `routers/users.py`, `services/event_dispatch_service.py`, `services/password_service.py`, 35 unit-test files, one new unit test file, `docker/backend/Dockerfile` and `tests/requirements-test.txt` (pin relocation + comments, no version change), two docs, one learnings fragment, this report
**Docker**: available; no image was built in this run, so the image-internal checks (`.pth` census, installed-tree OSV) are coverage gaps below

## Phase 0 — what this change is

Every platform token — the 7-day session, the 5-minute MFA challenge, the Workspace session, the event-loopback token — was minted and verified with python-jose (`from jose import jwt`, `except JWTError`). It is now PyJWT, already in the backend image at 2.14.0 and pinned here at **2.15.1** (0 OSV advisories): `jwt.encode(..., algorithm=ALGORITHM)`, `jwt.decode(..., algorithms=[ALGORITHM])`, `except PyJWTError`. The two deliberately unverified reads (`routers/users.py` copies `mode` out of a bearer the auth dependency already verified; `services/password_service.py` reads `jti` from a token it minted one line earlier) spell `options={"verify_signature": False}`. HS256 bytes are identical across the two libraries for identical claims, so every token already in a browser stays valid.

Why: python-jose 3.3.0 is listed under seven OSV ids (three CVEs); the newest release 3.5.0 still carries CVE-2024-33663 with no fixed version, so a bump cannot clear the scanner. **Reachability, stated before priority**: that class is HMAC algorithm confusion when a DER-encoded *public key* is the verifier's key with an unrestricted `algorithms`; Trinity pins `algorithms=["HS256"]` at every decode and `SECRET_KEY` is a shared hex secret (`config.py:91-101`). Neither precondition holds, so the swap is dependency hygiene on an under-maintained library, not the closing of a live hole. The python-jose pin stays in the image for now because a private module still imports it; that line and the test-requirements line go when it is moved.

Stack CVE quicklist: no row has overlap — no image base, lockfile, workflow, compose or runtime-CLI change. The one dependency touched is `pyjwt` 2.14.0 → 2.15.1 (relocated beside the backend's own dependencies), audited in P3.

## Phase 1 — attack surface delta

| Surface | Before | After |
|---|---|---|
| Endpoints, MCP tools, WebSocket channels, agent-context ingress | — | none added or changed |
| JWT verification, 10 decode sites (`dependencies.py` ×8, `routers/auth.py` ×2) | python-jose, `except JWTError` | PyJWT, `except PyJWTError` (base class; `InvalidKeyError` is a `PyJWTError` but not an `InvalidTokenError`), `algorithms=[ALGORITHM]` unchanged at every site |
| Token mint, 2 sites | `jose.jwt.encode` | `jwt.encode`, same header `{"alg": "HS256", "typ": "JWT"}`, same payload bytes |
| Unverified claim reads, 2 sites | `jwt.get_unverified_claims` | `jwt.decode(..., options={"verify_signature": False})` — PyJWT then verifies nothing (`exp/nbf/iat/aud/iss/sub/jti` all default off) |
| Behaviour tightened | python-jose accepted `exp == now`, a future `iat`, and raised a bare `AttributeError` on a `None` token and a bare `RecursionError` on a deeply nested header (both escaped `except JWTError` → 500) | PyJWT refuses `exp == now` and a future `iat`; `None` → `DecodeError`, nested header → `DecodeError`, both caught → 401/None |
| Behaviour loosened | — | none |

## Phases run and verdicts

**P2 Secrets** (diff): added lines scanned for key prefixes, private keys, addresses, private IPs — clean (`example.com` placeholders; the five fixture tokens are HS256 under a 36-byte dummy key named in the test, not a credential). Enterprise-docs guard pattern over the added doc lines: 0 hits; the private module is named only as "a private module still imports it". Invisible-Unicode grep over every changed file: 0 hits.

**P3 Supply chain** (scanner: OSV API per pin, 11-17): `pyjwt==2.15.1` (the pin this diff sets; latest release, 2026-09-28) — **0 advisories**. The 2.14.0 the image carried before had 2, both fixed in 2.15.0, neither reachable: (a) CVE-2026-101918 / GHSA-42vr-xj54-vc7v, CVSS 5.3 (A:L): `RecursionError` from `json.loads` on a ~20 000-deep payload reached only through an UNVERIFIED decode of attacker JSON (PyJWT verifies the signature before parsing the payload — probed: a nested payload under a signed decode is `InvalidSignatureError`, caught). Trinity's only unverified decode of a caller-supplied token is `routers/users.py:210`, wrapped in `except Exception` → `mode=None`; `password_service.py:179` reads a token this process just minted. Not reachable as a 500; DoS class, excluded by rule 1 regardless. (b) CVE-2026-102275 / GHSA-x33g-cr3x-6449: OKP (Ed25519/Ed448) JWK `x`/`d` inconsistency — needs a JWK key object; Trinity passes a string key, HS256 only. Not reachable. The bump to 2.15.1 is in this diff (payments-py declares `pyjwt<3.0.0,>=2.9.0`; `test_ent679` and `test_ent1124` assert image == test requirements == installed). Re-measured on 2.15.1: every behaviour delta below is unchanged, and the nested-payload unverified read is now a caught `DecodeError` (probe). `python-jose==3.3.0` stays installed: 3 CVEs, none imported by OSS code any more; reachable only through the private module's own decode, which is out of this PR. Dockerfile pip block: every line still `==` (no floor introduced). No lockfile change.

**P4 CI/CD / P5 Infrastructure / P6 Webhooks**: no overlap with the diff (no workflow, compose, Redis, network, webhook or internal-router change) — recorded as not applicable, not as clean.

**P7 LLM & agentic**: ASI03 identity — the auth boundary is unchanged: the catch class widens from `JWTError` to `PyJWTError`, which only adds caught failure modes (every PyJWT exception in the decode path descends from it; probed: expired, wrong key, malformed, empty, `None`, `alg=none`, HS512-under-HS256, MCP-key-shaped bearer, nested header). The MCP-key fall-through at `dependencies.py:1016` is the same `except` and is executed by the existing key suites. `algorithms` is an explicit single-item list at all 10 sites, guarded by a new AST test; `alg=none` and a sibling HMAC algorithm are refused (probed). Other ASI rows: no overlap.

**P8 Agent-shipped supply chain**: no overlap (no skill, template, plugin, hook or `.mcp.json` change).

**P9 OWASP 2025**: A04 Cryptographic — HS256 with a 256-bit hex key, algorithm pinned, expiry enforced; identical. PyJWT additionally warns (`InsecureKeyLengthWarning`) when an HMAC key is under 32 bytes; production keys are 64 hex chars (`config.py`, `start.sh`), and `enforce_minimum_key_length` is the one-line fail-closed option if ever ruled. A07 Authentication — session, MFA, Workspace and loopback token semantics unchanged; pre-swap tokens of every kind decode (fixtures minted with python-jose 3.3.0). A10 Exceptional conditions — every decode site states its direction (`except PyJWTError` → `None`/401, fail closed); the unverified read at `users.py:210` keeps its pre-existing `except Exception` → `mode=None` → the fresh token gets mode `admin`, which nothing reads as an authorization input (`dependencies.py:794` returns it; `password_service.py:177` copies it). Two latent 500s are removed (table above).

**P10 STRIDE** (delta only): Spoofing — unchanged (signature + algorithm pinned; a forged `alg` is refused). Tampering — unchanged. Repudiation — unchanged (no audit path touched). Information disclosure — none (error bodies unchanged: "Invalid token" / "Could not validate credentials"). DoS — the nested-header crash class is removed; the PyJWT payload-parse class is not reachable (above). Elevation — none.

**P11 Data classification** (delta): no new data; `SECRET_KEY` (RESTRICTED) handled by the same module-level import; tokens (CONFIDENTIAL) never logged by the changed lines.

**P12 Verification**: all probes run in a python:3.13 container with python-jose 3.5.0 (the test venv) and pyjwt 2.14.0, then repeated with pyjwt 2.15.1 from a scratch overlay — identical results; fixtures minted in a throwaway container with python-jose 3.3.0 (the image's pin). Guard tests: the new `test_ent1124_pyjwt_swap.py` (90 pass; 10 red on the pre-swap tree); `test_293_admin_gate_rejects_agent_keys`, `test_1310_auth_wiring`, `test_2996_human_only_routes`, `test_186_enumeration_uniformity`, `test_ent435_settings_sink_guard` run inside the full unit suite recorded in the results file. No finding survived the gate, so no independent verifier was spawned.

## Findings

```
SECURITY FINDINGS
═════════════════
#   Sev    Conf   Status      Evidence    Category         Finding                                   Phase   File:Line
──  ────   ────   ──────      ────────    ────────         ───────                                   ─────   ─────────
—   none at the 8/10 gate
```

**INFO (not counted)**
- `supply-chain/pyjwt`: the 2.14.0 the image carried had two OSV advisories, neither reachable (P3); this diff pins 2.15.1, which has none.
- `supply-chain/python-jose-3.3.0`: still installed, no longer imported by OSS code; its removal waits on the private module.

## Hypotheses

(daily mode — none retained)

## Coverage gaps

- OSV over the installed tree of the built backend image — not run (the per-pin OSV above covers the two libraries this diff touches). The `.pth` census WAS run on a backend image built from this branch before the pin bump: **0 `.pth` files** in `site-packages`; that image carried `pyjwt 2.14.0` and `python-jose 3.3.0`. No image was rebuilt after the bump to 2.15.1 (stack-level verification is left to CI).
- gitleaks over full history / zizmor: not run (diff mode; P2 grep ran over the diff, P4 has no overlap).
- The private module's decode path and its test: private repo, not in this diff.

## Trend

Prior reports on this surface: `cso-2026-09-29` and `cso-2026-09-30` Finding 8 named python-jose 3.3.0 among the pinned-stack advisories and judged it "not reachable with HS256 symmetric decode, cheap to fix" — this diff is the fix for the python-jose part; the reachability verdict is confirmed with the OSV alias dedupe (the two 3.5.0 ids are one CVE). Resolved on this surface: 0 (the pin itself stays until the private half moves). New: 0. Persistent: 0.
