<template>
  <!-- #3265: what a sent message carried — a thumbnail per image, a chip per
       other file, and a failed chip (with its reason) for an upload that did
       not land. Every thumbnail box is a fixed size, so nothing shifts when
       the picture arrives. -->
  <div
    v-if="attachments && attachments.length"
    ref="rootEl"
    class="flex flex-wrap items-end justify-end gap-1.5 max-w-full"
    data-testid="portal-message-attachments"
  >
    <template v-for="(a, i) in attachments" :key="`${a.filename}-${i}`">
      <span
        v-if="a.failed"
        class="inline-flex items-center gap-1.5 max-w-full rounded-md px-2 py-1 text-xs bg-status-danger-50 dark:bg-status-danger-900/30 text-status-danger-700 dark:text-status-danger-300 ring-1 ring-status-danger-300 dark:ring-status-danger-800"
        :title="a.error || ''"
        data-testid="portal-message-attachment-failed"
      >
        <svg class="w-3.5 h-3.5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" /></svg>
        <span class="truncate max-w-[12rem]">{{ a.filename }}</span>
        <span class="truncate max-w-[16rem]">· {{ a.error || 'Not uploaded' }}</span>
      </span>

      <button
        v-else-if="isImageAttachment(a) && thumbs[i] !== 'failed'"
        type="button"
        class="block w-40 h-28 shrink-0 overflow-hidden rounded-lg border border-gray-200 dark:border-gray-750 bg-gray-100 dark:bg-gray-750 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500"
        :title="`${a.filename} — click to download`"
        :aria-label="`Download ${a.filename}`"
        data-testid="portal-message-attachment-image"
        @click="download(a)"
      >
        <img v-if="thumbs[i]" :src="thumbs[i]" :alt="a.filename" class="w-full h-full object-cover" />
        <span v-else class="block w-full h-full animate-pulse motion-reduce:animate-none" aria-hidden="true"></span>
      </button>

      <button
        v-else
        type="button"
        class="inline-flex items-center gap-1.5 max-w-full rounded-md px-2 py-1 text-xs border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-750 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500"
        :title="`${a.filename} — click to download`"
        :aria-label="`Download ${a.filename}`"
        data-testid="portal-message-attachment-file"
        @click="download(a)"
      >
        <svg class="w-3.5 h-3.5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" /></svg>
        <span class="truncate max-w-[14rem]">{{ a.filename }}</span>
        <span v-if="a.size_bytes" class="tabular-nums text-gray-500 dark:text-gray-400">· {{ humanSize(a.size_bytes) }}</span>
      </button>
    </template>
    <InlineError v-if="downloadError" class="w-full" :message="downloadError" data-testid="portal-message-attachment-error" />
  </div>
</template>

<script setup>
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import InlineError from '@/components/InlineError.vue'
import { humanSize } from './portalFiles'
import { isImageAttachment, thumbnailUrl } from './portalMessageAttachments'

const props = defineProps({
  // [{ filename, size_bytes, mime_type, failed, error, file? }]
  attachments: { type: Array, default: () => [] },
  agentName: { type: String, required: true },
  // (agentName, filename) => Promise<Blob> — the authenticated upload read.
  loadBlob: { type: Function, required: true },
})

const rootEl = ref(null)
// Per index: undefined = not loaded yet, a URL = loaded, 'failed' = show a chip.
const thumbs = ref([])
const downloadError = ref('')
let visible = false
let observer = null

function loadThumbs() {
  props.attachments.forEach((a, i) => {
    if (!isImageAttachment(a) || thumbs.value[i]) return
    thumbnailUrl(props.agentName, a, props.loadBlob)
      .then((url) => { thumbs.value[i] = url })
      .catch(() => { thumbs.value[i] = 'failed' })
  })
}

// Thumbnails load when the message scrolls into view, not for the whole
// thread at once: the read route is rate-limited per person.
onMounted(() => {
  if (!rootEl.value) return
  if (typeof IntersectionObserver === 'undefined') {
    visible = true
    loadThumbs()
    return
  }
  observer = new IntersectionObserver((records) => {
    if (records.some((r) => r.isIntersecting)) {
      visible = true
      loadThumbs()
      observer.disconnect()
      observer = null
    }
  }, { rootMargin: '200px' })
  observer.observe(rootEl.value)
})

onBeforeUnmount(() => observer?.disconnect())

watch(() => props.attachments, () => {
  thumbs.value = []
  if (visible) loadThumbs()
})

async function download(a) {
  downloadError.value = ''
  let url = null
  try {
    const blob = a.file || await props.loadBlob(props.agentName, a.filename)
    url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = a.filename || 'download'
    document.body.appendChild(link)
    link.click()
    link.remove()
  } catch {
    downloadError.value = `Couldn't download ${a.filename}. Try again, or find it in the Files tab.`
  } finally {
    if (url) setTimeout(() => URL.revokeObjectURL(url), 0)
  }
}
</script>
