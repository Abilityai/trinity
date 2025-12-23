# Public Agent Links - Requirements

> **Status**: Draft
> **Created**: 2025-12-22
> **Priority**: High
> **Requirement ID**: 12.2

## Overview

Enable agent owners to generate shareable links that allow anyone to chat with their agents without requiring Trinity platform authentication. Optionally require email verification for accountability.

## User Stories

1. **As an agent owner**, I want to generate a shareable link so that anyone can interact with my agent without needing a Trinity account.

2. **As an agent owner**, I want to optionally require email verification so that I know who is using my shared agent.

3. **As a public user**, I want to chat with an agent using just a link, without creating an account.

4. **As an agent owner**, I want to revoke public links at any time to control access.

## Architecture

### Access Modes (per link)

| Mode | Description |
|------|-------------|
| **Open** | Anyone with link can chat immediately |
| **Email Verified** | User must verify email before chatting |

### User Flow - Open Mode

```
User opens link ──► Minimal chat UI ──► Chat immediately
```

### User Flow - Email Verified Mode

```
┌─────────────────────────────────────────────────────────────────┐
│  1. User opens /chat/{token}                                     │
│       │                                                          │
│       ▼                                                          │
│  2. UI checks if link requires email verification                │
│       │                                                          │
│       ├── No  ──► Show chat immediately                         │
│       │                                                          │
│       └── Yes ──► Show email input form                         │
│                    │                                             │
│                    ▼                                             │
│  3. User enters email, clicks "Send Code"                       │
│       │                                                          │
│       ▼                                                          │
│  POST /api/public/verify/request ─► Send 6-digit code via email │
│       │                                                          │
│       ▼                                                          │
│  4. User enters code from email                                  │
│       │                                                          │
│       ▼                                                          │
│  POST /api/public/verify/confirm ─► Validate code, return session│
│       │                                                          │
│       ▼                                                          │
│  5. Chat unlocked (session token stored in localStorage)        │
└─────────────────────────────────────────────────────────────────┘
```

### System Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Public User (no auth)                                        │
│       │                                                       │
│       ▼                                                       │
│  /chat/{token}  ─────► Minimal Chat UI (no navbar/auth)      │
│       │                                                       │
│       ▼                                                       │
│  [Optional] Email verification flow                          │
│       │                                                       │
│       ▼                                                       │
│  POST /api/public/chat/{token}  ─────► Validate token        │
│       │                                                       │
│       ▼                                                       │
│  Execute via headless task (parallel/stateless mode)         │
│       │                                                       │
│       ▼                                                       │
│  Response returned to user                                   │
└──────────────────────────────────────────────────────────────┘
```

## Database Schema

### agent_public_links

Stores shareable link configurations.

```sql
CREATE TABLE agent_public_links (
    id TEXT PRIMARY KEY,              -- UUID
    agent_name TEXT NOT NULL,         -- Target agent
    token TEXT UNIQUE NOT NULL,       -- Shareable token (urlsafe, 32 chars)
    created_by TEXT NOT NULL,         -- User ID who created the link
    created_at TEXT NOT NULL,         -- ISO timestamp
    expires_at TEXT,                  -- Optional expiration (ISO timestamp)
    enabled INTEGER DEFAULT 1,        -- 0=disabled, 1=enabled
    name TEXT,                        -- Optional friendly name for the link
    require_email INTEGER DEFAULT 0,  -- 0=open access, 1=email verification required
    FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name) ON DELETE CASCADE,
    FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE INDEX idx_public_links_token ON agent_public_links(token);
CREATE INDEX idx_public_links_agent ON agent_public_links(agent_name);
```

### public_link_verifications

Stores email verification codes (short-lived).

```sql
CREATE TABLE public_link_verifications (
    id TEXT PRIMARY KEY,              -- UUID
    link_id TEXT NOT NULL,            -- Reference to public link
    email TEXT NOT NULL,              -- User's email
    code TEXT NOT NULL,               -- 6-digit verification code
    created_at TEXT NOT NULL,         -- ISO timestamp
    expires_at TEXT NOT NULL,         -- Expiration (10 minutes from creation)
    verified INTEGER DEFAULT 0,       -- 0=pending, 1=verified
    session_token TEXT,               -- Issued after successful verification
    session_expires_at TEXT,          -- Session expiration (e.g., 24 hours)
    FOREIGN KEY (link_id) REFERENCES agent_public_links(id) ON DELETE CASCADE
);

