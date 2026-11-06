<template>
  <SettingsSection title="Who has talked to it">
    <SettingRow label="Client roster" hint="External people who have messaged this agent through a channel." testid="channels-roster">
      <template #value>
        <template v-if="failed">
          <span class="text-xs text-status-danger-700 dark:text-status-danger-400">Couldn't load the roster</span>
          <BaseButton size="sm" variant="ghost" @click="load">Retry</BaseButton>
        </template>
        <span v-else-if="loaded" class="text-xs text-gray-600 dark:text-gray-300" data-testid="channels-roster-summary">{{ summary }}</span>
      </template>
      <template #action>
        <BaseButton v-if="clients.length" size="sm" variant="secondary" data-testid="channels-roster-open" @click="open = true">Open roster</BaseButton>
      </template>
    </SettingRow>
    <ChannelConfigDialog v-if="open" title="Client roster" @close="open = false">
      <!-- #1925: a bounded wide table. Horizontal scroll INSIDE the container is the
           right treatment for unbounded column width (design-system principle 7). -->
      <div class="overflow-x-auto border border-gray-200 dark:border-gray-700 rounded-lg">
        <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700 text-sm">
          <thead class="bg-gray-50 dark:bg-gray-900/50">
            <tr class="text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
              <th class="px-4 py-2">Client</th>
              <th class="px-4 py-2">Channel</th>
              <th class="px-4 py-2">Verified email</th>
              <th
                class="px-4 py-2 text-right"
                title="Direct messages received from this client. Counting starts when this feature is deployed."
              >Messages</th>
              <th class="px-4 py-2">Last active</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-200 dark:divide-gray-700">
            <tr v-for="client in clients" :key="`${client.channel}:${client.identity}`">
              <td class="px-4 py-2 text-gray-900 dark:text-gray-100">
                <span class="font-medium">{{ client.display_name || client.identity }}</span>
                <span v-if="client.display_name" class="block text-xs text-gray-500 dark:text-gray-400">{{ client.identity }}</span>
              </td>
              <td class="px-4 py-2">
                <BaseBadge size="sm" variant="neutral">{{ client.channel }}</BaseBadge>
              </td>
              <td class="px-4 py-2 text-gray-600 dark:text-gray-300">{{ client.verified_email || '—' }}</td>
              <td class="px-4 py-2 text-right tabular-nums text-gray-700 dark:text-gray-200">{{ client.message_count }}</td>
              <td class="px-4 py-2 text-gray-500 dark:text-gray-400">{{ formatLastActive(client.last_active) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </ChannelConfigDialog>
  </SettingsSection>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import axios from 'axios'
import { useAuthStore } from '../../../stores/auth'
import { rosterSummary } from '../../../utils/channelsPane'
import BaseBadge from '../../base/BaseBadge.vue'
import BaseButton from '../../base/BaseButton.vue'
import ChannelConfigDialog from '../../ChannelConfigDialog.vue'
import SettingRow from '../SettingRow.vue'
import SettingsSection from '../SettingsSection.vue'

const props = defineProps({ agentName: { type: String, required: true } })

const authStore = useAuthStore()
const clients = ref([])
const loaded = ref(false)
const failed = ref(false)
const open = ref(false)
const summary = computed(() => rosterSummary(clients.value))

const load = async () => {
  failed.value = false
  try {
    const { data } = await axios.get(`/api/agents/${props.agentName}/clients`, { headers: authStore.authHeader })
    clients.value = data
    loaded.value = true
  } catch (err) {
    // Keep what's on screen: a failed read isn't "no clients" (ent#253).
    console.error('Failed to load client roster:', err)
    failed.value = true
  }
}

const formatLastActive = (iso) => {
  if (!iso) return 'never'
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

watch(() => props.agentName, (name) => { if (name) load() }, { immediate: true })
</script>
