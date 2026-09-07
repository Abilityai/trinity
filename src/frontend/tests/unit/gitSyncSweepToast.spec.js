/**
 * #2529 — the Push toast names what the sync untracked.
 *
 * Every sync reconciles the agent's `.gitignore` against the platform's
 * canonical list and `git rm --cached`s whatever now matches a rule. Before
 * #2529 the toast said only "Synced 5 file(s) to GitHub" — a sentence that
 * reads identically whether or not the same push silently deleted committed
 * files from the user's repo. That silence is why two field incidents ran for
 * two months before anyone noticed.
 *
 * The toast is the session-bound half of the report; the durable half is the
 * `gitignore_untracked` operator-queue entry the backend files. The type stays
 * 'success' on a successful sync deliberately — the toast host is a binary
 * success/danger ternary, so any other type paints a completed verb in the
 * danger token.
 */
import { describe, it, expect, vi } from 'vitest'
import { ref } from 'vue'

import { useGitSync } from '../../src/composables/useGitSync.js'

function setup(result) {
  const notify = vi.fn()
  const store = {
    syncToGithub: vi.fn().mockResolvedValue(result),
    getGitStatus: vi.fn().mockResolvedValue({}),
  }
  const agent = ref({ name: 'alpha' })
  return { notify, store, sync: useGitSync(agent, store, notify) }
}

describe('#2529 git sync toast — untracked paths are named', () => {
  it('names the removal count alongside the files pushed', async () => {
    const { notify, sync } = setup({
      success: true,
      files_changed: 5,
      removed_paths: ['.env.example', '.claude/settings.json'],
    })

    await sync.syncToGithub()

    expect(notify).toHaveBeenCalledWith(
      'Synced 5 file(s) to GitHub — 2 untracked by .gitignore',
      'success',
    )
  })

  it('says nothing extra when the sweep removed nothing', async () => {
    const { notify, sync } = setup({ success: true, files_changed: 5, removed_paths: [] })

    await sync.syncToGithub()

    expect(notify).toHaveBeenCalledWith('Synced 5 file(s) to GitHub', 'success')
  })

  it('reports removals even when there was nothing to commit', async () => {
    // The sweep runs BEFORE the agent call, so "no changes to sync" and
    // "the index was mutated" are not mutually exclusive.
    const { notify, sync } = setup({
      success: true,
      files_changed: 0,
      message: 'No changes to sync',
      removed_paths: ['errors.log'],
    })

    await sync.syncToGithub()

    expect(notify).toHaveBeenCalledWith(
      'No changes to sync — 1 untracked by .gitignore',
      'success',
    )
  })

  it('reports removals on a FAILED sync too', async () => {
    // The index mutation happens before the push, so a 409 is exactly as
    // obliged to report it as a 200.
    const { notify, sync } = setup({
      success: false,
      message: 'Sync conflict',
      removed_paths: ['a', 'b', 'c'],
    })

    await sync.syncToGithub()

    expect(notify).toHaveBeenCalledWith(
      'Sync conflict — 3 untracked by .gitignore',
      'error',
    )
  })

  it('survives an older backend that sends no removed_paths at all', async () => {
    const { notify, sync } = setup({ success: true, files_changed: 2 })

    await sync.syncToGithub()

    expect(notify).toHaveBeenCalledWith('Synced 2 file(s) to GitHub', 'success')
  })
})
