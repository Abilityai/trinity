<template>
  <div class="prose-portal" @click="onBodyClick">
    <!-- The ONE v-html in the Workspace transcript. Everything inside has been
         through `renderMarkdownWithCodeBlocks`, i.e. through DOMPurify. -->
    <div v-html="html"></div>
    <!-- Copy feedback is a colour change and a word swap on a button the reader
         may not be looking at, so it is announced as well as shown. -->
    <span class="sr-only" aria-live="polite">{{ announcement }}</span>
  </div>
</template>

<script setup>
/**
 * The rendered body of an agent's markdown, and everything that belongs to it:
 * the one `v-html`, the one `.prose-portal` stylesheet, and the one copy
 * handler (#2515).
 *
 * These three used to be two of three, twice — `prose-portal` was copied
 * verbatim into PortalConversation and PortalRoom *specifically so the two
 * could not drift*, which is the tell that they wanted to be one thing. Copying
 * a stylesheet is survivable; copying a stylesheet AND a clipboard handler into
 * every future surface that renders agent markdown is not.
 *
 * So this is a body, not a bubble: no avatar, no rounded corner, no chat chrome
 * (that is PortalAgentBubble). A surface that renders a `.md` file rather than a
 * message mounts THIS and inherits render, style and copy as one unit.
 */
import { computed, ref, onBeforeUnmount } from 'vue'
import { renderMarkdownWithCodeBlocks } from '@/utils/markdown'
import { codeBlockText, DATA_CODE_BLOCK, DATA_COPY_CODE } from '@/utils/codeBlocks'
import {
  copyText, copyFeedback, COPY_FEEDBACK_TTL_MS, COPY_CODE_ARIA, COPY_CODE_LABEL,
} from '@/utils/clipboard'

const props = defineProps({
  content: { type: String, default: '' },
})

const html = computed(() => renderMarkdownWithCodeBlocks(props.content || ''))

const announcement = ref('')
let announceTimer = null

// Per-button, because two blocks can be mid-feedback at once. A WeakMap so a
// re-render that replaces the buttons does not leak their timers.
const resetTimers = new WeakMap()

/**
 * ONE delegated listener rather than a listener per block: the buttons live
 * inside `v-html`, so they are replaced wholesale on every re-render and there
 * is nothing stable to bind to.
 */
function onBodyClick(e) {
  const btn = e.target?.closest?.(`[${DATA_COPY_CODE}]`)
  if (!btn) return
  const block = btn.closest(`[${DATA_CODE_BLOCK}]`)
  // `:scope > pre` — the wrapper's OWN pre, never a descendant one. The wrapper
  // can only have been built by the decorator (agent-supplied markers are
  // stripped before decoration), and it wraps exactly one block.
  const pre = block?.querySelector?.(':scope > pre')
  if (!pre) return
  copyBlock(btn, codeBlockText(pre.textContent))
}

async function copyBlock(btn, text) {
  // writeText first, nothing awaited before it: Safari grants clipboard access
  // only inside the task the click started.
  const result = await copyText(text)
  const { label, tone } = copyFeedback(result)

  btn.textContent = label
  btn.setAttribute('aria-label', label)
  btn.dataset.state = tone
  announce(label)

  clearTimeout(resetTimers.get(btn))
  resetTimers.set(btn, setTimeout(() => {
    // Restore the CONSTANTS, never a value captured before the click: two
    // clicks inside the window would otherwise restore "Copied" and leave the
    // button permanently claiming a success it is no longer reporting.
    btn.textContent = COPY_CODE_LABEL
    btn.setAttribute('aria-label', COPY_CODE_ARIA)
    delete btn.dataset.state
    resetTimers.delete(btn)
  }, COPY_FEEDBACK_TTL_MS))
}

function announce(text) {
  announcement.value = text
  clearTimeout(announceTimer)
  announceTimer = setTimeout(() => { announcement.value = '' }, COPY_FEEDBACK_TTL_MS)
}

onBeforeUnmount(() => { clearTimeout(announceTimer) })
</script>

<style scoped>
/* #2211: 8px between paragraphs, not 4px. At `text-sm` with the bubble's own
   padding, 4px read as a single dense block; 8px is the next step on the 4px
   grid the design contract defines (4 tight / 8 related / 12 grouped). */
