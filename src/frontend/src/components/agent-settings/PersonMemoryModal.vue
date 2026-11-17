<template>
  <!--
    One person's memory with this agent, for its owner (trinity-enterprise#962):
    Notes, Summary, History (paged, Undo on the latest write of each section,
    behind a confirm) and Facts (their retrieval-memory scope: each fact's
    journal, Forget behind a confirm, and search scoped to them).

    Every stored text here was written from conversations with that person, so
    it renders as plain text, never as markup. Long lists scroll inside their
    own region; the dialog scrolls inside, never the page.
  -->
  <BaseModal
    :model-value="modelValue"
    panel-class="relative flex w-full max-w-3xl max-h-[88vh] flex-col rounded-lg bg-white shadow-xl dark:bg-gray-800"
    :aria-label="email ? `What this agent remembers about ${email}` : 'Person memory'"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div ref="root" class="flex min-h-0 flex-1 flex-col" data-testid="person-memory-modal" @focusin="lastInside = true">
      <header class="flex items-start gap-3 border-b border-gray-200 px-6 py-4 dark:border-gray-750">
        <div class="min-w-0 flex-1">
          <h2 class="truncate text-lg font-semibold text-gray-900 dark:text-gray-100">{{ email }}</h2>
          <p class="text-[12.5px] text-gray-600 dark:text-gray-400">{{ subtitle }}</p>
        </div>
        <BaseButton variant="ghost" size="sm" data-testid="person-memory-close" @click="close">Close</BaseButton>
      </header>

      <div class="min-h-0 flex-1 space-y-6 overflow-y-auto px-6 py-4">
        <div v-if="personView.state === 'loading'" aria-busy="true">
          <SkeletonLoader variant="rows" :count="5" height="44px" gap="10px" />
          <span class="sr-only">Loading what this agent remembers about this person…</span>
        </div>

        <LoadFailed
          v-else-if="personView.state === 'failed'"
          dense
          title="Couldn't load this person's memory"
          :message="store.personError"
          @retry="store.openPerson(email)"
        />

        <template v-else>
          <!-- Notes -->
          <section aria-labelledby="pm-notes" data-testid="person-memory-notes">
            <h3 id="pm-notes" class="text-sm font-semibold text-gray-900 dark:text-gray-100">Notes</h3>
            <p class="mb-2 text-[12.5px] text-gray-600 dark:text-gray-400">What the agent wrote down about them.</p>
            <p v-if="person.notes" class="max-h-48 overflow-y-auto whitespace-pre-wrap break-words rounded-md border border-gray-200 px-3 py-2 text-sm text-gray-900 dark:border-gray-750 dark:text-gray-100">{{ person.notes }}</p>
            <p v-else class="text-[12.5px] text-gray-600 dark:text-gray-400">No notes.</p>
          </section>

          <!-- Summary -->
          <section aria-labelledby="pm-summary" data-testid="person-memory-summary">
            <h3 id="pm-summary" class="text-sm font-semibold text-gray-900 dark:text-gray-100">Summary</h3>
            <p class="mb-2 text-[12.5px] text-gray-600 dark:text-gray-400">
              Rebuilt from the latest messages every few turns, so an undone summary can come back while that conversation is recent.
            </p>
            <p v-if="person.summary" class="max-h-48 overflow-y-auto whitespace-pre-wrap break-words rounded-md border border-gray-200 px-3 py-2 text-sm text-gray-900 dark:border-gray-750 dark:text-gray-100">{{ person.summary }}</p>
            <p v-else class="text-[12.5px] text-gray-600 dark:text-gray-400">No summary.</p>
          </section>

          <!-- History -->
          <section aria-labelledby="pm-history" data-testid="person-memory-history">
            <h3 id="pm-history" class="text-sm font-semibold text-gray-900 dark:text-gray-100">History</h3>
            <p class="mb-2 text-[12.5px] text-gray-600 dark:text-gray-400 tabular-nums">
              Every change to the notes and the summary, newest first. Undo the latest change of each.
              <template v-if="store.writes.length"> · {{ store.writes.length }} shown</template>
            </p>
            <InlineError
              v-if="store.undoError"
              class="mb-2"
              :message="store.undoError"
              data-testid="person-memory-undo-error"
              @dismiss="store.undoError = ''"
            />
            <p v-if="!store.writes.length" class="text-[12.5px] text-gray-600 dark:text-gray-400">No changes recorded.</p>
            <ul
              v-else
              class="max-h-72 overflow-y-auto divide-y divide-gray-200 rounded-md border border-gray-200 dark:divide-gray-750 dark:border-gray-750"
              aria-label="Changes to this person's memory"
            >
              <li
                v-for="w in store.writes"
                :key="w.id"
                class="px-3 py-2"
                data-testid="person-memory-write"
              >
                <div class="flex items-start gap-2">
                  <span class="min-w-0 flex-1 text-[12.5px] text-gray-600 dark:text-gray-400">
                    <BaseBadge size="sm" :variant="w.section === 'summary' ? 'info' : 'neutral'">{{ w.section === 'summary' ? 'Summary' : 'Notes' }}</BaseBadge>
                    <span class="ml-1.5 font-medium text-gray-900 dark:text-gray-100">{{ writeLabel(w) }}</span>
                    · <span class="whitespace-nowrap" :title="formatLocalDateTime(w.written_at)">{{ formatRelativeTime(w.written_at) }}</span>
                    <template v-if="w.undone_at"> · {{ undoneLine(w) }}</template>
                  </span>
                  <BaseButton
                    v-if="w.undoable"
                    size="sm"
                    variant="secondary"
                    :loading="store.undoing === w.id"
                    loading-label="Undoing…"
                    :disabled="Boolean(store.undoing) && store.undoing !== w.id"
                    data-testid="person-memory-undo"
                    @click="askUndo(w)"
                  >Undo</BaseButton>
                </div>
                <p
                  class="mt-1 whitespace-pre-wrap break-words text-[12.5px]"
                  :class="w.undone_at ? 'text-gray-500 line-through decoration-gray-400 dark:text-gray-400' : 'text-gray-900 dark:text-gray-100'"
                >{{ w.text || '(cleared)' }}</p>
              </li>
            </ul>
            <div v-if="store.nextBefore" class="mt-2">
              <BaseButton
                variant="secondary"
                size="sm"
                :loading="store.writesLoadingMore"
                loading-label="Loading…"
                data-testid="person-memory-writes-more"
                @click="store.loadMoreWrites()"
              >Load more</BaseButton>
            </div>
            <InlineError
              v-if="store.writesMoreError"
              class="mt-2"
              :message="store.writesMoreError"
              @dismiss="store.writesMoreError = ''"
            />
          </section>

          <!-- Facts -->
          <section aria-labelledby="pm-facts" data-testid="person-memory-facts">
            <h3 id="pm-facts" class="text-sm font-semibold text-gray-900 dark:text-gray-100">Facts</h3>
            <p class="mb-2 text-[12.5px] text-gray-600 dark:text-gray-400">
              Retrieval memory kept from their conversations, recalled into the agent's prompt when relevant.
            </p>

            <p v-if="person.agent_scope" class="text-[12.5px] text-gray-600 dark:text-gray-400" data-testid="person-memory-agent-scope">
              This is the agent's owner. Their conversations feed the agent's own memory, shared with its owner — search it with Search memory in this section of Settings.
            </p>

            <template v-else>
              <div class="min-h-[64px]">
                <SkeletonLoader v-if="factsView.state === 'loading'" variant="rows" :count="2" height="36px" gap="8px" />
                <LoadFailed
                  v-else-if="factsView.state === 'failed'"
                  dense
                  title="Couldn't load this person's facts"
                  :message="store.factsError"
                  @retry="store.loadFacts()"
                />
                <p v-else-if="factsView.state === 'empty'" class="text-[12.5px] text-gray-600 dark:text-gray-400" data-testid="person-memory-facts-empty">
                  {{ factsEmptyReason }}
                </p>
                <template v-else>
                  <InlineError
                    v-if="store.forgetError"
                    class="mb-2"
                    :message="store.forgetError"
                    data-testid="person-memory-forget-error"
                    @dismiss="store.forgetError = ''"
                  />
                  <p class="mb-2 text-[12.5px] tabular-nums text-gray-600 dark:text-gray-400" data-testid="person-memory-facts-count">
                    {{ store.facts.length }} fact{{ store.facts.length === 1 ? '' : 's' }}{{ store.factsHasMore ? ' shown' : '' }} · newest first
                  </p>
                  <ul
                    class="max-h-72 overflow-y-auto divide-y divide-gray-200 rounded-md border border-gray-200 dark:divide-gray-750 dark:border-gray-750"
                    aria-label="Facts kept about this person"
                  >
                    <li v-for="f in store.facts" :key="f.id" class="px-3 py-2" data-testid="person-memory-fact">
                      <div class="flex items-start gap-2">
                        <div class="min-w-0 flex-1">
                          <p class="break-words text-sm text-gray-900 dark:text-gray-100">{{ f.text }}</p>
                          <p class="mt-0.5 text-[12.5px] text-gray-600 dark:text-gray-400">
                            <span :title="formatLocalDateTime(f.updated_at || f.created_at)">{{ formatRelativeTime(f.updated_at || f.created_at) }}</span><template v-if="f.source"> · {{ f.source }}</template><template v-if="f.recall_count"> · recalled {{ f.recall_count }}×</template>
                          </p>
                        </div>
                        <BaseButton
                          size="sm"
                          variant="ghost"
                          :aria-expanded="String(Boolean(open[f.id]))"
                          data-testid="person-memory-fact-history"
                          @click="toggleLineage(f.id)"
                        >History</BaseButton>
                        <BaseButton
                          size="sm"
                          variant="ghost"
                          :loading="store.forgetting === f.id"
                          loading-label="Forgetting…"
                          :disabled="Boolean(store.forgetting) && store.forgetting !== f.id"
                          data-testid="person-memory-forget"
                          @click="askForget(f)"
                        >Forget</BaseButton>
                      </div>
                      <div v-if="open[f.id]" class="mt-2 rounded-md bg-gray-50 px-3 py-2 dark:bg-gray-900" data-testid="person-memory-lineage">
                        <p v-if="lineageOf(f.id).state === 'loading'" class="text-[12.5px] text-gray-600 dark:text-gray-400">Loading this fact's history…</p>
                        <p v-else-if="lineageOf(f.id).state === 'failed'" class="text-[12.5px] text-gray-600 dark:text-gray-400">
                          Couldn't load this fact's history.
                          <button type="button" class="underline" @click="store.loadLineage(f.id)">Try again</button>
                        </p>
                        <p v-else-if="!lineageOf(f.id).events.length" class="text-[12.5px] text-gray-600 dark:text-gray-400">Nothing recorded for this fact.</p>
                        <ol v-else class="space-y-1">
                          <li v-for="ev in lineageOf(f.id).events" :key="ev.id" class="text-[12.5px] text-gray-600 dark:text-gray-400">
                            <span class="font-medium text-gray-900 dark:text-gray-100">{{ opLabel(ev.op) }}</span>
                            · <span :title="formatLocalDateTime(ev.created_at)">{{ formatRelativeTime(ev.created_at) }}</span>
                            <template v-if="ev.previous_text"> · was “{{ ev.previous_text }}”</template>
                            <template v-if="ev.op === 'search' && ev.candidate_text"> · for “{{ ev.candidate_text }}”</template>
                          </li>
                        </ol>
                      </div>
                    </li>
                  </ul>
                  <div v-if="store.factsHasMore" class="mt-2">
                    <BaseButton
                      variant="secondary"
                      size="sm"
                      :loading="store.factsLoadingMore"
                      loading-label="Loading…"
                      data-testid="person-memory-facts-more"
                      @click="store.loadMoreFacts()"
                    >Load more</BaseButton>
                  </div>
                </template>
              </div>

              <div class="mt-4">
                <RetrievalMemorySearch :agent-name="agentName" :user-email="email" :notify="notify" />
              </div>
            </template>
          </section>
        </template>
      </div>
    </div>

    <ConfirmDialog
      v-model:visible="confirmOpen"
      :title="pending.title"
      :message="pending.message"
      :confirm-text="pending.confirmText"
      cancel-text="Cancel"
      @confirm="onConfirm"
    />
  </BaseModal>
