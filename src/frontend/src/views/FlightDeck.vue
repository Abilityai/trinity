<template>
  <!--
    Flight Deck in its own window (abilityai/trinity-enterprise#1307).

    The Flight Deck is a mode of the Dashboard. This page is the same pane with
    no app navigation, for the tabless window the pane opens itself in ("Open
    in window") so it can be left on a second screen. It runs the fleet store
    exactly as the Dashboard does while mounted.
  -->
  <div class="h-dvh flex flex-col bg-gray-50 dark:bg-gray-900">
    <h1 class="sr-only">Flight Deck</h1>
    <FlightDeckPanel class="flex-1 min-h-0" :agents="visibleAgents" window-mode @retry="reload" />
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, watch } from 'vue'
import { storeToRefs } from 'pinia'
import FlightDeckPanel from '@/components/flight-deck/FlightDeckPanel.vue'
import { useNetworkStore } from '@/stores/network'
import { useAuthStore } from '@/stores/auth'

const networkStore = useNetworkStore()
const authStore = useAuthStore()
const { visibleAgents } = storeToRefs(networkStore)

// The socket follows the platform session, as on the Dashboard (#3445).
watch(() => authStore.token, (token) => networkStore.followToken(token))

async function reload() {
  await Promise.allSettled([
    networkStore.fetchAgents(),
    networkStore.fetchHistoricalCommunications(),
    networkStore.fetchSchedules(),
  ])
}

onMounted(async () => {
  await reload()
  networkStore.connectWebSocket()
  networkStore.startContextPolling()
  networkStore.startAgentRefresh()
  networkStore.startActivityRefresh()
})

onUnmounted(() => {
  networkStore.disconnectWebSocket()
  networkStore.stopContextPolling()
  networkStore.stopAgentRefresh()
  networkStore.stopActivityRefresh()
})
</script>
