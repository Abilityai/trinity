// trinity-enterprise#1312 — the values an emailed setup link carries.
//
// The link is `/setup#claim=<code>&email=<email>`: a fragment never reaches a
// server, so no access log, proxy or Referer sees it. The query form
// `/setup?claim=…&email=…` is accepted too. Only single string values count; a
// repeated parameter (an array) counts as absent.

function fromHash(hash) {
  const raw = typeof hash === 'string' ? hash.replace(/^#/, '') : ''
  if (!raw.includes('=')) return {}
  const params = new URLSearchParams(raw)
  const one = (k) => (params.getAll(k).length === 1 ? params.get(k) : undefined)
  return { claim: one('claim'), email: one('email'), named: params.has('claim') || params.has('email') }
}

const str = (v) => (typeof v === 'string' ? v.trim() : '')

// { claim, email, present }: `present` is true when the URL named either key at
// all, even with a value that is unusable, so the caller still clears it.
export function readSetupLink(route) {
  const query = route?.query || {}
  const hash = fromHash(route?.hash)
  const present = 'claim' in query || 'email' in query || !!hash.named
  return {
    claim: str(hash.claim) || str(query.claim),
    email: str(hash.email) || str(query.email),
    present,
  }
}

// The page's shape check only; the server compares the code (16–64 characters).
export const LINK_CLAIM_RE = /^[A-Za-z0-9_-]{16,64}$/

// A non-secret marker that this tab opened a setup link, so a reload after the
// address bar was cleared can say "open the link again". Never the code.
export const SETUP_LINK_FLAG = 'setup-link-opened'
