<template>
  <section data-testid="portal-agent-decisions">
    <div class="flex items-center justify-between gap-2 mb-2">
      <h2 class="text-[11px] font-semibold uppercase tracking-wide text-gray-400">Decisions</h2>
      <BaseButton
        v-if="page && page.can_record && !recording"
        size="sm"
        variant="secondary"
        data-testid="portal-decision-record-open"
        @click="openForm()"
      >Record a decision</BaseButton>
    </div>

    <!-- Loading: reserve the space, never optimistic. -->
    <div v-if="!store.decisionsLoaded && !store.decisionsError" class="space-y-2" aria-busy="true">
      <div class="h-4 w-2/3 rounded bg-gray-200 dark:bg-gray-800 animate-pulse"></div>
      <div class="h-12 rounded bg-gray-200 dark:bg-gray-800 animate-pulse"></div>
    </div>

    <InlineError v-else-if="store.decisionsError" :message="store.decisionsError" retryable @retry="load" />

    <template v-else-if="page">
      <!-- Evidence line: conversion, not volume. -->
      <p v-if="page.stats.recorded" class="text-[12.5px] text-gray-400 mb-2" data-testid="portal-decisions-stats">
        {{ page.stats.recorded }} recorded · {{ page.stats.reused }} reused
        <template v-if="page.stats.reversed"> · {{ page.stats.reversed }} reversed</template>
        <template v-for="c in page.stats.ask_classes" :key="c.ask_class">
          · <span class="font-mono">{{ c.ask_class }}</span>
          <BaseBadge v-if="c.stable" variant="success" dot>stable</BaseBadge>
          <span v-else>{{ c.count }} · {{ c.criteria.length }} criteria<template v-if="c.reversals">, {{ c.reversals }} reversed</template></span>
        </template>
      </p>

      <!-- The record form -->
      <form v-if="recording" class="rounded-xl border border-gray-200 dark:border-gray-800 px-3 py-3 mb-3 space-y-2"
            data-testid="portal-decision-form" @submit.prevent="submit">
        <div class="grid gap-2 sm:grid-cols-2">
          <BaseSelect v-model="form.outcome" label="Outcome" :error="fieldError('outcome')">
            <option value="approved">Approved</option>
            <option value="deferred">Deferred</option>
            <option value="killed">Killed</option>
          </BaseSelect>
          <BaseInput v-model="form.review_by" type="date" label="Review by" :error="fieldError('review_by')"
                     help="It expires unless reconfirmed" />
        </div>
        <BaseInput v-model="form.decided" label="What was decided" :error="fieldError('decided')" placeholder="One line" />
        <BaseInput v-model="form.criterion" label="The criterion that decided it" :error="fieldError('criterion')"
                   help="The reusable part — what made the winner win" />
        <div>
          <BaseInput v-model="altDraft" label="Alternatives that were live" :error="fieldError('alternatives')"
                     help="A decision with no alternatives is a note" placeholder="Type one, press Enter"
                     @keydown.enter.prevent="addAlternative" />
          <div v-if="form.alternatives.length" class="mt-1 flex flex-wrap gap-1">
            <button v-for="(a, i) in form.alternatives" :key="a" type="button"
                    class="rounded-full border border-gray-300 dark:border-gray-700 px-2 py-0.5 text-[11.5px]"
                    :title="'Remove ' + a" data-testid="portal-decision-alternative" @click="form.alternatives.splice(i, 1)">
              {{ a }} ×
            </button>
          </div>
        </div>
        <BaseInput v-model="form.reversal" label="What would reverse it" :error="fieldError('reversal')" placeholder="One line" />
        <div class="grid gap-2 sm:grid-cols-2">
          <BaseInput v-model="form.ask_class" label="Ask class (optional)" :error="fieldError('ask_class')"
                     placeholder="vendor-approval" help="Groups the graduation evidence" />
          <BaseSelect v-model="form.scope" label="Scope" :error="fieldError('scope')"
                      help="Direction (pricing, positioning, roadmap) is routed to canon">
            <option value="seat">Seat decision</option>
            <option value="direction">Direction — route to canon</option>
          </BaseSelect>
        </div>
        <BaseTextarea v-model="form.notes" label="Notes (optional)" :error="fieldError('notes')"
                      help="The reasoning and the trade-off — the only free prose" :rows="2" />
        <InlineError v-if="store.decisionError && !store.decisionError.decisionId" class="mt-1"
                     :message="store.decisionError.message" data-testid="portal-decision-refusal"
                     @dismiss="store.decisionError = null" />
        <div class="flex items-center gap-2">
          <BaseButton type="submit" size="sm" variant="primary" data-testid="portal-decision-submit"
                      :loading="store.decisionBusy === 'record'" loading-label="Recording…">Record</BaseButton>
          <BaseButton type="button" size="sm" variant="ghost" @click="recording = false">Cancel</BaseButton>
        </div>
      </form>

      <p v-if="hint" class="mb-2 text-xs text-status-warning-700 dark:text-status-warning-300" data-testid="portal-decision-hint">{{ hint }}</p>

      <!-- Empty state teaches the next action -->
      <p v-if="!page.decisions.length && !recording" class="text-sm text-gray-400" data-testid="portal-decisions-empty">
        Nothing recorded yet. When you and this companion approve, defer or kill something, record why — the
        criterion is what compounds. Your companion can record one too (record_decision).
      </p>

      <!-- Active + expired + routed -->
      <ul v-if="live.length" class="space-y-1.5">
        <li v-for="d in live" :key="d.id"
            class="rounded-lg border border-gray-200 dark:border-gray-800 px-3 py-2 text-[12.5px]"
            :data-testid="'portal-decision-' + d.status">
          <div class="flex items-center gap-2 flex-wrap">
            <BaseBadge :variant="outcomeVariant(d.outcome)">{{ d.outcome }}</BaseBadge>
            <span class="font-medium">{{ d.decided }}</span>
            <BaseBadge v-if="d.status === 'expired'" variant="warning" dot>expired</BaseBadge>
            <BaseBadge v-else-if="d.status === 'routed'" variant="warning" dot>routed to canon</BaseBadge>
            <span v-if="d.seat !== page.my_seat" class="text-[11px] text-gray-400">seat · {{ d.seat }}</span>
          </div>
          <p class="mt-0.5"><span class="text-gray-400">Because</span> {{ d.criterion }}</p>
          <p class="text-gray-400 text-[11.5px]">
            Instead of {{ d.alternatives.join(' / ') }} · reverses if {{ d.reversal }}
          </p>
          <p class="text-gray-400 text-[11px]">
            {{ d.decided_by.role ? d.decided_by.role + ' · ' : '' }}{{ d.decided_by.person }} · {{ relative(d.decided_at) }}
            · review by {{ d.review_by }}
            <template v-if="d.ask_class"> · <span class="font-mono">{{ d.ask_class }}</span></template>
            <template v-if="d.cites.length"> · cites {{ d.cites.length }}</template>
          </p>
          <p v-if="d.notes" class="mt-0.5 text-gray-500 dark:text-gray-400 text-[11.5px] whitespace-pre-wrap">{{ d.notes }}</p>
          <InlineError v-if="store.decisionError && store.decisionError.decisionId === d.id" class="mt-1"
                       :message="store.decisionError.message" @dismiss="store.decisionError = null" />
          <div v-if="d.writable && d.status !== 'routed' && inline.id !== d.id" class="mt-1.5 flex flex-wrap gap-1.5">
            <BaseButton size="sm" variant="secondary" :data-testid="'portal-decision-reconfirm-' + d.id"
                        @click="openInline('reconfirm', d)">Reconfirm</BaseButton>
            <BaseButton size="sm" variant="secondary" :data-testid="'portal-decision-correct-' + d.id"
                        @click="openForm(d)">Correct</BaseButton>
            <BaseButton size="sm" variant="ghost" :data-testid="'portal-decision-close-' + d.id"
                        @click="askClose(d)">Close</BaseButton>
            <BaseButton size="sm" variant="ghost" :data-testid="'portal-decision-reverse-' + d.id"
                        @click="openInline('reverse', d)">Reverse</BaseButton>
          </div>
          <!-- Reverse needs a reason, reconfirm a date: asked inline, next to the record. -->
          <form v-if="inline.id === d.id" class="mt-1.5 flex flex-wrap items-end gap-2"
                :data-testid="'portal-decision-inline-' + inline.action" @submit.prevent="submitInline">
            <BaseInput v-if="inline.action === 'reverse'" v-model="inline.reason" label="What changed"
                       placeholder="One line — this is the evidence" data-testid="portal-decision-reason" />
            <BaseInput v-else v-model="inline.review_by" type="date" label="New review date" data-testid="portal-decision-review-by" />
            <BaseButton type="submit" size="sm" variant="primary" data-testid="portal-decision-inline-submit"
                        :loading="store.decisionBusy === d.id">{{ inline.action === 'reverse' ? 'Reverse' : 'Reconfirm' }}</BaseButton>
            <BaseButton type="button" size="sm" variant="ghost" @click="inline.id = null">Cancel</BaseButton>
          </form>
        </li>
      </ul>

      <!-- History: superseded / closed / reversed, collapsed -->
      <details v-if="history.length" class="mt-2">
        <summary class="cursor-pointer text-[11.5px] text-gray-400" data-testid="portal-decisions-history">
          History · {{ history.length }}
        </summary>
        <ul class="mt-1 space-y-1">
          <li v-for="d in history" :key="d.id" class="text-[11.5px] text-gray-400 px-3"
              :data-testid="'portal-decision-' + d.status">
            <BaseBadge variant="neutral">{{ d.status }}</BaseBadge>
            {{ d.decided }} <span v-if="d.close_reason">— {{ d.close_reason }}</span>
            <span v-if="d.status === 'superseded'"> · corrected</span>
          </li>
        </ul>
      </details>

      <ConfirmDialog
        v-model:visible="confirm.open"
        title="Close this decision?"
        message="It is no longer live. It stays in history; nothing else changes."
        confirm-text="Close"
        variant="warning"
        @confirm="confirmClose"
      />
    </template>
  </section>
