# Requirements — Credential Management

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## 3. Credential Management

### 3.1 Manual Credential Entry
- **Status**: ✅ Implemented
- **Description**: Add credentials via UI form with name, value, service

### 3.2 OAuth2 Flows
- **Status**: ✅ Implemented
- **Description**: OAuth2 authentication for Google, Slack, GitHub, Notion
- **Key Features**: MCP-compatible credential normalization

### 3.3 Credential Hot-Reload
- **Status**: ✅ Implemented
- **Description**: Update credentials on running agents without restart
- **Key Features**: Hot-reload via UI paste, writes `.env` and regenerates `.mcp.json`

### 3.4 Bulk Credential Import
- **Status**: ✅ Implemented
- **Description**: Paste `.env`-style KEY=VALUE pairs with template selector

### 3.5 Credential Requirements Extraction
- **Status**: ✅ Implemented
- **Description**: Extract from `.mcp.json.template` and show configured vs missing status

---
