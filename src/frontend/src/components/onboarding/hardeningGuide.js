/**
 * Decidable logic behind the first-run hardening guide (#2380) — since ent#581
 * the `secure` step of the first-run overlay (`steps/StepSecure.vue`), whose
 * visibility is decided by the registry in `firstRunSteps.js`. The posture copy
 * below is what that step speaks, unchanged.
 *
 * Split out of the SFC because `vitest.config.js` runs `environment: 'node'`
 * with no component-mount harness, so a decision left inside a component is one
 * no test can reach — the ent#392 rule. Every sentence of posture copy lives
 * here, where the spec can assert on it directly.
 */

// The retired card's per-browser dismissal. Nothing writes it any more; the
// first-run overlay reads it once so an operator who dismissed the card is not
// re-asked after the upgrade (`firstRunSteps.js::legacySkips`).
export const HARDENING_GUIDE_DISMISSED_KEY = 'trinity_hardening_guide_dismissed'

// The posture that completes step ONE. It does NOT retire the card: the guide
// advises two steps, and retiring on the first meant the second was mentioned
// once and then never again, on the one surface that raises it. At this posture
// the card ADVANCES to the tunnel step instead, and a dismissal is what ends it.
export const DOMAIN_POSTURE = 'https-domain'

/**
 * Which stage this posture puts the step on: `address` decides how the instance
 * is reached, `tunnel` who can reach it at all.
 */
export function hardeningStage(installTlsPosture) {
  return installTlsPosture === DOMAIN_POSTURE ? 'tunnel' : 'address'
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
 * `https-ip` therefore says what it can see (an address) and what an install of
 * this shape is KNOWN to set up (a short-lived IP-bound certificate, hedged as
 * an expectation), then argues the upgrade from properties of the ADDRESS —
 * that it is awkward to share and answers to the whole public internet — which
 * are readable from the string itself. The renewal caveat is worded the same way
 * and for the same reason: a ~6-day certificate renewed only while the machine
 * runs is a property of that certificate PROFILE, so it is stated as what such a
 * setup does after a long shutdown, never as a claim about this instance's
 * current certificate or its expiry.
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
 * `https-domain` is the one posture that speaks only to step two: step one is
 * already done, so its copy opens on what is left rather than on an exposure.
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
      'Trinity cannot inspect the certificate from here — it only knows the address it was told to advertise. If a marketplace image or the DigitalOcean install script set this up, expect a short-lived certificate tied to the IP, renewed automatically while the server is running. Certificates on that profile last about six days, so a server left switched off for longer than that comes back to a browser warning until renewal catches up. Either way, an IP address is awkward to share and answers to the whole public internet, so a real name is worth adding.',
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

