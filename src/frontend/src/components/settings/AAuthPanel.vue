<template>
  <!--
    Calls between Trinity instances — the AAuth switch and issuer
    (abilityai/trinity-enterprise#862). Semantic tokens + gray neutrals only.
  -->
  <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg" data-testid="aauth-panel">
    <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
      <div class="flex items-center gap-2">
        <h2 class="text-lg font-medium text-gray-900 dark:text-white">Calls between Trinity instances (AAuth)</h2>
        <BaseBadge v-if="view.state === 'ready'" :variant="statusVariant" data-testid="aauth-status">
          <span aria-hidden="true">{{ statusGlyph }}</span>
          {{ statusLabel }}
        </BaseBadge>
      </div>
      <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
        Agents here call agents on another Trinity instance as themselves, with a key only this
        instance holds — no shared secret. The other instance checks the signature and admits only
        the agent identities it has listed.
      </p>
    </div>

    <div class="p-6 space-y-5">
      <div v-if="view.state === 'loading'" aria-busy="true">
        <span class="sr-only">Loading AAuth settings…</span>
        <SkeletonLoader :count="4" height="2rem" gap="1rem" />
      </div>

      <LoadFailed
        v-else-if="view.state === 'failed'"
        dense
        title="Couldn't load the AAuth settings"
        :message="store.aauthError"
        :retrying="store.aauthLoading"
        @retry="store.loadAAuth()"
      />

      <template v-else>
        <InlineError
          v-if="view.stale"
          :message="`Couldn't refresh the AAuth settings — showing the last values that loaded. ${store.aauthError}`"
          retryable
          @retry="store.loadAAuth()"
          @dismiss="store.aauthError = ''"
        />
        <InlineError :message="saveError" @dismiss="saveError = ''" />

        <!-- On without a valid issuer is off in effect: say so, never show a green switch. -->
        <div
          v-if="state.enabled && !state.issuer_valid"
          class="rounded-md border border-status-warning-200 dark:border-status-warning-500/30 bg-status-warning-50 dark:bg-status-warning-500/10 px-4 py-3 text-sm text-status-warning-700 dark:text-status-warning-300"
          data-testid="aauth-inert"
        >
          Switched on, but inactive: AAuth needs a valid issuer before anything is published, signed
          or accepted. Set the issuer below.
        </div>

        <div class="flex items-start justify-between gap-4">
          <div>
            <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Sign and accept agent-to-agent calls</p>
            <p class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
              <template v-if="state.live">
                This instance publishes its public key, signs its agents' calls, and accepts calls from
                agents another instance's operator has listed on an agent here.
              </template>
              <template v-else>Off — nothing is published, signed or accepted.</template>
              <template v-if="state.enabled_source === 'env'"> Currently set by <code>AAUTH_ENABLED</code>; switching it here overrides that.</template>
            </p>
          </div>
          <BaseToggle
            :model-value="!!state.enabled"
            :disabled="saving"
            aria-label="Sign and accept agent-to-agent calls"
            data-testid="aauth-toggle"
            @update:model-value="toggle"
          />
        </div>

        <div>
          <label for="aauth-issuer" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
            Issuer — this instance's public HTTPS host
          </label>
          <div class="mt-1 flex flex-col gap-2 sm:flex-row">
            <BaseInput
              id="aauth-issuer"
              v-model="issuerDraft"
              type="url"
              spellcheck="false"
              placeholder="https://trinity.example.com"
              :disabled="saving"
              :error="issuerError"
              class="flex-1 min-w-0"
              data-testid="aauth-issuer"
              @keyup.enter="saveIssuer"
            />
            <div class="flex items-start">
              <BaseButton
                :disabled="saving || !issuerDirty || !!issuerError"
                :loading="saving"
                loading-label="Saving…"
                data-testid="aauth-issuer-save"
                @click="saveIssuer"
              >
                Save
              </BaseButton>
            </div>
          </div>
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
            Must match the host in this instance's A2A agent card URLs. Agents here are known to other
            instances as
            <span class="font-mono break-all">{{ identityText }}</span>.
            <template v-if="state.issuer_source === 'env'"> Currently set by <code>AAUTH_ISSUER</code>.</template>
          </p>
        </div>

        <div class="rounded-md border border-gray-200 dark:border-gray-700 px-4 py-3" data-testid="aauth-readiness">
          <p class="text-xs font-mono uppercase tracking-wide text-gray-500 dark:text-gray-400">Ready to call another instance?</p>
          <ul class="mt-2 space-y-2">
            <li v-for="item in readiness" :key="item.key" class="flex items-start gap-2 text-sm" :data-ok="item.ok">
              <span
                aria-hidden="true"
                :class="item.ok ? 'text-status-success-700 dark:text-status-success-400' : 'text-gray-500 dark:text-gray-400'"
              >{{ item.ok ? '✓' : '○' }}</span>
              <span>
                <span class="text-gray-900 dark:text-gray-100">{{ item.label }}</span>
                <span class="sr-only">{{ item.ok ? '— done' : '— not yet' }}</span>
                <span v-if="!item.ok" class="block text-xs text-gray-600 dark:text-gray-300">{{ item.next }}</span>
              </span>
            </li>
          </ul>
          <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">
            The instance being called also needs AAuth on, and must list the calling agent's identity on
            the agent it calls. A peer on a private network must be on the outbound allowlist and serve
            HTTPS with a certificate this instance trusts.
          </p>
        </div>

        <details class="text-sm">
          <summary class="cursor-pointer text-xs text-gray-500 dark:text-gray-400 select-none">What this does not protect against</summary>
          <ul class="mt-2 space-y-1 list-disc pl-5 text-xs text-gray-600 dark:text-gray-300">
            <li>The instance key signs for every agent here, so the unit of trust is the instance: a compromised instance can speak as any of its agents.</li>
            <li>Identities are keyed by agent name. Renaming an agent changes its identity; an agent deleted and re-created under the same name inherits what peers granted the old one.</li>
            <li>Replay protection needs Redis and refuses calls while Redis is unavailable.</li>
            <li>A proxy that rewrites signed headers breaks every call; changing the issuer changes every agent's identity.</li>
          </ul>
        </details>

        <p class="text-xs text-gray-500 dark:text-gray-400" data-testid="aauth-prototype">
          Person-token and resource-calling prototypes:
          {{ state.prototype_enabled ? 'on (they also need the switch above)' : 'off' }}.
        </p>
      </template>
    </div>

    <ConfirmDialog
      v-model:visible="confirmIssuer"
      title="Change this instance's issuer?"
      :message="`Every agent here becomes aauth:<agent>@${draftHost}. Peers that listed the old identities will refuse their calls until they list the new ones.`"
      confirm-text="Change issuer"
      variant="warning"
      @confirm="commitIssuer"
    />
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { useA2aFederationStore } from '../../stores/a2aFederation'
import { buildAAuthUpdate, issuerProblem, readinessItems } from '../../utils/aauthSettings'
import { viewState } from '../../utils/loadingState'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import BaseToggle from '../base/BaseToggle.vue'
import ConfirmDialog from '../ConfirmDialog.vue'
import InlineError from '../InlineError.vue'
import LoadFailed from '../LoadFailed.vue'
import SkeletonLoader from '../SkeletonLoader.vue'

const store = useA2aFederationStore()
const saving = ref(false)
const saveError = ref('')
const issuerDraft = ref('')
const confirmIssuer = ref(false)

const view = computed(() => viewState({
  loading: store.aauthLoading, hasLoaded: store.aauthLoaded, error: store.aauthError,
}))
const state = computed(() => store.aauth || {})
const readiness = computed(() => readinessItems(state.value))
const issuerError = computed(() => issuerProblem(issuerDraft.value))
const issuerDirty = computed(() => Object.keys(buildAAuthUpdate(
  state.value, { enabled: state.value.enabled, issuer: issuerDraft.value },
)).length > 0)
const identityText = computed(() => state.value.identity_example || 'aauth:<agent-name>@<issuer host>')
const draftHost = computed(() => issuerDraft.value.trim().replace(/^https:\/\//, '') || '(none)')

const statusLabel = computed(() => {
  if (state.value.live) return 'Live'
  return state.value.enabled ? 'On, inactive' : 'Off'
})
const statusGlyph = computed(() => (state.value.live ? '●' : state.value.enabled ? '▲' : '○'))
const statusVariant = computed(() => (state.value.live ? 'success' : state.value.enabled ? 'warning' : 'neutral'))

// The draft follows the loaded value until the admin edits it.
watch(() => store.aauth?.issuer, (v) => { issuerDraft.value = v || '' }, { immediate: true })

async function save(body) {
  saveError.value = ''
  saving.value = true
  try {
    await store.saveAAuth(body)
  } catch (e) {
    saveError.value = e.message
  } finally {
    saving.value = false
  }
}

function toggle(next) {
  return save({ enabled: Boolean(next) })
}

function saveIssuer() {
  if (!issuerDirty.value || issuerError.value) return
  // Changing a LIVE issuer re-keys every agent's identity for every peer.
  if (state.value.live && state.value.issuer) {
    confirmIssuer.value = true
    return
  }
  return commitIssuer()
}

function commitIssuer() {
  return save(buildAAuthUpdate(state.value, { enabled: state.value.enabled, issuer: issuerDraft.value }))
}

onMounted(() => { store.loadAAuth() })
</script>
