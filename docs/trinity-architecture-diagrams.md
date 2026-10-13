# Trinity Agent Platform - Architecture Diagrams

> **Last Updated**: 2026-01-22
> **Platform**: The operating system for the AI-native company — open source, self-hosted, that you own

## 1. High-Level Platform Architecture

```mermaid
graph TB
    subgraph "Trinity Platform"
        subgraph "Frontend Layer"
            FE[Vue.js Frontend<br/>:80]
        end

        subgraph "Core Services"
            BE[FastAPI Backend<br/>:8000]
            MCP[MCP Server<br/>:8080]
            SCHED[Scheduler Service<br/>standalone]
        end

        subgraph "Data Layer"
            REDIS[(Redis<br/>Secrets/Cache)]
            SQLITE[(SQLite<br/>Platform Data)]
            VECTOR[Vector<br/>Log Aggregation]
        end

        subgraph "Process Engine"
            PE[Execution Engine]
            HANDLERS[Step Handlers]
            ANALYTICS[Analytics Service]
        end
    end

    subgraph "Agent Network (172.28.0.0/16)"
        SYS[trinity-system<br/>System Agent]
        A1[Agent 1]
        A2[Agent 2]
        AN[Agent N...]
    end

    FE --> BE
    FE -.->|WebSocket| BE
    BE --> MCP
    BE --> REDIS
    BE --> SQLITE
    BE --> VECTOR
    SCHED --> BE
    SCHED --> REDIS

    PE --> HANDLERS
    PE --> ANALYTICS
    BE --> PE

    MCP --> A1
    MCP --> A2
    MCP --> AN
    MCP --> SYS

    SYS -.->|Orchestrates| A1
    SYS -.->|Orchestrates| A2

    style FE fill:#4FC3F7,stroke:#0288D1,stroke-width:2px,color:#000
    style BE fill:#66BB6A,stroke:#388E3C,stroke-width:2px,color:#000
    style MCP fill:#9C27B0,stroke:#7B1FA2,stroke-width:2px,color:#fff
    style SCHED fill:#FF9800,stroke:#F57C00,stroke-width:2px,color:#000
    style PE fill:#E91E63,stroke:#C2185B,stroke-width:2px,color:#fff
    style SYS fill:#7C4DFF,stroke:#651FFF,stroke-width:3px,color:#fff
```

## 2. Agent Container Architecture

```mermaid
graph TB
    subgraph "Base Image: trinity-agent-base"
        RT[Multi-Runtime<br/>Python 3.11 | Node 20 | Go 1.21]
        CC[Claude Code CLI<br/>or Gemini CLI]
        AS[Agent Server<br/>FastAPI :8000]
    end

    subgraph "Agent Workspace"
        WS[/home/developer/workspace]
        ENV[.env<br/>Credentials]
        MCPJ[.mcp.json<br/>MCP Config]
        CLAUDE[.claude/<br/>Commands]
    end

    subgraph "Exposed Endpoints"
        CHAT[/api/chat]
        TASK[/api/task]
        FILES[/api/files]
        DASH[/api/dashboard]
        TERM[PTY Terminal]
    end

    RT --> CC
    CC --> AS
    AS --> WS
    AS --> ENV
    AS --> MCPJ
    AS --> CLAUDE

    AS --> CHAT
    AS --> TASK
    AS --> FILES
    AS --> DASH
    AS --> TERM

    style CC fill:#9C27B0,stroke:#7B1FA2,stroke-width:3px,color:#fff
    style AS fill:#66BB6A,stroke:#388E3C,stroke-width:2px,color:#000
    style WS fill:#FF5252,stroke:#D32F2F,stroke-width:2px,color:#fff
```

## 4. Agent-to-Agent Collaboration

