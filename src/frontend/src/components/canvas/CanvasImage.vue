<template>
  <figure class="m-0">
    <!-- Only ever bound to an https URL, a data:image URI, or an object URL we
         minted ourselves — `imageSource` decided which, and a workspace path is
         never put on an <img> directly (it would 401). -->
    <img
      v-if="src && !failed"
      :src="src"
      :alt="image.alt || image.caption || 'Image from the agent'"
      class="max-h-[480px] max-w-full rounded-lg border border-gray-200 dark:border-gray-800"
      loading="lazy"
      referrerpolicy="no-referrer"
      @error="failed = true"
    />

    <!-- Workspace image still loading: a fixed-footprint skeleton. -->
    <div
      v-else-if="!failed && image.kind === 'path' && agentName"
      class="h-40 w-full max-w-md animate-pulse rounded-lg bg-gray-100 motion-reduce:animate-none dark:bg-gray-800"
      aria-busy="true"
    ><span class="sr-only">Loading image…</span></div>

    <!-- Honest, not broken: say what the image is and why it is not here. -->
    <p v-else class="rounded-lg border border-dashed border-gray-300 px-3 py-2 text-xs text-gray-600 dark:border-gray-700 dark:text-gray-300">
      <template v-if="image.kind === 'path' && !agentName">
        This image is a file in the agent's workspace and is not available on this surface.
      </template>
      <template v-else-if="image.kind === 'path'">
        Image could not be loaded from the agent's workspace — it may be stopped, or the file may have moved.
      </template>
      <template v-else>Image could not be loaded.</template>
      <span class="ml-1 break-all font-mono text-gray-500 dark:text-gray-400">{{ shortSrc }}</span>
    </p>

    <figcaption v-if="image.caption" class="mt-1.5 text-xs text-gray-500 dark:text-gray-400">
      {{ image.caption }}
    </figcaption>
  </figure>
</template>

<script setup>
/**
 * Image for canvas `image` blocks (ent#536, the #979 rule).
 *
 * A web URL or inline data URI renders directly. A workspace-file image is
 * fetched through the authenticated `/files/preview` route via the agents
 * store (a bare `<img src>` to that route 401s) and bound as an object URL,
 * which is revoked when the source changes or the block unmounts so ten
 * canvases do not leak ten blobs.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useAgentsStore } from '../../stores/agents'

const props = defineProps({
  // From `canvasUtils.imageSource`: {kind: 'url'|'data'|'path', src, alt, caption}
  image: { type: Object, required: true },
  agentName: { type: String, default: null },
})

const agentsStore = useAgentsStore()
const objectUrl = ref(null)
const failed = ref(false)
let fetchSeq = 0

const src = computed(() => {
  if (props.image.kind === 'path') return objectUrl.value
  return props.image.src
})

const shortSrc = computed(() => {
  const s = props.image.kind === 'data' ? 'inline image' : props.image.src
  return s.length > 96 ? `${s.slice(0, 93)}…` : s
})

function revoke() {
  if (objectUrl.value) {
    try { URL.revokeObjectURL(objectUrl.value) } catch { /* already gone */ }
    objectUrl.value = null
  }
}

async function loadPath() {
  revoke()
  failed.value = false
  if (props.image.kind !== 'path' || !props.agentName) return
  const seq = ++fetchSeq
  try {
    const res = await agentsStore.getFilePreviewBlob(props.agentName, props.image.src)
    if (seq !== fetchSeq) { URL.revokeObjectURL(res.url); return } // superseded
    objectUrl.value = res.url
  } catch {
    if (seq === fetchSeq) failed.value = true
  }
}

watch(() => [props.image.src, props.image.kind, props.agentName], loadPath, { immediate: true })
onBeforeUnmount(() => { fetchSeq++; revoke() })
</script>
