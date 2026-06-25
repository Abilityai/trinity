<template>
  <!--
    Reusable loading skeleton (trinity-enterprise#1266 / #1266).
    Communicates "loading" instead of a frozen/empty UI while metrics load.
    Dark-mode aware; reserves layout space to avoid shift when real content
    replaces it. Variants:
      - rows  : stacked shimmer bars (timeline rows, lists, cards)
      - nodes : scattered node-card placeholders (collaboration graph)
  -->
  <div
    class="skeleton-loader w-full"
    role="status"
    aria-busy="true"
    aria-label="Loading"
  >
    <!-- rows: timeline / list / card placeholders -->
    <div v-if="variant === 'rows'" class="flex flex-col" :style="{ gap }">
      <div
        v-for="n in count"
        :key="n"
        class="animate-pulse bg-gray-200 dark:bg-gray-700 rounded-md w-full"
        :style="{ height }"
      ></div>
    </div>

    <!-- nodes: collaboration-graph placeholders -->
    <div
      v-else-if="variant === 'nodes'"
      class="flex flex-wrap items-center justify-center gap-10 p-8"
    >
      <div
        v-for="n in count"
        :key="n"
        class="flex flex-col items-center gap-2 animate-pulse"
      >
        <div
          class="rounded-full bg-gray-200 dark:bg-gray-700"
          :style="{ width: nodeSize, height: nodeSize }"
        ></div>
        <div class="h-3 w-16 rounded bg-gray-200 dark:bg-gray-700"></div>
      </div>
    </div>

    <span class="sr-only">Loading…</span>
  </div>
</template>

<script setup>
defineProps({
  // 'rows' (stacked bars) | 'nodes' (graph node placeholders)
  variant: { type: String, default: 'rows' },
  // number of placeholder rows / nodes to render
  count: { type: Number, default: 3 },
  // height of each row bar (rows variant)
  height: { type: String, default: '1.5rem' },
  // gap between row bars (rows variant)
  gap: { type: String, default: '0.5rem' },
  // diameter of each node circle (nodes variant)
  nodeSize: { type: String, default: '3.5rem' },
})
</script>
