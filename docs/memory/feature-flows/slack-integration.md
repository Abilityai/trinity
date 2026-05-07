# Slack Integration for Public Links (SLACK-001)

> **Status**: Complete
> **Created**: 2026-02-25
> **Spec**: `docs/requirements/SLACK_INTEGRATION.md`

## Overview

Enables Slack as a delivery channel for public agent links. Users can chat with Trinity agents via DMs to a Slack bot, using the same security model as web-based public links.

## User Stories

1. **As an agent owner**, I want to connect my Slack workspace to a public link so users can chat with my agent via DM.
2. **As a Slack user**, I want to DM a bot to interact with a Trinity agent without leaving Slack.
3. **As an admin**, I want email verification for Slack users to track who uses my agent.

## Architecture

### Deployment Model

```
Configuration Hierarchy (database settings take precedence over env vars):

1. Database Settings (via Settings UI - src/backend/services/settings_service.py:89-108):
   - slack_client_id        # Stored in settings table
   - slack_client_secret    # Stored in settings table
   - slack_signing_secret   # Stored in settings table

2. Environment Variables (fallback - src/backend/config.py:57-62):
   - SLACK_SIGNING_SECRET    # From Slack App settings (line 59)
   - SLACK_CLIENT_ID         # For OAuth flow (line 60)
   - SLACK_CLIENT_SECRET     # For OAuth flow (line 61)

Always from Environment Variables:
- SLACK_AUTO_VERIFY_EMAIL # Auto-verify via Slack profile email (line 62)
- PUBLIC_CHAT_URL         # Public-facing URL for Slack callbacks (line 45)
- FRONTEND_URL            # Admin UI URL for OAuth redirects (line 41)
```

### Data Flow (DM-Only)

```
User sends DM to bot
       |
       v
Slack API (message.im event)
       |
       v
POST ${PUBLIC_CHAT_URL}/api/public/slack/events
       |
       +-> 1. Verify Slack signature (routers/slack.py:69-76)
       +-> 2. Lookup link by team_id (routers/slack.py:132)
       +-> 3. Check user verification (routers/slack.py:152-157)
       +-> 4. Get/create session (routers/slack.py:299)
       +-> 5. Forward to agent via /api/task (routers/slack.py:306-316)
       +-> 6. Send response via chat.postMessage (routers/slack.py:166)
```

Key simplification: One Slack workspace = One public link = One agent

## Entry Points

### Owner Interface
- **UI**: `src/frontend/src/components/PublicLinksPanel.vue:125-181` - Slack section within link card
- **API**: `POST /api/agents/{name}/public-links/{id}/slack/connect` - Initiates OAuth

### Public Interface (Slack Events)
- **API**: `POST /api/public/slack/events` - Receives all Slack events
- **API**: `GET /api/public/slack/oauth/callback` - OAuth completion redirect

## Database Schema

### Tables

Defined in `src/backend/db/schema.py:476-521`:

**slack_link_connections** (lines 479-492):
| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | Connection UUID |
| link_id | TEXT FK | Public link (UNIQUE) |
| slack_team_id | TEXT | Slack workspace ID (UNIQUE) |
| slack_team_name | TEXT | Display name |
| slack_bot_token | TEXT | OAuth access token |
| connected_by | TEXT FK | User who connected |
| connected_at | TEXT | ISO timestamp |
| enabled | INTEGER | 1=active, 0=disabled |

**slack_user_verifications** (lines 494-506):
| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | Verification UUID |
| link_id | TEXT FK | Public link |
| slack_user_id | TEXT | Slack user ID |
| slack_team_id | TEXT | Slack workspace |
| verified_email | TEXT | Verified email address |
| verification_method | TEXT | 'slack_profile' or 'email_code' |
| verified_at | TEXT | ISO timestamp |

