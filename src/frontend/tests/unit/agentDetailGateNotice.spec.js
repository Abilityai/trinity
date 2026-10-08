/**
 * trinity#3274 — the agent page hands its toast host to the two panels whose
 * gated runs answer with a notice, and that host shows an `info` notice in the
 * info family. It rendered every non-`success` type red, so a "waiting for
 * approval" toast (and the existing "Restarting agent…" info toast) read as an
 * error.
 *
 * What the panels DO with `notify` is mounted in `skillGateSenders.spec.js`
 * (each calls `notify(message, 'info', …)` on a 202 `pending_approval`).
 *
 * @source-text-pin: AgentDetail.vue is never mounted by a spec — it runs five
 * composables with timers and the agents store's fetch chain before any tab
 * renders. What is pinned here is call-site shape only: that both panel
 * elements receive `:notify="showNotification"`, and that the toast's class
 * chain has an `info` arm in the `status-info` family ahead of the red
 * fallback. A panel mounted on its own passes identically with the prop
 * missing, which is the regression this guards.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { stripComments } from './helpers/stripComments'

const source = stripComments(readFileSync(
  fileURLToPath(new URL('../../src/views/AgentDetail.vue', import.meta.url)), 'utf8'))

function element(tag) {
  const start = source.indexOf(`<${tag}`)
  expect(start, `<${tag}> is no longer in AgentDetail.vue`).toBeGreaterThan(-1)
  return source.slice(start, source.indexOf('/>', start))
}

describe('AgentDetail — the gated-run notice reaches a toast', () => {
  it.each(['PlaybooksPanel', 'DashboardPanel'])('%s gets the toast host', (tag) => {
    expect(element(tag)).toMatch(/:notify="showNotification"/)
  })

  it('the toast has an info arm in the status-info family before the red fallback', () => {
    const info = source.indexOf("notification.type === 'info' ? 'bg-status-info-100")
    const fallback = source.indexOf(": 'bg-status-danger-100", info)
    expect(info).toBeGreaterThan(-1)
    expect(fallback).toBeGreaterThan(info)
  })
})
