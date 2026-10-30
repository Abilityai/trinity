<template>
  <!--
    trinity-enterprise#701: the people in a room, opened from the room header's
    "People · N" control so the header keeps one height for any number of people
    (principle 30). Lists who is here and who left; any person can Leave. A
    moderator on an install that offers seating (`room.can_seat_people`, the
    server's verdict) also seats colleagues, shows them the earlier messages,
    and removes them. Elsewhere the panel names the next step — @mention a
    colleague to let them read along — rather than a control that would refuse.
  -->
  <div
    class="shrink-0 border-b border-gray-200 dark:border-gray-750 bg-gray-50 dark:bg-gray-900 px-4 py-3"
    role="region"
    aria-label="People in this room"
    data-testid="room-people"
    @keydown.esc.stop="emit('close')"
  >
    <div class="flex items-center justify-between mb-2">
      <p class="font-mono text-[11px] uppercase tracking-wider text-gray-600 dark:text-gray-300">People in this room</p>
      <BaseButton variant="ghost" size="sm" aria-label="Close people panel" title="Close (Esc)" data-testid="room-people-close" @click="emit('close')">
        <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" /></svg>
      </BaseButton>
    </div>
    <ul class="max-h-48 overflow-y-auto space-y-1.5">
      <li
        v-for="p in people"
        :key="`${p.kind}:${p.identity}`"
        class="flex items-center gap-2 min-h-[2rem]"
        :data-testid="`room-person-${p.identity}`"
      >
        <PortalAvatar :name="p.identity" :size="22" />
        <div class="min-w-0 flex-1">
          <p class="text-sm truncate" :class="p.left_at ? 'text-gray-500 dark:text-gray-400' : 'text-gray-900 dark:text-gray-100'">
            {{ p.identity }}<span v-if="isYou(p)" class="text-gray-600 dark:text-gray-300"> (you)</span>
          </p>
          <p class="text-xs text-gray-600 dark:text-gray-400 truncate">
            <template v-if="p.left_at">
              Left <span :title="formatLocalDateTime(p.left_at)">{{ formatRelativeTime(p.left_at) }}</span>
            </template>
            <template v-else>{{ roleLabel(p) }}</template>
          </p>
        </div>
        <template v-if="manageable(p)">
          <BaseButton
            v-if="!p.sees_history"
            variant="ghost"
            size="sm"
            :loading="busy === `history:${p.identity}`"
            loading-label="Showing…"
            :data-testid="`room-person-history-${p.identity}`"
            @click="showHistory(p)"
          >Show earlier messages</BaseButton>
          <BaseButton
            variant="ghost"
            size="sm"
            :data-testid="`room-person-remove-${p.identity}`"
            @click="confirmRemove = p"
          >Remove</BaseButton>
        </template>
      </li>
    </ul>

    <!-- Seating: offered only where the server says so. -->
    <div v-if="room?.can_seat_people" class="mt-3 pt-3 border-t border-gray-200 dark:border-gray-750">
      <BaseInput
        id="room-seat-search"
        v-model="query"
        label="Add a person"
        placeholder="Search your team by name or email"
        autocomplete="off"
        help="They join as a collaborator: they can read and talk here, but can't approve, spend or reassign."
        data-testid="room-seat-search"
      />
      <div class="mt-2">
        <BaseToggle v-model="withHistory" label="Show them the earlier messages" data-testid="room-seat-history" />
      </div>
      <ul v-if="candidates.length" class="mt-2 max-h-40 overflow-y-auto space-y-1" aria-label="People you can add">
        <li v-for="c in candidates" :key="c.username" class="flex items-center gap-2 min-h-[2rem]">
          <PortalAvatar :name="c.username" :size="20" />
          <div class="min-w-0 flex-1">
            <p class="text-sm truncate" :class="c.seatable ? 'text-gray-900 dark:text-gray-100' : 'text-gray-500 dark:text-gray-400'">{{ c.label }}</p>
            <p v-if="c.reason" class="text-xs text-gray-600 dark:text-gray-400 truncate">{{ c.reason }}</p>
          </div>
          <BaseButton
            size="sm"
            variant="secondary"
            :disabled="!c.seatable"
            :loading="busy === `seat:${c.username}`"
            loading-label="Adding…"
            :data-testid="`room-seat-${c.username}`"
            @click="seat(c)"
          >Add</BaseButton>
        </li>
      </ul>
      <p v-else-if="searched && query.trim()" class="mt-2 text-xs text-gray-600 dark:text-gray-400">
        No one on your team matches “{{ query.trim() }}”.
      </p>
    </div>
    <p
      v-else-if="you?.kind === 'user'"
      class="mt-2 text-xs text-gray-600 dark:text-gray-400"
      data-testid="room-people-next-step"
    >To let a colleague read along, @mention them in a message.</p>

    <div class="mt-3 flex items-center gap-2">
      <BaseButton
        v-if="canLeave"
        variant="secondary"
        size="sm"
        :loading="busy === 'leave'"
        loading-label="Leaving…"
        data-testid="room-leave"
        @click="leave"
      >Leave room</BaseButton>
    </div>
    <InlineError class="mt-2" :message="error" @dismiss="error = ''" />

    <ConfirmDialog
      :visible="!!confirmRemove"
      title="Remove from this room?"
      :message="confirmRemove ? `${confirmRemove.identity} will no longer see this room or anything written in it. You can add them back later.` : ''"
      confirm-text="Remove"
      variant="danger"
      @confirm="remove"
      @cancel="confirmRemove = null"
      @update:visible="(v) => { if (!v) confirmRemove = null }"
    />
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useClientPortalStore } from '../../stores/clientPortal'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import BaseToggle from '../base/BaseToggle.vue'
import ConfirmDialog from '../ConfirmDialog.vue'
import InlineError from '../InlineError.vue'
import PortalAvatar from './PortalAvatar.vue'
import { formatLocalDateTime, formatRelativeTime } from '../../utils/timestamps'

