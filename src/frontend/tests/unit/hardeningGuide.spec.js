/**
 * The first-run hardening guide's contract (#2380).
 *
 * Three rules carry this surface, and none of them is visible to a structural
 * check:
 *
 *   1. **Never over an instance that isn't a marketplace droplet.** The gate is
 *      resolved server-side, and every failure path resolves to hidden. A
 *      missed nudge is a non-event; a "secure this instance" card sitting over
 *      a managed instance that is already behind Tailscale is an accusation
 *      nobody can act on.
 *   2. **Never before the answer arrives.** `marketplaceInstall` starts false,
 *      so a false→true flip after the fetch is indistinguishable from a real
 *      one — without the `featureFlagsLoaded` term the card flashes in on every
 *      page load.
 *   3. **Never a claim that wasn't measured.** The posture is derived by string
 *      -parsing the URL the instance ADVERTISES; nothing probes a socket or
 *      reads a certificate. The copy is asserted here so a later edit cannot
 *      quietly promote it to a verdict about the actual connection.
 *   4. **Never to someone who cannot act on it.** Settings → General is
 *      `adminOnly`, so for a non-admin the card's one button falls through to
 *      the default tab — and the copy would be disclosing the box's network
 *      posture to every user of it.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { setActivePinia, createPinia } from 'pinia'

vi.hoisted(() => {
  const mem = new Map()
  globalThis.localStorage = {
    getItem: (k) => (mem.has(k) ? mem.get(k) : null),
    setItem: (k, v) => mem.set(k, String(v)),
    removeItem: (k) => mem.delete(k),
    clear: () => mem.clear(),
  }
  globalThis.window = globalThis.window || { location: { pathname: '/' } }
})

// `stores/sessions.js` calls axios directly (not the shared `@/api` client), so
// axios is the seam. `stores/auth` is mocked because `loadFeatureFlags` reads
// `authHeader` off it before the request.
vi.mock('axios', () => ({ default: { get: vi.fn() } }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ authHeader: {} }) }))

import axios from 'axios'
import { useSessionsStore } from '@/stores/sessions'
import { resetInFlight } from '@/utils/inflight'
import {
  HARDENING_GUIDE_DISMISSED_KEY,
  DOMAIN_POSTURE,
  HARDENING_GUIDE_TUNNEL_DISMISSED_KEY,
  POSTURE_COPY,
  dismissKeyForStage,
  hardeningStage,
  isHardeningGuideVisible,
  persistHardeningGuideDismissed,
  postureCopy,
  readHardeningGuideDismissed,
} from '@/components/onboarding/hardeningGuide'

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')
const GUIDE_SFC = read('../../src/components/onboarding/HardeningGuide.vue')
const SETTINGS_SFC = read('../../src/views/Settings.vue')

/**
 * The rendered copy, with the HTML comments removed.
 *
 * The comments here EXPLAIN decisions the copy must not state — the VPN the
 * card no longer offers, the Tailscale install it must not appear over — so
 * asserting "the card never says VPN" against the raw file would fail on the
 * paragraph that records why. The assertion is about what a user reads.
 *
 * Stripped to a FIXPOINT rather than in one pass: a single
 * `replace(/<!--[\s\S]*?-->/g, '')` leaves a live `<!--` behind on nested
 * input (`<!--<!-- -->` -> `<!--`), which CodeQL flags as
 * js/incomplete-multi-character-sanitization. Nothing untrusted reaches this —
 * it reads a checked-in file — but the loop is both the rule's prescribed fix
 * and the more correct strip, so there is no reason to carry the weaker one.
 */
const withoutComments = (source) => {
  let out = source
  let previous
  do {
    previous = out
    out = out.replace(/<!--[\s\S]*?-->/g, '')
  } while (out !== previous)
  return out
}

/** A marketplace droplet still advertising HTTPS at its bare IP, seen by an admin. */
const marketplaceIp = {
  featureFlagsLoaded: true,
  isAdmin: true,
  marketplaceInstall: true,
  installTlsPosture: 'https-ip',
  dismissed: false,
}

let store

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  resetInFlight() // `once()` caches the flag document across the module's life.
  vi.clearAllMocks()
  store = useSessionsStore()
})

