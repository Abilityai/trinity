# Agent2Agent (A2A) Protocol: Technical Reference

**Researched**: 2026-05-08  
**Protocol Version**: v1.0 (released early 2026), v0.3.0 also documented  
**Spec URL**: https://a2a-protocol.org/latest/specification/  
**GitHub**: https://github.com/a2aproject/A2A  
**License**: Apache 2.0 (Linux Foundation)

---

## 1. What It Is and Why It Exists

The **Agent2Agent (A2A) Protocol** is an open standard for communication and interoperability between independent, potentially opaque AI agent systems. Announced by Google in April 2025 and donated to the Linux Foundation in June 2025, it addresses a core problem: agents built on different frameworks (LangChain, ADK, AutoGen, Semantic Kernel, etc.) or by different vendors cannot natively communicate.

### Design Goals

1. **Embrace agentic capabilities**: Agents collaborate in natural, unstructured modalities without sharing internal memory, tools, or context. An A2A server is opaque — clients interact only through the declared interface.
2. **Build on existing standards**: HTTP, JSON-RPC 2.0, Server-Sent Events (SSE). No new transport invented.
3. **Secure by default**: Enterprise-grade authentication with parity to OpenAPI security schemes.
4. **Support long-running tasks**: Everything from millisecond replies to multi-day research tasks, with real-time progress feedback.
5. **Modality agnostic**: Text, files (inline bytes or URI), structured JSON data, UI components.

### Governance

- Originally Google-authored; donated to **Linux Foundation** June 2025
- The **Agentic AI Foundation (AAIF)** was formed December 2025 under the Linux Foundation, with founding members: OpenAI, Anthropic, Google, Microsoft, AWS, Block
- Both A2A and MCP now live under AAIF, making them officially complementary rather than competing
- Technical Steering Committee: AWS, Cisco, Google, IBM Research, Microsoft, Salesforce, SAP, ServiceNow
- As of April 2026: 150+ supporting organizations, production deployments across major cloud platforms

---

## 2. Core Concepts

### 2.1 Participants

```
User → A2A Client (Client Agent) ←HTTP/JSON-RPC→ A2A Server (Remote Agent)
```

| Role | Description |
|------|-------------|
| **A2A Client** | Initiates requests; the "orchestrator" or user-facing agent |
| **A2A Server** | Exposes an HTTP endpoint implementing A2A; executes tasks |
| **Remote Agent** | An opaque agent behind the server — its internals are hidden |

An agent can act as both client and server simultaneously (a common pattern in multi-hop orchestration).

### 2.2 Agent Card

An **Agent Card** is a JSON document served at `/.well-known/agent-card.json` that functions as the agent's "business card." It describes capabilities, skills, security requirements, and service endpoints. Clients fetch this before communicating.

**Discovery**: `GET https://{agent-domain}/.well-known/agent-card.json`

#### v1.0 Agent Card Schema (complete)

```json
{
  "name": "Research Agent",
  "description": "Performs deep web research and synthesizes findings",
  "url": "https://research-agent.example.com/",
  "iconUrl": "https://research-agent.example.com/icon.png",
  "provider": {
    "organization": "Acme Corp",
    "url": "https://acme.com"
  },
  "version": "2.1.0",
  "documentationUrl": "https://research-agent.example.com/docs",
  "capabilities": {
    "streaming": true,
    "pushNotifications": true,
    "extendedAgentCard": true,
    "extensions": [
      {
        "uri": "https://example.com/extensions/citations",
        "description": "Returns citation metadata with results",
        "required": false
      }
    ]
  },
  "interfaces": [
    {
      "protocol": "json-rpc",
      "url": "https://research-agent.example.com/",
      "version": "1.0"
    },
    {
      "protocol": "grpc",
      "url": "https://research-agent.example.com:443/",
      "version": "1.0"
    }
  ],
  "securitySchemes": {
    "bearerAuth": {
      "type": "http",
      "scheme": "bearer",
      "bearerFormat": "JWT"
    },
    "oauth2": {
      "type": "oauth2",
      "flows": {
        "authorizationCode": {
          "authorizationUrl": "https://auth.example.com/authorize",
          "tokenUrl": "https://auth.example.com/token",
          "pkce_required": true,
          "scopes": {
            "research:read": "Read research results",
            "research:write": "Submit research tasks"
          }
        }
      }
    }
  },
  "security": [
    {"bearerAuth": []}
  ],
  "defaultInputModes": ["text/plain", "application/json"],
  "defaultOutputModes": ["text/plain", "text/markdown", "application/json"],
  "skills": [
    {
      "id": "web-research",
      "name": "Web Research",
      "description": "Searches the web and synthesizes findings into structured reports",
      "tags": ["research", "web", "synthesis"],
      "examples": [
        "Research the latest developments in quantum computing",
        "Summarize academic papers on transformer architectures"
      ],
      "inputModes": ["text/plain"],
      "outputModes": ["text/markdown", "application/json"]
    }
  ],
  "signatures": [
    {
      "protected": "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9",
      "signature": "MEUCIQDx...",
      "header": {}
    }
  ]
}
```

