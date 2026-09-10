import { test, expect } from '@playwright/test'

const API = '**/api/enterprise/client-portal'
const AGENT = 'e2e-model-choice'
const SESSION = 'e2e-model-thread'
const MODELS = [
  { id: 'claude-opus-5', label: 'Claude Opus 5', tier: 'Most capable' },
  { id: 'claude-sonnet-5', label: 'Claude Sonnet 5', tier: 'Balanced — fast and smart' },
  { id: 'claude-haiku-4-5-20251001', label: 'Claude Haiku 4.5', tier: 'Fastest' },
]
const DEFAULT = { model: MODELS[1].id, label: MODELS[1].label }
const json = (body) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

// Provider responses are controlled here. This proves rendered recovery and
// geometry, not that a live provider honors the chosen model.
async function workspace(page) {
  const state = { preference: {}, outcome: null, sent: [] }
  await page.route(`${API}/my-agents*`, r => r.fulfill(json({
    client_email: 'e2e@example.com', model_options: MODELS,
    agents: [{ name: AGENT, availability: 'ready', playbooks: [],
      model_default: DEFAULT }],
    multi_agent_chat_available: false,
  })))
  await page.route(`${API}/sessions*`, r => r.fulfill(json({ sessions: [
    { id: SESSION, agent_name: AGENT, title: 'Model checks', message_count: 0 },
  ] })))
  await page.route(`${API}/chat-state*`, r => r.fulfill(json({ chats: [] })))
  await page.route(`${API}/agents/${AGENT}/history*`, r => r.fulfill(json({
    agent_name: AGENT, session_id: SESSION, messages: [], last_turn_outcome: state.outcome,
  })))
  await page.route('**/api/users/me/preferences', r => r.fulfill(json({
    preferences: { workspace_model: { value: state.preference, updated_at: '1' } },
  })))
  await page.route('**/api/users/me/preferences/workspace_model', r => {
    state.preference = r.request().postDataJSON().value
    return r.fulfill(json({ key: 'workspace_model', value: state.preference, updated_at: '1' }))
  })
  await page.route(`${API}/agents/${AGENT}/chat/stream`, r => {
    state.sent.push(r.request().postDataJSON())
    state.outcome = { execution_id: 'e2e-model-rejection', category: 'invalid_model', retryable: true,
      message: "That model is unavailable. Switched back to the agent's default." }
    return r.fulfill(json({ execution_id: state.outcome.execution_id, session_id: SESSION }))
  })
  await page.route(`${API}/agents/${AGENT}/executions/*/stream`, r => r.fulfill({
    status: 200, contentType: 'text/event-stream', body: 'data: {"type":"stream_end"}\n\n',
  }))
  await page.goto(`/workspace/c/${SESSION}`)
  await expect(page.getByTestId('portal-model-picker')).toBeVisible()
  return state
}

