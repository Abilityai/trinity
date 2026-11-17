// @vitest-environment jsdom
/**
 * Systems → manifest preview for a caller who may not set the platform prompt
 * (abilityai/trinity#4025).
 *
 * A manifest's `prompt:` replaces the platform-wide prompt every agent gets, so
 * deploy writes it for an admin only. For anyone else the dry run returns
 * `prompt_updated: false` plus a `prompt_requires_admin:` warning. Nothing in the
 * UI changed for that: the consent gate reads `prompt_updated` and the warning
 * falls into the generic notes. This proves it on the rendered component and the
 * store: the skipped prompt stays visible as a note, only the prompt clause of
 * the acknowledgement goes, and enabled schedules still require it.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

import ManifestPreview from '@/components/systems/ManifestPreview.vue'
import { useSystemsStore } from '@/stores/systems'

const WARNING = "prompt_requires_admin: this manifest's `prompt:` is not applied for this "
  + 'caller. It replaces the platform-wide prompt every agent receives, so only an admin '
  + 'may set it; the rest of the manifest is unaffected by this.'

const ENABLED_SCHEDULE = {
  agent: 'acme-lead', name: 'daily', cron: '0 9 * * *', message: 'Report', enabled: true,
}

const PREVIEW = ({ promptUpdated, warnings = [], schedules = [] }) => ({
  status: 'valid',
  agents_to_create: [{ name: 'acme-lead', short_name: 'lead', template: 'local:plain' }],
  failed: [],
  warnings,
  permission_edges: {},
  schedules_preview: schedules,
  prompt_updated: promptUpdated,
})

const PROMPT_CLAUSE = 'replaces the platform-wide system prompt'
const GATE_HEADING = 'This changes more than just these agents'

function render(preview) {
  return mount(ManifestPreview, { props: { preview } })
}

function needsAck(preview) {
  const store = useSystemsStore()
  store.preview = preview
  return store.needsAcknowledgement
}

describe('ManifestPreview when the prompt requires an admin', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('shows the skipped prompt as a note and asks for no acknowledgement', () => {
    const preview = PREVIEW({ promptUpdated: false, warnings: [WARNING] })
    const w = render(preview)

    expect(w.text()).toContain(WARNING)
    expect(w.text()).not.toContain(PROMPT_CLAUSE)
    expect(w.text()).not.toContain(GATE_HEADING)
    expect(needsAck(preview)).toBe(false)
  })

  it('still asks for an acknowledgement when a schedule is enabled, without the prompt clause', () => {
    const preview = PREVIEW({
      promptUpdated: false, warnings: [WARNING], schedules: [ENABLED_SCHEDULE],
    })
    const w = render(preview)

    expect(w.text()).toContain(GATE_HEADING)
    expect(w.text()).toContain('1 recurring schedule')
    expect(w.text()).not.toContain(PROMPT_CLAUSE)
    expect(w.text()).toContain(WARNING)
    expect(needsAck(preview)).toBe(true)
  })

  it('keeps the prompt clause for a caller whose deploy will write the prompt', () => {
    const preview = PREVIEW({ promptUpdated: true })
    const w = render(preview)

    expect(w.text()).toContain(GATE_HEADING)
    expect(w.text()).toContain(PROMPT_CLAUSE)
    expect(needsAck(preview)).toBe(true)
  })
})