**Key v1.0 changes vs v0.3.0:**
- `protocolVersion`, `preferredTransport`, `additionalInterfaces` removed
- `supportsAuthenticatedExtendedCard` moved into `capabilities.extendedAgentCard`
- `interfaces[]` array replaces flat transport config
- Signed Agent Cards added (JWS + RFC 8785 JSON canonicalization)

### 2.3 Task

The **Task** is the fundamental unit of work. It is server-assigned (the server generates `id`), stateful, and progresses through a defined lifecycle.

#### Task Schema (v1.0)

```json
{
  "id": "363422be-b0f9-4692-a24d-278670e7c7f1",
  "contextId": "c295ea44-7543-4f78-b524-7a38915ad6e4",
  "status": {
    "state": "TASK_STATE_COMPLETED",
    "message": null,
    "timestamp": "2026-01-15T10:30:00.000Z"
  },
  "artifacts": [
    {
      "artifactId": "9b6934dd-37e3-4eb1-8766-962efaab63a1",
      "name": "research-report",
      "description": "Synthesized research findings",
      "parts": [
        {
          "text": "## Findings\n\nQuantum computing has advanced...",
          "mediaType": "text/markdown"
        }
      ],
      "metadata": {}
    }
  ],
  "history": [
    {
      "role": "ROLE_USER",
      "messageId": "msg-001",
      "parts": [{"text": "Research quantum computing advances"}],
      "timestamp": "2026-01-15T10:25:00.000Z"
    }
  ],
  "metadata": {},
  "createdAt": "2026-01-15T10:25:00.000Z",
  "lastModified": "2026-01-15T10:30:00.000Z"
}
```

#### Task Lifecycle States (v1.0)

| State (v1.0 SCREAMING_SNAKE_CASE) | Previous (v0.3.0) | Type | Meaning |
|---|---|---|---|
| `TASK_STATE_SUBMITTED` | `submitted` | Active | Acknowledged, queued |
| `TASK_STATE_WORKING` | `working` | Active | Agent actively processing |
| `TASK_STATE_INPUT_REQUIRED` | `input-required` | Interrupted | Agent needs client input to continue |
| `TASK_STATE_AUTH_REQUIRED` | `auth-required` | Interrupted | Authentication needed to proceed |
| `TASK_STATE_COMPLETED` | `completed` | Terminal | Finished successfully |
| `TASK_STATE_FAILED` | `failed` | Terminal | Error occurred |
| `TASK_STATE_CANCELED` | `canceled` | Terminal | Manually stopped |
| `TASK_STATE_REJECTED` | `rejected` | Terminal | Agent declined the task |

**Note**: v0.3.0 used lowercase strings with hyphens (`input-required`). v1.0 uses `SCREAMING_SNAKE_CASE` with prefix. This is a **breaking change** requiring migration.

### 2.4 Message

Messages are the conversational turns within a task. A task has a `history` field containing all messages.

```json
{
  "messageId": "msg-uuid-001",
  "contextId": "c295ea44-7543-4f78-b524-7a38915ad6e4",
  "taskId": "363422be-b0f9-4692-a24d-278670e7c7f1",
  "role": "ROLE_USER",
  "parts": [...],
  "metadata": {},
  "extensions": [],
  "referenceTaskIds": []
}
```

**Roles**: `ROLE_USER` (client-originated), `ROLE_AGENT` (server-originated). Both use `SCREAMING_SNAKE_CASE` in v1.0.

### 2.5 Part (Content Fragment)

Parts are the content containers inside messages and artifacts. v1.0 unified three previously separate types into a single `Part` struct with `oneof content`.

**TextPart:**
```json
{
  "text": "Here is your answer",
  "mediaType": "text/plain",
  "metadata": {}
}
```

**FilePart (inline bytes):**
```json
{
  "file": {
    "name": "report.pdf",
    "mimeType": "application/pdf",
    "bytes": "JVBERi0xLjQ..."
  },
  "metadata": {}
}
```

**FilePart (URI reference):**
```json
{
  "file": {
    "name": "output.png",
    "mimeType": "image/png",
    "uri": "https://storage.example.com/output.png?token=xyz"
  }
}
```

**DataPart (structured JSON):**
```json
{
  "data": {
    "type": "object",
    "content": {
      "ticketNumber": "REQ12312",
      "status": "open",
      "priority": "high"
    }
  },
  "mediaType": "application/json",
  "metadata": {}
}
```

**v1.0 breaking change**: The `kind` discriminator field (`"kind": "text"`, `"kind": "file"`) is removed. Type is determined by which field is present (`"text" in part`, `"file" in part`, `"data" in part`).

### 2.6 Artifact

Artifacts are task **outputs** — the produced results — distinguished from messages (which are conversational turns). Clients should parse artifacts separately from message history.

```json
{
  "artifactId": "9b6934dd-37e3-4eb1-8766-962efaab63a1",
  "name": "generated-image",
  "description": "The requested product photo",
  "parts": [
    {
      "file": {
        "mimeType": "image/png",
        "uri": "https://cdn.example.com/img/abc123.png"
      }
    }
  ],
  "metadata": {},
  "extensions": []
}
```

