/**
 * ent#454 — the teardown store contract.
 *
 * `environment: 'node'` (no component mounting), so this covers the store, which
 * is where the decisions that can silently go wrong actually live:
 *
 *   1. The URL and the VERB. The route is entitlement-gated and lives under a
 *      different prefix from every other systems call, so a copy-paste from
 *      `dryRun()` would POST to /api/systems/deploy and deploy a manifest where
 *      the user asked to remove one.
 *   2. `agents` in the DELETE body. Axios carries a DELETE body only under
 *      `{data}` — passing it as the second argument sends NOTHING, and the
 *      server would then remove every current member instead of the confirmed
 *      subset. Silent, and the worst possible direction to fail in.
 *   3. The preview is bound to the NAME it was produced for. A removal set for
 *      `acme` says nothing about `acme-2`.
 *   4. A `failed` report arriving as HTTP 500 is kept as a RESULT. Its
 *      per-member reasons are the only actionable output, and a naive catch
 *      discards exactly them (the ent#126 rule, re-earned here).
 *   5. A timeout is `outcomeUnknown`, never an error — the server keeps
 *      deleting, so a retry would delete more.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.hoisted(() => {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
  globalThis.window = globalThis.window || { location: { pathname: '/library' } }
})

vi.mock('axios', () => {
  const inst = {
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})

const axios = (await import('axios')).default
const { useSystemsStore, TEARDOWN_FEATURE_ID } = await import('@/stores/systems')

/** An axios-shaped rejection, so `normalizeError` sees what it really sees. */
function httpError(status, data) {
  const err = new Error(`Request failed with status code ${status}`)
  err.response = { status, data }
  return err
}

const PREVIEW = {
  status: 'preview',
  system_name: 'acme',
  dry_run: true,
  membership_verified: true,
  members: [
    { name: 'acme-web', evidence: 'both', is_ephemeral: false, outcome: 'candidate' },
    { name: 'acme-db', evidence: 'prefix', is_ephemeral: false, outcome: 'candidate' },
  ],
  excluded: [],
  system_views: [],
  warnings: [],
  recovery: 'per-agent, 180 days',
}

describe('teardown — the request it sends', () => {
  let store
  beforeEach(() => {
    setActivePinia(createPinia())
    store = useSystemsStore()
    vi.clearAllMocks()
  })

  it('previews with the gated URL, dry_run=true, and NO body', async () => {
    axios.delete.mockResolvedValue({ data: PREVIEW })
    store.setTeardownName('acme')

    await store.previewTeardown()

    expect(axios.delete).toHaveBeenCalledTimes(1)
    const [url, config] = axios.delete.mock.calls[0]
    expect(url).toBe('/api/enterprise/system-teardown/acme?dry_run=true')
    expect(config).toBeUndefined()
    expect(store.teardownPreview).toEqual(PREVIEW)
  })

  it('sends the confirmed set under `data`, which is the only place axios reads it', async () => {
    axios.delete.mockResolvedValue({ data: { status: 'torn_down', system_name: 'acme', members: [] } })
    store.setTeardownName('acme')

    await store.teardown(['acme-web'])

    const [url, config] = axios.delete.mock.calls[0]
    expect(url).toBe('/api/enterprise/system-teardown/acme?dry_run=false')
    expect(config.data).toEqual({ agents: ['acme-web'], strict: false })
    // Omitting `data` would send no body at all, and the server treats a missing
    // `agents` as "remove every current member" — the opposite of a confirmed
    // subset, and unnoticeable from the UI.
    expect(config.data.agents).toHaveLength(1)
  })

  it('distinguishes "remove all" from "remove nothing"', async () => {
    axios.delete.mockResolvedValue({ data: { status: 'torn_down', system_name: 'acme', members: [] } })
    store.setTeardownName('acme')

    await store.teardown(null)
    expect(axios.delete.mock.calls[0][1].data.agents).toBeNull()

    await store.teardown([])
    expect(axios.delete.mock.calls[1][1].data.agents).toEqual([])
  })

  it('url-encodes the system name', async () => {
    axios.delete.mockResolvedValue({ data: PREVIEW })
    store.setTeardownName('a/b')
    await store.previewTeardown()
    expect(axios.delete.mock.calls[0][0]).toContain('/system-teardown/a%2Fb?')
  })

  it('gives teardown its own long timeout — it is serial and removes containers', async () => {
    axios.delete.mockResolvedValue({ data: { status: 'torn_down', system_name: 'acme', members: [] } })
    store.setTeardownName('acme')
    await store.teardown(['acme-web'])
    expect(axios.delete.mock.calls[0][1].timeout).toBeGreaterThanOrEqual(300000)
  })

  it('refuses to call the server without a name', async () => {
    await store.previewTeardown()
    expect(axios.delete).not.toHaveBeenCalled()
    expect(store.teardownError).toMatch(/name of the system/i)
  })

  it('trims the typed name rather than sending whitespace', async () => {
    axios.delete.mockResolvedValue({ data: PREVIEW })
    store.setTeardownName('  acme  ')
    await store.previewTeardown()
    expect(axios.delete.mock.calls[0][0]).toContain('/system-teardown/acme?')
  })
})

