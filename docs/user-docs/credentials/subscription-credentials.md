# Subscription Credentials

Share Claude Max/Pro subscription tokens across multiple agents, with automatic assignment, live usage and headroom readings, early warnings before a weekly limit, and automatic switching when a subscription is rate-limited — so a turn completes on another subscription instead of failing.

> 📺 **Watch:** [Trinity Platform Demo](https://youtu.be/ivljtZqsxeo) *(May 2026)* · [all videos](../videos.md)

## Concepts

- **Subscription** -- A Claude Max or Pro subscription token (from `claude setup-token`, prefix `sk-ant-oat01-`) registered with Trinity. Stored encrypted (AES-256-GCM). Injected as an environment variable into assigned agents.
- **Auto-assign** -- New agents get a subscription automatically. Trinity skips any subscription that failed in the last 2 hours, then picks the one with the most cached headroom (furthest from whichever of its two limits is nearer). When no usable reading exists, it falls back to fewest-agents-first with an alphabetical tie-break.
- **Headroom** -- How much of a subscription's two Anthropic limits is already spent: the **5h** window and the **7d** window, each as a utilization percentage with a reset time. Trinity reads them from the provider by sending a minimal probe (~a dozen tokens of the subscription's own quota, visible in the Anthropic console). Both windows are fixed windows with a scheduled reset — consumption climbs inside the window and drops at the reset, so a percentage is not a rolling average.
- **Observed vs actual** -- Every usage reading carries its source. `actual (Anthropic)` means a provider probe answered and says how old the reading is; `observed (estimate from recorded consumption)` means Trinity is summing its own execution records because no fresh probe exists. Observed figures are always available; provider figures enrich them.
- **Failure event** -- One recorded refusal of a subscription by the provider, classified as **rate-limit** (quota) or **auth** (the token was rejected). The distinction matters everywhere: a dead token is never reported as a rate limit, and the provider's own "approaching the limit" warning tier is never reported as a limit either.
- **Auto-switch** -- When an agent's subscription refuses a turn, Trinity moves the agent to a different subscription and re-issues the turn once, so the turn completes. The new token is applied via a **hot-reload** of the running container — no recreate — so in-flight executions keep running. Default ON.
- **API-key fallback** -- When no subscription can serve a turn, Trinity moves the agent onto the platform API key instead of failing the message. Default ON; inert until a platform API key is configured.
- **Hot-reload rotation** -- Manual token changes hot-reload the same way: re-registering a subscription with a fresh token pushes the new token to every running agent on that subscription, and reassigning an agent from one subscription to another swaps the token in place. In-flight turns finish on the old token; the next turn uses the new one. Container recreation happens only for an auth-*mode* change — assigning a subscription to an agent that had none, or clearing one (the agent goes back to the API key).

## How It Works

### Registering a subscription

1. Run `claude setup-token` on your own machine to get a long-lived token (about a year).
2. In Trinity, open **Settings** → **Integrations** → **Claude Subscriptions**.
3. Under **Add Subscription**, enter a **Name**, choose a **Type** (**Claude Max** / **Claude Pro** / **Unknown**), and paste the **Token**. The field turns red until the token starts with `sk-ant-oat01-`.
4. Click **Register Subscription**. Registering a name that already exists replaces its token and hot-reloads every running agent on it.

The table lists each subscription's **Name**, **Type**, **Agents** count, **Pressure**, and **Created** date, with a **Delete** action. Deleting a subscription clears it from every assigned agent (the confirmation names how many). A running agent keeps its current token until it is next restarted, after which it uses the platform API key.

### Assigning agents

- **From Settings** — click a subscription row to expand it. **Assigned Agents** shows each agent as a chip with an **×** to remove it; pick an agent from **Select agent...** and click **Assign** to add one. Agents already on another subscription show `(on <name>)` in the list and move when assigned.
- **From the agent** — on Agent Detail, the auth badge in the header (amber with the subscription name, gray **API Key**, or red **No Auth**) is a dropdown for admins who own the agent: pick another subscription, or **API Key** to clear it.
- **Automatically** — every new agent is auto-assigned as described under Concepts. System agents are not.