### 2.7 Context

A `contextId` groups logically related tasks — think of it as a "conversation session" that can span multiple tasks. Clients can provide a `contextId` to continue a conversation; servers may generate one if not provided.

---

## 3. Protocol Transport

### 3.1 Transport Bindings

A2A supports three protocol bindings. All implement the same operations:

| Binding | Transport | Content-Type | Best For |
|---------|-----------|--------------|----------|
| **JSON-RPC 2.0** | HTTP(S) POST | `application/json` | Web apps, simple integrations |
| **HTTP+JSON/REST** | HTTP(S) REST | `application/json` | REST-native infrastructure |
| **gRPC** | HTTP/2 | Protocol Buffers | High-performance, typed systems |

The normative definition is the **Protocol Buffer spec** at `spec/a2a.proto`. All other representations are derived.

### 3.2 JSON-RPC Binding

All requests go to the agent's `url` (from the Agent Card) as HTTP POST:

```
POST https://agent.example.com/
Content-Type: application/json
Authorization: Bearer <token>
A2A-Version: 1.0
```

**Request envelope:**
```json
{
  "jsonrpc": "2.0",
  "id": "req-001",
  "method": "SendMessage",
  "params": { ... }
}
```

**Response envelope:**
```json
{
  "jsonrpc": "2.0",
  "id": "req-001",
  "result": { ... }
}
```

**Error envelope:**
```json
{
  "jsonrpc": "2.0",
  "id": "req-001",
  "error": {
    "code": -32603,
    "message": "Internal error",
    "data": {
      "status": {
        "code": 13,
        "message": "Task execution failed",
        "details": [
          {
            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
            "reason": "TASK_EXECUTION_FAILED",
            "domain": "a2a-protocol.org"
          }
        ]
      }
    }
  }
}
```

**Note on v1.0 error format**: Replaced RFC 9457 `application/problem+json` with `google.rpc.Status` + `google.rpc.ErrorInfo`. Content-Type for errors is now `application/json`.

### 3.3 HTTP+JSON REST Binding

| JSON-RPC Method | REST Endpoint | HTTP Verb |
|---|---|---|
| `SendMessage` | `POST /message:send` | POST |
| `SendStreamingMessage` | `POST /message:stream` | POST |
| `GetTask` | `GET /tasks/{taskId}` | GET |
| `ListTasks` | `GET /tasks` | GET |
| `CancelTask` | `POST /tasks/{taskId}:cancel` | POST |
| `SubscribeToTask` | `GET /tasks/{taskId}/subscribe` | GET (SSE) |
| `CreatePushNotificationConfig` | `POST /tasks/{taskId}/pushNotificationConfigs` | POST |
| `GetPushNotificationConfig` | `GET /tasks/{taskId}/pushNotificationConfigs/{configId}` | GET |
| `ListPushNotificationConfigs` | `GET /tasks/{taskId}/pushNotificationConfigs` | GET |
| `DeletePushNotificationConfig` | `DELETE /tasks/{taskId}/pushNotificationConfigs/{configId}` | DELETE |
| `GetExtendedAgentCard` | `GET /agentCard/extended` | GET |

**Note**: The `/v1` prefix from v0.3.0 was removed in v1.0.

### 3.4 Service Parameters (Headers)

Standard headers sent on every request:

| Header | Purpose | Example |
|--------|---------|---------|
| `A2A-Version` | Protocol version negotiation | `A2A-Version: 1.0` |
| `A2A-Extensions` | Comma-separated extension URIs client supports | `A2A-Extensions: https://ex.com/ext/citations` |
| `Authorization` | Authentication credential | `Authorization: Bearer eyJ...` |

An empty `A2A-Version` header defaults to version 0.3 for backward compatibility.

---

## 4. RPC Methods (Complete Reference)

### v1.0 Method Names

**Note**: v0.3.0 used slash-separated lowercase (`message/send`, `tasks/get`). v1.0 uses PascalCase (`SendMessage`, `GetTask`). This is a **breaking change**.

| v1.0 Method | v0.3.0 Method | Purpose |
|---|---|---|
| `SendMessage` | `message/send` | Send message, wait for final Task or Message response |
| `SendStreamingMessage` | `message/stream` | Send message, receive SSE stream of updates |
| `GetTask` | `tasks/get` | Retrieve current task state + history |
| `ListTasks` | `tasks/list` | Query multiple tasks with filtering (new in v1.0) |
| `CancelTask` | `tasks/cancel` | Request task cancellation |
| `SubscribeToTask` | `tasks/resubscribe` | Resume SSE stream for existing task |
| `CreateTaskPushNotificationConfig` | `tasks/pushNotificationConfig/set` | Register webhook |
| `GetTaskPushNotificationConfig` | `tasks/pushNotificationConfig/get` | Retrieve webhook config |
| `ListTaskPushNotificationConfigs` | *(new)* | List all webhook configs for task |
| `DeleteTaskPushNotificationConfig` | `tasks/pushNotificationConfig/delete` | Remove webhook |
| `GetExtendedAgentCard` | `agent/getAuthenticatedExtendedCard` | Fetch authenticated extended agent card |