**slack_pending_verifications** (lines 508-521):
| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | Pending verification UUID |
| link_id | TEXT FK | Public link |
| slack_user_id | TEXT | Slack user ID |
| slack_team_id | TEXT | Slack workspace |
| email | TEXT | Email being verified |
| code | TEXT | 6-digit verification code |
| created_at | TEXT | ISO timestamp |
| expires_at | TEXT | Expiration (10 minutes) |
| state | TEXT | 'awaiting_email' or 'awaiting_code' |

### Session Reuse

Slack sessions use the existing `public_chat_sessions` table with:
- `identifier_type = 'slack'`
- `session_identifier = '{team_id}:{user_id}'`

## API Endpoints

### Public Endpoints (No Auth)

| Endpoint | Method | File:Line | Handler |
|----------|--------|-----------|---------|
| `/api/public/slack/events` | POST | `routers/slack.py:53` | `handle_slack_event()` |
| `/api/public/slack/oauth/callback` | GET | `routers/slack.py:349` | `slack_oauth_callback()` |

### Authenticated Endpoints (Agent/Link Management)

| Endpoint | Method | File:Line | Handler |
|----------|--------|-----------|---------|
| `/api/agents/{name}/public-links/{id}/slack` | GET | `routers/slack.py:417` | `get_slack_connection_status()` |
| `/api/agents/{name}/public-links/{id}/slack` | PUT | `routers/slack.py:516` | `update_slack_connection()` |
| `/api/agents/{name}/public-links/{id}/slack` | DELETE | `routers/slack.py:489` | `disconnect_slack()` |
| `/api/agents/{name}/public-links/{id}/slack/connect` | POST | `routers/slack.py:453` | `initiate_slack_oauth()` |

### Admin Endpoints (Settings Configuration)

| Endpoint | Method | File:Line | Handler |
|----------|--------|-----------|---------|
| `/api/settings/slack` | GET | `routers/settings.py:429` | `get_slack_settings_status()` |
| `/api/settings/slack` | PUT | `routers/settings.py:486` | `update_slack_settings()` |
| `/api/settings/slack` | DELETE | `routers/settings.py:522` | `delete_slack_settings()` |

## Implementation Files

### Backend

| File | Line Range | Description |
|------|------------|-------------|
| `src/backend/config.py` | 57-62 | `SLACK_SIGNING_SECRET`, `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_AUTO_VERIFY_EMAIL` |
| `src/backend/db/schema.py` | 476-521 | 3 table definitions |
| `src/backend/db/migrations.py` | 57, 490-527 | Migration #20 `_migrate_slack_integration_tables()` |
| `src/backend/db/slack.py` | 1-371 | `SlackOperations` class (371 lines) |
| `src/backend/services/slack_service.py` | 1-317 | `SlackService` class (uses settings_service getters) |
| `src/backend/services/settings_service.py` | 89-161 | Slack credential getters with DB/env fallback |
| `src/backend/routers/slack.py` | 1-543 | Public + auth routers (543 lines) |
| `src/backend/routers/settings.py` | 425-554 | Slack settings CRUD endpoints (admin UI) |
| `src/backend/db_models.py` | 810-890 | 8 Pydantic models |
| `src/backend/database.py` | 125, 250, 1060-1113 | Import, init, delegation methods |
| `src/backend/main.py` | 65, 327-328 | Import and router mounting |

### Frontend

| File | Line Range | Description |
|------|------------|-------------|
| `src/frontend/src/components/PublicLinksPanel.vue` | 125-181, 377, 407-408, 533-601, 660-675 | Slack UI section |
| `src/frontend/src/views/Settings.vue` | 223-380, 1014-1027, 1226-1266 | Slack settings admin UI |

## Backend Layer

### SlackOperations Class (`src/backend/db/slack.py`)

