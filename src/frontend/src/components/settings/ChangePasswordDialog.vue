<template>
  <BaseModal
    :model-value="modelValue"
    labelledby="change-password-title"
    :close-on-backdrop="false"
    panel-class="relative w-full max-w-md rounded-lg bg-white p-6 shadow-xl dark:bg-gray-800"
    @update:model-value="(v) => { if (!v) close() }"
  >
    <div data-testid="change-password-dialog">
      <div class="flex items-baseline justify-between gap-3">
        <h2 id="change-password-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">
          Change password
        </h2>
        <span
          v-if="step !== 'done'"
          class="text-[12.5px] tabular-nums text-gray-500 dark:text-gray-400"
          data-testid="step-counter"
        >Step {{ stepNumber }} of {{ totalSteps }}</span>
      </div>

      <!-- One footprint for every step (principle 4): the body reserves the
           tallest step's height so the dialog does not jump between steps. -->
      <form class="mt-4 flex min-h-[19rem] flex-col" novalidate @submit.prevent="onPrimary">
        <div class="flex-1 space-y-4">
          <!-- Step 1 — the current password -->
          <template v-if="step === 'current'">
            <p class="text-sm text-gray-600 dark:text-gray-300">
              Confirm it's you before anything changes.
            </p>
            <BaseInput
              v-model="currentPassword"
              type="password"
              label="Current password"
              autocomplete="current-password"
              data-testid="current-password"
              :error="fieldError.current"
            />
          </template>

          <!-- Step 2 — second factor, only when the 2FA gate says so -->
          <template v-else-if="step === 'second'">
            <p class="text-sm text-gray-600 dark:text-gray-300">
              Your account uses two-factor authentication.
            </p>
            <BaseInput
              v-model="secondFactorCode"
              label="Authentication code"
              help="The 6-digit code from your authenticator app."
              autocomplete="one-time-code"
              inputmode="numeric"
              data-testid="second-factor-code"
              :error="fieldError.second"
            />
          </template>

          <!-- Step 3 — the new password -->
          <template v-else-if="step === 'new'">
            <BaseInput
              v-model="newPassword"
              type="password"
              label="New password"
              autocomplete="new-password"
              data-testid="new-password"
              :error="fieldError.new"
            />
            <ul class="grid grid-cols-2 gap-x-3 gap-y-1 text-[12.5px]" aria-label="Password requirements" data-testid="requirements">
              <li
                v-for="r in requirements"
                :key="r.id"
                class="flex items-center gap-1.5"
                :class="r.met ? 'text-status-success-700 dark:text-status-success-400' : 'text-gray-500 dark:text-gray-400'"
                :data-met="r.met ? 'true' : 'false'"
              >
                <svg v-if="r.met" class="h-3.5 w-3.5 flex-none" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7" />
                </svg>
                <svg v-else class="h-3.5 w-3.5 flex-none" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                  <circle cx="12" cy="12" r="7" stroke-width="2" />
                </svg>
                <span>{{ r.label }}<span class="sr-only">{{ r.met ? ' — met' : ' — not met' }}</span></span>
              </li>
            </ul>
            <BaseInput
              v-model="confirmPassword"
              type="password"
              label="Confirm new password"
              autocomplete="new-password"
              data-testid="confirm-password"
              :error="confirmError"
            />
          </template>

          <!-- Done -->
          <template v-else>
            <div class="flex flex-col items-center py-6 text-center" data-testid="change-password-done">
              <svg class="h-10 w-10 text-status-success-700 dark:text-status-success-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              <p class="mt-3 text-base font-semibold text-gray-900 dark:text-gray-100">Password updated</p>
              <p v-if="otherSessionsSignedOut" class="mt-1 text-sm text-gray-600 dark:text-gray-300">
                Your other sessions were signed out. This tab stays signed in.
              </p>
              <p v-else class="mt-1 text-sm text-gray-600 dark:text-gray-300">
                Other sessions could not be signed out automatically. Sign out on your other devices.
              </p>
            </div>
          </template>

          <InlineError :message="formError" @dismiss="formError = ''" />
        </div>

        <div class="mt-6 flex justify-end gap-2">
          <template v-if="step === 'done'">
            <BaseButton type="submit" data-testid="done-button">Done</BaseButton>
          </template>
          <template v-else>
            <BaseButton
              variant="secondary"
              data-testid="back-button"
              @click="step === 'current' ? close() : back()"
            >{{ step === 'current' ? 'Cancel' : 'Back' }}</BaseButton>
            <BaseButton
              type="submit"
              data-testid="primary-button"
              :disabled="!canContinue"
              :loading="store.submitting"
              :loading-label="step === 'new' ? 'Changing…' : 'Checking…'"
            >{{ step === 'new' ? 'Change password' : 'Continue' }}</BaseButton>
          </template>
        </div>
      </form>
    </div>
  </BaseModal>
</template>