### 4.1 SendMessage

Send a message to the agent. The server responds with either a completed `Task` or a direct `Message` (for lightweight, stateless responses that don't need task tracking).

**Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "SendMessage",
  "params": {
    "message": {
      "role": "ROLE_USER",
      "messageId": "9229e770-767c-417b-a0b0-f0741243c589",
      "parts": [
        {"text": "Tell me a joke"}
      ]
    },
    "configuration": {
      "acceptedOutputModes": ["text/plain"],
      "historyLength": 10,
      "returnImmediately": false
    },
    "metadata": {}
  }
}
```

**Response (Task):**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "id": "363422be-b0f9-4692-a24d-278670e7c7f1",
    "contextId": "c295ea44-7543-4f78-b524-7a38915ad6e4",
    "status": {"state": "TASK_STATE_COMPLETED", "timestamp": "2026-01-15T10:30:00.000Z"},
    "artifacts": [
      {
        "artifactId": "9b6934dd-37e3-4eb1-8766-962efaab63a1",
        "name": "joke",
        "parts": [{"text": "Why did the chicken cross the road? To get to the other side!"}]
      }
    ],
    "kind": "task"
  }
}
```

**Response (Direct Message, no task):**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "messageId": "363422be-b0f9-4692-a24d-278670e7c7f1",
    "contextId": "c295ea44-7543-4f78-b524-7a38915ad6e4",
    "role": "ROLE_AGENT",
    "parts": [{"text": "Why did the chicken cross the road?..."}],
    "kind": "message"
  }
}
```

**`returnImmediately: true`** causes the server to return immediately with a `TASK_STATE_SUBMITTED` or `TASK_STATE_WORKING` task, then the client polls via `GetTask`.

### 4.2 SendStreamingMessage

Same request format as `SendMessage`. Response is SSE:

```
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive

data: {"jsonrpc":"2.0","id":1,"result":{"id":"225d6247-...","status":{"state":"TASK_STATE_SUBMITTED","timestamp":"..."},"kind":"task"}}

data: {"jsonrpc":"2.0","id":1,"result":{"taskId":"225d6247-...","contextId":"05217e44-...","status":{"state":"TASK_STATE_WORKING","timestamp":"..."},"kind":"taskStatusUpdate"}}

data: {"jsonrpc":"2.0","id":1,"result":{"taskId":"225d6247-...","contextId":"05217e44-...","artifact":{"artifactId":"9b69...","parts":[{"text":"Chapter 1..."}]},"append":false,"lastChunk":false,"kind":"artifactUpdate"}}

data: {"jsonrpc":"2.0","id":1,"result":{"taskId":"225d6247-...","contextId":"05217e44-...","artifact":{"artifactId":"9b69...","parts":[{"text":" continued..."}]},"append":true,"lastChunk":true,"kind":"artifactUpdate"}}

data: {"jsonrpc":"2.0","id":1,"result":{"taskId":"225d6247-...","contextId":"05217e44-...","status":{"state":"TASK_STATE_COMPLETED","timestamp":"..."},"kind":"taskStatusUpdate"}}
```

**SSE Event Types:**

| `kind` (v1.0) | Payload | Meaning |
|---|---|---|
| `task` | Task object | Initial task creation; appears first in stream |
| `message` | Message object | Direct message response (no task tracking) |
| `taskStatusUpdate` | TaskStatusUpdateEvent | State transition |
| `artifactUpdate` | TaskArtifactUpdateEvent | New or partial artifact chunk |

**Artifact streaming**: `append: false, lastChunk: false` = new artifact, more chunks coming; `append: true` = append to existing artifact; `lastChunk: true` = this is the last chunk for this artifact.

**Note**: v1.0 removed the `final` boolean field from events; stream closure indicates completion instead.

### 4.3 GetTask

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "GetTask",
  "params": {
    "id": "363422be-b0f9-4692-a24d-278670e7c7f1",
    "historyLength": 20
  }
}
```

### 4.4 ListTasks (new in v1.0)