```mermaid
sequenceDiagram
    participant O as Orchestrator Agent
    participant MCP as Trinity MCP Server
    participant BE as Backend
    participant W1 as Worker Agent 1
    participant W2 as Worker Agent 2
    participant DB as Database

    O->>MCP: chat_with_agent("worker-1", "task A")
    MCP->>BE: Validate permissions
    BE->>DB: Check agent_permissions
    DB-->>BE: Permission granted
    BE->>W1: POST /api/task

    par Parallel Execution
        W1->>W1: Execute task A
        O->>MCP: chat_with_agent("worker-2", "task B", parallel=true)
        MCP->>W2: POST /api/task
        W2->>W2: Execute task B
    end

    W1-->>MCP: Task A result
    W2-->>MCP: Task B result
    MCP-->>O: Combined results

    Note over O,W2: Agent-scoped MCP keys<br/>enforce permission boundaries
```

## 5. Process Engine Architecture

```mermaid
graph TB
    subgraph "Process Definition"
        YAML[YAML Definition]
        VALID[Schema Validator]
        VER[Version Manager]
    end

    subgraph "Execution Engine"
        EE[ExecutionEngine]
        DR[DependencyResolver]
        SM[State Machine]
    end

    subgraph "Step Handlers"
        AT[AgentTaskHandler]
        HA[HumanApprovalHandler]
        GW[GatewayHandler]
        TM[TimerHandler]
        NF[NotificationHandler]
        SP[SubProcessHandler]
    end

    subgraph "Services"
        AN[AnalyticsService]
        AL[AlertService]
        IN[InformedNotifier]
        WS[WebSocketPublisher]
    end

    YAML --> VALID
    VALID --> VER
    VER --> EE

    EE --> DR
    DR --> SM
    SM --> AT
    SM --> HA
    SM --> GW
    SM --> TM
    SM --> NF
    SM --> SP

    EE --> AN
    EE --> AL
    EE --> IN
    EE --> WS

    style EE fill:#E91E63,stroke:#C2185B,stroke-width:3px,color:#fff
    style AT fill:#9C27B0,stroke:#7B1FA2,stroke-width:2px,color:#fff
    style HA fill:#FF9800,stroke:#F57C00,stroke-width:2px,color:#000
```

## 6. Process Execution State Machine

```mermaid
stateDiagram-v2
    [*] --> PENDING: Create Execution
    PENDING --> RUNNING: Start
    RUNNING --> PAUSED: Human Approval Required
    PAUSED --> RUNNING: Approval Decision
    RUNNING --> COMPLETED: All Steps Done
    RUNNING --> FAILED: Unrecoverable Error
    RUNNING --> CANCELLED: User Cancel
    PAUSED --> CANCELLED: User Cancel
    COMPLETED --> [*]
    FAILED --> [*]
    CANCELLED --> [*]
```

## 7. Authentication & Authorization Flow

```mermaid
graph TB
    subgraph "User Authentication"
        EMAIL[Email Login<br/>6-digit code]
        ADMIN[Admin Login<br/>Password]
        JWT[JWT Token]
    end

    subgraph "MCP API Keys"
        UKEY[User MCP Key<br/>trinity_mcp_...]
        AKEY[Agent MCP Key<br/>Auto-generated]
        SKEY[System Key<br/>Bypasses checks]
    end

    subgraph "Permission Enforcement"
        PERM[agent_permissions table]
        SHARE[agent_sharing table]
        OWN[agent_ownership table]
    end

    subgraph "Access Levels"
        L1[Owner: Full control]
        L2[Shared: View + Chat]
        L3[Admin: Everything]
    end

    EMAIL --> JWT
    ADMIN --> JWT
    JWT --> UKEY

    UKEY --> PERM
    AKEY --> PERM
    SKEY -.->|Bypass| PERM

    PERM --> L1
    SHARE --> L2
    OWN --> L3

    style EMAIL fill:#4CAF50,stroke:#2E7D32,stroke-width:2px,color:#fff
    style SKEY fill:#7C4DFF,stroke:#651FFF,stroke-width:2px,color:#fff
    style PERM fill:#FF9800,stroke:#F57C00,stroke-width:2px,color:#000
```

## 8. Container Security Model

