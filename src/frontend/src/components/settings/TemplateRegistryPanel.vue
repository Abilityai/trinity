<template>
  <!--
    Remote template registry (TMPL-002, trinity-enterprise#14).

    A NEW FILE on purpose. Settings.vue carries 63 non-gray raw-palette classes
    in `raw-color-baseline.json` and the ratchet only lets per-file counts
    shrink, so the panel lives here and Settings.vue gains one import and one
    tag — no color class at all. Everything below is semantic tokens
    (`status-*` / `action-*`) plus gray neutrals; zero raw palette, zero hex.
  -->
  <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
    <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
      <div class="flex items-center gap-2">
        <h2 class="text-lg font-medium text-gray-900 dark:text-white">Template registry</h2>
        <BaseBadge v-if="config.source === 'default'">Default</BaseBadge>
      </div>
      <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
        Where Trinity reads its list of GitHub starter templates from. If it can't be
        reached, the catalog falls back to your bundled templates — browsing degrades,
        creating never does: <code class="text-xs">github:owner/repo</code> always works.
      </p>
    </div>

    <div class="p-6 space-y-5">
      <!-- Failed FETCH of the panel's own config: LoadFailed, never the empty copy. -->
      <LoadFailed
        v-if="loadError"
        dense
        title="Couldn't load the registry settings"
        :message="loadError"
        :retrying="loading"
        @retry="load"
      />

      <template v-else>
        <!-- Failed VERB: persists next to the control, dismissible (principle 18). -->
        <InlineError
          :message="saveError"
          :detail="saveErrorDetail"
          @dismiss="clearSaveError"
        />

        <!-- Hard-disabled by config: an inert toggle that says why beats one
             that silently does nothing. -->
        <div
          v-if="config.hard_disabled"
          class="rounded-md border border-status-warning-200 dark:border-status-warning-500/30 bg-status-warning-50 dark:bg-status-warning-500/10 px-4 py-3 text-sm text-status-warning-700 dark:text-status-warning-300"
        >
          The registry is disabled by configuration
          (<code class="text-xs">TEMPLATE_REGISTRY_ENABLED=false</code>). Nothing is
          fetched, and no setting here can turn it back on.
        </div>

        <!-- An admin-curated GitHub list wins outright, so say so rather than
             showing a healthy registry that contributes nothing. -->
        <div
          v-else-if="config.suppressed_by_github_templates"
          class="rounded-md border border-status-info-200 dark:border-status-info-500/30 bg-status-info-50 dark:bg-status-info-500/10 px-4 py-3 text-sm text-status-info-700 dark:text-status-info-300"
        >
          You've curated your own GitHub Templates list, which takes full precedence.
          The registry isn't consulted while that list exists.
        </div>

        <!-- Toggle -->
        <div class="flex items-start justify-between gap-4">
          <div>
            <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Use the remote registry</p>
            <p class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
              <template v-if="config.enabled">Fetched hourly and cached.</template>
              <template v-else>Off — only your bundled templates are listed.</template>
            </p>
          </div>
          <BaseToggle
            :model-value="!!config.enabled"
            :disabled="saving || config.hard_disabled"
            aria-label="Use the remote template registry"
            @update:model-value="toggle"
          />
        </div>

        <!-- URL -->
        <div>
          <label for="template-registry-url" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
            Registry URL
          </label>
          <div class="mt-1 flex flex-col gap-2 sm:flex-row">
            <BaseInput
              id="template-registry-url"
              v-model="urlDraft"
              type="url"
              spellcheck="false"
              :placeholder="config.default_url"
              :disabled="saving"
              class="flex-1 min-w-0"
              @keyup.enter="save"
            />
            <div class="flex items-start gap-2">
              <BaseButton
                :disabled="saving || !dirty"
                :loading="saving"
                loading-label="Saving…"
                @click="save"
              >
                Save
              </BaseButton>
              <BaseButton
                variant="secondary"
                :disabled="saving || config.source !== 'settings'"
                @click="reset"
              >
                Reset
              </BaseButton>
            </div>
          </div>
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
            HTTPS only, no embedded credentials, must resolve to a public address.
            Leave blank to use the default
            (<span class="font-mono break-all">{{ config.default_url }}</span>).
          </p>
        </div>

        <!-- Status. This exists because fail-open makes every registry failure
             invisible in the catalog by design — this panel is the only place
             an operator can see that their registry 404s. -->
        <div class="rounded-md border border-gray-200 dark:border-gray-700 px-4 py-3">
          <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
            <BaseBadge :variant="statusVariant">
              <!-- Shape as well as hue (principle 24). -->
              <span aria-hidden="true">{{ statusGlyph }}</span>
              {{ statusLabel }}
            </BaseBadge>
            <span class="text-xs text-gray-500 dark:text-gray-400 tabular-nums">
              {{ status.template_count }} template{{ status.template_count === 1 ? '' : 's' }}
            </span>
            <span
              v-if="status.last_fetch_at"
              class="text-xs text-gray-500 dark:text-gray-400"
              :title="status.last_fetch_at"
            >
              last read {{ fmt(status.last_fetch_at) }}
            </span>
            <span v-if="status.stale" class="text-xs text-status-warning-700 dark:text-status-warning-300">
              serving the last known good copy
            </span>
          </div>

          <p v-if="status.last_error_code" class="mt-2 text-sm text-gray-600 dark:text-gray-300">
            {{ errorExplanation }}
          </p>

          <details v-if="status.errors && status.errors.length" class="mt-2">
            <summary class="cursor-pointer text-xs text-gray-500 dark:text-gray-400 select-none">
              {{ status.errors.length }} problem{{ status.errors.length === 1 ? '' : 's' }} in the document
            </summary>
            <ul class="mt-1 space-y-0.5">
              <li
                v-for="(err, i) in status.errors"
                :key="i"
                class="text-xs font-mono text-gray-600 dark:text-gray-400 break-words"
              >
                {{ err }}
              </li>
            </ul>
          </details>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import api from '../../api'
import InlineError from '../InlineError.vue'
import LoadFailed from '../LoadFailed.vue'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import BaseToggle from '../base/BaseToggle.vue'

const EMPTY_STATUS = {
  last_fetch_at: null,
  last_status: 'never',
  last_error_code: null,
  template_count: 0,
  stale: false,
  errors: [],
}

const config = ref({
  source: 'default',
  url: '',
  default_url: '',
  effective_url: '',
  enabled: true,
  hard_disabled: false,
  suppressed_by_github_templates: false,
  status: { ...EMPTY_STATUS },
})
const urlDraft = ref('')
const loading = ref(false)
const saving = ref(false)
const loadError = ref('')
const saveError = ref('')
const saveErrorDetail = ref('')

const status = computed(() => config.value.status || EMPTY_STATUS)
const dirty = computed(() => urlDraft.value.trim() !== (config.value.url || '').trim())

// The fixed vocabulary the backend publishes, translated once here. The backend
// never sends prose — a hostile registry's response text must not reach this
// panel — so the explanation is ours, keyed by code.
const ERROR_COPY = {
  disabled: 'The registry is switched off, so nothing is fetched.',
  invalid_url: 'The configured URL is not a valid public HTTPS address. Fix it and save again.',
  unreachable: "Trinity couldn't connect to the registry host. Check the URL and this machine's outbound network access.",
  timeout: 'The registry took too long to respond. It will be retried automatically.',
  http_error: 'The registry URL returned an error response — most often a wrong path or a private file.',
  redirect: 'The registry URL redirects. Redirects are refused for safety; point this at the final URL directly.',
  too_large: 'The registry document is larger than Trinity will read. It is capped at 256 KiB.',
  encoding_refused: 'The registry host returned a compressed response. Trinity reads this document uncompressed so the size cap applies to what actually arrives — serve it without Content-Encoding.',
  parse_refused: 'The registry document was refused by the YAML parser — likely a syntax error, a duplicate key, or an anchor/alias (which are not allowed here).',
  unsupported_version: 'The registry declares a schema version this Trinity does not understand. Upgrade Trinity, or serve a version 1 document.',
  bad_shape: 'The document was read but is not a registry — it needs a top-level `templates:` list.',
}

const errorExplanation = computed(() => {
  const code = status.value.last_error_code
  if (!code) return ''
  const copy = ERROR_COPY[code] || 'The registry could not be read.'
  return status.value.stale
    ? `${copy} Trinity is still serving the last copy it read successfully.`
    : `${copy} Only your bundled templates are listed until this resolves.`
})

const statusLabel = computed(() => {
  if (status.value.last_status === 'ok') return 'Reachable'
  if (status.value.last_status === 'disabled') return 'Disabled'
  if (status.value.last_status === 'failed') return status.value.stale ? 'Stale' : 'Unreachable'
  return 'Not read yet'
})

const statusGlyph = computed(() => {
  if (status.value.last_status === 'ok') return '●'
  if (status.value.last_status === 'disabled') return '○'
  if (status.value.last_status === 'failed') return status.value.stale ? '◐' : '▲'
  return '○'
})

const statusVariant = computed(() => {
  if (status.value.last_status === 'ok') return 'success'
  if (status.value.last_status === 'failed') return status.value.stale ? 'warning' : 'danger'
  return 'neutral'
})

function fmt(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

function clearSaveError() {
  saveError.value = ''
  saveErrorDetail.value = ''
}

function apply(data) {
  config.value = { ...config.value, ...data }
  urlDraft.value = data.url || ''
}

function failureMessage(e, fallback) {
  const detail = e?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail?.error) return detail.error
  return fallback
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const { data } = await api.get('/api/settings/template-registry')
    apply(data)
  } catch (e) {
    loadError.value = failureMessage(e, 'The settings request failed. Retry, or check the backend logs.')
  } finally {
    loading.value = false
  }
}

async function save() {
  clearSaveError()
  saving.value = true
  try {
    const { data } = await api.put('/api/settings/template-registry', {
      url: urlDraft.value.trim(),
    })
    apply(data)
  } catch (e) {
    saveError.value = failureMessage(e, "Couldn't save the registry URL.")
    saveErrorDetail.value = e?.message || ''
  } finally {
    saving.value = false
  }
}

async function toggle() {
  clearSaveError()
  saving.value = true
  try {
    const { data } = await api.put('/api/settings/template-registry', {
      enabled: !config.value.enabled,
    })
    apply(data)
  } catch (e) {
    saveError.value = failureMessage(e, "Couldn't change the registry setting.")
    saveErrorDetail.value = e?.message || ''
  } finally {
    saving.value = false
  }
}

async function reset() {
  clearSaveError()
  saving.value = true
  try {
    await api.delete('/api/settings/template-registry')
    await load()
  } catch (e) {
    saveError.value = failureMessage(e, "Couldn't reset the registry to its default.")
    saveErrorDetail.value = e?.message || ''
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>
