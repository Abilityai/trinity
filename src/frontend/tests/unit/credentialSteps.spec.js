/**
 * ent#582 — the rules and copy behind the first-run credential steps
 * (StepClaude, StepKeys) and the Settings key fields that reuse them.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import {
  CLAUDE_ALREADY_CONNECTED,
  CLAUDE_TABS,
  CLAUDE_TAB_COPY,
  FIRST_RUN_SUBSCRIPTION_NAME,
  KEY_ORDER,
  KEY_PROVIDERS,
  OAT_IN_API_KEY_TAB,
  SETTINGS_PATH,
  claudeCredentialError,
  claudeSavedText,
  claudeTabFor,
  configuredLine,
  describeKeyTest,
  fromAddressError,
  keyFormatError,
  laterHint,
  prefillFromAddress,
} from '../../src/components/onboarding/steps/credentialSteps'

describe('Claude step', () => {
  it('carries the spec rejection copy for an sk-ant-oat token on the API key tab, verbatim', () => {
    expect(OAT_IN_API_KEY_TAB).toBe(
      'That key was rejected. API keys start with sk-ant-api — this one starts with sk-ant-oat, ' +
        'which is a subscription token. Paste it on the Subscription token tab instead.'
    )
    expect(claudeCredentialError('api_key', 'sk-ant-oat01-abc')).toBe(OAT_IN_API_KEY_TAB)
  })

  it('accepts each credential on its own tab and nothing else', () => {
    expect(claudeCredentialError('api_key', ' sk-ant-api03-abc ')).toBe('')
    expect(claudeCredentialError('subscription', 'sk-ant-oat01-abc')).toBe('')
    expect(claudeCredentialError('subscription', '')).toBe('')
    expect(claudeCredentialError('api_key', 'hello')).toMatch(/start with sk-ant-api/)
    expect(claudeCredentialError('subscription', 'hello')).toMatch(/claude setup-token/)
    expect(claudeCredentialError('subscription', 'sk-ant-api03-abc')).toMatch(/API key tab/)
  })

  it('offers to move a pasted value to the tab it belongs on', () => {
    expect(claudeTabFor('api_key', 'sk-ant-oat01-x')).toBe('subscription')
    expect(claudeTabFor('subscription', 'sk-ant-api03-x')).toBe('api_key')
    expect(claudeTabFor('api_key', 'sk-ant-api03-x')).toBeNull()
  })

  it('always offers a path with no terminal (the API key tab)', () => {
    expect(CLAUDE_TABS.map((t) => t.id)).toEqual(['subscription', 'api_key'])
    expect(CLAUDE_TAB_COPY.api_key.help).toMatch(/no terminal/)
    expect(CLAUDE_TAB_COPY.subscription.help).toMatch(/No terminal\? Use the API key tab/)
    for (const t of CLAUDE_TABS) expect(CLAUDE_TAB_COPY[t.id].providerUrl).toMatch(/^https:\/\//)
  })
})

describe('Claude step — what the save did', () => {
  it('names the real number of agents the save connected', () => {
    expect(claudeSavedText(3)).toMatch(/^Connected 3 agents that had no Claude credential/)
    expect(claudeSavedText(1)).toMatch(/^Connected 1 agent that/)
  })

  it('says a save that connected nobody is saved for new agents, without claiming any', () => {
    expect(claudeSavedText(0)).toMatch(/^Saved\. New agents will use it\./)
    expect(claudeSavedText(0)).not.toMatch(/connected/i)
  })

  it.each([[undefined], [null], ['2'], [['seeded']], [-1], [1.5]])(
    'claims neither outcome when the count is missing or not a count: %j',
    (value) => {
      const text = claudeSavedText(value)
      expect(text).toMatch(/^Saved\. Manage it later/)
      expect(text).not.toMatch(/agent/i)
    }
  )

  it('every outcome says where to manage it', () => {
    for (const v of [0, 2, undefined]) expect(claudeSavedText(v)).toContain(SETTINGS_PATH)
  })

  it('says plainly that a second paste replaces the saved credential (the name is an upsert key)', () => {
    expect(FIRST_RUN_SUBSCRIPTION_NAME).toBe('primary')
    expect(CLAUDE_ALREADY_CONNECTED).toContain(`"${FIRST_RUN_SUBSCRIPTION_NAME}" subscription`)
    expect(CLAUDE_ALREADY_CONNECTED).toMatch(/replacing whatever is saved there now/)
    expect(CLAUDE_ALREADY_CONNECTED).not.toMatch(/\badd\b/)
  })

  it('the step shows the count from the save response, not a fixed sentence', () => {
    const step = readFileSync(
      fileURLToPath(new URL('../../src/components/onboarding/steps/StepClaude.vue', import.meta.url)),
      'utf8'
    )
    expect(step).toContain('connectedAgents.value = saved?.connected_agents')
    expect(step).toContain('claudeSavedText(connectedAgents.value)')
    expect(step).toContain('CLAUDE_ALREADY_CONNECTED')
  })
})

describe('optional keys', () => {
  it('each key names a provider link, a docs link, what it enables and what skipping costs', () => {
    expect(KEY_ORDER).toEqual(['github', 'resend', 'gemini'])
    for (const id of KEY_ORDER) {
      const p = KEY_PROVIDERS[id]
      expect(p.providerUrl).toMatch(/^https:\/\//)
      expect(p.docsUrl).toMatch(/^https:\/\/.*platform-keys\.md#/)
      expect(p.enables).toBeTruthy()
      expect(p.skipConsequence).toMatch(/^Without it/)
    }
  })

  it('is honest about the two consequences the issue names', () => {
    expect(KEY_PROVIDERS.resend.skipConsequence).toMatch(/nobody can sign in with an email code/)
    expect(KEY_PROVIDERS.gemini.skipConsequence).toMatch(/voice is off/)
    expect(KEY_PROVIDERS.gemini.skipConsequence).toMatch(/no generated avatars/)
  })

  it('every skip names where to do it later', () => {
    expect(SETTINGS_PATH).toBe('Settings → Integrations')
    expect(laterHint()).toContain(SETTINGS_PATH)
  })

  it.each([
    ['github', 'ghp_abc', ''],
    ['github', 'github_pat_abc', ''],
    ['github', 'nope', /ghp_ or github_pat_/],
    ['resend', 're_abc', ''],
    ['resend', 'sk-abc', /start with re_/],
    ['gemini', 'AIzaAbc', ''],
    ['gemini', 'sk-abc', /start with AIza/],
    ['gemini', '', ''],
  ])('%s %s', (provider, value, expected) => {
    const err = keyFormatError(provider, value)
    if (expected === '') expect(err).toBe('')
    else expect(err).toMatch(expected)
  })

  it('accepts a bare or named sender and gives an example otherwise', () => {
    expect(fromAddressError('noreply@acme.test')).toBe('')
    expect(fromAddressError('Trinity <codes@acme.test>')).toBe('')
    expect(fromAddressError('not an address')).toMatch(/noreply@your-domain\.com/)
  })

  it('never pre-fills a placeholder sender Resend is guaranteed to refuse', () => {
    expect(prefillFromAddress('noreply@trinity.example.com')).toBe('')
    expect(prefillFromAddress('noreply@example.com')).toBe('')
    expect(prefillFromAddress('noreply@acme.test')).toBe('noreply@acme.test')
  })
})

describe('describeKeyTest', () => {
  it('puts an unverified-domain failure under the sender, a rejected key under the key', () => {
    expect(describeKeyTest('resend', { valid: false, error: 'x', verified_domains: [] }).field).toBe('from')
    expect(describeKeyTest('resend', { valid: false, error: 'x' }).field).toBe('key')
    expect(describeKeyTest('gemini', { valid: false, error: 'bad' })).toMatchObject({ ok: false, error: 'bad' })
  })

  it('never reports success for a missing or malformed answer', () => {
    expect(describeKeyTest('gemini', null).ok).toBe(false)
    expect(describeKeyTest('gemini', {}).error).toMatch(/try again/)
  })

  it('flags a GitHub token that cannot reach repositories', () => {
    const r = describeKeyTest('github', { valid: true, has_repo_access: false })
    expect(r.ok).toBe(true)
    expect(r.warning).toMatch(/repo scope/)
  })

  it('passes a provider warning through on success', () => {
    expect(describeKeyTest('resend', { valid: true, warning: 'w' })).toMatchObject({ ok: true, warning: 'w' })
  })
})

describe('configuredLine', () => {
  it('says where a configured key resolves from', () => {
    expect(configuredLine({ configured: true, masked: '...abcd', source: 'settings' })).toBe(
      'Configured (...abcd) — saved in Settings.'
    )
    expect(configuredLine({ configured: true, masked: '...abcd', source: 'env' })).toMatch(/server environment/)
    expect(configuredLine({ configured: false })).toBe('')
  })
})
