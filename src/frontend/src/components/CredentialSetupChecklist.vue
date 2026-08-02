<script setup>
/**
 * Guided credential setup checklist (trinity-enterprise#127).
 *
 * What each credential is, whether it is actually set, and where to get it —
 * so an operator no longer has to already know what an agent wants and
 * hand-paste `KEY=VALUE` into Quick Inject.
 *
 * RENDERING CONTRACT (binding — the response model states the same rules):
 *
 *  - `title`, `description`, `source`, `default` and `errors[]` are
 *    author-controlled text that reaches an operator. They are interpolated as
 *    TEXT via `{{ }}` and deliberately NOT routed through `utils/markdown.js`.
 *    Markdown would be a WIDENING here: it hands the template author an
 *    arbitrary `[label](url)` surface immediately beside a credential input,
 *    which is precisely what having ONE validated `setup_url` exists to
 *    prevent. `v-html` stays banned (H-005).
 *
 *  - The anchor text for `setup_url` is ALWAYS the parsed host, never `title`.
 *    `<a href="https://evil.tld">OpenAI API keys</a>` recreates the userinfo
 *    attack in pure HTML with no validator in the way, and the backend rejects
 *    `user@host` precisely to stop label/destination divergence.
 *
 *  - `setup_url_display_host === null` means the host could not be verified
 *    (percent-encoded authority, malformed label, non-https): render the raw
 *    URL as INERT TEXT, never as a link. Failing open to the raw host would
 *    make a failed check indistinguishable from a passed one.
 *
 *  - `default` is a PLACEHOLDER and is suppressed entirely unless
 *    `secret === false`. Nothing enforces the schema's "NEVER put a real
 *    credential here", so an author — or a prompt-injected agent rewriting its
 *    own `template.yaml` — could set `default: "sk-attacker-controlled"`;
 *    prefilling it would turn author YAML into a one-click credential write,
 *    and `secret: true` would MASK the field, making it less likely the
 *    operator reads what they submit.
 *
 *  - `format` is an open vocabulary. It is shown as a chip and never mapped
 *    onto a DOM attribute.
 *
 * The component owns the input state and EMITS values; the parent
 * (`CredentialsPanel`) performs the write through the existing owner-gated
 * inject path. One writer, no new backend write surface.
 */
import { computed, ref, watch } from 'vue'

const props = defineProps({
  report: { type: Object, default: null },
  loading: { type: Boolean, default: false },
  error: { type: String, default: null },
  saving: { type: Boolean, default: false },
  saveResult: { type: Object, default: null }, // { success, message }
  // Inputs are gated on the agent running (the write path injects into a live
  // container); the CHECKLIST itself renders regardless — see the panel.
  canSubmit: { type: Boolean, default: false },
})

const emit = defineEmits(['submit', 'refresh'])

const values = ref({})
const revealed = ref({})

// A refreshed report must not silently keep half-typed values against rows that
// no longer exist, and must not clobber what the operator is typing either:
// keep only the entries whose row survived.
watch(
  () => props.report,
  (next) => {
    const names = new Set((next?.requirements || []).map((r) => r.name))
    const kept = {}
    for (const [k, v] of Object.entries(values.value)) {
      if (names.has(k)) kept[k] = v
    }
    values.value = kept
  }
)

const requirements = computed(() => props.report?.requirements || [])
const summary = computed(() => props.report?.summary || {})
const state = computed(() => props.report?.state || null)

const isDegraded = computed(() => state.value === 'degraded')
const isReady = computed(() => state.value === 'no_credentials_required')
const isIncomplete = computed(() => state.value === 'declaration_incomplete')

const blockingCount = computed(() => summary.value.blocking || 0)

/**
 * Ordered groups. Blocking first — that is the only group that answers "what
 * stops this agent working right now".
 */
