// @vitest-environment jsdom
import { describe, it, expect, vi, beforeAll } from 'vitest'
import { mount } from '@vue/test-utils'
import IntroVideo from '@/components/onboarding/IntroVideo.vue'

describe('the intro video sound', () => {
  beforeAll(() => {
    window.matchMedia = vi.fn(() => ({ matches: false }))
  })

  it('starts muted, so it can autoplay, and offers the sound', () => {
    const w = mount(IntroVideo)
    expect(w.find('[data-testid="intro-video-file"]').element.muted).toBe(true)
    expect(w.find('[data-testid="intro-video-sound"]').text()).toBe('Turn sound on')
  })

  it('turns the sound on and stops offering it', async () => {
    const w = mount(IntroVideo)
    await w.find('[data-testid="intro-video-sound"]').trigger('click')
    expect(w.find('[data-testid="intro-video-file"]').element.muted).toBe(false)
    expect(w.find('[data-testid="intro-video-sound"]').exists()).toBe(false)
  })

  it('follows the video\'s own controls', async () => {
    const w = mount(IntroVideo)
    const video = w.find('[data-testid="intro-video-file"]')
    video.element.muted = false
    await video.trigger('volumechange')
    expect(w.find('[data-testid="intro-video-sound"]').exists()).toBe(false)
    video.element.muted = true
    await video.trigger('volumechange')
    expect(w.find('[data-testid="intro-video-sound"]').exists()).toBe(true)
  })

  it('has no sound button on the drawn version', async () => {
    const w = mount(IntroVideo)
    await w.find('[data-testid="intro-video-file"]').trigger('error')
    expect(w.find('[data-testid="intro-video"]').exists()).toBe(true)
    expect(w.find('[data-testid="intro-video-sound"]').exists()).toBe(false)
  })
})
