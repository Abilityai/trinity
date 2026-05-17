---
name: capture-screenshots
description: Navigate the local Trinity demo instance with Playwright and capture screenshots of all UI functionality for documentation
allowed-tools:
  - mcp__playwright__browser_navigate
  - mcp__playwright__browser_take_screenshot
  - mcp__playwright__browser_click
  - mcp__playwright__browser_snapshot
  - mcp__playwright__browser_hover
  - mcp__playwright__browser_wait_for
  - mcp__playwright__browser_type
  - mcp__playwright__browser_press_key
  - mcp__playwright__browser_tabs
  - mcp__playwright__browser_resize
  - mcp__playwright__browser_run_code
  - Bash
  - Read
  - Write
  - Glob
user-invocable: true
---

# Capture Screenshots

Navigate the local Trinity demo instance and capture screenshots of every major UI area. Saves to `docs/screenshots/` for use in documentation.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Trinity UI | `http://localhost` | Yes | No | Local running instance |
| Screenshots | `docs/screenshots/` | No | Yes | PNG output directory |
| CLAUDE.local.md | `CLAUDE.local.md` | Yes | No | Admin credentials |

## Prerequisites

1. **Trinity platform running** — all services including frontend at http://localhost
2. **Demo data present** — at least a few agents exist (run `/create-demo-agent-fleet` first)
3. **Playwright MCP available** — browser automation tools accessible

## Process

### Step 0: Prepare

```bash
mkdir -p docs/screenshots
```

Set browser to a consistent desktop viewport for uniform screenshots:
- **Width**: 1440px
- **Height**: 900px

Use `mcp__playwright__browser_resize` with width=1440, height=900.

### Hi-Res Screenshot Technique

**Do NOT use `browser_take_screenshot`** — it forces `scale: 'css'` which produces 1x (blurry) images.

Instead, use `browser_run_code` to create a **new browser context** with `deviceScaleFactor: 2`:

```javascript
async (page) => {
  const browser = page.context().browser();
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2
  });
  const p = await ctx.newPage();

  // Login, navigate, and take all screenshots using p.screenshot()
  await p.goto('http://localhost/login');
  await p.screenshot({ path: 'docs/screenshots/01-login.png', type: 'png' });
  // ... (screenshots are 2880x1800 - 2x Retina quality)

  await p.close();
  await ctx.close();
}
```

This renders at 1440px CSS width but captures at 2x pixel density (2880x1800).
Files are written directly to disk — no base64 decode pipeline needed.

### Step 1: Login

1. Navigate to `http://localhost/login`
2. Take screenshot: `docs/screenshots/01-login.png`
3. Login as admin (username: `admin`, password from CLAUDE.local.md)
4. Wait for redirect to dashboard

### Step 2: Dashboard

1. Navigate to `http://localhost/`
2. Wait for agents to load (look for agent cards or status indicators)
3. Take screenshot: `docs/screenshots/02-dashboard.png`

### Step 3: Agents List

1. Navigate to `http://localhost/agents`
2. Wait for agent list to render
3. Take screenshot: `docs/screenshots/03-agents-list.png`

### Step 4: Agent Detail — Tasks Tab

Pick the first available agent (e.g., `acme-scout`).

1. Navigate to `http://localhost/agents/acme-scout`
2. Default tab is Tasks — wait for it to load
3. Take screenshot: `docs/screenshots/04-agent-tasks.png`

### Step 5: Agent Detail — Dashboard Tab

1. Click "Dashboard" tab (if agent has dashboard.yaml)
2. Take screenshot: `docs/screenshots/05-agent-dashboard.png`
3. If no dashboard tab exists, skip this step

### Step 6: Agent Detail — Chat Tab

1. Click "Chat" tab
2. Take screenshot: `docs/screenshots/06-agent-chat.png`

### Step 7: Agent Detail — Logs Tab

1. Click "Logs" tab
2. Wait for logs to load
3. Take screenshot: `docs/screenshots/07-agent-logs.png`

### Step 8: Agent Detail — Schedules Tab

1. Click "Schedules" tab
2. Take screenshot: `docs/screenshots/08-agent-schedules.png`

### Step 9: Agent Detail — Metrics Tab

1. Click "Metrics" tab
2. Take screenshot: `docs/screenshots/09-agent-metrics.png`

### Step 10: Agent Detail — Playbooks Tab

1. Click "Playbooks" tab
2. Take screenshot: `docs/screenshots/10-agent-playbooks.png`

### Step 11: Agent Detail — Credentials Tab

1. Click "Credentials" tab
2. Take screenshot: `docs/screenshots/11-agent-credentials.png`

