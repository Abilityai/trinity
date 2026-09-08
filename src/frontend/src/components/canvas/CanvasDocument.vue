<!--
  A canvas as a DOCUMENT (ent#554).

  One renderer for the printable form, shared by the shared-link page and every
  authenticated surface's "Download PDF". AC #7 asks that the PDF be identical
  wherever it is produced from; the only way to promise that is for there to be
  one component producing it, rather than each surface printing its own layout.

  The blocks still go through `CanvasBlock`, so the document cannot drift from
  the screen either — this adds the frame (title, agent, freshness, generated
  date) and the page rules, not a second rendering of the content.

  Always the LIGHT rendering (AC #7): the print rules force white regardless of
  the viewer's theme, because a dark canvas printed as-is wastes a cartridge and
  reads badly on paper.
-->
<template>
  <article class="canvas-document" :data-canvas-id="canvas?.canvas_id">
    <header class="canvas-document__head">
      <h1 class="text-lg font-semibold">{{ canvas?.title || canvas?.canvas_id }}</h1>
      <p class="mt-0.5 text-xs text-gray-500">
        {{ agentName }} · {{ fresh.label }}<span v-if="fresh.stale"> · may be out of date</span>
      </p>
    </header>

    <CanvasKit>
      <p v-if="!blocks.length" class="text-xs text-gray-500">This canvas is empty.</p>
      <div v-else class="space-y-4">
        <div v-for="(b, i) in blocks" :key="b.id || i" class="canvas-document__block">
          <CanvasBlock :block="b" />
        </div>
      </div>
    </CanvasKit>

    <!-- AC #5: title, agent and generation date travel with the document. -->
    <footer class="canvas-document__foot">
      {{ canvas?.title || canvas?.canvas_id }} · {{ agentName }} · generated {{ generatedOn }}
    </footer>
  </article>
</template>

<script setup>
import { computed } from 'vue'
import CanvasBlock from './CanvasBlock.vue'
import CanvasKit from './CanvasKit.vue'
import { freshness, renderableBlocks } from './canvasUtils'

const props = defineProps({
  canvas: { type: Object, default: null },
  agentName: { type: String, default: '' },
})

const blocks = computed(() => renderableBlocks(props.canvas?.blocks))
const fresh = computed(() => freshness(props.canvas || {}))
const generatedOn = computed(() => new Date().toISOString().slice(0, 10))
</script>

<style>
/* Global, not scoped: these rules have to reach the kit's own markup, which
   renders inside CanvasKit's slot and carries no scope attribute. */
.canvas-document__foot {
  display: none;
}

@media print {
  /* A block is never cut in half across a page break (AC #4). */
  .canvas-document__block {
    break-inside: avoid;
    page-break-inside: avoid;
  }
  .canvas-document__head {
    margin-bottom: 1rem;
  }
  /* Only in the document — on screen the surface already says all of this. */
  .canvas-document__foot {
    display: block;
    margin-top: 1.5rem;
    font-size: 10px;
    color: #6b7280;
  }
  /* Always the light rendering, whatever theme the viewer is in. */
  .canvas-document,
  .canvas-document * {
    background: transparent !important;
    color: #111827 !important;
  }
  @page {
    margin: 14mm;
  }
}
</style>