</template>

<script setup>
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { usePersonMemoryStore } from '../../stores/personMemory'
import { viewState } from '../../utils/loadingState'
import { formatLocalDateTime, formatRelativeTime } from '../../utils/timestamps'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseModal from '../base/BaseModal.vue'
import ConfirmDialog from '../ConfirmDialog.vue'
import InlineError from '../InlineError.vue'
import LoadFailed from '../LoadFailed.vue'
import SkeletonLoader from '../SkeletonLoader.vue'
import RetrievalMemorySearch from './RetrievalMemorySearch.vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  agentName: { type: String, required: true },
  notify: { type: Function, default: null },
})
const emit = defineEmits(['update:modelValue'])

const store = usePersonMemoryStore()
const root = ref(null)
const lastInside = ref(false)
const open = reactive({})
const confirmOpen = ref(false)
const pending = reactive({ kind: null, id: null, title: '', message: '', confirmText: '' })

const email = computed(() => store.personEmail || '')
const person = computed(() => store.person || { notes: '', summary: '', agent_scope: false })
const personView = computed(() => viewState({ hasLoaded: store.personLoaded, error: store.personError }))
const factsView = computed(() => viewState({
  hasLoaded: store.factsLoaded, error: store.factsError, count: store.facts.length,
}))

const CHANNELS = { telegram: 'Telegram', slack: 'Slack', whatsapp: 'WhatsApp' }

