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
      :badge="stage === 'tunnel' ? 'Done' : 'Recommended'"
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
        <BaseInput
          v-model="url"
          type="url"
          label="Public URL"
          placeholder="https://your-domain.com"
          :error="fieldError"
          :disabled="saving"
          help="The address people will use to reach this instance."
          data-testid="first-run-public-url"
        />
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
              Point a domain’s A record at this server, then save it as the
              <span class="text-gray-600 dark:text-gray-300">Public URL</span>
              above (it also lives in Settings → General). Trinity does not issue certificates
              itself, but whatever terminates TLS in front of it is configured to obtain one for
              the name you save, the first time someone visits it — so the domain gets an
              ordinary long-lived certificate instead of a short-lived IP one, and Trinity hands
              out the name instead of the IP. Only the name you save is allowed, so nobody else
              can point a domain here and have certificates issued. Give DNS time to settle
              first: until the record points here, the name has nothing to answer it.
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

          <p v-if="stage === 'address'" class="text-[12.5px] leading-[1.5] text-gray-500 dark:text-gray-400">
            These two stack. A name settles how the instance is addressed; a tunnel
            settles who can reach it at all. The tunnel needs the name, so it is the
            second step rather than a different one.
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
import { hardeningStage, postureCopy } from '../hardeningGuide'

const props = defineProps({ ctx: { type: Object, default: () => ({}) } })
const emit = defineEmits(['complete', 'skip'])

const settingsStore = useSettingsStore()

// `https-domain` means step one landed: the step reads done and the copy speaks
// only to the (optional) tunnel.
const stage = computed(() => hardeningStage(props.ctx.tlsPosture))
const copy = computed(() => postureCopy(props.ctx.tlsPosture) || postureCopy('unconfigured'))

const url = ref('')
const saving = ref(false)
const fieldError = ref('')
const saveError = ref('')

async function save() {
  const value = url.value.trim().replace(/\/+$/, '')
  // Named, actionable, with an example (principle 17). The posture is derived
  // from this string, so a bare hostname would read as "no public URL".
  if (!/^https?:\/\/[^\s/]+\.[^\s/]+/.test(value)) {
    fieldError.value = 'Enter the full address, including https:// — for example https://trinity.example.com'
    return
  }
  fieldError.value = ''
  saveError.value = ''
  saving.value = true
  try {
    await settingsStore.updateSetting('public_chat_url', value)
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