Cursor-based pagination:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "ListTasks",
  "params": {
    "contextId": "c295ea44-7543-4f78-b524-7a38915ad6e4",
    "status": "TASK_STATE_COMPLETED",
    "statusTimestampAfter": "2026-01-01T00:00:00.000Z",
    "pageSize": 50,
    "pageToken": "cursor-from-previous-response",
    "includeArtifacts": false
  }
}
```

Response includes `nextPageToken` (empty string when no more results).

### 4.5 Multi-Turn: INPUT_REQUIRED

When an agent needs clarification:

```json
{
  "result": {
    "id": "3f36680c-7f37-4a5f-945e-d78981fafd36",
    "status": {
      "state": "TASK_STATE_INPUT_REQUIRED",
      "message": {
        "role": "ROLE_AGENT",
        "parts": [{"text": "Which departure city should I book from?"}]
      }
    }
  }
}
```

Client responds by calling `SendMessage` again with the **same `taskId`** and `contextId`:

```json
{
  "params": {
    "message": {
      "role": "ROLE_USER",
      "taskId": "3f36680c-7f37-4a5f-945e-d78981fafd36",
      "contextId": "c295ea44-...",
      "parts": [{"text": "San Francisco"}]
    }
  }
}
```

---

## 5. Authentication & Authorization

### 5.1 Authentication Declaration (Agent Card)

Security schemes declared in the Agent Card follow OpenAPI 3.0 patterns:

```json
{
  "securitySchemes": {
    "apiKey": {
      "type": "apiKey",
      "in": "header",
      "name": "X-API-Key"
    },
    "bearerJWT": {
      "type": "http",
      "scheme": "bearer",
      "bearerFormat": "JWT"
    },
    "oauth2Flow": {
      "type": "oauth2",
      "flows": {
        "authorizationCode": {
          "authorizationUrl": "https://auth.example.com/authorize",
          "tokenUrl": "https://auth.example.com/token",
          "pkce_required": true,
          "scopes": {"agent:chat": "Chat with agent"}
        },
        "clientCredentials": {
          "tokenUrl": "https://auth.example.com/token",
          "scopes": {"agent:chat": "Chat with agent"}
        },
        "deviceCode": {
          "deviceAuthorizationUrl": "https://auth.example.com/device",
          "tokenUrl": "https://auth.example.com/token",
          "scopes": {"agent:chat": "Chat with agent"}
        }
      }
    },
    "oidc": {
      "type": "openIdConnect",
      "openIdConnectUrl": "https://auth.example.com/.well-known/openid-configuration"
    },
    "mtls": {
      "type": "mutualTLS"
    }
  },
  "security": [
    {"bearerJWT": []},
    {"oauth2Flow": ["agent:chat"]}
  ]
}
```

**v1.0 OAuth changes**: Implicit and Password flows removed (deprecated). Device Code flow (RFC 8628) added for CLI/IoT. `pkce_required` added to Authorization Code flow.

### 5.2 Extended Agent Cards

An agent can serve two versions of its Agent Card:

1. **Public card** (`/.well-known/agent-card.json`): Safe for unauthenticated access; minimal skill information; `capabilities.extendedAgentCard: true` signals more is available
2. **Extended card** (`GET /agentCard/extended` or via `GetExtendedAgentCard` RPC): Requires authentication; may expose additional skills, detailed schemas, or configuration

### 5.3 Push Notification Security

When using webhooks, the A2A server authenticates *to the client's webhook*. The server:
- Signs payloads as JWTs using a private key
- Exposes JWKS endpoint for client to verify signatures
- Includes `taskId`, `iat`, `exp`, `jti` claims for replay prevention

Client security for webhooks:
- Verify JWT signature against JWKS
- Check `jti` (JWT ID) for replay prevention
- Validate `taskId` claim matches expected task

---

## 6. Push vs Pull Delivery

A2A supports three delivery patterns, and clients can use multiple simultaneously:

| Pattern | Mechanism | Latency | Connection | Recommended For |
|---------|-----------|---------|------------|-----------------|
| **Polling** | `GetTask` on interval | High | None required | Simple integrations, restrictive networks |
| **Streaming** | SSE via `SendStreamingMessage` | Low | Persistent HTTP | Interactive apps, live progress |
| **Push** | HTTP POST to webhook | Variable | None required | Server-to-server, long-running, mobile/serverless |

### Push Notification Config

```json
{
  "jsonrpc": "2.0",
  "method": "SendMessage",
  "params": {
    "message": {
      "role": "ROLE_USER",
      "parts": [{"text": "Generate annual report"}]
    },
    "configuration": {
      "returnImmediately": true,
      "taskPushNotificationConfig": {
        "webhookUrl": "https://client.example.com/webhook/a2a",
        "token": "client-secret-validation-token",
        "authenticationInfo": {
          "schemes": ["Bearer"],
          "credentials": "expected-bearer-token"
        }
      }
    }
  }
}
```

**Webhook payload** (HTTP POST to `webhookUrl`):
```json
{
  "id": "43667960-d455-4453-b0cf-1bae4955270d",
  "contextId": "c295ea44-7543-4f78-b524-7a38915ad6e4",
  "status": {"state": "TASK_STATE_COMPLETED", "timestamp": "2026-01-15T10:30:00.000Z"},
  "kind": "task"
}
```

---

## 7. Discovery Mechanism

### 7.1 Well-Known URI (Primary)

Following RFC 8615:

```
GET https://{agent-domain}/.well-known/agent-card.json
```

Returns the Agent Card JSON document. No authentication required for the public card. Servers should include `Cache-Control: max-age=3600` and `ETag` headers.

### 7.2 Client Workflow

```
1. Know agent domain (out-of-band, registry, or configured)
2. GET /.well-known/agent-card.json
3. Parse capabilities, security schemes, skills
4. Acquire credentials per declared security scheme
5. POST to agent.url with JSON-RPC request + auth headers
```

### 7.3 Registry Discovery (Optional)

The spec mentions but does not mandate centralized registries. Registries can provide:
- Capability-based querying ("find an agent that can do OCR")
- Organization-wide agent directories
- Selective disclosure (only show agents to authorized clients)

### 7.4 Direct Configuration

For private systems, the Agent Card URL or inline card can be hardcoded in client config — no well-known endpoint required.

---

## 8. Streaming Support

### 8.1 SSE Architecture

```
Client                              Server
  │                                   │
  ├── POST /message:stream ─────────> │
  │   (or SendStreamingMessage RPC)   │
  │                                   │  (agent starts processing)
  │ <── HTTP 200 text/event-stream ───┤
  │ <── data: Task(submitted) ────────┤
  │ <── data: TaskStatusUpdate(work)─ ┤
  │ <── data: ArtifactUpdate(chunk1)─ ┤
  │ <── data: ArtifactUpdate(chunk2)─ ┤
  │ <── data: TaskStatusUpdate(done)─ ┤
  │   (connection closes)             │
