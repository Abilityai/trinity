# Agents created as agents own their repository (trinity-enterprise#705)

**What changed.** A new `github:` agent created as an *agent* (the default) gets a
working branch it alone writes, with auto-sync on and schedules paused while sync
fails. It only gets these when the repository is its creator's own — not a catalog
template, owned by the GitHub account the creator's own token belongs to — and that
token can push to it. The platform-wide token set by an admin never qualifies;
otherwise it is created pull-only, and the create response's `git_mode` says why.
A *deployment of a codebase* (`kind: "deployment"`) stays pull-only.

**What did not change.** Existing agents keep their mode. The new default applies
to creations after this release. Moving a live agent is per agent and explicit,
using the steps below.

## Migrating a live agent to working-branch mode

> **Not yet supported — this waits for trinity-enterprise#230.** No endpoint
> writes `source_mode` on an existing agent, and the binding must not be edited
> in the database by hand. Until ent#230 ships the one-click retrofit, leave live
> agents in their current mode. To give an agent's work a working branch today,
> do step 1 and then create a **new** agent from the same repository (it takes
> the working-branch default when the repo is yours), or use "Bind to your own
> repo".

When ent#230 lands, do these in order. **Do not skip step 1**: a populated
volume can hold commits and files that exist nowhere else.

1. **Get everything on the container disk into git.**
   - From the agent's Git tab (or `POST /api/agents/{name}/git/sync` with the
     pull-first strategy), push the container's local commits to a branch.
   - Confirm the branch holds them.
   - An agent with uncommitted files: commit them first, or decide explicitly
     which to drop.
2. **Switch the binding to working-branch mode** with ent#230's retrofit. Note
   the working branch it reserves (`trinity/<agent>/<id>`, shown on the Git tab).
3. **Recreate the container.** A mode switch takes effect only on a recreate:
   Stop, then Start, from the agent page. A recreate does **not** move the
   checked-out branch: the workspace volume keeps the branch it had, which for a
   source-mode agent is the default branch (`main`). The working branch is only
   checked out on a fresh clone.
4. **Check out the working branch in the workspace** (the agent's terminal):

   ```bash
   git fetch origin
   git checkout -B trinity/<agent>/<id>   # the branch from step 2
   git push -u origin HEAD
   ```

5. **Fail closed before turning on auto-sync.** This must print `ok`; if it
   prints anything else, stop — do not enable auto-sync:

   ```bash
   head=$(git rev-parse --abbrev-ref HEAD)
   default=$(git symbolic-ref --short refs/remotes/origin/HEAD | sed 's#^origin/##')
   [ "$head" != "$default" ] && [ "${head#trinity/}" != "$head" ] && echo ok
   ```

   Once the binding says working-branch mode, the heartbeat no longer refuses a
   push to the default branch (#3011's refusal only fires in source mode), so an
   agent left on `main` would auto-push straight to it — for a repo that deploys
   on push, a production deploy.
6. **Turn on auto-sync and freeze** in the agent's Settings → Git sync. Then check
   that the next cycle pushes to the working branch: the Git sync health shows
   `success`, and `main` on GitHub is unchanged.

If step 1 cannot be completed, **do not migrate the agent**. A live agent should
not be switched while work exists only on its disk.

## Exclusions

Keep an agent pull-only (`kind: "deployment"`, or leave it in source mode) when:

- **Its repository deploys to production on a push to its default branch.** Leave
  it pull-only until that deploy is gated on something other than a push. The
  auto-sync heartbeat also refuses to push a source-mode agent on the default
  branch (#3011).
- **It is a deployment of a codebase with submodules.** Submodule workspaces are
  not yet first-class in the platform's git paths (#2366).
- **It is built from a shared upstream** it does not own, such as a public
  template. Fork it to your own repository instead ("Fork to own" at creation, or
  "Bind to your own repo" later).
