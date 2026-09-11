/**
 * The first-run hardening guide's contract (#2380) — since ent#581 the
 * `secure` step of the first-run overlay (`steps/StepSecure.vue`). Its
 * visibility now comes from the registry (`firstRunSteps.spec.js` pins the
 * provenance, admin and flags-loaded terms there); this file keeps the copy,
 * the stage logic, the store's fail-closed read and the step's structure.
 *
 * Three rules carry this surface, and none of them is visible to a structural
 * check:
 *
 *   1. **Never over an instance that isn't a marketplace droplet.** The gate is
 *      resolved server-side, and every failure path resolves to hidden. A
 *      missed nudge is a non-event; a "secure this instance" card sitting over
 *      a managed instance that is already behind Tailscale is an accusation
 *      nobody can act on.
 *   2. **Never before the answer arrives.** `hardeningGuideEligible` starts false,
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
  POSTURE_COPY,
  hardeningStage,
  postureCopy,
} from '@/components/onboarding/hardeningGuide'

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')
const GUIDE_SFC = read('../../src/components/onboarding/steps/StepSecure.vue')
const OVERLAY_SFC = read('../../src/components/onboarding/FirstRunOverlay.vue')
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

let store

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  resetInFlight() // `once()` caches the flag document across the module's life.
  vi.clearAllMocks()
  store = useSessionsStore()
})

describe('stage', () => {
  it('builds the admin term once, in the overlay, from the getter that exists', () => {
    // `authStore.role` answers 'user' until /api/users/me lands, so the term
    // ANDs `profileVerified` in — otherwise the step flashes for a non-admin on
    // every page load (#2198). The getter is `role`: an older spec pinned
    // `userRole`, which the store never defined, so the card it guarded had
    // been permanently hidden (ent#437; `authRoleGetterContract.spec.js`).
    expect(OVERLAY_SFC).toMatch(/isAdmin:\s*auth\.profileVerified && auth\.role === 'admin'/)
    expect(OVERLAY_SFC).not.toMatch(/userRole/)
    expect(GUIDE_SFC).not.toMatch(/userRole/)
  })

  it('a configured domain ADVANCES to the tunnel stage; everything short of one is step one', () => {
    expect(DOMAIN_POSTURE).toBe('https-domain')
    expect(hardeningStage(DOMAIN_POSTURE)).toBe('tunnel')
    for (const posture of ['unconfigured', 'http', 'https-ip']) {
      expect(hardeningStage(posture)).toBe('address')
    }
  })

  it('keeps the retired card\'s dismissal key name, so the upgrade skip still finds it', () => {
    expect(HARDENING_GUIDE_DISMISSED_KEY).toBe('trinity_hardening_guide_dismissed')
  })

  it('the step derives its stage from the posture on screen', () => {
    // ent#581: dismissal is the overlay's per-step Skip now; the step itself
    // only picks which copy speaks.
    expect(GUIDE_SFC).toMatch(/hardeningStage\(props\.ctx\.tlsPosture\)/)
    // Step one's field cannot render once the domain exists — there is nothing
    // left to collect in-app, and a control that leads nowhere is the defect.
    expect(GUIDE_SFC).toMatch(/<form v-if="stage === 'address'"[\s\S]{0,900}Save domain/)
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
    // Writes the field that actually drives the posture, in place — leaving
    // the Dashboard would leave the setup sequence (ent#581).
    expect(GUIDE_SFC).toContain("updateSetting('public_chat_url'")
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

  it('keeps one action on the step face and the reasoning behind a disclosure', () => {
    // The step answers a droplet that is on the public internet right now; it
    // must be actionable at a glance, not two columns of prose.
    expect(GUIDE_SFC).toContain('<details')
    expect(GUIDE_SFC).toContain('data-testid="first-run-secure-why"')
    // Exactly one button, and it is the one collectable-in-app step. Secondary:
    // the overlay footer's Continue is the view's one primary (contract p.11).
    expect(GUIDE_SFC).toMatch(/variant="secondary"[\s\S]{0,200}Save domain/)
    const buttons = GUIDE_SFC.match(/<BaseButton/g) || []
    expect(buttons.length, 'one action; Skip belongs to the chassis').toBe(1)
    // No host command on this card. Saving the field is the whole step; a shell
    // instruction reappearing here means that stopped being true.
    expect(GUIDE_SFC).not.toMatch(/sudo /)
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
    // What CHANGED (#2380 follow-up): the sentence used to end "...picks up the
    // name", implying the proxy reconfigures itself from a Trinity setting. It
    // does not, on any install this card is shown to — so the copy now names
    // the command that reconfigures it. All three claims above stay true and
    // stay asserted; only the actor moved from "whatever is out there,
    // somehow" to a command the operator runs.
    expect(prose).not.toMatch(/picks up the name/)
  })

  it('does not phrase them as an either/or', () => {
    const prose = withoutComments(GUIDE_SFC) // comments explain, copy asserts
    expect(prose.toLowerCase()).not.toMatch(/\beither\b|\bor instead\b|\balternatively\b/)
  })

  it('carries the documented test hooks', () => {
    expect(GUIDE_SFC).toContain('data-testid="first-run-step-secure"')
    expect(GUIDE_SFC).toContain('data-testid="first-run-public-url"')
  })

  it('composes the primitives instead of hand-rolling a field, badge, or button', () => {
    for (const p of ['BaseInput', 'BaseBadge', 'BaseButton', 'InlineError']) {
      expect(GUIDE_SFC, `${p} must be composed`).toContain(`import ${p} from`)
    }
  })
})

describe('step one is finishable from the browser (#2380, on-demand TLS)', () => {
  it('writes the Public URL setting and nothing else', () => {
    // Saving the Public URL completes step one only because Caddy obtains the
    // certificate on demand, asking the backend whether the name is allowed.
    // Before that, saving reconfigured nothing: the domain served a certificate
    // error, this card advanced to step two on the strength of the operator's
    // own input, and the address section that would have explained the fix was
    // `v-if`'d away with it.
    expect(GUIDE_SFC).toContain("updateSetting('public_chat_url'")
    expect(GUIDE_SFC).not.toMatch(/sudo /)
    expect(GUIDE_SFC).not.toMatch(/set-domain\.sh/)
  })

  it('says the certificate is obtained for the saved name, without claiming to do it', () => {
    const prose = withoutComments(GUIDE_SFC).replace(/\s+/g, ' ')
    expect(prose).toContain('Trinity does not issue certificates itself')
    expect(prose).toMatch(/obtain one for the name you save/)
    // The allowlist IS the security model, and it answers the question an
    // operator would otherwise have to ask: can somebody else point a domain
    // here and have this server request certificates for it?
    expect(prose).toMatch(/Only the name you save is allowed/)
    // DNS first: on-demand issuance fails until the record resolves here, and it
    // fails on somebody's page load rather than announcing itself.
    expect(prose).toMatch(/until the record points here/)
  })
})

describe('the certificate-renewal caveat (#2380 item 7)', () => {
  const ip = POSTURE_COPY['https-ip']

  it('warns that a long shutdown outlives a short-lived certificate', () => {
    expect(ip.detail).toMatch(/six days/i)
    expect(ip.detail).toMatch(/switched off|shut down|shutdown/i)
    expect(ip.detail).toMatch(/renew/i)
  })

  it('states it as a property of the profile, never of THIS connection', () => {
    // The binding AC on POSTURE_COPY: nothing here may assert a property of the
    // actual connection, because TLS terminates outside the backend and no
    // socket is ever probed. The renewal sentence is hedged the same way the
    // certificate sentence already is — "expect", "if ... set this up".
    expect(ip.detail).toMatch(/If a marketplace image or the DigitalOcean install script set this up/)
    expect(ip.detail).toMatch(/\bexpect\b/)
    for (const forbidden of [
      /your certificate expires/i,
      /this certificate is/i,
      /is secure\b/i,
      /is valid\b/i,
    ]) {
      expect(ip.detail).not.toMatch(forbidden)
    }
  })
})

describe('the store fails closed', () => {
  const flagPayload = {
    install_source: 'do-marketplace',
    hardening_guide_eligible: true,
    install_tls_posture: 'https-ip',
  }

  it('starts closed before anything is fetched', () => {
    expect(store.featureFlagsLoaded).toBe(false)
    expect(store.installSource).toBe('unknown')
    expect(store.hardeningGuideEligible).toBe(false)
    expect(store.installTlsPosture).toBe('unconfigured')
  })

  it('carries the three fields through on a successful read', async () => {
    axios.get.mockResolvedValueOnce({ data: flagPayload })
    await store.loadFeatureFlags()

    expect(store.installSource).toBe('do-marketplace')
    expect(store.hardeningGuideEligible).toBe(true)
    expect(store.installTlsPosture).toBe('https-ip')
  })

  it('falls back to the closed values when the payload omits them', async () => {
    axios.get.mockResolvedValueOnce({ data: {} })
    await store.loadFeatureFlags()

    // Not `undefined` — that reads as falsy but prints as "undefined".
    expect(store.installSource).toBe('unknown')
    expect(store.hardeningGuideEligible).toBe(false)
    expect(store.installTlsPosture).toBe('unconfigured')
  })

  it('fails CLOSED when the flag read fails, so the step stays out', async () => {
    axios.get.mockRejectedValueOnce(new Error('boom'))
    await store.loadFeatureFlags()

    expect(store.installSource).toBe('unknown')
    expect(store.hardeningGuideEligible).toBe(false)
    expect(store.installTlsPosture).toBe('unconfigured')
    expect(store.featureFlagsLoaded).toBe(true) // resolved, just not to a gate
  })
})
