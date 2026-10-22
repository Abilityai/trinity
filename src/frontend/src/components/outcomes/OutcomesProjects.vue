<template>
  <!-- Operations › Outcomes per-project table (trinity-enterprise#941): the
       newest weekly record for each managed project. Bounded viewport with a
       stated total; numbers tabular and right-aligned. -->
  <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-750 rounded-lg" data-testid="outcomes-projects">
    <div class="flex items-center justify-between px-4 py-2 border-b border-gray-200 dark:border-gray-750">
      <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">Projects</h3>
      <span class="text-xs text-gray-500 dark:text-gray-400 tabular-nums" data-testid="outcomes-projects-total">
        {{ rows.length }} {{ rows.length === 1 ? 'project' : 'projects' }}
      </span>
    </div>
    <p v-if="!rows.length" class="px-4 py-6 text-sm text-gray-600 dark:text-gray-300" data-testid="outcomes-projects-empty">
      No managed project has a weekly record yet. Active Workspace Projects are analysed after each full week.
    </p>
    <div v-else class="max-h-[24rem] overflow-y-auto">
      <table class="w-full text-sm">
        <thead class="sticky top-0 bg-gray-100 dark:bg-gray-750 text-xs text-gray-600 dark:text-gray-300">
          <tr>
            <th scope="col" class="px-4 py-2 text-left font-medium">Project</th>
            <th scope="col" class="px-4 py-2 text-right font-medium">Done / planned</th>
            <th scope="col" class="px-4 py-2 text-right font-medium">Stalled</th>
            <th scope="col" class="px-4 py-2 text-right font-medium">Claims rejected</th>
            <th scope="col" class="px-4 py-2 text-right font-medium">Asks late</th>
            <th scope="col" class="px-4 py-2 text-right font-medium">Checks failed</th>
            <th scope="col" class="px-4 py-2 text-right font-medium">Cost / task</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="r in rows"
            :key="r.id"
            class="border-t border-gray-200 dark:border-gray-750"
            :data-testid="`outcomes-project-${r.scope_ref}`"
          >
            <td class="px-4 py-2 text-gray-900 dark:text-gray-100">
              {{ r.project_name || r.scope_ref }}
              <span class="block text-xs text-gray-500 dark:text-gray-400">week of {{ (r.window_start || '').slice(0, 10) }}</span>
            </td>
            <td class="px-4 py-2 text-right tabular-nums text-gray-700 dark:text-gray-300">
              {{ num(r.signals?.completion?.done) }} / {{ num(r.signals?.completion?.planned) }}
            </td>
            <td class="px-4 py-2 text-right tabular-nums text-gray-700 dark:text-gray-300">{{ num(r.signals?.stalls?.stalled_count) }}</td>
            <td class="px-4 py-2 text-right tabular-nums text-gray-700 dark:text-gray-300">{{ pct(r.signals?.done_claims?.rejection_rate) }}</td>
            <td class="px-4 py-2 text-right tabular-nums text-gray-700 dark:text-gray-300">{{ num(r.signals?.asks?.unanswered) }}</td>
            <td class="px-4 py-2 text-right tabular-nums text-gray-700 dark:text-gray-300">{{ pct(r.signals?.runs?.checks_failed_rate) }}</td>
            <td class="px-4 py-2 text-right tabular-nums text-gray-700 dark:text-gray-300">{{ usd(r.signals?.cost?.per_verified_task) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { num, pct, usd } from './format'

defineProps({ rows: { type: Array, default: () => [] } })
</script>
