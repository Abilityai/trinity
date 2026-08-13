<template>
  <div>
    <!-- Principle 15/25: a shape mismatch must stay VISIBLE. A tidy summary of
         a malformed payload otherwise looks intentional, which degrades more
         silently than the JSON dump it replaces — the reader can no longer tell
         "this is how the agent filed it" from "this is not what it meant". -->
    <p v-if="fallback" class="mb-2 text-xs text-gray-500 dark:text-gray-400">
      Unrecognised report format — showing a summary of what it contains.
    </p>

    <p v-if="!entries.length" class="text-sm text-gray-500 dark:text-gray-400">
      This report has no readable content.
    </p>

    <dl v-else class="divide-y divide-gray-100 dark:divide-gray-800">
      <div
        v-for="entry in entries"
        :key="entry.key || entry.label"
        class="py-1.5 sm:flex sm:gap-3 sm:items-baseline"
      >
        <dt class="text-xs text-gray-500 dark:text-gray-400 break-words sm:w-44 sm:shrink-0">
          {{ entry.label }}
        </dt>
        <!-- A described container ("12 items") is metadata about the payload,
             not content the agent wrote, so it reads at meta weight. -->
        <dd
          class="min-w-0 text-sm break-words sm:flex-1"
          :class="entry.hint === 'text'
            ? 'text-gray-800 dark:text-gray-200'
            : 'text-gray-500 dark:text-gray-400'"
        >{{ entry.value }}</dd>
      </div>
    </dl>

    <!-- Bounded AND stated (principle 28): a silent cut would make a 200-key
         payload look like a 40-key one. -->
    <p v-if="truncated" class="mt-2 text-xs text-gray-500 dark:text-gray-400">
      +{{ truncated.toLocaleString() }} more {{ truncated === 1 ? 'field' : 'fields' }} not shown
    </p>

    <details v-if="allowRaw" class="mt-3">
      <summary class="text-xs text-gray-500 dark:text-gray-400 cursor-pointer select-none">
        Show raw JSON
      </summary>
      <div class="mt-2"><ReportJson :payload="payload" /></div>
    </details>
  </div>
</template>

<script setup>
/**
 * The shared fallback for a payload no typed renderer can present (#2162).
 *
 * It replaces `ReportJson` in that role on EVERY surface, not only the
 * client-facing one. Two fallbacks would mean a client says "this report looks
 * wrong", the operator opens Agent Detail, and the two are looking at different
 * renderings of the same row — and it would leave a component in the shared
 * `components/reports/` directory with exactly one caller, inviting a third
 * variant. Nothing is lost for the operator: the raw payload moves one click
 * away rather than disappearing, and "what is actually in this report?" is now
 * answered before you have to read JSON.
 *
 * `allow-raw` is a POLICY, not a mechanism: *this surface never shows a raw
 * payload*. Named that way because it must cover two different routes to the
 * same place — a shape mismatch, and an agent that deliberately set
 * `display_hint: "json"` (a valid value in the MCP tool's enum). A "fallback
 * override" prop or slot would only have covered the first, silently leaving the
 * second able to put a raw dump in front of an external client.
 *
 * All the logic lives in `reportSummary.js`; this file is a dumb renderer,
 * because there is no component-mount harness here and logic written inline
 * would ship untested.
 */
import { computed } from 'vue'

import ReportJson from './ReportJson.vue'
import { summarizePayload } from './reportSummary'

const props = defineProps({
  payload: { type: [Object, Array, String, Number, Boolean], default: () => ({}) },
  // True when this was reached by SHAPE MISMATCH rather than chosen. Drives the
  // caption only — a deliberate `json` report is not malformed and must not be
  // labelled as such.
  fallback: { type: Boolean, default: false },
  // Operator default. The Workspace passes false.
  allowRaw: { type: Boolean, default: true },
})

const summary = computed(() => summarizePayload(props.payload))
const entries = computed(() => summary.value.entries)
const truncated = computed(() => summary.value.truncated)
</script>