</template>

<script setup>
/**
 * ent#638 — the seat decision record (Tandem R25): why a thing was approved,
 * deferred or killed, by whom, on which criterion, and what would reverse it.
 * The person records, corrects (supersede), reconfirms, closes or reverses
 * their own seat's decisions; the companion records over MCP. Mounted and
 * proven by tests/unit/portalAgentDecisions.spec.js (#2918).
 */
import { computed, reactive, ref, watch, onMounted } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import InlineError from '@/components/InlineError.vue'
import ConfirmDialog from '@/components/ConfirmDialog.vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseInput from '@/components/base/BaseInput.vue'
import BaseSelect from '@/components/base/BaseSelect.vue'
import BaseTextarea from '@/components/base/BaseTextarea.vue'

const props = defineProps({
  agentName: { type: String, required: true },
})

const store = useClientPortalStore()
const mine = computed(() => store.decisionsAgent === props.agentName)
const page = computed(() => (mine.value ? store.decisions : null))
const live = computed(() => (page.value?.decisions || []).filter((d) => ['active', 'expired', 'routed'].includes(d.status)))
const history = computed(() => (page.value?.decisions || []).filter((d) => !['active', 'expired', 'routed'].includes(d.status)))

const recording = ref(false)
const correcting = ref(null)     // decision id being superseded
const altDraft = ref('')
const hint = ref(null)
const blank = () => ({
  outcome: 'approved', decided: '', alternatives: [], criterion: '', reversal: '',
  review_by: defaultReviewBy(), scope: 'seat', notes: '', ask_class: '',
})
const form = reactive(blank())