| Method | Line | Description |
|--------|------|-------------|
| `create_slack_connection()` | 24-45 | Create new workspace connection |
| `get_slack_connection()` | 47-62 | Get by connection ID |
| `get_slack_connection_by_link()` | 64-79 | Get by public link ID |
| `get_slack_connection_by_team()` | 81-101 | Get by Slack team ID (joins link data) |
| `update_slack_connection()` | 103-134 | Update enabled/team_name |
| `delete_slack_connection()` | 136-151 | Delete with cascade cleanup |
| `delete_slack_connection_by_link()` | 153-163 | Delete by link ID |
| `get_user_verification()` | 182-210 | Check if user is verified |
| `create_user_verification()` | 212-234 | Create verification record (upsert) |
| `get_pending_verification()` | 240-260 | Get active pending verification |
| `create_pending_verification()` | 262-291 | Start verification flow |
| `update_pending_verification()` | 293-330 | Transition state machine |
| `delete_pending_verification()` | 332-345 | Remove after success |
| `cleanup_expired_pending_verifications()` | 347-356 | Cleanup job |

### SlackService Class (`src/backend/services/slack_service.py`)

| Method | Line | Description |
|--------|------|-------------|
| `verify_slack_signature()` | 52-86 | HMAC-SHA256 signature verification |
| `get_oauth_url()` | 92-106 | Generate OAuth URL with scopes |
| `exchange_oauth_code()` | 108-146 | Exchange code for access token |
| `encode_oauth_state()` | 148-170 | Create signed state token |
| `decode_oauth_state()` | 172-200 | Verify and decode state token |
| `send_message()` | 206-237 | Send via chat.postMessage |
| `get_user_email()` | 239-268 | Get email from Slack profile |
| `open_dm_channel()` | 270-296 | Open DM channel with user |
| `get_oauth_callback_redirect()` | 298-309 | Build redirect URL |

### Router Handlers (`src/backend/routers/slack.py`)

**Public Router** (prefix `/api/public/slack`):

| Handler | Line | Description |
|---------|------|-------------|
| `handle_slack_event()` | 53-105 | Main event handler |
| `_handle_slack_dm()` | 108-173 | Process DM messages |
| `_handle_slack_verification()` | 176-280 | Verification state machine |
| `_forward_to_agent()` | 283-327 | Forward to agent, get response |
| `slack_oauth_callback()` | 349-407 | OAuth completion handler |

**Auth Router** (prefix `/api/agents`):

| Handler | Line | Description |
|---------|------|-------------|
| `get_slack_connection_status()` | 417-450 | Get connection status |
| `initiate_slack_oauth()` | 453-486 | Start OAuth flow (uses `get_slack_signing_secret()` from settings_service) |
| `disconnect_slack()` | 489-513 | Disconnect workspace |
| `update_slack_connection()` | 516-542 | Update settings |

### Database Delegation (`src/backend/database.py:1060-1113`)

```python
# Line 1063-1067
def create_slack_connection(self, link_id, slack_team_id, slack_team_name,
                            slack_bot_token, connected_by):
    return self._slack_ops.create_slack_connection(...)

# Line 1087-1088
def get_slack_user_verification(self, link_id, slack_user_id, slack_team_id):
    return self._slack_ops.get_user_verification(...)
```

## Frontend Layer

### PublicLinksPanel.vue

**State** (lines 407-408):
```javascript
const slackConnections = ref({})  // { linkId: { connected, team_name, enabled, ... } }
const slackLoading = ref({})      // { linkId: true/false }
```

**Methods**:

| Method | Line | Description |
|--------|------|-------------|
| `fetchSlackStatus()` | 529-543 | Load connection status for a link |
| `connectSlack()` | 549-567 | Initiate OAuth flow (opens popup) |
| `toggleSlackEnabled()` | 569-586 | Enable/disable connection |
| `disconnectSlack()` | 588-605 | Remove Slack connection |

**Template** (lines 125-181):
- Line 127-131: Slack icon and label
- Line 135-143: "Connect Slack" button (not connected state)
- Line 147-175: Connected state (team badge, toggle, disconnect)
- Line 178-180: "Connected by X on Y" info line

