<template>
  <div>
    <!-- Result view --------------------------------------------------------- -->
    <div v-if="store.deployResult">
      <DeployResult
        :result="store.deployResult"
        @view-fleet="goToFleet"
        @install-another="startOver"
      />
    </div>

    <!-- Outcome-unknown view ------------------------------------------------
         A timeout does NOT cancel the server: deploy is synchronous and serial,
         so agents may still be being created. Offering "retry" here would create
         duplicates, so it deliberately is not offered. -->
    <div v-else-if="store.outcomeUnknown">
      <div class="rounded-lg border-2 border-status-warning-400 dark:border-status-warning-600 bg-status-warning-50 dark:bg-status-warning-900/20 p-4">
        <h3 class="font-semibold text-status-warning-900 dark:text-status-warning-100 flex items-center gap-2">
          <span aria-hidden="true">❓</span> Outcome unknown — deployment may still be running
        </h3>
        <p class="mt-2 text-sm text-status-warning-800 dark:text-status-warning-200">
          {{ store.outcomeUnknown }}
        </p>
        <p class="mt-2 text-sm text-status-warning-800 dark:text-status-warning-200">
          <strong>Do not simply try again.</strong> Check your agent list first — deploying the
          same manifest a second time creates a duplicate, suffixed copy of every agent that
          did get created.
        </p>
        <div class="mt-3 flex flex-wrap gap-2">
          <button
            class="rounded-lg bg-action-primary-600 px-4 py-2 text-sm font-medium text-white hover:bg-action-primary-700"
            @click="goToAgents"
          >
            Check the agent list
          </button>
          <button
            class="rounded-lg border border-gray-300 dark:border-gray-600 px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700"
            @click="startOver"
          >
            Start over
          </button>
        </div>
      </div>
    </div>

    <!-- Install view -------------------------------------------------------- -->
    <div v-else class="space-y-6">
      <!-- Source picker -->
      <div>
        <div class="flex flex-wrap gap-2 border-b border-gray-200 dark:border-gray-700 pb-2">
          <button
            v-for="s in SOURCES"
            :key="s.id"
            :class="[
              'rounded-lg px-3 py-1.5 text-sm font-medium transition-colors',
              source === s.id
                ? 'bg-action-primary-600 text-white'
                : 'text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700'
            ]"
            :data-testid="`source-${s.id}`"
            @click="source = s.id"
          >
            {{ s.label }}
          </button>
        </div>

        <!-- Pick from bundled -->
        <div v-if="source === 'bundled'" class="mt-4">
          <div v-if="store.bundledLoading" class="text-sm text-gray-500 dark:text-gray-400">
            Loading bundled systems…
          </div>
          <div
            v-else-if="!store.bundled.length"
            class="rounded-lg border border-dashed border-gray-300 dark:border-gray-600 p-6 text-center"
          >
            <p class="text-sm text-gray-600 dark:text-gray-400">
              No bundled system manifests are available on this instance.
            </p>
            <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
              You can still paste or upload a manifest.
            </p>
          </div>
          <ul v-else class="grid gap-3 sm:grid-cols-2">
            <li
              v-for="m in store.bundled"
              :key="m.id"
              class="rounded-lg border p-4 transition-colors"
              :class="selectedId === m.id
                ? 'border-action-primary-500 bg-action-primary-50 dark:bg-action-primary-900/20'
                : 'border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800'"
              :data-testid="`bundled-card-${m.id}`"
            >
              <div class="flex items-start justify-between gap-2">
                <h4 class="font-semibold text-gray-900 dark:text-white">
                  {{ m.name || m.filename }}
                </h4>
                <span
                  v-if="!m.valid"
                  class="shrink-0 rounded-full bg-status-danger-100 dark:bg-status-danger-900/40 px-2 py-0.5 text-xs font-medium text-status-danger-800 dark:text-status-danger-200"
                >
                  cannot deploy
                </span>
                <span
                  v-else-if="m.already_deployed"
                  class="shrink-0 rounded-full bg-status-warning-100 dark:bg-status-warning-900/40 px-2 py-0.5 text-xs font-medium text-status-warning-800 dark:text-status-warning-200"
                >
                  already installed
                </span>
              </div>
              <!-- Plain text, never v-html (H-005). -->
              <p v-if="m.description" class="mt-1 text-sm text-gray-600 dark:text-gray-400">
                {{ m.description }}
              </p>
              <p v-if="!m.valid && m.reason" class="mt-1 text-xs text-status-danger-700 dark:text-status-danger-300">
                {{ m.reason }}
              </p>
              <div class="mt-2 flex flex-wrap gap-1.5 text-xs">
                <span class="rounded bg-gray-100 dark:bg-gray-700 px-2 py-0.5 text-gray-700 dark:text-gray-300">
                  {{ m.agent_count }} agent(s)
                </span>
                <span
                  v-if="m.schedule_count"
                  class="rounded bg-status-warning-100 dark:bg-status-warning-900/40 px-2 py-0.5 text-status-warning-800 dark:text-status-warning-200"
                >
                  {{ m.schedule_count }} schedule(s)
                </span>
                <span
                  v-if="m.sets_prompt"
                  class="rounded bg-status-warning-100 dark:bg-status-warning-900/40 px-2 py-0.5 text-status-warning-800 dark:text-status-warning-200"
                >
                  replaces global prompt
                </span>
                <span
                  v-if="m.permissions_preset"
                  class="rounded bg-gray-100 dark:bg-gray-700 px-2 py-0.5 text-gray-700 dark:text-gray-300"
                >
                  {{ m.permissions_preset }}
                </span>
              </div>
              <button
                class="mt-3 text-sm font-medium text-action-primary-600 dark:text-action-primary-400 hover:text-action-primary-800 dark:hover:text-action-primary-300"
                :data-testid="`bundled-load-${m.id}`"
                @click="pickBundled(m)"
              >
                {{ selectedId === m.id ? 'Loaded below' : 'Load this manifest' }}
              </button>
            </li>
          </ul>
        </div>

        <!-- Upload -->
        <div v-else-if="source === 'upload'" class="mt-4">
          <label
            class="flex cursor-pointer flex-col items-center rounded-lg border-2 border-dashed border-gray-300 dark:border-gray-600 p-6 text-center hover:border-action-primary-400"
          >
            <span class="text-sm font-medium text-gray-700 dark:text-gray-200">
              Choose a .yaml or .yml manifest
            </span>
            <span class="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Read in your browser — the file itself is never uploaded
            </span>
            <input
              type="file"
              accept=".yaml,.yml,application/x-yaml,text/yaml"
              class="hidden"
              data-testid="manifest-file"
              @change="onFile"
            />
          </label>
          <p v-if="uploadError" class="mt-2 text-sm text-status-danger-600 dark:text-status-danger-400">
            {{ uploadError }}
          </p>
          <p v-else-if="uploadName" class="mt-2 text-sm text-gray-600 dark:text-gray-400">
            Loaded <span class="font-mono">{{ uploadName }}</span>
          </p>
        </div>
      </div>

      <!-- Editor -->
      <div>
        <div class="flex items-baseline justify-between">
          <label for="manifest-yaml" class="text-sm font-medium text-gray-700 dark:text-gray-200">
            Manifest YAML
          </label>
          <span v-if="store.manifestText" class="text-xs text-gray-500 dark:text-gray-400">
            {{ store.manifestText.length }} characters
          </span>
        </div>
        <BaseTextarea
          id="manifest-yaml"
          mono
          :model-value="store.manifestText"
          rows="16"
          spellcheck="false"
          placeholder="name: my-system&#10;agents:&#10;  worker:&#10;    template: local:default"
          class="mt-1"
          data-testid="manifest-textarea"
          @update:model-value="store.setManifestText($event)"
        />
      </div>

      <!-- Error -->
      <div
        v-if="store.error"
        class="rounded-lg border border-status-danger-300 dark:border-status-danger-700 bg-status-danger-50 dark:bg-status-danger-900/20 p-3"
        data-testid="install-error"
      >
        <!-- Plain text: the backend's own named message (AC #4), never a raw blob. -->
        <p class="text-sm text-status-danger-800 dark:text-status-danger-200">{{ store.error }}</p>
      </div>

      <!-- Actions -->
      <div class="flex flex-wrap items-center gap-3">
        <button
          :disabled="!store.manifestText.trim() || store.isLoading"
          class="rounded-lg bg-action-primary-600 px-4 py-2 text-sm font-medium text-white hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
          data-testid="dry-run"
          @click="store.dryRun()"
        >
          {{ store.isLoading ? 'Checking…' : 'Preview' }}
        </button>
        <button
          :disabled="!canDeployNow"
          class="rounded-lg bg-status-success-600 px-4 py-2 text-sm font-medium text-white hover:bg-status-success-700 disabled:opacity-50 disabled:cursor-not-allowed"
          data-testid="deploy"
          @click="store.deploy()"
        >
          {{ store.isDeploying ? 'Deploying…' : 'Deploy' }}
        </button>
        <p class="text-xs text-gray-500 dark:text-gray-400">{{ deployHint }}</p>
      </div>

      <!-- Preview -->
      <ManifestPreview
        v-if="store.previewIsCurrent"
        :preview="store.preview"
        :acknowledged="acknowledged"
        @update:acknowledged="acknowledged = $event"
      />
    </div>
  </div>