```mermaid
graph TB
    subgraph "Security Layers"
        USER[Non-root User<br/>developer:1000]
        CAP[Capabilities<br/>DROP: ALL<br/>ADD: NET_BIND_SERVICE]
        SEC[Security Options<br/>no-new-privileges<br/>apparmor:docker-default]
        NET[Network Isolation<br/>172.28.0.0/16]
    end

    subgraph "Resource Controls"
        CPU[CPU Limits<br/>Per-agent configurable]
        MEM[Memory Limits<br/>Per-agent configurable]
        TMPFS[tmpfs /tmp<br/>noexec,nosuid]
    end

    subgraph "Data Protection"
        VOL[Isolated Volumes]
        AUDIT[Vector Log Aggregation]
        TLS[HTTPS in Production]
    end

    USER --> CAP
    CAP --> SEC
    SEC --> NET

    NET --> CPU
    CPU --> MEM
    MEM --> TMPFS

    TMPFS --> VOL
    VOL --> AUDIT
    AUDIT --> TLS

    style USER fill:#E91E63,stroke:#C2185B,stroke-width:2px,color:#fff
    style CAP fill:#FF5722,stroke:#E64A19,stroke-width:2px,color:#fff
    style VOL fill:#795548,stroke:#5D4037,stroke-width:2px,color:#fff
```

## 9. Credential Flow Architecture

```mermaid
graph TB
    subgraph "Credential Sources"
        OAUTH[OAuth Providers<br/>Google, Slack, GitHub, Notion]
        MANUAL[Manual Entry<br/>API Keys]
        BULK[Bulk Import<br/>.env format]
    end

    subgraph "Platform Core"
        UI[Frontend UI]
        API[Backend API]
        REDIS[(Redis Store<br/>AOF Persistence)]
    end

    subgraph "Agent Container"
        ENV[.env file<br/>KEY=VALUE]
        MCPJ[.mcp.json<br/>Generated from template]
        SERVERS[MCP Servers]
    end

    subgraph "Hot Reload"
        HR[Hot-Reload API]
        REGEN[Regenerate .mcp.json]
    end

    OAUTH --> UI
    MANUAL --> UI
    BULK --> UI
    UI --> API
    API --> REDIS

    API -.->|At creation| ENV
    API -.->|At creation| MCPJ
    ENV --> SERVERS
    MCPJ --> SERVERS

    HR --> ENV
    HR --> REGEN
    REGEN --> MCPJ

    style OAUTH fill:#4285F4,stroke:#1976D2,stroke-width:2px,color:#fff
    style REDIS fill:#DC382D,stroke:#A41E22,stroke-width:2px,color:#fff
    style SERVERS fill:#9C27B0,stroke:#7B1FA2,stroke-width:2px,color:#fff
```

## 10. Docker Compose Service Dependencies

```mermaid
graph BT
    FRONT[frontend<br/>:80 nginx/Vue.js]
    BACK[backend<br/>:8000 FastAPI]
    MCP[mcp-server<br/>:8080 FastMCP]
    REDIS[(redis<br/>:6379)]
    VECTOR[vector<br/>:8686 Log Aggregation]
    SCHED[scheduler<br/>Standalone Service]

    FRONT --> BACK
    BACK --> REDIS
    BACK --> VECTOR
    MCP --> BACK
    SCHED --> BACK
    SCHED --> REDIS

    subgraph "Agent Network"
        A1[agent-1]
        A2[agent-2]
        SYS[trinity-system]
    end

    A1 --> MCP
    A2 --> MCP
    SYS --> MCP

    style FRONT fill:#4FC3F7,stroke:#0288D1,stroke-width:2px,color:#000
    style BACK fill:#66BB6A,stroke:#388E3C,stroke-width:2px,color:#000
    style MCP fill:#9C27B0,stroke:#7B1FA2,stroke-width:2px,color:#fff
    style REDIS fill:#DC382D,stroke:#A41E22,stroke-width:2px,color:#fff
    style VECTOR fill:#00BCD4,stroke:#0097A7,stroke-width:2px,color:#000
    style SCHED fill:#FF9800,stroke:#F57C00,stroke-width:2px,color:#000
    style SYS fill:#7C4DFF,stroke:#651FFF,stroke-width:2px,color:#fff
```

