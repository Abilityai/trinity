<template>
  <!-- #1925 triage — NOT a tab strip: a bounded wide table. Horizontal scroll
       INSIDE the container is the correct treatment for unbounded column width
       (design-system principle 7); collapsing columns into a "More" menu would
       hide data, not navigation. Left as-is deliberately so a later audit does
       not re-flag it. -->
  <div class="overflow-x-auto">
    <table class="min-w-full text-sm">
      <thead>
        <tr class="text-left text-xs text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700">
          <!-- #2771: headers render inline markdown on the same terms as cells.
               Consistency is the point — an agent that bolds a column name and
               bolds the values under it should not get two behaviours. -->
          <th
            v-for="col in columns"
            :key="col"
            class="py-1.5 pr-4 font-medium [overflow-wrap:anywhere]"
            v-html="headerHtml(col)"
          ></th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="(row, idx) in rows"
          :key="idx"
          class="border-b border-gray-100 dark:border-gray-800 align-top"
        >
          <!-- #2771: `v-html` of INLINE-only markdown, sanitised by the one
               DOMPurify policy in `utils/markdown.js`. `[overflow-wrap:anywhere]`
               matches CanvasProse — a long link or code span in a cell wraps
               instead of widening the column. -->
          <td
            v-for="col in columns"
            :key="col"
            class="py-1.5 pr-4 text-gray-800 dark:text-gray-200 [overflow-wrap:anywhere]"
            v-html="cellHtml(row, col)"
          ></td>
        </tr>
        <tr v-if="rows.length === 0">
          <td :colspan="columns.length || 1" class="py-3 text-gray-400 text-xs">No rows.</td>
        </tr>
      </tbody>
    </table>
    <!-- #1537: a tabular report is fetched a page at a time, so the card shows
         how much of the set it is holding and can pull the next window. -->
    <div v-if="meta && meta.total > rows.length" class="mt-2 flex items-center gap-3">
      <span class="text-xs text-gray-500 dark:text-gray-400">
        Showing {{ rows.length.toLocaleString() }} of {{ meta.total.toLocaleString() }} rows
      </span>
      <button
        class="text-xs px-2 py-0.5 rounded border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
        :disabled="loadingMore"
        @click="onLoadMore"
      >{{ loadingMore ? 'Loading…' : 'Load more' }}</button>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { renderInlineMarkdown } from '../../utils/markdown'
import { cellSource, escapeText } from '../../utils/inlineMarkdown'

// Expected payload shape: { columns: string[], rows: Array<object|array> }
// `meta`/`onLoadMore` are optional (#1537): supplied when the parent fetched
// this report through the paginated row reader. Absent for a whole-payload
// fetch, in which case the footer never renders and this behaves as before.
const props = defineProps({
  payload: { type: Object, default: () => ({}) },
  meta: { type: Object, default: null },
  loadMore: { type: Function, default: null },
})

const loadingMore = ref(false)
async function onLoadMore() {
  if (!props.loadMore || loadingMore.value) return
  loadingMore.value = true
  try {
    await props.loadMore()
  } finally {
    loadingMore.value = false
  }
}

const columns = computed(() => (Array.isArray(props.payload?.columns) ? props.payload.columns : []))
const rows = computed(() => (Array.isArray(props.payload?.rows) ? props.payload.rows : []))

/**
 * #2771 — a cell's rendered HTML. The value/escaping decision is pure
 * (`utils/inlineMarkdown.js::cellSource`, unit-tested); only strings are parsed
 * as markdown, so a number, boolean or JSON blob renders exactly as it did
 * before, escaped rather than interpreted.
 */
function cellHtml(row, col) {
  const { kind, text } = cellSource(row, columns.value, col)
  if (kind === 'empty') return ''
  return kind === 'markdown' ? renderInlineMarkdown(text) : escapeText(text)
}

function headerHtml(col) {
  return typeof col === 'string' ? renderInlineMarkdown(col) : escapeText(String(col ?? ''))
}
</script>