describe('teardown — the preview is bound to its name', () => {
  let store
  beforeEach(() => {
    setActivePinia(createPinia())
    store = useSystemsStore()
    vi.clearAllMocks()
    axios.delete.mockResolvedValue({ data: PREVIEW })
  })

  it('is current for the name it was produced for', async () => {
    store.setTeardownName('acme')
    await store.previewTeardown()
    expect(store.teardownPreviewIsCurrent).toBe(true)
  })

  it('is NOT current once the name changes', async () => {
    store.setTeardownName('acme')
    await store.previewTeardown()
    store.setTeardownName('acme-2')
    expect(store.teardownPreviewIsCurrent).toBe(false)
    // The payload is dropped, but the marker survives, so the UI can say
    // "the name changed" rather than "preview first" — a different instruction.
    expect(store.teardownPreview).toBeNull()
    expect(store.teardownPreviewedName).toBe('acme')
  })

  it('has nothing previewed at all after a reset', async () => {
    store.setTeardownName('acme')
    await store.previewTeardown()
    store.resetTeardown()
    expect(store.teardownPreviewedName).toBeNull()
    expect(store.teardownPreview).toBeNull()
    expect(store.teardownName).toBe('')
  })

  it('flags membership the server could not verify', async () => {
    axios.delete.mockResolvedValue({
      data: { ...PREVIEW, membership_verified: false },
    })
    store.setTeardownName('acme')
    await store.previewTeardown()
    expect(store.teardownMembershipUnverified).toBe(true)
  })

  it('does not flag an unverified read when there is no preview', () => {
    expect(store.teardownMembershipUnverified).toBe(false)
  })

  it('goes stale if the name is changed WITHOUT going through the setter', async () => {
    // Found by mutation: replacing the name comparison with `true` passed every
    // other test in this file, because `setTeardownName` nulls the preview and
    // the `!== null` clause then carries the assertion on its own. So the
    // comparison was untested — and it is not redundant: `teardownName` is an
    // exposed ref, so any caller can assign it directly and skip the
    // invalidation (the same bypass `previewedText` guards on the deploy side).
    // Without this test, the second half of the guard could be deleted and
    // nothing would notice until a user read `acme`'s removal set while
    // `acme-2` was in the box.
    store.setTeardownName('acme')
    await store.previewTeardown()
    expect(store.teardownPreviewIsCurrent).toBe(true)

    store.teardownName = 'acme-2'

    expect(store.teardownPreview).not.toBeNull()
    expect(store.teardownPreviewIsCurrent).toBe(false)
  })
})

