# CSO Diff Audit — 2026-09-30 — abilityai/trinity-enterprise#672 (deprecated library skills)

**Mode**: diff (all phases) · **Confidence gate**: 8/10 · **Branch**: `feature/672-skill-deprecation-lifecycle` → `dev` · **Skill**: cso v1.2 · **Audited**: the branch diff against merge-base `863240f32` (commit `5051e2da3`, plus the test added during this audit).

## Architecture (Phase 0)

A skills library can retire a skill with two frontmatter keys, `deprecated` and `superseded-by`. The change carries them from the library to every surface that lists or assigns a skill:

- `services/skill_packaging.extract_contract` reads both keys through the existing hardened frontmatter loader.
  - `deprecated` must be a real boolean.
  - `superseded_by` is the author's text. It is reduced to one printable line of at most 200 characters (`_one_line` drops control and format characters) and is None unless the skill is deprecated.
  - A malformed value produces a named `frontmatter_invalid:*` warning.
- `deprecation_warning()` is the single producer of the machine code `deprecated[:<name>]`. It adds a successor only when the text passes `validate_skill_name`.
- `skill_service` copies the fields into the listing entry and appends the code to each injection result. `deliver_assigned` stamps the code on the assign response from a library read that runs beside delivery.
- The code reaches the start endpoint's public projection through a widened `_WARNING_RE`.
- REST `GET /api/skills/library` names both fields in its explicit `SkillInfo`, and MCP `list_skills` names them in its field map.
- The Vue surfaces render a badge, a "Superseded by …" line and a warning note, all by interpolation.

Trust boundary: text written by a library author (a synced git repository, semi-trusted) reaches three audiences:

- human operators, through the UI;
- any authenticated caller, including agent keys, through the listing route and MCP `list_skills`;
- agents, through the per-skill warning code in injection results and start responses.

## Stack CVE quicklist

Not re-evaluated. No pin, lockfile, Dockerfile, compose file or workflow is in the diff, so every row is unchanged since the last full run.

## Attack surface (Phase 1, diff-scoped)

