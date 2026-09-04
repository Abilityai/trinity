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
export const HARDENING_GUIDE_TUNNEL_DISMISSED_KEY = 'trinity_hardening_guide_tunnel_dismissed'

// The posture that completes step ONE. It does NOT retire the card: the guide
// advises two steps, and retiring on the first meant the second was mentioned
// once and then never again, on the one surface that raises it. At this posture
// the card ADVANCES to the tunnel step instead, and a dismissal is what ends it.
export const DOMAIN_POSTURE = 'https-domain'

// Two stages, in the order the card advises them. `address` decides how the
// instance is reached; `tunnel` decides who can reach it at all.
export const HARDENING_STAGES = ['address', 'tunnel']

/** Which step this posture puts the card on. */
export function hardeningStage(installTlsPosture) {
  return installTlsPosture === DOMAIN_POSTURE ? 'tunnel' : 'address'
}

/**
 * Dismissal is PER STAGE, and that is load-bearing rather than tidy.
 *
 * One key would let a dismissal of "you are on a bare IP" silently consume the
 * tunnel advice the operator has not been shown yet — the same shape as the
 * ent#437 warm ask being spent behind another card. The address stage keeps the
 * original key, so an existing dismissal keeps holding and nothing migrates.
 */
export function dismissKeyForStage(stage) {
  return stage === 'tunnel' ? HARDENING_GUIDE_TUNNEL_DISMISSED_KEY : HARDENING_GUIDE_DISMISSED_KEY
}

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
 * `https-domain` no longer hides the card. It advances it: step one is done, so
 * the card switches to the tunnel step and the operator gets one look at the
 * advice before dismissing it. `dismissed` is therefore the only thing that
 * ends the guide, and it is resolved per stage by the caller.
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
  if (dismissed) return false
  // `installTlsPosture` no longer gates visibility — it selects the STAGE (and
  // therefore which copy speaks). An unknown posture has no copy, and the
  // component's own belt hides the card rather than rendering an empty shell.
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
  'https-domain': {
    badge: 'Domain set',
    badgeVariant: 'success',
    headline:
      'Your domain is set. One optional step is left \u2014 a Cloudflare Tunnel can take this server off the public internet.',
    detail:
      'Step one is done: Trinity now hands out a name rather than an IP, and whatever terminates TLS in front of it can pick up that name. The step below is about reach rather than address \u2014 unless something in front of this server already restricts it, it still answers anyone who finds the address. This one is optional, and dismissing it here is a fine answer.',
  },
}

/** Copy for a posture, or `null` when the guide should not be speaking at all. */
export function postureCopy(posture) {
  return POSTURE_COPY[posture] || null
}

/** Read the persisted dismissal for one stage. Any storage failure reads as "not dismissed". */
export function readHardeningGuideDismissed(stage = 'address') {
  try {
    return localStorage.getItem(dismissKeyForStage(stage)) === '1'
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
export function persistHardeningGuideDismissed(stage = 'address') {
  try {
    localStorage.setItem(dismissKeyForStage(stage), '1')
    return true
  } catch (e) {
    console.warn('[hardeningGuide] could not persist dismissal:', e?.message || e)
    return false
  }
}