**URL Parameter Handling** (lines 660-675):
```javascript
const slackStatus = urlParams.get('slack')
if (slackStatus === 'connected') {
  copyNotification.value = 'slack-connected'
  // ...
}
```

## Pydantic Models (`src/backend/db_models.py:810-890`)

| Model | Line | Description |
|-------|------|-------------|
| `SlackConnectionCreate` | 814-820 | Internal create request |
| `SlackConnection` | 823-831 | Connection record |
| `SlackConnectionStatus` | 834-841 | API response (no token) |
| `SlackOAuthInitResponse` | 844-846 | OAuth URL response |
| `SlackUserVerification` | 849-857 | Verified user record |
| `SlackPendingVerification` | 860-870 | In-progress verification |
| `SlackEvent` | 873-881 | Incoming Slack event |
| `SlackOAuthState` | 884-889 | OAuth state token fields |

## Verification Flow

```
User sends first DM
       |
       v
Link requires email? (routers/slack.py:152)
       |
   +---+---+
   | NO    | YES
   v       v
[Chat]  User already verified? (routers/slack.py:191-192)
            |
        +---+---+
        | NO    | YES
        v       v
    SLACK_AUTO_VERIFY_EMAIL? (routers/slack.py:201)
        |       [Chat]
    +---+---+
    | YES   | NO
    v       v
  Auto-    "Reply with your email" (routers/slack.py:211-215)
  verify       |
    |          v
    v     Send code to email (routers/slack.py:237-242)
[Chat]         |
               v
          "Enter 6-digit code" (routers/slack.py:254-265)
               |
               v
          Verify -> [Chat] (routers/slack.py:267-278)
```

### Verification Messages

Defined in `routers/slack.py`:

**Initial prompt** (lines 211-215):
```
Hi! Before we chat, I need to verify your email address.
Please reply with your email address to continue.
```

**Code sent** (lines 239-242):
```
I've sent a 6-digit verification code to {email}.
Reply with the code to complete verification. The code expires in 10 minutes.
```

**Verification complete** (lines 273-277):
```
Verified! You can now chat with me.
What would you like to know?
```

## OAuth Flow

**Step-by-step**:

1. **User clicks "Connect Slack"** (`PublicLinksPanel.vue:549`)
2. **Frontend calls** `POST /api/agents/{name}/public-links/{id}/slack/connect` (line 553)
3. **Backend generates signed state** (`slack_service.py:148-170`)
4. **Backend returns OAuth URL** (`slack_service.py:92-106`)
   - Scopes: `im:history,chat:write,users:read.email`
5. **Frontend opens Slack OAuth** (`PublicLinksPanel.vue:560`)
6. **User authorizes in Slack**
7. **Slack redirects to** `/api/public/slack/oauth/callback` (line 349)
8. **Backend validates state** (`routers/slack.py:370-374`)
9. **Backend exchanges code for token** (`slack_service.py:108-146`)
10. **Backend creates connection** (`routers/slack.py:388-395`)
11. **Redirect to frontend with** `?slack=connected` (`slack_service.py:307`)
12. **Frontend shows success notification** (`PublicLinksPanel.vue:662-667`)

## Security

### Request Verification (`slack_service.py:52-86`)

```python
# Line 75-80
sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
expected_signature = 'v0=' + hmac.new(
    SLACK_SIGNING_SECRET.encode('utf-8'),
    sig_basestring.encode('utf-8'),
    hashlib.sha256
).hexdigest()
```

- 5-minute timestamp tolerance (line 69)
- Constant-time comparison (line 83)

### OAuth State Token (`slack_service.py:148-200`)

- Encodes `link_id`, `agent_name`, `user_id`, `timestamp`
- HMAC-SHA256 signed with `SECRET_KEY`
- 15-minute expiry (line 193)

### Authorization Checks

Uses standard codebase access control methods (consistent with other routers):

