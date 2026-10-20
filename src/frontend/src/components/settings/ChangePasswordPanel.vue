<template>
  <!--
    #3825 — change the signed-in account's password. Rendered by Settings only
    for an account that has one (`/api/users/me` → has_password): email-only
    accounts sign in with a code and have nothing to change here.
  -->
  <BaseCard data-testid="change-password-panel">
    <h2 class="text-lg font-medium text-gray-900 dark:text-white">Password</h2>
    <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
      Change the password you sign in with. Every other signed-in session is signed out; this one stays.
    </p>

    <form class="mt-4 max-w-md space-y-3" @submit.prevent="submit">
      <BaseInput
        v-model="current"
        type="password"
        label="Current password"
        autocomplete="current-password"
        :error="fieldErrors.current"
        :disabled="saving"
        data-testid="current-password"
      />
      <BaseInput
        v-model="next"
        type="password"
        label="New password"
        autocomplete="new-password"
        help="At least 12 characters, with an uppercase and a lowercase letter, a digit and a symbol."
        :error="fieldErrors.next"
        :disabled="saving"
        data-testid="new-password"
      />
      <BaseInput
        v-model="confirm"
        type="password"
        label="Confirm new password"
        autocomplete="new-password"
        :error="fieldErrors.confirm"
        :disabled="saving"
        data-testid="confirm-password"
      />

      <label class="flex items-start gap-2 text-sm text-gray-700 dark:text-gray-300">
        <input
          v-model="revokeKeys"
          type="checkbox"
          :disabled="saving"
          class="mt-0.5 h-4 w-4 text-action-primary-600 focus:ring-action-primary-500 border-gray-300 dark:border-gray-600 rounded"
          data-testid="revoke-api-keys"
        />
        <span>
          Also revoke the MCP API keys I created.
          <span class="block text-gray-500 dark:text-gray-400">
            Keys are not signed out by a password change. Your agents' own keys are not affected.
          </span>
        </span>
      </label>

      <InlineError :message="formError" />

      <div class="flex items-center gap-3">
        <BaseButton
          type="submit"
          :disabled="!canSubmit"
          :loading="saving"
          loading-label="Changing…"
          data-testid="change-password-submit"
        >
          Change password
        </BaseButton>
        <span v-if="done" class="text-sm text-status-success-700 dark:text-status-success-400" data-testid="change-password-done">
          Password changed.
        </span>
      </div>

      <p
        v-if="done && done.api_keys_revoked"
        class="text-sm text-gray-600 dark:text-gray-300"
        data-testid="api-keys-revoked"
      >
        Revoked {{ done.api_keys_revoked }} API {{ done.api_keys_revoked === 1 ? 'key' : 'keys' }} you created.
      </p>
      <p
        v-if="done && done.api_keys_active"
        class="text-sm text-status-warning-700 dark:text-status-warning-400"
        data-testid="api-keys-active"
      >
        {{ done.api_keys_active }} API {{ done.api_keys_active === 1 ? 'key' : 'keys' }} you created still
        {{ done.api_keys_active === 1 ? 'works' : 'work' }} — a password change does not sign keys out. Review them in
        <router-link to="/settings?tab=mcp-keys" class="underline">Settings → MCP Keys</router-link>.
      </p>
      <p
        v-if="done && !done.other_sessions_signed_out"
        class="text-sm text-status-warning-700 dark:text-status-warning-400"
        data-testid="sessions-not-signed-out"
      >
        Your other sessions could not be signed out because the session store was unavailable. Restart the platform to sign them out.
      </p>
      <p
        v-if="done && done.admin_env_stale"
        class="text-sm text-gray-600 dark:text-gray-300"
        data-testid="env-stale"
      >
        <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs">ADMIN_PASSWORD</code>
        in <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs">.env</code> still holds the old
        password. A restart keeps your new one, but update the file so the MCP server's password sign-in and scripts
        that read it keep working.
      </p>
    </form>
  </BaseCard>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import BaseButton from '../base/BaseButton.vue'
import BaseCard from '../base/BaseCard.vue'
import BaseInput from '../base/BaseInput.vue'
import InlineError from '../InlineError.vue'
import { useAuthStore } from '../../stores/auth'

const MIN_LENGTH = 12

const auth = useAuthStore()
const current = ref('')
const next = ref('')
const confirm = ref('')
const revokeKeys = ref(false)
const saving = ref(false)
const formError = ref('')
const serverErrors = reactive({ current: '', next: '' })
// The last successful change's response; null until one lands.
const done = ref(null)

const fieldErrors = computed(() => ({
  current: serverErrors.current,
  next: serverErrors.next
    || (next.value && next.value.length < MIN_LENGTH
      ? `Use at least ${MIN_LENGTH} characters (currently ${next.value.length}).`
      : ''),
  confirm: confirm.value && confirm.value !== next.value ? "Doesn't match the new password." : '',
}))

const canSubmit = computed(() =>
  !saving.value
  && current.value !== ''
  && next.value.length >= MIN_LENGTH
  && confirm.value === next.value,
)

const MESSAGES = {
  invalid_current_password: 'The current password is wrong.',
  too_many_attempts: 'Too many wrong current passwords. Wait 15 minutes, then try again.',
  password_unchanged: 'The new password is the same as the current one. Choose a different one.',
  no_password: 'This account signs in with an emailed code and has no password to change.',
}

async function submit() {
  if (!canSubmit.value) return
  saving.value = true
  formError.value = ''
  serverErrors.current = ''
  serverErrors.next = ''
  done.value = null
  try {
    done.value = await auth.changePassword(current.value, next.value, revokeKeys.value)
    current.value = ''
    next.value = ''
    confirm.value = ''
  } catch (e) {
    const detail = e?.response?.data?.detail
    const code = detail?.code
    if (code === 'invalid_current_password') {
      serverErrors.current = MESSAGES[code]
    } else if (code === 'weak_password' || code === 'password_unchanged') {
      serverErrors.next = code === 'weak_password'
        ? (detail.errors || []).join(' ')
        : MESSAGES[code]
    } else {
      formError.value = MESSAGES[code] || detail?.message || "The password couldn't be changed. Try again."
    }
  } finally {
    saving.value = false
  }
}
</script>
