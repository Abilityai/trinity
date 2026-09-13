# Feature Flow: Workspace chat — the model dropdown (trinity-enterprise#403)

## Overview

The Workspace composer had no model control. Every turn ran on whatever the agent
defaulted to, and the person in the conversation could neither see it nor change it.
This adds a **short, curated, plain-language** dropdown for **platform users**, with a
three-state resolution, the chosen model reaching the turn, and the model **stamped on the
execution row**.

A client-facing dropdown is not the operator combobox. `ModelSelector.vue` is a preset list
plus free-text entry over raw model ids (`claude-sonnet-4-6`, …) — right for an operator
panel, wrong in front of a client. That difference is what most of the decisions below are
about.

**OSS-core, deliberately ungated** (the standing Workspace ruling, ent#356). No table, no
migration, no Alembic revision, no entitlement gate.

Related: [public-channel-model.md](public-channel-model.md) (#894, the middle rung),
[model-selection.md](model-selection.md) (the operator surfaces),
[workspace-absorbs-session.md](workspace-absorbs-session.md) (the turn engine),
[workspace-roster-briefing.md](workspace-roster-briefing.md) (the payload this rides on).

## User Story

> As someone working with an agent in the Workspace, I want to pick how capable or how fast
> this chat should be, in words I understand, without being shown a list of raw model ids —
> and I want the default to plainly read as *the agent's own* choice rather than something
> I selected.

---

## Entry Points

| Surface | Where | What it does |
|---|---|---|
| UI | `PortalConversation.vue` — inside the composer shell, right-aligned on the control row beside Send (#2662) | the `BaseSelect` on `variant="ghost"`; renders only when the roster says this principal may choose |
| API (read) | `GET /api/enterprise/client-portal/my-agents` | carries `model_options` (instance-level) and each card's `model_default` — the capability channel (#2128) |
| API (turn) | `POST /api/enterprise/client-portal/agents/{name}/chat` and `.../chat/stream` | both accept `model`; both validate it before anything else. `/chat` is also ent#83's headless surface and the browser's fallback when streaming fails, which is why a check on one route only is a gap that opens exactly when the other is in use |
| API (write) | `PUT /api/users/me/preferences/workspace_model` | the user's server record — not a new endpoint, one line in `PREFERENCE_KEYS` |

## The three states

```
  explicit choice   →   the agent's public_channel_model (#894)   →   the platform default
   (the dropdown)          (set by the owner, Sharing tab)            (#831, execute_task)
```

`None` means **inherit**, all the way down — the shape the #894 plumbing already uses
(`routers/agent_config.py` writes `None` for "unset"). Collapsing it to two states would
make "the user chose the platform default" indistinguishable from "nobody chose anything".

**AC 5 is settled as *inherit*, never a third model source.** The issue's whole point is
not leaving two sources silently disagreeing about one agent's model.

---

## Curation — a policy dimension on the ONE catalog

`services/model_catalog.py` gains two fields on `ModelEntry`, **appended last and set by
keyword**:

| field | meaning |
|---|---|
| `workspace` | offered in the Workspace composer |
| `workspace_tier` | the plain-language primary text the option renders |

⚠️ `ModelEntry` is a **positional frozen dataclass** — all ten entries pass their three
booleans positionally. A field inserted anywhere but last silently reassigns
`public_channel` / `admin_default_selectable` / `recommended` on every entry, with no error.

The curated set (three):

| id | tier | why |
|---|---|---|
| `claude-opus-5` | Most capable | the top tier |
| `claude-sonnet-5` | Balanced — fast and smart | the everyday choice |
| `claude-haiku-4-5-20251001` | Fastest | the cheap/quick tier |

`claude-fable-5` is out: its `note` also reads "Most capable", and Opus-vs-Fable is an
operator distinction, not a client one. Every "Legacy" entry is out — a client-facing
surface offering "Claude Opus 4.6 — Legacy" *is* the combobox this reacts against.

**Why `workspace_tier` and not the existing `note`.** Joining `label — note` yields two
options both leading with "Most capable", and `Claude Fable 5 — Most capable — longest
tasks (latest)` — an em-dash nested inside the em-dash doing the joining. `note` is copy
written for the operator picker. A new policy dimension on the same source is exactly the
`admin_default_selectable`-vs-`recommended` precedent; a second hand-typed list is the
drift #2086 exists to prevent.

Two **import-time assertions**, both load-bearing:

```python
assert all(m.public_channel for m in MODEL_CATALOG if m.workspace)   # ⊆ the #894 allow-list
assert all(m.workspace_tier for m in MODEL_CATALOG if m.workspace)   # or the option is blank
```

The first is the subset rule: the Workspace must never accept a model the #894 operator
route would 422, because the value it accepts is resolved through the same ladder that
route writes. `WORKSPACE_MODELS` is the derived frozenset.

The generated mirror `src/frontend/src/constants/modelCatalog.js` is regenerated by
`python scripts/gen_model_catalog.py`; `tests/unit/test_2086_model_catalog_parity.py`
hard-codes the emitted key set and asserts equality, so a new field is a two-file change by
construction.

---

## The payload — the roster is the capability channel

`GET /api/enterprise/client-portal/my-agents`:

```jsonc
{
  "agents": [
    { "name": "scout",
      "model_default": { "model": "claude-haiku-4-5-20251001",
                         "label": "Claude Haiku 4.5",
                         "source": "agent" } }      // "agent" | "platform"
  ],
  "model_options": [                                 // INSTANCE-level, not per card
    { "id": "claude-opus-5", "tier": "Most capable", "label": "Claude Opus 5" }
  ]
}
```

**Options on the roster, the default on the card.** `realtime_voice` and
`multi_agent_chat_available` sit on `PortalRoster` precisely because they are
instance-level. The option list is identical for every agent; putting it on each card would
ship N copies on exactly the path #2159/#2163 exist to keep small — and it means
`get_agent_card` needs no payload change at all.

**Both fail closed.** `model_default: null` renders no control, and that is the value for:

- every **non-platform principal** (the Workspace model choice is platform-users-only), and
- a **non-Claude runtime** — the platform passes no `--model` to the Codex runtime at all,
  so a Claude-model list there promises something and changes nothing.

An older client, a partial payload, an empty option list or a failed read all land on the
same "no control" result. This is the opposite direction to the `availability` field on the
same card (#2196), and that asymmetry is deliberate: `availability`'s bug is *denying a
working agent*, this one's is *promising an affordance that cannot work*.

**#2128 — the gate has to be here.** The roster payload is the only capability channel an
external client has; `GET /api/settings/feature-flags` is `get_current_user`-gated and
returns empty for every external client, so a UI gate written against it is dead for
exactly the audience it would be protecting.

### How it is resolved without new queries

- Both roster queries (`client_portal/db.py::get_shared_roster` / `::get_owned_roster`)
  already join `agent_ownership`, so `public_channel_model` is **one more column**, not a
  per-card read (the #2159 rationale, verbatim).
- The instance-level facts — the option list, the platform default, its label — are
  resolved **once per roster load** in `get_roster`, beside `tts_ready` and the default
  voice, and threaded into `_row_to_card` as `model_context`.
- `_row_to_card` gains `is_platform`, `runtime` and `model_context` as **keyword-only with
  no default**. A default would let the agent-page call site keep compiling while silently
  serving the wrong card; the safe value differs per call site.
- The runtime comes from `docker_service.agent_container_runtimes()` — a **second** sparse
  `containers.list()`, O(1) in fleet size rather than the inspect-per-agent #2160 forbids.
  It is a separate leaf rather than a widening of `agent_container_states()` so #2196's
  guard suite keeps pinning what it pins, and it is awaited **sequentially** rather than
  with `asyncio.gather`: `test_2163_roster_latency_floor.py` pins that `get_roster`
  contains no fan-out at all. Two fixed O(1) reads are not the N-agent fan-out #2163
  closed — but that guard is blanket on purpose, and loosening a guard to admit one's own
  change is how the property it protects stops being true. The trade is one extra
  `/containers/json` (~50-200ms) once per roster load, for a capability gate that does not
  offer a Claude-model list to a Codex agent.

> **The sparse trap, in its other form.** Under `sparse=True` docker-py's
> `container.labels` **raises** — it reads `attrs["Config"]["Labels"]`, which only a full
> inspect populates. The runtime is therefore read from `attrs["Labels"]`, the key the
> `/containers/json` summary actually carries. Reading `.labels` would raise on every
> container, be swallowed, and return "no runtimes" forever: safe, silent, permanently
> wrong. An unreadable runtime falls back to `claude-code`, matching `get_agent_runtime`'s
> own documented posture.

**A stale override degrades identically on the card and at turn time.** `_row_to_card` runs
`settings_service.is_valid_public_channel_model` over the raw column, exactly as
`db.get_public_channel_model` does (#1080) — otherwise the label and the turn would
disagree about one stale value.

**The label never crashes and never renders blank.** `platform_default_model` is written
through the generic `PUT /api/settings/{key}` with **no** catalog check, and free-text ids
like `claude-sonnet-4-6[1m]` are in legitimate circulation, so the resolver is
`catalog_label(id) or id` — a bare lookup would 500 the roster, this surface's front door.

---

## The turn

```
  Workspace composer (platform user, Claude runtime, control rendered)
        │  BaseSelect:  "" = Agent's default (Sonnet 4.6) | <curated id>
        ▼
  POST /client-portal/agents/{n}/chat[/stream]        body.model
        │  ① (body.model or "").strip() or None   ← BEFORE validation, or "" 422s
        │  ② may this principal choose?   no ──► 403
        │  ③ id ∈ WORKSPACE_MODELS        no ──► 422, STRING detail
        │  ④ the requested model joins the idempotency scope
        ▼
  start_portal_turn                        portal_chat  (sync / ent#83)
        │  resolve ONCE, immediately after the availability gate:
        │     requested ─or─ public_channel_model ─or─ THE PLATFORM DEFAULT (concrete)
        ├────────────► create_task_execution(model_used=resolved)
        └────────────► portal_chat(model=<requested>, resolved_model=<trusted>)
                             ├─► _precreate_sync_execution(model_used=resolved)
                             └─► run_resumable_turn(model=resolved)
                                     ├─► execute_task  (non-None ⇒ no default lookup)
                                     └─► cold retry: a NEW row, the SAME model
```

### Why the resolution happens at that exact line

`schedule_executions.model_used` is written **only at row creation** — `execute_task`
persists it inside `if not execution_id:` — and there is **no UPDATE path for that column
anywhere in the repo**. Both portal turn paths pre-create the row. So the model reaching
the turn as a kwarg is not enough; it has to reach the two `create_task_execution` calls.

This is also why the ladder's last rung is the **platform default as a concrete id** rather
than `None` (review, 2026-09-08). `None` meant "let `execute_task` resolve it" — which it
does, at `task_execution_service.py:1044` — but its paired `model_used=model` write sits
inside the same `if not execution_id:`, so on a portal turn the resolution happens and the
stamp never does. The default state of every agent (no pick, no `public_channel_model`) then
recorded NULL while the turn ran on the platform default. The rung reads
`settings_service.get_platform_default_model()`, the same function `execute_task` calls, so
the row and the turn hold one opinion; `execute_task`'s own lookup becomes a no-op.

`_precreate_sync_execution` runs ~200 lines into `portal_chat`, after `_persist_user_turn`,
the history read, the inbox collection and the system-prompt build. Resolving where the
value is *used* — beside the turn kwargs — would stamp that pre-created row `None` again
and half-fix the requirement on exactly the path **#2426** already burned. It is resolved
immediately after the availability gate, into one local, and threaded to both.

**`resolved_model` is required whenever `execution_id` is passed.** The tempting shape,
`resolved_model or resolve_turn_model(...)`, would let a caller that already stamped a row
silently re-resolve — and the override can change between the two reads, so the row and the
turn would disagree about one turn. That path raises instead; no request can reach it.

The requested value cannot simply be re-laundered through the composer's allow-list either:
an inherited `public_channel_model` may legitimately sit **outside** the curated set
(`claude-opus-4-7` is public-channel-selectable but not workspace-selectable).

### One turn, two rows

`run_resumable_turn`'s cold retry strips `execution_id` from its kwargs and re-runs, so
`execute_task` **creates a second row** and stamps it from the same forwarded `model`. The
two rows must agree — pinned by a test, because the second row is the one that answered.

### `triggered_by` is unchanged

Every portal turn still dispatches as `triggered_by="public"` on both paths. That field has
other consumers (the "Public" analytics bucket, `INLINE_CHANNEL_TRIGGERS`), and it is
asserted in the tests.

### The idempotency scope (Invariant #18)

`scope = f"portal_stream:{agent_name}:{email}:{requested_model or '-'}"`. Without the model
term, a client re-sending the same `Idempotency-Key` with a *different* model would be
handed the previous turn's snapshot — a silent replay that ignores the change the user just
made. The **requested** value and not the resolved one, deliberately: an owner editing
`public_channel_model` between two genuine retries of one request must not fork the scope
and turn a replay into a second billed turn. Validation runs **before** the scope is built,
so a refused model never consumes the key.

---

## Persistence — the user's server record, not the browser

`"workspace_model"` is **one line** in `services/user_preferences_service.PREFERENCE_KEYS`
— never a new table, which is what that module's own docstring says. Value shape
`{"<agent_name>": "<model-id>"}`, read and written through the existing
`stores/userPreferences.js` engine (debounce, compare-and-set, 409 adoption, identity-change
queue drop). Routes: `GET/PUT/DELETE /api/users/me/preferences[/{key}]`, gated
`get_current_user` + `reject_non_interactive_principal` — exactly and only this control's
audience.

It is per **(user, agent)** by construction: the server knows who is asking, so "never
leaks between agents or between clients" is a property of the storage rather than of a key
we have to get right.

> **Why not localStorage.** `composables/useColumnResize.js` records the trap, caught live:
> the portal store's `clientEmail` starts `null` and is filled from a network response, so
> a browser key built on it reads `anon` on every reload and writes under the email a
> moment later. The server record dissolves the problem rather than working around it.
> `stores/fleetGrid.js` moved the same way (ent#413).

**Known wrinkle, stated rather than discovered:** the record arrives asynchronously, so the
select can show "Agent's default" for one frame before adopting the stored value. Unlike a
column width this causes no layout jank, and **no turn can run on the wrong model** —
nothing is sent until Send. If the frame ever proves objectionable, the fix is to await the
preference load with the roster, not to move to browser storage.

**Self-healing.** The stored value is a raw model id and the catalog churns by design
(three entries already carry a "Legacy" relabel). On load, an id absent from `model_options`
is **dropped** and the control falls back to inherit — mirroring what the backend already
does for a retired `public_channel_model`. Without it, a retired id is sent on every turn,
refused every time, forever, across reloads, with nothing pointing at the stored preference
as the cause.

---

## Error Handling — degradation that is honest, and never a lie about billing

| case | answer |
|---|---|
| `""` / whitespace / omitted | → `None` = inherit (normalised **before** validation) |
| not in `WORKSPACE_MODELS` | 422, **string** detail naming the model |
| a `model` from a principal with no control | 403 — the grant gate, not the UI gate |
| stored `public_channel_model` no longer valid | degrades to the platform default, on the card AND at turn time |
| `platform_default_model` off-catalog | label degrades to the raw id; the roster still builds |
| stored preference no longer offered | dropped → inherit |
| `model_default` absent / no options / non-Claude runtime | no control renders |
| the turn fails generically **and** a model was chosen | names the model, category `invalid_model`, **clears the stored choice** |
| the turn fails with `AUTH` / `BILLING` / `CAPACITY` / `TIMEOUT` | **untouched** |

**Blank is inherit, and it is normalised first.** The control's default option has value
`""`, so `(body.model or "").strip() or None` runs before the allow-list check — copying
the #894 PUT's own idiom. Without it the validator refuses `""` and **every default turn
422s on day one**.

**A string detail, not a dict.** `components/portal/portalUtils.js::deliveryFailureReason`
returns `detail` only when it is a **string**; anything else degrades to *"The message
wasn't delivered (error 422)."* A structured body would drop the model name, the reason and
the remedy on the one surface this message exists for.

**An unservable model is NOT reclassified.** `_PULL_ERROR_CODES` has **no model member** —
there is no error code meaning "this model is unavailable" — and `client_portal/service.py`
merges `AUTH`/`BILLING` into one *"reached its usage limit"* answer with a true and
specific cause. Rewording that branch whenever a model was chosen would blame the model for
an exhausted subscription and send the person round the dropdown looking for one that
works. So only the **generic `agent_error`** branch names the model:

> The agent couldn't complete this on Claude Opus 5. Switched back to the agent's default —
> try again, or pick another model.

…under a **ninth** failure category, `invalid_model`. It is a new token rather than a reuse
because it is the one category the client *branches* on rather than merely renders: it
clears the stored preference, so the sentence is **true** on the next turn instead of
looping the person into the same failure on every retry. The clear runs on the settle of a
turn **this tab actually sent**, and deliberately NOT on the load/reattach path (review,
2026-09-08). `rememberVerdict` fires on load too, off the durable Redis verdict — 15 minutes
of TTL, cleared only at the next dispatch or on a success — so clearing there re-fired on
every reload inside that window: a user who re-picked after the failure had the fresh choice
wiped again on the next refresh, and written through to the server for every device. A
re-send is the only way the loop can recur, and a re-send settles through the arm that is
kept.

**The room transition.** `PortalConversation.vue` diverts an `@mention` send into
`escalate-to-room` and returns before the turn path, and `PortalRoom.vue` has its own
composer with no dropdown. The select is therefore **disabled with a reason** while the
draft is room-bound, rather than displaying a setting it is not honouring.
`'model-picker'` also joins `VOICE_LOCKED_CONTROLS`: during a voice call the conversation
runs on the voice provider's own model.

---

## Side Effects

| Effect | Where | Note |
|---|---|---|
| `schedule_executions.model_used` written | both pre-creation sites | **write-once at row creation** — there is no UPDATE path, which is why the ladder must resolve to a concrete id (see *The turn*) |
| The user's `workspace_model` record written | `PUT /api/users/me/preferences/workspace_model` | per (user, agent); a `gesture` origin, so it is the user's own change and not a reconciliation |
| The record CLEARED by the self-heal | on `settleDelivery` only | deliberately not on `rememberVerdict`, which also runs on load/reattach off the durable Redis verdict (15-min TTL) — clearing there wiped a re-picked model on every refresh inside that window, for every device |
| Idempotency key scope carries the **requested** model | both turn routes | a different pick must not replay the previous turn's snapshot; the *requested* and not the resolved value, so an owner editing `public_channel_model` between two genuine retries of one request does not fork the scope |
| No new audit event, no new WS broadcast | — | the turn's own activity and events are unchanged; `triggered_by` stays `"public"` |
| One extra `/containers/json` per roster load | `_runtime_map` | O(1) in fleet size, not the N+1 #2160 forbids. Registered as known debt — it duplicates `agent_container_states`' call |

## Security Considerations

- **New attack surface:** one request field, `PortalChatRequest.model`.
- **The closed allow-list is the security control, not a nicety.** The value reaches the
  agent as `cmd.extend(["--model", model])` (`headless_executor.py`, `claude_code.py`,
  `gemini_runtime.py`). It is an argv list with no `shell=True`, so there is no
  shell-injection path — but an arbitrary string as a CLI value is argv/flag-smuggling
  surface against the agent runtime, and the Workspace sends **no** model today, so this
  field *creates* that surface. Validation is against a closed set — never a regex, never a
  prefix check.
- **Auth.** `PortalPrincipal.is_platform` is `False` for a portal-session token and `True`
  for a platform JWT. ent#163 `/auth/exchange` mints a *portal session*, so a delegated
  principal is `is_platform=False` and the gate is not bypassable there. The payload gate is
  cosmetic; **the router gate is the control**, and it is on **both** turn routes —
  `POST .../chat` runs an inline path and is ent#83's documented headless surface as well as
  the browser's fallback when streaming fails.
- **Authorization breadth — accepted deliberately, not overlooked (operator, 2026-09-07).**
  There is **no ownership check**. `is_platform` includes ordinary platform users and
  user-scoped API keys, and `_roster_rows` unions agents merely *shared with* the caller —
  so a rostered non-owner can pin every turn to Opus and beat the owner's deliberate Haiku
  setting, while #894 itself is owner-gated (`assert_agent_owner`) precisely because the
  model is a cost decision. Raised by three reviewers and knowingly accepted: the roster
  gate is the control and the execution row's `model_used` is the after-the-fact audit.
  **The smallest reversal is one `assert_agent_owner` call at each of the two router entry
  points — no payload and no UI change.**
- **Cost.** The existing rate limits bound request *count*, not spend. No new limiter;
  recorded rather than hidden.
- No credential material touches this path, and no new audit event: the execution row *is*
  the record (the #894 PUT audits a config *change*; a per-turn choice is not one).

---

## Deliberate behaviour change

**The #894 rung applies to every portal turn, not only a platform user's.** The issue calls
its absence the defect: the override "is **not** consulted on the Workspace path, even
though a Workspace turn dispatches as `triggered_by="public"`". Applying it only for
platform principals would leave the streaming route and the synchronous ent#83 route
resolving differently — a brand-new "two sources silently disagree", which is the thing AC 5
exists to kill. `resolve_turn_model` therefore takes **no principal at all**, which is what
makes the two routes agreeing true by construction rather than by review.

So **on deploy the model changes for existing external-client conversations** wherever an
owner set `public_channel_model`. Pinned by a test that a non-platform principal resolves
the same ladder, and it belongs in the release note.

---

## Backend Layer

| File | Change |
|---|---|
| `services/model_catalog.py` | `workspace` + `workspace_tier` appended last; three entries flagged; two `_JS_KEY_MAP` rows; two assertions; `WORKSPACE_MODELS` |
| `src/frontend/src/constants/modelCatalog.js` | **regenerated** (`scripts/gen_model_catalog.py`) — never hand-edited |
| `services/user_preferences_service.py` | `+"workspace_model"` in `PREFERENCE_KEYS` |
| `services/docker_service.py` | `+agent_container_runtimes()` — the batch, sparse runtime read |
| `services/docker_utils.py` | `+agent_container_runtimes_async()`, `+agent_runtime_async()` |
| `client_portal/db.py` | `+agent_ownership.c.public_channel_model` on both roster queries |
| `client_portal/models.py` | `+PortalModelOption`, `+PortalModelDefault`; `+model_options`; `+model_default`; `+PortalChatRequest.model` |
| `client_portal/service.py` | `_model_context` / `workspace_model_options` / `catalog_label` / `_card_model_default` / `_runtime_map` / `_agent_runtime`; `_row_to_card(*, is_platform, runtime, model_context)`; `resolve_turn_model`; `validate_requested_model`; `model_used=` at **both** creation sites; `model=` in the turn kwargs; the `invalid_model` branch; `+"invalid_model"` in `PORTAL_FAILURE_CATEGORIES` |
| `client_portal/router.py` | validation on both turn routes; the model in the idempotency scope; threading |

`services/session_turn_service.py` is **not touched** — `run_resumable_turn` already
forwards `model` through `**execute_kwargs` on the initial call and on the cold retry.

## Frontend Layer

| File | Change |
|---|---|
| `components/portal/portalModelChoice.js` | **NEW** — the pure rules (`modelControlState`, `reconcileStored`, `storedFor`, `withChoice`, `optionText`, `optionTitle`, `defaultOptionText`, `shouldClearChoice`). No storage access |
| `components/portal/PortalConversation.vue` | `BaseSelect variant="ghost"` on the composer shell's control row, right-aligned beside Send (#2662 — ent#403 shipped it on its own row above the composer because the single-row layout had 34px left to give it); value from `userPreferences`; `model` on both send paths; disabled for a voice call and for a room-bound draft; the self-heal on settle and on reattach |
| `components/base/BaseSelect.vue` · `components/base/fieldClasses.js` | `variant="ghost"` (#2662) — the borderless, content-width, 44px recipe (`FIELD_GHOST_CLASS`) this control wears in chat chrome. A variant on the primitive, not a hand-rolled lookalike; the native `<select>` is kept for keyboard, focus ring and the platform picker on touch |
| `components/portal/portalVoiceMode.js` | `+'model-picker'` in `VOICE_LOCKED_CONTROLS` |
| `stores/clientPortal.js` | `modelOptions` (fail-closed, cleared with the session); `model` on `sendPortalChat` + `startPortalChat` |
| `utils/gridStorageKeys.js` | `+workspaceModel` in `PREF_KEYS` — the frontend half of the server allowlist |

---

## Testing

| Suite | What it pins |
|---|---|
| `tests/unit/test_ent403_workspace_model.py` | the ladder (incl. that it cannot see the principal); the four blank forms; the 403 and the string-detail 422; argv-shaped strings against the closed set; `model_used` at **both** creation sites; the cold-retry row; `triggered_by` unchanged; the card's fail-closed cases; the off-catalog label; `AUTH`/`BILLING`/`CAPACITY`/`TIMEOUT` copy unchanged; the idempotency scope |
| `tests/unit/test_2086_model_catalog_parity.py` | the emitted key set; `WORKSPACE_MODELS ⊆ PUBLIC_CHANNEL_MODELS`; every workspace entry has a distinct tier |
| `src/frontend/tests/unit/portalModelChoice.spec.js` | the pure rules; the curated set against the generated catalog; source-structure assertions for the wiring |
| `src/frontend/e2e/workspace-model-choice.spec.js` | **rendered geometry against a live stack** (#2662): the picker inside the composer shell, below the field, right of every icon button and left of Send, vertically centred with it; no border or fill of its own; the field ≥ 90% of the form; every action box 44px — **the picker's own box included**, measured rather than inferred from its class; and a click on the shell's padding band putting the caret in the field. Run at 375 / 768 / 1280. **Every box comes from ONE `page.evaluate` (#2705)** — the locators choose the elements, the page returns the geometry in one frame — under `page.emulateMedia({ reducedMotion: 'reduce' })`, and the shell click is `locator.click({ position })`: reading the boxes one round-trip at a time let the rail column's 300 ms entry (#2676) land between two reads, the stale `send.x` made the Send-skip miss Send itself, and the spec failed 3 of 3 attempts at 768 and 1280 in some CI stacks while 375 passed only because the column is hidden below `sm` |
| `src/frontend/tests/unit/portalComposerAlignment.spec.js` | the one-shell structure across **both** composers (`PortalConversation.vue`, `PortalRoom.vue`): the shell carries the chrome, the textarea is transparent and borderless, the focus ring is scoped `has-[textarea:focus]` and never `focus-within`, and `items-end` does not return; plus the ghost recipe's **cascade shape** — `h-11`, no `w-full`, the border colour on the valid/invalid arms rather than in the base string, and every dark hover tint paired with a dark disabled reset |
| `src/frontend/tests/unit/portalVoiceMode.spec.js` | the composer's call state (#2662): the shell sheds its border and fill for the call's duration rather than dimming — it is the parent of both inert regions and holds the live toggle — and **both** arms are bound, with no chrome colour in the static class |
| `src/frontend/tests/unit/baseSelectChevron.spec.js` | the picker's open-state chevron (#2662): the `:open` sibling rule, the stricter select-then-chevron adjacency it asserts on purpose (`~` is the GENERAL sibling, so an element between the two is harmless — what kills the flip silently is wrapping the svg or moving it above the select), the reduced-motion guard, and that the flip stays on the `ghost` recipe rather than the shared `<select>` — plus a control proving the suite's comment-stripper is not the identity |

The runtime read the capability gate is *driven by* has its own block in the first file:
`agent_container_runtimes` reading `attrs["Labels"]` under `sparse=True` (docker-py's
`.labels` **raises** there, so a `.labels` rewrite would be swallowed by the leaf's own
`except` and answer `None` forever — the control silently returns on every Codex agent),
keyed by container name and not by the stale `trinity.agent-name` label, `{}` vs `None`,
a label-less legacy container reading `claude-code`, and `_runtime_map`'s
narrow/validate/never-raise guards. Twin of
`test_2196_roster_availability.py::test_the_batch_read_uses_the_sparse_attrs_shape`.

**Status: ⚠️ — verified as far as this machine can go, and no further.**

Proven on a live sibling stack (real Docker, real database, the real routes): the runtime
gate against a live daemon (a `codex` agent gets `model_default: null`, a `claude-code` one
gets a populated card); **all three ladder rungs stamping the real execution row** — an
explicit pick, `model: ""` resolving to the platform default rather than NULL, and the #894
override reaching the row *including for an external portal-token client*; the 422's string
detail bounded at 64; and a portal client's hand-crafted `model` refused **403**, not
ignored.

**Verification honesty.** `ANTHROPIC_API_KEY` is present but empty locally, so a
locally-created agent cannot execute a turn: the suites prove the value is threaded,
validated and stamped — **not** that the agent ran on it.

Rendered geometry **is** machine-verified as of #2662, which is a change from this flow's
original posture. vitest still runs `environment: 'node'` with no mount harness, so the
unit specs remain source-structure assertions — but `e2e/workspace-model-choice.spec.js`
drives a real browser against a live sibling stack and measures actual boxes at three
widths (#2659: assert what rendered, not what the source says) — all from one frame, under
reduced motion, since #2705. What that covers: the
picker's placement relative to the field, to every icon button and to Send; that it carries
no border or fill of its own; the field's share of the form; the 44px action boxes. What is
still a **human** check: both themes, and the hover/focus tints — the e2e reads geometry and
computed border/background at rest, not the full colour ladder, and it drives only the states a
page load produces (no hover, no `error`, no `disabled` picker).

That gap is exactly where the review found two live cascade defects the whole green suite could
not see — the ghost select's error border and its dark-mode disabled hover, both fixed. They were
measured by compiling the **real** `fieldClasses.js` constants into a Tailwind sheet, rendering
one `<select>` per state in a headless Chromium, and reading `getComputedStyle` back in both
themes. That is the cheapest instrument that can tell "the classes were written" from "the classes
survived to the box", and until those states reach the e2e it stays the way to check them.

The **open-state chevron is the exception, and it is source-asserted only.** The flip rides
on `:open`, whose whole point is that the platform draws the picker — so no test in either
suite can open it and read the rotation back. `baseSelectChevron.spec.js` pins the class, the
sibling adjacency it depends on, the reduced-motion guard and the `ghost`-only scope; that
the chevron *visibly* turns was confirmed by hand in a real Chrome 151 and is a **human**
check on every future change. It also degrades silently by design: on an engine older than
Baseline 2026-05 the chevron simply stays put, which is correct behaviour and indistinguishable
from the rule having been broken.

**Deferred, stated:** `model_used` records the model *requested at dispatch*, not one
reconciled with what the agent actually ran — matching `execute_task`'s own semantics.
There is also no per-instance curation: narrowing or disabling the control is a code change,
not a setting.

---

## Related Flows

| Flow | Relationship |
|---|---|
| [public-channel-model.md](public-channel-model.md) (#894) | **Upstream.** Owns the per-agent override this flow's middle rung reads, and the `PUBLIC_CHANNEL_MODELS` set the curated set is a subset of. That flow's own doc records the deliberate change: the rung now applies to every portal turn, not only a platform user's. |
| [workspace-agents-at-the-centre.md](workspace-agents-at-the-centre.md) | **Upstream.** The roster this flow hangs `model_options` and each card's `model_default` on — the capability channel a portal principal actually has (#2128). |
| [workspace-chat-tabs-and-titles.md](workspace-chat-tabs-and-titles.md) | **Sibling.** The same composer and the same session; a chat tab does not change which model a turn runs on. |
| `model_catalog` (#2086) | **Upstream.** The ONE catalog. Curation is two appended fields on it, never a second hand-typed list; `src/frontend/src/constants/modelCatalog.js` is generated from it. |
| Workspace sequence — ent#547 (header), trinity#2581, #2582/ent#548 (Files tab) | **Siblings.** All touch `PortalConversation.vue`; merge order is fixed for that reason. This is step 3 of 7. |