## 11. Execution Queue & Scheduling

```mermaid
sequenceDiagram
    participant USER as User/Schedule
    participant BE as Backend
    participant QUEUE as Redis Queue
    participant AGENT as Agent Container
    participant WS as WebSocket

    USER->>BE: Trigger execution
    BE->>QUEUE: submit() - atomic SET NX EX

    alt Slot Available
        QUEUE-->>BE: Slot acquired
        BE->>AGENT: POST /api/task
        BE->>WS: Broadcast activity_started

        AGENT->>AGENT: Execute Claude Code
        AGENT-->>BE: Result + execution_log

        BE->>QUEUE: complete() - Lua atomic pop-and-set
        BE->>WS: Broadcast activity_completed
    else Agent Busy
        QUEUE-->>BE: Agent busy
        BE-->>USER: Queued (position in queue)
    end
```

## 12. Live Execution Streaming

```mermaid
sequenceDiagram
    participant UI as Frontend
    participant BE as Backend
    participant AGENT as Agent Server
    participant CC as Claude Code

    UI->>BE: GET /api/agents/{name}/executions/{id}/stream
    BE->>AGENT: GET /api/executions/{id}/stream

    loop SSE Stream
        CC->>AGENT: stdout line
        AGENT->>BE: SSE: {type: "log", data: "..."}
        BE->>UI: SSE forward
        UI->>UI: Append to log viewer
    end

    CC->>AGENT: Exit
    AGENT->>BE: SSE: {type: "complete"}
    BE->>UI: Stream end
```

## 13. Phase Implementation Status

```mermaid
graph LR
    subgraph "Completed Phases"
        P1[Phase 1-6<br/>Core Platform<br/>✅]
        P7[Phase 7<br/>GitHub Sync<br/>✅]
        P9[Phase 9<br/>Deep Agent Core<br/>✅]
        P11[Phase 11<br/>Ecosystem<br/>✅]
        P14[Phase 14<br/>Process Engine<br/>✅]
    end

    subgraph "In Progress"
        P10[Phase 10<br/>Memory<br/>🔄]
        P12[Phase 12<br/>Event Bus<br/>🔄]
    end

    subgraph "Planned"
        P13[Phase 13<br/>Scalability]
        P15[Phase 15<br/>Compliance]
    end

    P1 --> P7
    P7 --> P9
    P9 --> P11
    P11 --> P14
    P14 --> P10
    P10 --> P12
    P12 --> P13
    P13 --> P15

    style P1 fill:#4CAF50,stroke:#2E7D32,stroke-width:2px,color:#fff
    style P7 fill:#4CAF50,stroke:#2E7D32,stroke-width:2px,color:#fff
    style P9 fill:#4CAF50,stroke:#2E7D32,stroke-width:2px,color:#fff
    style P11 fill:#4CAF50,stroke:#2E7D32,stroke-width:2px,color:#fff
    style P14 fill:#4CAF50,stroke:#2E7D32,stroke-width:2px,color:#fff
    style P10 fill:#FFC107,stroke:#F57F17,stroke-width:2px,color:#000
    style P12 fill:#FFC107,stroke:#F57F17,stroke-width:2px,color:#000
    style P13 fill:#E0E0E0,stroke:#9E9E9E,stroke-width:2px,color:#000
    style P15 fill:#E0E0E0,stroke:#9E9E9E,stroke-width:2px,color:#000
```

## 14. Dashboard Timeline Visualization

