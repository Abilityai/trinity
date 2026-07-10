<template>
  <div class="fixed inset-0 z-40 flex justify-end">
    <div class="absolute inset-0 bg-black/30" @click="$emit('close')"></div>

    <div class="relative z-50 w-full max-w-md h-full bg-white dark:bg-gray-900 shadow-xl flex flex-col">
      <div class="flex items-center justify-between px-4 py-3 border-b border-gray-200 dark:border-gray-700">
        <div class="min-w-0">
          <div class="font-medium text-gray-900 dark:text-white truncate">Files from {{ agent.name }}</div>
          <div class="text-xs text-gray-400">Shared documents you can download</div>
        </div>
        <button class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 text-xl leading-none" @click="$emit('close')" aria-label="Close">×</button>
      </div>

      <!-- Send a file to the agent -->
      <div class="px-4 py-3 border-b border-gray-200 dark:border-gray-800">
        <label
          class="flex items-center justify-center gap-2 text-sm rounded-lg border border-dashed border-gray-300 dark:border-gray-700 px-3 py-2 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
          :class="uploading ? 'opacity-60 pointer-events-none' : ''"
        >
          <span>{{ uploading ? 'Sending…' : '⬆️ Send a file to ' + agent.name }}</span>
          <input type="file" class="hidden" @change="onUpload" :disabled="uploading" />
        </label>
        <p v-if="uploadMsg" :class="[
          'mt-2 text-xs',
          uploadMsg.type === 'error' ? 'text-status-danger-600 dark:text-status-danger-400'
                                     : 'text-status-success-600 dark:text-status-success-400'
        ]">{{ uploadMsg.text }}</p>
      </div>

      <!-- How files work — keeps expectations straight (files persist; they're
           not force-fed into every message). -->
      <details class="px-4 py-2 border-b border-gray-200 dark:border-gray-800 text-xs text-gray-500 dark:text-gray-400">
        <summary class="cursor-pointer select-none hover:text-gray-700 dark:hover:text-gray-200">How do files work?</summary>
        <div class="mt-2 space-y-1.5 leading-relaxed">
          <p>Files you send are kept in <span class="font-medium">{{ agent.name }}</span>'s inbox and stay there for the rest of your conversations — you don't need to re-send them.</p>
          <p>They're <span class="font-medium">not pasted into every message</span>. The agent is told which files are in your inbox and opens documents when they're relevant. For an <span class="font-medium">image</span>, just mention it (“look at the photo I sent”) and it's shown to the agent for that message.</p>
        </div>
      </details>

      <div class="flex-1 overflow-y-auto p-4 space-y-6">
        <div v-if="loading" class="text-center py-12">
          <div class="animate-spin rounded-full h-7 w-7 border-b-2 border-action-primary-500 mx-auto"></div>
        </div>

        <template v-else>
          <!-- Files you sent (your inbox on the agent) -->
          <section>
            <h4 class="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-2">Files you sent</h4>
            <div v-if="uploads.length === 0" class="text-xs text-gray-400 py-2">
              You haven't sent any files to {{ agent.name }} yet.
            </div>
            <ul v-else class="space-y-2">
              <li
                v-for="u in uploads"
                :key="'up-' + u.filename"
                class="flex items-center gap-3 rounded-lg border border-gray-200 dark:border-gray-800 p-3"
              >
                <span class="text-xl shrink-0">📎</span>
                <div class="min-w-0 flex-1">
                  <div class="text-sm font-medium text-gray-900 dark:text-white truncate">{{ u.filename }}</div>
                  <div class="text-xs text-gray-400">{{ humanSize(u.size_bytes) }}<span v-if="u.uploaded_at"> · sent {{ formatDate(u.uploaded_at) }}</span></div>
                </div>
                <span class="shrink-0 text-[11px] text-gray-400">in agent's inbox</span>
              </li>
            </ul>
          </section>

          <!-- Files from the agent (shared to you) -->
          <section>
            <h4 class="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-2">Files from {{ agent.name }}</h4>
            <div v-if="error" class="text-sm text-status-danger-600 dark:text-status-danger-400">{{ error }}</div>
            <div v-else-if="docs.length === 0" class="text-xs text-gray-400 py-2">
              {{ agent.name }} hasn't shared any files with you yet.
            </div>
            <ul v-else class="space-y-2">
              <li
                v-for="d in docs"
                :key="d.id"
                class="flex items-center gap-3 rounded-lg border border-gray-200 dark:border-gray-800 p-3"
              >
                <span class="text-xl shrink-0">{{ icon(d) }}</span>
                <div class="min-w-0 flex-1">
                  <div class="text-sm font-medium text-gray-900 dark:text-white truncate">{{ d.filename }}</div>
                  <div class="text-xs text-gray-400">{{ humanSize(d.size_bytes) }}<span v-if="d.created_at"> · {{ formatDate(d.created_at) }}</span></div>
                </div>
                <a
                  :href="d.download_url"
                  target="_blank"
                  rel="noopener"
                  class="shrink-0 text-xs px-2.5 py-1 rounded-md bg-action-primary-600 hover:bg-action-primary-700 text-white"
                >Download</a>
              </li>
            </ul>
          </section>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'

const props = defineProps({
  agent: { type: Object, required: true },   // { name }
})
defineEmits(['close'])

const store = useClientPortalStore()
const docs = ref([])
const uploads = ref([])
const loading = ref(true)
const error = ref(null)
const uploading = ref(false)
const uploadMsg = ref(null)

async function refreshUploads() {
  try { uploads.value = await store.fetchUploads(props.agent.name) }
  catch { /* best-effort; agent may be offline */ }
}

async function onUpload(ev) {
  const file = ev.target.files && ev.target.files[0]
  if (!file) return
  uploading.value = true
  uploadMsg.value = null
  try {
    const res = await store.uploadDocument(props.agent.name, file)
    uploadMsg.value = { type: 'success', text: `Sent “${res.filename}” to ${props.agent.name}.` }
    await refreshUploads()   // show it in "Files you sent"
  } catch (err) {
    uploadMsg.value = { type: 'error', text: err.response?.data?.detail || 'Upload failed.' }
  } finally {
    uploading.value = false
    ev.target.value = ''   // allow re-selecting the same file
  }
}

function humanSize(n) {
  n = Number(n) || 0
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}
function icon(d) {
  const t = (d.mime_type || '').toLowerCase()
  if (t.startsWith('image/')) return '🖼️'
  if (t.includes('pdf')) return '📕'
  if (t.includes('zip') || t.includes('tar')) return '🗜️'
  if (t.startsWith('text/') || t.includes('json') || t.includes('csv')) return '📃'
  return '📄'
}
function formatDate(iso) {
  try { return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) }
  catch { return iso }
}

onMounted(async () => {
  try {
    docs.value = await store.fetchDocuments(props.agent.name)
  } catch (err) {
    error.value = err.response?.data?.detail || 'Failed to load files.'
  }
  await refreshUploads()
  loading.value = false
})
</script>
