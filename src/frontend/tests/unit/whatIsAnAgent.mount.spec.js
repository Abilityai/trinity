// @vitest-environment jsdom
/**
 * "What's an agent?" plays a short video with captions and the intro's music. The transcript is always
 * there for screen readers, and it shows on the page when the video can't play.
 */
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import WhatIsAnAgent from '@/components/onboarding/WhatIsAnAgent.vue'

describe('the explainer video', () => {
  it('plays the video muted and inline, with its poster, and no two columns', () => {
    const w = mount(WhatIsAnAgent)
    const video = w.get('video')
    expect(video.attributes('src')).toBe('/intro/what-is-an-agent.mp4')
    expect(video.attributes('poster')).toBe('/intro/what-is-an-agent-poster.jpg')
    expect(video.element.muted).toBe(true)
    expect(video.attributes('playsinline')).toBeDefined()
    expect(video.attributes('controls')).toBeDefined()
    expect(w.text()).not.toContain('Asking Claude')
  })

  it('keeps the transcript for screen readers, and shows it when the video fails', async () => {
    const w = mount(WhatIsAnAgent)
    const transcript = w.get('[data-testid="what-is-an-agent-transcript"]')
    expect(transcript.text()).toContain("It finds Nick's address in your CRM.")
    expect(transcript.classes()).toContain('sr-only')
    await w.get('video').trigger('error')
    expect(w.get('[data-testid="what-is-an-agent-transcript"]').classes()).not.toContain('sr-only')
  })

  it('has the intro\'s music, starting muted so it can autoplay, with a button to turn it on', async () => {
    const w = mount(WhatIsAnAgent)
    const video = w.get('[data-testid="what-is-an-agent-video"]')
    expect(w.get('[data-testid="what-is-an-agent-sound"]').text()).toBe('Turn sound on')
    await w.get('[data-testid="what-is-an-agent-sound"]').trigger('click')
    expect(video.element.muted).toBe(false)
    expect(w.find('[data-testid="what-is-an-agent-sound"]').exists()).toBe(false)
  })

  it('follows the video\'s own controls for the sound', async () => {
    const w = mount(WhatIsAnAgent)
    const video = w.get('[data-testid="what-is-an-agent-video"]')
    video.element.muted = false
    await video.trigger('volumechange')
    expect(w.find('[data-testid="what-is-an-agent-sound"]').exists()).toBe(false)
    video.element.muted = true
    await video.trigger('volumechange')
    expect(w.find('[data-testid="what-is-an-agent-sound"]').exists()).toBe(true)
  })

  it('has no sound button once the video has failed', async () => {
    const w = mount(WhatIsAnAgent)
    await w.get('video').trigger('error')
    expect(w.find('[data-testid="what-is-an-agent-sound"]').exists()).toBe(false)
  })

  it('still ends with the summary and Got it', async () => {
    const w = mount(WhatIsAnAgent)
    expect(w.get('[data-testid="what-is-an-agent-summary"]').text()).toContain('An agent is Claude')
    await w.get('[data-testid="what-is-an-agent-done"]').trigger('click')
    expect(w.emitted('done')).toHaveLength(1)
  })
})