```mermaid
graph TB
    subgraph "Timeline View"
        GRID[Time Grid<br/>15-minute intervals]
        NOW[NOW Marker<br/>90% viewport]
        ROWS[Agent Rows]
    end

    subgraph "Execution Boxes"
        GREEN[Green<br/>Manual Trigger]
        PINK[Pink<br/>MCP Call]
        PURPLE[Purple<br/>Scheduled]
        CYAN[Cyan<br/>Agent-Triggered]
    end

    subgraph "Collaboration"
        ARROWS[Animated Arrows<br/>Agent-to-Agent]
        VALID[30s Tolerance Window<br/>Arrow Validation]
    end

    GRID --> ROWS
    NOW --> GRID

    ROWS --> GREEN
    ROWS --> PINK
    ROWS --> PURPLE
    ROWS --> CYAN

    GREEN --> ARROWS
    PURPLE --> ARROWS
    ARROWS --> VALID

    style GREEN fill:#4CAF50,stroke:#2E7D32,stroke-width:2px,color:#fff
    style PINK fill:#E91E63,stroke:#C2185B,stroke-width:2px,color:#fff
    style PURPLE fill:#9C27B0,stroke:#7B1FA2,stroke-width:2px,color:#fff
    style CYAN fill:#00BCD4,stroke:#0097A7,stroke-width:2px,color:#fff
```

## 15. MCP Server Tools (21 Total)

```mermaid
graph TB
    subgraph "Agent Management"
        T1[list_agents]
        T2[get_agent]
        T3[get_agent_info]
        T4[create_agent]
        T5[delete_agent]
        T6[start_agent]
        T7[stop_agent]
    end

    subgraph "Communication"
        T8[chat_with_agent]
        T9[get_chat_history]
    end

    subgraph "Operations"
        T10[get_agent_logs]
        T11[reload_credentials]
        T12[get_credential_status]
        T13[get_agent_ssh_access]
    end

    subgraph "Templates"
        T14[list_templates]
    end

    subgraph "Process Engine"
        T15[list_processes]
        T16[execute_process]
        T17[get_execution_status]
        T18[list_approvals]
        T19[decide_approval]
    end

    subgraph "System Operations"
        T20[emergency_stop]
        T21[fleet_status]
    end

    style T8 fill:#9C27B0,stroke:#7B1FA2,stroke-width:2px,color:#fff
    style T15 fill:#E91E63,stroke:#C2185B,stroke-width:2px,color:#fff
    style T20 fill:#F44336,stroke:#D32F2F,stroke-width:2px,color:#fff
```

## Key Architecture Principles

### 1. Agent Orchestration
- Agents are autonomous units with planning, memory, and delegation capabilities
- System Agent (`trinity-system`) provides platform-level orchestration
- Process Engine enables complex multi-agent workflows

### 2. Complete Data Isolation
- Each agent has its own Docker container and workspace
- Credentials never shared between agents
- Permission system controls agent-to-agent communication

### 3. Dual Execution Modes
- **Sequential Chat**: Maintains context with `--continue` flag
- **Parallel Task**: Stateless execution for fan-out patterns

### 4. Event-Driven Architecture
- WebSocket broadcasts for real-time updates
- Activity stream tracks all agent operations
- Dashboard visualizes execution timeline

### 5. Process-Driven Workflows
- YAML-based process definitions
- Six step types: agent_task, human_approval, gateway, timer, notification, sub_process
- EMI pattern (Executor/Monitor/Informed) for role assignment

### 6. Production Security
- Container hardening (non-root, cap-drop, apparmor)
- Redis-backed secrets with AOF persistence
- Vector log aggregation for audit trails
- HTTPS with Let's Encrypt in production

## Technology Stack Summary

| Layer | Technology | Purpose |
|-------|------------|---------|
| Frontend | Vue.js 3 + Tailwind | Web UI |
| Backend | FastAPI + Python 3.11 | REST API + WebSocket |
| MCP Server | FastMCP | Agent orchestration protocol |
| Scheduler | APScheduler + Redis locks | Cron automation |
| Database | SQLite | Platform data |
| Secrets | Redis + AOF | Credential storage |
| Logging | Vector | Log aggregation |
| Containers | Docker | Agent isolation |
| Runtime | Claude Code / Gemini CLI | AI agent execution |
