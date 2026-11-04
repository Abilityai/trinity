<!--
  Before setup hands someone over to a chat: who they're about to meet, what happens
  there, and that the first reply takes a moment. Shared by the audit and first-agent steps.
-->
<template>
  <ConfirmDialog
    :visible="visible"
    variant="info"
    confirm-variant="primary"
    :title="`You're about to meet ${BUILD_GUIDE_NAME}`"
    :message="message"
    :confirm-text="confirmText"
    cancel-text="Not yet"
    @update:visible="$emit('update:visible', $event)"
    @confirm="$emit('confirm')"
    @cancel="$emit('update:visible', false)"
  />
</template>

<script setup>
import { computed } from 'vue'
import ConfirmDialog from '../ConfirmDialog.vue'
import { BUILD_GUIDE_NAME } from '../portal/builder/portalBuild'

const props = defineProps({
  visible: { type: Boolean, default: false },
  // 'audit', 'agent' or 'profile'
  purpose: { type: String, default: 'audit' },
})
defineEmits(['update:visible', 'confirm'])

const name = BUILD_GUIDE_NAME
const MESSAGES = {
  audit: `${name} is your first Trinity agent. In a chat, ${name} learns how your company works and recommends the first agent to build. It takes about 10 minutes, and you can stop whenever you like. The first reply can take a minute or two while ${name} gets ready.`,
  agent: `${name} is your first Trinity agent. In a chat, ${name} asks what you have in mind and walks you through building it. The first reply can take a minute or two while ${name} gets ready.`,
  profile: `${name} is your first Trinity agent. In a short chat, ${name} asks about your work, so Trinity knows what your job involves. It takes about 2 minutes. The first reply can take a minute or two while ${name} gets ready.`,
}
const message = computed(() => MESSAGES[props.purpose] || MESSAGES.agent)
const confirmText = computed(() => (props.purpose === 'audit' ? 'Start the audit' : `Go to ${name}`))
</script>