| Endpoint | Check | Method | File:Line |
|----------|-------|--------|-----------|
| Connect Slack | Owner or admin | `db.can_user_share_agent()` | `routers/slack.py:460` |
| Disconnect | Owner or admin | `db.can_user_share_agent()` | `routers/slack.py:495` |
| Update | Owner or admin | `db.can_user_share_agent()` | `routers/slack.py:522` |
| Get status | Any access level | `db.can_user_access_agent()` | `routers/slack.py:425` |

## Error Handling

| Error Case | HTTP Status | Handler Location |
|------------|-------------|------------------|
| Signature verification failed | 200* | `routers/slack.py:73-76` |
| Invalid OAuth state | Redirect | `routers/slack.py:372-374` |
| OAuth token exchange failed | Redirect | `routers/slack.py:382-385` |
| Link not found | 404 | `routers/slack.py:431-432` |
| Access denied | 403 | `routers/slack.py:427-428` |
| Already connected | 400 | `routers/slack.py:472-473` |
| Slack not configured | 400 | `routers/slack.py:475-476` |

*Slack events always return 200 to prevent retries

## Frontend UI

### PublicLinksPanel Integration (lines 125-181)

```
+---------------------------------------------------------------+
| Public Link: "Customer Support"                                |
+---------------------------------------------------------------+
| ... existing link content (URL, copy buttons, etc.) ...        |
|                                                                |
| -----------------------------------------------------------   |
| [Slack icon] Slack              [Connect Slack]     <- Line 136-143
|                                                                |
| -- OR (when connected) ----------------------------------      |
|                                                                |
| [Slack icon] Slack    [* Acme Corp] [toggle] [x]   <- Line 147-175
| Connected by admin@example.com on Feb 25, 2026     <- Line 178-180
+---------------------------------------------------------------+
```

### UI States

| State | Elements Shown |
|-------|----------------|
| Not connected | "Connect Slack" button |
| Loading | "Connecting..." text |
| Connected + Enabled | Green badge, disable toggle, disconnect button |
| Connected + Disabled | Green badge, enable toggle, disconnect button |

## Session Persistence

Slack sessions reuse the existing public chat session infrastructure:

```python
# routers/slack.py:296
session_identifier = f"{team_id}:{user_id}"

# routers/slack.py:299
session = db.get_or_create_public_chat_session(link_id, session_identifier, "slack")
```

Context is built using `db.build_public_chat_context()` (line 302), same as web public links.

## Testing

### Unit Tests

- `test_slack_signature_verification` - Valid/invalid/expired signatures
- `test_url_verification_challenge` - Returns challenge for Slack setup
- `test_lookup_link_by_team` - Finds correct link for workspace
- `test_user_verification_flow` - Email code state machine
- `test_auto_verify_with_email_scope` - Auto-verifies via Slack profile

### Integration Tests

- `test_oauth_flow_complete` - Full OAuth -> token storage -> redirect
- `test_dm_to_response` - Slack DM event -> agent call -> response
- `test_session_persistence` - Multiple messages maintain context

### Manual Tests

1. Connect Slack -> DM bot -> receive response
2. Enable "require email" -> DM bot -> enter email -> enter code -> chat
3. Disconnect -> verify DMs no longer work
4. Re-connect -> verify previous sessions are cleared

## Settings Configuration Flow

### Overview

Admin-configurable Slack credentials via the Settings UI, eliminating the need for environment variables.

### Configuration Hierarchy

```
1. Database setting (if exists) -> USED
2. Environment variable (fallback) -> USED if no database setting
3. Not configured -> Slack integration disabled
```

### Entry Points

- **UI**: `src/frontend/src/views/Settings.vue:223-380` - Slack Integration section
- **API**: `GET/PUT/DELETE /api/settings/slack`

### Frontend Layer

#### Settings.vue Component

