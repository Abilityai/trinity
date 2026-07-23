<template>
  <div class="h-[calc(100vh-4rem)] flex bg-white dark:bg-gray-900">
    <RoomsRail
      :rooms="store.rooms"
      :loading="store.roomsLoading"
      :active-room-id="store.activeRoomId"
      @new-session="showDialog = true"
      @open="openRoom"
    />

    <!-- center: transcript + composer -->
    <div class="flex-1 min-w-0 flex flex-col">
      <template v-if="store.activeRoom">
        <!-- header -->
        <div class="shrink-0 h-14 px-4 flex items-center gap-3 border-b border-gray-200 dark:border-gray-800">
          <div class="flex -space-x-2">
            <PortalAvatar
              v-for="p in store.agentParticipants.slice(0, 4)"
              :key="p.identity"
              :name="p.identity"
              :avatar-url="avatarByName[p.identity] || null"
              :size="26"
              class="ring-2 ring-white dark:ring-gray-900"
            />
          </div>
          <div class="min-w-0">
            <div class="flex items-center gap-2">
              <span class="text-sm font-semibold truncate">{{ store.activeRoom.name }}</span>
              <span
                class="text-[10px] font-semibold px-1.5 py-0.5 rounded"
                :class="store.isClosed
                  ? 'bg-gray-200 text-gray-600 dark:bg-gray-700 dark:text-gray-300'
                  : 'bg-status-success-100 text-status-success-700 dark:bg-status-success-900 dark:text-status-success-200'"
              >{{ store.isClosed ? 'Closed' : 'Active' }}</span>
            </div>
            <div v-if="store.activeRoom.topic" class="text-xs text-gray-400 truncate">{{ store.activeRoom.topic }}</div>
          </div>
          <div class="ml-auto flex items-center gap-3">
            <span class="text-[11px] text-gray-400">{{ store.activeRoom.message_count }}/{{ store.activeRoom.max_messages }} msgs</span>
            <span class="text-[11px] text-gray-400">${{ (store.activeRoom.cost || 0).toFixed(3) }}</span>
            <button
              v-if="!store.isClosed"
              class="text-xs px-2.5 py-1 rounded-lg border border-gray-300 dark:border-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800"
              @click="closeRoom"
            >Close</button>
          </div>
        </div>

        <RoomTranscript
          :messages="store.activeRoom.messages || []"
          :participants="store.activeRoom.participants || []"
          :working-state="store.workingState"
          :execution-costs="{}"
          :avatar-by-name="avatarByName"
        />

        <p v-if="store.error" class="px-4 py-1 text-xs text-status-danger-600 dark:text-status-danger-400">{{ store.error }}</p>

        <RoomComposer
          :participants="store.activeRoom.participants || []"
          :avatar-by-name="avatarByName"
          :posting="store.posting"
          :closed="store.isClosed"
          :stop-reason="store.activeRoom.stop_reason"
          @send="send"
        />
      </template>

      <!-- no room selected -->
      <div v-else class="flex-1 flex items-center justify-center text-center px-6">
        <div>
          <div class="mx-auto w-12 h-12 rounded-full bg-gray-100 dark:bg-gray-800 flex items-center justify-center mb-4">
            <svg class="w-6 h-6 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.6" d="M17 8h2a2 2 0 012 2v6a2 2 0 01-2 2h-2v4l-4-4H9a2 2 0 01-2-2m10-8V6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2v4l4-4h2" /></svg>
          </div>
          <p class="text-sm font-medium text-gray-700 dark:text-gray-200">Shared sessions</p>
          <p class="mt-1 text-xs text-gray-400 max-w-xs">Work a topic with several agents at once. Pick a session on the left, or start a new one.</p>
        </div>
      </div>
    </div>

    <ParticipantsRail
      v-if="store.activeRoom"
      :participants="store.activeParticipants"
      :working-state="store.workingState"
      :avatar-by-name="avatarByName"
      :message-count="store.activeRoom.message_count || 0"
      :max-messages="store.activeRoom.max_messages || 60"
      :cost="store.activeRoom.cost || 0"
      :max-cost="store.activeRoom.max_cost_usd"
      :expires-at="store.activeRoom.expires_at"
    />

    <NewRoomDialog v-if="showDialog" :roster="roster" @close="showDialog = false" @created="onCreate" />
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useRoomsStore } from '../../stores/rooms'
import { useAgentsStore } from '../../stores/agents'
import PortalAvatar from '../../components/portal/PortalAvatar.vue'
import RoomsRail from '../../components/rooms/RoomsRail.vue'
import RoomTranscript from '../../components/rooms/RoomTranscript.vue'
import RoomComposer from '../../components/rooms/RoomComposer.vue'
import ParticipantsRail from '../../components/rooms/ParticipantsRail.vue'
import NewRoomDialog from '../../components/rooms/NewRoomDialog.vue'

const store = useRoomsStore()
const agentsStore = useAgentsStore()
const router = useRouter()
const route = useRoute()
const showDialog = ref(false)

// avatar URL by agent name — from the fleet roster already loaded, so no
// per-message fetch (the #1734 idiom).
const roster = computed(() => agentsStore.agents || [])
const avatarByName = computed(() =>
  Object.fromEntries(roster.value.map((a) => [a.name, a.avatar_url]))
)

async function openRoom(id) {
  await store.openRoom(id)
  if (route.params.roomId !== id) router.replace({ name: 'Sessions', params: { roomId: id } })
}
async function send(content) {
  try { await store.postMessage(content) } catch { /* store surfaces error */ }
}
async function closeRoom() {
  if (store.activeRoomId) await store.closeRoom(store.activeRoomId)
}
async function onCreate(payload, onErr) {
  try {
    const room = await store.createRoom(payload)
    showDialog.value = false
    await openRoom(room.id)
  } catch (e) {
    const d = e?.response?.data?.detail
    onErr?.((d && (d.message || d)) || 'Failed to create session')
  }
}

onMounted(async () => {
  if (!agentsStore.agents?.length) { try { await agentsStore.fetchAgents() } catch { /* non-fatal */ } }
  await store.fetchRooms()
  if (route.params.roomId) await openRoom(route.params.roomId)
})
onUnmounted(() => store.clear())
</script>
