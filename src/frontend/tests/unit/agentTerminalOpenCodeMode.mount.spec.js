// @vitest-environment jsdom
/**
 * #2981 checkpoint H (H3) — the terminal's CLI mode for OpenCode.
 *
 * Mounted with xterm stubbed: the fact is the WebSocket URL the component
 * opens, since the backend picks the exec command from its `mode` query.
 * Before H an `opencode` agent asked for `mode=claude`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'

vi.mock('@xterm/xterm', () => ({
  Terminal: class {
    constructor() { this.cols = 80; this.rows = 24 }
    loadAddon() {} open() {} write() {} writeln() {} clear() {} focus() {} dispose() {}
    onData() { return { dispose() {} } }
    onResize() { return { dispose() {} } }
  },
}))
vi.mock('@xterm/addon-fit', () => ({ FitAddon: class { fit() {} dispose() {} } }))
vi.mock('@xterm/addon-web-links', () => ({ WebLinksAddon: class { dispose() {} } }))
vi.mock('@xterm/addon-webgl', () => ({ WebglAddon: class { onContextLoss() {} dispose() {} } }))
vi.mock('@xterm/addon-canvas', () => ({ CanvasAddon: class { dispose() {} } }))
vi.mock('@xterm/xterm/css/xterm.css', () => ({}))
vi.mock('@/utils/platformSession', () => ({ readStoredToken: () => 'tok' }))

let urls
beforeEach(() => {
  urls = []
  vi.useFakeTimers()
  globalThis.ResizeObserver = class { observe() {} disconnect() {} }
  globalThis.WebSocket = class {
    constructor(url) { urls.push(url); this.readyState = 0 }
    send() {} close() {}
  }
  globalThis.WebSocket.OPEN = 1
})
afterEach(() => { vi.useRealTimers() })

const openTerminal = async (runtime, model = '') => {
  const { default: AgentTerminal } = await import('@/components/AgentTerminal.vue')
  const w = mount(AgentTerminal, { props: { agentName: 'a1', runtime, model, autoConnect: true } })
  vi.advanceTimersByTime(200)
  return w
}

describe('AgentTerminal — CLI mode per runtime', () => {
  it('an opencode agent opens mode=opencode with its model, labelled OpenCode', async () => {
    const w = await openTerminal('opencode', 'anthropic/claude-sonnet-5')
    expect(urls).toHaveLength(1)
    expect(urls[0]).toContain('mode=opencode')
    expect(urls[0]).toContain('model=anthropic%2Fclaude-sonnet-5')
    expect(w.text()).toContain('OpenCode')
    w.unmount()
  })

  it.each([
    ['claude-code', 'mode=claude', 'Claude Code'],
    ['gemini-cli', 'mode=gemini', 'Gemini CLI'],
    ['codex', 'mode=codex', 'Codex CLI'],
  ])('%s keeps %s', async (runtime, mode, label) => {
    const w = await openTerminal(runtime)
    expect(urls[0]).toContain(mode)
    expect(w.text()).toContain(label)
    w.unmount()
  })
})
