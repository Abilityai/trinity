<template>
  <div class="overflow-x-auto">
    <table class="min-w-full text-sm">
      <thead>
        <tr class="text-left text-xs text-gray-500 border-b border-gray-200 dark:border-gray-700">
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
  </div>
</template>

<script setup>
import { computed } from 'vue'

// Expected payload shape: { columns: string[], rows: Array<object|array> }
const props = defineProps({ payload: { type: Object, default: () => ({}) } })

const columns = computed(() => (Array.isArray(props.payload?.columns) ? props.payload.columns : []))
const rows = computed(() => (Array.isArray(props.payload?.rows) ? props.payload.rows : []))

function cell(row, col) {
  const v = Array.isArray(row) ? row[columns.value.indexOf(col)] : row?.[col]
  if (v === null || v === undefined) return ''
  return typeof v === 'object' ? JSON.stringify(v) : String(v)
}
</script>