// #2662: the composer is ONE shell — the field on top, the controls in a row
// inside it, the model picker right-aligned beside Send. What this file is
// answerable for is that shape, rendered: the picker inside the shell and on
// Send's row, and the field spanning the shell rather than sharing a row with
// 44px buttons. That sharing is what this test used to police from the other
// direction — the single-row layout left 34px to type in when ent#403 put the
// picker beside the buttons, and 143px of a 351px form even without it.
//
// Deliberately NOT an absolute pixel floor. The row's total width depends on
// the scrollbar the host platform draws — macOS overlays it and reserves
// nothing, Linux CI reserves ~15px — so the identical correct layout measures
// differently on a developer's machine and on the runner, and a floor between
// the two passes locally and fails in CI for a reason no change to this
// feature can fix. Every assertion below is therefore taken from ONE render and
// stated relative to the boxes around it, which is true under either scrollbar.
for (const width of [375, 768, 1280]) {
  test(`@smoke the composer is one shell — field on top, picker beside Send — at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await workspace(page)

    const input = page.locator('textarea')
    await input.fill('A readable message for the agent')
    const field = await input.boundingBox()
    const formEl = input.locator('xpath=ancestor::form')
    const form = await formEl.boundingBox()
    const pickerEl = page.getByTestId('portal-model-picker')
    const picker = await pickerEl.boundingBox()
    const sendEl = page.getByRole('button', { name: 'Send', exact: true })
    const send = await sendEl.boundingBox()

    // 1. The picker is INSIDE the shell, under the field, on Send's row, and
    //    immediately left of Send. Each of the three ways this has shipped or
    //    could regress fails a different line: above the form (ent#403), below
    //    it as a footer (the first cut of #2662), or drifted into the left
    //    cluster of icon buttons.
    expect(picker.y).toBeGreaterThanOrEqual(field.y + field.height)
    expect(picker.y + picker.height).toBeLessThanOrEqual(form.y + form.height)
    expect(picker.x + picker.width).toBeLessThanOrEqual(send.x)
    expect(Math.abs((picker.y + picker.height / 2) - (send.y + send.height / 2))).toBeLessThanOrEqual(2)
    const buttons = await formEl.locator('button').all()
    expect(buttons.length).toBeGreaterThan(1)
    for (const button of buttons) {
      const box = await button.boundingBox()
      if (box.x >= send.x) continue                       // Send itself
      expect(box.x + box.width, 'an icon button sits right of the picker').toBeLessThanOrEqual(picker.x)
    }

    // 1b. It is a chat control, not a form field — the original complaint.
    //     Ghost means no visible border and no fill of its own. Rendered, not
    //     source-matched: a class list proves the classes were written, not
    //     that they survived to the box (#2659). And it is the element that
    //     yields when the row runs out — at 375px it truncates; Send does not.
    const chrome = await pickerEl.evaluate((el) => {
      const cs = getComputedStyle(el)
      return { border: cs.borderTopColor, background: cs.backgroundColor }
    })
    const invisible = (c) => c === 'rgba(0, 0, 0, 0)' || c === 'transparent'
    expect(invisible(chrome.border)).toBe(true)
    expect(invisible(chrome.background)).toBe(true)
    expect(picker.width).toBeLessThanOrEqual(272)          // the ghost `max-w-[17rem]` ceiling — fits the longest label at 13.5px
    expect(send.width).toBe(44)

    // 2. The field spans the shell. Stacked, it competes with nothing for width,
    //    so it is the shell's inner width — the form less the shell's own
    //    padding — at EVERY viewport. Relative, so it holds under either
    //    scrollbar and any padding tweak short of putting a button back beside
    //    it: before #2662 this was 41% of the form at 375px.
    expect(field.width).toBeGreaterThanOrEqual(form.width * 0.9)
    expect(await input.evaluate(el => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(1)

    // 3. Every action box keeps its 44px touch target (#2259) — the cheap way
    //    to fit a picker on the row would be to shrink these. The PICKER is one
    //    of those boxes (#2662): a 30px select beside 44px buttons is a 30px tap
    //    target on a phone. It needs its own line because nothing above catches
    //    it — the loop below walks `<button>`s only, and the centre-alignment
    //    check in 1 passes for a short picker exactly as it does for a tall one.
    expect(picker.height).toBe(44)
    for (const button of buttons) {
      const action = await button.boundingBox()
      expect(action.width).toBe(44)
      expect(action.height).toBe(44)
    }
    // 4. The shell is ONE click target. The chrome moved off the textarea, so
    //    the box's own 8px padding band is not the field any more — but it still
    //    reads as the field, and before #2662 the box WAS the textarea. Measured
    //    rather than asserted in source: the guard that keeps this from stealing
    //    a control's click is easy to write in a way that never fires at all.
    //
    //    Mid-WIDTH, not a corner: the shell is `rounded-2xl` and hit-testing
    //    respects border-radius, so a point 4px in from the right edge and 4px
    //    down is OUTSIDE the 16px arc — the click falls through the shell and
    //    reads BODY against a perfectly working handler.
    await page.mouse.click(form.x + form.width / 2, form.y + 4)
    expect(await page.evaluate(() => document.activeElement?.tagName)).toBe('TEXTAREA')

    await expect(sendEl).toBeVisible()
  })
}

test('@smoke rejected model clears the choice; a fresh choice survives stale verdict reload', async ({ page }) => {
  const state = await workspace(page)
  const picker = page.getByTestId('portal-model-picker')
  await picker.selectOption(MODELS[0].id)
  await expect.poll(() => state.preference[AGENT]).toBe(MODELS[0].id)
  await page.locator('textarea').fill('Check the selected model')
  await page.getByRole('button', { name: 'Send', exact: true }).click()
  await expect(picker).toHaveValue('')
  expect(state.sent[0].model).toBe(MODELS[0].id)
  await expect(page.getByText(state.outcome.message).first()).toBeVisible()
  await expect.poll(() => state.preference[AGENT]).toBeUndefined()
  await picker.selectOption(MODELS[2].id)
  await expect.poll(() => state.preference[AGENT]).toBe(MODELS[2].id)
  await page.reload()
  await expect(picker).toHaveValue(MODELS[2].id)
  // A second reload guards against a delayed stale-outcome preference write.
  await page.reload()
  await expect(picker).toHaveValue(MODELS[2].id)
})
