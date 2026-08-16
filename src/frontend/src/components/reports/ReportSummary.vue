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
  </div>
</template>

<script setup>
/**
 * The CLIENT-FACING fallback for a payload no typed renderer can present (#2162).
 *
 * Passed to `ReportRenderer` as `:fallback-component` by the Workspace agent
 * page, and by nothing else. The operator surfaces keep `ReportJson`, and that
 * split IS the design rather than an omission — AC #2 asks for a fallback
 * "deliberately stricter than the operator side, because the audience is an
 * external client". A raw dump is a FEATURE when you are debugging an agent's
 * own output and a defect when the reader is that agent's customer.
 *
 * So there is deliberately no raw-payload escape hatch in here, and adding one
 * would re-open the bug: the override covers an agent-chosen
 * `display_hint: "json"` as well as a shape mismatch (`json` is a valid value in
 * the MCP tool's enum), so a disclosure would hand the client the dump by either
 * route.
 *
 * All the logic lives in `reportSummary.js`; this file is a dumb renderer,
 * because there is no component-mount harness here and logic written inline
 * would ship untested.
 */
import { computed } from 'vue'

import { summarizePayload } from './reportSummary'

const props = defineProps({
  payload: { type: [Object, Array, String, Number, Boolean], default: () => ({}) },
  // True when this was reached by SHAPE MISMATCH rather than chosen. Drives the
  // caption only — a deliberate `json` report is not malformed and must not be
  // labelled as such.
  fallback: { type: Boolean, default: false },
})

const summary = computed(() => summarizePayload(props.payload))
const entries = computed(() => summary.value.entries)
const truncated = computed(() => summary.value.truncated)
</script>