**State** (lines 1014-1027):
```javascript
const slackClientId = ref('')
const slackClientSecret = ref('')
const slackSigningSecret = ref('')
const showSlackClientSecret = ref(false)
const showSlackSigningSecret = ref(false)
const savingSlackSettings = ref(false)
const slackSaveSuccess = ref(false)
const slackSettings = ref({
  configured: false,
  client_id: { configured: false, masked: null, source: null },
  client_secret: { configured: false, masked: null, source: null },
  signing_secret: { configured: false, masked: null, source: null }
})
```

**Methods**:

| Method | Line | Description |
|--------|------|-------------|
| `loadSlackSettings()` | 1227-1233 | Fetch status from `GET /api/settings/slack` |
| `saveSlackSettings()` | 1236-1266 | Save via `PUT /api/settings/slack` |

**Template** (lines 223-380):
- Line 223-230: Section header with description
- Line 234-251: Client ID input with masked placeholder and source indicator
- Line 254-284: Client Secret input (password type with toggle)
- Line 287-317: Signing Secret input (password type with toggle)
- Line 320-335: Save button with loading state
- Line 338-361: Status card (green=ready, yellow=incomplete)
- Line 364-377: Expandable setup instructions

**API Calls**:
```javascript
// Load status
await axios.get('/api/settings/slack')

// Save settings (partial update - only non-empty fields)
await axios.put('/api/settings/slack', {
  client_id: slackClientId.value,
  client_secret: slackClientSecret.value,
  signing_secret: slackSigningSecret.value
})
```

### Backend Layer

#### Settings Router (`src/backend/routers/settings.py`)

| Endpoint | Method | Line | Handler |
|----------|--------|------|---------|
| `/api/settings/slack` | GET | 429-476 | `get_slack_settings_status()` |
| `/api/settings/slack` | PUT | 486-519 | `update_slack_settings()` |
| `/api/settings/slack` | DELETE | 522-554 | `delete_slack_settings()` |

**GET /api/settings/slack** (lines 429-476):
```python
# Admin-only - returns masked key info
return {
    "configured": bool(client_id and client_secret and signing_secret),
    "client_id": {
        "configured": bool(client_id),
        "masked": "1234...5678" if client_id else None,
        "source": "settings" | "env" | None
    },
    "client_secret": {...},
    "signing_secret": {...}
}
```

**PUT /api/settings/slack** (lines 486-519):
```python
class SlackSettingsUpdate(BaseModel):
    client_id: str = None
    client_secret: str = None
    signing_secret: str = None

# Partial update - only provided values are saved
if body.client_id is not None:
    db.set_setting('slack_client_id', body.client_id.strip())
```

**DELETE /api/settings/slack** (lines 522-554):
```python
# Removes all 3 settings from database
# Returns fallback_configured: true if env vars exist
```

#### Settings Service (`src/backend/services/settings_service.py`)

**Methods** (lines 89-108):

| Method | Line | Description |
|--------|------|-------------|
| `get_slack_client_id()` | 89-94 | DB first, then `SLACK_CLIENT_ID` env var |
| `get_slack_client_secret()` | 96-101 | DB first, then `SLACK_CLIENT_SECRET` env var |
| `get_slack_signing_secret()` | 103-108 | DB first, then `SLACK_SIGNING_SECRET` env var |

**Convenience functions** (lines 149-161):
```python
# Module-level functions for backward compatibility
def get_slack_client_id() -> str:
    return settings_service.get_slack_client_id()

def get_slack_client_secret() -> str:
    return settings_service.get_slack_client_secret()

def get_slack_signing_secret() -> str:
    return settings_service.get_slack_signing_secret()
```

#### Slack Service Integration (`src/backend/services/slack_service.py`)

**Updated imports** (lines 25-29):
```python
from services.settings_service import (
    get_slack_signing_secret,
    get_slack_client_id,
    get_slack_client_secret,
)
```

**Usage in methods**:
- `verify_slack_signature()` line 65: `signing_secret = get_slack_signing_secret()`
- `get_oauth_url()` line 103: `"client_id": get_slack_client_id()`
- `exchange_oauth_code()` lines 127-128: Uses both `get_slack_client_id()` and `get_slack_client_secret()`

