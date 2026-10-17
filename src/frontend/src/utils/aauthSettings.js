/**
 * Settings → AAuth: the panel's rules, kept out of the SFC so they are tested
 * directly (abilityai/trinity-enterprise#862).
 */

/**
 * The PUT body: only the fields the admin changed (learnings 2026-09-29).
 * Sending an untouched value would write it as a row, and a row beats the
 * environment from then on.
 *
 * @param {{enabled: boolean, issuer: string}} loaded  what GET returned
 * @param {{enabled: boolean, issuer: string}} draft   what the form holds
 */
export function buildAAuthUpdate(loaded, draft) {
  const body = {}
  if (Boolean(draft.enabled) !== Boolean(loaded.enabled)) body.enabled = Boolean(draft.enabled)
  const issuer = (draft.issuer || '').trim()
  if (issuer !== (loaded.issuer || '').trim()) body.issuer = issuer
  return body
}

const HOST_RE = /^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.(?![0-9]+$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/

/**
 * Pre-validate the issuer client-side (principle 17). The server applies the
 * same rule and is the authority; this only lets the admin see the problem
 * before saving. Returns '' when acceptable (an empty value clears it).
 */
export function issuerProblem(value) {
  const v = (value || '').trim()
  if (!v) return ''
  const example = ' Example: https://trinity.example.com'
  if (!v.startsWith('https://')) return `The issuer must start with https://.${example}`
  const rest = v.slice('https://'.length)
  if (rest.includes('/') || rest.includes('?') || rest.includes('#')) {
    return `No path — the issuer is the host only.${example}`
  }
  if (rest.includes(':')) return `No port — AAuth identities are host names on the default HTTPS port.${example}`
  if (rest !== rest.toLowerCase()) return `Use lowercase — the issuer is compared exactly.${example}`
  if (!HOST_RE.test(rest)) return `Use a DNS host name with a dot, not an IP address.${example}`
  return ''
}

/**
 * The federation-readiness checklist: what else a call to another instance
 * needs. Each item names its next step, so the panel never shows a red mark
 * without saying what to do about it (principle 16).
 */
export function readinessItems(state) {
  const r = state?.readiness || {}
  return [
    {
      key: 'issuer',
      ok: Boolean(r.issuer_valid),
      label: 'Issuer set to this instance\'s public HTTPS host',
      next: 'Enter the host other instances reach this one on, e.g. https://trinity.example.com.',
    },
    {
      key: 'live',
      ok: Boolean(state?.live),
      label: 'AAuth switched on',
      next: 'Turn on the switch above once the issuer is set.',
    },
    {
      key: 'outbound',
      ok: Boolean(r.outbound_enabled),
      label: 'Outbound A2A calls allowed',
      next: 'Turn on outbound calls in the A2A endpoints panel below.',
    },
    {
      key: 'endpoint',
      ok: (r.aauth_endpoints || 0) > 0,
      label: `An endpoint registered with the AAuth scheme (${r.aauth_endpoints || 0} of ${r.endpoints || 0})`,
      next: 'Register the peer agent\'s A2A URL below with Authentication set to AAuth.',
    },
  ]
}