</template>

<script setup>
/**
 * Install a multi-agent system from a manifest (trinity-enterprise#126).
 *
 * Three sources, one string: a bundled manifest, an uploaded file (read
 * client-side via FileReader — there is no multipart endpoint), or pasted text.
 * All three end up as `store.manifestText` and go to the same
 * `POST /api/systems/deploy`.
 *
 * Deploy is gated on a CURRENT preview (`previewIsCurrent`), which compares the
 * text the preview was produced from against the text in the box. Without that
 * binding a user can preview manifest A, edit it to B, and deploy B while reading
 * A's preview.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSystemsStore } from '../../stores/systems'
import ManifestPreview from './ManifestPreview.vue'
import DeployResult from './DeployResult.vue'
import BaseTextarea from '../base/BaseTextarea.vue'

// Mirrors the server-side byte cap on SystemDeployRequest.manifest. `file.size`
// is in bytes and so is the server's validator, so the two agree exactly.
// Client-side this is only a courtesy — the server is the enforcement point.
const MANIFEST_MAX_BYTES = 256 * 1024

const SOURCES = [
  { id: 'bundled', label: 'Pick a system' },
  { id: 'upload', label: 'Upload a file' },
  { id: 'paste', label: 'Paste YAML' }
]

const router = useRouter()
const store = useSystemsStore()

const source = ref('bundled')
const selectedId = ref(null)
const uploadName = ref('')
const uploadError = ref('')
const acknowledged = ref(false)

const canDeployNow = computed(
  () => store.canDeploy && (!store.needsAcknowledgement || acknowledged.value)
)

const deployHint = computed(() => {
  if (store.isDeploying) return 'This can take a minute per agent — do not close the page.'
  if (!store.manifestText.trim()) return ''
  if (!store.previewIsCurrent) {
    // `previewedText`, not `preview`: editing clears the preview PAYLOAD, so
    // keying on it made this branch unreachable and told someone who had just
    // previewed to "Preview first". The marker outlives the payload precisely so
    // these two states stay distinguishable.
    return store.previewedText
      ? 'The manifest changed — preview again before deploying.'
      : 'Preview first to see what this would create.'
  }
  if (store.previewHasBlockers) return 'Fix the blockers below before deploying.'
  if (store.needsAcknowledgement && !acknowledged.value) {
    return 'Confirm the highlighted consequences below to enable Deploy.'
  }
  return ''
})

function resetLocalState () {
  acknowledged.value = false
  uploadError.value = ''
}

// Consent is per-manifest, so it dies with the text it was given for.
//
// Without this, the acknowledgement outlives the manifest it applied to: preview a
// manifest with schedules, tick the box, edit the YAML, preview again — the box is
// still ticked and Deploy re-enables, so the user consented to manifest A's
// consequences and deployed manifest B's. That is the same
// deploy-what-you-didn't-look-at failure the preview/previewedText binding exists
// to prevent, one field over. Covers every source (typing, an uploaded file, a
// bundled card) because they all funnel through setManifestText.
watch(() => store.manifestText, () => { acknowledged.value = false })

async function pickBundled (manifest) {
  resetLocalState()
  selectedId.value = manifest.id
  await store.loadBundled(manifest.id)
}

function onFile (event) {
  const file = event.target.files?.[0]
  if (!file) return
  resetLocalState()
  uploadName.value = file.name
  if (!/\.(ya?ml)$/i.test(file.name)) {
    uploadError.value = 'Please choose a .yaml or .yml file.'
    return
  }
  if (file.size > MANIFEST_MAX_BYTES) {
    uploadError.value = `That file is larger than ${Math.round(MANIFEST_MAX_BYTES / 1024)} KB.`
    return
  }
  const reader = new FileReader()
  reader.onerror = () => { uploadError.value = 'Could not read that file.' }
  reader.onload = () => { store.setManifestText(String(reader.result || '')) }
  reader.readAsText(file)
}

function startOver () {
  store.reset()
  resetLocalState()
  selectedId.value = null
  uploadName.value = ''
  // A fresh install may have consumed a name, so `already_deployed` is now stale.
  store.fetchBundled()
}

function goToFleet () {
  const result = store.deployResult
  const viewId = result?.system_view_created
  if (viewId) {
    router.push({ path: '/', query: { view: viewId } })
    return
  }
  router.push({ path: '/', query: { tags: result?.system_name } })
}

function goToAgents () {
  router.push('/agents')
}

onMounted(() => {
  store.fetchBundled()
})
</script>
