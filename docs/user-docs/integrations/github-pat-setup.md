# GitHub Personal Access Token Setup

Configure a GitHub Personal Access Token (PAT) to enable repository creation, code sync, and issue management for your Trinity agents.

## Concepts

- **Personal Access Token (PAT)** -- A GitHub credential that grants Trinity access to your repositories. Acts as a password for API access.
- **Classic Token** -- The traditional PAT format. Simpler to configure, grants broad access per scope.
- **Fine-Grained Token** -- Newer PAT format. Allows granular permissions on specific repositories.
- **Scopes** -- Permission categories that control what the token can do (read code, write issues, etc.).

## When You Need a PAT

A GitHub PAT is required for:

| Feature | Requires PAT |
|---------|--------------|
| Create new GitHub repo for an agent | Yes |
| Push agent code to GitHub | Yes |
| Pull updates from private repos | Yes |
| Create/update GitHub Issues from agents | Yes |
| Clone from public templates | No |
| Pull from public repos (read-only) | No |

> **"Requires PAT" ≠ "auto-wired."** The token needs the right scopes for each row above — but for Issues, PRs, and other **GitHub CLI / REST-API** work, Trinity does **not** wire the token the way it does for `git`. See [What the PAT Authenticates](#what-the-pat-authenticates-git-vs-the-gh-cli) below before you rely on `gh`.

## What the PAT Authenticates: Git vs. the `gh` CLI

Trinity wires the token for **git transport only**. When an agent has a GitHub repo and a resolved PAT (per-agent override or the platform PAT), Trinity:

- bakes the token into the agent repo's `origin` remote URL (`https://oauth2:<token>@github.com/...`), and
- exposes it inside the container as the **`GITHUB_PAT`** environment variable (and in the agent's `.env`).

That is enough for `git clone`, `git push`, and `git pull` against the agent's own repo to work automatically — which is what Source Mode, Working Branch Mode, and scheduled commits rely on.

It is **not** enough for the GitHub CLI or the REST API:

| Operation | Auto-authenticated by Trinity's token? |
|-----------|----------------------------------------|
| `git` push / pull / clone (agent's own repo) | **Yes** — token is in the remote URL |
| `gh issue`, `gh pr`, `gh api`, `gh repo` … | **No** — see below |
| GitHub REST API via `curl` / a github MCP server | **Only if the agent passes `$GITHUB_PAT` itself** |

Two reasons `gh` doesn't "just work":

1. **`gh` reads `GH_TOKEN` / `GITHUB_TOKEN`, not `GITHUB_PAT`.** Trinity sets `GITHUB_PAT`; it does not set the variables `gh` looks for, and it does not run `gh auth login`. The token isn't in a `.git-credentials` file either — it lives only in the remote URL — so `gh` can't discover it from git config.
2. **`gh` is not installed in the default agent base image.** An agent that wants it must install it, or its template must add it.

> **Setting the PAT will not fix a `gh` failure.** The `set_agent_github_pat` MCP tool and the **Settings → GitHub Personal Access Token** field only change which token **`git`** uses. They do not install `gh`, set `GH_TOKEN`, or authenticate the CLI/REST API. A `gh: not logged in` error, or a 401/403 from `gh`/the REST API, is **not** solved by setting or rotating the PAT.

### Making an agent's `gh` / API calls work today

The token is already present as `$GITHUB_PAT`, so an agent (or its template) can use it explicitly:

```bash
# GitHub CLI — install gh once, then authenticate from the env var
gh auth login --with-token <<< "$GITHUB_PAT"
# ...or per-command, no persistent login:
GH_TOKEN="$GITHUB_PAT" gh issue list

# REST API directly — no gh needed
curl -H "Authorization: Bearer $GITHUB_PAT" \
  https://api.github.com/repos/OWNER/REPO/issues
```

**The token still needs the right scopes for the operation.** A PAT scoped only for git contents can push code but will 403 on Issues/PRs. For issue management, make sure the token carries `repo` (classic) or **Issues: Read and write** (fine-grained) — see the scope tables above.

## Who Owns the Platform PAT

Trinity stores a single **platform-wide PAT** that every agent inherits unless an agent has its own per-agent override. In the standard setup, the **Trinity admin** creates this token in their own GitHub account — which means:

- **The token's reach is the admin's reach.** Anything the admin's GitHub user can do, every agent using the platform PAT can do.
- **Agents are hosted under the admin's GitHub account / org** for the repos they push to. If an agent needs to push to a repo the admin doesn't have access to, use a per-agent PAT override instead.
- **The admin's decision up front**: how much of their own GitHub access should bleed through to the agents? This is the question that picks classic vs. fine-grained.

## Choosing a Token Type

| If you want... | Pick | Tradeoff |
|----------------|------|----------|
| Simplest setup; fine with agents inheriting access to **every repo your account can reach** | **Classic PAT** | Wider blast radius if the token leaks. New agents work instantly — no GitHub-side steps. |
| Agents scoped to a **specific list of repos**, nothing else | **Fine-grained PAT** | Smaller blast radius. Every time you add an agent on a new repo, you edit the token to include that repo. |

Classic is faster to operate. Fine-grained is safer. Pick the tradeoff that matches your organization's risk posture.

## How It Works

### Option A: Classic Token (simplest, widest access)

A classic PAT with `repo` scope grants access to **every repository your GitHub user can see** — personal repos, plus any org repos your account has access to. Best when you're comfortable with that reach and want zero-maintenance setup.

![GitHub new classic personal access token page with the `repo` scope checked](../images/pat/classic-token-scopes.png)


**Step 1: Create the token**

1. Go to [github.com/settings/tokens](https://github.com/settings/tokens)
2. Click **Generate new token** → **Generate new token (classic)**
3. Set a descriptive name: `Trinity Platform`
4. Set expiration (90 days recommended, or "No expiration" for persistent setups)
5. Select these scopes:

| Scope | Required | Purpose |
|-------|----------|---------|
| `repo` | **Yes** | Full control of repositories — read/write code, issues, PRs |
| `workflow` | Optional | Trigger GitHub Actions (if agents manage CI/CD) |
| `read:org` | Optional | Read organization membership (for org repos) |

6. Click **Generate token**
7. **Copy the token immediately** — you cannot view it again

**Step 2: Configure in Trinity**

1. Go to **Settings** in Trinity (sidebar → Settings)
2. Find the **GitHub Personal Access Token (PAT)** section
3. Paste your token
4. Click **Test** to verify it works
5. Click **Save**

The test shows your GitHub username and confirms repo access.

**Saving auto-propagates to running agents.** When the platform PAT changes, Trinity pushes the new token into every running agent's `.env` file within seconds — no restart required. The save response lists which agents were updated, skipped, or failed. Agents with a per-agent PAT override, or agents that never configured GitHub, are skipped. Per-agent failures are reported but never block the save.

### Option B: Fine-Grained Token (scoped, safer, more maintenance)

A fine-grained PAT is limited to a **specific list of repositories** and a **specific set of permissions**. Agents can only touch what you listed — not the admin's other repos. Best for production and for admins whose personal GitHub account also holds unrelated work you don't want agents near.


**Step 1: Create the token**

1. Go to [github.com/settings/tokens?type=beta](https://github.com/settings/tokens?type=beta)
2. Click **Generate new token**
3. Set a descriptive name: `Trinity Platform`
4. Set expiration
5. Under **Repository access**, choose:
   - **All repositories** — Token works with any repo you own/administer
   - **Only select repositories** — Pick specific repos for Trinity agents

6. Under **Permissions**, expand **Repository permissions** and set:

| Permission | Access Level | Purpose |
|------------|--------------|---------|
| **Contents** | Read and write | Push/pull code, read files |
| **Issues** | Read and write | Create and manage issues |
| **Metadata** | Read-only | Required (auto-selected) |
| **Pull requests** | Read and write | Optional: create PRs |
| **Workflows** | Read and write | Optional: manage GitHub Actions |

7. Click **Generate token**
8. **Copy the token immediately**

**Step 2: Configure in Trinity**

Same as Option A — paste in Settings, test, save.

### For Organization Repositories

If you're creating repos in a GitHub Organization:

1. The PAT must belong to a user with **admin access** to the organization
2. The organization may require **SSO authorization** for the token:
   - After creating the token, click **Configure SSO** next to it
   - Authorize for your organization
3. For fine-grained tokens, the organization must enable them in Settings → Personal access tokens

## Choosing Between Token Types

| Consideration | Classic Token | Fine-Grained Token |
|---------------|---------------|-------------------|
| Setup complexity | Simple (check boxes) | More steps |
| Repository scope | All repos by default | Can limit to specific repos |
| Permission granularity | Broad scopes | Individual permissions |
| Organization support | Full | Requires org admin approval |
| Token format | `ghp_...` | `github_pat_...` |
| Recommended for | Development, personal use | Production, shared teams |

## Hosting Agents from External Contributors

A useful consequence of how classic PATs work: **a classic PAT reaches every repo its owner can access, including repos that were shared with the owner as a collaborator.** This unlocks a simple workflow for letting external agent developers deploy their agents on your Trinity instance without handing over tokens or transferring repos.

**The pattern:**
1. External developer has their agent code in their own GitHub repo.
2. They invite the **Trinity admin's GitHub user** as a collaborator, with role:
   - **Write** — if the agent needs to push back (Working Branch mode, scheduled commits, etc.). This is the usual case.
   - **Read** — if the agent only pulls (Source mode / deploy-only).
3. Admin accepts the invitation.
4. Trinity's classic PAT now reaches the repo immediately — no token change, no restart. Deploy the agent from that repo like any other.
5. When the engagement ends, the external developer removes the admin as a collaborator. Access disappears the same moment. The PAT is untouched.

**Important caveats:**

- **Only works with classic PATs.** A fine-grained PAT is scoped to a single resource owner (one user or one org) and cannot reach a friend's or another org's repositories, regardless of collaborator status. If you're running fine-grained, use a [per-agent PAT override](#for-agents) instead — the external developer creates their own fine-grained PAT scoped to their repo and you paste it as the agent's override.
- **SSO-protected orgs.** If the shared repo lives inside an org that enforces SAML SSO, the admin's classic token must be SSO-authorized for that org (token settings → **Configure SSO** → Authorize). Otherwise Git operations fail even though the admin user can see the repo in the browser.
- **Admin's role caps what the PAT can do.** If the admin was invited with Read, pushes fail with 403 regardless of what the classic PAT's scopes allow.
- **Blast radius still applies.** The classic PAT can reach *all* repos the admin has collaborator access to — not just the one you intended to deploy. If that's a concern, use a per-agent PAT override instead so each external agent brings its own scoped token.

## Updating the Token Later

Most edits to an existing PAT happen **in-place in GitHub** — the token string stays the same, so you do **not** need to paste a new token into Trinity. The main exceptions are regenerating, rotating, or (for fine-grained tokens) changing permissions in ways that may require org re-approval.

**Fine-grained PAT — editable in-place:**
- **Repository list** — adding or removing repos. Common when you deploy a new agent that needs access to a repo not in the original list.
- **Expiration date** — extend or shorten.
- **Name / description.**

**Fine-grained PAT — when to regenerate:**
- **Permission changes** (e.g. adding `Issues: Read and write`) — the UI may let you save permission changes in-place, but org PAT policies can require re-approval and some deployments see inconsistent behavior. **Safest path: regenerate the token and paste the new one into Trinity Settings.** If the token is on a personal account with no org policy, in-place editing usually works; you can try it first.
- **Token lost / suspected compromise** — always regenerate.

**Classic PAT:**
- Scopes are editable in-place (check/uncheck boxes and save).
- The token string itself only changes if you click **Regenerate**.

After any change that produces a **new token string**, update it in Trinity via **Settings → GitHub Personal Access Token → Test → Save**. Trinity auto-propagates the new token to all running agents that use the platform PAT within seconds.

## For Agents

### Checking PAT Status

```
GET /api/settings/api-keys
```

Returns:
```json
{
  "github": {
    "configured": true,
    "masked": "ghp_****xxxx",
    "source": "settings"
  }
}
```

### Testing a PAT

```
POST /api/settings/api-keys/github/test
Content-Type: application/json

{
  "api_key": "ghp_your_token_here"
}
```

Returns:
```json
{
  "valid": true,
  "username": "your-github-username",
  "has_repo_access": true
}
```

### Saving a PAT

```
PUT /api/settings/api-keys/github
Content-Type: application/json

{
  "api_key": "ghp_your_token_here"
}
```

### MCP Tool

```
initialize_github_sync(agent_name, repo_url)
```

Uses the configured PAT to create/connect a GitHub repository for the agent.

## Troubleshooting

### "Resource not accessible by personal access token"

**Cause:** Token lacks required permissions.

**Fix (Classic):** Regenerate with `repo` scope checked.

**Fix (Fine-Grained):** Add "Contents: Read and write" permission.

### "Must have admin rights to Repository"

**Cause:** For organization repos, your user needs admin access to the org.

**Fix:** Ask an org admin to grant you admin access, or create the repo in your personal account.

### "SSO authorization required"

**Cause:** Organization uses SAML SSO and the token isn't authorized.

**Fix:**
1. Go to [github.com/settings/tokens](https://github.com/settings/tokens)
2. Find your token
3. Click **Configure SSO**
4. Click **Authorize** for the organization

### Token works in test but fails for specific repo

**Cause (Fine-Grained):** The repo isn't in the token's allowed repository list.

**Fix:** Edit the token and add the repository under "Repository access."

### "Bad credentials" error

**Cause:** Token is expired, revoked, or mistyped.

**Fix:** Generate a new token and reconfigure in Trinity Settings.

## Security Best Practices

1. **Use fine-grained tokens in production** — Limit blast radius if token is compromised
2. **Set expiration dates** — 90 days is a reasonable balance
3. **Use separate tokens per environment** — Dev and prod should have different tokens
4. **Never commit tokens to git** — Trinity stores the PAT in the database, not in code
5. **Rotate tokens periodically** — Update in Settings when you regenerate
6. **Revoke unused tokens** — Clean up old tokens at github.com/settings/tokens

## Limitations

- Trinity stores one platform-wide GitHub PAT. All agents share it unless overridden per-agent.
- Fine-grained tokens require GitHub to be configured to allow them (enabled by default for personal accounts).
- Organization-owned fine-grained tokens require org admin approval.
- PAT propagation targets only currently-running agents. Stopped agents receive the updated PAT on next start.
- See "Updating the Token Later" above for what can be edited in-place vs. what requires regenerating the token.

## See Also

**Trinity docs:**
- [GitHub Sync](github-sync.md) — Using git sync after PAT is configured
- [Creating Agents](../agents/creating-agents.md) — Creating agents from GitHub templates
- [Platform Settings](../operations/dashboard.md) — Other settings configuration

**GitHub references:**
- [Managing your personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens) — Official GitHub docs: how PATs work, creation, deletion, security
- [About authentication to GitHub](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/about-authentication-to-github) — Official: full picture of GitHub authentication methods and when to use each
- [Introducing fine-grained personal access tokens](https://github.blog/security/application-security/introducing-fine-grained-personal-access-tokens-for-github/) — GitHub Blog: why fine-grained PATs exist and what problems they solve
- [GitHub Classic vs. Fine-grained Personal Access Tokens](https://www.finecloud.ch/blog/github-classic-vs-fine-grained-personal-access-tokens/) — Third-party explainer: clear side-by-side comparison with practical scenarios
