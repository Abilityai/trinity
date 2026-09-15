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
 * The two lines that must be readable WITHOUT opening the disclosure (#2691):
 * what saving this buys, and what has to be true first. Walked live on a fresh
 * droplet, nobody in the room could say either — the field's help text said
 * only "the address people will use to reach this instance", and the
 * prerequisite was four sentences deep inside <details>.
 *
 * Shared with Settings → General so the meaning does not depend on which
 * surface you arrive through, and so the two cannot drift apart.
 */
export const DOMAIN_BENEFIT =
  'Trinity hands out this name instead of the IP, and the web server in front obtains a certificate for it.'
export const DOMAIN_PREREQUISITE =
  'Point the domain at this server first — save it here before DNS resolves and the name has nothing to answer it.'

/**
 * Saving this value re-points live integrations, immediately (#2691).
 *
 * Every Telegram webhook is re-registered and every WhatsApp binding rewritten
 * to the new base the moment a non-empty value is stored. On a name that is not
 * live yet, working bots move to an address that answers nothing, and nothing
 * in the product says so. Stated on the admin surface that owns the setting
 * (Settings → General) rather than in the first-run flow, where an operator has
 * no channels configured yet and the sentence would be noise.
 */
/**
 * The step-by-step walkthrough this step links out to (#2692) —
 * `docs/user-docs/guides/deploying/hardening.md` in the public repo, published
 * by the docs site under `getting-started/deploying/`.
 *
 * The published site, not a `github.com/.../blob/main/...` URL: the one other
 * docs link in the first-run flow (`steps/credentialSteps.js`) points at a blob
 * path and 404s today, because `main` is behind `dev` between release cuts.
 * This 404s until the release that carries the page too, which is the same one.
 */
export const HARDENING_DOCS_URL = 'https://docs.ability.ai/getting-started/deploying/hardening'

export const DOMAIN_SIDE_EFFECT =
  'Saving re-points Telegram and WhatsApp webhooks to this address straight away, so set it once the domain is live.'

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
  // Saved, but nothing has arrived at the name yet. This is the honest state
  // for every domain the moment it is entered, and it is where a typo or an
  // absent DNS record stays forever (#2691).
  'https-domain': {
    badge: 'Domain saved',
    badgeVariant: 'info',
    headline:
      'Your domain is saved. The first visit to it is what proves the name works \u2014 until then Trinity cannot tell.',
    detail:
      'Trinity now hands out the name rather than the IP. It issues no certificates itself: the web server in front asks Trinity whether a name is allowed, and obtains the certificate for it on the first request that actually arrives for that name. So nothing here confirms the domain until someone visits it \u2014 if DNS does not point at this server, the visit fails in the browser and Trinity never learns of it. The step below is about reach rather than address: unless something in front of this server already restricts it, it still answers anyone who finds the address.',
  },
  // The one state in this file that rests on something OBSERVED rather than
  // parsed: a request for exactly this name arrived and a certificate followed.
  // It survives a proxy, a load balancer or a reserved IP in between, which is
  // why it is the tick rather than a DNS lookup taken at save time.
  'https-domain-reached': {
    badge: 'Domain reached',
    badgeVariant: 'success',
    headline:
      'Your domain is serving traffic. One step is left \u2014 a Cloudflare Tunnel can take this server off the public internet.',
    detail:
      'Step one is done, and confirmed: a request for your domain reached this server and a certificate was obtained for it. The step below is about reach rather than address \u2014 this server still answers anyone who finds the address. It is optional, but it is the difference between an instance anyone can knock on and one that is only reachable through Cloudflare. Skip it if you are evaluating; come back to it from Settings \u2192 General before this instance matters.',
  },
}

/**
 * Copy for a posture, or `null` when the guide should not be speaking at all.
 *
 * `reached` splits the domain posture in two, and that split is the point of
 * #2691: the posture itself is parsed from a string an admin typed, so it can
 * only ever say "saved". Claiming the domain WORKS takes evidence, and the only
 * evidence available is that a request for that exact name arrived here.
 */
export function postureCopy(posture, reached = false) {
  if (posture === DOMAIN_POSTURE && reached) return POSTURE_COPY['https-domain-reached']
  return POSTURE_COPY[posture] || null
}

