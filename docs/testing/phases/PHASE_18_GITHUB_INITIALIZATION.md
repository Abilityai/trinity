# Phase 18: GitHub Repository Initialization

> **Purpose**: Validate GitHub repository initialization for existing agents
> **Duration**: ~15 minutes
> **Assumes**: Phase 1 PASSED, Phase 2 PASSED (agents exist), GitHub PAT configured
> **Output**: Agent files synced to GitHub repository

---

## Prerequisites

- ✅ Phase 1 PASSED (logged in as admin)
- ✅ At least one running agent (without existing git config)
- ✅ GitHub Personal Access Token (PAT) configured
- ✅ GitHub account with repo creation permissions

---

## Background

GitHub Repository Initialization allows syncing agent files to GitHub:

**Features**:
- Creates new GitHub repository (personal or org)
- Initializes git in agent container
- Pushes agent files to repository
- Sets up working branch for agent

**Agent Files Pushed**:
- `CLAUDE.md` - Agent instructions
- `.claude/` - Claude configuration
- `.trinity/` - Trinity metadata
- `.mcp.json` - MCP server configuration
- Workspace files

**Files Excluded**:
- `.bash_logout`, `.bashrc` - System files
- `.cache/`, `.ssh/` - Temporary/sensitive dirs

---

## Pre-Test Setup

### Verify GitHub PAT

**Action**: Check GitHub credentials configured

```bash
curl http://localhost:8000/api/credentials \
  -H "Authorization: Bearer $TOKEN"
```

**Expected**:
- [ ] `GITHUB_PAT` or `GITHUB_TOKEN` in credentials list

**If Missing**: Add via Settings → Credentials or API

### Choose Test Agent

Select an agent that:
- Is currently running
- Does NOT have existing git configuration
- Has some files to push (CLAUDE.md, etc.)

---

## Test Steps

### Step 1: Navigate to Agent Git Tab

**Action**:
- Go to agent detail page
- Click "Git" tab

**Expected**:
- [ ] Git tab visible in agent detail
- [ ] Shows "Initialize GitHub Sync" button (if not configured)
- [ ] OR shows existing git status (if already configured)

---

### Step 2: Click Initialize GitHub Sync

**Action**:
- Click "Initialize GitHub Sync" button
- Wait for modal to appear

**Expected**:
- [ ] Modal dialog opens
- [ ] Repository owner field (your username or org)
- [ ] Repository name field
- [ ] Private/public toggle
- [ ] "Initialize" button

---

### Step 3: Enter Repository Details

**Action**:
- Enter owner: `your-github-username` or `your-org`
- Enter name: `test-agent-repo` (unique name)
- Select visibility: Private (recommended for testing)
- Click "Initialize"

**Expected**:
- [ ] Modal shows "Initializing..." with spinner
- [ ] Note appears: "This may take 10-60 seconds..."
- [ ] Progress feedback visible

---

### Step 4: Wait for Initialization

**Action**:
- Wait for initialization to complete (up to 120 seconds)

**Expected**:
- [ ] Modal closes automatically on success
- [ ] Success toast/notification appears
- [ ] Git tab updates with repository info

**If Timeout**:
- Check backend logs: `docker logs trinity-backend | grep -i git`
- May need to retry (modal stuck bug was fixed)

---

### Step 5: Verify Repository Created on GitHub

**Action**:
- Visit: `https://github.com/{owner}/{repo-name}`

**Expected**:
- [ ] Repository exists on GitHub
- [ ] Has initial commit
- [ ] Contains agent files:
  - [ ] `CLAUDE.md`
  - [ ] `.claude/` directory (if exists)
  - [ ] `.trinity/` directory (if exists)
  - [ ] `.gitignore` (excludes system files)
- [ ] `main` branch exists
- [ ] Working branch exists: `trinity/{agent-name}/{id}`

---

### Step 6: Verify Git Status in UI

**Action**:
- Return to agent's Git tab
- Observe status display

**Expected**:
- [ ] Remote URL shown
- [ ] Current branch shown
- [ ] Last sync timestamp
- [ ] Sync status indicator

---

### Step 7: Test Re-Initialization Prevention

**Action**:
- Try to click "Initialize GitHub Sync" again

