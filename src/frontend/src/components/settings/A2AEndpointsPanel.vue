<template>
  <!--
    Outbound A2A endpoints — the platform-wide registry `call_a2a_agent`
    resolves against (#736), with the authentication scheme per endpoint
    (abilityai/trinity-enterprise#862). Semantic tokens + gray neutrals only.
  -->
  <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg" data-testid="a2a-endpoints-panel">
    <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
      <div class="flex items-start justify-between gap-4">
        <div>
          <h2 class="text-lg font-medium text-gray-900 dark:text-white">Outbound A2A endpoints</h2>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Agents on other instances (or any A2A server) that agents here may call. An agent picks a
            target by name and never supplies a URL; every agent on this instance may call every
            endpoint listed.
          </p>
        </div>
        <div class="flex items-center gap-2 flex-none">
          <span class="text-xs text-gray-600 dark:text-gray-300" data-testid="a2a-outbound-status">
            {{ store.outboundEnabled ? 'Calls allowed' : 'Calls off' }}
          </span>
          <BaseToggle
            :model-value="store.outboundEnabled"
            :disabled="switching || view.state !== 'ready' && view.state !== 'empty'"
            aria-label="Allow outbound A2A calls"
            data-testid="a2a-outbound-toggle"
            @update:model-value="toggleOutbound"
          />
        </div>
      </div>
    </div>

    <div class="p-6 space-y-5">
      <div v-if="view.state === 'loading'" aria-busy="true">
        <span class="sr-only">Loading outbound endpoints…</span>
        <SkeletonLoader :count="3" height="2rem" gap="0.75rem" />
      </div>

      <LoadFailed
        v-else-if="view.state === 'failed'"
        dense
        title="Couldn't load the outbound endpoints"
        :message="store.endpointsError"
        :retrying="store.endpointsLoading"
        @retry="store.loadEndpoints()"
      />

      <template v-else>
        <InlineError
          v-if="view.stale"
          :message="`Couldn't refresh the endpoints — showing the last list that loaded. ${store.endpointsError}`"
          retryable
          @retry="store.loadEndpoints()"
          @dismiss="store.endpointsError = ''"
        />
        <InlineError :message="actionError" @dismiss="actionError = ''" />

        <div
          v-if="!store.outboundEnabled"
          class="rounded-md border border-status-warning-200 dark:border-status-warning-500/30 bg-status-warning-50 dark:bg-status-warning-500/10 px-4 py-3 text-sm text-status-warning-700 dark:text-status-warning-300"
          data-testid="a2a-outbound-off"
        >
          Outbound calls are off: a registered endpoint cannot be called until the switch above is on.
        </div>

        <!-- The list: bounded viewport, sticky header, stated total (principle 28). -->
        <div class="rounded-md border border-gray-200 dark:border-gray-700">
          <div class="max-h-80 overflow-y-auto">
            <table class="min-w-full text-sm" data-testid="a2a-endpoints-table">
              <thead class="sticky top-0 bg-gray-100 dark:bg-gray-750 text-[11px] font-mono uppercase tracking-wide text-gray-600 dark:text-gray-300">
                <tr>
                  <th class="px-3 py-2 text-left font-medium">Name</th>
                  <th class="px-3 py-2 text-left font-medium">URL</th>
                  <th class="px-3 py-2 text-left font-medium">Auth</th>
                  <th class="px-3 py-2 text-right font-medium"><span class="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody class="divide-y divide-gray-200 dark:divide-gray-700">
                <tr v-if="view.state === 'empty'">
                  <td colspan="4" class="px-3 py-6 text-center text-sm text-gray-500 dark:text-gray-400" data-testid="a2a-endpoints-empty">
                    No endpoints yet. Register the first one below — for another Trinity instance,
                    use its agent's A2A URL and the AAuth scheme.
                  </td>
                </tr>
                <tr v-for="ep in store.endpoints" :key="ep.id" :data-testid="`a2a-endpoint-${ep.name}`">
                  <td class="px-3 py-2 font-medium text-gray-900 dark:text-gray-100">{{ ep.name }}</td>
                  <td class="px-3 py-2 font-mono text-xs text-gray-600 dark:text-gray-300 break-all">{{ ep.url }}</td>
                  <td class="px-3 py-2">
                    <BaseBadge :variant="ep.auth_scheme === 'aauth' ? 'info' : 'neutral'" size="sm">
                      {{ ep.auth_scheme === 'aauth' ? 'AAuth · signed as the agent' : (ep.has_credentials ? 'Bearer · credential stored' : 'Bearer · no credential') }}
                    </BaseBadge>
                  </td>
                  <td class="px-3 py-2 text-right whitespace-nowrap">
                    <BaseButton variant="ghost" size="sm" :disabled="busy" @click="edit(ep)">Edit</BaseButton>
                    <BaseButton variant="ghost" size="sm" :disabled="busy" :data-testid="`a2a-remove-${ep.name}`" @click="askRemove(ep)">Remove</BaseButton>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="px-3 py-1.5 border-t border-gray-200 dark:border-gray-700 text-xs text-gray-500 dark:text-gray-400 tabular-nums">
            {{ store.endpoints.length }} endpoint{{ store.endpoints.length === 1 ? '' : 's' }}
          </p>
        </div>

        <!-- Register / update by name. -->
        <form class="space-y-3" data-testid="a2a-endpoint-form" @submit.prevent="save">
          <p class="text-sm font-medium text-gray-900 dark:text-gray-100">
            {{ editing ? `Update “${form.name}”` : 'Register an endpoint' }}
          </p>
          <div class="grid gap-3 sm:grid-cols-2">
            <BaseInput
              id="a2a-endpoint-name"
              v-model="form.name"
              label="Name"
              placeholder="cornelius-architect"
              :disabled="busy || editing"
              :error="errors.name"
              data-testid="a2a-endpoint-name"
            />
            <BaseInput
              id="a2a-endpoint-url"
              v-model="form.url"
              label="A2A URL"
              type="url"
              spellcheck="false"
              placeholder="https://trinity.example.com/a2a/cornelius-architect"
              :disabled="busy"
              :error="errors.url"
              data-testid="a2a-endpoint-url"
            />
            <BaseSelect
              id="a2a-endpoint-scheme"
              v-model="form.auth_scheme"
              label="Authentication"
              :disabled="busy"
              :help="form.auth_scheme === 'aauth'
                ? 'Each call is signed as the calling agent (aauth:<agent>@<this host>). No credential is stored; the peer must list that identity.'
                : 'The stored credential is sent as Authorization: Bearer.'"
              data-testid="a2a-endpoint-scheme"
            >
              <option value="bearer">Bearer credential</option>
              <option value="aauth">AAuth (signed as the agent)</option>
            </BaseSelect>
            <BaseInput
              v-if="form.auth_scheme !== 'aauth'"
              id="a2a-endpoint-credential"
              v-model="form.credentials"
              label="Credential"
              type="password"
              autocomplete="off"
              :placeholder="editing && editing.has_credentials ? 'Leave blank to keep the stored one' : 'Token or API key (optional)'"
              :disabled="busy"
              data-testid="a2a-endpoint-credential"
            />
            <p
              v-else-if="editing && editing.has_credentials"
              class="text-xs text-status-warning-700 dark:text-status-warning-300 self-end"
              data-testid="a2a-endpoint-drop-note"
            >
              Saving drops the stored credential — an AAuth endpoint holds none.
            </p>
          </div>
          <div class="flex items-center gap-2">
            <BaseButton type="submit" :disabled="busy || !canSave" :loading="saving" loading-label="Saving…" data-testid="a2a-endpoint-save">
              {{ editing ? 'Save changes' : 'Register' }}
            </BaseButton>
            <BaseButton v-if="editing" variant="secondary" :disabled="busy" @click="resetForm">Cancel</BaseButton>
          </div>
        </form>
      </template>
    </div>

    <ConfirmDialog
      v-model:visible="confirmRemove"
      title="Remove this endpoint?"
      :message="`Agents will no longer be able to call “${pendingRemove?.name || ''}”. A stored credential for it is deleted.`"
      confirm-text="Remove"
      @confirm="doRemove"
      @cancel="pendingRemove = null"
    />
  </div>
</template>

<script setup>
import { ref, computed, reactive, onMounted } from 'vue'
import { useA2aFederationStore } from '../../stores/a2aFederation'
import { viewState } from '../../utils/loadingState'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import BaseSelect from '../base/BaseSelect.vue'
import BaseToggle from '../base/BaseToggle.vue'
import ConfirmDialog from '../ConfirmDialog.vue'
import InlineError from '../InlineError.vue'
import LoadFailed from '../LoadFailed.vue'
import SkeletonLoader from '../SkeletonLoader.vue'

const store = useA2aFederationStore()
const saving = ref(false)
const switching = ref(false)
const removing = ref(false)
const actionError = ref('')
const editing = ref(null)
const confirmRemove = ref(false)
const pendingRemove = ref(null)
const form = reactive({ name: '', url: '', auth_scheme: 'bearer', credentials: '' })

const busy = computed(() => saving.value || switching.value || removing.value)
const view = computed(() => viewState({
  loading: store.endpointsLoading, hasLoaded: store.endpointsLoaded,
  error: store.endpointsError, count: store.endpoints.length,
}))

// Named, with an example (principle 17). The server re-validates (SSRF, ports, the allowlist).
const errors = computed(() => {
  const out = { name: '', url: '' }
  const name = form.name.trim()
  const url = form.url.trim()
  if (name.length > 200) out.name = 'Keep the name under 200 characters.'
  if (url && !/^https?:\/\/\S+$/.test(url)) out.url = 'Enter a full URL. Example: https://trinity.example.com/a2a/agent-name'
  if (url && form.auth_scheme === 'aauth') {
    if (!url.startsWith('https://')) out.url = 'An AAuth endpoint must be https://.'
    else if (/^https:\/\/[^/]+:\d+/.test(url)) out.url = 'An AAuth endpoint must use the default HTTPS port (no :port).'
  }
  return out
})
const canSave = computed(() => form.name.trim() && form.url.trim() && !errors.value.name && !errors.value.url)

function resetForm() {
  editing.value = null
  form.name = ''
  form.url = ''
  form.auth_scheme = 'bearer'
  form.credentials = ''
}

function edit(ep) {
  editing.value = ep
  form.name = ep.name
  form.url = ep.url
  form.auth_scheme = ep.auth_scheme || 'bearer'
  form.credentials = ''
}

async function save() {
  if (!canSave.value) return
  actionError.value = ''
  saving.value = true
  try {
    const body = { name: form.name.trim(), url: form.url.trim(), auth_scheme: form.auth_scheme }
    // An aauth endpoint stores no secret; a blank credential on a bearer update keeps the stored one.
    if (form.auth_scheme !== 'aauth' && form.credentials) body.credentials = form.credentials
    await store.upsertEndpoint(body)
    resetForm()
  } catch (e) {
    actionError.value = e.message
  } finally {
    saving.value = false
  }
}

function askRemove(ep) {
  pendingRemove.value = ep
  confirmRemove.value = true
}

async function doRemove() {
  const ep = pendingRemove.value
  pendingRemove.value = null
  if (!ep) return
  actionError.value = ''
  removing.value = true
  try {
    await store.removeEndpoint(ep.id || ep.name)
    if (editing.value?.id === ep.id) resetForm()
  } catch (e) {
    actionError.value = e.message
  } finally {
    removing.value = false
  }
}

async function toggleOutbound(next) {
  actionError.value = ''
  switching.value = true
  try {
    await store.setOutboundEnabled(Boolean(next))
  } catch (e) {
    actionError.value = e.message
  } finally {
    switching.value = false
  }
}

onMounted(() => { store.loadEndpoints() })
</script>
