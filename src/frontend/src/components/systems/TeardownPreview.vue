<template>
  <div class="space-y-4">
    <!-- Unverified membership: the verb is unavailable, and why ------------ -->
    <section
      v-if="preview.membership_verified === false"
      class="rounded-lg border-2 border-status-danger-400 dark:border-status-danger-600 bg-status-danger-50 dark:bg-status-danger-900/20 p-4"
    >
      <h4 class="text-sm font-semibold text-status-danger-900 dark:text-status-danger-100 flex items-center gap-2">
        <span aria-hidden="true">⛔</span> Membership could not be verified
      </h4>
      <p class="mt-1 text-sm text-status-danger-800 dark:text-status-danger-200">
        The system's membership tags could not be read, so the agents below were
        matched by <strong>name only</strong>. A different system whose name starts
        the same way would look identical. Removal is refused until the platform
        database is reachable — <strong>nothing has been removed</strong>. Try the
        preview again in a moment.
      </p>
    </section>

    <!-- Platform agents that matched and can never be removed -------------- -->
    <section
      v-if="preview.excluded?.length"
      class="rounded-lg border border-gray-200 dark:border-gray-750 bg-gray-50 dark:bg-gray-900/40 p-4"
    >
      <h4 class="text-sm font-semibold text-gray-800 dark:text-gray-200">
        Protected — matched the name, will not be removed
      </h4>
      <ul class="mt-2 space-y-1">
        <li
          v-for="e in preview.excluded"
          :key="e.name"
          class="text-sm font-mono text-gray-600 dark:text-gray-300"
        >
          {{ e.name }}
        </li>
      </ul>
      <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">
        Platform agents are never included in a system teardown.
      </p>
    </section>

    <!-- The removal set, as a checklist ------------------------------------ -->
    <section>
      <div class="flex items-baseline justify-between gap-3 mb-2">
        <h4 class="text-sm font-semibold text-gray-800 dark:text-gray-200">
          Agents to remove
        </h4>
        <!-- Stated total beside a bounded list (principle 28). -->
        <span class="text-xs text-gray-500 dark:text-gray-400 tabular-nums">
          {{ checkedCount }} of {{ members.length }} selected
        </span>
      </div>

      <p v-if="!members.length" class="text-sm text-gray-600 dark:text-gray-400">
        This system has no removable members.
      </p>

      <!-- Bounded viewport: a fleet is unbounded data (principle 28). -->
      <ul
        v-else
        class="max-h-80 overflow-y-auto divide-y divide-gray-200 dark:divide-gray-750 rounded-lg border border-gray-200 dark:border-gray-750"
      >
        <li v-for="m in members" :key="m.name" class="p-3">
          <label class="flex items-start gap-3 cursor-pointer">
            <input
              type="checkbox"
              class="mt-0.5 rounded border-gray-300 dark:border-gray-600 text-action-primary-600 focus:ring-action-primary-500"
              :checked="isChecked(m.name)"
              :data-testid="`member-${m.name}`"
              @change="toggle(m.name, $event.target.checked)"
            />
            <span class="min-w-0 flex-1">
              <span class="flex flex-wrap items-center gap-2">
                <span class="font-mono text-sm text-gray-900 dark:text-gray-100 break-all">
                  {{ m.name }}
                </span>
                <!-- Shape as well as colour (principle 24): each badge is one fact. -->
                <BaseBadge v-if="m.evidence === 'prefix'" variant="warning">
                  matched by name only
                </BaseBadge>
                <BaseBadge v-if="m.is_ephemeral" variant="danger">
                  no recovery
                </BaseBadge>
                <BaseBadge v-if="m.status" :variant="m.status === 'running' ? 'success' : 'neutral'">
                  {{ m.status }}
                </BaseBadge>
              </span>
              <span
                v-if="m.evidence === 'prefix'"
                class="mt-1 block text-xs text-status-warning-700 dark:text-status-warning-300"
              >
                No deploy tagged this agent as part of “{{ preview.system_name }}”.
                It may belong to a different system — uncheck it if so.
              </span>
              <span
                v-else-if="m.is_ephemeral"
                class="mt-1 block text-xs text-status-danger-700 dark:text-status-danger-300"
              >
                Ephemeral agent — discarded permanently, with no recovery window.
              </span>
              <span
                v-if="m.template"
                class="mt-1 block text-xs text-gray-500 dark:text-gray-400 break-all"
              >
                {{ m.template }}
              </span>
            </span>
          </label>
        </li>
      </ul>
    </section>

    <!-- What else goes ------------------------------------------------------ -->
    <section v-if="preview.system_views?.length || preview.tag">
      <h4 class="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-2">
        Also removed
      </h4>
      <ul class="space-y-1.5 text-sm">
        <li
          v-for="v in preview.system_views || []"
          :key="v.id"
          class="flex flex-wrap items-center gap-2 text-gray-700 dark:text-gray-300"
        >
          <span>System view <strong>{{ v.name }}</strong></span>
          <BaseBadge v-if="v.outcome === 'skipped'" variant="warning">
            kept — {{ v.reason || 'not removable' }}
          </BaseBadge>
        </li>
        <li v-if="preview.tag" class="text-gray-700 dark:text-gray-300">
          The <strong>{{ preview.tag.name }}</strong> tag goes with the agents
          ({{ preview.tag.member_count }} tagged) — it is not a separate record to delete.
        </li>
      </ul>
    </section>

    <!-- Notes the server raised -------------------------------------------- -->
    <section v-if="otherWarnings.length">
      <h4 class="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-2">Notes</h4>
      <ul class="space-y-1 text-sm text-gray-600 dark:text-gray-300 list-disc list-inside">
        <!-- Plain text, never v-html (H-005). -->
        <li v-for="w in otherWarnings" :key="w">{{ w }}</li>
      </ul>
    </section>

    <!-- Consequence + acknowledgement -------------------------------------- -->
    <section
      class="rounded-lg border-2 border-status-warning-400 dark:border-status-warning-600 bg-status-warning-50 dark:bg-status-warning-900/20 p-4"
    >
      <h4 class="font-semibold text-status-warning-900 dark:text-status-warning-100 flex items-center gap-2">
        <span aria-hidden="true">⚠️</span> This removes running agents
      </h4>
      <ul class="mt-2 space-y-1 text-sm text-status-warning-800 dark:text-status-warning-200 list-disc list-inside">
        <li>
          <strong>{{ checkedCount }} agent{{ checkedCount === 1 ? '' : 's' }}</strong>
          will be removed and their containers destroyed. Schedules stop firing.
        </li>
        <li v-if="recoverableCount">
          {{ recoverableCount }} can be restored by an admin within the recovery
          window — <strong>as records only</strong>. The containers are not rebuilt.
        </li>
        <li v-if="ephemeralCount">
          {{ ephemeralCount }} {{ ephemeralCount === 1 ? 'is' : 'are' }} ephemeral and
          <strong>cannot be recovered at all</strong>.
        </li>
      </ul>
      <p v-if="preview.recovery" class="mt-2 text-xs text-status-warning-800 dark:text-status-warning-200">
        {{ preview.recovery }}
      </p>
      <!-- Same CONTRACT as ManifestPreview.vue's ack — the `:acknowledged` prop
           and the `update:acknowledged` emit — but a distinct test id. Install
           and remove can both be previewed on this one page, so a shared
           `data-testid` would be two elements at one address: strict-mode
           ambiguous for any Playwright `getByTestId('ack-checkbox')`, which is
           how the existing system-install spec addresses it. The pattern is
           worth sharing; the address is not. -->
      <label class="mt-3 flex items-start gap-2 cursor-pointer">
        <input
          type="checkbox"
          :checked="acknowledged"
          class="mt-0.5 rounded border-gray-300 dark:border-gray-600"
          data-testid="teardown-ack-checkbox"
          @change="$emit('update:acknowledged', $event.target.checked)"
        />
        <span class="text-sm font-medium text-status-warning-900 dark:text-status-warning-100">
          I understand and want to remove these agents
        </span>
      </label>
    </section>
  </div>
