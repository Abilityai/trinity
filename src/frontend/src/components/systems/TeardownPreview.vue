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
      <!-- What is true in BOTH arms. `membership_verified` is one boolean over
           two independent faults (`tags_readable AND roster_complete`), and it
           cannot say which fired — so naming one of them here, as this did
           with the tag read, is a false statement on the other half of the
           flag: a failed OWNERSHIP read leaves the tags perfectly readable and
           the LIST incomplete instead. The cause is quoted from the server
           below rather than guessed at. -->
      <p class="mt-1 text-sm text-status-danger-800 dark:text-status-danger-200">
        The removal set below could not be confirmed against the platform
        database, so it may not be this system's real membership — a different
        system whose name starts the same way can look identical.
      </p>

      <!-- The cause, in the server's own words. Same choice the recovery
           statement makes: the server owns it, the UI does not paraphrase. -->
      <ul
        v-if="membershipWarnings.length"
        class="mt-2 space-y-1 text-sm text-status-danger-800 dark:text-status-danger-200 list-disc list-inside"
        data-testid="teardown-membership-fault"
      >
        <!-- Plain text, never v-html (H-005). -->
        <li v-for="w in membershipWarnings" :key="w">{{ w }}</li>
      </ul>

      <!-- The advice has to match the fault. Neither read fails as a blip:
           "try again in a moment" produced the identical screen forever and
           trained the operator to read a real stop as flakiness. What happened,
           what it means, what to do (principle 25) — and the one offer that
           does work is the per-agent path the server's own refusal names. -->
      <p class="mt-2 text-sm text-status-danger-800 dark:text-status-danger-200">
        Removal is refused while that is true — <strong>nothing has been
        removed</strong>. This does not clear on its own: the platform database
        needs attention first. Until then, remove agents one at a time from the
        agent list.
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

      <!-- Why the count above is deliberately short of the total. Without this
           the unticked rows read as a miscount, and an operator who re-ticks
           them to "fix" it has undone the safeguard by hand. -->
      <p
        v-if="unconfirmed.length"
        class="mb-2 text-xs text-status-warning-700 dark:text-status-warning-300"
        data-testid="teardown-unconfirmed-note"
      >
        {{ unconfirmed.length }} of these matched “{{ preview.system_name }}” by
        <strong>name only</strong>, so
        {{ unconfirmed.length === 1 ? 'it starts' : 'they start' }} unticked.
        Tick one only if you know it belongs to this system.
      </p>

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
                <BaseBadge v-if="!isConfirmedMember(m)" variant="warning">
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
                v-if="!isConfirmedMember(m)"
                class="mt-1 block text-xs text-status-warning-700 dark:text-status-warning-300"
              >
                No deploy tagged this agent as part of “{{ preview.system_name }}”.
                It may belong to a different system — tick it only if it belongs
                to this one.
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
        <!-- No count here. `SystemTeardownTag.member_count` is `len(members)`
             — every candidate, tagged or matched by name — so calling it
             "N tagged" overstates the tag whenever any member is `prefix`,
             and in the refusal state it quotes a figure from the very read
             the banner above says failed. The total the operator needs is
             already stated on the list ("N of M selected"); the sentence's
             job is that the tag is not its own deletable record. -->
        <li v-if="preview.tag" class="text-gray-700 dark:text-gray-300">
          The <strong>{{ preview.tag.name }}</strong> tag goes with the agents —
          it is not a separate record to delete.
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
      <!-- `text-sm` like every other h4 in this file, including the danger
           callout at the top which has the identical anatomy. Written without a
           size class it fell through to the UA default (1em = 16px), which made
           the DEEPEST heading on the page the LARGEST — bigger than its four
           siblings and bigger than the h3 that contains them all. -->
      <h4 class="text-sm font-semibold text-status-warning-900 dark:text-status-warning-100 flex items-center gap-2">
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
 * one's prefix. The server cannot tell — only the operator can — so such a
 * member arrives UNTICKED and is an opt-IN, while a confirmed member is
 * ticked and an opt-out. The confirmed list is sent back on execute and
 * intersected with freshly-resolved membership.
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
import { isConfirmedMember } from '../../stores/systems'

const props = defineProps({
  preview: { type: Object, required: true },
  acknowledged: { type: Boolean, default: false },
  // The confirmed set, owned by the parent so Remove can gate on it.
  checked: { type: Array, default: () => [] }
})

const emit = defineEmits(['update:acknowledged', 'update:checked'])

const members = computed(() => props.preview.members || [])
const checkedCount = computed(() => props.checked.length)

/**
 * Members the server matched by NAME with no deploy tag to confirm them.
 *
 * These start unticked — the decision and its reasoning live in the store's
 * `teardownDefaultSelection`, which the parent applies. The panel and the store
 * must not disagree about which members it covers, so both now read the SAME
 * exported predicate rather than two hand-mirrored polarities: this list is
 * exactly the complement of the pre-ticked one, including for an `evidence`
 * value neither was written for.
 */
const unconfirmed = computed(() => members.value.filter((m) => !isConfirmedMember(m)))

const recoverableCount = computed(
  () => members.value.filter((m) => props.checked.includes(m.name) && !m.is_ephemeral).length
)
const ephemeralCount = computed(
  () => members.value.filter((m) => props.checked.includes(m.name) && m.is_ephemeral).length
)

/**
 * Warning routing. Every server line lands in exactly ONE place.
 *
 * `MEMBERSHIP_FAULT` is the pair behind `membership_verified`: a failed tag
 * read and an incomplete roster. Both belong INSIDE the refusal banner as its
 * cause — the roster line used to fall through to "Notes", which put the
 * specific fault in a footnote underneath a banner that named the other one.
 *
 * `SUPPRESSED` is the pair that already has a dedicated block above (the
 * protected-agents list, the per-member `no recovery` badge). Repeating the
 * prose trains people to skip the panel that matters.
 *
 * The two predicates partition the list, so a line can neither be shown twice
 * nor silently dropped.
 */
const MEMBERSHIP_FAULT = /could not be verified|roster could not be completed/i
// `matched by NAME ONLY` joined this list the moment the short-selection note
// gave that warning a dedicated block. Left in "Notes" it was not merely
// duplicated — the server's sentence ends "Uncheck anything that does not
// belong before confirming", which is the OPT-OUT instruction this change
// reversed. The screen said tick and untick about the same row, six lines
// apart, and the stale half read as the authoritative one because it came
// from the server.
const SUPPRESSED = /were excluded|no recovery window|matched by NAME ONLY/i

/**
 * Claimed by the banner — and ONLY while the banner is actually rendering.
 *
 * The server only raises these alongside `membership_verified: false`, so the
 * guard is belt-and-braces. It is there so the rule is "one line, one place"
 * rather than "one line, one place, assuming the server keeps a coupling this
 * file cannot see": a line the banner does not take falls through to Notes
 * below instead of disappearing from the screen entirely.
 */
const membershipWarnings = computed(
  () => (props.preview.membership_verified === false
    ? (props.preview.warnings || []).filter((line) => MEMBERSHIP_FAULT.test(line))
    : [])
)

const otherWarnings = computed(
  () => (props.preview.warnings || []).filter(
    (line) => !SUPPRESSED.test(line) && !membershipWarnings.value.includes(line)
  )
)

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
