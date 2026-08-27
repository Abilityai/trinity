/**
 * #2411 — the SSH access toggle read the ops payload one level too shallow.
 *
 * `GET /api/settings/ops/config` returns settings nested TWICE:
 *
 *     { settings: { ssh_access_enabled: { value: "true", default: "false", … } } }
 *
 * `Settings.vue` read `response.data.ssh_access_enabled`, which is `undefined`,
 * so `undefined === 'true'` pinned the switch to OFF on every load. Because the
 * click handler computes `!sshAccessEnabled.value`, the FIRST click then always
 * sent `true` — an operator with ephemeral SSH enabled, clicking to disable it,
 * re-enabled it and watched it render as off. The write path was always
 * correct; the mismatch was one-sided, which is why it looked like it stuck for
 * the session and reverted on reload.
 *
 * WHY A PURE MODULE. `Settings.vue` cannot be mounted here — `@vue/test-utils`
 * is not a dependency and vitest runs `environment: 'node'` — so a rule kept
 * inline is a rule no test can reach. That is precisely how a one-line read bug
 * survived inside a security control. The decidable part now lives in
 * `utils/opsSettings.js` and is tested directly; the two assertions that can
 * only be answered from source (that the SFC calls it, and that the flat read
 * is gone) are made by reading the file.
 */
import { describe, it, expect } from 'vitest'
import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'

import { readOpsBool, opsBoolValue } from '../../src/utils/opsSettings'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const settingsVue = fs.readFileSync(
  path.resolve(__dirname, '../../src/views/Settings.vue'), 'utf8',
)

// The payload shape the endpoint actually returns (routers/settings.py
// `get_ops_settings` builds a descriptor per key, not a bare string).
const realPayload = (value) => ({
  settings: {
    ssh_access_enabled: {
      value,
      default: 'false',
      description: 'Enable ephemeral SSH access to agent containers via MCP tool (default: false)',
      is_default: value === 'false',
    },
  },
})

describe('#2411 — the toggle reflects the STORED value', () => {
  it('renders ON when the stored value is true', () => {
    // AC #3, and the case that fails against the shipped read: the old
    // expression reached for `response.data.ssh_access_enabled`, which is
    // undefined here.
    expect(readOpsBool(realPayload('true'), 'ssh_access_enabled')).toBe(true)
  })

  it('renders OFF when the stored value is false', () => {
    expect(readOpsBool(realPayload('false'), 'ssh_access_enabled')).toBe(false)
  })

  it('is not satisfied by the fix the issue itself proposed', () => {
    // Worth pinning. #2411 suggests `response.data.settings?.ssh_access_enabled`,
    // which resolves to the DESCRIPTOR OBJECT — and `object === 'true'` is
    // false, so the toggle would still have rendered OFF while looking fixed.
    // The `.value` hop is the one that matters.
    const descriptor = realPayload('true').settings.ssh_access_enabled
    expect(descriptor === 'true').toBe(false)
    expect(readOpsBool(realPayload('true'), 'ssh_access_enabled')).toBe(true)
  })
})

describe('#2411 — one click inverts the stored value, not a pinned false', () => {
  it('sends false when the stored value is true', () => {
    // The bug in one line: with the read pinned to false, `!false` made every
    // first click send `true` — the opposite of the operator's intent on the
    // only path that matters for a security control.
    const current = readOpsBool(realPayload('true'), 'ssh_access_enabled')
    expect(opsBoolValue(!current)).toBe('false')
  })

  it('sends true when the stored value is false', () => {
    const current = readOpsBool(realPayload('false'), 'ssh_access_enabled')
    expect(opsBoolValue(!current)).toBe('true')
  })
})

describe('#2411 — an unreadable payload degrades to the SAFE direction', () => {
  // AC #4. `ssh_access_enabled` defaults to "false" server-side, so a control
  // that cannot read its own state must not claim the permissive one.
  it.each([
    ['null payload', null],
    ['undefined payload', undefined],
    ['no settings key', {}],
    ['settings is null', { settings: null }],
    ['key absent', { settings: {} }],
    ['descriptor without value', { settings: { ssh_access_enabled: {} } }],
    ['value is null', { settings: { ssh_access_enabled: { value: null } } }],
    ['value is a number', { settings: { ssh_access_enabled: { value: 1 } } }],
    ['settings is a string', { settings: 'true' }],
  ])('%s degrades to false without throwing', (_label, payload) => {
    expect(() => readOpsBool(payload, 'ssh_access_enabled')).not.toThrow()
    expect(readOpsBool(payload, 'ssh_access_enabled')).toBe(false)
  })

  it('only the exact string true is true', () => {
    for (const v of ['True', ' true ', 'TRUE']) {
      // Case and padding are tolerated — an ops value is written by our own
      // PUT, but a hand-edited DB row should not silently read as false.
      expect(readOpsBool(realPayload(v), 'ssh_access_enabled')).toBe(true)
    }
    for (const v of ['yes', '1', 'on', 'false', '']) {
      expect(readOpsBool(realPayload(v), 'ssh_access_enabled')).toBe(false)
    }
  })
})

describe('#2411 — the SFC uses the rule (what only source can answer)', () => {
  it('reads through readOpsBool rather than reaching into the payload', () => {
    expect(settingsVue).toContain("readOpsBool(response.data, 'ssh_access_enabled')")
  })

  it('no longer contains the flat read that caused the bug', () => {
    // The exact expression that shipped. If it reappears — here or for a
    // sibling ops setting surfaced later — this fails.
    expect(settingsVue).not.toMatch(/response\.data\.ssh_access_enabled/)
  })

  it('is the only place that reads ops/config, so there is no sibling mismatch', () => {
    // AC #5, pinned rather than checked once by hand: a second GET added later
    // must come through the same reader.
    const gets = settingsVue.match(/axios\.get\('\/api\/settings\/ops\/config'/g) || []
    expect(gets.length).toBe(1)
  })
})
