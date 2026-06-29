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
      Resources (/resources), Reliability (/circuit-breaker),
      Compute/Auth (api-key, /github-pat), Git sync (/git/auto-sync,
      /git/freeze-schedules-if-failing).
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
  </div>
</template>

<script setup>
import GuardrailsPanel from '../GuardrailsPanel.vue'
import CapacityPanel from '../CapacityPanel.vue'

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
