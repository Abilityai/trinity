# Feature: Helicone LLM Observability Integration

> **Status**: Proposed
> **Priority**: High
> **Phase**: 11.x (Ecosystem & Enterprise)
> **Created**: 2025-12-20
> **Requirement ID**: 11.1
> **Last Reviewed**: 2025-12-20

## Overview

Integrate Helicone AI observability platform into Trinity to provide comprehensive LLM request monitoring, cost tracking, budget controls, and analytics for all agent Claude Code sessions.

## User Stories

1. **As a platform admin**, I want to see all LLM API calls across all agents so that I can monitor usage and costs.
2. **As an agent owner**, I want to see the cost breakdown of my agent's Claude Code sessions so that I can optimize prompt usage.
3. **As a platform admin**, I want to set spending limits per agent so that runaway agents don't consume excessive API credits.
4. **As a developer**, I want to analyze slow queries and high-token requests so that I can debug performance issues.

---

## Decision: Helicone vs OpenTelemetry

Claude Code has **built-in OpenTelemetry support** which provides similar metrics without a proxy layer. Consider the trade-offs:

| Capability | Helicone | OpenTelemetry (Built-in) |
|------------|----------|--------------------------|
| Cost tracking | ✅ Full dashboard | ✅ `claude_code.cost.usage` metric |
| Token tracking | ✅ By type/model | ✅ `claude_code.token.usage` metric |
| Session tracking | ✅ Full history | ✅ `claude_code.session.count` metric |
| Request logging | ✅ Full prompts/responses | ✅ Events (prompts optional) |
| Semantic caching | ✅ Yes | ❌ No |
| Rate limiting | ✅ Built-in | ❌ Requires separate implementation |
| Dashboard | ✅ Full web UI | ❌ Requires Grafana/similar |
| Latency overhead | ~5ms (proxy) | <1ms (local export) |
| Setup complexity | Medium | Low |

**Recommendation**: Use Helicone when you need caching, rate limiting, or a full observability dashboard. Use OpenTelemetry for lightweight metrics integrated into existing monitoring infrastructure.

### OpenTelemetry Alternative (If Not Using Helicone)

If you prefer OpenTelemetry, inject these env vars into agent containers:

```python
env_vars = {
    'CLAUDE_CODE_ENABLE_TELEMETRY': '1',
    'OTEL_METRICS_EXPORTER': 'otlp',
    'OTEL_LOGS_EXPORTER': 'otlp',
    'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://otel-collector:4317',
    'OTEL_RESOURCE_ATTRIBUTES': f'agent.name={config.name},agent.owner={owner}',
}
```