```

### 8.2 SubscribeToTask (Reconnection)

If an SSE connection drops while a task is in progress, the client can resubscribe:

```json
{
  "method": "SubscribeToTask",
  "params": {
    "id": "363422be-b0f9-4692-a24d-278670e7c7f1"
  }
}
```

Returns: Initial Task state + stream of remaining events.

Errors:
- `UnsupportedOperationError` — agent doesn't support streaming
- Task in terminal state — stream opens and closes immediately

### 8.3 Concurrent Streams

Multiple clients can subscribe to the same task simultaneously. The server broadcasts **identical events in order** to all subscribers. Closing one stream does not affect others.

---

## 9. Versioning & Extensions

### 9.1 Version Negotiation

Clients send `A2A-Version: 1.0` header. Servers:
- Process using semantics of the requested version
- Return `VersionNotSupportedError` if version is unsupported
- May expose multiple interfaces with different versions declared in Agent Card

Empty header = v0.3 semantics (backward compatibility).

### 9.2 Extension Mechanism

Extensions allow protocol augmentation without breaking changes:

**Agent Card declaration:**
```json
{
  "capabilities": {
    "extensions": [
      {
        "uri": "https://example.com/a2a-ext/citations",
        "description": "Adds citation metadata to artifacts",
        "required": false,
        "data": {"maxCitations": 20}
      }
    ]
  }
}
```

Client declares supported extensions via `A2A-Extensions` header. If a required extension is not declared, the server returns `ExtensionSupportRequiredError`.

---

## 10. Error Codes Reference

### A2A-Specific Errors

| Error | Meaning |
|-------|---------|
| `TaskNotFoundError` | Task ID invalid, expired, or inaccessible |
| `TaskNotCancelableError` | Task is in terminal state |
| `PushNotificationNotSupportedError` | Agent's `capabilities.pushNotifications` is false |
| `UnsupportedOperationError` | Operation not supported (e.g., streaming on non-streaming agent) |
| `ContentTypeNotSupportedError` | Requested media type not in `acceptedOutputModes` |
| `InvalidAgentResponseError` | Agent's own response violated the spec |
| `ExtendedAgentCardNotConfiguredError` | Extended card requested but not configured |
| `ExtensionSupportRequiredError` | Required extension not declared by client |
| `VersionNotSupportedError` | Requested A2A-Version not supported |

### HTTP Status Mappings

| Code | Meaning |
|------|---------|
| 401 | Missing or invalid authentication |
| 403 | Authenticated but insufficient permissions |
| 400 | Malformed request or invalid parameters |
| 404 | Resource not found |
| 500 | Internal server error |
| 503 | Temporary unavailability |

---

## 11. Comparison to MCP (Model Context Protocol)

MCP and A2A are **complementary** — the AAIF/Linux Foundation now governs both. They solve different problems at different layers.

| Dimension | A2A | MCP |
|-----------|-----|-----|
| **Purpose** | Agent ↔ Agent collaboration | Agent → Tool/Resource access |
| **Communication model** | Peer-to-peer, async, stateful | Client-server, request/response |
| **State** | Task-based lifecycle, persistent | Stateless calls |
| **Streaming** | First-class (SSE) | Not primary |
| **Discovery** | Agent Card at well-known URI | Protocol initialization (handshake) |
| **Identity** | OpenAPI security schemes | Server-declared capabilities |
| **Multi-turn** | Native (`INPUT_REQUIRED` state) | Not built-in |
| **Artifacts** | Explicit output type | Returns in tool results |
| **Typical use** | "Ask agent-B to research X" | "Call calculator tool with 2+2" |

**The complementary stack:**

```
Agent (orchestrator)
  │
  ├── A2A → Remote Agent A (opaque, different framework)
  │           └── MCP → SQLite tool
  │           └── MCP → Web search tool
  │
  ├── A2A → Remote Agent B (specialized, vendor X)
  │
  └── MCP → Local file tool
  └── MCP → Database tool