CREATE INDEX idx_verifications_link ON public_link_verifications(link_id);
CREATE INDEX idx_verifications_email ON public_link_verifications(email);
CREATE INDEX idx_verifications_code ON public_link_verifications(code);
```

### public_link_usage

Tracks usage for analytics and abuse detection.

```sql
CREATE TABLE public_link_usage (
    id TEXT PRIMARY KEY,              -- UUID
    link_id TEXT NOT NULL,            -- Reference to public link
    email TEXT,                       -- NULL if open mode, email if verified
    ip_address TEXT,                  -- Client IP for rate limiting
    message_count INTEGER DEFAULT 0,  -- Number of messages sent
    created_at TEXT NOT NULL,         -- First usage timestamp
    last_used_at TEXT,                -- Most recent usage
    FOREIGN KEY (link_id) REFERENCES agent_public_links(id) ON DELETE CASCADE
);

CREATE INDEX idx_usage_link ON public_link_usage(link_id);
CREATE INDEX idx_usage_ip ON public_link_usage(ip_address);
```

## API Endpoints

### Owner Endpoints (Authenticated)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/agents/{name}/public-links` | Create a public link |
| GET | `/api/agents/{name}/public-links` | List all public links for agent |
| GET | `/api/agents/{name}/public-links/{id}` | Get specific link details |
| PUT | `/api/agents/{name}/public-links/{id}` | Update link (enable/disable, expiry) |
| DELETE | `/api/agents/{name}/public-links/{id}` | Revoke/delete link |

### Public Endpoints (Unauthenticated)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/public/link/{token}` | Get link info (requires_email, enabled, expired) |
| POST | `/api/public/verify/request` | Request email verification code |
| POST | `/api/public/verify/confirm` | Verify code and get session token |
| POST | `/api/public/chat/{token}` | Send chat message |

## Request/Response Models

### Create Public Link

**Request**: `POST /api/agents/{name}/public-links`
```json
{
  "name": "Customer Support Demo",
  "require_email": true,
  "expires_at": "2025-03-01T00:00:00Z"
}
```

**Response**: `201 Created`
```json
{
  "id": "uuid",
  "agent_name": "support-bot",
  "token": "abc123xyz789...",
  "url": "https://trinity.example.com/chat/abc123xyz789...",
  "name": "Customer Support Demo",
  "require_email": true,
  "expires_at": "2025-03-01T00:00:00Z",
  "enabled": true,
  "created_at": "2025-12-22T10:00:00Z"
}
```

### Get Link Info (Public)

**Request**: `GET /api/public/link/{token}`

**Response**: `200 OK`
```json
{
  "valid": true,
  "require_email": true,
  "agent_available": true
}
```

**Error Response**: `404 Not Found` (invalid/expired/disabled token)
```json
{
  "valid": false,
  "reason": "expired" | "disabled" | "not_found"
}
```

### Request Email Verification

**Request**: `POST /api/public/verify/request`
```json
{
  "token": "abc123xyz789...",
  "email": "user@example.com"
}
```

**Response**: `200 OK`
```json
{
  "message": "Verification code sent",
  "expires_in_seconds": 600
}
```

### Confirm Email Verification

**Request**: `POST /api/public/verify/confirm`
```json
{
  "token": "abc123xyz789...",
  "email": "user@example.com",
  "code": "123456"
}
```

**Response**: `200 OK`
```json
{
  "verified": true,
  "session_token": "session_xxx...",
  "expires_at": "2025-12-23T10:00:00Z"
}
```

### Public Chat

**Request**: `POST /api/public/chat/{token}`
```json
{
  "message": "Hello, I need help with...",
  "session_token": "session_xxx..."  // Required if require_email=true
}
```

**Response**: `200 OK`
```json
{
  "response": "Hi! I'd be happy to help you with...",
  "usage": {
    "input_tokens": 150,
    "output_tokens": 200
  }
}
```

## Frontend Components

### Routes

| Route | Component | Auth Required |
|-------|-----------|---------------|
| `/chat/:token` | `PublicChat.vue` | No |
| AgentDetail tab | `PublicLinksPanel.vue` | Yes (owner) |

### PublicChat.vue

Minimal chat interface for public users:

