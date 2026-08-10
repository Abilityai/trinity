/**
 * S5 — GitConflictModal operator-readable copy (issue #386).
 *
 * Mounts the modal with a fabricated `conflict` object for each class and
 * asserts that:
 *   - the operator-facing title matches the per-class copy
 *   - the recommendation sentence names the suggested action
 *   - the raw stderr is rendered inside a <details> block
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'

import GitConflictModal from '../../src/frontend/src/components/GitConflictModal.vue'

const RAW_P2 = `From /tmp/trinity-repro/p2/bare
 * branch            main       -> FETCH_HEAD
Rebasing (1/2)Auto-merging trader.conf
CONFLICT (add/add): Merge conflict in trader.conf
error: could not apply 0703f4b... Add polymarket trader configuration`

const RAW_P5 = `remote: error: cannot lock ref 'refs/heads/trinity/alpaca-repro-a/bd47596d'
 ! [remote rejected] trinity/alpaca-repro-a/bd47596d -> trinity/alpaca-repro-a/bd47596d (failed to update ref)`

function mountModal({ conflictClass, type = 'sync', rawStderr = 'raw git output goes here' }) {
  return mount(GitConflictModal, {
    props: {
      show: true,
      conflict: {
        type,
        conflictType: 'merge_conflict',
        message: 'legacy message string',
        conflictClass,
        rawStderr,
      },
    },
  })
}

describe('GitConflictModal — per-class operator copy', () => {
  it('renders AHEAD_ONLY with push-your-changes recommendation', () => {
    const wrapper = mountModal({ conflictClass: 'AHEAD_ONLY' })
    expect(wrapper.get('[data-testid="gcm-title"]').text()).toBe('You have local changes to push')
    expect(wrapper.get('[data-testid="gcm-recommendation"]').text()).toMatch(/push/i)
    // Raw stderr present inside <details>
    const details = wrapper.get('[data-testid="gcm-details"]')
    expect(details.element.tagName.toLowerCase()).toBe('details')
    expect(details.get('summary').text()).toMatch(/developers/i)
    expect(wrapper.get('[data-testid="gcm-raw-stderr"]').text()).toContain('raw git output')
  })

  it('renders PARALLEL_HISTORY with divergence explanation and adopt-upstream recommendation', () => {
    const wrapper = mountModal({ conflictClass: 'PARALLEL_HISTORY', rawStderr: RAW_P2 })
    const title = wrapper.get('[data-testid="gcm-title"]').text()
    expect(title.toLowerCase()).toContain('diverged')
    const rec = wrapper.get('[data-testid="gcm-recommendation"]').text().toLowerCase()
    expect(rec).toMatch(/adopt/)
    // Body bullets list the key facts
    const body = wrapper.get('[data-testid="gcm-body"]').text().toLowerCase()
    expect(body).toMatch(/rewritten|replay|force/)
    // Raw stderr from the actual P2 repro is visible inside <details>
    expect(wrapper.get('[data-testid="gcm-raw-stderr"]').text()).toContain('could not apply 0703f4b')
  })

  it('renders WORKING_BRANCH_EXTERNAL_WRITE with another-process explanation', () => {
    const wrapper = mountModal({ conflictClass: 'WORKING_BRANCH_EXTERNAL_WRITE', rawStderr: RAW_P5 })
    expect(wrapper.get('[data-testid="gcm-title"]').text().toLowerCase()).toContain('another process')
    // The body and the details block both describe the race.
    expect(wrapper.get('[data-testid="gcm-body"]').text().toLowerCase()).toMatch(/moved|another/)
    expect(wrapper.get('[data-testid="gcm-raw-stderr"]').text()).toContain('cannot lock ref')
  })

  it('renders UNKNOWN with "cannot sync" title and share-details recommendation', () => {
    const wrapper = mountModal({ conflictClass: 'UNKNOWN' })
    expect(wrapper.get('[data-testid="gcm-title"]').text()).toBe('Your agent cannot sync')
    expect(wrapper.get('[data-testid="gcm-recommendation"]').text().toLowerCase()).toMatch(/git details/)
    expect(wrapper.find('[data-testid="gcm-details"]').exists()).toBe(true)
  })

  it('falls back to pre-S5 copy when conflictClass is missing (backward compat)', () => {
    // Intentionally omit conflictClass and rawStderr; mimic an older agent image.
    const wrapper = mount(GitConflictModal, {
      props: {
        show: true,
        conflict: { type: 'sync', conflictType: 'push_rejected', message: 'Push rejected' },
      },
    })
    expect(wrapper.get('[data-testid="gcm-title"]').text()).toBe('Push Conflict')
    // No details section without rawStderr.
    expect(wrapper.find('[data-testid="gcm-details"]').exists()).toBe(false)
  })
})
