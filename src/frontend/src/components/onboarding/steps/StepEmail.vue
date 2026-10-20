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

    <form v-if="!ctx.hasEmail" class="mt-5 max-w-md space-y-2" novalidate @submit.prevent="save">
      <BaseInput
        v-model="email"
        type="email"
        label="Email"
        placeholder="you@company.com"
        autocomplete="email"
        :error="fieldError"
        :disabled="saving || codeSent"
        :help="codeSent ? '' : 'We\'ll email a 6-digit code to confirm the address is yours.'"
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
import FirstRunStepHeader from '../FirstRunStepHeader.vue'

const props = defineProps({ ctx: { type: Object, default: () => ({}) } })
const emit = defineEmits(['complete', 'skip'])

const auth = useAuthStore()

// Mirrors the backend _EMAIL_RE shape check, as /setup does.
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s.]+$/

const lead = computed(() =>
  props.ctx.hasEmail
    ? `You can sign in with ${props.ctx.userEmail} and your password. Change it in Settings → General.`
    : 'You sign in with the username admin today. Bind an email and you can sign in with that and your password instead.'
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
    emit('complete')
  } catch (e) {
    saveError.value = apiErrorMessage(e, 'Could not save the email. Try again.')
  } finally {
    saving.value = false
  }
}
</script>
