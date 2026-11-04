<!--
  Step `email` (ent#581) — absorbs FinishSetupCard section 1 (#2381).

  FALLBACK ONLY. A marketplace operator binds their email while claiming the
  instance at /setup (ent#580), so the registry predicate is false and this
  step never appears — the "no duplicate asks" AC is that predicate, not a
  promise in this file. It survives for the ADMIN_PASSWORD-provisioned install,
  where nobody was ever asked. The field is here rather than a link to
  Settings, because leaving the Dashboard would leave the sequence.
-->
<template>
  <div data-testid="first-run-step-email">
    <FirstRunStepHeader
      kicker="Sign-in"
      :title="ctx.hasEmail ? 'Sign-in email is set' : 'Add a sign-in email'"
      :lead="lead"
      :badge="ctx.hasEmail ? 'Done' : 'Optional'"
      schematic="email"
    />

    <SignInEmailForm v-if="!ctx.hasEmail" class="mt-5" @saved="$emit('complete')" />
    <!-- A warning they have to act on, so the design system's alert, not body text. -->
    <div
      v-if="ctx.emailDeliveryAvailable === false"
      class="mt-4 flex max-w-lg items-start gap-2.5 rounded-lg bg-status-warning-100 px-3.5 py-3 text-sm text-status-warning-700 dark:bg-status-warning-500/16 dark:text-status-warning-300"
      role="status"
      data-testid="first-run-email-no-provider"
    >
      <svg class="mt-0.5 h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true">
        <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z" />
      </svg>
      <p>
        <strong class="font-semibold">Signing in with a code sent by email won't work yet.</strong>
        This install can't send email. To turn it on, add an email provider under Optional extras, later in this setup.
      </p>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import FirstRunStepHeader from '../FirstRunStepHeader.vue'
import SignInEmailForm from './SignInEmailForm.vue'

const props = defineProps({ ctx: { type: Object, default: () => ({}) } })
defineEmits(['complete', 'skip'])

const lead = computed(() =>
  props.ctx.hasEmail
    ? `You can sign in with ${props.ctx.userEmail} and your admin password. Change it in Settings → General.`
    : 'You sign in with the username admin today. Add an email and you can sign in with that and your password instead.'
)
</script>