### Database Operations

Settings are stored in the existing `settings` table:

| Key | Description |
|-----|-------------|
| `slack_client_id` | Slack App Client ID |
| `slack_client_secret` | Slack App Client Secret |
| `slack_signing_secret` | Slack App Signing Secret |

**Database methods used**:
- `db.get_setting_value(key, default)` - Read setting
- `db.set_setting(key, value)` - Create/update setting
- `db.delete_setting(key)` - Remove setting

### UI States

| State | Description | Visual |
|-------|-------------|--------|
| Not configured | No settings in DB, no env vars | Yellow warning card |
| Partially configured | Some credentials missing | Yellow warning card |
| Fully configured (settings) | All 3 in database | Green success card, "from settings" labels |
| Fully configured (env) | All 3 from env vars | Green success card, "from environment" labels |
| Mixed sources | Some from DB, some from env | Green success card, mixed labels |

### Error Handling

| Error Case | HTTP Status | Handler |
|------------|-------------|---------|
| Not admin | 403 | `require_admin()` check |
| Database error | 500 | Exception catch in each handler |
| Invalid request body | 422 | Pydantic validation |

### Security Considerations

1. **Admin-only access**: All endpoints require admin authentication via `require_admin(current_user)`
2. **Masked values**: API never returns actual secret values, only masked versions (e.g., `1234...5678`)
3. **Source tracking**: UI shows whether value came from settings or environment for transparency
4. **No plaintext logging**: Secret values are never logged

### Data Flow Diagram

```
Admin opens Settings page
       |
       v
loadSlackSettings()
       |
       v
GET /api/settings/slack
       |
       v
get_slack_settings_status()
       |
       +-> get_slack_client_id()     -> DB setting or SLACK_CLIENT_ID env
       +-> get_slack_client_secret() -> DB setting or SLACK_CLIENT_SECRET env
       +-> get_slack_signing_secret() -> DB setting or SLACK_SIGNING_SECRET env
       |
       v
Return masked status with sources
       |
       v
UI displays current state
       |
[Admin enters new values]
       |
       v
saveSlackSettings()
       |
       v
PUT /api/settings/slack
       |
       v
db.set_setting() for each provided field
       |
       v
Reload status (refresh UI)
```

## Unified Cross-Channel Access Control (#311)

> **Canonical reference**: [unified-channel-access-control.md](unified-channel-access-control.md). This section only describes the Slack-specific pieces — the policy model, `access_requests` table, router gate, and approval UI live in that flow.

Issue #311 introduced a single per-agent access policy shared across all channels (web public links, Slack, Telegram). Slack's role in that flow is minimal: the workspace OAuth token already proves user identity, so the adapter just looks up the sender's email via Slack's `users.info` API. No 6-digit verification round-trip is needed (unlike Telegram's `/login`), which makes Slack the simplest channel to wire in.

### New adapter method: `resolve_verified_email`

Added to `SlackAdapter` at `src/backend/adapters/slack_adapter.py:51-67`:

```python
async def resolve_verified_email(self, message: NormalizedMessage) -> Optional[str]:
    bot_token = self.get_bot_token(message)
    if not bot_token:
        return None
    try:
        email = await slack_service.get_user_email(bot_token, message.sender_id)
    except Exception as e:
        logger.warning(f"Slack resolve_verified_email failed: {e}")
        return None
    return email.lower() if email else None
```

- `get_bot_token(message)` resolves the workspace token via `slack_workspaces` (new path) with `slack_link_connections` as a legacy fallback (see lines 69-81 in the same file).
- `slack_service.get_user_email(bot_token, user_id)` calls Slack's `users.info` API (`services/slack_service.py:239-268`).
- Emails are returned lowercased for case-insensitive matching against `agent_sharing.shared_with_email`.
- No persistence — the adapter re-resolves live on every message. The workspace OAuth token is already durable state.

### Router gate integration

