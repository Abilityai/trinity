// @vitest-environment jsdom
/**
 * #4588 — Settings > Direct calls: the "Speech provider" select rendered with
 * no options, because BaseSelect takes its <option>s through the default slot
 * and has no `options` prop (the `:options` binding fell through as an
 * attribute). Mount the real component and assert the options exist.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import DirectVoiceSettings from '../../src/components/settings/DirectVoiceSettings.vue'
import { useSettingsStore } from '../../src/stores/settings'

describe('DirectVoiceSettings speech provider select', () => {
  let store
  beforeEach(() => {
    setActivePinia(createPinia())
    store = useSettingsStore()
    store.getDirectVoiceSettings = vi.fn().mockResolvedValue({
      enabled: true, provider: 'gemini', ready_provider: 'gemini', reason: null,
    })
    store.setDirectVoiceSettings = vi.fn().mockImplementation(async (p) => ({
      enabled: true, provider: 'auto', ready_provider: null, reason: null, ...p,
    }))
  })

  it('offers Automatic, Gemini and ElevenLabs and reflects the stored provider', async () => {
    const w = mount(DirectVoiceSettings)
    await flushPromises()
    const select = w.find('[data-testid="direct-voice-provider"]')
    const options = select.findAll('option')
    expect(options.map((o) => o.attributes('value'))).toEqual(['auto', 'gemini', 'elevenlabs'])
    expect(options.map((o) => o.text())).toEqual(['Automatic', 'Gemini', 'ElevenLabs'])
    expect(select.element.value).toBe('gemini')
  })

  it('choosing a provider saves it', async () => {
    const w = mount(DirectVoiceSettings)
    await flushPromises()
    await w.find('[data-testid="direct-voice-provider"]').setValue('elevenlabs')
    expect(store.setDirectVoiceSettings).toHaveBeenCalledWith({ provider: 'elevenlabs' })
  })
})
