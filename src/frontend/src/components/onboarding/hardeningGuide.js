/**
 * Decidable logic behind `HardeningGuide.vue` (#2380).
 *
 * Split out of the SFC because `vitest.config.js` runs `environment: 'node'`
 * with no component-mount harness, so a decision left inside a component is one
 * no test can reach — the ent#392 rule. The component is a dispatcher over
 * this module; every visibility term and every sentence of posture copy lives
 * here, where the spec can assert on it directly.
 */

// Per-browser dismissal, matching `trinity_front_desk_dismissed` (ent#319) and
// `trinity_onboarding_dismissed`. Deliberately NOT a server round-trip: this is
// an ambient, non-gating nudge that retires itself the moment a domain is
// configured, so a per-user table would be more machinery than the behaviour is
// worth — and it must not add an endpoint to a first-boot security surface.
export const HARDENING_GUIDE_DISMISSED_KEY = 'trinity_hardening_guide_dismissed'

// The posture that means the job is done. Kept as a named constant because two
// places read it: the visibility predicate, and the copy table's deliberate
// omission of an entry for it.
export const SETTLED_POSTURE = 'https-domain'

/**
 * Should the guide render?
 *
 * `featureFlagsLoaded` is a required term, not a convenience: without it the
 * card flashes in on every page load before the answer arrives, because
 * `marketplaceInstall` starts false and a false→true flip after the fetch is
 * indistinguishable from a real one (the `stores/firstRun.js` `loaded`
 * rationale). On an established fleet the honest render during the fetch is
 * nothing at all.
 *
 * `marketplaceInstall` is resolved SERVER-side — the browser never decides
 * which install sources count as a marketplace. Every non-marketplace install,
 * including the entire managed fleet whose plain-HTTP-over-Tailscale shape is
 * indistinguishable from an unhardened droplet by any other signal, is false
 * here.
 *
 * `isAdmin` is a REAL GATE, not cosmetics, for two independent reasons. (1) The
 * only remediation this card offers is admin-only: `general` is `adminOnly` in
 * `Settings.vue`'s tab list, so a non-admin who follows the button lands on
 * `resolveTabFromQuery`'s fallback tab — a card whose single action dead-ends
 * for the person reading it. (2) The copy discloses this instance's network
 * posture (“advertises a plain-HTTP address”) to every user of the box, and the
 * flag document it derives from is served to any authenticated principal. It
 * defaults FALSE so the safe direction — hidden — is what an omitted term buys.
 *
 * `https-domain` hides the card permanently with no client state at all: a
 * configured domain IS the completion condition, so the guide is self-retiring
 * and cannot linger on an instance that already did the work.
 */
export function isHardeningGuideVisible({
  featureFlagsLoaded = false,
  isAdmin = false,
  marketplaceInstall = false,
  installTlsPosture = 'unconfigured',
  dismissed = false,
} = {}) {
  if (!featureFlagsLoaded) return false
  if (!isAdmin) return false
  if (!marketplaceInstall) return false
  if (installTlsPosture === SETTLED_POSTURE) return false
  if (dismissed) return false
  return true
}

/**
 * What the card is allowed to say, per posture.
 *
 * The binding constraint (an explicit AC) is honesty about what is actually
 * known. `install_tls_posture` is derived by PURE STRING PARSING of the URL this
 * instance is configured to ADVERTISE — nothing probes a socket, opens a
 * connection, or reads a certificate, because TLS terminates outside the
 * backend. So no sentence here may assert a property of the actual connection:
 * not that it is secure, not that a certificate is browser-trusted or issued by
 * anyone in particular, and not that anything "works today" or is "not broken".
 * All three are claims about a wire this code has never touched.
 *
 * `https-ip` therefore says what it can see (an address) and what a marketplace
 * image is KNOWN to install (a short-lived IP-bound certificate, hedged as an
 * expectation), then argues the upgrade from properties of the ADDRESS — that
 * it is awkward to share and answers to the whole public internet — which are
 * readable from the string itself.
 *
 * `http` is the one posture that names an exposure, and it still hedges: a
 * proxy in front of Trinity may already terminate TLS, in which case the
 * advertised address is stale rather than dangerous, and from here the two are
 * indistinguishable.
 *
 * Badge variants are chosen so none of them screams "broken": `neutral` for an
 * absence of information, `warning` for the posture whose advertised address
 * carries no encryption, `info` for one with room to improve.
 *
 * There is deliberately no `https-domain` entry — that posture never renders.
 */
export const POSTURE_COPY = {
  unconfigured: {
    badge: 'No public URL',
    badgeVariant: 'neutral',
    headline: 'No public URL is configured for this instance yet.',
    detail:
      'Trinity has no address to hand out, so it cannot tell how people are reaching it today. Both steps below settle that — one decides the address, the other decides who can use it.',
  },
  http: {
    badge: 'Advertises HTTP',
    badgeVariant: 'warning',
    headline: 'This instance advertises a plain-HTTP address.',
    detail:
      'Anyone reaching Trinity at that address sends traffic unencrypted, sign-in codes included. If something in front of Trinity already terminates TLS, this is a stale address rather than an exposure — Trinity cannot tell which from here. Either way, both steps below apply.',
  },
  'https-ip': {
    badge: 'HTTPS at an IP',
    badgeVariant: 'info',
    headline: 'This instance advertises HTTPS at a bare IP address.',
    detail:
      'Trinity cannot inspect the certificate from here — it only knows the address it was told to advertise. If a marketplace image set this up, expect a short-lived certificate tied to the IP and renewed every few days. Either way, an IP address is awkward to share and answers to the whole public internet, so a real name is worth adding.',
  },
}

/** Copy for a posture, or `null` when the guide should not be speaking at all. */
export function postureCopy(posture) {
  return POSTURE_COPY[posture] || null
}

/** Read the persisted dismissal. Any storage failure reads as "not dismissed". */
export function readHardeningGuideDismissed() {
  try {
    return localStorage.getItem(HARDENING_GUIDE_DISMISSED_KEY) === '1'
  } catch {
    // Private mode / disabled storage. Showing the card again is the safe
    // direction for a security nudge; it is one click to wave away.
    return false
  }
}

/**
 * Persist the dismissal. Returns whether it stuck — the caller hides the card
 * for this session either way, so a refusal is a warning, never a failed verb.
 */
export function persistHardeningGuideDismissed() {
  try {
    localStorage.setItem(HARDENING_GUIDE_DISMISSED_KEY, '1')
    return true
  } catch (e) {
    console.warn('[hardeningGuide] could not persist dismissal:', e?.message || e)
    return false
  }
}
