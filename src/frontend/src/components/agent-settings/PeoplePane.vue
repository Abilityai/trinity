<template>
  <!--
    Settings › People — "Who works with it" (trinity-enterprise#891).

    A relocation, not a redesign: the Access tab's panel moves here unchanged —
    who the agent serves (ent#810 seats) and the operators who can open it,
    with platform roles (ent#837) and invites. Platform users live in this
    pane; outsiders who reach the agent through a channel are under Channels
    (the ent#16 rule: one pane per user class).
  -->
  <AccessPanel :agent-name="agentName" :agent-owner="agentOwner" embedded />
</template>

<script setup>
import AccessPanel from '../AccessPanel.vue'

defineProps({
  agentName: { type: String, required: true },
  agentOwner: { type: String, default: null },
})
</script>