const groups = computed(() => {
  const blocking = []
  const missing = []
  const setRows = []
  const unknown = []
  const advisory = []
  for (const row of requirements.value) {
    if (row.advisory) advisory.push(row)
    else if (row.status === 'unknown') unknown.push(row)
    else if (row.status === 'set') setRows.push(row)
    else if (row.required === true) blocking.push(row)
    else missing.push(row)
  }
  return [
    { key: 'blocking', title: 'Required — not set', rows: blocking, tone: 'danger' },
    { key: 'missing', title: 'Optional — not set', rows: missing, tone: 'warning' },
    { key: 'unknown', title: 'Status unknown', rows: unknown, tone: 'gray' },
    { key: 'set', title: 'Configured', rows: setRows, tone: 'success' },
    {
      key: 'advisory',
      title: 'Referenced but not declared',
      rows: advisory,
      tone: 'gray',
    },
  ].filter((g) => g.rows.length > 0)
})

const degradedMessage = computed(() => {
  switch (props.report?.degraded_reason) {
    case 'agent_not_running':
      return 'The agent is stopped, so Trinity cannot tell which credentials are set. Requirements below come from the template catalog.'
    case 'agent_unreachable':
      return "Trinity could not read this agent's workspace. Status is unknown."
    case 'template_unreadable':
      return "This agent's template.yaml could not be read. Requirements below may be incomplete."
    case 'no_template':
      return 'This agent has no template.yaml, so its credential requirements are unknown.'
    case 'template_label_missing':
      return 'This agent is not linked to a template, so its credential requirements are unknown.'
    case 'catalog_unavailable':
      return "The template this agent was created from could not be fetched, so its requirements could not be confirmed."
    default:
      return 'Credential requirements could not be confirmed.'
  }
})

/**
 * Only a verified https host is ever rendered as a link. Re-checked here rather
 * than trusted: the backend validates, and the UI must not become the second
 * authority that forgets.
 */
function isLinkable(row) {
  return Boolean(row.setup_url_display_host) && /^https:\/\//i.test(row.setup_url || '')
}

/** The registrable domain, emphasised inside the full host. */
function hostParts(row) {
  const host = row.setup_url_display_host || ''
  const registrable = row.setup_url_registrable || ''
  if (registrable && host.endsWith(registrable) && host.length > registrable.length) {
    return { prefix: host.slice(0, host.length - registrable.length), registrable }
  }
  return { prefix: '', registrable: host }
}

function placeholderFor(row) {
  // Suppressed unless the author marked the variable non-secret.
  if (row.secret === false && row.default) return row.default
  return row.status === 'set' ? 'Set — enter a new value to replace it' : ''
}

function toggleReveal(name) {
  revealed.value = { ...revealed.value, [name]: !revealed.value[name] }
}

const filled = computed(() =>
  Object.entries(values.value)
    .filter(([, v]) => typeof v === 'string' && v.trim() !== '')
    .map(([k]) => k)
)

function submit() {
  const payload = {}
  for (const name of filled.value) payload[name] = values.value[name]
  if (Object.keys(payload).length === 0) return
  emit('submit', payload)
}

/** Clear the inputs after a successful write; the parent refetches the report. */
watch(
  () => props.saveResult,
  (result) => {
    if (result && result.success) {
      values.value = {}
      revealed.value = {}
    }
  }
)
</script>

