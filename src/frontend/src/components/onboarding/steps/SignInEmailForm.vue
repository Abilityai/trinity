<!--
  The sign-in email form: an address, then the code that proves the mailbox (ent#720).
  An admin on an install that can't send email binds it without a code, and the backend
  audits that as unverified. Used by the `email` and `profile` setup steps.
-->
<template>
  <div>
    <form class="max-w-md space-y-2" novalidate @submit.prevent="save">
      <BaseInput
        v-model="email"
        type="email"
        label="Email"
        placeholder="you@company.com"
        autocomplete="email"
        :error="fieldError"
        :disabled="saving || codeSent"
        :help="codeSent ? '' : emailHelp"
        data-testid="first-run-email-input"
      />
      <!-- ent#720: binding a sign-in email needs proof of the mailbox. -->
      <BaseInput
        v-if="codeSent"
        v-model="code"
        label="Confirmation code"
        placeholder="6-digit code"
        inputmode="numeric"
        autocomplete="one-time-code"
        :disabled="saving"
        :help="`Sent to ${email.trim()}. It expires in 10 minutes.`"
        data-testid="first-run-email-code"
      />
      <div class="flex items-center gap-2">
        <BaseButton
          type="submit"
          variant="secondary"
          size="sm"
          :loading="saving"
          :loading-label="codeSent ? 'Confirming…' : 'Saving…'"
          :disabled="!email.trim() || (codeSent && code.trim().length !== 6)"
          data-testid="first-run-email-save"
        >
          {{ codeSent ? 'Confirm email' : 'Save email' }}
        </BaseButton>
        <BaseButton
          v-if="codeSent"
          variant="ghost"
          size="sm"
          :disabled="saving"
          data-testid="first-run-email-change"
          @click="reset"
        >
          Use a different email
        </BaseButton>
        <slot name="actions" :saving="saving"></slot>
      </div>
    </form>
    <InlineError v-if="saveError" class="mt-3" :message="saveError" @dismiss="saveError = ''" />
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useAuthStore } from '../../../stores/auth'
import BaseButton from '../../base/BaseButton.vue'
import BaseInput from '../../base/BaseInput.vue'
import InlineError from '../../InlineError.vue'
import { apiErrorMessage } from '../../../utils/apiError'

const emit = defineEmits(['saved'])

const auth = useAuthStore()

// Mirrors the backend _EMAIL_RE shape check, as /setup does.
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s.]+$/

const emailHelp = computed(() =>
  auth.emailDeliveryAvailable === false
    ? "This install can't send email yet, so the address is saved without a confirmation code."
    : "We'll email a 6-digit code to confirm the address is yours."
)

const email = ref('')
const code = ref('')
const codeSent = ref(false)
const saving = ref(false)
const fieldError = ref('')
const saveError = ref('')

function reset() {
  codeSent.value = false
  code.value = ''
  saveError.value = ''
}

async function save() {
  const value = email.value.trim()
  if (!EMAIL_RE.test(value)) {
    fieldError.value = 'Enter a full email address — for example you@company.com'
    return
  }
  fieldError.value = ''
  saveError.value = ''
  saving.value = true
  try {
    const out = await auth.bindOwnEmail(value, codeSent.value ? code.value.trim() : null)
    if (out.needsCode) {
      codeSent.value = true
      return
    }
    email.value = ''
    reset()
    emit('saved')
  } catch (e) {
    saveError.value = apiErrorMessage(e, 'Could not save the email. Try again.')
  } finally {
    saving.value = false
  }
}
</script>