**Expected**:
- [ ] Button disabled or hidden
- [ ] OR: Shows "Already configured" message
- [ ] Cannot create duplicate repository

---

### Step 8: Verify via API

**Action**: Check git status via API

```bash
curl http://localhost:8000/api/agents/AGENT_NAME/git/status \
  -H "Authorization: Bearer $TOKEN"
```

**Expected Response**:
```json
{
  "enabled": true,
  "remote_url": "https://github.com/owner/repo.git",
  "branch": "trinity/agent-name/abc123",
  "last_sync": "2025-12-26T...",
  "status": "synced"
}
```

---

### Step 9: Test with Organization

**Action** (if org access available):
- Create new test agent
- Initialize with org owner: `your-org`
- Complete initialization

**Expected**:
- [ ] Repository created in organization
- [ ] Proper permissions respected
- [ ] Works same as personal repo

---

### Step 10: Verify Container Git State

**Action**: Check git state inside container

```bash
docker exec agent-AGENT_NAME bash -c "cd /home/developer && git status"
```

**Expected**:
- [ ] Shows git repository status
- [ ] On correct branch
- [ ] Working tree clean (after initial push)

---

## Critical Validations

### Files Actually Pushed

**Validation**: Important agent files exist in repo

```bash
# Check GitHub API
curl https://api.github.com/repos/OWNER/REPO/contents \
  -H "Authorization: token $GITHUB_PAT"
```

**Expected Files**:
- [ ] `CLAUDE.md` - Agent's main instructions
- [ ] `.gitignore` - Excludes system files
- [ ] `.claude/` - Claude configuration (if exists)
- [ ] `.trinity/` - Trinity metadata (if exists)

---

### .gitignore Excludes System Files

**Validation**: System files not committed

```bash
# Check .gitignore in repo
curl https://raw.githubusercontent.com/OWNER/REPO/main/.gitignore
```

**Should Contain**:
```
.bash_logout
.bashrc
.bash_history
.cache/
.ssh/
.local/
```

---

### Orphaned Record Cleanup

**Validation**: System handles partial failures

If initialization fails partway:
1. Database record created but git not initialized
2. Next attempt should auto-cleanup orphaned record
3. Re-try should work

---

## Success Criteria

Phase 18 is **PASSED** when:
- ✅ Git tab shows initialization option
- ✅ Modal captures owner/repo/visibility
- ✅ Repository created on GitHub
- ✅ Agent files pushed (CLAUDE.md, .claude/, .trinity/)
- ✅ System files excluded via .gitignore
- ✅ Working branch created
- ✅ Git status displays in UI
- ✅ Re-initialization prevented
- ✅ API returns correct git status
- ✅ Works for personal and org repos

---

## Troubleshooting

**Initialization times out**:
- Large agents take longer (100+ MB)
- Backend timeout is 120 seconds
- Check logs for actual status

**Repository not created**:
- Verify GitHub PAT has `repo` scope
- Check PAT not expired
- Verify owner name correct

**Files not pushed**:
- Check workspace detection
- Verify git initialized in correct directory
- Check .gitignore not excluding important files

**"Already configured" but no repo**:
- Orphaned database record
- Delete agent's git config and retry
- Or use API to clear: `DELETE /api/agents/{name}/git/config`

**Permission denied (org)**:
- Verify org membership
- Check org allows PAT access
- Fine-grained PAT may need org approval

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/agents/{name}/git/status` | Get git sync status |
| POST | `/api/agents/{name}/git/initialize` | Initialize GitHub sync |
| DELETE | `/api/agents/{name}/git/config` | Clear git configuration |
| POST | `/api/agents/{name}/git/sync` | Manual sync trigger |

---

## Related Documentation

- Feature Flow: `docs/memory/feature-flows/github-repo-initialization.md`
- Requirements: `requirements.md` section 9.7
- GitPanel Component: `src/frontend/src/components/GitPanel.vue`

---

## Cleanup

After testing:
- [ ] Delete test repository from GitHub (if desired)
- [ ] Clear agent git config if needed
- [ ] Keep for sync testing if continuing

---

## Next Phase

GitHub Initialization completes the extended testing suite.

Return to:
- **Phase 12**: Cleanup (delete test agents)
- **Phase 0**: Start fresh test run

---

**Status**: Ready for testing
**Last Updated**: 2025-12-26
