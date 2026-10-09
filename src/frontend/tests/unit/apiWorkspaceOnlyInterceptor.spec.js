/**
 * trinity-enterprise#837 — the shared `api` client reports the operator floor's
 * named 403 to the one handler (and only that 403), and still rejects the
 * original error so the caller's own handling runs. Driven through the REAL
 * `api` instance with a stub adapter, so the interceptor in `api.js` is what runs.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import api from '../../src/api'
import { setWorkspaceOnlyHandler } from '../../src/utils/workspaceOnly'

function failWith(status, detail) {
  return async (config) => {
    const error = new Error(`HTTP ${status}`)
    error.config = config
    error.response = { status, data: { detail }, headers: {}, config }
    throw error
  }
}

afterEach(() => setWorkspaceOnlyHandler(null))

describe('api.js and the workspace_only refusal', () => {
  it('reports the named refusal and still rejects the original error', async () => {
    const handler = vi.fn()
    setWorkspaceOnlyHandler(handler)
    const refusal = { code: 'workspace_only', message: 'This account works in the Workspace. Open /workspace.' }
    await expect(
      api.get('/api/agents', { adapter: failWith(403, refusal) }),
    ).rejects.toMatchObject({ response: { status: 403 } })
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('leaves every other 403 to its caller', async () => {
    const handler = vi.fn()
    setWorkspaceOnlyHandler(handler)
    await expect(
      api.post('/api/agents/x/share', {}, { adapter: failWith(403, 'Admin access required') }),
    ).rejects.toBeTruthy()
    expect(handler).not.toHaveBeenCalled()
  })
})