Only the agent's owner or an admin can change an assignment. Registering, deleting, and reading usage are admin-only.

### Encryption Requirement

- `CREDENTIAL_ENCRYPTION_KEY` must be set in `.env`. This is auto-generated by `start.sh` on fresh deployments.
- If missing: an **Encryption not configured** banner appears in the Claude Subscriptions section, the Register button is disabled, and the API returns 503.
- Check status: `GET /api/subscriptions/encryption-status`.

### Reading usage and headroom

The **Pressure** column in the subscriptions table gives each subscription a one-glance state:

| Pressure cell | Meaning |
|---|---|
| `rate-limited` (red) | The subscription is refusing on quota right now — a fresh provider verdict, or a rate-limit event in the last 2 hours with no fresh verdict to the contrary. Hover for the 24h event count. |
| `5h: 42% · 7d: 88%` | A provider reading under 30 minutes old: the share of each limit already spent. Hover for the reading's age. |
| `N events (24h)` (amber) | Failure events in the last 24 hours but no fresh reading; hover for the rate-limit / auth split. |
| `ok` | No events and no fresh reading. |
| `—` | Usage couldn't be read. |

Expand the row for the **Usage** block:

- **Live headroom** — one line per window: `42% of 5h window · resets 14:05`, plus `Probe status: …` when the last probe didn't return `ok` (for example `rate_limited` or `invalid_token`). The source line beside the heading says `actual (Anthropic) · 12 min ago` or `observed (estimate from recorded consumption)`.
- **Refresh** — fires one probe now. Re-clicking within 60 seconds serves the cached reading with its age rather than probing again.
- **Observed consumption** — a table for **Last 5h** and **Last 7d**: `≈ In tokens (est.)` (an estimate from recorded context occupancy, not a billed counter), **Out tokens**, **Cost**, and **Messages**. Cost is API-equivalent — what the consumption would cost at API prices — not a bill.
- **Failure events** — `⚠ N subscription failure events in the last 24h (3 rate-limit · 1 auth)`.
- **Show per-agent breakdown** — which agents burn the quota: 7-day rows ordered by cost, with out tokens, messages, and the 5h cost, so you can see who to move or stagger.

A subscription that recovers stops claiming it is rate-limited as soon as a probe says the provider is serving it again; the platform re-checks any subscription still wearing a rate-limited badge every 5 minutes (while automatic checks are on) so the badge clears without waiting out the 2-hour window. While it is limited, the row also says when the limit returns.

### Automatic quota checks

The **Check subscription quota automatically** toggle governs all probing Trinity does on its own: the ambient refresh while a dashboard or the Settings page is open (at most one probe per 15 minutes per subscription), the recovery re-check above, and the hourly sample behind the weekly-limit alert. It is on by default. Turn it off and headroom updates only via **Refresh**, the recovery re-check stops, and weekly-limit alerts stop.

Every probe that runs is also kept as a history row for 30 days, so utilization trends are answerable over the API (see For Agents). Viewing history never probes.

### Warn before the weekly limit

**Warn me before the weekly limit** raises an operator alert when a subscription passes a set share of its 7-day window — default **75**, allowed range 50–99, or **0** to turn the alerts off. Trinity samples each subscription about once an hour and files the alert into the operator queue (Operations → Needs Response), one per subscription per weekly window. Urgency follows your own burn rate: at the threshold but on track to finish the window under 100%, the alert is filed **low** priority; on track to run out before the reset, **high**. A second, escalating alert fires at 90% (or at your threshold if it is higher). The alert body names the utilization, the reset time, the projection, and the agents on that subscription.

When *every* registered subscription (two or more) is past the threshold, one fleet-wide **high** alert replaces the per-subscription ones for that cycle: `All N subscriptions are near their weekly limit`.

The status line under the control says whether the alert is actually running — `Active — warning at 75%, escalating at 90%.` — or names why it isn't: no subscriptions registered, threshold set to 0, automatic quota checks off, or the platform cache unreachable. "No alerts" is never left indistinguishable from "not checking".

### What happens on a rate limit

With **Automatically switch subscriptions when usage limits are reached** on (the default), a rate-limited or rejected subscription no longer costs the user their message:

1. **Before the first attempt** — if the agent's subscription is already known not to serve (a fresh provider reading says it is refusing, or it hit a rate limit in the last 2 hours with no fresh reading to the contrary), Trinity switches the agent before dispatching, so the first message after a limit does not burn a failed attempt.
2. **Mid-turn** — if the provider refuses the turn (rate limit or rejected token), Trinity records the failure event, switches the agent, hot-reloads the token, and re-issues the turn once on the new subscription. A turn gets at most one remediation.
3. **Choosing the destination** — candidates that failed in the last 2 hours are skipped, unless there is positive evidence they recovered (a fresh reading says the provider is serving them, or the window they failed in has since reset). Survivors are ranked by cached headroom — furthest from the nearer of the 5h/7d walls first, in 10-point bands, with fewest-agents as the tie-break — and any candidate the provider is currently refusing is dropped. Ranking reads cached readings only; it never probes.
4. **Nothing to switch to** — with **Fall back to the platform API key** on and a platform key configured, the agent's subscription assignment is cleared and the agent is restarted on the API key so the turn completes. This is not a temporary redirect: reassign a subscription deliberately when you want spend back on it. With the fallback off (or no key), the turn fails and the error names the earliest known reset time.

Every switch is logged as an activity on the agent and sends a **high**-priority notification — `Subscription auto-switched to '<name>'` — that says what failed and why the destination was chosen (for example, "It had the most headroom of the 3 alternatives"). The API-key fallback notifies as `Switched to the platform API key`. A switched turn's response carries `subscription_switch`, so an API client can tell that a retry is worthwhile.

Auth failures are treated differently from quota: a rejected token is recorded as an **auth** event and the subscription is never shown as rate-limited. Past auth events alone do not trigger the pre-dispatch switch, since a credential problem may be shared by every subscription; a turn the provider actually rejects still switches and retries. Re-register the token.

### Where pressure shows up

- **Settings → Integrations → Claude Subscriptions** — the Pressure column and the Usage block, above.
- **Dashboard** — the admin-only **Subscription pressure** Grid tile (one row per subscription with colour-banded 5h/7d bars), and the per-agent **sub limit** / **sub 429s** / **sub auth** chips on Grid tiles and List rows. See [Dashboard](../operations/dashboard.md).
- **Agent Detail** — the auth badge in the header names the subscription.

### Settings reference

All four controls live under the subscriptions table in **Settings → Integrations → Claude Subscriptions**:

| Control | Default | Effect |
|---|---|---|
| **Automatically switch subscriptions when usage limits are reached** | On | Enables auto-switch. Failure events are recorded either way, so usage readings stay complete when it is off. |
| **Check subscription quota automatically** | On | Enables every autonomous probe: ambient refresh, recovery re-check, weekly sampling. |
| **Fall back to the platform API key** | On | Lets a turn move onto the platform API key when no subscription can serve it. Shows a warning when no key is configured. |
| **Warn me before the weekly limit** | 75 | Weekly-limit alert threshold (50–99, or 0 = off). |

