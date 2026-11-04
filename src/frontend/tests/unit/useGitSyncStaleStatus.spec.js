/**
 * trinity-enterprise#950 — the agent header shows the git status on every tab,
 * so a poll answer that lands after the page moved on must not be written.
 *
 * AgentDetail clears `gitStatus` on agent switch and on stop; an in-flight
 * `GET …/git/status` for the PREVIOUS agent (or for the agent before it
 * stopped) would otherwise land after that reset and put agent A's repo and
 * counts under agent B's name until B's first poll.
 */
import { describe, expect, it } from 'vitest'
import { ref } from 'vue'
import { useGitSync } from '../../src/composables/useGitSync'

function deferredStore() {
  const pending = []
  return {
    pending,
    getGitStatus: (name) => new Promise((resolve) => pending.push({ name, resolve })),
  }
}

describe('useGitSync drops a git status answer that is no longer current', () => {
  it('an answer for the previous agent is discarded', async () => {
    const agent = ref({ name: 'alpha', status: 'running' })
    const store = deferredStore()
    const { gitStatus, refreshGitStatus } = useGitSync(agent, store, () => {})
    refreshGitStatus()
    agent.value = { name: 'beta', status: 'running' }
    gitStatus.value = null
    store.pending[0].resolve({ git_enabled: true, remote_url: 'https://github.com/acme/alpha' })
    await Promise.resolve(); await Promise.resolve()
    expect(gitStatus.value).toBeNull()
  })

  it('an answer that lands after the agent stopped is discarded', async () => {
    const agent = ref({ name: 'alpha', status: 'running' })
    const store = deferredStore()
    const { gitStatus, refreshGitStatus } = useGitSync(agent, store, () => {})
    refreshGitStatus()
    agent.value = { ...agent.value, status: 'stopped' }
    gitStatus.value = null
    store.pending[0].resolve({ git_enabled: true, remote_url: 'https://github.com/acme/alpha' })
    await Promise.resolve(); await Promise.resolve()
    expect(gitStatus.value).toBeNull()
  })

  it('a current answer is still written', async () => {
    const agent = ref({ name: 'alpha', status: 'running' })
    const store = deferredStore()
    const { gitStatus, refreshGitStatus } = useGitSync(agent, store, () => {})
    refreshGitStatus()
    store.pending[0].resolve({ git_enabled: true, remote_url: 'https://github.com/acme/alpha' })
    await Promise.resolve(); await Promise.resolve()
    expect(gitStatus.value?.remote_url).toBe('https://github.com/acme/alpha')
  })
})
