<template>
  <!--
    Settings › Channels — "Where outsiders reach it" (trinity#966 step 6,
    trinity-enterprise#340). Each section loads and saves its own data. The pane
    composes them, and owns the header line and the `&channel=` deep link.
  -->
  <div class="space-y-6">
    <p v-if="summaryLine" class="text-sm text-gray-600 dark:text-gray-300" data-testid="channels-summary">{{ summaryLine }}</p>
    <WhoCanChat :agent-name="agentName" :shares="shares" @summary="access = $event" @agent-updated="$emit('agent-updated')" />
    <MessagingRows :agent-name="agentName" :open-channel="openChannel" @status="onStatus" @channel-closed="closeChannel" />
    <WebRows :agent-name="agentName" @status="onStatus" />
    <SpeaksOutside :agent-name="agentName" />
    <ClientRoster :agent-name="agentName" />
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { deepLinkChannel, paneSummary } from '../../utils/channelsPane'
import ClientRoster from './channels/ClientRoster.vue'
import MessagingRows from './channels/MessagingRows.vue'
import SpeaksOutside from './channels/SpeaksOutside.vue'
import WebRows from './channels/WebRows.vue'
import WhoCanChat from './channels/WhoCanChat.vue'

const props = defineProps({
  agentName: { type: String, required: true },
  shares: { type: Array, default: () => [] },
})
defineEmits(['agent-updated'])

const route = useRoute()
const router = useRouter()

const access = ref({ mode: null, pending: null })
const surfaces = ref({})

function onStatus({ name, connected }) {
  surfaces.value = { ...surfaces.value, [name]: connected }
}

// Only what's confirmed: a surface that failed its check (null) isn't listed.
const reachable = computed(() => {
  const names = Object.keys(surfaces.value)
  return names.length ? names.filter((n) => surfaces.value[n] === true) : null
})
const summaryLine = computed(() => paneSummary({ ...access.value, reachable: reachable.value }))

const openChannel = computed(() => deepLinkChannel(route.query.channel))
function closeChannel() {
  if (!route.query.channel) return
  const { channel: _channel, ...query } = route.query
  router.replace({ query })
}

watch(() => props.agentName, () => {
  access.value = { mode: null, pending: null }
  surfaces.value = {}
})
</script>