describe('teardown — an unconfirmable member is opt-IN, never pre-ticked', () => {
  // The panel's watcher applies `teardownDefaultSelection` verbatim, so this IS
  // the arriving selection. The decision lives in the store precisely so it can
  // be executed here: `environment: 'node'` mounts no component, so the same
  // rule written into the `.vue` watcher was a fact no runnable test could see,
  // and it shipped wrong.
  let store
  beforeEach(() => {
    setActivePinia(createPinia())
    store = useSystemsStore()
    vi.clearAllMocks()
  })

  async function previewOf(members) {
    axios.delete.mockResolvedValue({ data: { ...PREVIEW, members } })
    store.setTeardownName('acme')
    await store.previewTeardown()
  }

  it('ticks the confirmed member and leaves the prefix match alone', async () => {
    await previewOf(PREVIEW.members)
    expect(store.teardownDefaultSelection).toEqual(['acme-web'])
    expect(store.teardownUnconfirmedMembers).toEqual(['acme-db'])
  })

  it('does it in the HEALTHY state too — the 503 refusal does not cover this', async () => {
    // The whole point. `membership_verified: false` disables Remove anyway, so
    // a default that were only safe there would be safe nowhere that matters:
    // tags read fine, one agent has no tag row, Remove is ENABLED.
    await previewOf(PREVIEW.members)
    expect(store.teardownMembershipUnverified).toBe(false)
    expect(store.teardownDefaultSelection).not.toContain('acme-db')
  })

  it('ticks `tag`-only evidence — a deploy tag is confirmation, prefix is not', async () => {
    await previewOf([
      { name: 'acme-web', evidence: 'both' },
      { name: 'acme-db', evidence: 'tag' },
      { name: 'acme-extra-worker', evidence: 'prefix' },
    ])
    expect(store.teardownDefaultSelection).toEqual(['acme-web', 'acme-db'])
  })

  it('selects NOTHING when the server confirmed nothing', async () => {
    // An untagged legacy system. Remove stays disabled on the empty-selection
    // gate until the operator affirms a member by hand, which is the intended
    // outcome for a set the server cannot vouch for at all.
    await previewOf([
      { name: 'acme-web', evidence: 'prefix' },
      { name: 'acme-db', evidence: 'prefix' },
    ])
    expect(store.teardownDefaultSelection).toEqual([])
    expect(store.teardownUnconfirmedMembers).toHaveLength(2)
  })

  it('is empty before anything is previewed, rather than throwing', () => {
    expect(store.teardownDefaultSelection).toEqual([])
    expect(store.teardownUnconfirmedMembers).toEqual([])
  })

  // The rule is an allowlist of positive confirmation, not `!== 'prefix'`.
  // `evidence` is free-form text off the wire and this feeds a verb that
  // DELETES, so anything the client does not recognise has to fall to the
  // unticked side. Under the denylist these three arrived PRE-TICKED and,
  // because the panel's badge tested `=== 'prefix'` rather than the
  // complement, they were also unbadged — swept into a delete with no signal.
  it.each([
    ['no evidence field at all', {}],
    ['a null evidence', { evidence: null }],
    ['a future fourth value', { evidence: 'roster' }],
  ])('leaves a member with %s unticked, and says it is unconfirmed', async (_label, extra) => {
    await previewOf([
      { name: 'acme-web', evidence: 'tag', is_ephemeral: false },
      { name: 'acme-odd', is_ephemeral: false, ...extra },
    ])
    expect(store.teardownDefaultSelection).toEqual(['acme-web'])
    expect(store.teardownUnconfirmedMembers).toEqual(['acme-odd'])
  })

  it('partitions the roster: every member is pre-ticked or named unconfirmed, never both or neither', async () => {
    const members = [
      { name: 'a', evidence: 'tag' },
      { name: 'b', evidence: 'both' },
      { name: 'c', evidence: 'prefix' },
      { name: 'd', evidence: 'roster' },
      { name: 'e' },
    ]
    await previewOf(members)
    const ticked = store.teardownDefaultSelection
    const short = store.teardownUnconfirmedMembers
    expect([...ticked, ...short].sort()).toEqual(members.map((m) => m.name).sort())
    expect(ticked.filter((n) => short.includes(n))).toEqual([])
  })
})