const props = defineProps({
  roomId: { type: String, required: true },
  // The room payload: `people`, `you`, `can_seat_people`.
  room: { type: Object, default: null },
})
const emit = defineEmits(['changed', 'left', 'close'])

const store = useClientPortalStore()
const people = computed(() => props.room?.people || [])
const you = computed(() => props.room?.you || null)
const isYou = (p) => !!you.value && p.kind === you.value.kind && p.identity === you.value.identity
const canLeave = computed(() => !!you.value && (you.value.kind === 'user' || you.value.kind === 'workspace_user')
  && people.value.some((p) => isYou(p) && !p.left_at))

function roleLabel(p) {
  if (p.role === 'moderator') return 'Started this room'
  if (p.role === 'collaborator') return 'Collaborator'
  return 'In this room'
}
// The moderator manages the seated collaborators — never themselves, never
// someone who left.
const manageable = (p) => !!props.room?.can_seat_people && !p.left_at && !isYou(p) && p.role === 'collaborator'

const query = ref('')
const withHistory = ref(false)
const candidates = ref([])
const searched = ref(false)
const busy = ref('')
const error = ref('')
const confirmRemove = ref(null)

function reason(err, fallback) {
  const d = err?.response?.data?.detail
  return d?.message || (typeof d === 'string' ? d : null) || fallback
}

let searchTimer = null
let searchToken = 0
watch(query, (q) => {
  clearTimeout(searchTimer)
  if (!q.trim()) { candidates.value = []; searched.value = false; return }
  searchTimer = setTimeout(async () => {
    const token = ++searchToken
    try {
      const rows = await store.fetchSeatablePeople(props.roomId, q.trim())
      if (token === searchToken) { candidates.value = rows; searched.value = true }
    } catch (err) {
      if (token === searchToken) error.value = reason(err, 'Could not search your team. Try again.')
    }
  }, 250)
})

async function seat(c) {
  busy.value = `seat:${c.username}`
  error.value = ''
  try {
    await store.seatPerson(props.roomId, c.username, { showHistory: withHistory.value })
    query.value = ''
    withHistory.value = false
    emit('changed')
  } catch (err) {
    error.value = reason(err, `Could not add ${c.username}.`)
  } finally {
    busy.value = ''
  }
}

async function showHistory(p) {
  busy.value = `history:${p.identity}`
  error.value = ''
  try {
    await store.showPersonHistory(props.roomId, p.identity)
    emit('changed')
  } catch (err) {
    error.value = reason(err, `Could not show ${p.identity} the earlier messages.`)
  } finally {
    busy.value = ''
  }
}

async function remove() {
  const p = confirmRemove.value
  confirmRemove.value = null
  if (!p) return
  busy.value = `remove:${p.identity}`
  error.value = ''
  try {
    await store.removePerson(props.roomId, p.identity)
    emit('changed')
  } catch (err) {
    error.value = reason(err, `Could not remove ${p.identity}.`)
  } finally {
    busy.value = ''
  }
}

async function leave() {
  busy.value = 'leave'
  error.value = ''
  try {
    await store.leaveRoom(props.roomId)
    emit('left', props.roomId)
  } catch (err) {
    error.value = reason(err, 'Could not leave this room.')
  } finally {
    busy.value = ''
  }
}
</script>
