<!--
  The Files tab's preview lightbox (trinity-enterprise#548).

  Opens ONE row of the tab's flat list and walks the previewable ones with
  next / previous, the arrow keys and Escape. Built to
  `docs/memory/design-system-contract.md` from line one — semantic tokens only,
  light and dark both first-class, `viewState`-keyed loading, a bounded viewport
  for the unbounded case (a large text file).

  Four decisions worth reading before changing anything here.

  **Stacking.** `ConfirmDialog.vue` is also `<Teleport to="body">` at `z-50`.
  Equal z-index means DOM insertion order decides, which neither component
  controls. This overlay therefore sits DELIBERATELY BELOW it (`z-40`): a
  destructive confirm raised from the preview must render on top of the preview,
  never behind it.

  **`v-if`, never `v-show`.** `Portal.vue` mounts the rail column and the mobile
  sheet as SIBLINGS, so on a phone with the sheet open the tab body exists
  twice — and a teleported overlay ignores its ancestor's `hidden`. Two
  simultaneous lightboxes is the failure mode; unmounting is what prevents it.

  **Images render only through `<img :src="objectUrl">`.** Never an inline
  `<svg>`, never `v-html`. ent#548 lists svg among the image types and an
  uploaded SVG is a script host; `<img>` never executes it.

  **No `Range` header.** The text cap is applied by fetching the whole blob and
  slicing client-side. `main.py`'s CORS `allow_headers` does not list `Range`,
  so a ranged preview dies silently on any deployment whose portal base URL is
  genuinely cross-origin — and slicing also keeps the preview off
  `/api/files/{id}`'s transfer-start counter path entirely.
