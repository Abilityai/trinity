<template>
  <div class="fixed inset-0 z-40">
    <div class="absolute inset-0 bg-black/30" @click="$emit('close')"></div>

    <!-- Right slide-over on desktop; bottom sheet on mobile -->
    <div
      class="absolute bg-white dark:bg-gray-900 shadow-xl flex flex-col
             inset-x-0 bottom-0 max-h-[85vh] rounded-t-2xl
             sm:inset-y-0 sm:right-0 sm:left-auto sm:w-full sm:max-w-md sm:max-h-none sm:rounded-none"
      @dragover.prevent="dragging = true"
      @dragleave.prevent="dragging = false"
      @drop.prevent="onDrop"
    >
      <div class="shrink-0 flex items-center justify-between px-4 h-14 border-b border-gray-200 dark:border-gray-800">
        <div class="min-w-0">
          <div class="font-medium truncate">Files · {{ agent.name }}</div>
          <div class="text-xs text-gray-400">Send files to the agent or download what it shares</div>
        </div>
        <button class="p-2 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200" aria-label="Close" @click="$emit('close')">
          <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" /></svg>
        </button>
      </div>

      <!-- Drop zone / send -->
      <div class="px-4 py-3 border-b border-gray-200 dark:border-gray-800">
        <label
          class="flex flex-col items-center justify-center gap-1 text-sm rounded-xl border-2 border-dashed px-3 py-5 cursor-pointer transition"
          :class="[dragging ? 'border-action-primary-500 bg-action-primary-50 dark:bg-action-primary-900/20' : 'border-gray-300 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800', uploading ? 'opacity-60 pointer-events-none' : '']"
        >
          <svg class="w-6 h-6 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 01-.88-7.9A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" /></svg>
          <span class="font-medium">{{ uploading ? 'Sending…' : `Drop a file, or click to send to ${agent.name}` }}</span>
          <input type="file" class="hidden" :disabled="uploading" @change="onPick" />
        </label>
        <p v-if="uploadMsg" class="mt-2 text-xs" :class="uploadMsg.type === 'error' ? 'text-status-danger-600 dark:text-status-danger-400' : 'text-status-success-600 dark:text-status-success-400'">{{ uploadMsg.text }}</p>
      </div>

      <div class="flex-1 overflow-y-auto p-4 space-y-6">
        <div v-if="loading" class="text-center py-10">
          <div class="animate-spin rounded-full h-7 w-7 border-b-2 border-action-primary-500 mx-auto"></div>
        </div>
        <template v-else>
          <section>
            <h4 class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 mb-2">Files you sent</h4>
            <div v-if="!uploads.length" class="text-xs text-gray-400 py-1">Nothing sent yet.</div>
            <ul v-else class="space-y-2">
              <li v-for="u in uploads" :key="'u-' + u.filename" class="flex items-center gap-3 rounded-xl border border-gray-200 dark:border-gray-800 p-3">
                <FileIcon :mime="u.mime_type" />
                <div class="min-w-0 flex-1">
                  <div class="text-sm font-medium truncate">{{ u.filename }}</div>
                  <div class="text-xs text-gray-400">{{ humanSize(u.size_bytes) }}<span v-if="u.uploaded_at"> · sent {{ formatDate(u.uploaded_at) }}</span></div>
                </div>
              </li>
            </ul>
          </section>
          <section>
            <h4 class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 mb-2">Files from {{ agent.name }}</h4>
            <div v-if="error" class="text-sm text-status-danger-600 dark:text-status-danger-400">{{ error }}</div>
            <div v-else-if="!docs.length" class="text-xs text-gray-400 py-1">Nothing shared with you yet.</div>
            <ul v-else class="space-y-2">
              <li v-for="d in docs" :key="d.id" class="flex items-center gap-3 rounded-xl border border-gray-200 dark:border-gray-800 p-3">
                <FileIcon :mime="d.mime_type" />
                <div class="min-w-0 flex-1">
                  <div class="text-sm font-medium truncate">{{ d.filename }}</div>
                  <div class="text-xs text-gray-400">{{ humanSize(d.size_bytes) }}<span v-if="d.created_at"> · {{ formatDate(d.created_at) }}</span></div>
                </div>
                <a :href="d.download_url" target="_blank" rel="noopener" class="shrink-0 text-xs px-2.5 py-1.5 rounded-lg bg-action-primary-600 hover:bg-action-primary-700 text-white">Download</a>
              </li>
            </ul>
          </section>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, h } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'

const props = defineProps({ agent: { type: Object, required: true } })
defineEmits(['close'])

const store = useClientPortalStore()
const docs = ref([])
const uploads = ref([])
const loading = ref(true)
const error = ref(null)
const uploading = ref(false)
const uploadMsg = ref(null)
const dragging = ref(false)

// Small inline SVG file icon (replaces emoji glyphs; picks a hue by type).
const FileIcon = (p) => {
  const t = (p.mime || '').toLowerCase()
  const cls = t.startsWith('image/') ? 'text-status-success-500'
    : t.includes('pdf') ? 'text-status-danger-500'
    : (t.startsWith('text/') || t.includes('json') || t.includes('csv')) ? 'text-action-primary-500'
    : 'text-gray-400'
  return h('svg', { class: `w-6 h-6 shrink-0 ${cls}`, fill: 'none', viewBox: '0 0 24 24', stroke: 'currentColor' }, [
    h('path', { 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'stroke-width': 2, d: 'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z' }),
  ])
}

async function refreshUploads() {
  try { uploads.value = await store.fetchUploads(props.agent.name) } catch { /* best-effort */ }
}
async function upload(file) {
  if (!file) return
  uploading.value = true
  uploadMsg.value = null
  try {
    const res = await store.uploadDocument(props.agent.name, file)
    uploadMsg.value = { type: 'success', text: `Sent “${res.filename || file.name}” to ${props.agent.name}.` }
    await refreshUploads()
  } catch (err) {
    uploadMsg.value = { type: 'error', text: err.response?.data?.detail || 'Upload failed.' }
  } finally { uploading.value = false }
}
function onPick(e) { const f = e.target.files?.[0]; e.target.value = ''; upload(f) }
function onDrop(e) { dragging.value = false; const f = e.dataTransfer?.files?.[0]; if (f) upload(f) }

function humanSize(n) {
  n = Number(n) || 0
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}
function formatDate(iso) {
  try { return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) }
  catch { return iso }
}

onMounted(async () => {
  try { docs.value = await store.fetchDocuments(props.agent.name) }
  catch (err) { error.value = err.response?.data?.detail || 'Failed to load files.' }
  await refreshUploads()
  loading.value = false
})
</script>
