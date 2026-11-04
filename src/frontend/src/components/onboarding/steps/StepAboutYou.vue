<!--
  Step `profile` (Workspace Build): who this person is, which sets how their guide
  talks to them. The answers are saved on the account, and the sign-in
  email shown here is what makes signing in as admin and with an email the same person.
-->
<template>
  <div data-testid="first-run-step-profile">
    <FirstRunStepHeader
      kicker="You"
      title="About you"
      lead="So your guide knows how much to explain. There are no wrong answers."
      badge="Recommended"
    />
    <section class="mt-5 rounded-lg border border-gray-200 dark:border-gray-750 p-3" data-testid="first-run-identity">
      <template v-if="ctx.hasEmail && !changing">
        <div class="flex items-center justify-between gap-3">
          <p class="min-w-0 truncate text-sm text-gray-900 dark:text-gray-100">
            Your sign-in email: <span class="font-[550]" data-testid="first-run-identity-email">{{ ctx.userEmail }}</span>
          </p>
          <BaseButton variant="ghost" size="sm" data-testid="first-run-identity-change" @click="changing = true">Change</BaseButton>
        </div>
        <p class="mt-1 text-[12.5px] text-gray-500 dark:text-gray-400">
          Your answers stay with this account, so Trinity knows it's you whichever way you sign in.
          <template v-if="ctx.emailDeliveryAvailable === false">Sign-in codes by email need an email provider, which you can add under Optional extras.</template>
        </p>
      </template>
      <template v-else>
        <p class="mb-2 text-sm text-gray-900 dark:text-gray-100">What email will you sign in with?</p>
        <p class="mb-2 text-[12.5px] text-gray-500 dark:text-gray-400">
          It links to this account, so Trinity knows it's you whichever way you sign in.
        </p>
        <SignInEmailForm @saved="changing = false">
          <template #actions="{ saving }">
            <BaseButton v-if="ctx.hasEmail" variant="ghost" size="sm" :disabled="saving" data-testid="first-run-identity-keep" @click="changing = false">Keep {{ ctx.userEmail }}</BaseButton>
          </template>
        </SignInEmailForm>
      </template>
    </section>
    <BuildQuestions group="you" :intake="ctx.buildIntake" @update-intake="$emit('update-intake', $event)" @answered="$emit('complete')" />
  </div>
</template>

<script setup>
import { ref } from 'vue'
import BaseButton from '../../base/BaseButton.vue'
import FirstRunStepHeader from '../FirstRunStepHeader.vue'
import BuildQuestions from './BuildQuestions.vue'
import SignInEmailForm from './SignInEmailForm.vue'

defineProps({ ctx: { type: Object, default: () => ({}) } })
defineEmits(['update-intake', 'complete', 'skip'])

const changing = ref(false)
</script>
