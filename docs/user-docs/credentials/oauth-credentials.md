# OAuth Credentials

Trinity exposes a small OAuth2 helper API for four providers — Google, Slack, GitHub, and Notion — that builds a provider authorization URL from client credentials configured on the backend. It does not complete the OAuth exchange for you: the practical way to give an agent a provider token today is to obtain it yourself and inject it as a credential.

## How It Works

1. An admin sets the provider's client credentials as backend environment variables: `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`, `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET`, `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET`, `NOTION_CLIENT_ID` / `NOTION_CLIENT_SECRET`. A provider with no client ID is reported as not configured.
2. `POST /api/oauth/{provider}/init` returns the provider's authorization URL (with a `state` value) for the scopes Trinity requests: Google `openid email profile` plus Drive, GitHub `repo user`, Slack `chat:write channels:read`, Notion none.
3. Open that URL and approve access. The provider redirects to `/api/oauth/{provider}/callback` on your backend.

Trinity currently ships **no handler for that callback** and no OAuth button on an agent's Credentials tab, so the redirect is not turned into a stored token. Once you hold a provider token, add it to the agent through the [Credentials tab](credential-management.md#adding-credentials) — as `KEY=VALUE` in `.env`, where the agent's `.mcp.json.template` picks it up through `${VAR}` placeholders.

Platform-level Slack installation is a separate, complete OAuth flow: **Install to Workspace** on the Slack Integration settings section — see [Slack Integration](../integrations/slack-integration.md).

## For Agents

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/oauth/providers` | GET | The four providers, whether each is configured, and the scopes Trinity would request |
| `/api/oauth/{provider}/init` | POST | Build the provider's authorization URL; `400` for an unknown provider, `500` when its client ID is not set |

Agents do not call these endpoints. Provider tokens reach an agent as injected credentials, resolved into `.mcp.json` at runtime.

## See Also

- [Credential Management](./credential-management.md)
- [Slack Integration](../integrations/slack-integration.md) — the workspace install flow that is complete end to end
- [GitHub PAT Setup](../integrations/github-pat-setup.md) — the supported way to give agents GitHub access