function defaultReviewBy() {
  const d = new Date(Date.now() + 90 * 86400_000)
  return d.toISOString().slice(0, 10)
}

function openForm(decision = null) {
  Object.assign(form, blank())
  correcting.value = decision ? decision.id : null
  if (decision) {
    Object.assign(form, {
      outcome: decision.outcome, decided: decision.decided, alternatives: [...decision.alternatives],
      criterion: decision.criterion, reversal: decision.reversal, review_by: decision.review_by,
      notes: decision.notes || '', ask_class: decision.ask_class || '',
    })
  }
  altDraft.value = ''
  hint.value = null
  store.decisionError = null
  recording.value = true
}

function addAlternative() {
  const v = altDraft.value.trim()
  if (v && !form.alternatives.includes(v)) form.alternatives.push(v)
  altDraft.value = ''
}

function fieldError(name) {
  const e = store.decisionError
  return e && !e.decisionId && e.fields && e.fields[name] ? e.fields[name] : ''
}

async function submit() {
  if (altDraft.value.trim()) addAlternative()
  const body = {
    outcome: form.outcome, decided: form.decided, alternatives: form.alternatives, criterion: form.criterion,
    reversal: form.reversal, review_by: form.review_by, scope: form.scope,
    notes: form.notes || null, ask_class: form.ask_class || null,
  }
  const result = correcting.value
    ? await store.actOnSeatDecision(props.agentName, correcting.value, { action: 'supersede', fields: body })
    : await store.recordSeatDecision(props.agentName, body)
  if (result) {
    recording.value = false
    correcting.value = null
    hint.value = result.hint || null
  }
}

const confirm = reactive({ open: false, decision: null })
const inline = reactive({ id: null, action: null, reason: '', review_by: '' })

function askClose(decision) {
  confirm.decision = decision
  confirm.open = true
}

function confirmClose() {
  return store.actOnSeatDecision(props.agentName, confirm.decision.id, { action: 'close' })
}

function openInline(action, decision) {
  Object.assign(inline, { id: decision.id, action, reason: '', review_by: defaultReviewBy() })
  store.decisionError = null
}

async function submitInline() {
  const body = { action: inline.action }
  if (inline.action === 'reverse') body.reason = inline.reason
  if (inline.action === 'reconfirm') body.review_by = inline.review_by
  const result = await store.actOnSeatDecision(props.agentName, inline.id, body)
  if (result) inline.id = null
}

function outcomeVariant(outcome) {
  return { approved: 'success', deferred: 'warning', killed: 'danger' }[outcome] || 'neutral'
}

function load() { return store.loadAgentDecisions(props.agentName) }
watch(() => props.agentName, load)
onMounted(load)

function relative(iso) {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const mins = Math.round((Date.now() - then) / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.round(hrs / 24)
  return days < 30 ? `${days}d ago` : new Date(iso).toLocaleDateString()
}
</script>
