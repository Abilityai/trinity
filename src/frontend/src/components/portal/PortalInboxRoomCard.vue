<!--
  trinity-enterprise#959 — a ROOM, opened in the Inbox's reading pane.

  A room is a chat with more than one agent, so it reads like a chat: its new
  messages, from the first unread one (`first_unread_message_id`, which rides
  the `/chat-state` preview) onward, capped at the pane's tail, one header per
  run of ONE sender — two agents speaking in turn are two runs, each named.

  The transcript is the room's own membership-scoped read (`GET /api/rooms/{id}`,
  a uniform 404 for anyone not in it) — the same call the room makes when it
  opens. Honest state (principle 15): a skeleton until it answers, `LoadFailed`
  with retry when it fails, then the messages. It says `rendered` once they are
  on screen; the container reads the room only then, so a failed load leaves it
  unread.
-->
<template>
  <div data-testid="inbox-pane-room">
    <div v-if="view.state === 'loading'" class="space-y-3" aria-busy="true" data-testid="inbox-pane-room-loading">
      <div v-for="i in 3" :key="i" class="animate-pulse motion-reduce:animate-none h-12 rounded-lg bg-gray-100 dark:bg-gray-800/60"></div>
      <span class="sr-only">Loading this chat…</span>
    </div>
    <LoadFailed
      v-else-if="view.state === 'failed'"
      dense
      title="Couldn't load this chat"
      message="The chat is still there — try again, or open it."
      :retrying="busy"
      data-testid="inbox-pane-room-failed"
      @retry="load"
    />
    <template v-else>
      <p
        v-if="windowed.earlier > 0"
        class="mb-3 text-[12.5px] text-gray-600 dark:text-gray-300"
        data-testid="inbox-pane-earlier"
      >
        {{ windowed.earlier }} earlier {{ windowed.earlier === 1 ? 'arrival' : 'arrivals' }} —
        <button type="button" class="text-action-primary-600 dark:text-action-primary-400 hover:underline" @click="$emit('open-chat')">Open in chat</button>
      </p>
      <section v-for="run in runs" :key="run.key" class="mb-4" data-testid="inbox-pane-run">
        <template v-if="run.role === 'system'">
          <p
            v-for="(m, i) in run.messages"
            :key="m.id || i"
            class="text-[12.5px] text-gray-500 dark:text-gray-400"
            :data-testid="`inbox-pane-message-${m.id || i}`"
          >{{ m.content }}</p>
        </template>
        <template v-else>
          <p
            class="mb-1.5 text-[12.5px] text-gray-600 dark:text-gray-300"
            :class="run.role === 'user' ? 'text-right' : 'pl-[38px]'"
            data-testid="inbox-pane-run-header"
          >{{ run.role === 'user' ? 'You' : labelFor(run.sender) }} · <span class="tabular-nums" :title="absolute(run.at)">{{ relative(run.at) }}</span></p>
          <div v-if="run.role === 'user'" class="flex flex-col items-end gap-1.5">
            <div
              v-for="(m, i) in run.messages"
              :key="m.id || i"
              class="max-w-[85%] rounded-2xl rounded-br-md px-3.5 py-3 text-sm leading-relaxed whitespace-pre-wrap bg-action-primary-600 text-white"
              :class="BUBBLE_WRAP_CLASS"
              :data-testid="`inbox-pane-message-${m.id || i}`"
            >{{ m.content }}</div>
          </div>
          <div v-else class="flex items-start gap-2.5">
            <PortalAvatar :name="run.sender || ''" :size="28" class="mt-0.5" />
            <div class="min-w-0 flex-1 space-y-1.5">
              <div v-for="(m, i) in run.messages" :key="m.id || i" class="max-w-[85%]" :data-testid="`inbox-pane-message-${m.id || i}`">
                <PortalAgentBubble :content="m.content || ''" :copyable="false" />
              </div>
            </div>
          </div>
        </template>
      </section>
      <p
        v-if="!windowed.shown.length"
        class="text-sm text-gray-600 dark:text-gray-300"
        data-testid="inbox-pane-room-empty"
      >Nothing new in this chat. Open it to see the whole conversation.</p>
    </template>
  </div>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import LoadFailed from '@/components/LoadFailed.vue'
import PortalAvatar from './PortalAvatar.vue'
import PortalAgentBubble from './PortalAgentBubble.vue'
import { BUBBLE_WRAP_CLASS } from './portalBubble'
import { useClientPortalStore } from '@/stores/clientPortal'
import { relativeTime } from './portalUtils'
import { paneWindow, paneRuns, roomPaneMessages } from './portalInbox'
import { viewState } from '@/utils/loadingState'
import { formatLocalDateTime } from '@/utils/timestamps'

const props = defineProps({
  // The Inbox item (`portalInbox.roomItem`).
  item: { type: Object, required: true },
  // agent name → label, for each run's sender.
  labels: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['rendered', 'open-chat'])

const store = useClientPortalStore()
const messages = ref(null)
const failed = ref(false)
const busy = ref(false)
let gen = 0

const view = computed(() => viewState({ hasLoaded: messages.value !== null, error: failed.value }))
const windowed = computed(() => paneWindow(messages.value || [], props.item.first_unread_message_id, props.item.n))
const runs = computed(() => paneRuns(windowed.value.shown))
const labelFor = (name) => (name && props.labels && props.labels[name]) || name || 'Agent'

async function load() {
  const mine = ++gen
  busy.value = true
  failed.value = false
  try {
    const data = await store.fetchRoom(props.item.id)
    if (mine !== gen) return
    messages.value = roomPaneMessages(data?.messages)
    await nextTick()
    emit('rendered', props.item.key)
  } catch {
    if (mine === gen) failed.value = true
  } finally {
    if (mine === gen) busy.value = false
  }
}
watch(() => props.item.key, () => { messages.value = null; load() }, { immediate: true })

const relative = (iso) => relativeTime(iso)
let zone = ''
try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || '' } catch { zone = '' }
const absolute = (iso) => (iso ? `${formatLocalDateTime(iso)}${zone ? ` (${zone})` : ''}` : undefined)

defineExpose({ reload: load })
</script>
