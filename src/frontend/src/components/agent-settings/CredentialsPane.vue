<template>
  <!--
    Settings › Credentials — "What secrets it runs with" (trinity-enterprise#951).

    A relocation, not a redesign: #3798 (ent#888) folded the Credentials tab
    into Storage as one section among repository, git sync and folders. It
    answers a different question and is what an operator opens most often
    after creating an agent, so it is a pane of its own again, with the old
    `?tab=credentials` deep link landing here.

    Credential changes apply without a restart (`.env` is re-read at every
    spawn), so nothing here reports to the restart banner.
  -->
  <CredentialsPanel :agent-name="agentName" :agent-status="agentStatus" embedded />
</template>

<script setup>
import CredentialsPanel from '../CredentialsPanel.vue'

defineProps({
  agentName: { type: String, required: true },
  agentStatus: { type: String, default: 'stopped' },
})
</script>