-->
<template>
  <Teleport to="body">
    <div
      class="fixed inset-0 z-40 flex flex-col bg-gray-950/55 p-4 sm:p-6"
      role="dialog"
      aria-modal="true"
      :aria-label="`Preview: ${filename}`"
      data-testid="portal-file-preview"
      @click.self="$emit('close')"
    >
      <div
        ref="cardEl"
        class="mx-auto flex w-full max-w-4xl min-h-0 flex-1 flex-col overflow-hidden rounded-[10px] border border-gray-200 bg-white shadow-lg dark:border-gray-750 dark:bg-gray-800"
      >
        <!-- Header: identity, then navigation, then the two actions. -->
        <div class="flex items-center gap-3 border-b border-gray-200 px-4 py-3 dark:border-gray-750">
          <div class="min-w-0 flex-1">
            <p class="truncate text-[14px] font-medium text-gray-900 dark:text-gray-100" data-testid="portal-file-preview-name">{{ filename }}</p>
            <p class="text-[12.5px] text-gray-600 dark:text-gray-400 tabular-nums">
              {{ humanSize(size) }}<span v-if="typeLabel"> · {{ typeLabel }}</span>
            </p>
          </div>
          <div class="flex shrink-0 items-center gap-1">
            <button
              type="button"
              class="rounded-md p-1.5 text-gray-600 hover:bg-gray-100 disabled:opacity-45 disabled:hover:bg-transparent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:text-gray-400 dark:hover:bg-gray-750 dark:focus-visible:ring-action-primary-400/40"
              :disabled="prevIndex === null"
              aria-label="Previous file"
              data-testid="portal-file-preview-prev"
              @click="$emit('navigate', prevIndex)"
            >
              <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7" /></svg>
            </button>
            <button
              type="button"
              class="rounded-md p-1.5 text-gray-600 hover:bg-gray-100 disabled:opacity-45 disabled:hover:bg-transparent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:text-gray-400 dark:hover:bg-gray-750 dark:focus-visible:ring-action-primary-400/40"
              :disabled="nextIndex === null"
              aria-label="Next file"
              data-testid="portal-file-preview-next"
              @click="$emit('navigate', nextIndex)"
            >
              <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" /></svg>
            </button>
            <BaseButton size="sm" variant="secondary" data-testid="portal-file-preview-download" @click="$emit('download')">
              Download
            </BaseButton>
            <BaseButton size="sm" variant="ghost" data-testid="portal-file-preview-close" @click="$emit('close')">
              Close
            </BaseButton>
          </div>
        </div>

        <!-- Body. One footprint across every face (contract p4): the wrapper
             owns the height, and loading / rendered / card all live inside it. -->
        <div class="min-h-0 flex-1 overflow-auto bg-gray-50 p-4 dark:bg-gray-900">
          <PortalSkeleton v-if="view.state === 'loading'" variant="rail" />

          <div v-else-if="view.state === 'ready' && kind === 'image'" class="flex h-full items-center justify-center">
            <img
              :src="objectUrl"
              :alt="filename"
              referrerpolicy="no-referrer"
              class="max-h-full max-w-full rounded-md object-contain"
              data-testid="portal-file-preview-image"
            />
          </div>

          <PortalMarkdown
            v-else-if="view.state === 'ready' && kind === 'markdown'"
            :content="text"
            data-testid="portal-file-preview-markdown"
          />

          <pre
            v-else-if="view.state === 'ready' && kind === 'text'"
            class="whitespace-pre-wrap break-words font-mono text-[12.5px] leading-relaxed text-gray-900 dark:text-gray-100"
            data-testid="portal-file-preview-text"
          >{{ text }}</pre>

          <!-- The card. Not an error state and not a dead end: it is what a
               `.zip` looks like, what an over-cap image looks like, and — the
               clause a reviewer will check — what a FAILED byte fetch looks
               like. "Never a blank modal" has to cover a failure too, so this
               is deliberately not an InlineError with a retry loop. -->
          <div v-else class="flex h-full items-center justify-center">
            <div class="max-w-sm rounded-lg border border-gray-200 bg-white p-4 text-center dark:border-gray-750 dark:bg-gray-800" data-testid="portal-file-preview-card">
              <p class="text-[14px] font-medium text-gray-900 dark:text-gray-100">{{ filename }}</p>
              <p class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-400 tabular-nums">
                {{ humanSize(size) }}<span v-if="typeLabel"> · {{ typeLabel }}</span>
              </p>
              <p class="mt-2 text-[12.5px] text-gray-600 dark:text-gray-400">{{ cardReason }}</p>
              <BaseButton size="sm" variant="primary" class="mt-3" @click="$emit('download')">Download</BaseButton>
            </div>
          </div>
        </div>

        <!-- The cap, stated in the UI and not only in code (ent#548 AC 7). -->
        <p
          v-if="capNotice"
          class="border-t border-gray-200 px-4 py-2 text-[12.5px] text-gray-600 dark:border-gray-750 dark:text-gray-400"
          data-testid="portal-file-preview-cap"
        >
          {{ capNotice }}
        </p>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import BaseButton from '@/components/base/BaseButton.vue'
import PortalMarkdown from './PortalMarkdown.vue'
import PortalSkeleton from './PortalSkeleton.vue'
import { viewState } from '@/utils/loadingState'
import {
  IMAGE_PREVIEW_CAP_BYTES,
  TEXT_PREVIEW_CAP_BYTES,
  humanSize,
  neighbour,
  previewCapNotice,
  previewKind,
} from './portalFiles'

const props = defineProps({
  // The tab's flat list — the SAME array it renders from, so an index here and
  // an index there cannot mean two different files.
  rows: { type: Array, default: () => [] },
  index: { type: Number, default: 0 },
  // `(row) => Promise<Blob>` — the caller owns where bytes come from (an
  // agent share is a signed URL, an upload is an authenticated route), so this
  // component never learns either.
  loadBlob: { type: Function, required: true },
})

const emit = defineEmits(['close', 'navigate', 'download'])

const row = computed(() => props.rows[props.index] || null)
const file = computed(() => row.value?.item || null)
const filename = computed(() => file.value?.filename || 'File')
const size = computed(() => Number(file.value?.size_bytes) || 0)
const kind = computed(() => previewKind(file.value))
const typeLabel = computed(() => file.value?.mime_type || '')

const prevIndex = computed(() => neighbour(props.rows, props.index, -1))
const nextIndex = computed(() => neighbour(props.rows, props.index, 1))

const objectUrl = ref(null)
const text = ref('')
const loadedBytes = ref(0)
const totalBytes = ref(0)
// `hasLoaded` is the honest "we have something to show" bit — a FAILED fetch
// sets it too, because the failure face is the card, not a retry (see the
// template). `viewState` then decides loading vs ready off it.
const hasLoaded = ref(false)
const failed = ref(false)

const view = computed(() => viewState({ hasLoaded: hasLoaded.value, count: 1 }))
const capNotice = computed(() =>
  kind.value === 'markdown' || kind.value === 'text'
    ? previewCapNotice(loadedBytes.value, totalBytes.value)
    : ''
)
const cardReason = computed(() => {
  if (failed.value) return "This file couldn't be read just now — you can still download it."
  if (kind.value === 'image') return 'This image is too large to preview here.'
  return "This file type can't be previewed here."
})

// The CanvasImage.vue pattern: a monotonic sequence, so a slow fetch that
// resolves after the reader has already paged on is discarded rather than
// replacing the file they are now looking at.
let fetchSeq = 0

function revoke() {
  if (objectUrl.value) {
    try { URL.revokeObjectURL(objectUrl.value) } catch { /* already gone */ }
    objectUrl.value = null
  }
}

async function load() {
  revoke()
  text.value = ''
  loadedBytes.value = 0
  totalBytes.value = 0
  failed.value = false
  hasLoaded.value = false

  const current = file.value
  if (!current) { hasLoaded.value = true; return }

  // Two faces that need no fetch at all: an unpreviewable type, and an image
  // over the cap. Both land on the card, so the reader sees the answer at once
  // instead of watching a skeleton for a request that was never worth making.
  if (kind.value === 'none' || (kind.value === 'image' && size.value > IMAGE_PREVIEW_CAP_BYTES)) {
    hasLoaded.value = true
    return
  }

  const seq = ++fetchSeq
  try {
    const blob = await props.loadBlob(row.value)
    if (seq !== fetchSeq) return
    totalBytes.value = blob?.size ?? size.value
    if (kind.value === 'image') {
      objectUrl.value = URL.createObjectURL(blob)
    } else {
      // Sliced here rather than asked for with `Range` — see the header.
      const slice = blob.slice(0, TEXT_PREVIEW_CAP_BYTES)
      loadedBytes.value = slice.size
      text.value = await slice.text()
    }
  } catch {
    if (seq !== fetchSeq) return
    failed.value = true
  } finally {
    if (seq === fetchSeq) hasLoaded.value = true
  }
}

watch(() => [props.index, file.value?.filename, file.value?.id], load, { immediate: true })

// ---- keyboard ---------------------------------------------------------------
//
// CAPTURE phase, and that is load-bearing rather than stylistic. The
// conversation's turn-cancel listener is on `document` in the BUBBLE phase, so
// a bubble listener here would run second — `shouldCancelOnEscape` would have
// already seen `defaultPrevented === false` and cancelled an in-flight turn
// before this modal ever got to close.
//
// Known residual, filed as a follow-up: `PortalConversation.vue` handles Escape
// for an ACTIVE VOICE CALL in a branch above that rule and never consults
// `defaultPrevented`, so a preview opened during a voice call also ends the
// call. Not fixed here — this merges after the voice track.
function onKeydown(e) {
  // Capture-phase listeners on `document` run in REGISTRATION order, and the
  // tab body mounts before this modal — so a confirm it raised owns Escape
  // first and marks the event. Honour that rather than closing both overlays
  // on one keystroke; it is the same `defaultPrevented` protocol we ask the
  // conversation to honour.
  if (e.defaultPrevented) return
  if (e.key === 'Escape') {
    e.preventDefault()
    emit('close')
    return
  }
  if (e.key === 'ArrowLeft' && prevIndex.value !== null) {
    e.preventDefault()
    emit('navigate', prevIndex.value)
    return
  }
  if (e.key === 'ArrowRight' && nextIndex.value !== null) {
    e.preventDefault()
    emit('navigate', nextIndex.value)
    return
  }
  if (e.key === 'Tab') trapTab(e)
}

const cardEl = ref(null)

const FOCUSABLE = 'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'

// No focus-trap utility exists in this repo (ConfirmDialog has none either), so
// this is the ~15 lines rather than a dependency.
function trapTab(e) {
  const root = cardEl.value
  if (!root) return
  const items = Array.from(root.querySelectorAll(FOCUSABLE))
  if (!items.length) return
  const first = items[0]
  const last = items[items.length - 1]
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault()
    first.focus()
  }
}

// Armed at mount, ABOVE every await (contract p23): a surface that renders as
// interactive must already be interactive, so Escape cannot be waiting on a
// fetch that has not landed.
onMounted(async () => {
  document.addEventListener('keydown', onKeydown, { capture: true })
  await nextTick()
  // Initial focus on the SAFE action, per the modal recipe.
  const el = cardEl.value?.querySelector('[data-testid="portal-file-preview-close"]')
  if (el && typeof el.focus === 'function') el.focus()
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown, { capture: true })
  fetchSeq++
  revoke()
})
</script>
