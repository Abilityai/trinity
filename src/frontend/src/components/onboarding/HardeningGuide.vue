<!--
  First-run hardening guide (#2380).

  A marketplace install (DigitalOcean, Vultr) boots straight onto the public
  internet at whatever address the provider handed it, and nothing in the
  product ever mentions that. This card is the mention: it states the posture
  the instance ADVERTISES, and offers the two things that improve it.

  Four properties are load-bearing:

  1. **The gate is `marketplace_install`, resolved server-side.** Every other
     install — the whole managed fleet included — is false, so this never
     appears over an instance somebody already put behind Tailscale. The
     browser holds no copy of which sources count as a marketplace.

  2. **Admins only.** Not cosmetics: Settings → General is `adminOnly`, so for
     anyone else the card's one action falls through to the default tab, and
     the copy would be disclosing this box's network posture to every user of
     it. The predicate lives in the pure module (defaulting to hidden) rather
     than in `Dashboard.vue`, so the term stays testable — and it reads
     `profileVerified` first, because `userRole` answers 'user' until
     /api/users/me lands and the card would otherwise flash for a non-admin on
     every page load (the AdminEmailNudge #2198 rationale).

  3. **It is self-retiring.** `https-domain` hides it permanently with no client
     state: configuring a domain IS the completion condition. Dismissal is only
     for the operator who has decided not to act yet.

  4. **It claims only what is known.** The posture is derived by string-parsing
     the URL this instance is configured to hand out — nothing opens a socket or
     reads a certificate, because TLS terminates outside the backend. So the
     copy says "advertises", never "secure", and never renders a verdict on the
     certificate a browser would actually be shown.

  Markup composes BaseCard/BaseBadge/BaseButton rather than following the two
  neighbouring onboarding cards, which hand-roll their shell and are pre-ratchet.
-->
<template>
  <div v-if="visible" data-testid="hardening-guide" class="mx-4 mt-3 mb-3">
    <BaseCard flush>
      <div class="flex items-start justify-between gap-3 px-4 py-3">
        <div class="min-w-0">
          <div class="flex flex-wrap items-center gap-2">
            <h3 class="text-sm font-[550] text-gray-900 dark:text-gray-100">
              Secure this instance
            </h3>
            <!-- One fact per badge: what the instance advertises. Never a
                 verdict on a certificate nobody here has inspected. -->
            <BaseBadge :variant="copy.badgeVariant" dot>{{ copy.badge }}</BaseBadge>
          </div>
          <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">
            {{ copy.headline }}
          </p>

          <!--
            ONE action on the card face. Adding a domain is the only step that is
            collectable in-app, and it is also the completion condition that
            retires this card — so it is the primary, and everything explanatory
            moves behind the disclosure below. A first login should be able to
            act on this in one glance without reading two columns of prose.
          -->
          <div v-if="stage === 'address'" class="mt-3">
            <BaseButton
              variant="primary"
              size="sm"
              data-testid="hardening-guide-settings"
              @click="openSettings"
            >
              Add a domain
            </BaseButton>
          </div>

          <!--
            Native <details>: keyboard-accessible, no JS, no state to manage, and
            not a primitive the catalog covers. The card is a nudge on a first
            login — the reasoning has to be reachable, not unavoidable.
          -->
          <details class="mt-3" data-testid="hardening-guide-why">
            <summary
              class="cursor-pointer select-none text-[12.5px] text-action-primary-600 hover:text-action-primary-700 dark:text-action-primary-500 dark:hover:text-action-primary-400"
            >
              Why this matters
            </summary>

            <div class="mt-3 space-y-3 border-t border-gray-200 dark:border-gray-750 pt-3">
              <p class="text-[12.5px] leading-[1.5] text-gray-500 dark:text-gray-400">
                {{ copy.detail }}
              </p>

              <!--
                Two paths, presented as COMPLEMENTARY (issue AC): one settles how
                the instance is addressed, the other settles who can reach it at
                all — and the second BUILDS ON the first, because a tunnel needs
                the domain. The wording never offers them as an either/or.
              -->
              <div v-if="stage === 'address'" class="min-w-0">
                <h4 class="text-sm font-[550] text-gray-900 dark:text-gray-100">
                  Give it a real name
                </h4>
                <!--
                  Trinity does not issue, renew, or install certificates: `public_chat_url`
                  is a display/webhook-base setting and nothing in the tree reconfigures a
                  proxy or a listener from it. So this promises only what setting it does —
                  change the name Trinity hands out — and attributes the certificate change
                  to whatever actually terminates TLS.
                -->
                <p class="mt-1 text-[12.5px] leading-[1.5] text-gray-500 dark:text-gray-400">
                  Point a domain’s A record at this server, then set it as the
                  <span class="text-gray-600 dark:text-gray-300">Public URL</span>
                  in Settings → General so Trinity hands out the name instead of the IP. Trinity
                  does not issue certificates itself — whatever terminates TLS in front of it
                  picks up the name and can then use an ordinary long-lived certificate instead
                  of a short-lived IP one.
                </p>
              </div>

              <div class="min-w-0">
                <h4 class="text-sm font-[550] text-gray-900 dark:text-gray-100">
                  Serve it without exposing it
                </h4>
                <!--
                  #2380, decision recorded 2026-09-01: a Cloudflare Tunnel, NOT a VPN.
                  A VPN reaches the same posture but breaks every inbound integration —
                  Telegram, WhatsApp, VoIP, public agent links, x402, inbound A2A and
                  webhook triggers all call US. Slack survives on Socket Mode; nothing
                  else does. VPN remains a documented deployment mode in
                  docs/DEPLOYMENT.md; it is removed from this card only.

                  The honest limitation is stated in the copy rather than hidden: the
                  token has to reach `.env` and the tunnel starts under a compose
                  profile, and Trinity runs in a container with no host privileges. So
                  this path is guidance, and deliberately carries no button that could
                  not finish the job.
                -->
                <p class="mt-1 text-[12.5px] leading-[1.5] text-gray-500 dark:text-gray-400">
                  <template v-if="stage === 'address'">With that domain on Cloudflare, a</template><template v-else>With your domain on Cloudflare, a</template>
                  tunnel lets this server stop listening on the public internet altogether:
                  <span class="font-mono text-gray-600 dark:text-gray-300">cloudflared</span>
                  connects outward to Cloudflare, and traffic arrives back through that
                  connection. Inbound integrations — Telegram, WhatsApp, VoIP, public agent
                  links, webhook triggers — keep working, because they still reach a public
                  hostname. Trinity ships the service behind the
                  <span class="font-mono text-gray-600 dark:text-gray-300">tunnel</span>
                  compose profile, driven by
                  <span class="font-mono text-gray-600 dark:text-gray-300">TUNNEL_TOKEN</span>
                  in <span class="font-mono text-gray-600 dark:text-gray-300">.env</span>, so that
                  last step happens on the host rather than from this page.
                </p>
              </div>

              <p
                v-if="stage === 'address'"
                class="text-[12.5px] leading-[1.5] text-gray-500 dark:text-gray-400"
              >
                These two stack. A name settles how the instance is addressed; a tunnel
                settles who can reach it at all. The tunnel needs the name, so it is the
                second step rather than a different one.
              </p>
            </div>
          </details>
        </div>

        <BaseButton
          variant="ghost"
          size="sm"
          data-testid="hardening-guide-dismiss"
          aria-label="Dismiss the instance hardening guide"
          title="Dismiss"
          @click="dismiss"
        >
          <svg class="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </BaseButton>
      </div>
    </BaseCard>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { useSessionsStore } from '../../stores/sessions'
import BaseCard from '../base/BaseCard.vue'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import {
  hardeningStage,
  isHardeningGuideVisible,
  persistHardeningGuideDismissed,
  postureCopy,
  readHardeningGuideDismissed,
} from './hardeningGuide'

const store = useSessionsStore()
const authStore = useAuthStore()
const router = useRouter()

// Which of the two steps the card is on. `https-domain` means step one landed,
// so the card advances rather than retiring — see `hardeningGuide.js`.
const stage = computed(() => hardeningStage(store.installTlsPosture))

// Read once at setup, so a dismissal made in another tab this session does not
// pop the card back mid-render. Kept PER STAGE: waving away "you are on a bare
// IP" must not also consume tunnel advice the operator has never been shown.
const dismissed = ref({
  address: readHardeningGuideDismissed('address'),
  tunnel: readHardeningGuideDismissed('tunnel'),
})

const copy = computed(() => postureCopy(store.installTlsPosture) || {})

const visible = computed(
  () =>
    isHardeningGuideVisible({
      featureFlagsLoaded: store.featureFlagsLoaded,
      // `profileVerified` (#2198) is load-bearing, not defensive: `userRole`
      // falls back to 'user' until /api/users/me lands, so without it the card
      // would flash for a non-admin on every page load and then vanish.
      // `role`, not `userRole` — the latter never existed on the auth store, so
      // this card had been permanently hidden (found by the ent#437 eyeball).
      isAdmin: authStore.profileVerified && authStore.role === 'admin',
      marketplaceInstall: store.marketplaceInstall,
      installTlsPosture: store.installTlsPosture,
      dismissed: dismissed.value[stage.value],
    }) &&
    // A posture with no copy is one this card has nothing honest to say about.
    // Belt on the predicate, which already excludes the only such value today.
    !!postureCopy(store.installTlsPosture)
)

const dismiss = () => {
  // Hidden for this session regardless of whether storage accepted the write —
  // the helper warns, and a refused write is not a failed verb to the user.
  // Scoped to the stage on screen, so the other step can still have its turn.
  dismissed.value = { ...dismissed.value, [stage.value]: true }
  persistHardeningGuideDismissed(stage.value)
}

const openSettings = () => router.push('/settings?tab=general')

onMounted(() => {
  // Shared, cached, and already awaited by the rest of the page: `once()` means
  // this costs nothing when another consumer got there first.
  store.loadFeatureFlags()
})
</script>