<script setup>
/**
 * trinity-enterprise#709 — change your own password, stepped:
 *   1 current password → 2 second factor (only when the 2FA gate requires it)
 *   → 3 new + confirm (first-run rules as inline hints) → done.
 *
 * The steps are UX; `PUT /api/users/me/password` re-checks everything. The
 * dialog holds the current password in memory between steps and clears every
 * field on close.
 */
import { computed, reactive, ref, watch } from 'vue'
import BaseModal from '../base/BaseModal.vue'
import BaseInput from '../base/BaseInput.vue'
import BaseButton from '../base/BaseButton.vue'
import InlineError from '../InlineError.vue'
import { useAccountPasswordStore } from '../../stores/accountPassword'
import { passwordRequirements, meetsPasswordRequirements } from '../../utils/passwordRules'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue', 'changed'])

const store = useAccountPasswordStore()

const step = ref('current')
const secondFactorRequired = ref(false)
const currentPassword = ref('')
const secondFactorCode = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const otherSessionsSignedOut = ref(true)
const formError = ref('')
const fieldError = reactive({ current: '', second: '', new: '' })

const totalSteps = computed(() => (secondFactorRequired.value ? 3 : 2))
const stepNumber = computed(() => {
  if (step.value === 'current') return 1
  if (step.value === 'second') return 2
  return totalSteps.value
})

const requirements = computed(() => passwordRequirements(newPassword.value))
const mismatch = computed(() => confirmPassword.value !== '' && confirmPassword.value !== newPassword.value)
const sameAsCurrent = computed(() => newPassword.value !== '' && newPassword.value === currentPassword.value)
const confirmError = computed(() => {
  if (mismatch.value) return "Passwords don't match — type the same new password in both fields."
  return ''
})

const canContinue = computed(() => {
  if (step.value === 'current') return currentPassword.value !== ''
  if (step.value === 'second') return secondFactorCode.value.trim() !== ''
  if (step.value === 'new') {
    return meetsPasswordRequirements(newPassword.value)
      && confirmPassword.value === newPassword.value
      && !sameAsCurrent.value
  }
  return true
})

watch(sameAsCurrent, (same) => {
  fieldError.new = same ? 'The new password must be different from your current one.' : ''
})

function clearErrors() {
  formError.value = ''
  fieldError.current = ''
  fieldError.second = ''
  if (!sameAsCurrent.value) fieldError.new = ''
}

function reset() {
  step.value = 'current'
  secondFactorRequired.value = false
  currentPassword.value = ''
  secondFactorCode.value = ''
  newPassword.value = ''
  confirmPassword.value = ''
  otherSessionsSignedOut.value = true
  formError.value = ''
  fieldError.current = ''
  fieldError.second = ''
  fieldError.new = ''
}

function close() {
  reset()
  emit('update:modelValue', false)
}

function back() {
  clearErrors()
  if (step.value === 'new' && secondFactorRequired.value) step.value = 'second'
  else step.value = 'current'
}

// Where a refusal belongs: next to the field it is about, on its own step.
const CODE_STEP = {
  current_password_incorrect: ['current', 'current'],
  too_many_attempts: ['current', null],
  second_factor_required: ['second', 'second'],
  second_factor_invalid: ['second', 'second'],
  password_mismatch: ['new', null],
  password_too_weak: ['new', 'new'],
  password_unchanged: ['new', 'new'],
}

function showRefusal(error) {
  const [target, field] = CODE_STEP[error.code] || [null, null]
  if (target === 'second' && !secondFactorRequired.value) secondFactorRequired.value = true
  if (target) step.value = target
  if (field) fieldError[field] = error.message
  else formError.value = error.message
}

async function onPrimary() {
  if (step.value === 'done') return close()
  if (!canContinue.value || store.submitting) return
  clearErrors()

  if (step.value === 'current') {
    const res = await store.verifyCurrent(currentPassword.value)
    if (!res.ok) return showRefusal(res.error)
    if (res.data.second_factor_enrollment_required) {
      formError.value = 'Your account requires two-factor authentication. Set it up in Settings → Security, then change your password.'
      return
    }
    secondFactorRequired.value = !!res.data.second_factor_required
    step.value = secondFactorRequired.value ? 'second' : 'new'
    return
  }

  if (step.value === 'second') {
    step.value = 'new'
    return
  }

  const res = await store.change({
    currentPassword: currentPassword.value,
    newPassword: newPassword.value,
    confirmPassword: confirmPassword.value,
    secondFactorCode: secondFactorRequired.value ? secondFactorCode.value.trim() : null,
  })
  if (!res.ok) return showRefusal(res.error)
  otherSessionsSignedOut.value = res.data?.other_sessions_signed_out !== false
  // Nothing sensitive stays in memory once the change is done.
  currentPassword.value = ''
  secondFactorCode.value = ''
  newPassword.value = ''
  confirmPassword.value = ''
  step.value = 'done'
  emit('changed')
}

watch(() => props.modelValue, (open) => { if (!open) reset() })
</script>
