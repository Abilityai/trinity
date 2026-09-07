<template>
  <div>
    <!-- Contained error: the source stays visible so the reader (and the
         agent, read back through the operator) can see what failed. Text
         interpolation, never v-html, for both. -->
    <pre
      v-if="error"
      class="overflow-x-auto rounded-lg border border-status-danger-500/40 bg-gray-50 p-3 text-xs text-status-danger-700 dark:bg-gray-900 dark:text-status-danger-300"
    >Diagram error: {{ error }}

{{ source }}</pre>

    <!-- Skeleton while the (lazily loaded) renderer works — a fixed footprint,
         pulse in the chrome fill, per the design contract. -->
    <div
      v-else-if="!svg"
      class="h-32 animate-pulse rounded-lg bg-gray-100 motion-reduce:animate-none dark:bg-gray-800"
      aria-busy="true"
    ><span class="sr-only">Rendering diagram…</span></div>

    <!-- The SVG has been through the app's one DOMPurify policy. -->
    <div v-else class="canvas-diagram overflow-x-auto" v-html="svg"></div>
  </div>
</template>

<script setup>
/**
 * Mermaid for canvas `diagram` blocks (ent#536, the #979 rules).
 *
 * Rendered in-parent, not in a sandboxed iframe: the production CSP
 * (`script-src 'self'`) blocks inline scripts in a srcdoc iframe and CORP blocks
 * the bundle from the iframe's opaque origin. So the SVG is produced off-DOM by
 * mermaid under `securityLevel: 'strict'` with HTML labels OFF (see
 * `MERMAID_CONFIG` for why that is load-bearing), then passed through the same
 * DOMPurify instance every other `v-html` in the app uses.
 *
 * The library is imported lazily — it is ~1.5 MB and only a canvas that shows a
 * diagram should pay for it. Initialisation is global to mermaid, so it is done
 * once per theme at module level rather than per instance; render ids come
 * from a module counter so two diagrams on one canvas never share a
 * `<marker id>` (which makes arrowheads vanish).
 */
import { onBeforeUnmount, ref, watch } from 'vue'
import { sanitizeSvg } from '../../utils/markdown'
import { useThemeStore } from '../../stores/theme'
import { MERMAID_CONFIG } from './canvasUtils'

const props = defineProps({
  source: { type: String, required: true },
})

let mermaidModule = null
let initialisedTheme = null
let renderSeq = 0
// mermaid.render shares parser/layout state across calls, so two diagrams
// mounting at once (a canvas with a `diagram` block AND a ```mermaid fence)
// bleed nodes into each other's SVG. Every render in the app queues behind
// the previous one — seen live on the ent#536 seed, not hypothesised.
let renderChain = Promise.resolve()

async function ensureMermaid(dark) {
  if (!mermaidModule) mermaidModule = (await import('mermaid')).default
  const theme = dark ? 'dark' : 'default'
  if (initialisedTheme !== theme) {
    mermaidModule.initialize({ ...MERMAID_CONFIG, flowchart: { ...MERMAID_CONFIG.flowchart }, theme })
    initialisedTheme = theme
  }
  return mermaidModule
}

function renderSerially(dark, id, source) {
  const run = renderChain.then(async () => {
    const mermaid = await ensureMermaid(dark)
    return mermaid.render(id, source)
  })
  // Keep the chain alive past a failure; the caller handles its own error.
  renderChain = run.catch(() => {})
  return run
}

const themeStore = useThemeStore()
const svg = ref('')
const error = ref('')
let mySeq = 0
let unmounted = false

async function render() {
  const seq = ++renderSeq
  mySeq = seq
  error.value = ''
  svg.value = ''
  const id = `canvas-mmd-${seq}`
  try {
    const out = await renderSerially(themeStore.isDark, id, String(props.source))
    if (unmounted || seq !== mySeq) return // superseded or gone
    // `sanitizeSvg`, not `sanitizeHtml`: the diagram's own id-scoped <style>
    // must survive, and every other path now forbids the element (ent#537).
    svg.value = sanitizeSvg(out.svg)
  } catch (e) {
    // mermaid can leave its scratch node behind on a parse error.
    if (typeof document !== 'undefined') document.getElementById(`d${id}`)?.remove()
    if (unmounted || seq !== mySeq) return
    error.value = e && e.message ? String(e.message) : String(e)
  }
}

watch(() => [props.source, themeStore.isDark], render, { immediate: true })
onBeforeUnmount(() => { unmounted = true })
</script>

<style scoped>
.canvas-diagram :deep(svg) {
  max-width: 100%;
  height: auto;
}
</style>