.prose-portal :deep(p) { margin: 0.5rem 0; }
/* First and last paragraph must not double up with the bubble padding, or the
   looser rhythm reads as a lopsided bubble. */
.prose-portal :deep(p:first-child) { margin-top: 0; }
.prose-portal :deep(p:last-child) { margin-bottom: 0; }
.prose-portal :deep(ul) { list-style: disc; padding-left: 1.25rem; }
.prose-portal :deep(ol) { list-style: decimal; padding-left: 1.5rem; }
.prose-portal :deep(ul),
.prose-portal :deep(ol) { margin: 0.5rem 0; }
.prose-portal :deep(li) { margin: 0.25rem 0; }
/* A nested list is a step in, on the same rhythm — not a second block. */
.prose-portal :deep(li > ul),
.prose-portal :deep(li > ol) { margin: 0.25rem 0; }
.prose-portal :deep(a) { text-decoration: underline; }

/* ---- Headings (#2616) -----------------------------------------------------
   Preflight resets h1-h6 to inherited size AND weight, so every heading an
   agent writes arrived at body weight — a 900-word reply with no structure.
   The ladder is deliberately BOUNDED: `# Title` names a section of the reply,
   not the page, so it tops out at the design system's section size (18/650)
   rather than page-title size, and h3+ lands on the meta overline. Three
   visibly distinct steps, all inside the six-size scale; a chat bubble has no
   room for six. */
.prose-portal :deep(h1),
.prose-portal :deep(h2),
.prose-portal :deep(h3),
.prose-portal :deep(h4),
.prose-portal :deep(h5),
.prose-portal :deep(h6) {
  /* 12px above / 4px below: a heading belongs to what follows it. */
  @apply mt-3 mb-1 leading-snug text-gray-900 dark:text-gray-100;
}
.prose-portal :deep(h1) {
  @apply text-[18px];
  font-weight: 650;
}
.prose-portal :deep(h2) {
  @apply text-sm;
  font-weight: 550;
}
.prose-portal :deep(h3),
.prose-portal :deep(h4),
.prose-portal :deep(h5),
.prose-portal :deep(h6) {
  @apply text-[11px] font-medium uppercase tracking-wide text-gray-600 dark:text-gray-400;
}
/* A reply that opens on a heading must not double the bubble's own padding. */
.prose-portal :deep(h1:first-child),
.prose-portal :deep(h2:first-child),
.prose-portal :deep(h3:first-child),
.prose-portal :deep(h4:first-child),
.prose-portal :deep(h5:first-child),
.prose-portal :deep(h6:first-child) { margin-top: 0; }

/* ---- Tables (#2616) -------------------------------------------------------
   The table is its own OBJECT one step off the bubble tint, exactly as a code
   block is, and its anatomy is `ReportTable` / `ck-table`'s rather than a third
   look: mono-caps header on chrome, 6/12 cell padding, a rule per row.

   `display: block` makes the table element its own horizontal scroll viewport
   (the rule CanvasKit already proved for the 24rem rail, #2583) so a ten-column
   table scrolls instead of widening the bubble; `overflow-wrap: normal` in the
   cells is its other half — a "break anywhere" inherited from a wrapper squeezes
   an auto-layout table to one character per line. */
.prose-portal :deep(table) {
  display: block;
  /* `fit-content`, not the block box's automatic full width: a two-column table
     that stretched to the bubble's edge would draw its border around an acre of
     nothing. It still scrolls, because `max-width` caps it at the column. */
  width: fit-content;
  max-width: 100%;
  overflow-x: auto;
  border-collapse: collapse;
  font-variant-numeric: tabular-nums;
  @apply my-2 rounded-md border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-900 text-[12.5px];
}
.prose-portal :deep(table:first-child) { margin-top: 0; }
.prose-portal :deep(table:last-child) { margin-bottom: 0; }
.prose-portal :deep(thead th) {
  @apply bg-gray-100 dark:bg-gray-750 text-gray-600 dark:text-gray-400 font-mono text-[11px] font-medium uppercase tracking-wide;
  padding: 6px 12px;
  text-align: start;
  white-space: nowrap;
}
.prose-portal :deep(th),
.prose-portal :deep(td) {
  @apply border-b border-gray-200 dark:border-gray-750;
  overflow-wrap: normal;
}
.prose-portal :deep(td) {
  padding: 6px 12px;
  vertical-align: top;
}
.prose-portal :deep(tbody tr:last-child td) { border-bottom: 0; }
/* GFM alignment survives sanitising as the `align` attribute, but a
   presentational hint loses to the author `text-align` above — so the two
   aligned cases are restated here, and a numeric column gets figures that
   line up. */