const subtitle = computed(() => {
  const p = store.person
  if (!p) return ''
  const parts = []
  if (p.channel) {
    let who = CHANNELS[p.channel] || p.channel
    if (p.channel_handle) who += ` @${String(p.channel_handle).replace(/^@/, '')}`
    if (p.channel_name) who += ` (${p.channel_name})`
    parts.push(`${who}, as the channel reported it`)
  }
  if (p.updated_at) parts.push(`memory updated ${formatRelativeTime(p.updated_at)}`)
  return parts.join(' · ')
})

/** The Facts empty reason reuses the Memory toggle's own wording when
 *  retrieval memory is off, so the two rows in one pane never disagree. */
const factsEmptyReason = computed(() => {
  const s = store.memoryStatus
  if (s && !s.enabled_platform) return 'No facts kept about this person. Retrieval memory: Platform switch is off.'
  if (s && s.opt_out) return 'No facts kept about this person. Retrieval memory: Kept out.'
  return 'No facts kept about this person.'
})

function writeLabel(w) {
  if (w.kind === 'summarizer') return 'Conversation summary'
  if (w.kind === 'scheduled_run') return w.schedule_name ? `Scheduled run · ${w.schedule_name}` : 'Scheduled run'
  return 'In a conversation'
}

function undoneLine(w) {
  if (!w.undone_by_operator) return 'undone by them'
  return w.undone_by ? `undone by ${w.undone_by}` : 'undone by an operator'
}