- No navbar, no Trinity branding (anonymous mode per requirements)
- Email verification form (if required)
- Simple chat input/output
- Mobile responsive
- Stateless (no conversation history displayed)

**States**:
1. Loading (checking link validity)
2. Invalid/Expired link error
3. Email verification form (if required)
4. Code input form (after email sent)
5. Chat interface (after verification or if open mode)

### PublicLinksPanel.vue

Management interface for agent owners:

- List of all public links with status
- Create new link button
- Toggle enable/disable
- Copy link button
- Delete link button
- Usage statistics per link

## Email Service

### Configuration

New environment variables:
```bash
# Email service configuration
EMAIL_PROVIDER=smtp  # or "sendgrid", "ses", etc.
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=noreply@example.com
SMTP_PASSWORD=xxx
SMTP_FROM=noreply@trinity.example.com

# Optional: SendGrid
SENDGRID_API_KEY=SG.xxx
```

### Email Template

**Subject**: Your verification code

**Body**:
```
Your verification code is: 123456

This code expires in 10 minutes.

If you didn't request this code, you can safely ignore this email.
```

## Security Considerations

### Token Security
- Tokens are 32-character urlsafe random strings
- Generated with `secrets.token_urlsafe(24)`
- Not guessable or enumerable

### Rate Limiting
- Email verification requests: 3 per email per 10 minutes
- Chat requests: 30 per IP per minute (configurable)
- Failed verification attempts: 5 per code, then code invalidated

### Session Security
- Session tokens expire after 24 hours
- Sessions tied to specific link + email combination
- Sessions invalidated when link is disabled/deleted

### Abuse Prevention
- Track usage per IP and email
- Owner can see usage statistics
- Owner can disable links immediately
- Optional: Add CAPTCHA for high-traffic links (future enhancement)

## Implementation Phases

### Phase 1: Core Infrastructure
- [ ] Database schema (3 tables)
- [ ] Backend models (Pydantic)
- [ ] Public link CRUD endpoints
- [ ] Public link validation endpoint

### Phase 2: Email Verification
- [ ] Email service abstraction
- [ ] SMTP implementation
- [ ] Verification request endpoint
- [ ] Verification confirm endpoint
- [ ] Code generation and validation

### Phase 3: Public Chat
- [ ] Public chat endpoint
- [ ] Integration with headless task execution
- [ ] Usage tracking
- [ ] Rate limiting middleware

### Phase 4: Frontend
- [ ] PublicChat.vue component
- [ ] Email verification flow UI
- [ ] PublicLinksPanel.vue component
- [ ] Integration with AgentDetail.vue

### Phase 5: Polish
- [ ] Error handling and user feedback
- [ ] Mobile responsiveness
- [ ] Usage analytics display
- [ ] Documentation

## Testing

### Prerequisites
- [ ] Backend running
- [ ] Email service configured (or mock)
- [ ] Test agent created and running

### Test Cases

#### Link Management
1. Create public link (open mode)
2. Create public link (email required)
3. List public links
4. Disable/enable link
5. Delete link
6. Access with expired link (should fail)

#### Email Verification
1. Request code for valid email
2. Verify with correct code
3. Verify with incorrect code (should fail)
4. Verify with expired code (should fail)
5. Rate limiting on code requests

#### Public Chat
1. Chat with open link (no verification)
2. Chat with verified session
3. Chat without session on email-required link (should fail)
4. Chat with expired session (should fail)
5. Chat with disabled link (should fail)

## Future Enhancements

- [ ] Custom branding per link (logo, colors)
- [ ] Conversation history for verified users
- [ ] CAPTCHA integration for abuse prevention
- [ ] Webhook notifications for link usage
- [ ] Domain restriction (only allow emails from specific domains)
- [ ] Usage quotas per link
- [ ] Analytics dashboard

## Related Requirements

- **12.1 Parallel Headless Execution**: Used for stateless public chat
- **9.4 Agent-to-Agent Collaboration**: Similar token-based access patterns
- **2.5 Agent Sharing**: Existing sharing model (authenticated users only)

## Open Questions

1. Should we support conversation history for verified users? (Currently: No, stateless)
2. Maximum links per agent? (Currently: Unlimited)
3. Default session duration? (Currently: 24 hours)
4. Should disabled links return 404 or specific error? (Currently: Specific error with reason)