describe('visibility', () => {
  it('shows on a marketplace install advertising HTTPS at a bare IP', () => {
    expect(isHardeningGuideVisible(marketplaceIp)).toBe(true)
  })

  it('shows on a marketplace install with no public URL, and on plain HTTP', () => {
    expect(isHardeningGuideVisible({ ...marketplaceIp, installTlsPosture: 'unconfigured' })).toBe(true)
    expect(isHardeningGuideVisible({ ...marketplaceIp, installTlsPosture: 'http' })).toBe(true)
  })

  it('stays hidden when the install is not a marketplace one', () => {
    // The whole managed fleet lands here: plain HTTP behind Tailscale is
    // indistinguishable from an unhardened droplet by every other signal, so
    // provenance is the only gate that can tell them apart.
    expect(isHardeningGuideVisible({ ...marketplaceIp, marketplaceInstall: false })).toBe(false)
    expect(
      isHardeningGuideVisible({ ...marketplaceIp, marketplaceInstall: false, installTlsPosture: 'http' })
    ).toBe(false)
  })

  it('renders nothing before the flags load', () => {
    expect(isHardeningGuideVisible({ ...marketplaceIp, featureFlagsLoaded: false })).toBe(false)
  })

  it('stays hidden for a non-admin on a marketplace install', () => {
    // Not cosmetics. `general` is `adminOnly`, so `resolveTabFromQuery` drops a
    // non-admin on the default tab — the card's only action dead-ends for the
    // person reading it, under a headline about this box being unencrypted.
    expect(isHardeningGuideVisible({ ...marketplaceIp, isAdmin: false })).toBe(false)
    expect(
      isHardeningGuideVisible({ ...marketplaceIp, isAdmin: false, installTlsPosture: 'http' })
    ).toBe(false)
  })

  it('stays hidden while the profile is still unverified', () => {
    // `authStore.role` answers 'user' until /api/users/me lands, so the SFC
    // ANDs `profileVerified` in before asking the predicate — otherwise the card
    // flashes for a non-admin on every page load (#2198). The getter is `role`:
    // this spec used to pin `userRole`, which the store never defined, so the
    // card it guards had been permanently hidden (ent#437 eyeball finding;
    // `authRoleGetterContract.spec.js` now pins the name repo-wide).
    const unverifiedAdmin = false // profileVerified && role === 'admin'
    expect(isHardeningGuideVisible({ ...marketplaceIp, isAdmin: unverifiedAdmin })).toBe(false)
    expect(GUIDE_SFC).toContain('authStore.profileVerified')
    expect(GUIDE_SFC).toMatch(/authStore\.role === 'admin'/)
    expect(GUIDE_SFC).not.toMatch(/authStore\.userRole/)
    // The gate lives in the pure module, not in Dashboard.vue, so it is testable.
    expect(GUIDE_SFC).toMatch(/isAdmin:\s*authStore\.profileVerified/)
  })

  it('shows for a verified admin on a marketplace install', () => {
    expect(isHardeningGuideVisible({ ...marketplaceIp, isAdmin: true })).toBe(true)
  })

  it('predicate: a configured domain ADVANCES the card, it does not retire it', () => {
    // The guide advises two steps. Retiring on step one meant step two was
    // mentioned once and then never again, on the only surface that raises it —
    // so `https-domain` now selects the tunnel stage instead of hiding.
    expect(DOMAIN_POSTURE).toBe('https-domain')
    expect(isHardeningGuideVisible({ ...marketplaceIp, installTlsPosture: DOMAIN_POSTURE })).toBe(true)
    expect(hardeningStage(DOMAIN_POSTURE)).toBe('tunnel')
    // Everything short of a domain is still step one.
    for (const posture of ['unconfigured', 'http', 'https-ip']) {
      expect(hardeningStage(posture)).toBe('address')
    }
    // A dismissal is now the only thing that ends the guide.
    expect(
      isHardeningGuideVisible({ ...marketplaceIp, installTlsPosture: DOMAIN_POSTURE, dismissed: true })
    ).toBe(false)
  })

  it('scopes dismissal per stage, so step one cannot silently spend step two', () => {
    // One key would let "you are on a bare IP, go away" also consume tunnel
    // advice the operator has never been shown — the same shape as the ent#437
    // warm ask being spent behind another card.
    expect(dismissKeyForStage('address')).toBe(HARDENING_GUIDE_DISMISSED_KEY)
    expect(dismissKeyForStage('tunnel')).toBe(HARDENING_GUIDE_TUNNEL_DISMISSED_KEY)
    expect(dismissKeyForStage('address')).not.toBe(dismissKeyForStage('tunnel'))

    // Dismissing the address stage leaves the tunnel stage unread.
    persistHardeningGuideDismissed('address')
    expect(readHardeningGuideDismissed('address')).toBe(true)
    expect(readHardeningGuideDismissed('tunnel')).toBe(false)

    // The address key keeps its original name, so an existing dismissal holds
    // with nothing to migrate.
    expect(HARDENING_GUIDE_DISMISSED_KEY).toBe('trinity_hardening_guide_dismissed')
  })

  it('the component resolves dismissal against the stage on screen', () => {
    expect(GUIDE_SFC).toMatch(/hardeningStage\(store\.installTlsPosture\)/)
    expect(GUIDE_SFC).toContain('dismissed: dismissed.value[stage.value]')
    expect(GUIDE_SFC).toContain("persistHardeningGuideDismissed(stage.value)")
    // Step one's action cannot render once the domain exists — there is nothing
    // left to collect in-app, and a button that leads nowhere is the defect.
    expect(GUIDE_SFC).toMatch(/v-if="stage === 'address'"[\s\S]{0,400}Add a domain/)
  })

  it('advances in-session on the save that configures the domain', () => {
    // `loadFeatureFlags` early-returns once `featureFlagsLoaded` is true, so
    // without the forced re-read an admin who follows this card's own
    // instruction never sees the card ADVANCE to step two until a hard reload.
    const save = SETTINGS_SFC.slice(SETTINGS_SFC.indexOf('async function savePublicUrl()'))
    const body = save.slice(0, save.indexOf('\n}\n'))
    expect(body).toContain('sessionsStore.loadFeatureFlags(true)')
    // Before the catch, i.e. on the success path only.
    expect(body.indexOf('loadFeatureFlags(true)')).toBeLessThan(body.indexOf('} catch'))
  })

  it('stays hidden once dismissed', () => {
    expect(isHardeningGuideVisible({ ...marketplaceIp, dismissed: true })).toBe(false)
  })

  it('defaults every term to hidden when called with nothing', () => {
    expect(isHardeningGuideVisible()).toBe(false)
    expect(isHardeningGuideVisible({})).toBe(false)
  })
})

