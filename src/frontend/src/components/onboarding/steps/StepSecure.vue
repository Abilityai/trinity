<!--
  Step `secure` (ent#581) — absorbs the first-run hardening guide (#2380/#2564).

  Both stages in one step: the domain field on the face, the reasoning and the
  Cloudflare Tunnel guidance behind a native <details>. The posture copy lives
  in `hardeningGuide.js`, unchanged, and the four properties that carried the
  card still hold:

  1. The gate is `hardening_guide_eligible`, resolved server-side — PROVENANCE,
     never TLS state, so this never appears over a managed instance already
     behind Tailscale. The registry predicate reads it; this file does not.
  2. Admins only (`eligible` in firstRunSteps.js): the copy discloses the box's
     network posture, and the setting it writes is admin-only.
  3. Self-retiring: saving a domain IS the completion condition. The step then
     reads done and speaks only to the tunnel, which is optional.
  4. It claims only what is known. The posture is string-parsed from the URL
     this instance is configured to hand out — nothing opens a socket or reads
     a certificate — so the copy says "advertises", never "secure".
-->
<template>
  <div data-testid="first-run-step-secure">
    <FirstRunStepHeader
      kicker="Network"
      title="Secure this instance"
      :lead="copy.headline"
      :badge="stage === 'tunnel' && reached ? 'Done' : 'Recommended'"
      schematic="secure"
    />

    <div class="mt-5 space-y-4">
      <!-- One fact per badge: what the instance advertises. Never a verdict on
           a certificate nobody here has inspected. -->
      <BaseBadge :variant="copy.badgeVariant" dot>{{ copy.badge }}</BaseBadge>

      <!--
        ONE action on the face, and since #2380's on-demand-TLS change it is an
        action that finishes the job: Caddy asks the backend whether a hostname
        is allowed (`/api/public/tls-allowed`) and obtains the certificate on
        first request, so saving this field IS the whole step. Do not
        reintroduce a host command here: a non-engineer following a deploy
        guide has no root shell in the loop.
      -->
      <form v-if="stage === 'address'" class="max-w-md space-y-2" novalidate @submit.prevent="save">
        <!-- Benefit and prerequisite are readable WITHOUT opening the
             disclosure (#2691). The help text used to say only "the address
             people will use to reach this instance", which states neither. -->
        <BaseInput
          v-model="url"
          type="url"
          label="Public URL"
          placeholder="https://your-domain.com"
          :error="fieldError"
          :disabled="saving"
          :help="DOMAIN_BENEFIT"
          data-testid="first-run-public-url"
        />
        <p
          class="text-[12.5px] leading-[1.5] text-gray-500 dark:text-gray-400"
          data-testid="first-run-public-url-prerequisite"
        >
          {{ DOMAIN_PREREQUISITE }}
        </p>
        <BaseButton
          type="submit"
          variant="secondary"
          size="sm"
          :loading="saving"
          loading-label="Saving…"
          :disabled="!url.trim()"
          data-testid="first-run-public-url-save"
        >
          Save domain
        </BaseButton>
      </form>
      <InlineError v-if="saveError" :message="saveError" @dismiss="saveError = ''" />

      <!--
        The save's own confirmation. Without it the only signals are the field
        emptying and the posture badge changing, which is too little for an
        action that re-points every live Telegram and WhatsApp webhook.

        It states the half that is known (the value is stored) and the half
        that is not (nothing has arrived at the name), because those are
        genuinely different facts here and the second is what the operator
        will otherwise assume. It is NOT a claim that the domain works —
        `postureCopy` owns that, and only once a request has landed.
      -->
      <p
        v-if="savedUrl"
        class="text-[12.5px] leading-[1.5] text-status-success-700 dark:text-status-success-300"
        data-testid="first-run-public-url-saved"
        role="status"
      >
        Saved. Nothing has reached
        <span class="font-mono">{{ savedUrl }}</span>
        yet — open it in a browser to confirm it works.
      </p>

      <!-- Native <details>: keyboard-accessible, no JS, no state. The reasoning
           has to be reachable, not unavoidable. -->
      <details data-testid="first-run-secure-why">
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
            Two paths, presented as COMPLEMENTARY (#2380 AC): one settles how
            the instance is addressed, the other who can reach it at all — and
            the second BUILDS ON the first, because a tunnel needs the domain.
          -->
          <div v-if="stage === 'address'" class="min-w-0">
            <h3 class="text-sm font-[550] text-gray-900 dark:text-gray-100">
              Give it a real name
            </h3>
            <!--
              Trinity issues no certificates: `public_chat_url` is a display and
              webhook-base setting. What makes saving it the whole step is the
              WEB SERVER: the provisioned Caddyfile carries on-demand TLS with an
              `ask` gate at `/api/public/tls-allowed`, so Caddy obtains a
              certificate for the saved name on first request and refuses every
              other name. Still no verdict on the live connection.
            -->
            <p class="mt-1 text-[12.5px] leading-[1.5] text-gray-500 dark:text-gray-400">
              Point your domain’s A record at this server, then save it as the
              <span class="text-gray-600 dark:text-gray-300">Public URL</span>
              above (it also lives in Settings → General). Trinity does not issue certificates
              itself — the web server in front of it is configured to obtain one for the name
              you save, the first time someone visits. After that Trinity hands out the name
              instead of the IP. Only the name you save is allowed, so nobody else can point a
              domain here and have certificates issued. Point the domain at this server before
              you save: the certificate is
              obtained on the first request that arrives for the name, so if the record is
              missing or points elsewhere, that request never gets here — the visitor sees a
              certificate error, and Trinity, which is not part of that exchange, carries on
              showing the name as saved.
            </p>
          </div>

          <div class="min-w-0">
            <h3 class="text-sm font-[550] text-gray-900 dark:text-gray-100">
              Serve it without exposing it
            </h3>
            <!--
              #2380, decided 2026-09-01: a Cloudflare Tunnel, NOT a VPN. A VPN
              reaches the same posture but breaks every inbound integration —
              Telegram, WhatsApp, VoIP, public agent links, x402, inbound A2A and
              webhook triggers all call US. The token has to reach `.env` and the
              tunnel starts under a compose profile, so this path is guidance and
              carries no button that could not finish the job.
            -->
            <p class="mt-1 text-[12.5px] leading-[1.5] text-gray-500 dark:text-gray-400">
              <template v-if="stage === 'address'">With that domain on Cloudflare, a</template><template v-else>With your domain on Cloudflare, a</template>
              tunnel lets this server stop listening on the public internet altogether:
              Cloudflare holds a connection open from the inside, and visitors arrive
              through it. Telegram, WhatsApp, voice calls, public agent links and webhooks
              keep working, because they still reach a public address. Setting it up takes a
              few minutes on the server itself, so it happens on the host rather than from
              this page — the guide below walks through it.
            </p>
          </div>

          <p v-if="stage === 'address'" class="text-[12.5px] leading-[1.5] text-gray-500 dark:text-gray-400">
            These two stack. A name settles how the instance is addressed; a tunnel
            settles who can reach it at all. The tunnel needs the name, so it is the
            second step rather than a different one.
          </p>

          <!-- #2692: the full walkthrough — both steps end to end, plus the VPN
               option this page deliberately does not carry. First docs link this
               step has ever had; until the next release cut it 404s, which is
               the same release the page itself ships in. -->
          <p class="text-[12.5px] leading-[1.5] text-gray-500 dark:text-gray-400">
            <a
              :href="HARDENING_DOCS_URL"
              target="_blank"
              rel="noopener noreferrer"
              class="text-action-primary-600 hover:text-action-primary-700 dark:text-action-primary-500 dark:hover:text-action-primary-400"
              data-testid="first-run-secure-docs"
            >
              Read the full hardening guide
            </a>
            — both steps end to end, and a private-network option for an instance only you
            need to reach.
          </p>
        </div>
      </details>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useSettingsStore } from '../../../stores/settings'
import BaseBadge from '../../base/BaseBadge.vue'
import BaseButton from '../../base/BaseButton.vue'
import BaseInput from '../../base/BaseInput.vue'
import InlineError from '../../InlineError.vue'
import FirstRunStepHeader from '../FirstRunStepHeader.vue'
import {
  DOMAIN_BENEFIT,
  DOMAIN_PREREQUISITE,
  HARDENING_DOCS_URL,
  hardeningStage,
  postureCopy,
} from '../hardeningGuide'

const props = defineProps({ ctx: { type: Object, default: () => ({}) } })
const emit = defineEmits(['complete', 'skip'])

const settingsStore = useSettingsStore()

// `https-domain` means step one landed: the step reads done and the copy speaks
// only to the (optional) tunnel.
const stage = computed(() => hardeningStage(props.ctx.tlsPosture))
// #2691: saved is not the same as working. The tick waits for a request to
// actually arrive for the saved name — the only proof available from in here,
// and the one thing a DNS lookup at save time could not tell us, since a
// proxied or load-balanced domain resolves somewhere else by design.
const reached = computed(() => !!props.ctx.publicUrlReached)
const copy = computed(
  () => postureCopy(props.ctx.tlsPosture, reached.value) || postureCopy('unconfigured')
)

const url = ref('')
const saving = ref(false)
const fieldError = ref('')
const saveError = ref('')
// The saved value, kept so the confirmation can name it after the field clears.
const savedUrl = ref('')

async function save() {
  const value = url.value.trim().replace(/\/+$/, '')
  // Named, actionable, with an example (principle 17). The posture is derived
  // from this string, so a bare hostname would read as "no public URL".
  // `https` only (#2691): the pattern was `https?`, so `http://…` saved
  // cleanly, the step emitted `complete` and advanced, and the badge then read
  // "Advertises HTTP" — the step congratulating the operator for reaching the
  // posture it exists to move them off.
  if (!/^https:\/\/[^\s/]+\.[^\s/]+/.test(value)) {
    fieldError.value = 'Enter the full address, including https:// — for example https://trinity.example.com'
    return
  }
  fieldError.value = ''
  saveError.value = ''
  // A new attempt retires the previous confirmation, or a failure would show
  // an error next to a success that no longer describes anything.
  savedUrl.value = ''
  saving.value = true
  try {
    await settingsStore.updateSetting('public_chat_url', value)
    savedUrl.value = value
    url.value = ''
    // The chassis re-reads the flags, which re-derives `install_tls_posture`.
    emit('complete')
  } catch (e) {
    const detail = e?.response?.data?.detail
    saveError.value =
      typeof detail === 'string' ? detail : 'Could not save the Public URL. Check the address and try again.'
  } finally {
    saving.value = false
  }
}
</script>