```

An orchestrating agent uses **A2A to delegate tasks to other agents** and **MCP to access tools for its own tasks**. A sub-agent itself also uses MCP internally.

---

## 12. Open Source Status

| Item | Detail |
|------|--------|
| **GitHub** | https://github.com/a2aproject/A2A |
| **Original Google repo** | https://github.com/google/A2A (redirects) |
| **Spec** | https://a2a-protocol.org/latest/specification/ |
| **License** | Apache 2.0 |
| **Governance** | Linux Foundation / AAIF Technical Steering Committee |
| **Current version** | v1.0 (released early 2026) |
| **Previous stable** | v0.3.0 |

### Official SDKs (all Apache 2.0)

| Language | Repo | Status |
|----------|------|--------|
| Python | https://github.com/a2aproject/a2a-python | Production |
| JavaScript/TypeScript | (a2aproject org) | Production |
| Java | (a2aproject org) | Production |
| Go | (a2aproject org) | Production |
| .NET/C# | (a2aproject org) | Production |

### Platform Support

- **Google**: ADK (Agent Development Kit) — native A2A; Vertex AI Agent Engine
- **Microsoft**: Azure AI Foundry, Copilot Studio
- **AWS**: Amazon Bedrock AgentCore (A2A is a first-class protocol)
- **LangChain**: LangSmith agent server API supports A2A
- **150+ organizations** in production as of April 2026

### Python SDK Quick Example

**Server:**
```python
import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentCapabilities, AgentCard, AgentSkill, AgentInterface
from a2a.utils import new_agent_text_message

class MyAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message_text = context.message.parts[0].text
        result = f"Processed: {message_text}"
        await event_queue.enqueue_event(new_agent_text_message(result))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        pass

skill = AgentSkill(
    id='process',
    name='Process Request',
    description='Process any text request',
    tags=['general'],
    examples=['Process this data'],
)

card = AgentCard(
    name='My Agent',
    description='A sample A2A agent',
    url='http://0.0.0.0:9999/',
    version='1.0.0',
    defaultInputModes=['text/plain'],
    defaultOutputModes=['text/plain'],
    capabilities=AgentCapabilities(streaming=True),
    skills=[skill],
)

server = A2AStarletteApplication(
    agent_card=card,
    http_handler=DefaultRequestHandler(
        agent_executor=MyAgentExecutor(),
        task_store=InMemoryTaskStore(),
    ),
)

if __name__ == '__main__':
    uvicorn.run(server.build(), host='0.0.0.0', port=9999)
```

**Client:**
```python
import asyncio, httpx
from a2a.client import A2ACardResolver, A2AClient
from a2a.types import SendMessageRequest, MessageSendParams
from uuid import uuid4

async def main():
    async with httpx.AsyncClient() as http:
        resolver = A2ACardResolver(httpx_client=http, base_url='http://localhost:9999')
        card = await resolver.get_agent_card()
        client = A2AClient(httpx_client=http, agent_card=card)

        request = SendMessageRequest(
            id=str(uuid4()),
            params=MessageSendParams(
                message={
                    'role': 'ROLE_USER',
                    'messageId': uuid4().hex,
                    'parts': [{'text': 'hello'}],
                }
            )
        )
        response = await client.send_message(request)
        print(response)

asyncio.run(main())
```

---

## 13. Trinity Integration Analysis

### 13.1 Conceptual Fit

Trinity is already doing most of what A2A formalizes, but through its own proprietary protocol (EVT-001 Redis Streams bus + MCP `chat_with_agent` tool). An A2A integration would position Trinity agents as **first-class citizens in the broader agent ecosystem** rather than a closed platform.

| Trinity Concept | A2A Equivalent |
|----------------|----------------|
| `chat_with_agent` MCP tool | `SendMessage` / `SendStreamingMessage` |
| `EVT-001` Redis Streams events | A2A push notifications / SSE |
| `agent_permissions` table | A2A security schemes + `security` rules |
| `agent_schedules` | No direct equivalent; closest is `returnImmediately: true` + poll |
| `agent_activities` table | A2A task history + task lifecycle |
| `operator_queue` (INPUT_REQUIRED) | `TASK_STATE_INPUT_REQUIRED` |
| `agent_sharing` (ACL) | A2A's `securitySchemes` + `security` field |
| Agent templates / CLAUDE.md | A2A `skills` array in Agent Card |
| `trinity.agent-name` Docker label | A2A `AgentCard.name` |

### 13.2 Integration Options

**Option A: A2A Server Per Agent (Recommended)**

Each Trinity agent exposes a `/a2a/` endpoint on its agent-server (port 8000 internal). The backend or nginx proxies `GET /.well-known/agent-card.json` and `POST /a2a/` for external A2A clients.

- Agents become discoverable by any A2A-compatible orchestrator
- Agent Card generated from `template.yaml` + skill definitions
- Reuses existing `agent-server.py` FastAPI app
- Trinity becomes an A2A Server host for its fleet

**Option B: A2A Proxy at Backend Level**

The Trinity backend exposes a single A2A gateway at `https://{domain}/a2a/{agent-name}/` with Agent Cards generated dynamically per agent.

- Centralized auth and routing
- Simpler agent-server changes
- Less per-agent isolation

