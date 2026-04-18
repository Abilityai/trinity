/**
 * S2 — Parallel-history detection in useGitSync composable.
 *
 * Verifies that when the agent-server status response carries a stale or
 * absent common-ancestor AND behind > 0, the composable flips an
 * `isParallelHistory` flag so the parallel-history modal variant renders
 * instead of the two-button Pull First / Force Push modal.
 */
import { describe, it, expect, vi } from 'vitest'
import { ref, nextTick } from 'vue'

// Import path is relative to this file: tests/git-sync/...
import { useGitSync } from '../../src/frontend/src/composables/useGitSync.js'

function makeHarness(statusPayload) {
  const agentRef = ref({ name: 'polygon-vybe', status: 'running' })
  const agentsStore = {
    getGitStatus: vi.fn().mockResolvedValue(statusPayload),
    syncToGithub: vi.fn(),
    pullFromGithub: vi.fn(),
  }
  const showNotification = vi.fn()
  const sync = useGitSync(agentRef, agentsStore, showNotification)
  return { sync, agentsStore }
}

describe('useGitSync parallel-history detection', () => {
  it('flips isParallelHistory when common_ancestor is stale AND behind>0', async () => {
    const { sync } = makeHarness({
      git_enabled: true,
      branch: 'trinity/polygon-vybe/abc12345',
      pull_branch: 'main',
      ahead: 2,
      behind: 1,
      changes_count: 0,
      common_ancestor_sha: '1111111111111111111111111111111111111111',
      common_ancestor_age_days: 90,
    })

    await sync.loadGitStatus()
    await nextTick()

    expect(sync.isParallelHistory.value).toBe(true)
  })

  it('flips isParallelHistory when common_ancestor_sha is empty AND behind>0', async () => {
    const { sync } = makeHarness({
      git_enabled: true,
      branch: 'trinity/polygon-vybe/abc12345',
      pull_branch: 'main',
      ahead: 1,
      behind: 1,
      changes_count: 0,
      common_ancestor_sha: '',
      common_ancestor_age_days: 0,
    })

    await sync.loadGitStatus()
    await nextTick()

    expect(sync.isParallelHistory.value).toBe(true)
  })

  it('does NOT flip isParallelHistory for a simple behind (recent shared ancestor)', async () => {
    const { sync } = makeHarness({
      git_enabled: true,
      branch: 'trinity/polygon-vybe/abc12345',
      pull_branch: 'main',
      ahead: 0,
      behind: 3,
      changes_count: 0,
      common_ancestor_sha: '2222222222222222222222222222222222222222',
      common_ancestor_age_days: 1,
    })

    await sync.loadGitStatus()
    await nextTick()

    expect(sync.isParallelHistory.value).toBe(false)
  })

  it('does NOT flip isParallelHistory when behind is 0 even if ancestor is old', async () => {
    const { sync } = makeHarness({
      git_enabled: true,
      branch: 'trinity/polygon-vybe/abc12345',
      pull_branch: 'main',
      ahead: 5,
      behind: 0,
      changes_count: 0,
      common_ancestor_sha: '3333333333333333333333333333333333333333',
      common_ancestor_age_days: 400,
    })

    await sync.loadGitStatus()
    await nextTick()

    expect(sync.isParallelHistory.value).toBe(false)
  })

  it('surfaces pull_branch from the status payload (for label-agnostic copy)', async () => {
    const { sync } = makeHarness({
      git_enabled: true,
      branch: 'trinity/polygon-vybe/abc12345',
      pull_branch: 'trunk',
      ahead: 1,
      behind: 1,
      changes_count: 0,
      common_ancestor_sha: '',
      common_ancestor_age_days: 0,
    })

    await sync.loadGitStatus()
    await nextTick()

    expect(sync.pullBranch.value).toBe('trunk')
  })
})
