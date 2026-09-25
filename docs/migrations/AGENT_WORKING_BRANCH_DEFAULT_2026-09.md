# Agents created as agents own their repository (trinity-enterprise#705)

**What changed.** A new `github:` agent created as an *agent* (the default) gets a
working branch it alone writes, with auto-sync on and schedules paused while sync
fails. It only gets these when its GitHub token can push to that repository;
otherwise it is created pull-only, and the create response's `git_mode` says why.
A *deployment of a codebase* (`kind: "deployment"`) stays pull-only.

**What did not change.** Existing agents keep their mode. The new default applies
to creations after this release. Moving a live agent is per agent and explicit,
using the steps below.

## Migrating a live agent to working-branch mode

Do these in order. **Do not skip step 1**: a populated volume can hold commits
and files that exist nowhere else.

1. **Get everything on the container disk into git.**
   - From the agent's Git tab (or `POST /api/agents/{name}/git/sync` with the
     pull-first strategy), push the container's local commits to a branch.
   - Confirm the branch holds them.
   - An agent with uncommitted files: commit them first, or decide explicitly
     which to drop.
2. **Switch the binding to working-branch mode.**
   - Set the agent's git config to `source_mode = 0` with a reserved working
     branch.
   - Once trinity-enterprise#230 lands, this is the one-click retrofit.
3. **Recreate the container.** A mode switch takes effect only on a recreate:
   Stop, then Start, from the agent page.
4. **Turn on auto-sync and freeze** in the agent's Settings → Git sync. Then check
   that the next cycle pushes: the Git sync health shows `success`.

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
