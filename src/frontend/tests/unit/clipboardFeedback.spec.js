/**
 * Copying, and saying what happened (#2515).
 *
 * The branches worth pinning are the ones a browser rarely shows you and a
 * manual check never reaches: an insecure origin (where `navigator.clipboard`
 * is simply undefined), a denied permission, and a clipboard that throws
 * something else. The old behaviour for all three was a `console.error` — a
 * control that silently did nothing.
 */
import { describe, it, expect, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import {
  copyText, copyFeedback, copyToClipboard, COPY_FEEDBACK_TTL_MS,
  COPY_CODE_LABEL, COPY_CODE_ARIA, COPY_MESSAGE_ARIA,
} from '../../src/utils/clipboard.js'

const here = dirname(fileURLToPath(import.meta.url))
const SRC = resolve(here, '../../src')

/** A stand-in document whose execCommand outcome the test chooses. */
function fakeDoc(execResult) {
  const removed = []
  const doc = {
    body: { appendChild: vi.fn() },
    createElement: () => ({
      style: {},
      setAttribute() {},
      select() {},
      setSelectionRange() {},
      remove() { removed.push(true) },
    }),
    execCommand: vi.fn(() => execResult),
    removed,
  }
  return doc
}

describe('copyText — the happy path', () => {
  it('uses the async clipboard when it is there', async () => {
    const writeText = vi.fn(async () => {})
    expect(await copyText('ls -la', { clipboard: { writeText } }))
      .toEqual({ ok: true, via: 'clipboard' })
    expect(writeText).toHaveBeenCalledWith('ls -la')
  })

  it('calls writeText FIRST, with nothing awaited before it', async () => {
    // Safari grants clipboard access only inside the task the click started, so
    // an await ahead of the write spends the transient activation and the copy
    // fails on that browser alone.
    const order = []
    const clipboard = { writeText: async () => { order.push('writeText') } }
    const doc = { get body() { order.push('doc'); return { appendChild() {} } } }
    await copyText('x', { clipboard, doc })
    expect(order[0]).toBe('writeText')
  })

  it('coerces a non-string rather than writing "undefined" silently', async () => {
    const writeText = vi.fn(async () => {})
    await copyText(null, { clipboard: { writeText } })
    expect(writeText).toHaveBeenCalledWith('')
  })
})

describe('copyText — insecure origins, where the API is simply absent', () => {
  it('falls back to execCommand, so plain-http deploys still copy', async () => {
    // `navigator.clipboard` is undefined over http://<lan-or-tailscale-ip>,
    // which is a first-class Trinity topology, not a misconfiguration. Without
    // the fallback, Copy is permanently dead there and the honest failure
    // message is all those operators would ever see.
    const doc = fakeDoc(true)
    expect(await copyText('payload', { clipboard: null, doc }))
      .toEqual({ ok: true, via: 'execCommand' })
    expect(doc.execCommand).toHaveBeenCalledWith('copy')
  })

  it('removes the scratch textarea afterwards', async () => {
    const doc = fakeDoc(true)
    await copyText('x', { clipboard: null, doc })
    expect(doc.removed.length).toBe(1)
  })

  it('removes it even when execCommand throws', async () => {
    const doc = fakeDoc(true)
    doc.execCommand = () => { throw new Error('nope') }
    expect(await copyText('x', { clipboard: null, doc })).toEqual({ ok: false, reason: 'unavailable' })
    expect(doc.removed.length).toBe(1)
  })

  it('reports unavailable when execCommand declines, or is absent too', async () => {
    expect(await copyText('x', { clipboard: null, doc: fakeDoc(false) }))
      .toEqual({ ok: false, reason: 'unavailable' })
    expect(await copyText('x', { clipboard: null, doc: null }))
      .toEqual({ ok: false, reason: 'unavailable' })
    expect(await copyText('x', { clipboard: {}, doc: {} }))
      .toEqual({ ok: false, reason: 'unavailable' })
  })
})

describe('copyText — refusals are told apart', () => {
  const reject = (name) => ({
    writeText: async () => { throw Object.assign(new Error('x'), { name }) },
  })

  it('a denied permission is its own answer — the reader can act on it', async () => {
    expect(await copyText('x', { clipboard: reject('NotAllowedError') }))
      .toEqual({ ok: false, reason: 'denied' })
    expect(await copyText('x', { clipboard: reject('SecurityError') }))
      .toEqual({ ok: false, reason: 'denied' })
  })

  it('anything else is an error, not a denial', async () => {
    expect(await copyText('x', { clipboard: reject('DataCloneError') }))
      .toEqual({ ok: false, reason: 'error' })
  })

  it('does NOT fall back after a real refusal', async () => {
    // A denial is an answer. Retrying through execCommand would paper over it
    // and make the two states indistinguishable to the caller.
    const doc = fakeDoc(true)
    await copyText('x', { clipboard: reject('NotAllowedError'), doc })
    expect(doc.execCommand).not.toHaveBeenCalled()
  })

  it('never throws, whatever it is handed', async () => {
    const nasty = [
      { clipboard: { writeText: () => { throw new Error('sync') } } },
      { clipboard: { writeText: async () => { throw null } } },
      { clipboard: { writeText: 'not a function' }, doc: null },
    ]
    for (const opts of nasty) {
      await expect(copyText('x', opts)).resolves.toBeTruthy()
    }
  })

  it('never puts the copied text anywhere but the clipboard', async () => {
    // The payload is an agent's output and may be the credential the operator
    // just asked it for; a console.error would put it in every browser log.
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const log = vi.spyOn(console, 'log').mockImplementation(() => {})
    await copyText('sk-secret', { clipboard: { writeText: async () => { throw new Error('x') } } })
    expect(spy).not.toHaveBeenCalled()
    expect(warn).not.toHaveBeenCalled()
    expect(log).not.toHaveBeenCalled()
    spy.mockRestore(); warn.mockRestore(); log.mockRestore()
  })
})

describe('copyFeedback — identity in the WORD, not only the colour', () => {
  it('names each outcome distinctly', () => {
    expect(copyFeedback({ ok: true, via: 'clipboard' })).toEqual({ label: 'Copied', tone: 'ok' })
    expect(copyFeedback({ ok: false, reason: 'unavailable' }).label).toBe('Copy unavailable')
    expect(copyFeedback({ ok: false, reason: 'denied' }).label).toBe('Copy blocked')
    expect(copyFeedback({ ok: false, reason: 'error' }).label).toBe('Copy failed')
    const labels = ['unavailable', 'denied', 'error'].map((r) => copyFeedback({ ok: false, reason: r }).label)
    expect(new Set(labels).size).toBe(3)
  })

  it('degrades to a failure rather than claiming success on a shape it does not know', () => {
    for (const bad of [null, undefined, {}, { ok: false }, { ok: false, reason: 'wat' }]) {
      expect(copyFeedback(bad).tone, JSON.stringify(bad)).toBe('error')
    }
  })

  it('every failure is toned "error" and the success "ok"', () => {
    expect(copyFeedback({ ok: true }).tone).toBe('ok')
    expect(copyFeedback({ ok: false, reason: 'denied' }).tone).toBe('error')
  })
})

describe('the constants the controls restore to', () => {
  it('are exported, so the SFC restores a constant rather than a captured value', () => {
    expect(COPY_CODE_LABEL).toBe('Copy')
    expect(COPY_CODE_ARIA).toBe('Copy code')
    expect(COPY_MESSAGE_ARIA).toBe('Copy message')
  })

  it('the feedback window is brief, per the AC', () => {
    expect(COPY_FEEDBACK_TTL_MS).toBeGreaterThan(0)
    expect(COPY_FEEDBACK_TTL_MS).toBeLessThanOrEqual(3000)
  })
})

describe('the pre-existing API this module already had', () => {
  it('still exports copyToClipboard', () => {
    // #2515 added a second entry point to this file. `copyToClipboard` is the
    // ORIGINAL one and has four live callers, none of which is otherwise under
    // test — so replacing the module rather than extending it would have
    // shipped four dead Copy buttons with a green suite. This is that guard.
    expect(typeof copyToClipboard).toBe('function')
  })

  it('and every caller of it still IMPORTS it', () => {
    // The import, not just the identifier: three of these files also define a
    // LOCAL `copyText` wrapper that calls `copyToClipboard`, so a bare mention
    // matches the call site and would keep passing with the import deleted —
    // which is the shape of the failure this guard exists for.
    const callers = [
      'components/A2aPanel.vue',
      'components/ConnectorChannelPanel.vue',
      'components/McpExposedPanel.vue',
      'components/settings/McpKeysTab.vue',
    ]
    for (const rel of callers) {
      const src = readFileSync(resolve(SRC, rel), 'utf8')
      expect(src, rel).toMatch(/import \{[^}]*\bcopyToClipboard\b[^}]*\} from ['"][^'"]*utils\/clipboard['"]/)
    }
  })
})