**Option C: A2A Client Only (Trinity as Orchestrator)**

Trinity agents call external A2A agents using the A2A client protocol via a new MCP tool or a new service layer.

- MCP tool: `call_a2a_agent(agent_card_url, message, ...)`
- Reuses existing `agent_permissions` access control
- No server-side changes to Trinity agents needed

**Recommended path**: Start with **Option C** (client-only, low risk, high value) to enable Trinity agents to call external A2A agents. Follow with **Option A** to make Trinity agents callable by external orchestrators.

### 13.3 Concrete Implementation Sketch

#### A2A Client Tool (new MCP tool)

A new MCP tool `call_a2a_agent` could work as follows:

1. Accept `agent_card_url` (or inline card JSON), `message`, optional `context_id`
2. Fetch Agent Card from `/.well-known/agent-card.json`
3. Negotiate auth per declared schemes (API key/Bearer stored in agent `.env`)
4. Call `SendStreamingMessage` via SSE, streaming responses back to the calling agent
5. Map A2A `TASK_STATE_INPUT_REQUIRED` to a blocking prompt in the current chat session
6. Store task state in Redis (keyed by `a2a:task:{taskId}`) for long-running tasks

This maps cleanly onto the existing `task_execution_service.py` lifecycle.

#### A2A Server (exposing Trinity agents)

A Trinity A2A server endpoint would:

1. Serve Agent Card at `/.well-known/agent-card.json` by reading the agent's `template.yaml`, `CLAUDE.md`, and skill definitions
2. Accept `POST /` (JSON-RPC) and route to existing `/api/chat` endpoint in `agent-server.py`
3. Map Trinity's async task model (`schedule_executions` table) to A2A Task states
4. Map `EVT-001` events to A2A SSE events via a thin adapter on the Redis Streams bus
5. Use `agent_sharing` + `agent_permissions` to enforce A2A security requirements
6. Auth: Generate Bearer tokens via existing MCP key infrastructure

#### EVT-001 → A2A Push Notification Mapping

```
EVT-001 Redis event:            → A2A webhook POST:
{type: "agent_activity",         {id: taskId,
 activity_state: "completed"}      status: {state: "TASK_STATE_COMPLETED"}}

{type: "agent_activity",         {id: taskId,
 activity_state: "started"}       status: {state: "TASK_STATE_WORKING"}}
```

The existing `StreamDispatcher` in `services/event_bus.py` could be extended to also POST to registered A2A webhook URLs.

### 13.4 What A2A Gives Trinity

1. **External discoverability**: External orchestrators (Google ADK, Azure Copilot, AWS Bedrock) can find and call Trinity agents
2. **Inbound routing from other agent frameworks**: LangChain, AutoGen, etc. can call Trinity agents without knowing Trinity's internal API
3. **Outbound federation**: Trinity agents can call agents hosted elsewhere (no vendor lock-in for sub-agents)
4. **Standard auth story**: OAuth2/Bearer replaces Trinity-specific MCP API keys for cross-platform scenarios
5. **Industry credibility**: 150+ organizations including Google, Microsoft, AWS adoption

### 13.5 Risks and Considerations

| Risk | Mitigation |
|------|-----------|
| v0.3.0 vs v1.0 method name differences | Target v1.0, add a v0.3 compat shim |
| Enum case changes (lowercase → SCREAMING_SNAKE_CASE) | Normalize at ingress/egress layer |
| `kind` discriminator removed in v1.0 | Parse by field presence, not `kind` |
| A2A Task IDs are server-generated UUIDs | Map to Trinity's `schedule_executions.id` |
| Security: unauthenticated Agent Card access | Serve minimal card publicly; gated extended card needs agent auth |
| Long-running tasks: A2A `WORKING` vs Trinity schedule execution | Use `returnImmediately: true` + push notifications for async flows |
| Context management: A2A `contextId` vs Trinity `chat_session_id` | Generate Trinity session from A2A contextId, persist mapping |

---

## 14. References

- **Official spec**: https://a2a-protocol.org/latest/specification/
- **v1.0 announcement**: https://a2a-protocol.org/latest/announcing-1.0/
- **What's new in v1.0**: https://a2a-protocol.org/latest/whats-new-v1/
- **GitHub**: https://github.com/a2aproject/A2A
- **Python SDK**: https://github.com/a2aproject/a2a-python
- **Sample apps**: https://github.com/a2aproject/a2a-samples
- **Google ADK A2A docs**: https://adk.dev/a2a/
- **Google announcement blog**: https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/
- **Linux Foundation announcement**: https://www.linuxfoundation.org/press/linux-foundation-launches-the-agent2agent-protocol-project-to-enable-secure-intelligent-communication-between-ai-agents/
- **AWS Bedrock A2A contract**: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-a2a-protocol-contract.html
- **A2A vs MCP (official)**: https://a2a-protocol.org/latest/topics/a2a-and-mcp/
- **Streaming details**: https://a2a-protocol.org/latest/topics/streaming-and-async/
- **Agent discovery**: https://a2a-protocol.org/latest/topics/agent-discovery/