describe('dismissal', () => {
  it('writes the documented key and is honoured on the next read', () => {
    expect(readHardeningGuideDismissed()).toBe(false)

    expect(persistHardeningGuideDismissed()).toBe(true)

    expect(localStorage.getItem(HARDENING_GUIDE_DISMISSED_KEY)).toBe('1')
    expect(HARDENING_GUIDE_DISMISSED_KEY).toBe('trinity_hardening_guide_dismissed')
    expect(readHardeningGuideDismissed()).toBe(true)
    expect(isHardeningGuideVisible({ ...marketplaceIp, dismissed: readHardeningGuideDismissed() })).toBe(false)
  })

  it('reports the refusal without throwing when storage rejects the write', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const setItem = localStorage.setItem
    localStorage.setItem = () => { throw new Error('quota') }

    expect(persistHardeningGuideDismissed()).toBe(false)

    localStorage.setItem = setItem
    expect(warn).toHaveBeenCalled()
    warn.mockRestore()
  })

  it('reads as not-dismissed when storage cannot be read at all', () => {
    const getItem = localStorage.getItem
    localStorage.getItem = () => { throw new Error('private mode') }

    // Safe direction for a security nudge: show it again, it is one click away.
    expect(readHardeningGuideDismissed()).toBe(false)

    localStorage.getItem = getItem
  })

  it('does not reach the network — dismissal is per-browser only', () => {
    persistHardeningGuideDismissed()
    expect(axios.get).not.toHaveBeenCalled()
  })
})