### Step 12: Agent Detail — Files Tab

1. Click "Files" tab
2. Take screenshot: `docs/screenshots/12-agent-files.png`

### Step 13: Agent Detail — Git Tab

1. Click "Git" tab
2. Take screenshot: `docs/screenshots/13-agent-git.png`

### Step 14: Agent Detail — Folders Tab

1. Click "Folders" tab
2. Take screenshot: `docs/screenshots/14-agent-folders.png`

### Step 15: Agent Detail — Sharing Tab

1. Click "Sharing" tab
2. Take screenshot: `docs/screenshots/15-agent-sharing.png`

### Step 16: Agent Detail — Permissions Tab

1. Click "Permissions" tab
2. Take screenshot: `docs/screenshots/16-agent-permissions.png`

### Step 17: Agent Detail — Info Tab

1. Click "Info" tab
2. Take screenshot: `docs/screenshots/17-agent-info.png`

### Step 18: Templates

1. Navigate to `http://localhost/templates`
2. Wait for template list to load
3. Take screenshot: `docs/screenshots/18-templates.png`

### Step 19: Operating Room

1. Navigate to `http://localhost/operating-room`
2. Take screenshot of default tab (Needs Response): `docs/screenshots/25-operating-room.png`
3. Click "Notifications" tab
4. Take screenshot: `docs/screenshots/26-operating-room-notifications.png`
5. Click "Cost Alerts" tab
6. Take screenshot: `docs/screenshots/27-operating-room-cost-alerts.png`

### Step 20: Health Monitoring

1. Navigate to `http://localhost/monitoring`
2. Wait for health data to load
3. Take screenshot: `docs/screenshots/28-monitoring.png`

### Step 21: API Keys

1. Navigate to `http://localhost/api-keys`
2. Take screenshot: `docs/screenshots/29-api-keys.png`

### Step 22: Settings

1. Navigate to `http://localhost/settings`
2. Take screenshot of default view: `docs/screenshots/30-settings.png`
3. If there are sub-sections (SSH, Skills Library, etc.), click through and capture:
   - `docs/screenshots/31-settings-ssh.png`
   - `docs/screenshots/32-settings-skills.png`

### Step 23: Create Agent Dialog

1. Navigate to `http://localhost/agents`
2. Click "Create Agent" button
3. Take screenshot of the creation dialog/form: `docs/screenshots/34-create-agent.png`
4. Close/cancel the dialog

### Step 24: Summary

After all screenshots are captured, list them:

```bash
ls -la docs/screenshots/*.png | awk '{print $NF, $5}'
```

Print a summary of how many screenshots were captured and any that were skipped.

## Screenshot Naming Convention

| Pattern | Meaning |
|---------|---------|
| `NN-area.png` | Two-digit prefix for ordering |
| `NN-area-detail.png` | Sub-area screenshots |

Always use lowercase, hyphens for spaces. The numeric prefix ensures consistent ordering.

## Troubleshooting

- **Login redirect loop**: Clear browser state, ensure backend is running at port 8000
- **Empty pages**: Wait longer — use `browser_wait_for` with a text or selector condition
- **Missing tabs**: Some agent tabs only appear if the agent has relevant data (e.g., Dashboard tab needs dashboard.yaml)
- **Stale screenshots**: Delete `docs/screenshots/*.png` before re-running for a clean set

## Completion Checklist

- [ ] Browser resized to 1440x900
- [ ] Login screenshot captured
- [ ] Dashboard captured
- [ ] Agents list captured
- [ ] All agent detail tabs captured (Tasks through Info)
- [ ] Templates page captured
- [ ] Operating Room (all tabs) captured
- [ ] Monitoring captured
- [ ] API Keys captured
- [ ] Settings captured
- [ ] Create Agent dialog captured
- [ ] Summary printed

## Self-Improvement

After completing this skill's primary task, consider tactical improvements:

- [ ] **Review execution**: Were there pages that failed to load, new tabs added, or UI changes?
- [ ] **Identify improvements**: Should new screenshots be added for new features? Should wait times be adjusted?
- [ ] **Scope check**: Only tactical/execution changes — NOT changes to core purpose or goals
- [ ] **Apply improvement** (if identified):
  - [ ] Edit this SKILL.md with the specific improvement (e.g., add new step for a new page)
  - [ ] Keep changes minimal and focused
- [ ] **Version control** (if in a git repository):
  - [ ] Stage: `git add .claude/skills/capture-screenshots/SKILL.md`
  - [ ] Commit: `git commit -m "refactor(capture-screenshots): <brief improvement description>"`
