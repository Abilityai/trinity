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

// The composer's action row is `flex items-end gap-2`: every button is a 44px
// box (#2259) and the field takes whatever is left. So for B buttons the row
// spends exactly `B * (44 + 8)` — B boxes and, whatever the nesting, B gaps
// (each row of N items has N-1 gaps, and the field's wrapper is the one item
// that is not a button) — and a correct layout gives the field the remainder.
const ROW_COST_PER_BUTTON = 44 + 8

// What this PR is answerable for is that the picker costs the composer NOTHING
// horizontally — the defect it fixes is a select sharing the action row and
// leaving 34px to type in.
//
// Deliberately NOT an absolute pixel floor. The row's total width depends on
// the scrollbar the host platform draws — macOS overlays it and reserves
// nothing, Linux CI reserves ~15px — so the identical correct layout measures
// 143px of field on a developer's machine and 128px on the runner, and a floor
// between the two passes locally and fails in CI for a reason no change to this
// feature can fix. Every assertion below is therefore taken from ONE render and
// stated relative to the row it is in, which is true under either scrollbar.
for (const width of [375, 768, 1280]) {
  test(`@smoke the model picker costs the composer no typing space at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await workspace(page)

    const input = page.locator('textarea')
    await input.fill('A readable message for the agent')
    const field = await input.boundingBox()
    const form = await input.locator('xpath=ancestor::form').boundingBox()
    const picker = await page.getByTestId('portal-model-picker').boundingBox()

    // 1. The picker is on its OWN row — it ends above where the action row
    //    begins. This is the regression itself: put it back beside the buttons
    //    and this fails at every width.
    expect(picker.y + picker.height).toBeLessThanOrEqual(form.y)
    expect(picker.x + picker.width).toBeLessThanOrEqual(width)

    // 2. The field takes ALL the slack the buttons leave, so the picker's row
    //    is free and nothing else has crept into the action row either.
    const buttons = await input.locator('xpath=ancestor::form').locator('button').all()
    expect(buttons.length).toBeGreaterThan(0)
    expect(field.width).toBe(form.width - buttons.length * ROW_COST_PER_BUTTON)
    expect(await input.evaluate(el => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(1)

    // 3. Every action button keeps its 44px touch target — the other half of
    //    the fix, since the cheap way to buy typing space is to shrink these.
    for (const button of buttons) {
      const action = await button.boundingBox()
      expect(action.width).toBe(44)
      expect(action.height).toBe(44)
    }
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible()
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
