<template>
  <!--
    Settings › Channels — "Where outsiders reach it" (trinity#966 step 6,
    ent#340). This step only re-homes the Voice card from the old flat
    Settings list (migration map: Channels › How it speaks outside); the
    channel console itself is still the Sharing tab until ent#340 moves it,
    and the row below says so and opens it rather than leaving a dead end.
  -->
  <div class="space-y-6">
    <SettingsSection title="How it speaks outside">
      <div class="px-4 py-3">
        <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Voice replies</p>
        <p class="mt-0.5 mb-3 text-[12.5px] text-gray-600 dark:text-gray-400">
          Let the agent answer with a spoken voice note on messaging channels. Each channel can still opt out in its own settings.
        </p>
        <VoiceRepliesControl :agent-name="agentName" embedded />
      </div>
    </SettingsSection>

    <SettingsSection v-if="hasSharingTab" title="Who can chat, and where">
      <SettingRow
        label="Chat policy, channels and public links"
        hint="Who may chat, Slack, Telegram, WhatsApp, voice calls, public links and the client roster are on the Sharing tab for now."
      >
        <template #action>
          <BaseButton variant="secondary" size="sm" @click="$emit('navigate-tab', 'sharing')">Open Sharing</BaseButton>
        </template>
      </SettingRow>
    </SettingsSection>
  </div>
</template>

<script setup>
import SettingsSection from './SettingsSection.vue'
import SettingRow from './SettingRow.vue'
import BaseButton from '../base/BaseButton.vue'
import VoiceRepliesControl from '../VoiceRepliesControl.vue'

defineProps({
  agentName: { type: String, required: true },
  hasSharingTab: { type: Boolean, default: false },
})
defineEmits(['navigate-tab'])
</script>
