<template>
  <div class="space-y-4">
    <!-- Outcome header ---------------------------------------------------- -->
    <div
      class="rounded-lg border p-4"
      :class="hasBlockers
        ? 'border-status-danger-300 dark:border-status-danger-700 bg-status-danger-50 dark:bg-status-danger-900/20'
        : 'border-status-success-300 dark:border-status-success-700 bg-status-success-50 dark:bg-status-success-900/20'"
    >
      <div class="flex items-start gap-3">
        <span class="text-lg" aria-hidden="true">{{ hasBlockers ? '⛔' : '✅' }}</span>
        <div>
          <h3
            class="font-semibold"
            :class="hasBlockers
              ? 'text-status-danger-800 dark:text-status-danger-200'
              : 'text-status-success-800 dark:text-status-success-200'"
          >
            {{ hasBlockers ? 'This manifest cannot deploy yet' : 'No blockers found' }}
          </h3>
          <!-- Never "this will deploy": github: templates are not probed, so the
               preview genuinely cannot promise a remote-template manifest works. -->
          <p class="mt-1 text-sm text-gray-600 dark:text-gray-400">
            <template v-if="hasBlockers">
              Fix the {{ preview.failed.length }} problem{{ preview.failed.length === 1 ? '' : 's' }} below, then preview again.
            </template>
            <template v-else-if="hasRemoteTemplates">
              Local templates and resource settings check out. This manifest also uses
              <code class="text-xs">github:</code> templates, which are only verified at deploy time.
            </template>
            <template v-else>
              Local templates and resource settings check out.
            </template>
          </p>
        </div>
      </div>
    </div>

    <!-- Blockers ---------------------------------------------------------- -->
    <section v-if="hasBlockers">
      <h4 class="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-2">Blockers</h4>
      <ul class="space-y-2">
        <li
          v-for="f in preview.failed"
          :key="`${f.short_name}-${f.name}`"
          class="rounded-md border border-status-danger-200 dark:border-status-danger-800 bg-white dark:bg-gray-800 p-3"
        >
          <div class="flex items-baseline justify-between gap-2">
            <span class="font-mono text-sm text-gray-900 dark:text-gray-100">{{ f.short_name }}</span>
            <span v-if="f.status_code" class="text-xs text-gray-500 dark:text-gray-400">
              HTTP {{ f.status_code }}
            </span>
          </div>
          <p v-if="f.template" class="mt-0.5 font-mono text-xs text-gray-500 dark:text-gray-400">
            {{ f.template }}
          </p>
          <!-- Plain text, never v-html (H-005). `reason` is credential-sanitized
               server-side, but it is NOT HTML-sanitized. -->
          <p class="mt-1 text-sm text-status-danger-700 dark:text-status-danger-300">{{ f.reason }}</p>
        </li>
      </ul>
    </section>

    <!-- Duplicate-name confirmation -------------------------------------- -->
    <section
      v-if="duplicateWarnings.length"
      class="rounded-lg border-2 border-status-warning-400 dark:border-status-warning-600 bg-status-warning-50 dark:bg-status-warning-900/20 p-4"
    >
      <h4 class="font-semibold text-status-warning-900 dark:text-status-warning-100 flex items-center gap-2">
        <span aria-hidden="true">⚠️</span> These agents already exist
      </h4>
      <p class="mt-1 text-sm text-status-warning-800 dark:text-status-warning-200">
        Deploying creates <strong>separate, suffixed copies</strong> rather than updating the
        existing agents. There is no un-deploy: removing them afterwards is manual, one agent
        at a time.
      </p>
      <ul class="mt-2 space-y-1">
        <li
          v-for="w in duplicateWarnings"
          :key="w"
          class="text-sm text-status-warning-900 dark:text-status-warning-100 font-mono"
        >
          {{ w }}
        </li>
      </ul>
    </section>

    <!-- Agents ------------------------------------------------------------ -->
    <section>
      <h4 class="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-2">
        Agents to create ({{ agents.length }})
      </h4>
      <div class="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
        <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700 text-sm">
          <thead class="bg-gray-50 dark:bg-gray-800">
            <tr>
              <th class="px-3 py-2 text-left font-medium text-gray-500 dark:text-gray-400">Agent name</th>
              <th class="px-3 py-2 text-left font-medium text-gray-500 dark:text-gray-400">In manifest</th>
              <th class="px-3 py-2 text-left font-medium text-gray-500 dark:text-gray-400">Template</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-200 dark:divide-gray-700 bg-white dark:bg-gray-800">
            <tr v-for="a in agents" :key="a.name">
              <td class="px-3 py-2 font-mono text-gray-900 dark:text-gray-100">{{ a.name }}</td>
              <td class="px-3 py-2 text-gray-500 dark:text-gray-400">{{ a.short_name }}</td>
              <td class="px-3 py-2 font-mono text-xs text-gray-600 dark:text-gray-300">{{ a.template }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
        Names are provisional — they are resolved again at deploy, so an agent created in the
        meantime can shift them.
      </p>
    </section>

    <!-- Permission topology ---------------------------------------------- -->
    <section>
      <h4 class="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-2">
        Permissions
        <span v-if="permissionGrantCount" class="font-normal text-gray-500 dark:text-gray-400">
          — {{ permissionGrantCount }} grant{{ permissionGrantCount === 1 ? '' : 's' }}
        </span>
      </h4>
      <p v-if="!permissionSources.length" class="text-sm text-gray-500 dark:text-gray-400">
        No inter-agent permissions are configured. Agents will not be able to call each other.
      </p>
      <ul v-else class="space-y-1">
        <li
          v-for="[source, targets] in permissionSources"
          :key="source"
          class="rounded-md bg-gray-50 dark:bg-gray-800 px-3 py-2 text-sm"
        >
          <span class="font-mono text-gray-900 dark:text-gray-100">{{ source }}</span>
          <template v-if="targets.length">
            <span class="text-gray-400 dark:text-gray-500 mx-1">&rarr;</span>
            <span class="font-mono text-gray-700 dark:text-gray-300">{{ targets.join(', ') }}</span>
          </template>
          <span v-else class="ml-1 text-gray-500 dark:text-gray-400 italic">
            permissions cleared (cannot call anyone)
          </span>
        </li>
      </ul>
      <p v-if="permissionSources.length" class="mt-1 text-xs text-gray-500 dark:text-gray-400">
        Shown assuming every agent is created. If some fail, only the agents that exist are wired up.
      </p>
    </section>

    <!-- Schedules --------------------------------------------------------- -->
    <section>
      <h4 class="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-2">
        Schedules ({{ schedules.length }})
      </h4>
      <p v-if="!schedules.length" class="text-sm text-gray-500 dark:text-gray-400">
        No schedules. The agents will only run when you or another agent triggers them.
      </p>
      <div v-else class="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
        <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700 text-sm">
          <thead class="bg-gray-50 dark:bg-gray-800">
            <tr>
              <th class="px-3 py-2 text-left font-medium text-gray-500 dark:text-gray-400">Agent</th>
              <th class="px-3 py-2 text-left font-medium text-gray-500 dark:text-gray-400">Name</th>
              <th class="px-3 py-2 text-left font-medium text-gray-500 dark:text-gray-400">Cron</th>
              <th class="px-3 py-2 text-left font-medium text-gray-500 dark:text-gray-400">Message</th>
              <th class="px-3 py-2 text-left font-medium text-gray-500 dark:text-gray-400">State</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-200 dark:divide-gray-700 bg-white dark:bg-gray-800">
            <tr v-for="(s, i) in schedules" :key="`${s.agent}-${s.name}-${i}`">
              <td class="px-3 py-2 font-mono text-xs text-gray-900 dark:text-gray-100">{{ s.agent }}</td>
              <td class="px-3 py-2 text-gray-700 dark:text-gray-300">{{ s.name }}</td>
              <td class="px-3 py-2 font-mono text-xs text-gray-700 dark:text-gray-300">{{ s.cron }}</td>
              <td class="px-3 py-2 font-mono text-xs text-gray-600 dark:text-gray-400">{{ s.message }}</td>
              <td class="px-3 py-2">
                <span
                  v-if="s.enabled"
                  class="inline-flex items-center rounded-full bg-status-warning-100 dark:bg-status-warning-900/40 px-2 py-0.5 text-xs font-medium text-status-warning-800 dark:text-status-warning-200"
                >
                  runs automatically
                </span>
                <span v-else class="text-xs text-gray-500 dark:text-gray-400">disabled</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- Other warnings ---------------------------------------------------- -->
    <section v-if="otherWarnings.length">
      <h4 class="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-2">Notes</h4>
      <ul class="space-y-1">
        <li
          v-for="w in otherWarnings"
          :key="w"
          class="rounded-md bg-status-info-50 dark:bg-status-info-900/20 px-3 py-2 text-sm text-status-info-800 dark:text-status-info-200"
        >
          {{ w }}
        </li>
      </ul>
    </section>

    <!-- Consent gate ------------------------------------------------------ -->
    <section
      v-if="needsAcknowledgement"
      class="rounded-lg border-2 border-status-warning-400 dark:border-status-warning-600 bg-status-warning-50 dark:bg-status-warning-900/20 p-4"
    >
      <h4 class="font-semibold text-status-warning-900 dark:text-status-warning-100 flex items-center gap-2">
        <span aria-hidden="true">⚠️</span> This changes more than just these agents
      </h4>
      <ul class="mt-2 space-y-1 text-sm text-status-warning-800 dark:text-status-warning-200 list-disc list-inside">
        <li v-if="preview.prompt_updated">
          It <strong>replaces the platform-wide system prompt</strong> for
          <strong>every agent on this Trinity instance</strong>, not only the ones created here.
        </li>
        <li v-if="enabledScheduleCount">
          It starts <strong>{{ enabledScheduleCount }} recurring schedule{{ enabledScheduleCount === 1 ? '' : 's' }}</strong>
          immediately. These agents will run on their own, on a timer, and consume API budget.
        </li>
      </ul>
      <label class="mt-3 flex items-start gap-2 cursor-pointer">
        <input
          type="checkbox"
          :checked="acknowledged"
          class="mt-0.5 rounded border-gray-300 dark:border-gray-600"
          data-testid="ack-checkbox"
          @change="$emit('update:acknowledged', $event.target.checked)"
        />
        <span class="text-sm font-medium text-status-warning-900 dark:text-status-warning-100">
          I understand and want to continue
        </span>
      </label>
    </section>
  </div>
</template>

<script setup>
/**
 * Dry-run preview for a system manifest (trinity-enterprise#126, AC #2).
 *
 * Renders the backend's own preview — resolved agent names, permission topology
 * and schedules all come from `POST /api/systems/deploy` with `dry_run: true`,
 * computed by the same pure resolvers the real writers use. Nothing here parses
 * YAML: only the backend knows the `_N`-suffixed resolved names.
 *
 * Two deliberate honesty constraints:
 *   * it never claims the manifest "will deploy" — `github:` templates are not
 *     probed, so that promise cannot be made;
 *   * the topology is optimistic (resolved against all agents, while a partial
 *     deploy wires up only those created), and says so.
 *
 * All manifest-derived text renders as plain text, never v-html (H-005).
 * Failure reasons are credential-sanitized server-side but not HTML-sanitized.
 */
import { computed } from 'vue'

const props = defineProps({
  preview: { type: Object, required: true },
  acknowledged: { type: Boolean, default: false }
})

defineEmits(['update:acknowledged'])

const agents = computed(() => props.preview.agents_to_create || [])
const schedules = computed(() => props.preview.schedules_preview || [])
const hasBlockers = computed(() => (props.preview.failed || []).length > 0)

const hasRemoteTemplates = computed(
  () => agents.value.some(a => (a.template || '').startsWith('github:'))
)

// {source: targets} preserves insertion order from the backend's write-set.
const permissionSources = computed(() => Object.entries(props.preview.permission_edges || {}))
const permissionGrantCount = computed(
  () => permissionSources.value.reduce((n, [, targets]) => n + targets.length, 0)
)

const enabledScheduleCount = computed(() => schedules.value.filter(s => s.enabled).length)

const needsAcknowledgement = computed(
  () => Boolean(props.preview.prompt_updated) || enabledScheduleCount.value > 0
)

// Split so the duplicate-name case gets its own confirm-grade panel instead of
// being buried in a generic notes list. On a fresh install, re-installing a
// bundled manifest hits this by default.
const DUPLICATE_RE = /already exists/i
const duplicateWarnings = computed(
  () => (props.preview.warnings || []).filter(w => DUPLICATE_RE.test(w))
)
const otherWarnings = computed(
  () => (props.preview.warnings || []).filter(w => !DUPLICATE_RE.test(w))
)
</script>
