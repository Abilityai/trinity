<!--
  A project's "Needs you" — the body of the hub card (#3574).

  The Overview is a list, not a second Inbox pane. A pending ask is drawn as the
  Inbox's own list row — the agent, when, and the title on ONE line — and the
  full ask card (`PortalAsks`, the one answer surface) mounts only for a row the
  person opened with Answer, in place, with the exact in-place flow the Inbox
  has. Before #3574 the card mounted PortalAsks for every ask: a multi-paragraph
  approval ran for screens and, the hub being one grid, stretched the Metrics
  card beside it to match.

  Rows come from `projectNeedsYouRows` (projectsUtils): pending in urgency
  order; a row the person opened stays until collapsed even after its ask ends
  (PortalAsks draws the ending and the ent#468 confirmation); a row inside its
  Undo window (ent#748) is forced open, since Undo lives on the card.

  Gates, through `viewState` (utils/loadingState): the read failed (the project
  read's own verdict, or the Inbox list's — `asksFailed` / `asksAbsent` leave
  `asksLoaded` false, so a bare "not loaded" gate would skeleton forever) →
  loading (ids but no list yet — never "nothing waiting" before the read) →
  nothing waiting (on the ROWS, not the ids) → the rows.
-->
<template>
  <div data-testid="project-needs-you-body">
    <p v-if="view.state === 'failed'" class="mt-2 text-sm text-gray-600 dark:text-gray-300" data-testid="project-needs-you-failed">Couldn't read your Inbox just now. Your asks are still in the Inbox.</p>
    <div
      v-else-if="view.state === 'loading'"
      class="mt-2"
      aria-busy="true"
      data-testid="project-needs-you-loading"
    >
      <SkeletonLoader :count="2" height="1rem" />
      <span class="sr-only">Loading your asks</span>
    </div>
    <p v-else-if="view.state === 'empty'" class="mt-2 text-sm text-gray-600 dark:text-gray-300">Nothing on this project is waiting on you.</p>
    <ul v-else class="mt-2 space-y-2" data-testid="project-needs-you-rows">
      <li
        v-for="row in rows"
        :key="row.id"
        :data-testid="`project-ask-${row.id}`"
        :data-open="row.open ? 'true' : 'false'"
      >
        <template v-if="row.open">
          <!-- The one answer surface, narrowed to this ask (askIds wins over
               pending-only: an ask that ends while open stays drawn as ended). -->
          <div :ref="(el) => setCardRef(row.id, el)">
            <PortalAsks :ask-ids="[row.id]" show-agent pending-only @open-thread="(t) => emit('open-thread', t)" />
          </div>
          <!-- Not while the row is forced open by its Undo window (ent#748):
               collapsing would change nothing, and a control that does
               nothing is worse than none. -->
          <BaseButton
            v-if="!portal.askDismissals || !portal.askDismissals[row.id]"
            size="sm"
            variant="ghost"
            :class="TOUCH"
            class="mt-1"
            :data-testid="`project-ask-collapse-${row.id}`"
            @click="collapse(row)"
          >Collapse</BaseButton>
        </template>
        <div
          v-else
          class="rounded-xl border border-gray-200 dark:border-gray-750 px-3 py-2.5"
          :data-testid="`project-ask-row-${row.id}`"
        >
          <div class="flex items-center gap-2 min-w-0">
            <!-- The Inbox row's shape: the agent keeps its width (to 60%), the
                 rest truncates. The time never shrinks; the kind label gives way
                 first, then the agent name (#3994 — all three were shrink-0, so
                 in the narrow column the row overflowed its card). -->
            <span class="min-w-0 shrink-[0.25] max-w-[60%] text-sm font-medium truncate text-gray-900 dark:text-gray-100">{{ row.agent_name }}</span>
            <span class="min-w-0 truncate text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400">{{ kindLabel(row.kind) }}</span>
            <!-- #2915 / ent#610 B5: the Workspace's coarse sync badge, only
                 while pending — the same rule as the ask card's header. -->
            <BaseBadge
              v-if="row.status === 'pending' && workspaceAskBadge(row.ask)"
              :variant="workspaceAskBadge(row.ask).variant"
              dot
              :title="workspaceAskBadge(row.ask).title"
            >{{ workspaceAskBadge(row.ask).label }}</BaseBadge>
            <span
              class="ml-auto shrink-0 text-[12.5px] tabular-nums text-gray-500 dark:text-gray-400"
              :title="absolute(row.at)"
            >{{ relative(row.at) }}</span>
          </div>
          <!-- The title is agent markdown, inline (#3115) — bold, code and
               links only, no block element — so one line can truncate it. -->
          <p class="mt-0.5 text-sm truncate text-gray-700 dark:text-gray-300" :data-testid="`project-ask-title-${row.id}`">
            <AskMarkdown :text="row.title" inline />
          </p>
          <div class="mt-1.5 flex flex-wrap items-center gap-3">
            <BaseButton
              :ref="(el) => setAnswerRef(row.id, el)"
              size="sm"
              variant="secondary"
              :class="TOUCH"
              :data-testid="`project-ask-answer-${row.id}`"
              @click="expand(row)"
            >Answer</BaseButton>
            <router-link
              :to="inboxRoute(row)"
              :class="INBOX_LINK_CLASS"
              class="text-xs"
              :data-testid="`project-ask-inbox-${row.id}`"
            >Open in Inbox</router-link>
          </div>
        </div>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { computed, nextTick, reactive, watch } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import SkeletonLoader from '@/components/SkeletonLoader.vue'
import AskMarkdown from '@/components/operator/AskMarkdown.vue'
import PortalAsks from '@/components/portal/PortalAsks.vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import { queueTypeLabel, workspaceAskBadge } from '@/utils/operatorQueue'
import { formatRelativeTime, formatLocalDateTime } from '@/utils/timestamps'
import { viewState } from '@/utils/loadingState'
import { askInboxRoute } from '../portalRailAsks'
import { INBOX_LINK_CLASS } from '../portalUtils'
import { projectNeedsYouRows } from './projectsUtils'

const props = defineProps({
  // The server's project read: the viewer's own open asks on this project. It
  // can only narrow the shared Inbox list, never widen it.
  askIds: { type: Array, default: () => [] },
  // The project read could not reach the Inbox.
  unavailable: { type: Boolean, default: false },
})
const emit = defineEmits(['open-thread'])

const portal = useClientPortalStore()
const expanded = reactive({})

const rows = computed(() => projectNeedsYouRows(portal.asks, props.askIds, {
  expanded,
  dismissals: portal.askDismissals || {},
}))

// No ids → nothing is waiting, whatever the list's state; ids → the list's
// own verdict decides (the Inbox's rule, PortalInbox `view`).
const view = computed(() => {
  if (props.unavailable) return { state: 'failed', stale: false }
  if (!props.askIds.length) return { state: 'empty', stale: false }
  return viewState({
    hasLoaded: portal.asksLoaded,
    error: portal.asksFailed || portal.asksAbsent,
    count: rows.value.length,
  })
})

// An opened ask the list no longer carries (the answer path drops an ask the
// server returned no body for) leaves no stale key behind.
watch(() => portal.asks, (list) => {
  const ids = new Set((Array.isArray(list) ? list : []).map((a) => a && a.id))
  for (const id of Object.keys(expanded)) if (!ids.has(id)) delete expanded[id]
})

// Answer / Collapse: the ask card's own 44px floor on a touch screen
// (PortalAsks' chips, #3056) — BaseButton sm has no min-height of its own.
const TOUCH = 'max-sm:min-h-11 [@media(pointer:coarse)]:min-h-11'

const kindLabel = (kind) => queueTypeLabel(kind) || 'Question'
const relative = (t) => (t ? formatRelativeTime(t) : '')
const absolute = (t) => (t ? formatLocalDateTime(t) : '')
// The Inbox's own addressing (`?tab=action&from=<agent>&item=ask:<id>`): the
// ask opens selected there.
const inboxRoute = (row) => askInboxRoute(row) || { path: '/workspace/inbox', query: { tab: 'action' } }

// Focus follows the toggle: Answer removes the button under focus, so the
// opened card (tabindex -1 on PortalAsks' card) takes it; Collapse removes the
// card, so the Answer button takes it back.
const answerEls = new Map()
const cardEls = new Map()
const keep = (map) => (id, el) => {
  const node = el && el.$el ? el.$el : el
  if (node) map.set(id, node)
  else map.delete(id)
}
const setAnswerRef = keep(answerEls)
const setCardRef = keep(cardEls)
async function expand(row) {
  expanded[row.id] = true
  await nextTick()
  cardEls.get(row.id)?.querySelector('[tabindex="-1"]')?.focus?.({ preventScroll: true })
}
async function collapse(row) {
  delete expanded[row.id]
  await nextTick()
  answerEls.get(row.id)?.focus?.({ preventScroll: true })
}
</script>