const OPS = {
  add: 'Kept', update: 'Rewritten', delete: 'Retired', none: 'Seen again, unchanged',
  screened: 'Screened out', prune: 'Pruned', recall: 'Recalled into a prompt',
  search: 'Found by a search', forget: 'Forgotten',
}
function opLabel(op) {
  return OPS[op] || op
}

function lineageOf(id) {
  return store.lineage[id] || { state: 'loading', events: [] }
}

function toggleLineage(id) {
  open[id] = !open[id]
  if (open[id] && !store.lineage[id]) store.loadLineage(id)
}

function askUndo(w) {
  const summary = w.section === 'summary'
  Object.assign(pending, {
    kind: 'undo',
    id: w.id,
    title: 'Undo this change?',
    message: summary
      ? "Their summary goes back to what it was before it. This can't be redone."
      : "Their notes go back to what they were before it. This can't be redone.",
    confirmText: 'Undo change',
  })
  confirmOpen.value = true
}

function askForget(f) {
  Object.assign(pending, {
    kind: 'forget',
    id: f.id,
    title: 'Forget this fact?',
    message: 'The agent will stop recalling it.',
    confirmText: 'Forget',
  })
  confirmOpen.value = true
}

async function onConfirm() {
  const { kind, id } = pending
  if (kind === 'undo') {
    const ok = await store.undo(id)
    if (ok && props.notify) props.notify('Change undone.', 'success')
  } else if (kind === 'forget') {
    const ok = await store.forget(id)
    if (ok && props.notify) props.notify('Fact forgotten.', 'success')
  }
}

function close() {
  emit('update:modelValue', false)
}

function focusSafe() {
  const el = root.value?.querySelector('[data-testid="person-memory-close"]')
  if (el && typeof el.focus === 'function') el.focus()
}

// Undo and Forget disable themselves while in flight and then unmount (the
// row is no longer undoable, the fact is gone), and the confirm hands focus
// back to that control. A disabled or removed control drops focus to <body>,
// outside the dialog that owns Esc and the Tab trap (learnings 2026-10-22):
// ONE post-flush watcher over every state that can do that re-homes focus on
// Close, the safe control that is always there.
watch(
  [() => store.undoing, () => store.forgetting, () => store.writes, () => store.facts, confirmOpen],
  () => {
    if (!props.modelValue || !root.value || confirmOpen.value || !lastInside.value) return
    const active = document.activeElement
    const lost = !active || active === document.body
      || (root.value.contains(active) && (active.disabled || active.getAttribute('aria-disabled') === 'true'))
    if (lost) focusSafe()
  },
  { flush: 'post' },
)

watch(() => props.modelValue, (isOpen) => {
  lastInside.value = false
  confirmOpen.value = false
  for (const k of Object.keys(open)) delete open[k]
  if (isOpen) nextTick(() => { lastInside.value = true })
})
</script>
