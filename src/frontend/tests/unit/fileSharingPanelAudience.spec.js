// @vitest-environment jsdom
/**
 * #2955 — the owner's Sharing panel derives who each file is for ONCE per row.
 *
 * `FileSharingPanel.vue` called `describeSharedFileAudience(file)` for the
 * label, its `title`, and the detail line's `v-if`, text and `title` — up to
 * five times per row inside the `v-for`. A `computed` over the listing now
 * maps every row once, and the five bindings read the precomputed
 * `{ label, detail }`. The "For" column has to render IDENTICALLY.
 *
 * Mounted rather than grepped (the #2829 rule): "once per row" and
 * "identically" are properties of what renders, which a regex over the SFC
 * cannot prove. The rule itself (`utils/sharedFileAudience.js`) stays the
 * single source and is spec'd on its own in `sharedFileAudience.spec.js`.
 *
 * Expectations are computed through `vi.importActual` — NEVER through the
 * spied import: under `vi.mock` hoisting this spec's own import IS the spy,
 * so using it for the reference values would inflate the exact call count.
 *
 * Emails are `example.com` and the number is from the 555-01xx fiction range:
 * this is a public repository.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const getFileSharingStatus = vi.fn()
const listSharedFiles = vi.fn()
const setFileSharingStatus = vi.fn()
const revokeSharedFile = vi.fn()

vi.mock('../../src/stores/agents', () => ({
  useAgentsStore: () => ({ getFileSharingStatus, listSharedFiles, setFileSharingStatus, revokeSharedFile }),
}))

// The real composables index re-exports fourteen composables, several of which
// pull `@/api` (axios) at module scope — mock the whole index.
vi.mock('../../src/composables', () => ({
  useNotification: () => ({ showNotification: vi.fn() }),
}))

// The audience rule, spied but real: the count is the assertion, the values
// must be the module's own.
vi.mock('../../src/utils/sharedFileAudience', async (importOriginal) => {
  const mod = await importOriginal()
  return { describeSharedFileAudience: vi.fn(mod.describeSharedFileAudience) }
})

// eslint-disable-next-line import/first
import { describeSharedFileAudience } from '../../src/utils/sharedFileAudience'
// eslint-disable-next-line import/first
import FileSharingPanel from '../../src/components/FileSharingPanel.vue'

const { describeSharedFileAudience: real } = await vi.importActual('../../src/utils/sharedFileAudience')

const EXPIRES = new Date(Date.now() + 3 * 24 * 60 * 60 * 1000).toISOString()

const row = (id, over = {}) => ({
  file_id: id,
  filename: `${id}.pdf`,
  size_bytes: 2048,
  mime_type: 'application/pdf',
  url: `https://example.com/api/files/${id}?sig=sig-${id}`,
  created_at: '2026-09-22T10:00:00Z',
  expires_at: EXPIRES,
  download_count: 0,
  last_downloaded_at: null,
  addressed_to: null,
  addressed_to_channel: null,
  audience_source: null,
  ...over,
})

// The three "For" states the owner can see, plus an addressed row whose
// detail line is EMPTY (so the conditional second line is exercised both ways).
const files = [
  row('addressed', { addressed_to: 'ada@example.com', audience_source: 'turn' }),
  row('channel-only', { addressed_to_channel: 'whatsapp:+15555550142', audience_source: 'channel' }),
  row('owner-only', { audience_source: 'ambiguous' }),
  row('addressed-no-detail', { addressed_to: 'bob@example.com', audience_source: null }),
]

async function mountPanel() {
  getFileSharingStatus.mockResolvedValue({ enabled: true, restart_required: false, volume_attached: true })
  listSharedFiles.mockResolvedValue({
    files,
    total_bytes: files.reduce((n, f) => n + f.size_bytes, 0),
    quota_bytes: 500 * 1024 * 1024,
  })
  const wrapper = mount(FileSharingPanel, { props: { agentName: 'atlas' } })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('FileSharingPanel — the "For" column', () => {
  it('says who each file is for — label, tooltip and detail — once per row', async () => {
    const w = await mountPanel()
    const trs = w.findAll('tbody tr')
    expect(trs).toHaveLength(files.length)

    trs.forEach((tr, i) => {
      const expected = real(files[i])
      const cell = tr.findAll('td')[1]
      const lines = cell.findAll('div')

      expect(cell.text()).toContain(expected.label)
      expect(lines[0].attributes('title')).toBe(expected.label)
      if (expected.detail) {
        expect(lines).toHaveLength(2)
        expect(cell.text()).toContain(expected.detail)
        expect(lines[1].attributes('title')).toBe(expected.detail)
      } else {
        expect(lines).toHaveLength(1)
      }
    })

    // Exact, not `≤`: "once per row" IS the acceptance criterion. If this goes
    // red after the change, that is a real second derivation to find.
    expect(describeSharedFileAudience).toHaveBeenCalledTimes(files.length)
  })

  it('a row action still receives the file it was rendered for', async () => {
    // jsdom has no clipboard; assignment does not take, defineProperty does.
    const writeText = vi.fn().mockResolvedValue()
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })

    const w = await mountPanel()
    const copy = w.findAll('tbody tr')[0].findAll('button')[0]
    expect(copy.text()).toBe('Copy URL')
    await copy.trigger('click')
    await flushPromises()

    expect(writeText).toHaveBeenCalledWith(files[0].url)
  })
})
