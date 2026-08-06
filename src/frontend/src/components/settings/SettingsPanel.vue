<template>
  <!--
    Per-agent Settings tab (#1108). Sectioned home for agent configuration.
    Each setting is its own <section> card. Adding a new section is purely
    additive — drop another <section> into the stack; no restructuring needed.
    Section #1 is Guardrails (GUARD-001), rendered unchanged — its own p-6
    is the card's content padding, so we don't double-pad here (avoids the
    extra height that forced a scroller, #1108 follow-up).

    Growth path (each a follow-up PR, endpoints already exist):
      Behavior/Execution (/autonomy, /read-only, /timeout, model, /capacity),
      Resources (/resources),
      Compute/Auth (api-key, /github-pat), Git sync (/git/auto-sync,
      /git/freeze-schedules-if-failing).
    Reliability (/circuit-breaker) shipped as the ReliabilityPanel section (#1712).
  -->
  <div class="space-y-4">
    <!-- Section 1: Guardrails -->
    <section class="rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
      <GuardrailsPanel :agent-name="agentName" :notify="notify" />
    </section>

    <!-- Section 2: Parallel Capacity (#506) -->
    <section class="rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
      <CapacityPanel :agent-name="agentName" :notify="notify" />
    </section>

    <!-- Section 3: Expose via MCP (#846) -->
    <section class="rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden p-6">
      <McpExposedPanel :agent-name="agentName" :notify="notify" />
    </section>

    <!-- Section 3b: Trinity access key — the agent's own scope='agent' MCP key (#1854) -->
    <section class="rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden p-6">
      <AgentMcpKeyPanel :agent-name="agentName" :notify="notify" />
    </section>

    <!-- Section 4: Reliability — dispatch circuit breaker (#526; honest toggle #1712) -->
    <section class="rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
      <ReliabilityPanel :agent-name="agentName" :notify="notify" />
    </section>

    <!-- Section 4: Voice replies (ent#117) — agent-level enable + voice selection.
         Per-channel on/off flags live in each channel's panel (Sharing tab). -->
    <section class="rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden p-6">
      <h3 class="text-base font-semibold text-gray-900 dark:text-gray-100">Voice</h3>
      <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
        Give this agent the ability to reply with a spoken voice note on messaging channels.
      </p>
      <VoiceRepliesControl :agent-name="agentName" />
    </section>
  </div>
</template>

<script setup>
import GuardrailsPanel from '../GuardrailsPanel.vue'
import CapacityPanel from '../CapacityPanel.vue'
import McpExposedPanel from '../McpExposedPanel.vue'
import AgentMcpKeyPanel from '../AgentMcpKeyPanel.vue'
import ReliabilityPanel from '../ReliabilityPanel.vue'
import VoiceRepliesControl from '../VoiceRepliesControl.vue'

defineProps({
  agentName: {
    type: String,
    required: true
  },
  notify: {
    type: Function,
    default: null
  }
})
</script>
