<template>
  <SettingsSection title="Messaging" description="One row per channel. Connection, behaviour and identity are set in its dialog.">
    <ChannelConfigRow
      title="Slack"
      :agent-name="agentName"
      :status-url="`/api/agents/${agentName}/slack/channel`"
      :derive-status="slackStatus"
      :open="openChannel === 'slack'"
      @status="report('Slack', $event)"
      @closed="$emit('channel-closed')"
    >
      <!-- ent#340: the dedicated bot is a tab here, not a second row. It's only
           offered where entitled (ent#222). -->
      <template v-if="botEntitled">
        <OverflowTabs v-model="slackTab" :tabs="SLACK_TABS" class="mb-4" />
        <SlackChannelPanel v-if="slackTab === 'channel'" :agent-name="agentName" />
        <SlackAgentBotPanel v-else :agent-name="agentName" />
      </template>
      <SlackChannelPanel v-else :agent-name="agentName" />
    </ChannelConfigRow>

    <ChannelConfigRow
      title="Telegram"
      :agent-name="agentName"
      :status-url="`/api/agents/${agentName}/telegram`"
      :derive-status="telegramStatus"
      :open="openChannel === 'telegram'"
      @status="report('Telegram', $event)"
      @closed="$emit('channel-closed')"
    >
      <TelegramChannelPanel :agent-name="agentName" />
    </ChannelConfigRow>

    <ChannelConfigRow
      title="WhatsApp"
      :agent-name="agentName"
      :status-url="`/api/agents/${agentName}/whatsapp`"
      :derive-status="whatsappStatus"
      :open="openChannel === 'whatsapp'"
      @status="report('WhatsApp', $event)"
      @closed="$emit('channel-closed')"
    >
      <WhatsAppChannelPanel :agent-name="agentName" />
    </ChannelConfigRow>

    <!-- VoIP (#28) is gated on the platform `voip_available` flag. -->
    <ChannelConfigRow
      v-if="sessionsStore.voipAvailable"
      title="Voice calls"
      :agent-name="agentName"
      :status-url="`/api/agents/${agentName}/voip`"
      :derive-status="voipStatus"
      :open="openChannel === 'voip'"
      @status="report('Voice calls', $event)"
      @closed="$emit('channel-closed')"
    >
      <VoipChannelPanel :agent-name="agentName" />
    </ChannelConfigRow>
  </SettingsSection>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useEnterpriseStore } from '../../../stores/enterprise'
import { useSessionsStore } from '../../../stores/sessions'
import { slackStatus, telegramStatus, whatsappStatus, voipStatus } from '../../../utils/channelsPane'
import ChannelConfigRow from '../../ChannelConfigRow.vue'
import OverflowTabs from '../../OverflowTabs.vue'
import SlackAgentBotPanel from '../../SlackAgentBotPanel.vue'
import SlackChannelPanel from '../../SlackChannelPanel.vue'
import TelegramChannelPanel from '../../TelegramChannelPanel.vue'
import VoipChannelPanel from '../../VoipChannelPanel.vue'
import WhatsAppChannelPanel from '../../WhatsAppChannelPanel.vue'
import SettingsSection from '../SettingsSection.vue'

defineProps({
  agentName: { type: String, required: true },
  openChannel: { type: String, default: null },
})
const emit = defineEmits(['status', 'channel-closed'])

const SLACK_TABS = [{ id: 'channel', label: 'Channel' }, { id: 'bot', label: 'Dedicated bot' }]
const slackTab = ref('channel')

const sessionsStore = useSessionsStore()
const enterpriseStore = useEnterpriseStore()
const botEntitled = computed(() => enterpriseStore.isEntitled('slack_per_agent_bots'))
const report = (name, { connected }) => emit('status', { name, connected })

onMounted(() => {
  sessionsStore.loadFeatureFlags()
  enterpriseStore.loadFeatureFlags()
})
</script>