</template>

<script setup>
/**
 * Dry-run preview for a system teardown (trinity-enterprise#454, AC #1).
 *
 * Renders the backend's own preview and nothing else — membership, evidence,
 * the ephemeral flag, the view and the tag are all resolved server-side by the
 * same code that will perform the removal, so what is previewed is what runs.
 * Nothing here infers membership from names: that inference is exactly the bug
 * the shared predicate exists to prevent.
 *
 * It is a CHECKLIST, not a list, and that is load-bearing rather than cosmetic.
 * A member whose `evidence` is `prefix` matched by name with no deploy tag to
 * confirm it, so it may belong to a sibling system whose name shares this
 * one's prefix. The server cannot tell — only the operator can — so the badge
 * plus the opt-out is the mechanism, and the confirmed list is sent back on
 * execute and intersected with freshly-resolved membership.
 *
 * The acknowledgement reuses ent#126's `:acknowledged` / `update:acknowledged`
 * contract deliberately: one pattern for "restate the consequence and make the
 * user affirm it", not two. Its `data-testid` is deliberately NOT shared — see
 * the note at the control itself.
 *
 * All server-derived text renders as plain text, never v-html (H-005).
 */
import { computed } from 'vue'
import BaseBadge from '../base/BaseBadge.vue'

const props = defineProps({
  preview: { type: Object, required: true },
  acknowledged: { type: Boolean, default: false },
  // The confirmed set, owned by the parent so Remove can gate on it.
  checked: { type: Array, default: () => [] }
})

const emit = defineEmits(['update:acknowledged', 'update:checked'])

const members = computed(() => props.preview.members || [])
const checkedCount = computed(() => props.checked.length)

const recoverableCount = computed(
  () => members.value.filter((m) => props.checked.includes(m.name) && !m.is_ephemeral).length
)
const ephemeralCount = computed(
  () => members.value.filter((m) => props.checked.includes(m.name) && m.is_ephemeral).length
)

/**
 * Warnings not already rendered as their own panel.
 *
 * The server raises a warning for the unverified read, the excluded agents and
 * the ephemeral members, and each of those has a dedicated block above. Showing
 * the prose copy as well would say everything twice, which trains people to
 * skip the panel that matters.
 */
const otherWarnings = computed(() => {
  const w = props.preview.warnings || []
  return w.filter(
    (line) => !/could not be verified|were excluded|no recovery window/i.test(line)
  )
})

function isChecked (name) {
  return props.checked.includes(name)
}

function toggle (name, on) {
  const next = on
    ? [...props.checked, name]
    : props.checked.filter((n) => n !== name)
  emit('update:checked', next)
}
</script>
