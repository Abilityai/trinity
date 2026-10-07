<template>
  <!-- A line the platform says in the conversation, not the agent's words —
       centred and muted, outside both bubbles. The Workspace's platform line
       (`PortalConversation.vue` PLATFORM_LINE_CLASS), with the contract's
       tertiary ink. trinity#3274: a held (gated-skill) request's notice. -->
  <p
    class="my-3 text-center text-xs text-gray-500 dark:text-gray-400 whitespace-pre-wrap break-words"
    data-testid="chat-system-line"
  >{{ content }}</p>
</template>

<script setup>
defineProps({
  content: {
    type: String,
    required: true
  }
})
</script>
