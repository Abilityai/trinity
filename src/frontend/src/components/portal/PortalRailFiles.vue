<!--
  The rail's Files tab body (trinity-enterprise#475) — what the Files
  slide-over (`PortalFilesPanel`, deleted) did, docked into the ent#474 rail:
  send a file to an agent in this chat (drop or pick), see what you sent, and
  download what the agent shared back. A room groups by participating agent
  and picks the recipient with a select — one drop zone, not one per agent.

  Reads `stores/portalRailFeeds.js`; never fetches. The shell owns the feed
  (`composables/usePortalRailFeeds.js`) so the collapsed rail can signal a
  newly shared file with this body unmounted, and so the container-backed
  inbox (`uploads`) is read only while this tab is on screen.

  Per-agent inbox scoping is unchanged: the same client-portal routes the
  drawer called, roster-gated server-side. Loading is a SKELETON keyed on the
  feed's verdict (AC 6 as amended 2026-09-06 — never the scanline, never a
  spinner, never a bare `loading` gate); a failed first fetch is `LoadFailed`,
  a failed refresh keeps the lists and raises `InlineError` (#1926).
-->
<template>
  <div
    class="space-y-5"
    data-testid="portal-rail-files"
    @dragover.prevent="dragging = true"
    @dragleave.prevent="dragging = false"
    @drop.prevent="onDrop"
  >
    <!-- Send -->
    <div>
      <label v-if="participants.length > 1" class="block text-[11px] text-gray-500 dark:text-gray-400 mb-2">
        Send to
        <select v-model="target" class="mt-0.5 w-full text-xs rounded border-gray-300 dark:border-gray-600 dark:bg-gray-800" data-testid="portal-rail-files-target">
          <option v-for="p in participants" :key="p" :value="p">{{ p }}</option>
        </select>
      </label>
      <label
        class="flex flex-col items-center justify-center gap-1 text-sm rounded-xl border-2 border-dashed px-3 py-5 cursor-pointer transition"
        :class="[dragging ? 'border-action-primary-500 bg-action-primary-50 dark:bg-action-primary-900/20' : 'border-gray-300 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800', uploading ? 'opacity-60 pointer-events-none' : '']"
        data-testid="portal-rail-files-drop"
      >
        <svg class="w-6 h-6 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 01-.88-7.9A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" /></svg>
        <span class="font-medium text-center">{{ uploading ? 'Sending…' : `Drop a file, or click to send to ${targetName}` }}</span>
        <input type="file" class="hidden" :disabled="uploading" @change="onPick" />
      </label>
      <InlineError v-if="uploadError" :message="uploadError" @dismiss="uploadError = ''" />
      <p v-else-if="uploadOk" class="mt-2 text-xs text-status-success-600 dark:text-status-success-400" role="status">{{ uploadOk }}</p>
    </div>

    <!-- Lists -->
    <PortalSkeleton v-if="view.state === 'loading'" variant="rail" />

    <LoadFailed
      v-else-if="view.state === 'failed'"
      title="Couldn't load files"
      :message="feeds.error || 'The files for this chat could not be read.'"
      :retrying="feeds.loading"
      @retry="feeds.refresh({ uploads: true })"
    />

    <template v-else>
      <InlineError v-if="view.stale" :message="feeds.error" @dismiss="feeds.error = null" />

      <section v-for="agent in participants" :key="agent" class="space-y-4" :data-testid="`portal-rail-files-${agent}`">
        <div v-if="participants.length > 1" class="flex items-center gap-2 min-w-0">
          <PortalAvatar :name="agent" :size="18" />
          <span class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 truncate">{{ agent }}</span>
          <span class="ml-auto text-xs text-gray-400">{{ countLabel(agent) }}</span>
        </div>

        <div>
          <h4 class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 mb-2">Files you sent</h4>
          <div v-if="!feeds.uploadsLoaded[agent]" class="h-3 w-24 rounded animate-pulse motion-reduce:animate-none bg-gray-100 dark:bg-gray-800/60" aria-busy="true"></div>
          <div v-else-if="!sent(agent).length" class="text-xs text-gray-400 py-1">Nothing sent yet.</div>
          <ul v-else class="space-y-2">
            <li v-for="u in sent(agent)" :key="'u-' + u.filename" class="flex items-center gap-3 rounded-xl border border-gray-200 dark:border-gray-800 p-3">
              <FileIcon :mime="u.mime_type" />
              <div class="min-w-0 flex-1">
                <div class="text-sm font-medium truncate">{{ u.filename }}</div>
                <div class="text-xs text-gray-400">{{ humanSize(u.size_bytes) }}<span v-if="u.uploaded_at"> · sent {{ formatDate(u.uploaded_at) }}</span></div>
              </div>
            </li>
          </ul>
        </div>

        <div>
          <h4 class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 mb-2">Files from {{ agent }}</h4>
          <div v-if="!shared(agent).length" class="text-xs text-gray-400 py-1">Nothing shared with you yet.</div>
          <ul v-else class="space-y-2">
            <li v-for="d in shared(agent)" :key="d.id" class="flex items-center gap-3 rounded-xl border border-gray-200 dark:border-gray-800 p-3">
              <FileIcon :mime="d.mime_type" />
              <div class="min-w-0 flex-1">
                <div class="text-sm font-medium truncate">{{ d.filename }}</div>
                <div class="text-xs text-gray-400">{{ humanSize(d.size_bytes) }}<span v-if="d.created_at"> · {{ formatDate(d.created_at) }}</span></div>
              </div>
              <a :href="d.download_url" target="_blank" rel="noopener" class="shrink-0 text-xs px-2.5 py-1.5 rounded-lg bg-action-primary-600 hover:bg-action-primary-700 text-white">Download</a>
            </li>
          </ul>
        </div>
      </section>
    </template>
  </div>
</template>

<script setup>
import { computed, h, ref, watch } from 'vue'
import { usePortalRailFeedsStore } from '@/stores/portalRailFeeds'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import PortalAvatar from './PortalAvatar.vue'
import PortalSkeleton from './PortalSkeleton.vue'
import { feedView } from './portalRail'

const props = defineProps({
  participants: { type: Array, default: () => [] },
})

const feeds = usePortalRailFeedsStore()

const dragging = ref(false)
const uploading = ref(false)
const uploadError = ref('')
const uploadOk = ref('')
const target = ref(null)

const participants = computed(() => props.participants)
const targetName = computed(() => target.value || participants.value[0] || 'the agent')

// Lists are always rendered around the send zone — the two "Nothing … yet."
// lines ARE the empty copy — so the verdict only decides loading / failed /
// ready (count is pinned at 1: an agent with no files is a ready tab).
const view = computed(() => feedView({
  participants: participants.value,
  hasLoaded: feeds.hasLoaded,
  error: feeds.error,
  count: 1,
}))

function sent(agent) { return feeds.uploads[agent] || [] }
function shared(agent) { return feeds.documents[agent] || [] }
function countLabel(agent) {
  const n = shared(agent).length
  return n ? `${n} shared` : 'nothing shared'
}

// The recipient follows the participant SET (joined key, not array identity).
const participantsKey = computed(() => participants.value.join(' '))
watch(participantsKey, () => {
  if (!participants.value.includes(target.value)) target.value = participants.value[0] || null
}, { immediate: true })

// Small inline SVG file icon (picks a hue by type — one fact, shape + hue).
const FileIcon = (p) => {
  const t = (p.mime || '').toLowerCase()
  const cls = t.startsWith('image/') ? 'text-status-success-500'
    : t.includes('pdf') ? 'text-status-danger-500'
    : (t.startsWith('text/') || t.includes('json') || t.includes('csv')) ? 'text-action-primary-500'
    : 'text-gray-400'
  return h('svg', { class: `w-6 h-6 shrink-0 ${cls}`, fill: 'none', viewBox: '0 0 24 24', stroke: 'currentColor', 'aria-hidden': 'true' }, [
    h('path', { 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'stroke-width': 2, d: 'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z' }),
  ])
}

async function upload(file) {
  if (!file) return
  const agent = targetName.value
  uploading.value = true
  uploadError.value = ''
  uploadOk.value = ''
  try {
    const res = await feeds.upload(agent, file)
    uploadOk.value = `Sent “${res?.filename || file.name}” to ${agent}.`
  } catch (err) {
    // The server names the reason (size, type, rate) — show that, not a
    // generic line (contract #17/#18).
    uploadError.value = err?.response?.data?.detail || 'Upload failed.'
  } finally {
    uploading.value = false
  }
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
</script>