describe('teardown — failures stay actionable', () => {
  let store
  beforeEach(() => {
    setActivePinia(createPinia())
    store = useSystemsStore()
    vi.clearAllMocks()
    store.setTeardownName('acme')
  })

  it('keeps a `failed` report that arrives as HTTP 500', async () => {
    const report = {
      status: 'failed',
      system_name: 'acme',
      members: [{ name: 'acme-web', outcome: 'failed', reason: 'docker daemon gone' }],
    }
    axios.delete.mockRejectedValue(httpError(500, report))

    const out = await store.teardown(['acme-web'])

    expect(out).toEqual(report)
    expect(store.teardownResult).toEqual(report)
    expect(store.teardownError).toBeNull()
    expect(store.teardownOutcomeUnknown).toBeNull()
  })

  it('surfaces the 503 refusal as an error, not as a result', async () => {
    axios.delete.mockRejectedValue(
      httpError(503, { detail: 'System membership could not be verified' })
    )
    await store.teardown(['acme-web'])
    expect(store.teardownResult).toBeNull()
    expect(store.teardownError).toMatch(/could not be verified/)
  })

  it('surfaces a 404 on preview without claiming an unknown outcome', async () => {
    axios.delete.mockRejectedValue(
      httpError(404, { detail: "System 'acme' not found or no accessible agents" })
    )
    await store.previewTeardown()
    expect(store.teardownError).toMatch(/not found/)
    expect(store.teardownOutcomeUnknown).toBeNull()
  })

  it('treats a timeout on EXECUTE as an unknown outcome, never an error', async () => {
    const err = new Error('timeout of 300000ms exceeded')
    err.code = 'ECONNABORTED'
    axios.delete.mockRejectedValue(err)

    await store.teardown(['acme-web'])

    expect(store.teardownOutcomeUnknown).toBeTruthy()
    expect(store.teardownError).toBeNull()
    expect(store.teardownResult).toBeNull()
  })

  it('says what was in flight — a teardown timeout never talks about deploying', async () => {
    // The normalizer is shared with deploy, and its default copy is
    // "Deployment may still be running... re-deploying creates duplicate
    // agents" — the opposite advice, at the worst moment of a removal.
    const err = new Error('timeout of 300000ms exceeded')
    err.code = 'ECONNABORTED'
    axios.delete.mockRejectedValue(err)

    await store.teardown(['acme-web'])

    expect(store.teardownOutcomeUnknown).toMatch(/removal|removed/i)
    expect(store.teardownOutcomeUnknown).not.toMatch(/deploy/i)
  })

  it('a 5xx with no body says members may be gone, not that agents may exist', async () => {
    axios.delete.mockRejectedValue(httpError(502, null))

    await store.teardown(['acme-web'])

    expect(store.teardownOutcomeUnknown).toMatch(/removed/i)
    expect(store.teardownOutcomeUnknown).not.toMatch(/created/i)
  })

  it('a PREVIEW failure never implies work in flight — a dry run writes nothing', async () => {
    const err = new Error('timeout of 30000ms exceeded')
    err.code = 'ECONNABORTED'
    axios.delete.mockRejectedValue(err)

    await store.previewTeardown()

    expect(store.teardownError).toMatch(/Nothing was removed/i)
  })

  it('treats a timeout on PREVIEW as a plain error — a dry run removes nothing', async () => {
    const err = new Error('timeout of 30000ms exceeded')
    err.code = 'ECONNABORTED'
    axios.delete.mockRejectedValue(err)

    await store.previewTeardown()

    expect(store.teardownOutcomeUnknown).toBeNull()
    expect(store.teardownError).toBeTruthy()
  })

  it('clears the in-flight flags on both paths', async () => {
    axios.delete.mockRejectedValue(httpError(500, { detail: 'boom' }))
    await store.previewTeardown()
    await store.teardown(['acme-web'])
    expect(store.isPreviewingTeardown).toBe(false)
    expect(store.isTearingDown).toBe(false)
  })
})

describe('teardown — it never disturbs the install flow', () => {
  let store
  beforeEach(() => {
    setActivePinia(createPinia())
    store = useSystemsStore()
    vi.clearAllMocks()
  })

  it('keeps a deploy result on screen while a teardown is previewed', async () => {
    // Install and remove are two flows a user can have half-finished at once on
    // one page. One shared result slot would let either silently replace the
    // other's output.
    axios.post.mockResolvedValue({ data: { status: 'deployed', system_name: 'acme', agents_created: ['acme-web'] } })
    store.setManifestText('name: acme')
    await store.deploy()
    expect(store.deployResult.status).toBe('deployed')

    axios.delete.mockResolvedValue({ data: PREVIEW })
    store.setTeardownName('acme')
    await store.previewTeardown()

    expect(store.deployResult.status).toBe('deployed')
    expect(store.teardownPreview.status).toBe('preview')
  })

  it('exports the entitlement id the UI gates on, so the panel and the store agree', () => {
    expect(TEARDOWN_FEATURE_ID).toBe('system_teardown')
  })
})
