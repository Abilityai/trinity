<template>
  <!--
    #3792: a fixed-size two-pane picker. The panel's size never depends on what
    is selected: the header and footer are fixed, and each pane scrolls on its
    own, vertically only. Source-specific options (repository, import intent,
    agent-or-codebase, fork-to-own) open inside the right pane, below the
    control that revealed them, so nothing outside that pane moves.

    BaseModal owns the overlay, Esc, the focus trap and the scroll lock (z-50,
    teleported to <body>, so it outranks page-level overlay chrome, ent#261).
    Backdrop clicks do not close it: a stray click must not discard typing.
  -->
  <BaseModal
    :model-value="true"
    :close-on-backdrop="false"
    labelledby="create-agent-title"
    :panel-class="PANEL_CLASS"
    @close="$emit('close')"
  >
    <!-- trinity-enterprise#15: after a github-sourced create succeeds the body
         swaps to the post-create validation step, inside the same panel. -->
    <form v-if="!postCreate" class="flex flex-col h-full min-h-0" @submit.prevent="createAgent">
      <div class="flex items-center justify-between h-14 px-5 shrink-0 border-b border-gray-200 dark:border-gray-750">
        <h3 id="create-agent-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">New agent</h3>
        <BaseButton variant="ghost" size="sm" type="button" aria-label="Close" @click="$emit('close')">
          <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </BaseButton>
      </div>

      <div class="flex flex-col sm:flex-row flex-1 min-h-0">
        <!-- Left pane: every source in one list. Fixed width; scrolls itself. -->
        <div class="flex flex-col shrink-0 h-44 sm:h-auto sm:w-72 border-b sm:border-b-0 sm:border-r border-gray-200 dark:border-gray-750 bg-gray-50 dark:bg-gray-900">
          <div class="p-3 shrink-0">
            <BaseInput
              id="template-search"
              v-model="search"
              type="search"
              placeholder="Search templates"
              aria-label="Search templates"
              autocomplete="off"
            />
          </div>

          <div class="flex-1 min-h-0 overflow-y-auto overflow-x-hidden [scrollbar-gutter:stable] px-2 pb-3" data-testid="source-list">
            <p :class="OVERLINE_CLASS">Start</p>
            <button
              v-for="opt in startOptions"
              :key="opt.id"
              type="button"
              data-testid="source-option"
              :aria-pressed="form.template === opt.id ? 'true' : 'false'"
              :class="[OPTION_CLASS, form.template === opt.id ? OPTION_SELECTED_CLASS : OPTION_IDLE_CLASS]"
              @click="selectSource(opt.id)"
            >
              <span :class="[ICON_CLASS, ICON_NEUTRAL_CLASS]"><SourceIcon :kind="opt.icon" /></span>
              <span class="min-w-0 truncate text-sm font-medium">{{ opt.title }}</span>
            </button>

            <!-- Loading → failed → empty → ready share the list's footprint. -->
            <div v-if="listState.state === 'loading'" aria-busy="true" class="px-2 pt-4 space-y-3">
              <span class="sr-only">Loading templates</span>
              <div v-for="n in 4" :key="n" class="h-7 rounded-md bg-gray-200 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" />
            </div>
            <LoadFailed
              v-else-if="listState.state === 'failed'"
              dense
              title="Templates didn't load"
              message="Blank and GitHub repository still work."
              :detail="templatesError"
              @retry="fetchTemplates"
            />
            <template v-else>
              <template v-for="group in templateGroups" :key="group.key">
                <p v-if="group.items.length" :class="OVERLINE_CLASS">{{ group.title }}</p>
                <button
                  v-for="template in group.items"
                  :key="template.id"
                  type="button"
                  data-testid="source-option"
                  :aria-pressed="form.template === template.id ? 'true' : 'false'"
                  :title="template.display_name"
                  :class="[OPTION_CLASS, form.template === template.id ? OPTION_SELECTED_CLASS : OPTION_IDLE_CLASS]"
                  @click="selectSource(template.id)"
                >
                  <span :class="[ICON_CLASS, group.key === 'featured' ? ICON_ACCENT_CLASS : ICON_NEUTRAL_CLASS]">
                    <SourceIcon :kind="group.icon" />
                  </span>
                  <span class="min-w-0 truncate text-sm font-medium">{{ template.display_name }}</span>
                </button>
              </template>
              <p v-if="search && !matchCount" class="px-2 pt-4 text-sm text-gray-600 dark:text-gray-400">
                No templates match “{{ search }}”.
              </p>
            </template>
          </div>
        </div>

        <!-- Right pane: the selected source, the name, and its options. -->
        <div class="flex-1 min-w-0 min-h-0 overflow-y-auto overflow-x-hidden [scrollbar-gutter:stable] px-6 py-5 space-y-5" data-testid="source-detail">
          <div class="flex items-start gap-3">
            <span :class="['w-10 h-10 rounded-lg', ICON_BOX_CLASS, isForkToOwn ? ICON_ACCENT_CLASS : ICON_NEUTRAL_CLASS]">
              <SourceIcon :kind="detail.icon" />
            </span>
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-center gap-2">
                <h4 class="text-base font-semibold text-gray-900 dark:text-gray-100 break-words">{{ detail.title }}</h4>
                <BaseBadge v-if="detail.badge" size="sm" :variant="isForkToOwn ? 'primary' : 'neutral'">{{ detail.badge }}</BaseBadge>
              </div>
              <!-- Always two lines tall, so the Name field sits at one height for every source. -->
              <p class="mt-1 h-10 text-sm leading-5 text-gray-600 dark:text-gray-400 line-clamp-2 break-words" :title="detail.description">{{ detail.description }}</p>
                          </div>
          </div>

          <!-- One name. The ID row is always present (preview, override or the
               named error), so typing never changes this block's height. -->
          <div class="min-h-[6.5rem]">
            <BaseInput
              id="agent-name"
              v-model="form.name"
              label="Name"
              required
              placeholder="e.g. Marketing Assistant"
              autocomplete="off"
              :error="nameError"
              :aria-describedby="sanitizedName ? 'agent-name-preview' : undefined"
            />
            <div v-if="!nameError" class="mt-1.5 flex items-center gap-2 min-h-[2rem] text-xs text-gray-600 dark:text-gray-400">
              <template v-if="editingId">
                <label for="agent-id" class="shrink-0">ID</label>
                <input
                  id="agent-id"
                  ref="idInput"
                  v-model="idOverride"
                  type="text"
                  autocomplete="off"
                  class="flex-1 min-w-0 h-7 px-2 rounded-md font-mono text-xs border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-action-primary-500/40 focus:border-action-primary-500"
                />
                <button type="button" class="shrink-0 font-medium text-action-primary-700 dark:text-action-primary-300 hover:underline" @click="resetId">Use name</button>
              </template>
              <template v-else>
                <span class="shrink-0">ID</span>
                <p v-if="sanitizedName" id="agent-name-preview" aria-live="polite" class="min-w-0 truncate">
                  <code class="font-mono px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-750 text-gray-800 dark:text-gray-200">{{ sanitizedName }}</code>
                  <span class="ml-1.5">permanent, used in URLs and API keys</span>
                </p>
                <span v-else class="min-w-0 truncate">Created from the name</span>
                <button v-if="sanitizedName" type="button" class="ml-auto shrink-0 font-medium text-action-primary-700 dark:text-action-primary-300 hover:underline" @click="startEditId">Edit</button>
              </template>
            </div>
          </div>

          <div>
            <p class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Runtime</p>
            <div class="flex items-center gap-2 h-10 px-3 rounded-md border border-gray-200 dark:border-gray-750 text-sm text-gray-700 dark:text-gray-300" data-testid="runtime-row">
              <svg class="w-3.5 h-3.5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V8a4 4 0 018 0v3" /></svg>
              <span class="truncate">{{ runtimeLabel }}</span>
            </div>
          </div>

          <!-- github-custom: the repository, then how to import it. -->
          <div v-if="form.template === 'github-custom'">
            <BaseInput
              id="github-repo"
              v-model="githubRepoUrl"
              label="Repository"
              placeholder="owner/repo or https://github.com/owner/repo"
              autocomplete="off"
              spellcheck="false"
            />
            <!-- trinity-enterprise#15: clone/copy/fork intent selector -->
            <ImportIntentPicker v-model="importIntent" />
          </div>

          <!-- trinity-enterprise#704: agent vs deployment, on the paths that bind git -->
          <AgentKindPicker v-if="showKindPicker" v-model="agentKind" />

          <!-- Fork-to-own fields (trinity-enterprise#93; also shown for the
               github-custom 'fork' intent, trinity-enterprise#15) -->
          <div v-if="showForkFields" class="p-4 rounded-lg border border-gray-200 dark:border-gray-750 space-y-4">
            <p class="text-xs text-gray-600 dark:text-gray-400">
              This is copied into a repository <span class="font-medium">you own</span>. Your agent's
              knowledge lives there, and template updates stay one <code>git pull upstream</code> away.
            </p>
            <BaseInput
              id="fork-destination"
              v-model="forkDestination"
              label="Your repository"
              placeholder="your-github-username/my-agent-brain"
              help="It is created for you if it doesn't exist. If you create it yourself, leave it empty, with no README."
              autocomplete="off"
            />
            <SavedGithubTokenField v-model="forkPat" v-model:overriding="forkPatOverriding" :has-saved="hasSavedGithubPat" input-id="fork-github-token">
              <p class="mt-1 text-xs text-gray-600 dark:text-gray-400">
                Prefer a fine-grained token scoped to this repository (Administration and Contents write).
                A classic token with <code>repo</code> scope reaches all your repositories, and the agent can read
                its own git credential. Stored encrypted as this agent's git identity.
              </p>
            </SavedGithubTokenField>
            <fieldset>
              <legend class="text-sm font-medium text-gray-700 dark:text-gray-300">Visibility</legend>
              <div class="mt-1 flex gap-4">
                <label class="inline-flex items-center text-sm text-gray-700 dark:text-gray-300">
                  <input v-model="forkPrivate" type="radio" :value="true" class="text-action-primary-600 focus:ring-action-primary-500" />
                  <span class="ml-1.5">Private (recommended)</span>
                </label>
                <label class="inline-flex items-center text-sm text-gray-700 dark:text-gray-300">
                  <input v-model="forkPrivate" type="radio" :value="false" class="text-action-primary-600 focus:ring-action-primary-500" />
                  <span class="ml-1.5">Public</span>
                </label>
              </div>
              <p v-if="!forkPrivate" class="mt-2 p-2 rounded-md text-xs font-medium bg-status-danger-100 text-status-danger-700 dark:bg-status-danger-500/16 dark:text-status-danger-300">
                Everything this agent captures, including notes and possibly personal information, will be public on GitHub. Choose Private unless you are certain.
              </p>
            </fieldset>
          </div>
        </div>
      </div>

      <div class="flex items-center gap-3 h-16 px-5 shrink-0 border-t border-gray-200 dark:border-gray-750 bg-gray-50 dark:bg-gray-900">
        <p
          v-if="error"
          role="alert"
          :title="error"
          class="flex-1 min-w-0 text-sm text-status-danger-700 dark:text-status-danger-400 line-clamp-2"
        >{{ error }}</p>
        <span v-else class="flex-1" />
        <BaseButton variant="secondary" type="button" @click="$emit('close')">Cancel</BaseButton>
        <BaseButton
          type="submit"
          :disabled="!sanitizedName"
          :loading="loading"
          :loading-label="showForkFields ? 'Creating your repository…' : 'Creating…'"
        >
          Create agent
        </BaseButton>
      </div>
    </form>
    <div v-else class="h-full overflow-y-auto">
      <ImportValidationStep
        :agent-name="postCreate.name"
        :import-snapshot="postCreate.snapshot"
        :git-mode="postCreate.gitMode"
        @close="$emit('close')"
      />
    </div>
  </BaseModal>
</template>

<script setup>
import { ref, reactive, onMounted, computed, watch, nextTick, h } from 'vue'
import { useAgentsStore } from '../stores/agents'
import api from '../api'
import ImportIntentPicker from './ImportIntentPicker.vue'
import AgentKindPicker from './AgentKindPicker.vue'
import ImportValidationStep from './ImportValidationStep.vue'
import SavedGithubTokenField from './SavedGithubTokenField.vue'
import LoadFailed from './LoadFailed.vue'
import { useAuthStore } from '../stores/auth'
import BaseModal from './base/BaseModal.vue'
import BaseInput from './base/BaseInput.vue'
import BaseButton from './base/BaseButton.vue'
import BaseBadge from './base/BaseBadge.vue'
import { viewState } from '../utils/loadingState'

// #3792: the panel's footprint. A fixed height (clamped to the viewport) is
// what keeps every selection change inside the panes.
const PANEL_CLASS = 'relative flex flex-col w-full max-w-[880px] h-[min(600px,calc(100dvh-2rem))] overflow-hidden rounded-xl bg-white dark:bg-gray-800 shadow-xl text-left'
const OVERLINE_CLASS = 'px-2 pt-3 pb-1 font-mono text-[11px] uppercase tracking-wider text-gray-500 dark:text-gray-400'
const OPTION_CLASS = 'flex items-center gap-2.5 w-full p-2 rounded-md text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40'
const OPTION_IDLE_CLASS = 'text-gray-900 dark:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-750'
const OPTION_SELECTED_CLASS = 'text-gray-900 dark:text-gray-100 bg-action-primary-100 dark:bg-action-primary-500/16 ring-1 ring-inset ring-action-primary-500'
const ICON_BOX_CLASS = 'flex items-center justify-center shrink-0'
const ICON_CLASS = `w-7 h-7 rounded-md ${ICON_BOX_CLASS}`
const ICON_NEUTRAL_CLASS = 'bg-gray-200 text-gray-600 dark:bg-gray-750 dark:text-gray-300'
const ICON_ACCENT_CLASS = 'bg-action-primary-600 text-white'

const ICON_PATHS = {
  plus: 'M12 5v14M5 12h14',
  repo: 'M6 3v12M6 15a3 3 0 103 3M6 15a3 3 0 013 3m0 0h3a6 6 0 006-6V9M18 9a3 3 0 100-6 3 3 0 000 6z',
  bulb: 'M9 18h6M10 21h4M12 3a6 6 0 00-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0012 3z',
  doc: 'M9 12h6M9 16h6M7 3h6l5 5v11a2 2 0 01-2 2H7a2 2 0 01-2-2V5a2 2 0 012-2z',
}
// Stroke icons, text-coloured; decorative (the label sits beside each).
const SourceIcon = (props) => h('svg', {
  class: 'w-4 h-4', fill: 'none', viewBox: '0 0 24 24', stroke: 'currentColor',
  'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true',
}, [h('path', { d: ICON_PATHS[props.kind] || ICON_PATHS.doc })])
SourceIcon.props = ['kind']

const props = defineProps({
  initialTemplate: {
    type: String,
    default: ''
  }
})

const emit = defineEmits(['close', 'created'])
const agentsStore = useAgentsStore()

const form = reactive({
  name: '',
  template: props.initialTemplate || ''
})

// #3792: one Name field. The typed name is the display name; the permanent ID
// is derived from it unless the user overrides it. `idOverride` is null while
// the ID follows the name.
const editingId = ref(false)
const idOverride = ref('')
const idInput = ref(null)
const rawId = computed(() => (editingId.value ? idOverride.value : form.name))

// Keep the preview in sync with utils/helpers.py::sanitize_agent_name.
// The API still receives the original input and owns authoritative sanitization.
const sanitize = (value) => value
  .replace(/[^a-zA-Z0-9_.-]/g, '-')
  .replace(/^[^a-zA-Z0-9]+/, '')
  .replace(/-+/g, '-')
  .replace(/-+$/, '')
  .toLowerCase()
const sanitizedName = computed(() => sanitize(rawId.value))
const nameSubmitted = ref(false)
const nameError = computed(() => !sanitizedName.value && (rawId.value || nameSubmitted.value)
  ? (editingId.value
    ? 'Enter an ID with at least one ASCII letter or number (a-z, 0-9), for example my-agent.'
    : 'Enter a name with at least one ASCII letter or number (a-z, 0-9), for example my-agent.')
  : '')

const startEditId = async () => {
  idOverride.value = sanitizedName.value
  editingId.value = true
  await nextTick()
  idInput.value?.focus()
}
const resetId = () => {
  editingId.value = false
  idOverride.value = ''
}

// Mirrors DISPLAY_LABEL_MAX_LEN in src/backend/models.py.
const DISPLAY_LABEL_MAX_LEN = 120

const githubRepoUrl = ref('')
const search = ref('')

// trinity-enterprise#15: import intent for the github-custom path.
// 'clone' (default) | 'copy' | 'fork'. Featured templates never send it.
const importIntent = ref('clone')

// trinity-enterprise#704: 'agent' (default) | 'deployment'. Asked only where the
// create binds git: a custom repo CLONE. Copy has no git link; Fork is an agent
// in the user's own repo by definition; featured fork-to-own templates are the
// same case.
const agentKind = ref('agent')

// trinity-enterprise#15: post-create validation context. Non-null swaps the
// modal body to ImportValidationStep ({name, snapshot}) — set only for
// github-sourced creates; other creates keep the immediate close.
const postCreate = ref(null)

// Fork-to-own inputs (trinity-enterprise#93)
const forkDestination = ref('')
const forkPat = ref('')
// #3164: the user's saved personal GitHub token (presence only) stands in for
// a typed one; the field is then an optional override.
const hasSavedGithubPat = ref(false)
// #3164 review: an override submitted empty is an empty token, not the saved one.
const forkPatOverriding = ref(false)
const forkPrivate = ref(true)

// Watch for initialTemplate changes (in case modal is reused)
watch(() => props.initialTemplate, (newVal) => {
  form.template = newVal || ''
})

const selectSource = async (id) => {
  form.template = id
  if (id === 'github-custom') {
    await nextTick()
    document.getElementById('github-repo')?.focus()
  }
}

const templates = ref([])
const loading = ref(false)
const error = ref('')
const templatesLoaded = ref(false)
const templatesError = ref('')
const listState = computed(() => viewState({ hasLoaded: templatesLoaded.value, error: templatesError.value }))

const startOptions = [
  { id: '', title: 'Blank agent', icon: 'plus' },
  { id: 'github-custom', title: 'Import a GitHub repository', icon: 'repo' },
]

// Computed properties to separate GitHub and local templates.
// Fork-to-own templates (trinity-enterprise#93) get the featured section and
// are excluded from the plain GitHub list.
// Only 'required' is load-bearing in v1 — the backend enforces exactly that
// value, so the UI features (and demands destination+PAT for) the same set.
const featuredTemplates = computed(() => {
  return templates.value.filter(t => t.fork_to_own === 'required')
})

const githubTemplates = computed(() => {
  return templates.value.filter(t => t.source === 'github' && t.fork_to_own !== 'required')
})

const localTemplates = computed(() => {
  return templates.value.filter(t => t.source === 'local' || !t.source)
})

const matches = (t) => {
  const q = search.value.trim().toLowerCase()
  if (!q) return true
  return [t.display_name, t.description, t.tagline, t.github_repo]
    .some(v => typeof v === 'string' && v.toLowerCase().includes(q))
}
const templateGroups = computed(() => [
  { key: 'featured', title: 'Featured', icon: 'bulb', items: featuredTemplates.value.filter(matches) },
  { key: 'local', title: 'Local templates', icon: 'doc', items: localTemplates.value.filter(matches) },
  { key: 'github', title: 'GitHub templates', icon: 'repo', items: githubTemplates.value.filter(matches) },
])
const matchCount = computed(() => templateGroups.value.reduce((n, g) => n + g.items.length, 0))

const isForkToOwn = computed(() => selectedTemplate.value?.fork_to_own === 'required')

// trinity-enterprise#15: the destination/PAT/visibility fieldset shows for
// featured fork-to-own templates AND for the github-custom 'fork' intent.
const isGithubCustomFork = computed(
  () => form.template === 'github-custom' && importIntent.value === 'fork'
)
const showForkFields = computed(() => isForkToOwn.value || isGithubCustomFork.value)

// Only a custom-repo clone can come out as an agent with its own branch: every
// list template is a catalog entry, which the backend always makes pull-only
// (`_apply_agent_kind_default`, catalog_template) — PR #3022 review.
const showKindPicker = computed(
  () => form.template === 'github-custom' && importIntent.value === 'clone'
)

const selectedTemplate = computed(() => {
  if (!form.template) return null
  return templates.value.find(t => t.id === form.template)
})

// The right pane's header for whatever is selected.
const detail = computed(() => {
  if (form.template === '') {
    return { icon: 'plus', title: 'Blank agent', description: 'An empty agent. Add skills, credentials and schedules after it is created.' }
  }
  if (form.template === 'github-custom') {
    return { icon: 'repo', title: 'Import a GitHub repository', description: 'Create the agent from any repository you can read.' }
  }
  const t = selectedTemplate.value
  if (!t) return { icon: 'doc', title: 'Template', description: '' }
  if (isForkToOwn.value) {
    return { icon: 'bulb', title: t.display_name, badge: 'Your own copy', description: t.tagline || firstLine(t.description) }
  }
  if (t.source === 'github') {
    return { icon: 'repo', title: t.display_name, badge: 'GitHub template', description: firstLine(t.description), repo: t.github_repo }
  }
  return { icon: 'doc', title: t.display_name, badge: 'Local template', description: firstLine(t.description) }
})

// Runtime selection is trinity-enterprise#603; until then this row is
// read-only. The catalog does not report a template's runtime, so a template
// row says who decides rather than guessing.
const runtimeLabel = computed(() => {
  if (form.template === '') return 'Claude Code'
  const source = form.template === 'github-custom' ? 'repository' : 'template'
  return `Set by the ${source} (Claude Code unless it declares another)`
})

const firstLine = (description) => (description ? description.split('\n')[0] : '')

// Parse GitHub repo from various input formats into owner/repo
const parseGithubRepo = (input) => {
  if (!input) return null
  let repo = input.trim()
  // Handle full URLs: https://github.com/owner/repo(.git)
  const urlMatch = repo.match(/github\.com\/([^/]+\/[^/\s#?.]+)/)
  if (urlMatch) {
    repo = urlMatch[1].replace(/\.git$/, '')
  }
  // Validate owner/repo format
  if (/^[a-zA-Z0-9._-]+\/[a-zA-Z0-9._-]+$/.test(repo)) {
    return repo
  }
  return null
}

// trinity-enterprise#964: the name the picked source already carries — a
// repository's own name without the owner, or the agent name a GitHub
// template declares (its template.yaml `name:`, else its repo name). Blank and
// local templates carry none. A candidate that sanitizes to nothing is none,
// so the field stays empty and submit shows the usual error.
const repoName = (repo) => (repo ? repo.split('/').pop() : '')
const sourceName = computed(() => {
  let candidate = ''
  if (form.template === 'github-custom') {
    candidate = repoName(parseGithubRepo(githubRepoUrl.value))
  } else if (selectedTemplate.value?.source === 'github') {
    candidate = selectedTemplate.value.agent_name || repoName(selectedTemplate.value.github_repo)
  }
  return sanitize(candidate) ? candidate : ''
})
// Prefill never clobbers the person: Name takes the source's name only while
// it is empty or still the previous prefill. The ID override is never touched.
let lastPrefill = ''
watch(sourceName, (next) => {
  if (form.name === '' || form.name === lastPrefill) form.name = next
  lastPrefill = next
})

const fetchTemplates = async () => {
  templatesError.value = ''
  try {
    // Invariant #7 — shared api client (was the last raw-axios /api/templates
    // consumer alongside the Library page, migrated together in ent#263).
    const response = await api.get('/api/templates')
    templates.value = response.data
    templatesLoaded.value = true
    // If a caller injected an initialTemplate that doesn't exist in this
    // deploy (e.g. the onboarding wizard prefilling `local:scout` on an
    // install without that template), fall back to the blank agent so the
    // form never points at a missing template.
    if (
      form.template &&
      form.template !== 'github-custom' &&
      !templates.value.some(t => t.id === form.template)
    ) {
      form.template = ''
    }
    // A preselected template (Library → Use Template) may sit below the fold.
    await nextTick()
    document.querySelector('[data-testid="source-option"][aria-pressed="true"]')?.scrollIntoView?.({ block: 'nearest' })
  } catch (err) {
    templatesError.value = err?.message || 'Failed to load templates'
  }
}

const createAgent = async () => {
  nameSubmitted.value = true
  if (!sanitizedName.value) return

  loading.value = true
  error.value = ''

  try {
    // The raw ID goes to the API, which owns authoritative sanitization (#3463).
    const payload = {
      name: rawId.value
    }
    // #3792: the typed name is the display name (ent#1640) whenever it reads
    // differently from the ID. Over the backend's cap the agent renders under
    // its ID instead of failing the create.
    const label = (form.name || '').trim()
    if (label && label !== sanitizedName.value && label.length <= DISPLAY_LABEL_MAX_LEN) {
      payload.display_label = label
    }

    if (form.template === 'github-custom') {
      const repo = parseGithubRepo(githubRepoUrl.value)
      if (!repo) {
        error.value = 'Enter a GitHub repository as owner/repo, for example acme/my-agent.'
        loading.value = false
        return
      }
      payload.template = `github:${repo}`
      // trinity-enterprise#15: explicit import intent. Only the github-custom
      // path sends it — featured fork-to-own templates are unchanged (legacy).
      payload.import_intent = importIntent.value
    } else if (form.template) {
      payload.template = form.template
    }

    // trinity-enterprise#704: the answer to "what is this repository?"
    if (showKindPicker.value) payload.kind = agentKind.value

    // Fork-to-own (trinity-enterprise#93): destination + token are required
    // for templates that declare it — and for the github-custom 'fork' intent
    // (trinity-enterprise#15). Shape check only — the backend owns the
    // authoritative validation.
    if (showForkFields.value) {
      const dest = forkDestination.value.trim()
      if (!/^[A-Za-z0-9][A-Za-z0-9-]*\/[A-Za-z0-9._-]+$/.test(dest)) {
        error.value = 'Enter your repository as owner/name (e.g., your-username/my-agent-brain)'
        loading.value = false
        return
      }
      const typedPat = forkPat.value.trim()
      if (!typedPat && (!hasSavedGithubPat.value || forkPatOverriding.value)) {
        error.value = 'A GitHub token is required to create the repository in your account — enter one, or save your personal token in Settings → MCP Keys → Personal GitHub Token'
        loading.value = false
        return
      }
      // No token in the payload → the backend uses the saved personal one.
      payload.fork_to_own = {
        destination_repo: dest,
        ...(typedPat ? { github_pat: typedPat } : {}),
        private: forkPrivate.value
      }
    }

    const agent = await agentsStore.createAgent(payload)
    forkPat.value = ''  // hygiene: don't keep the token in the reactive ref
    // `created` fires immediately, exactly as before — Dashboard/Library/
    // first-run overlay consumers keep working (the overlay's agent step
    // unmounts us on `created`, so the validation step never renders there).
    emit('created', agent)
    // trinity-enterprise#15: github-sourced creates (github-custom any intent,
    // or a GitHub template from the list, fork-to-own included) swap to the
    // post-create validation step instead of auto-closing. Skippable — Close
    // is always available there. trinity-enterprise#704: a list template is
    // the path most likely to come back pull-only, so it must reach the notice.
    const githubSourced = form.template === 'github-custom' || selectedTemplate.value?.source === 'github'
    if (githubSourced) {
      postCreate.value = {
        name: agent?.name || sanitizedName.value,
        snapshot: agent?.import_snapshot || null,
        gitMode: agent?.git_mode || null,  // trinity-enterprise#704
      }
    } else {
      emit('close')
    }
  } catch (err) {
    const detail = err.response?.data?.detail
    if (detail && typeof detail === 'object' && (detail.code === 'QUOTA_EXCEEDED' || detail.error)) {
      // Structured errors (quota, FORK_* codes) carry a user-facing message.
      error.value = `${detail.error}`
    } else if (typeof detail === 'string') {
      error.value = detail
    } else if (Array.isArray(detail)) {
      // FastAPI 422 validation errors
      error.value = detail[0]?.msg || 'Invalid input'
    } else {
      error.value = 'Failed to create agent'
    }
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  fetchTemplates()
  useAuthStore().fetchGithubPatStatus().then((saved) => { hasSavedGithubPat.value = saved })
  // BaseModal focuses the first tabbable control (the Close button); the name
  // is where the user starts, so take focus once the shell has settled.
  setTimeout(() => document.getElementById('agent-name')?.focus(), 0)
})
</script>