.prose-portal :deep(th[align="right"]),
.prose-portal :deep(td[align="right"]) { text-align: right; }
.prose-portal :deep(th[align="center"]),
.prose-portal :deep(td[align="center"]) { text-align: center; }

/* ---- Callout and divider (#2616) ------------------------------------------
   The 3px left rule is `ck-callout`'s, so a quoted line reads the same whether
   the agent wrote it into a message or onto a canvas. */
.prose-portal :deep(blockquote) {
  @apply my-2 rounded-md border-l-[3px] border-gray-300 dark:border-gray-700 bg-gray-200/50 dark:bg-gray-900/40 px-3 py-2 text-gray-700 dark:text-gray-300;
}
.prose-portal :deep(blockquote > :first-child) { margin-top: 0; }
.prose-portal :deep(blockquote > :last-child) { margin-bottom: 0; }
.prose-portal :deep(hr) {
  @apply my-3 border-0 border-t border-gray-200 dark:border-gray-750;
}

/* ---- Code blocks (#2515) --------------------------------------------------
   A block is its own OBJECT, one step off the bubble's tint in both themes,
   rather than a tinted paragraph that happened to be monospaced. */
.prose-portal :deep(.code-block) {
  @apply my-2 rounded-md overflow-hidden border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-900;
}
/* A message that IS a single code block must not double its bubble padding. */
.prose-portal :deep(.code-block:first-child) { margin-top: 0; }
.prose-portal :deep(.code-block:last-child) { margin-bottom: 0; }

/* The overline: 11px mono caps, the design system's own meta treatment.
   `select-none` so a select-all or a drag through the message does not pull
   "bash Copy" into the middle of the text the reader is copying by hand. */
.prose-portal :deep(.code-block-bar) {
  @apply flex items-center justify-between gap-2 px-2.5 py-1 select-none border-b border-gray-200 dark:border-gray-750 font-mono text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400;
}

/* Always visible: the control is the block's chrome, not an overlay. A
   hover-revealed Copy is unreachable on touch, and the AC's floor is
   "visible on hover or focus" — this clears it without a @media rule. */
.prose-portal :deep(.code-block-copy) {
  @apply rounded px-1.5 py-0.5 normal-case tracking-normal text-[11px] font-medium text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 focus:outline-none focus:ring-2 focus:ring-action-primary-500/40;
}
/* Feedback in colour AND in the word the button now carries. */
.prose-portal :deep(.code-block-copy[data-state="ok"]) {
  @apply text-status-success-600 dark:text-status-success-400;
}
.prose-portal :deep(.code-block-copy[data-state="error"]) {
  @apply text-status-danger-600 dark:text-status-danger-400;
}

/* Wrap at the edge. NO `overflow-x`: a scroller inside a chat bubble hides the
   end of a line behind a gesture nobody makes, and on touch it competes with
   the thread's own scroll. `anywhere` breaks a 300-character unbroken token,
   which is the case that used to widen the whole column.
   Accepted cost: ASCII tables and box drawing lose alignment on a narrow
   column. The COPY is unaffected — it reads textContent. */
.prose-portal :deep(pre) {
  @apply m-0 px-3 py-2.5 text-xs leading-relaxed font-mono bg-transparent;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.prose-portal :deep(pre code) {
  font-size: inherit;
  padding: 0;
  background: transparent;
}
/* Inline code: an explicit one-step tint so it reads as code, at a size that
   stays legible beside 14px body text. */
.prose-portal :deep(:not(pre) > code) {
  @apply font-mono rounded px-1 py-0.5 bg-gray-200/70 dark:bg-gray-700/70;
  font-size: 0.9em;
}
</style>
