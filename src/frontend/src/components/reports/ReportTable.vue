<template>
  <div class="overflow-x-auto">
    <table class="min-w-full text-sm">
      <thead>
        <tr class="text-left text-xs text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700">
          <th v-for="col in columns" :key="col" class="py-1.5 pr-4 font-medium">{{ col }}</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="(row, idx) in rows"
          :key="idx"
          class="border-b border-gray-100 dark:border-gray-800 align-top"
        >
          <td
            v-for="col in columns"
            :key="col"
            class="py-1.5 pr-4 text-gray-800 dark:text-gray-200"
          >{{ cell(row, col) }}</td>
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

function cell(row, col) {
  const v = Array.isArray(row) ? row[columns.value.indexOf(col)] : row?.[col]
  if (v === null || v === undefined) return ''
  return typeof v === 'object' ? JSON.stringify(v) : String(v)
}
</script>
