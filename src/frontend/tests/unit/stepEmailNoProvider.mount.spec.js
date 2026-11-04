// @vitest-environment jsdom
/**
 * On an install that can't send email, the sign-in step says codes by email won't work.
 * People need to act on that, so it is drawn as the design system's warning alert
 * (tinted ground, an icon, a bold lead sentence, the fix inline), not as grey body text.
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import StepEmail from '@/components/onboarding/steps/StepEmail.vue'

const mountStep = (ctx) => mount(StepEmail, { props: { ctx }, global: { plugins: [createPinia()] } })
const NO_PROVIDER = '[data-testid="first-run-email-no-provider"]'

describe('the sign-in email step, on an install that cannot send email', () => {
  it('shows the warning as a tinted alert with an icon, a bold lead and the fix', () => {
    const note = mountStep({ hasEmail: true, userEmail: 'user@example.com', emailDeliveryAvailable: false }).get(NO_PROVIDER)
    expect(note.classes()).toContain('bg-status-warning-100')
    expect(note.find('svg').exists()).toBe(true)
    expect(note.get('strong').text()).toBe("Signing in with a code sent by email won't work yet.")
    expect(note.text()).toContain('To turn it on, add an email provider under Optional extras, later in this setup.')
  })

  it('shows no warning when the install can send email', () => {
    const w = mountStep({ hasEmail: true, userEmail: 'user@example.com', emailDeliveryAvailable: true })
    expect(w.find(NO_PROVIDER).exists()).toBe(false)
  })
})