describe('honest copy', () => {
  const ALL = Object.entries(POSTURE_COPY)

  it('speaks for every posture that renders, and only those', () => {
    expect(Object.keys(POSTURE_COPY).sort()).toEqual([
      'http',
      'https-domain',
      'https-ip',
      'unconfigured',
    ])
    // An unrecognised posture still has nothing to say, and the component's own
    // belt hides the card rather than rendering an empty shell.
    expect(postureCopy('something-new')).toBeNull()
  })

  it('the tunnel stage names the remaining step on the card FACE', () => {
    // At this stage there is no button — the last move happens on the host — so
    // a reader who dismisses without expanding must still have met the point.
    const { headline, detail, badgeVariant } = POSTURE_COPY['https-domain']
    expect(headline).toMatch(/Cloudflare Tunnel/)
    expect(headline.toLowerCase()).toMatch(/optional/)
    expect(badgeVariant).toBe('success')
    // Honest about reach: it hedges rather than asserting what is in front of
    // this server, exactly as the `http` copy does.
    expect(detail.toLowerCase()).toMatch(/unless something in front of/)
    expect(detail.toLowerCase()).toMatch(/dismissing it here is a fine answer/)
  })

  // `install_tls_posture` is derived by PURE STRING PARSING of the URL this
  // instance is configured to advertise. Nothing opens a socket, completes a
  // handshake, or reads a certificate — TLS terminates outside the backend.
  //
  // So the rule is not "avoid the word secure". It is that ANY sentence
  // asserting a property of the ACTUAL CONNECTION is out of bounds regardless of
  // how it is worded: who issued the certificate, whether a browser trusts it,
  // whether it currently works, whether anything is or is not broken. Each is a
  // claim about a wire this code has never touched, and a reassuring one is
  // worse than an alarming one — it tells an operator to stop looking.
  //
  // The narrow `/\bsecure\b/` grep this replaced sailed straight past
  // "browser-trusted Let's Encrypt certificate that works today", which is four
  // such claims in one clause.
  const FORBIDDEN = [
    [/\bsecure\b|\bsecured\b/, 'asserts the connection is secure'],
    [/verified|validated/, 'asserts something was verified'],
    [/browser-trusted|browser trusted|trusted by (the )?browser/, 'asserts a browser trusts the certificate'],
    [/let['’]s encrypt|zerossl|digicert/, 'names an issuer nothing here has read'],
    [/works today|is working|currently works/, 'asserts the connection currently works'],
    [/nothing here is broken|nothing is broken|not broken/, 'asserts nothing is broken'],
    [/is secure|is encrypted|is valid|certificate is/, 'renders a verdict on the live connection'],
  ]

  it('never asserts a property of a connection it has not inspected', () => {
    for (const [posture, copy] of ALL) {
      const text = `${copy.badge} ${copy.headline} ${copy.detail}`.toLowerCase()
      for (const [pattern, why] of FORBIDDEN) {
        expect(text, `${posture} copy ${why}`).not.toMatch(pattern)
      }
    }
  })

  it('would catch the exact claims this guide used to ship', () => {
    // A guard nobody has seen fail is a guard nobody knows works.
    const regression =
      'That is a real, browser-trusted Let’s Encrypt certificate and it works today — nothing here is broken.'
    const hits = FORBIDDEN.filter(([pattern]) => pattern.test(regression.toLowerCase()))
    expect(hits.length).toBeGreaterThanOrEqual(4)
  })

  it('says "advertises" rather than asserting what the instance actually is', () => {
    expect(POSTURE_COPY.http.headline.toLowerCase()).toContain('advertises')
    expect(POSTURE_COPY['https-ip'].headline.toLowerCase()).toContain('advertises')
    // `unconfigured` has nothing being advertised — it must say so plainly
    // rather than borrow the word.
    expect(POSTURE_COPY.unconfigured.headline.toLowerCase()).toContain('no public url')
  })

  it('says outright that it cannot see the certificate, and hedges what it expects', () => {
    const { badgeVariant, detail } = POSTURE_COPY['https-ip']
    const text = detail.toLowerCase()

    // The honest core: state the limit of the observation before anything else.
    expect(text).toContain('cannot inspect the certificate')
    expect(text).toContain('the address it was told to advertise')
    // The short-lived renewal profile is an EXPECTATION about a marketplace
    // image, not a reading — so it has to arrive hedged.
    expect(text).toMatch(/if a marketplace image|expect a/)
    expect(text).toMatch(/short-lived/)
    // The upgrade case is argued from the ADDRESS, which is the one thing a
    // string-derived posture genuinely knows.
    expect(text).toContain('awkward to share')
    expect(text).toContain('public internet')
    // A red badge would imply a verdict this copy explicitly declines to give.
    expect(['info', 'neutral', 'success']).toContain(badgeVariant)
  })

  it('hedges the http posture instead of asserting an exposure it cannot confirm', () => {
    const text = POSTURE_COPY.http.detail.toLowerCase()
    // A proxy in front of Trinity may already terminate TLS; from here the
    // stale-address case and the real-exposure case are indistinguishable.
    expect(text).toContain('unencrypted')
    expect(text).toMatch(/already terminates tls/)
    expect(text).toMatch(/stale address rather than an exposure/)
    expect(text).toContain('trinity cannot tell which from here')
    // Hedged or not, the remediation is the same, and it must still say so.
    expect(text).toContain('both steps below apply')
  })

  it('never dresses any posture as a failure', () => {
    // `success` earns its place on `https-domain` only: step one is genuinely
    // done there, and the badge reports a completed step rather than a verdict
    // on the connection. Nothing here may read as broken.
    for (const [posture, copy] of ALL) {
      expect(['neutral', 'info', 'warning', 'success'], `${posture} badge`).toContain(
        copy.badgeVariant
      )
      expect(copy.badgeVariant, `${posture} must not read as broken`).not.toBe('danger')
      expect(copy.badgeVariant, `${posture} must not read as broken`).not.toBe('urgent')
    }
    // And `success` is reserved for the one posture that completed a step.
    const successes = ALL.filter(([, c]) => c.badgeVariant === 'success').map(([k]) => k)
    expect(successes).toEqual(['https-domain'])
  })
})

describe('the two paths are complementary, not alternatives', () => {
  it('offers both and says they stack', () => {
    // An explicit AC: the card must never read as a choice between hardening
    // the address and hardening the reach.
    expect(GUIDE_SFC).toContain('These two stack')
    // Both live in the source; each is `v-if`'d to the stage it belongs to, so
    // step one's block and the stacking sentence retire once the domain exists.
    expect(GUIDE_SFC).toMatch(/v-if="stage === 'address'"[\s\S]{0,120}Give it a real name/)
    expect(GUIDE_SFC).toMatch(/Give it a real name/)
    expect(GUIDE_SFC).toMatch(/Serve it without exposing it/)
    expect(GUIDE_SFC).toMatch(/A record/)
    // Deep-links at the field that actually writes `public_chat_url`.
    expect(GUIDE_SFC).toContain("/settings?tab=general")
  })

  it('offers a Cloudflare Tunnel, not a VPN (#2380, decided 2026-09-01)', () => {
    // The second path was VPN/Tailscale until the issue recorded the swap: a
    // VPN reaches the same posture but breaks every inbound integration, since
    // Telegram, WhatsApp, VoIP, public links, x402, inbound A2A and webhook
    // triggers all call us. PR #2431 shipped the pre-decision copy; this is the
    // guard that stops it coming back.
    const prose = withoutComments(GUIDE_SFC)
    expect(prose).toMatch(/Cloudflare/)
    expect(prose).toMatch(/cloudflared/)
    expect(prose).toMatch(/TUNNEL_TOKEN/)
    expect(prose).not.toMatch(/Tailscale/)
    expect(prose.toLowerCase()).not.toMatch(/\bvpn\b/)
  })

  it('states the tunnel prerequisite and the step Trinity cannot take', () => {
    const prose = withoutComments(GUIDE_SFC).replace(/\s+/g, ' ')
    // Not an alternative to the domain — it NEEDS the domain (explicit AC).
    expect(prose).toMatch(/With that domain on Cloudflare/)
    expect(prose).toMatch(/The tunnel needs the name/)
    // Honest about the half it cannot finish: the token reaches `.env` and the
    // tunnel starts under a compose profile, from the host.
    expect(prose).toMatch(/happens on the host rather than from this page/)
  })

  it('keeps one action on the card face and the reasoning behind a disclosure', () => {
    // The card is a first-login nudge on a droplet that is answering the public
    // internet; it must be actionable at a glance, not two columns of prose.
    expect(GUIDE_SFC).toContain('<details')
    expect(GUIDE_SFC).toContain('data-testid="hardening-guide-why"')
    // Exactly one non-dismiss button, and it is the one collectable-in-app step.
    expect(GUIDE_SFC).toMatch(/variant="primary"[\s\S]{0,200}Add a domain/)
    const buttons = GUIDE_SFC.match(/<BaseButton/g) || []
    expect(buttons.length, 'one action + one dismiss').toBe(2)
  })

  it('does not promise a certificate change Trinity does not perform', () => {
    // `public_chat_url` is a display/webhook-base setting: nothing in the tree
    // reads it and reconfigures a proxy, a listener, or a certificate. The card
    // may promise the NAME changes, and must attribute the certificate to
    // whatever actually terminates TLS.
    // Template copy wraps across lines, so match on collapsed whitespace.
    const prose = withoutComments(GUIDE_SFC).replace(/\s+/g, ' ')
    expect(prose).not.toMatch(/90-day certificate then replaces/)
    expect(prose).not.toMatch(/certificate then replaces|replaces the short-lived/)
    expect(prose).toContain('Trinity does not issue certificates itself')
    expect(prose).toMatch(/whatever terminates TLS in front of it/)
    expect(prose).toMatch(/hands out the name instead of the IP/)
  })

  it('does not phrase them as an either/or', () => {
    const prose = withoutComments(GUIDE_SFC) // comments explain, copy asserts
    expect(prose.toLowerCase()).not.toMatch(/\beither\b|\bor instead\b|\balternatively\b/)
  })

  it('carries the documented test hooks and an aria-label on dismiss', () => {
    expect(GUIDE_SFC).toContain('data-testid="hardening-guide"')
    expect(GUIDE_SFC).toContain('data-testid="hardening-guide-dismiss"')
    expect(GUIDE_SFC).toMatch(/aria-label="Dismiss the instance hardening guide"/)
  })

  it('composes the primitives instead of hand-rolling a card, badge, or button', () => {
    // The two neighbouring onboarding cards hand-roll their shell and are
    // pre-ratchet; this one must not copy them (design-system-contract §Primitives).
    for (const p of ['BaseCard', 'BaseBadge', 'BaseButton']) {
      expect(GUIDE_SFC, `${p} must be composed`).toContain(`import ${p} from`)
    }
  })
})

describe('the store fails closed', () => {
  const flagPayload = {
    install_source: 'do-marketplace',
    marketplace_install: true,
    install_tls_posture: 'https-ip',
  }

  it('starts closed before anything is fetched', () => {
    expect(store.featureFlagsLoaded).toBe(false)
    expect(store.installSource).toBe('unknown')
    expect(store.marketplaceInstall).toBe(false)
    expect(store.installTlsPosture).toBe('unconfigured')
  })

  it('carries the three fields through on a successful read', async () => {
    axios.get.mockResolvedValueOnce({ data: flagPayload })
    await store.loadFeatureFlags()

    expect(store.installSource).toBe('do-marketplace')
    expect(store.marketplaceInstall).toBe(true)
    expect(store.installTlsPosture).toBe('https-ip')
    expect(
      isHardeningGuideVisible({
        featureFlagsLoaded: store.featureFlagsLoaded,
        isAdmin: true, // held constant: these cases exercise the flag path
        marketplaceInstall: store.marketplaceInstall,
        installTlsPosture: store.installTlsPosture,
        dismissed: false,
      })
    ).toBe(true)
  })

  it('falls back to the closed values when the payload omits them', async () => {
    axios.get.mockResolvedValueOnce({ data: {} })
    await store.loadFeatureFlags()

    // Not `undefined` — that reads as falsy but prints as "undefined".
    expect(store.installSource).toBe('unknown')
    expect(store.marketplaceInstall).toBe(false)
    expect(store.installTlsPosture).toBe('unconfigured')
  })

  it('fails CLOSED when the flag read fails, so the card stays hidden', async () => {
    axios.get.mockRejectedValueOnce(new Error('boom'))
    await store.loadFeatureFlags()

    expect(store.installSource).toBe('unknown')
    expect(store.marketplaceInstall).toBe(false)
    expect(store.installTlsPosture).toBe('unconfigured')
    expect(store.featureFlagsLoaded).toBe(true) // resolved, just not to a gate

    expect(
      isHardeningGuideVisible({
        featureFlagsLoaded: store.featureFlagsLoaded,
        isAdmin: true, // held constant: these cases exercise the flag path
        marketplaceInstall: store.marketplaceInstall,
        installTlsPosture: store.installTlsPosture,
        dismissed: false,
      })
    ).toBe(false)
  })
})