## For Agents

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/subscriptions` | GET | List subscriptions with assigned agents — admins see the fleet, everyone else their own |
| `/api/subscriptions` | POST | Register (or, by name, replace) a subscription. Admin |
| `/api/subscriptions/{id}` | GET | One subscription with its agents. Admin. `{id}` accepts the UUID or the name on every route below |
| `/api/subscriptions/{id}` | DELETE | Delete a subscription; clears its agents. Admin |
| `/api/subscriptions/{id}/usage` | GET | 5h/7d observed usage, 24h failure events by kind, `rate_limited_now`, `source`, and the `headroom` block (per-window utilization, status, reset, snapshot age). Admin |
| `/api/subscriptions/{id}/usage/breakdown` | GET | Per-agent 5h/7d rows ordered by cost. Admin |
| `/api/subscriptions/{id}/usage/refresh` | POST | Probe the provider now (floored at one per 60 s) and return the refreshed usage. Admin |
| `/api/subscriptions/{id}/headroom/history?window=24h\|7d\|30d` | GET | Probe history — one point per hour (24h, 7d) or day (30d), each with its own timestamp; missing buckets are absent, and `coverage_pct` says how thin the series is. Never probes. Admin |
| `/api/subscriptions/agents/{agent_name}` | PUT | Assign a subscription: `?subscription_name=<name>`. Owner or admin. Returns `restart_result` |
| `/api/subscriptions/agents/{agent_name}` | DELETE | Clear the agent's subscription (back to the API key; restarts a running agent). Owner or admin |
| `/api/subscriptions/agents/{agent_name}/auth` | GET | The agent's auth mode: `subscription`, `api_key`, or `not_configured` |
| `/api/agents/subscription-pressure` | GET | Per-agent pressure for every agent you can access — the source of the Dashboard chips |
| `/api/subscriptions/encryption-status` | GET | Whether `CREDENTIAL_ENCRYPTION_KEY` is configured. Admin |
| `/api/subscriptions/settings/auto-switch` | GET / PUT | Auto-switch toggle (`?enabled=`). Admin |
| `/api/subscriptions/settings/headroom-auto-refresh` | GET / PUT | Automatic quota checks (`?enabled=`); the GET also carries `weekly_alert` with `active`, `inactive_reason`, and the threshold bounds. Admin |
| `/api/subscriptions/settings/api-key-fallback` | GET / PUT | API-key fallback (`?enabled=`); the GET also reports `key_configured`. Admin |
| `/api/subscriptions/settings/headroom-alert-threshold` | PUT | Weekly-limit threshold (`?threshold_pct=`, 0 or 50–99). Admin |

Retention for probe history and failure events is 30 days each (`subscription_headroom_retention_days`, `subscription_failure_event_retention_days`), visible on `GET /api/settings/retention` and set through `PUT /api/settings/{key}`.

**API Endpoints**: See [Backend API Docs](http://localhost:8000/docs) for full schemas.

### MCP Tools

- `register_subscription(name, token, subscription_type?, rate_limit_tier?)` -- Register a new subscription (or replace the token of an existing name). Admin.
- `list_subscriptions()` -- List subscriptions and their assigned agents.
- `assign_subscription(agent_name, subscription_name)` -- Assign a subscription to an agent. Owner or admin.
- `clear_agent_subscription(agent_name)` -- Remove the subscription assignment from an agent. Owner or admin.
- `get_agent_auth(agent_name)` -- Get the auth configuration for an agent.
- `delete_subscription(subscription_name)` -- Delete a subscription. Admin.

Usage, headroom, and the settings toggles have no MCP tool; use the REST endpoints above.

## Limitations

- Requires `CREDENTIAL_ENCRYPTION_KEY` in `.env`. Without it, subscription features are unavailable.
- Auto-switch depends on failure detection. If an agent does not surface a rate-limit or auth-class error through standard logging, auto-switch will not trigger.
- Hot-reload applies the new token to the **next** Claude subprocess; turns already in flight finish on the previous token. On older agent base images that lack the hot-reload endpoint, the switch falls back to recreating the container (which drops in-flight executions).
- Headroom percentages come from a provider probe, so they are only as fresh as the last probe: surfaces show a percentage only when the reading is under 30 minutes old and fall back to a qualitative state otherwise. A reset time is shown regardless of age.
- The weekly-limit alert keeps the figure it was raised with; a later, higher reading arrives as the separate escalation alert rather than an edit.
- Headroom history is API-only; no page charts it yet.
- **An API key in `.env` will not override a subscription.** On a Claude-runtime agent authenticated by a subscription, Trinity strips `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` from the execution environment before each run. Claude prefers an API key over the subscription token, so a stale one left on the agent's workspace volume would silently authenticate every run — and its failures would be blamed on the subscription, marking healthy subscriptions unhealthy in turn. If you genuinely want an agent on an API key, clear its subscription rather than putting a key in `.env`. Agents on other runtimes are unaffected. `GET /api/credentials/status` reports which keys are being suppressed, by name.

## See Also

- [Credential Management](credential-management.md)
- [Dashboard](../operations/dashboard.md) — the Subscription pressure tile and per-agent pressure chips
- [Operations Page](../operations/operating-room.md) — where weekly-limit alerts land
- [First-Time Setup](../getting-started/setup.md) — the Settings page overview
