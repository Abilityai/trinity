<template>
  <section v-if="page && (page.classes.length || page.other_seats.length)" data-testid="portal-agent-autonomy">
    <div class="flex items-center justify-between gap-2 mb-2">
      <h2 class="text-[11px] font-semibold uppercase tracking-wide text-gray-400">What it may do unprompted</h2>
      <BaseBadge :variant="page.ceiling_allows_unprompted ? 'success' : 'neutral'" dot
                 data-testid="portal-autonomy-level">{{ page.level }}</BaseBadge>
    </div>

    <!-- The two instance-wide facts, said once, so a class does not have to
         repeat them per row. -->
    <p class="text-[12.5px] text-gray-400 mb-2" data-testid="portal-autonomy-ceiling">
      {{ page.level_label }}
      <template v-if="!page.ceiling_allows_unprompted">
        · nothing runs unprompted below L2
      </template>
    </p>
    <InlineError
      v-if="!page.agent_autonomy_enabled"
      class="mb-2"
      data-testid="portal-autonomy-hard-off"
      message="This agent's autonomy switch is off — the hard off. Nothing runs unprompted while it is, whatever a class has earned."
    />

    <ul class="space-y-1.5">
      <li v-for="c in page.classes" :key="c.ask_class"
          class="rounded-lg border border-gray-200 dark:border-gray-800 px-3 py-2 text-[12.5px]"
          :data-testid="'portal-autonomy-class-' + c.state">
        <div class="flex items-center gap-2 flex-wrap">
          <span class="font-mono text-[11.5px]">{{ c.ask_class }}</span>
          <BaseBadge :variant="c.unprompted ? 'success' : 'neutral'" dot>
            {{ c.unprompted ? 'unprompted' : 'on-request' }}
          </BaseBadge>
          <BaseBadge v-if="c.held" variant="warning" dot>held</BaseBadge>
          <BaseBadge v-if="c.guard_metric === 'capped'" variant="warning">guard-capped</BaseBadge>
          <span v-if="!c.unprompted && c.earned_state === 'graduated'" class="text-[11px] text-gray-400">
            earned — blocked by the dial
          </span>
        </div>

        <!-- Every reason, not the first: a person fixing one wants the rest. -->
        <ul v-if="c.blocked_by.length" class="mt-1 space-y-0.5" :data-testid="'portal-autonomy-blockers-' + c.ask_class">
          <li v-for="b in c.blocked_by" :key="b" class="text-gray-500 dark:text-gray-400">— {{ blockerText(b) }}</li>
        </ul>
        <p v-else class="mt-1 text-gray-400">
          {{ c.evidence.count }} decisions on one criterion, no reversals, no thumbs-down in
          {{ page.rating_window_days }} days.
        </p>

        <p v-if="c.evidence.criteria && c.evidence.criteria.length === 1" class="mt-0.5 text-gray-400">
          <span class="text-gray-400">Criterion ·</span> {{ c.evidence.criteria[0] }}
        </p>
        <p v-if="c.evidence_expires_at" class="text-[11px] text-gray-400">
          Evidence holds until {{ c.evidence_expires_at }}
        </p>

        <InlineError v-if="actionError && actionError.askClass === c.ask_class" class="mt-1"
                     :message="actionError.message" @dismiss="store.autonomyActionError = null" />

        <div v-if="c.writable" class="mt-1.5 flex flex-wrap gap-1.5">
          <BaseButton v-if="!c.held" size="sm" variant="ghost"
                      :data-testid="'portal-autonomy-hold-' + c.ask_class"
                      :loading="store.autonomyBusy === c.ask_class"
                      @click="act(c, 'hold')">Hold on-request</BaseButton>
          <BaseButton v-else-if="page.can_release" size="sm" variant="secondary"
                      :data-testid="'portal-autonomy-release-' + c.ask_class"
                      :loading="store.autonomyBusy === c.ask_class"
                      @click="act(c, 'release')">Release</BaseButton>
          <span v-else class="text-[11px] text-gray-400" data-testid="portal-autonomy-release-owner-only">
            only the agent's owner can release a hold
          </span>
        </div>
      </li>
    </ul>

    <!-- Other seats: the owner's view. Never rendered for anyone else. -->
    <details v-if="page.other_seats.length" class="mt-2">
      <summary class="cursor-pointer text-[11.5px] text-gray-400" data-testid="portal-autonomy-other-seats">
        Other seats · {{ page.other_seats.length }}
      </summary>
      <ul class="mt-1 space-y-1">
        <li v-for="c in page.other_seats" :key="c.seat + c.ask_class" class="text-[11.5px] text-gray-400 px-3">
          <span class="font-mono">{{ c.ask_class }}</span> · {{ c.seat }} ·
          {{ c.unprompted ? 'unprompted' : 'on-request' }}
        </li>
      </ul>
    </details>

    <p class="mt-2 text-[11px] text-gray-400" data-testid="portal-autonomy-note">
      A class graduates when its decisions apply one criterion repeatedly with no reversals and you have not
      rated the agent down — never because the ask is frequent. There is no promote button: you can hold a
      class, not grant it.
    </p>
  </section>
</template>

<script setup>
/**
 * ent#641 — the autonomy dial, per kind of ask (Tandem P12).
 *
 * The state a class shows is the LIVE verdict: what its decisions earned,
 * ANDed with the instance ceiling, the agent's autonomy switch and the clock.
 * `earned_state` is rendered separately where they differ, because "not yet
 * earned" and "earned but the dial is down" are different things to fix.
 * Mounted and proven by tests/unit/portalAgentAutonomy.spec.js (#2918).
 */
import { computed, watch, onMounted } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import InlineError from '@/components/InlineError.vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'

const props = defineProps({
  agentName: { type: String, required: true },
})

const store = useClientPortalStore()
const mine = computed(() => store.autonomyAgent === props.agentName)
const page = computed(() => (mine.value ? store.autonomy : null))
const actionError = computed(() => (mine.value ? store.autonomyActionError : null))

/** The server sends the vocabulary AND the sentence for each blocker, so the
 *  panel and the companion say the same words. An unknown code is shown raw
 *  rather than swallowed. */
function blockerText(code) {
  return (page.value && page.value.blocker_text && page.value.blocker_text[code]) || FALLBACK[code] || code
}

const FALLBACK = {
  level_below_l2: 'the instance dial is below L2, so nothing runs unprompted anywhere',
  agent_autonomy_off: "this agent's autonomy switch is off",
  evidence_expired: 'the decisions this was promoted on have passed their review date',
  too_few_records: 'fewer than three decisions on record for this kind of ask',
  criterion_not_stable: 'the decisions did not apply one consistent criterion',
  reversal_in_window: 'a decision in this window was reversed',
  negative_rating_in_window: 'this seat rated the agent down inside the rating window',
  guard_metric_capped: "this class moves a metric another role holds",
  held_by_operator: 'an operator is holding this class on-request',
}

function act(c, action) {
  return store.actOnAskClass(props.agentName, c.ask_class, { action, seat: c.seat })
}

function load() { return store.loadAgentAutonomy(props.agentName) }
watch(() => props.agentName, load)
onMounted(load)
</script>