<template>
  <div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
    <div class="px-4 py-3 border-b border-gray-200 dark:border-gray-700 flex items-start justify-between gap-3">
      <div>
        <h3 class="text-lg font-medium text-gray-900 dark:text-white">Credential Setup</h3>
        <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">
          What this agent needs, what is already set, and where to get each one.
        </p>
      </div>
      <button
        class="shrink-0 text-xs font-medium text-action-primary-600 dark:text-action-primary-400 hover:underline disabled:opacity-50"
        :disabled="loading"
        @click="emit('refresh')"
      >{{ loading ? 'Checking…' : 'Refresh' }}</button>
    </div>

    <!-- Failure to load the report itself (not a degraded read). -->
    <p v-if="error" class="px-4 py-3 text-sm text-status-danger-600 dark:text-status-danger-400">
      {{ error }}
    </p>

    <template v-else-if="report">
      <!-- Headline. `degraded` dominates: a degraded lookup and a genuinely
           credential-free agent produce an identical empty list, and "Ready" is
           the one state nobody investigates. -->
      <div
        class="px-4 py-3 border-b border-gray-100 dark:border-gray-700 text-sm"
        :class="{
          'bg-status-danger-50 dark:bg-status-danger-900/30 text-status-danger-800 dark:text-status-danger-300': blockingCount > 0 && !isDegraded,
          'bg-status-warning-50 dark:bg-status-warning-900/30 text-status-warning-800 dark:text-status-warning-300': isDegraded || isIncomplete,
          'bg-status-success-50 dark:bg-status-success-900/20 text-status-success-800 dark:text-status-success-300': isReady && !isDegraded,
          'bg-gray-50 dark:bg-gray-800/60 text-gray-700 dark:text-gray-300': !isDegraded && !isIncomplete && !isReady && blockingCount === 0,
        }"
      >
        <template v-if="isDegraded">{{ degradedMessage }}</template>
        <template v-else-if="isIncomplete">
          This template hasn't declared its credentials. The variables below are
          referenced by its config files, so they are shown as a starting point —
          they are advisory, not a verified requirement list.
        </template>
        <template v-else-if="isReady">Ready — this agent needs no credentials.</template>
        <template v-else-if="blockingCount > 0">
          {{ blockingCount }} required {{ blockingCount === 1 ? 'credential is' : 'credentials are' }} not set.
        </template>
        <template v-else>All required credentials are set.</template>
      </div>

      <!-- Problems in the template's own declaration. Text-interpolated. -->
      <details v-if="report.errors && report.errors.length" class="px-4 py-2 border-b border-gray-100 dark:border-gray-700">
        <summary class="text-xs text-gray-500 dark:text-gray-400 cursor-pointer">
          Template declaration problems ({{ report.errors.length }})
        </summary>
        <ul class="mt-1 space-y-0.5">
          <li v-for="(msg, i) in report.errors" :key="i" class="text-xs text-gray-500 dark:text-gray-400 font-mono break-words">
            {{ msg }}
          </li>
        </ul>
      </details>

      <div v-for="group in groups" :key="group.key" class="border-b border-gray-100 dark:border-gray-700 last:border-0">
        <h4 class="px-4 pt-3 pb-1 text-xs font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">
          {{ group.title }} ({{ group.rows.length }})
        </h4>
        <ul>
          <li v-for="row in group.rows" :key="row.name" class="px-4 py-3 space-y-1.5">
            <div class="flex items-center gap-2 flex-wrap">
              <span class="font-mono text-sm text-gray-900 dark:text-white">{{ row.name }}</span>

              <span
                class="text-[10px] uppercase font-semibold px-1.5 py-0.5 rounded"
                :class="{
                  'bg-status-success-100 text-status-success-700 dark:bg-status-success-900/40 dark:text-status-success-300': row.status === 'set',
                  'bg-status-danger-100 text-status-danger-700 dark:bg-status-danger-900/40 dark:text-status-danger-300': row.status === 'missing' && row.required === true,
                  'bg-status-warning-100 text-status-warning-700 dark:bg-status-warning-900/40 dark:text-status-warning-300': row.status === 'missing' && row.required !== true,
                  'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300': row.status === 'unknown',
                }"
              >{{ row.status }}</span>

              <!-- Tri-state `required`. `unknown` is rendered as its own thing:
                   a bare `- FOO` carries no authorial intent, and showing it as
                   required cries wolf on every legacy template. -->
              <span v-if="row.required === true" class="text-[10px] uppercase font-semibold px-1.5 py-0.5 rounded bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300">required</span>
              <span v-else-if="row.required === false" class="text-[10px] uppercase font-semibold px-1.5 py-0.5 rounded bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300">optional</span>
              <span v-else class="text-[10px] uppercase font-semibold px-1.5 py-0.5 rounded bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400">not stated</span>

              <span v-if="row.format" class="text-[10px] uppercase font-semibold px-1.5 py-0.5 rounded bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400">{{ row.format }}</span>
              <span v-if="row.advisory" class="text-[10px] uppercase font-semibold px-1.5 py-0.5 rounded bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400">advisory</span>
            </div>

            <p v-if="row.title && row.title !== row.name" class="text-sm text-gray-800 dark:text-gray-200">{{ row.title }}</p>
            <p v-if="row.description" class="text-xs text-gray-500 dark:text-gray-400">{{ row.description }}</p>

            <!-- Where to get it. Anchor text is the PARSED HOST, never `title`. -->
            <p v-if="row.setup_url" class="text-xs">
              <template v-if="isLinkable(row)">
                <span class="text-gray-500 dark:text-gray-400">Get one at </span>
                <a
                  :href="row.setup_url"
                  :title="row.setup_url"
                  target="_blank"
                  rel="noopener noreferrer"
                  referrerpolicy="no-referrer"
                  class="font-mono text-action-primary-600 dark:text-action-primary-400 hover:underline"
                >
                  <span class="text-gray-500 dark:text-gray-400">{{ hostParts(row).prefix }}</span><span class="font-semibold">{{ hostParts(row).registrable }}</span>
                </a>
              </template>
              <template v-else>
                <span class="text-gray-500 dark:text-gray-400">Setup link (host could not be verified, so it is not clickable): </span>
                <span class="font-mono text-gray-600 dark:text-gray-300 break-all">{{ row.setup_url }}</span>
              </template>
            </p>

            <p v-if="row.source" class="text-[11px] text-gray-400 dark:text-gray-500 font-mono">{{ row.source }}</p>

            <div class="flex items-center gap-2">
              <input
                v-model="values[row.name]"
                :type="row.secret !== false && !revealed[row.name] ? 'password' : 'text'"
                :placeholder="placeholderFor(row)"
                :disabled="!canSubmit || saving"
                autocomplete="off"
                spellcheck="false"
                class="flex-1 min-w-0 px-3 py-2 text-sm font-mono rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-action-primary-500/40 focus:border-action-primary-500"
              />
              <button
                v-if="row.secret !== false"
                type="button"
                class="shrink-0 text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
                @click="toggleReveal(row.name)"
              >{{ revealed[row.name] ? 'Hide' : 'Show' }}</button>
            </div>
          </li>
        </ul>
      </div>

      <div v-if="requirements.length" class="px-4 py-3 bg-gray-50 dark:bg-gray-900/50 border-t border-gray-200 dark:border-gray-700">
        <p v-if="!canSubmit" class="text-xs text-gray-500 dark:text-gray-400 mb-2">
          Start the agent to save credentials.
        </p>
        <p
          v-if="saveResult"
          class="text-sm mb-2"
          :class="saveResult.success ? 'text-status-success-600 dark:text-status-success-400' : 'text-status-danger-600 dark:text-status-danger-400'"
        >{{ saveResult.message }}</p>
        <div class="flex items-center justify-between gap-3">
          <span class="text-xs text-gray-500 dark:text-gray-400">
            <template v-if="filled.length">{{ filled.length }} to save</template>
            <template v-else>Enter a value to save it into the agent's .env</template>
          </span>
          <button
            type="button"
            class="inline-flex items-center px-3 py-1.5 text-sm font-medium rounded-md text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:bg-gray-400 disabled:cursor-not-allowed"
            :disabled="!canSubmit || saving || filled.length === 0"
            @click="submit"
          >{{ saving ? 'Saving…' : 'Save credentials' }}</button>
        </div>
      </div>
    </template>

    <p v-else-if="loading" class="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
      Checking credential requirements…
    </p>
  </div>
</template>