`ChannelMessageRouter._handle_message_inner` (`src/backend/adapters/message_router.py:210-264`) calls `adapter.resolve_verified_email` in step 5b of the gate. The returned email is used for:

1. **Access check** — `db.email_has_agent_access(agent_name, email)` (owner / admin / `agent_sharing` row).
2. **Open-access bypass** — admitted if the agent has `open_access=true`.
3. **Access request path** — if neither of the above, `db.upsert_access_request(agent_name, email, "slack")` queues the request for owner approval; user gets "🔒 Your access request is pending approval".

Channel-wide (non-DM) group messages bypass the gate entirely — see the "Group chat bypass" section of [unified-channel-access-control.md](unified-channel-access-control.md). Full gate semantics (require_email flag, legacy passthrough fallback) are documented there.

### OAuth scope requirement

For `resolve_verified_email` to return a value, the Slack app must be installed with the **`users:read.email`** OAuth scope (already present in the scope list at `slack_service.py:92-106` — `im:history,chat:write,users:read.email`).

If the scope is missing, or the user has hidden their profile email, `users.info` returns `null` and `resolve_verified_email` returns `None`. When the agent's policy has `require_email=true`, the router then falls back to `adapter.prompt_auth` — the default text reply from the ABC — since `SlackAdapter` does not override `prompt_auth` (there's no Slack-native "send a verification code" flow the way Telegram has `/login`).

### Relation to `slack_user_verifications`

The existing `slack_user_verifications` + `slack_pending_verifications` tables (see Database Schema above) power the **per-public-link** email verification flow from SLACK-001 (used when a link has `require_email=true` but the workspace can't auto-resolve email).

The #311 gate — for **workspace-connected agents** routed via the channel adapter (SLACK-002) — does not use these tables. It reads directly from the Slack API on each message. The tables remain in place for the legacy public-link path; no new per-message persistence is added.

### See also

- **[Unified Channel Access Control](unified-channel-access-control.md)** — canonical reference for policy columns, `access_requests` table, the router gate, `AccessPolicyMixin`, and approval UI.
- **[Agent Sharing](agent-sharing.md)** — `agent_sharing` is the cross-channel allow-list; approving an access request inserts a row here.
- **[Slack Channel Routing](slack-channel-routing.md)** (SLACK-002) — the `SlackAdapter` + channel binding model that #311 plugs into.

## Related

- **[Public Agent Links](public-agent-links.md)** (15.1) - Base infrastructure, session persistence (PUB-005)
- **Email Verification** - Reused for Slack user verification (`email_service.send_verification_code()`)
- **Session Persistence** - `public_chat_sessions` with `identifier_type='slack'`

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2026-02-25 | Claude | Initial implementation |
| 2026-02-25 | Claude | Enhanced with exact line numbers and code references |
| 2026-02-25 | Claude | Added Settings Configuration flow (admin UI for Slack credentials) |
| 2026-03-01 | Claude | Fixed access checks to use `can_user_access_agent()`/`can_user_share_agent()` (Issue #48) |
| 2026-03-12 | Claude | Fixed `initiate_slack_oauth()` to use `get_slack_signing_secret()` from settings_service instead of importing from config.py |
| 2026-04-12 | Claude | Added `SlackAdapter.resolve_verified_email` for unified cross-channel access control (#311) — see [unified-channel-access-control.md](unified-channel-access-control.md) |
| 2026-05-05 | Claude | Encrypted `slack_link_connections.slack_bot_token` at rest via AES-256-GCM (#453, Invariant #12). Same pattern as `db/slack_channels.py`, `telegram_channels.py`, `whatsapp_channels.py`. One-shot migration `slack_bot_token_encryption` walks both `slack_link_connections` and `slack_workspaces` and re-encrypts any plaintext `xoxb-*` rows on next backend startup. Read path includes plaintext fallback so runtime works pre-migration. 14 unit tests in `tests/unit/test_slack_token_encryption.py`. |