See [Claude Code Monitoring Documentation](https://code.claude.com/docs/monitoring-usage) for details.

---

## Architecture

### Single Container Deployment

Deploy Helicone as a single all-in-one Docker container that includes:
- AI Gateway (Rust-based proxy, <5ms overhead)
- PostgreSQL database
- ClickHouse analytics database
- MinIO object storage
- Web Dashboard UI

**Important**: The container internally uses port 3000 for the dashboard. We remap to 3001 externally to avoid conflict with Trinity frontend.

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Trinity Platform                                                         │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐    │
│  │ Agent 1     │  │ Agent 2     │  │ Agent 3     │  │ Agent N     │    │
│  │ Claude Code │  │ Claude Code │  │ Claude Code │  │ Claude Code │    │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘    │
│         │                │                │                │            │
│         └────────────────┼────────────────┼────────────────┘            │
│                          │                │                              │
│                          ▼                ▼                              │
│                 ┌─────────────────────────────────────┐                 │
│                 │      Helicone All-in-One Container   │                 │
│                 │                                      │                 │
│                 │  ┌────────────────────────────────┐ │                 │
│                 │  │   AI Gateway (:8585)           │ │                 │
│                 │  │   - Proxies to Anthropic API   │ │                 │
│                 │  │   - Logs all requests          │ │                 │
│                 │  │   - Rate limiting              │ │                 │
│                 │  │   - Caching                    │ │                 │
│                 │  └────────────┬───────────────────┘ │                 │
│                 │               │                      │                 │
│                 │  ┌────────────▼───────────────────┐ │                 │
│                 │  │   ClickHouse (:8123)           │ │                 │
│                 │  │   Time-series analytics        │ │                 │
│                 │  └────────────────────────────────┘ │                 │
│                 │                                      │                 │
│                 │  ┌────────────────────────────────┐ │                 │
│                 │  │   PostgreSQL (:5432)           │ │                 │
│                 │  │   User/org/settings data       │ │                 │
│                 │  └────────────────────────────────┘ │                 │
│                 │                                      │                 │
│                 │  ┌────────────────────────────────┐ │                 │
│                 │  │   Web Dashboard (:3000→3001)   │ │                 │
│                 │  │   Analytics UI                 │ │                 │
│                 │  └────────────────────────────────┘ │                 │
│                 │                                      │                 │
│                 └──────────────────┬──────────────────┘                 │
│                                    │                                     │
└────────────────────────────────────┼─────────────────────────────────────┘
                                     │
                                     ▼
                          ┌─────────────────────┐
                          │   Anthropic API     │
                          │   api.anthropic.com │
                          └─────────────────────┘
```

### Network Configuration

- **Helicone container**: Joins `trinity-network` (same as backend, frontend, redis)
- **Agent containers**: On `agent-net` (172.28.0.0/16) but can reach `trinity-network` via Docker DNS
- **Gateway URL**: Agents use `http://helicone:8585/v1` (Docker DNS resolution)

### Gateway API Requirements

Per [Claude Code LLM Gateway documentation](https://code.claude.com/docs/llm-gateway), the gateway must:

1. Expose Anthropic Messages API format: `/v1/messages`, `/v1/messages/count_tokens`
2. Forward headers: `anthropic-beta`, `anthropic-version`
3. Preserve request body fields

Helicone's AI Gateway supports Anthropic API format. Verify during testing.

---

## Functional Requirements

### 11.1.1 Helicone Container Deployment

- **Status**: ⏳ Not Started
- **Priority**: High
- **Description**: Add Helicone all-in-one container to Trinity docker-compose

**Acceptance Criteria**:
- [ ] Add `helicone` service to `docker-compose.yml`
- [ ] Use official `helicone/helicone-all-in-one` image (pinned version)
- [ ] Expose ports: 3001 (dashboard remapped from 3000), 8585 (gateway)
- [ ] Persist data via Docker volume `helicone-data`
- [ ] Connect to `trinity-network` for backend/agent access
- [ ] Container starts automatically with Trinity stack
- [ ] Health check configured

**Implementation Details**:

```yaml
# docker-compose.yml addition
helicone:
  image: helicone/helicone-all-in-one:v2025.08.10  # Pin to specific version
  container_name: trinity-helicone
  ports:
    - "3001:3000"    # Dashboard (container uses 3000, remap to 3001)
    - "8585:8585"    # AI Gateway
  volumes:
    - helicone-data:/data
  environment:
    - HELICONE_API_KEY=${HELICONE_API_KEY:-sk-helicone-trinity}
  networks:
    - trinity-network
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:3000/health"]
    interval: 30s
    timeout: 10s
    retries: 3
    start_period: 60s
  restart: unless-stopped

volumes:
  helicone-data:
```

**Files to Modify**:
| File | Change |
|------|--------|
| `docker-compose.yml` | Add helicone service, helicone-data volume |
| `.env.example` | Add HELICONE_API_KEY, HELICONE_ENABLED variables |

---

### 11.1.2 Agent Gateway Routing

- **Status**: ⏳ Not Started
- **Priority**: High
- **Description**: Route all agent Claude Code requests through Helicone gateway

**Acceptance Criteria**:
- [ ] Inject `ANTHROPIC_BASE_URL` environment variable into agent containers
- [ ] URL points to Helicone gateway: `http://helicone:8585/v1`
- [ ] Configurable via `HELICONE_ENABLED` environment variable
- [ ] Fallback to direct Anthropic API if Helicone disabled or unavailable
- [ ] All Claude Code API calls proxied through Helicone when enabled
- [ ] Original ANTHROPIC_API_KEY still used for actual API auth

**Implementation Details**:

Agent environment variable injection in `src/backend/routers/agents.py:503-538`:

```python
# Current (line 507):
env_vars = {
    "ANTHROPIC_API_KEY": anthropic_key,
    # ... other vars
}

# Updated with Helicone support:
helicone_enabled = os.getenv("HELICONE_ENABLED", "true").lower() == "true"
helicone_key = os.getenv("HELICONE_API_KEY", "")

env_vars = {
    "ANTHROPIC_API_KEY": anthropic_key,
    # ... other vars
}

# Conditionally route through Helicone
if helicone_enabled and helicone_key:
    env_vars["ANTHROPIC_BASE_URL"] = "http://helicone:8585/v1"
```

**Fallback Behavior**:
- If `HELICONE_ENABLED=false`, agents use default `api.anthropic.com`
- If Helicone container is down, agents will fail (no automatic fallback)
- Future: Health check Helicone before injecting URL

**Files to Modify**:
| File | Line(s) | Change |
|------|---------|--------|
| `src/backend/routers/agents.py` | 503-538 | Add conditional ANTHROPIC_BASE_URL injection |
| `src/backend/config.py` | N/A | Add HELICONE_ENABLED config constant |
| `docker/base-image/startup.sh` | N/A | No changes needed - env vars auto-available |

---

### 11.1.3 Per-Agent Property Tracking

- **Status**: ⏳ Not Started
- **Priority**: High
- **Description**: Tag each request with agent metadata for filtering in Helicone dashboard

**Acceptance Criteria**:
- [ ] Each request includes agent name in Helicone properties
- [ ] Each request includes owner user ID
- [ ] Each request includes agent type
- [ ] Helicone dashboard can filter by agent/user/type

**Implementation Details**:

Helicone uses custom HTTP headers for property tracking. Claude Code supports custom headers via the `ANTHROPIC_CUSTOM_HEADERS` environment variable (see [Claude Code Settings](https://code.claude.com/docs/settings)).

```python
# src/backend/routers/agents.py - during container creation

# Build Helicone headers string (comma-separated "Name: Value" pairs)
helicone_headers = ", ".join([
    f"Helicone-Auth: Bearer {helicone_key}",
    f"Helicone-Property-Agent-Name: {config.name}",
    f"Helicone-Property-Owner-Id: {current_user.username}",
    f"Helicone-Property-Agent-Type: {config.type or 'business-assistant'}",
    f"Helicone-Property-Template: {config.template or 'none'}",
])

env_vars = {
    # ... existing vars ...
    "ANTHROPIC_BASE_URL": "http://helicone:8585/v1",
    "ANTHROPIC_CUSTOM_HEADERS": helicone_headers,
}
```

**Important**: Claude Code reads `ANTHROPIC_CUSTOM_HEADERS` and includes them in all API requests. This is the official mechanism - do NOT use `HELICONE_PROPERTY_*` env vars (those are not read by Claude Code).

**Files to Modify**:
| File | Line(s) | Change |
|------|---------|--------|
| `src/backend/routers/agents.py` | 503-538 | Add ANTHROPIC_CUSTOM_HEADERS with Helicone properties |

---

### 11.1.4 Helicone Dashboard Access

- **Status**: ⏳ Not Started
- **Priority**: Medium
- **Description**: Provide access to Helicone dashboard for observability

**Acceptance Criteria**:
- [ ] Helicone dashboard accessible at port 3001 (external)
- [ ] Link to Helicone from Trinity Settings page
- [ ] Optional: Embed Helicone dashboard in Trinity UI via iframe
- [ ] Admin-only access control

**Implementation Options**:

**Option A: External Link (Recommended for Phase 1)**
Add link to Settings.vue pointing to `http://localhost:3001` (dev) or appropriate production URL.

**Option B: Iframe Embed (Future)**
Add "Observability" tab in AgentDetail.vue with embedded Helicone filtered by agent using URL parameters.

**Files to Modify**:
| File | Change |
|------|--------|
| `src/frontend/src/views/Settings.vue` | Add "LLM Observability" section with link |
| `src/frontend/src/views/AgentDetail.vue` | (Future) Add Observability tab |

---

### 11.1.5 Cost Tracking & Analytics

- **Status**: ⏳ Not Started
- **Priority**: High
- **Description**: Expose cost data from Helicone for display in Trinity UI

**Acceptance Criteria**:
- [ ] Query Helicone API for per-agent cost data
- [ ] Display cost in agent detail header (e.g., "$2.47 today")
- [ ] Display cost breakdown in agent analytics (tokens, requests)
- [ ] Historical cost charts (7-day, 30-day)

**Implementation Details**:

Create new backend service and endpoint that queries Helicone:

```python
# src/backend/services/helicone_service.py
import httpx

class HeliconeService:
    def __init__(self, api_key: str, base_url: str = "http://helicone:3000"):
        self.api_key = api_key
        self.base_url = base_url

    async def get_agent_costs(self, agent_name: str, period: str = "day") -> dict:
        """Query Helicone for agent cost data."""
        async with httpx.AsyncClient() as client:
            # Query Helicone API with property filter
            response = await client.get(
                f"{self.base_url}/api/v1/requests/costs",
                headers={"Authorization": f"Bearer {self.api_key}"},
                params={
                    "filter": f"properties.Agent-Name={agent_name}",
                    "period": period,
                }
            )
            return response.json()

# src/backend/routers/agents.py
@router.get("/{agent_name}/costs")
async def get_agent_costs(
    agent_name: str,
    period: str = "day",
    current_user: User = Depends(get_current_user)
):
    """Query Helicone for agent cost data."""
    helicone = HeliconeService(os.getenv("HELICONE_API_KEY"))
    return await helicone.get_agent_costs(agent_name, period)
```

**Files to Modify**:
| File | Change |
|------|--------|
| `src/backend/services/helicone_service.py` | New file - Helicone API client |
| `src/backend/routers/agents.py` | Add `/costs` endpoint |
| `src/frontend/src/views/AgentDetail.vue` | Display cost in header |
| `src/frontend/src/stores/agents.js` | Add `fetchAgentCosts` action |

---

### 11.1.6 Budget Controls

- **Status**: ⏳ Not Started
- **Priority**: Medium
- **Description**: Set spending limits per agent with automatic enforcement

**Acceptance Criteria**:
- [ ] Admin can set daily/monthly budget per agent
- [ ] Budget stored in Trinity database (agent_budgets table)
- [ ] Backend checks **previous spend** before allowing new chat requests
- [ ] Agent chat blocked when budget exceeded
- [ ] Warning at 80% budget threshold
- [ ] Email notification on budget exceed (optional)

**Implementation Details**:

Budget configuration in agent settings UI or template:

```yaml
# template.yaml (optional defaults)
budget:
  daily_limit: 10.00  # USD
  monthly_limit: 100.00
  action_on_exceed: block  # block | warn | none
```

**Important**: Budget check must query **previous spend** before allowing a new request. You cannot pre-check the cost of an upcoming request (costs are only known after API call completes).

```python
# src/backend/routers/chat.py

async def check_budget_before_chat(agent_name: str, db: Session) -> tuple[bool, str]:
    """
    Check if agent is within budget BEFORE allowing new chat request.

    Returns (allowed, message) tuple.
    Note: We check previous spend, not the upcoming request cost.
    """
    budget = db.query(AgentBudget).filter_by(agent_name=agent_name).first()
    if not budget:
        return True, ""  # No budget configured

    # Get current spend from Helicone
    helicone = HeliconeService(os.getenv("HELICONE_API_KEY"))
    daily_spend = await helicone.get_agent_costs(agent_name, "day")
    monthly_spend = await helicone.get_agent_costs(agent_name, "month")

    # Check daily limit
    if daily_spend["total_cost"] >= budget.daily_limit:
        if budget.action_on_exceed == "block":
            return False, f"Daily budget exceeded (${daily_spend['total_cost']:.2f} / ${budget.daily_limit:.2f})"

    # Check monthly limit
    if monthly_spend["total_cost"] >= budget.monthly_limit:
        if budget.action_on_exceed == "block":
            return False, f"Monthly budget exceeded (${monthly_spend['total_cost']:.2f} / ${budget.monthly_limit:.2f})"

    # Warning at 80% threshold (log but allow)
    if daily_spend["total_cost"] >= budget.daily_limit * 0.8:
        logger.warning(f"Agent {agent_name} at {daily_spend['total_cost']/budget.daily_limit*100:.0f}% of daily budget")

    return True, ""

# In chat endpoint:
@router.post("/{agent_name}/chat")
async def chat_with_agent(agent_name: str, message: ChatMessage, ...):
    allowed, error_msg = await check_budget_before_chat(agent_name, db)
    if not allowed:
        raise HTTPException(status_code=429, detail=error_msg)

    # Proceed with chat...
```

**Files to Modify**:
| File | Change |
|------|--------|
| `src/backend/database.py` | Add agent_budgets table |
| `src/backend/routers/chat.py` | Add budget check before processing |
| `src/frontend/src/views/AgentDetail.vue` | Add budget settings UI |
| `src/frontend/src/components/BudgetPanel.vue` | New component for budget config |

---

### 11.1.7 Rate Limiting

- **Status**: ⏳ Not Started
- **Priority**: Medium
- **Description**: Configure rate limits per agent via Helicone

**Acceptance Criteria**:
- [ ] Rate limits configurable per agent
- [ ] Limits can be by: requests/minute, tokens/minute
- [ ] 429 response when limit exceeded
- [ ] Rate limit status visible in UI

**Implementation Details**:

Helicone rate limiting is configured via the Helicone web dashboard or API, **not** via YAML config files. The rate limits are applied based on the property headers we inject (e.g., `Helicone-Property-Agent-Name`).

**Configuration via Helicone Dashboard**:
1. Navigate to Helicone dashboard → Settings → Rate Limits
2. Create rule: Property `Agent-Name` equals `{agent-name}` → 100 requests/minute
3. Rules are evaluated server-side by Helicone gateway

**Configuration via Helicone API** (programmatic):
```python
# src/backend/services/helicone_service.py
async def set_rate_limit(self, agent_name: str, requests_per_minute: int):
    """Configure rate limit for an agent via Helicone API."""
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{self.base_url}/api/v1/rate-limits",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "property": "Agent-Name",
                "value": agent_name,
                "limit": requests_per_minute,
                "window": "1m",
            }
        )
```

**Files to Modify**:
| File | Change |
|------|--------|
| `src/backend/services/helicone_service.py` | Add rate limit configuration methods |
| `src/frontend/src/components/RateLimitPanel.vue` | New UI for rate limit config |

---

### 11.1.8 Response Caching

- **Status**: ⏳ Not Started
- **Priority**: Low
- **Description**: Enable semantic caching for repeated queries

**Acceptance Criteria**:
- [ ] Helicone caching enabled for similar prompts
- [ ] Cache hit rate visible in analytics
- [ ] Cache bypass option per request
- [ ] Cost savings from caching displayed

**Implementation Details**:

Caching is configured in Helicone via dashboard or headers. To enable:

1. **Global enable via dashboard**: Helicone Dashboard → Settings → Caching → Enable
2. **Per-request control via headers**: Add to `ANTHROPIC_CUSTOM_HEADERS`:
   ```
   Helicone-Cache-Enabled: true
   ```

```python
# Optional: Add cache header to agent env vars
helicone_headers = ", ".join([
    f"Helicone-Auth: Bearer {helicone_key}",
    f"Helicone-Property-Agent-Name: {config.name}",
    "Helicone-Cache-Enabled: true",  # Enable caching
    # ...
])
```

---

## Non-Functional Requirements

### Performance
- Gateway latency overhead: <5ms (Helicone specification)
- Dashboard load time: <2s
- No impact on agent Claude Code responsiveness

### Reliability
- Helicone container auto-restarts on failure (restart: unless-stopped)
- Health check: `curl http://localhost:3000/health` every 30s
- **Fallback behavior**: If `HELICONE_ENABLED=false`, agents bypass Helicone
- Data persistence across container restarts via Docker volume

### Security
- Helicone container on internal network only (no external port for dashboard in production)
- Dashboard access requires network access (future: Trinity auth integration)
- API keys never logged in plaintext
- Helicone-Auth header secures gateway access

### Scalability
- ClickHouse handles millions of logged requests
- Single container sufficient for typical Trinity deployment (<50 agents)
- Upgrade path to distributed Helicone for enterprise (Helm charts available)

---

## Data Model

### New Database Tables

```sql
-- Agent budget configuration
CREATE TABLE agent_budgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT UNIQUE NOT NULL,
    daily_limit REAL DEFAULT 10.0,
    monthly_limit REAL DEFAULT 100.0,
    action_on_exceed TEXT DEFAULT 'warn',  -- 'block', 'warn', 'none'
    warning_threshold REAL DEFAULT 0.8,    -- 80% threshold for warnings
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name) ON DELETE CASCADE
);

CREATE INDEX idx_agent_budgets_name ON agent_budgets(agent_name);
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `HELICONE_API_KEY` | API key for Helicone auth | (required if enabled) |
| `HELICONE_ENABLED` | Enable/disable Helicone routing | `true` |
| `ANTHROPIC_BASE_URL` | Gateway URL (injected into agents when Helicone enabled) | `http://helicone:8585/v1` |

---

## Files to Create

| File | Purpose |
|------|---------|
| `src/backend/services/helicone_service.py` | Helicone API client for cost queries, rate limits |
| `src/frontend/src/components/BudgetPanel.vue` | Budget configuration UI |
| `src/frontend/src/components/CostsPanel.vue` | Cost display and charts |
| `docs/memory/feature-flows/helicone-observability.md` | Feature flow documentation |

## Files to Modify

| File | Section | Change |
|------|---------|--------|
| `docker-compose.yml` | services | Add helicone service with health check |
| `docker-compose.yml` | volumes | Add helicone-data volume |
| `.env.example` | N/A | Add HELICONE_API_KEY, HELICONE_ENABLED |
| `src/backend/routers/agents.py` | create_agent_internal() L503-538 | Add ANTHROPIC_BASE_URL, ANTHROPIC_CUSTOM_HEADERS |
| `src/backend/routers/agents.py` | N/A | Add `/costs` endpoint |
| `src/backend/routers/chat.py` | chat_with_agent() | Add budget check before processing |
| `src/backend/database.py` | N/A | Add agent_budgets table |
| `src/frontend/src/views/Settings.vue` | N/A | Add Observability section with link |
| `src/frontend/src/views/AgentDetail.vue` | header | Display cost data, add Costs tab |
| `docs/memory/requirements.md` | Phase 11 | Add 11.1.x requirements |

---

## Testing

### Prerequisites
- [ ] Helicone container running (`docker-compose up helicone`)
- [ ] At least one agent created and running
- [ ] ANTHROPIC_API_KEY configured
- [ ] HELICONE_API_KEY configured

### Test Steps

#### 1. Verify Gateway Routing
**Action**: Send a chat message to an agent
**Expected**:
- Request proxied through Helicone
- Request logged in Helicone dashboard
- Response returned normally

**Verify**:
- [ ] Helicone dashboard shows request at http://localhost:3001
- [ ] Agent received correct response
- [ ] Latency overhead <10ms
- [ ] Check agent container: `echo $ANTHROPIC_BASE_URL` shows helicone URL

#### 2. Verify Per-Agent Tracking
**Action**: Send messages to multiple agents
**Expected**: Each request tagged with correct agent_name property

**Verify**:
- [ ] Helicone dashboard can filter by "Agent-Name" property
- [ ] Each agent's requests are isolated
- [ ] Owner ID and Agent Type properties present

#### 3. Verify Cost Tracking
**Action**: Check agent costs after multiple messages
**Expected**: Cost data available via API and UI

**Verify**:
- [ ] `GET /api/agents/{name}/costs` returns cost data
- [ ] AgentDetail.vue displays cost in header

#### 4. Verify Budget Controls
**Action**: Set $0.10 daily budget, send messages until exceeded
**Expected**: Agent chat blocked when budget exceeded

**Verify**:
- [ ] Warning logged at 80% budget
- [ ] 429 error with message when budget exceeded
- [ ] UI shows budget status

#### 5. Verify Fallback (Helicone Disabled)
**Action**: Set `HELICONE_ENABLED=false`, restart agents
**Expected**: Agents communicate directly with Anthropic API

**Verify**:
- [ ] Agent container `$ANTHROPIC_BASE_URL` is unset or default
- [ ] Chat still works without Helicone

---

## Rollout Plan

### Phase 1: Basic Gateway (Week 1)
1. Add Helicone container to docker-compose with health check
2. Configure agent routing through gateway (ANTHROPIC_BASE_URL)
3. Inject property headers (ANTHROPIC_CUSTOM_HEADERS)
4. Verify all requests logged in Helicone dashboard
5. Add link to Helicone dashboard in Settings page

### Phase 2: Cost Display (Week 2)
1. Create HeliconeService for API queries
2. Add `/api/agents/{name}/costs` endpoint
3. Display costs in AgentDetail header
4. Add cost charts to agent detail page

### Phase 3: Budget Controls (Week 3)
1. Add agent_budgets table to database
2. Add budget UI in agent settings
3. Implement budget check in chat endpoint
4. Add notifications for budget warnings

### Phase 4: Advanced Features (Week 4+)
1. Rate limiting configuration via UI
2. Caching configuration
3. OpenTelemetry integration (optional complement)

---

## Related Flows

- **Upstream**: Agent Lifecycle (`agent-lifecycle.md`) - Container creation with env vars
- **Upstream**: Credential Injection (`credential-injection.md`) - API key management
- **Downstream**: Agent Chat (`agent-chat.md`) - Budget check integration
- **Related**: Activity Stream (`activity-stream.md`) - Could correlate with LLM costs

---

## References

- [Helicone GitHub](https://github.com/Helicone/helicone)
- [Helicone AI Gateway](https://github.com/Helicone/ai-gateway)
- [Helicone Docker Self-Hosting](https://docs.helicone.ai/getting-started/self-host/docker)
- [Helicone All-in-One Docker Hub](https://hub.docker.com/r/helicone/helicone-all-in-one)
- [Claude Code LLM Gateway Configuration](https://code.claude.com/docs/llm-gateway)
- [Claude Code Settings & Environment Variables](https://code.claude.com/docs/settings)
- [Claude Code Monitoring (OpenTelemetry)](https://code.claude.com/docs/monitoring-usage)

---

*Document created: 2025-12-20*
*Last updated: 2025-12-20*
*Reviewed: 2025-12-20 - Fixed technical issues per Claude Code docs*
