// @vitest-environment jsdom
/**
 * trinity-enterprise#1117 — the Credentials panel writes `.env` in the agent's
 * own encoding.
 *
 * The panel rewrites the whole file on every save, so every credential in it
 * goes through the panel's reader and writer again. The reader (and the
 * agent's `unquote_env_value`) turns `\\` back into `\`; the writer escaped
 * only `"`. A credential containing two backslashes therefore lost one on the
 * next save of ANY credential, and the agent then authenticated with a string
 * nobody supplied.
 *
 * Contract: tests/fixtures/ent1117-env-escape-cases.json (repo root). Each
 * row's `line` is what the agent's own `format_env_line("KEY", value)` emits —
 * generated from it, never hand-typed. The pytest twin
 * `tests/unit/test_ent1117_s11_env_escape_table.py` proves every row against
 * the agent's real encoder and decoder (and the backend's `agent_env_patch`
 * writer); THIS spec proves the browser side agrees with the same rows, so the
 * two cannot drift apart without one of them going red.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('axios', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } }
  return { default: { ...inst, create: () => inst } }
})
vi.mock('../../src/api', () => ({
  default: { get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))

import { formatEnvLine, unquoteEnvValue } from '../../src/utils/envFile'
import { useAgentsStore } from '../../src/stores/agents'
import CredentialsPanel from '../../src/components/CredentialsPanel.vue'

// `__dirname`, not `import.meta.url`: under jsdom the module URL is not a file: URL.
const FIXTURE = join(__dirname, '..', '..', '..', '..', 'tests', 'fixtures', 'ent1117-env-escape-cases.json')
const rows = JSON.parse(readFileSync(FIXTURE, 'utf-8'))

const B = '\\'
const rhs = (line) => line.slice(line.indexOf('=') + 1)

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('the shared value -> line table', () => {
  it('carries the cases the defect is about', () => {
    const byId = Object.fromEntries(rows.map((r) => [r.id, r.value]))
    expect(byId['double-backslash']).toBe(`a${B}${B}b`)
    expect(byId['embedded-quote']).toBe('a"b')
    expect(byId['trailing-backslash']).toBe(`x${B}`)
    expect(byId['windows-path']).toBe(`C:${B}path${B}new`)
    expect(byId['backslash-then-quote']).toBe(`a${B}"b`)
    expect(rows.length).toBeGreaterThanOrEqual(15)
  })

  for (const row of rows) {
    it(`${row.id}: encodes to the agent's line and decodes back exactly`, () => {
      expect(formatEnvLine('KEY', row.value)).toBe(row.line)
      expect(unquoteEnvValue(rhs(row.line))).toBe(row.value)
    })

    it(`${row.id}: survives three read-write round trips unchanged`, () => {
      let value = row.value
      for (let i = 0; i < 3; i++) value = unquoteEnvValue(rhs(formatEnvLine('KEY', value)))
      expect(value).toBe(row.value)
    })
  }
})

describe('Credentials panel: saving one credential keeps every other one exact', () => {
  it('Quick Inject rewrites the existing lines byte-for-byte', async () => {
    const existing = rows.map((r, i) => r.line.replace(/^KEY=/, `K_${i}=`))
    const store = useAgentsStore()
    store.getCredentialStatus = vi.fn().mockResolvedValue({
      files: { '.env': { exists: true, size: 12, modified: '2026-11-05T00:00:00Z' } },
    })
    store.getCredentialRequirements = vi.fn().mockResolvedValue(null)
    store.downloadAgentFile = vi.fn().mockResolvedValue(
      '# Credential file - managed by Trinity\n\n' + existing.join('\n') + '\n')
    store.injectCredentials = vi.fn().mockResolvedValue({})

    const w = mount(CredentialsPanel, {
      props: { agentName: 'shared-agent', agentStatus: 'running' },
      global: { stubs: { CredentialSetupChecklist: true } },
    })
    await flushPromises()
    await w.find('textarea[rows="5"]').setValue('NEW_KEY=new-value')
    const inject = w.findAll('button').find((x) => x.text().trim() === 'Inject')
    expect(inject, 'no "Inject" button').toBeTruthy()
    await inject.trigger('click')
    await flushPromises()

    expect(store.injectCredentials).toHaveBeenCalledTimes(1)
    const written = store.injectCredentials.mock.calls[0][1]['.env'].split('\n')
    for (const line of existing) expect(written).toContain(line)
    expect(written).toContain('NEW_KEY="new-value"')
  })
})