| Surface | Change |
|---|---|
| Endpoints | None new. `GET /api/skills/library` gains two fields. `GET /api/skills/library/{name}` (human-only, ent#139) gains the same two. `POST`/`PUT /api/agents/{name}/skills…` and `POST /skill-sets/{set}` add `delivery.skills[name].warnings`, but only for a deprecated name. The start endpoint's `skills_result` may now carry `deprecated:<name>`. |
| Auth dependencies | None added, removed or changed (no `@router`, `Depends`, `require_*`, `reject_*` or `assert_*` line in the diff). |
| MCP tools | `list_skills` maps two more fields. Static description text changes on `list_skills`, `assign_skill_to_agent` and `get_agent_skills`, with no per-agent metadata. The tool set and scopes are unchanged. |
| Agent-context ingress | No new ingress class. `list_skills` already returns library-authored text (`description`, uncapped); `superseded_by` adds a narrower field to the same channel. The agent's CLAUDE.md is deliberately not annotated. |
| Frontend | Three badge sites, three successor-line sites and two notes, all interpolated. No `v-html`, `innerHTML` or bound `href` is added. |
| Migrations, dependencies, CI, Docker/compose, vendored policy files, `.claude/` | Unchanged. |

## Findings (Phases 2–12)

**None.** Every phase ran against the diff:

- **Phase 2, secrets.** Lines added in `git log -p 863240f32..HEAD` carry no known secret prefix (`AKIA`, `sk-`, `gh*_`, `github_pat_`, `xox*-`, `re_`, private keys, userinfo URLs), no private IP, and no email outside the commit trailer.
  - The enterprise-docs guard pattern matches nothing in the added docs.
  - Test fixtures are synthetic. The private repository named in two live library values does not appear in the diff.
- **Phases 3–5, supply chain / CI / infra.** No dependency, lockfile, workflow, Dockerfile, compose file, vendored policy mirror or migration is touched.
- **Phase 6, integrations.** No webhook, channel, A2A or backend→agent call path is touched.
- **Phase 7, LLM / agentic.**
  - **ASI01 / LLM01:** the new author text reaches an agent only as a ≤200-character JSON field in a tool result, next to the existing uncapped `description`.
  - **ASI04:** frontmatter parsing still goes through `utils.safe_yaml` (`parse_frontmatter` is unchanged).
  - **ASI09:** operators see the text as characters only.
  - **LLM05:** no HTML sink is added.
  - **ASI03:** the admin-gate, human-only-route and auth-wiring guards pass (below).
  - **ASI06:** nothing is persisted.
  - **ASI08:** no dispatch path is touched.
- **Phase 8, agent-shipped supply chain.** What is injected into agents is unchanged: the package, the manifest, and the CLAUDE.md section (pinned by `test_the_agents_claude_md_line_is_not_annotated`). The concealed-instruction grep (tag block, zero-width, bidi) over all 24 files in the diff has 0 hits. No injection or auditor-addressed strings.
- **Phase 9, OWASP 2025.**
  - **A01:** no route or dependency change. `get_skill` stays behind `reject_agent_principal`; the listing was already open to agent keys and gains non-sensitive, library-derived fields (not instance state, not URL-bearing config — the ent#334 class does not apply).
  - **A05:** no SQL, subprocess, YAML load or template construction added. `_WARNING_RE` is a linear character class, and its detail is a `validate_skill_name`-shaped token by construction. Only a name without `:` enters a `kind:detail` code, so no code can be smuggled through it.
  - **A10:** the one new broad `except` wraps an informational lookup, not a guard. Its fail-open direction is stated in the docstring ("never fails a committed assign"). A malformed `deprecated` reads as *not* deprecated plus a named warning, which is security-neutral.
- **Phase 10, STRIDE (skills listing / assign surface).** No change to spoofing, repudiation or elevation. Tampering: a library author already controls the skill's instructions, so the new field adds no capability. Information disclosure: public library content only. DoS: the added library read is bounded by the existing git timeouts and runs beside delivery, not in front of it.
- **Phase 11, data.** `deprecated` and `superseded_by` are PUBLIC or INTERNAL: library content, derived on every parse and never stored.

### Candidates considered and not reported

1. **The new log line interpolates `{e}` from `list_skills()`** (`skill_service.py`, `_deprecation_notes`). The ent#292 / #615 credential-to-log class. The read path runs only `git rev-parse HEAD` and `git ls-tree …` with `cwd` set, and no URL or token in argv (`skill_source_clone.py` `_resolve`, `tree_shas`, `_git`). `_clones()` catches and logs its own DB and constructor errors, so a `TimeoutExpired` or `OSError` string cannot carry the source URL. Below the gate; hardening (`_scrub_pat(str(e))`) left to the owner.
2. **A name-shaped successor that is not a skill** (`TBD`, or a dotted name such as `evil.example.com`) enters the code as `deprecated:<text>` and is rendered as text. It is not linkified on any surface, and a library author already controls the skill body. Informational.
3. **`description` is not put through `_one_line`.** It can still carry bidi or format characters on the same surfaces. Pre-existing and out of this diff.
4. **Author text in an agent's tool result** (MCP `list_skills`). This is the existing library-content channel, narrowed for the new field, and equal in trust to the skill bodies an assigned agent already runs.

## Active verification (Phase 12)

- **Challenged controls, green:** `test_2996_human_only_routes.py`, `test_1310_auth_wiring.py`, `test_293_admin_gate_rejects_agent_keys.py`, `test_models_centralized.py`, `test_186_enumeration_uniformity.py`, `test_1965_agent_server_safe_yaml.py`, `test_2991_start_skills_result.py` — 159 passed.
- **Rendering probe, added during this audit:** `skillDeprecation.spec.js` › "renders an HTML-shaped successor as text, never as markup". It mounts the Library card with `superseded_by = <img src=x onerror=…>` and asserts the literal text, no `img` element, and no script effect. Mutating the successor line to `v-html` turns it red. The component was restored byte-identical.
- **Sanitiser:** `test_successor_text_is_one_printable_bounded_line`. RLO, ZWSP and ESC are dropped, whitespace is collapsed, and the text is cut at 200. The fixture spells the characters as escapes.
- **Branch mutation battery** (from `/review`): 39 wiring lines, all red.

## Hypotheses

None (daily mode).

## Coverage gaps

None for the diff scope. Out of scope by mode: the Phase 0 quicklist and the Phase 3–6 scanners, which have no files in the diff to scan.

## Trend

This is the first audit of these files for this issue. There is no prior finding on `skill_packaging.py`, the listing route or the Library and Skills-tab components to close. No new findings.
