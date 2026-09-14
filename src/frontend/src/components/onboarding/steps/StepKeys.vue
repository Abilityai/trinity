<!--
  Step `keys` (ent#582) — the optional platform keys: GitHub · email provider
  (Resend) · Gemini. Each is independently skippable: leave it empty and
  Continue. Every card says what the key enables, what skipping costs, where to
  get one, where the docs are, and where to add it later (Settings →
  Integrations, where the same field lives). A saved key marks the step done
  for this session (the registry has no state that says "optional keys done").
-->
<template>
  <div data-testid="first-run-step-keys">
    <FirstRunStepHeader
      kicker="Integrations"
      title="Other keys"
      lead="Each of these switches on one more capability. None of them blocks your first run — fill in what you have now and skip the rest."
      badge="Optional"
      schematic="keys"
    />

    <div class="mt-5 space-y-5">
      <BaseCard v-for="id in KEY_ORDER" :key="id">
        <PlatformKeyField :provider="id" first-run @saved="emit('complete')" />
      </BaseCard>
    </div>
  </div>
</template>

<script setup>
import BaseCard from '../../base/BaseCard.vue'
import PlatformKeyField from '../../settings/PlatformKeyField.vue'
import FirstRunStepHeader from '../FirstRunStepHeader.vue'
import { KEY_ORDER } from './credentialSteps'

defineProps({ ctx: { type: Object, default: () => ({}) } })
const emit = defineEmits(['complete', 'skip'])
</script>
