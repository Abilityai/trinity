# Git remote token scrub — operator runbook (2026-09)

**Applies to**: every Trinity install upgrading past `trinity-enterprise#615`.
**Action required**: none to adopt. One decision to make afterwards — see
[Rotate the platform token](#rotate-the-platform-token).

---

## What changed

Trinity used to persist every agent's git remote with the platform token
inside it:

```
https://oauth2:<TOKEN>@github.com/<org>/<repo>.git
```

That put the token in two places:

1. **`.git/config` on the agent's workspace volume** — at rest, readable by
   the agent itself, for the life of the container.
2. **The process table** — git expands the stored URL into
   `git-remote-https`'s argv on *every* fetch and push, including the 60-second
   sync-health poll, so it was visible to `ps` roughly twice a minute even for
   an agent that ran no git of its own. From there it could reach container
   logs and the logs API.

Remotes are now credential-less. Git asks a **credential helper**
(`/usr/local/bin/git-credential-trinity`, registered in `/etc/gitconfig`) over
stdin, which is neither persisted nor visible in any process listing.

Nothing about *where* a credential is stored changed: per-agent tokens stay in
`agent_git_config.github_pat_encrypted` (AES-256-GCM), and the platform token
stays in Settings.

---

## What happens on upgrade

Nothing you need to run. Three paths converge the existing fleet:

| Path | When it runs | What it covers |
|---|---|---|
| Boot-time one-shot | ~20 s after the backend starts, once per boot (leader-locked) | Every **running** agent — including one the Docker daemon restarted after a host reboot |
| Start hook | Every agent start / restart / recreate | That agent |
| `startup.sh` | Every container start, on the new base image | That container's own `origin` |

Each pass is idempotent: it installs the helper, checks that a credential
actually resolves, and only then removes the token from the URLs. It also
reaches submodule configs (including nested ones) and `url.<base>.insteadOf`
sections.

**Agents on an older base image are covered too** — the backend installs the
helper into the running container, so no rebuild or recreate is required.

---

## The one thing that can be left behind

**The sweep never removes a credential it could not replace.** One class of
agent has its only credential inside its own remote URL: an agent bound with
`POST /api/agents/{name}/git/initialize` on the platform token, which wrote no
`.env` and no per-agent token row. For those, the sweep rescues the token into
`/home/developer/.trinity/git-credential` (mode 0600, never committed) and then
strips the URL.

If that rescue cannot be written — a full disk, a read-only volume — the sweep
**leaves the URL exactly as it was** and files an operator-queue alert:

> *"&lt;agent&gt; has N git remote URL(s) with an embedded credential that Trinity
> deliberately did NOT remove…"*

**To clear it**: give that agent a token of its own (agent → **Git** tab → add
a GitHub token). The next start finishes the job. That is also the better
end state — a per-agent, repo-scoped token is the only change that actually
reduces blast radius.

Terms used in the sweep's log line (`ent#615: remote-token sweep for …`):

| Field | Meaning |
|---|---|
| `remotes_scrubbed` | URLs rewritten without a credential |
| `harvested` | A credential was rescued out of a URL into the helper's file |
| `seeded` | The caller supplied a credential and it was placed |
| `refused` | URLs deliberately left alone — no replacement could be placed |
| `gitmodules_hits` | Credential-bearing URLs found in a **tracked** `.gitmodules` — see below |
| `helper_ok` | A credential resolves in this container |
| `competing_helpers` | Other git credential helpers registered in this container |
| `root_readable` | The sweep could actually read the tree it swept. **`0` means its all-clear is not evidence** — see below |

---

## `.gitmodules` — the case the sweep cannot fix

`.gitmodules` is a **tracked** file. A token there was committed and pushed to
GitHub before this change, and no local sweep can undo that. The sweep reports
it (`gitmodules_hits`) and logs an error naming the agent.

### `root_readable=0` — the sweep could not look

An all-zero report is what a healthy, already-clean agent produces, so on its own
it cannot tell you *"nothing to do"* apart from *"could not even look"*.
`root_readable` is the discriminator.

It is `0` when the platform's maintenance exec cannot traverse the agent's
workspace. The expected cause is **reduced Linux capabilities**: with
`agent_full_capabilities` off the container gets `RESTRICTED_CAPABILITIES`, which
withholds `DAC_OVERRIDE`, so even root inside the container is subject to
ordinary permission checks against the `developer`-owned, mode-0700 home.

Nothing is destroyed — the token stays exactly where it already was — but the
remediation did **not** run. Trinity files one operator-queue alert per agent per
day for this (`ent615-git-token-scrub-unreadable-…`), separate from the refusal
alert because the action differs. Re-run the agent with full capabilities, or
check that agent's remotes by hand:

```bash
docker exec agent-<name> git config --get-regexp 'remote\..*\.(url|pushurl)'
```

**If any agent reports a non-zero `gitmodules_hits`, rotating the platform
token is mandatory, not advisory** — the token is in a repository's history.

---

## Rotate the platform token

Recommended after adoption, for every install:

Encryption and credential-less URLs protect things **going forward**. Backups,
log archives and container-log history taken before the upgrade still contain
what the URLs used to carry.

1. Mint a replacement token (prefer **fine-grained**, minimum scopes).
2. Settings → update the platform GitHub token. Propagation is automatic and
   needs no restart: the new token is written to each agent's `.env`, which is
   the helper's first rung.
3. Confirm a fetch works on a couple of agents (agent → Git tab, or the
   sync-health dot on the dashboard).
4. Revoke the old token on GitHub.

---

## Verifying

From the host:

```bash
# 1. The remote carries no credential.
docker exec agent-<name> git -C /home/developer config --get remote.origin.url
# → https://github.com/<org>/<repo>.git      (no "@" before the host)

# 2. A credential still resolves (prints nothing — the exit code is the answer).
docker exec agent-<name> sh -c \
  'printf "protocol=https\nhost=github.com\n\n" | git credential fill >/dev/null 2>&1; echo $?'
# → 0

# 3. Fetch works.
docker exec agent-<name> git -C /home/developer fetch origin
```

`git credential fill` **prints the credential on stdout** — the redirection in
step 2 is not cosmetic. Do not run it without one.

---

## Rolling back

Downgrading the backend restores the old behaviour for *new* writes: agents
whose URLs were already scrubbed keep working, because the helper stays
installed in the container and `/etc/gitconfig` is untouched by a backend
downgrade. Recreating such an agent on an **older base image** removes the
helper; that agent then needs a token in its `.env` or baked env, which the
normal propagation supplies — or re-upgrade.

Nothing in this change is destructive to a credential: the sweep either
relocates one or declines to touch it.

---

## Not closed by this change

- **An agent can still read its own credential.** `GITHUB_PAT` remains in the
  container environment and in `/home/developer/.env`. Root ownership of the
  helper stops the agent *rewriting* what platform git executes; it does not
  hide the value. A credential broker outside the container is tracked
  separately (`trinity-enterprise#558`).
- **Blast radius is unchanged.** The platform token is still one token for the
  whole fleet. The lever that changes that already exists and is unused:
  per-agent, repo-scoped tokens in each agent's **Git** tab.
