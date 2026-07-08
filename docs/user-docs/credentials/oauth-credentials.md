# OAuth Credentials

OAuth2 authentication flows for connecting agents to external services like Google, Slack, GitHub, and Notion.

## How It Works

1. Open the agent's Credentials tab.
2. Click the OAuth provider button (Google, Slack, GitHub, or Notion).
3. Your browser redirects to the provider's authorization page.
4. After you approve access, the callback returns to Trinity.
5. Credentials are normalized to MCP-compatible format and injected into the agent.

## Supported Providers

- **Google** -- Gmail, Calendar, Drive, and other Google Workspace services.
- **Slack** -- Workspace access for messaging and channel operations.
- **GitHub** -- Repository access, issues, pull requests.
- **Notion** -- Page and database access.

## OAuth Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/oauth/{provider}/authorize` | GET | Start OAuth flow |
| `/oauth/{provider}/callback` | GET | OAuth callback handler |

## For Agents

OAuth tokens are stored in Redis with AOF persistence. When an OAuth flow completes, Trinity:

1. Normalizes the token data to `KEY=VALUE` format for `.env` injection.
2. Regenerates the agent's `.mcp.json` with the new credentials.
3. Makes the credentials available to all MCP servers configured in the agent.

Agents do not need to handle OAuth flows directly. Trinity manages token acquisition and injection on their behalf.

## See Also

- [Credential Management](./credential-management.md)
- [MCP Server](../integrations/mcp-server.md)
