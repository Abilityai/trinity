<!--
  trinity-enterprise#1054 — invite a person, and the record of the invitations.

  Create: name, work email, role (Workspace-only by default) and expiry, opened
  pre-filled. The invitation lets that email sign in at /login until it is
  accepted, revoked or expires. While the terms gate is on, a person accepts
  the published terms version at first sign-in; with the gate on and no
  version published the server refuses to invite (422 `terms_version_unset`),
  named here next to the form.

  List: name · email · status · invited by · sent · accepted · terms version,
  with resend and revoke on a pending invitation. Revoking (or an expiry)
  removes the sign-in the invitation opened and suspends an account that never
  accepted; an account that accepted is left alone.
-->
<template>
  <div class="space-y-4" data-testid="invites-panel">
    <form
      class="rounded-lg border border-gray-200 dark:border-gray-750 bg-gray-50 dark:bg-gray-900 p-4 space-y-3"
      data-testid="invite-form"
      @submit.prevent="onCreate"
    >
      <div>
        <h4 class="text-sm font-medium text-gray-900 dark:text-gray-100">Invite a person</h4>
        <p class="mt-1 text-xs text-gray-600 dark:text-gray-300">
          They get an email with a sign-in link and sign in with a code sent to that address.
        </p>
      </div>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <BaseInput v-model="name" label="Name" placeholder="Maya Lopez" :disabled="creating" data-testid="invite-name" />
        <BaseInput v-model="email" type="email" label="Work email" placeholder="person@example.com" :disabled="creating" data-testid="invite-email" />
        <BaseSelect v-model="role" label="Role" :disabled="creating" data-testid="invite-role">
          <option v-for="r in PLATFORM_ROLES" :key="r" :value="r">{{ platformRoleLabel(r) }}</option>
        </BaseSelect>
        <BaseInput
          v-model="expiryDays"
          type="number"
          label="Expires after (days)"
          :disabled="creating"
          :error="expiryError"
          data-testid="invite-expiry"
        />
      </div>
      <InlineError :message="createError" :detail="createErrorDetail" @dismiss="createError = ''" />
      <div class="flex flex-wrap items-center gap-3">
        <BaseButton
          type="submit"
          :disabled="!email.trim() || !!expiryError"
          :loading="creating"
          loading-label="Sending…"
          data-testid="invite-send"
        >Send invite</BaseButton>
        <p
          v-if="result"
          class="text-sm"
          :class="result.sent ? 'text-status-success-700 dark:text-status-success-400' : 'text-status-warning-700 dark:text-status-warning-400'"
          role="status"
          data-testid="invite-result"
        >{{ result.text }}</p>
      </div>
    </form>

    <div data-testid="invites-action-error">
      <InlineError :message="actionError" :detail="actionErrorDetail" @dismiss="actionError = ''" />
    </div>

    <LoadFailed
      v-if="view.state === 'failed'"
      dense
      title="Couldn't load the invitations"
      message="The list could not be read, so it is not known who has been invited. Try again."
      :detail="detailOf(store.error)"
      :retrying="store.loading"
      @retry="store.load()"
    />

    <div v-else-if="view.state === 'loading'" aria-busy="true" data-testid="invites-loading">
      <span class="sr-only">Loading invitations…</span>
      <div v-for="n in 3" :key="n" class="h-9 mb-2 rounded bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" aria-hidden="true"></div>
    </div>

    <p v-else-if="view.state === 'empty'" class="text-sm text-gray-600 dark:text-gray-300" data-testid="invites-empty">
      No invitations yet. Invite a person above; they appear here with their status.
    </p>

    <div v-else>
      <p class="mb-2 text-xs text-gray-600 dark:text-gray-300 tabular-nums" data-testid="invites-total">
        {{ store.invites.length }} {{ store.invites.length === 1 ? 'invitation' : 'invitations' }}
      </p>
      <div class="max-h-96 overflow-auto rounded-lg border border-gray-200 dark:border-gray-750">
        <table class="min-w-full text-sm">
          <thead class="sticky top-0 bg-gray-100 dark:bg-gray-750">
            <tr>
              <th v-for="h in HEADERS" :key="h" scope="col" class="px-3 py-2 text-left font-mono text-[11px] uppercase tracking-wider text-gray-600 dark:text-gray-300">{{ h }}</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-200 dark:divide-gray-750 bg-white dark:bg-gray-800">
            <tr v-for="inv in store.invites" :key="inv.id" data-testid="invite-row">
              <td class="px-3 py-2 text-gray-900 dark:text-gray-100">{{ inv.name || '—' }}</td>
              <td class="px-3 py-2 text-gray-900 dark:text-gray-100 break-all">{{ inv.email }}</td>
              <td class="px-3 py-2">
                <BaseBadge size="sm" :variant="STATUS_VARIANT[inv.status] || 'neutral'" data-testid="invite-status">{{ inv.status }}</BaseBadge>
              </td>
              <td class="px-3 py-2 text-gray-600 dark:text-gray-300 break-all">{{ inviterOf(inv) }}</td>
              <td class="px-3 py-2 text-gray-600 dark:text-gray-300 tabular-nums">
                <time v-if="inv.email_sent_at" :datetime="inv.email_sent_at" :title="formatLocalDateTime(inv.email_sent_at)">{{ formatRelativeTime(inv.email_sent_at) }}</time>
                <span v-else>not sent</span>
              </td>
              <td class="px-3 py-2 text-gray-600 dark:text-gray-300 tabular-nums" data-testid="invite-accepted-at">
                <time v-if="inv.accepted_at" :datetime="inv.accepted_at" :title="formatLocalDateTime(inv.accepted_at)">{{ formatRelativeTime(inv.accepted_at) }}</time>
                <span v-else>—</span>
              </td>
              <td class="px-3 py-2 font-mono text-gray-900 dark:text-gray-100" data-testid="invite-terms-version">{{ termsVersionOf(inv) }}</td>
              <td class="px-3 py-2 whitespace-nowrap text-right">
                <template v-if="inv.status === 'pending'">
                  <BaseButton variant="ghost" size="sm" :disabled="busyId === inv.id" data-testid="invite-resend" @click="onResend(inv)">Resend</BaseButton>
                  <BaseButton variant="ghost" size="sm" :disabled="busyId === inv.id" data-testid="invite-revoke" @click="revoking = inv">Revoke</BaseButton>
                </template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <ConfirmDialog
      :visible="!!revoking"
      title="Revoke this invitation?"
      :message="revokeMessage"
      confirm-text="Revoke invitation"
      variant="danger"
      @confirm="onRevoke"
      @cancel="revoking = null"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import BaseSelect from '../base/BaseSelect.vue'
import ConfirmDialog from '../ConfirmDialog.vue'
import InlineError from '../InlineError.vue'
import LoadFailed from '../LoadFailed.vue'
import { useInvitesStore } from '../../stores/invites'
import { viewState } from '../../utils/loadingState'
import { formatLocalDateTime, formatRelativeTime } from '../../utils/timestamps'
import { PLATFORM_ROLES, platformRoleLabel } from '../../utils/workspaceOnly'

const props = defineProps({
  // Platform users, to name who sent each invitation (`invited_by` is a user id).
  users: { type: Array, default: () => [] },
})

const HEADERS = ['Name', 'Email', 'Status', 'Invited by', 'Sent', 'Accepted', 'Terms version', '']
const STATUS_VARIANT = { pending: 'info', accepted: 'success', expired: 'warning', revoked: 'neutral' }
const DEFAULT_EXPIRY_DAYS = 14

const store = useInvitesStore()
const view = computed(() => viewState({ hasLoaded: store.hasLoaded, error: store.error, count: store.invites.length }))

const name = ref('')
const email = ref('')
const role = ref('user')
const expiryDays = ref(String(DEFAULT_EXPIRY_DAYS))
const creating = ref(false)
const createError = ref('')
const createErrorDetail = ref('')
const result = ref(null)

const busyId = ref(null)
const revoking = ref(null)
const actionError = ref('')
const actionErrorDetail = ref('')

const expiryError = computed(() => {
  const n = Number(expiryDays.value)
  return Number.isInteger(n) && n >= 1 && n <= 365 ? '' : 'Enter a whole number of days from 1 to 365, for example 14.'
})

const revokeMessage = computed(() => (revoking.value
  ? `Revoke the invitation to ${revoking.value.email}? They can no longer sign in with it. If they never accepted the terms, their account is suspended; an account that already accepted is not changed.`
  : ''))

function detailOf(e) {
  if (!e) return ''
  const d = e.response?.data?.detail
  return String((d && (d.message || d.code)) || d || e.message || '')
}

/** What a refused write says: the server's words, or the fix for a named code. */
function messageOf(e, fallback) {
  const d = e?.response?.data?.detail
  if (d?.code === 'terms_version_unset') {
    return 'Set the terms version first: the terms gate is on but no terms version is published, so an invited person could not accept anything. Publish terms_version (and terms_text) in Settings, then send the invite.'
  }
  if (d && typeof d === 'object' && d.message) return d.message
  if (typeof d === 'string' && d) return d
  return fallback
}

function inviterOf(inv) {
  if (!inv.invited_by) return '—'
  const u = props.users.find((x) => String(x.id) === String(inv.invited_by))
  return u ? (u.email || u.username) : `user #${inv.invited_by}`
}

function termsVersionOf(inv) {
  if (inv.status !== 'accepted') return '—'
  return store.acceptanceByEmail[(inv.email || '').toLowerCase()]?.terms_version || '—'
}

async function onCreate() {
  if (creating.value || !email.value.trim() || expiryError.value) return
  creating.value = true
  createError.value = ''
  createErrorDetail.value = ''
  result.value = null
  const sentTo = email.value.trim()
  try {
    const invite = await store.create({
      email: sentTo,
      name: name.value.trim() || null,
      role: role.value,
      expires_days: Number(expiryDays.value),
    })
    result.value = invite.email_sent === false
      ? { sent: false, text: `Invited ${sentTo}, but the email was not sent. Share the sign-in page (/login) with them, or use Resend once email is set up.` }
      : { sent: true, text: `Invited ${sentTo}. The email with the sign-in link is on its way.` }
    name.value = ''
    email.value = ''
    role.value = 'user'
    expiryDays.value = String(DEFAULT_EXPIRY_DAYS)
  } catch (e) {
    createError.value = messageOf(e, 'The invitation was not created. Check the address and try again.')
    createErrorDetail.value = detailOf(e)
  } finally {
    creating.value = false
  }
}

async function runAction(inv, fn, fallback) {
  busyId.value = inv.id
  actionError.value = ''
  actionErrorDetail.value = ''
  try {
    await fn()
  } catch (e) {
    actionError.value = messageOf(e, fallback)
    actionErrorDetail.value = detailOf(e)
  } finally {
    busyId.value = null
  }
}

function onResend(inv) {
  return runAction(inv, () => store.resend(inv.id), `The invitation to ${inv.email} was not resent. Try again.`)
}

function onRevoke() {
  const inv = revoking.value
  revoking.value = null
  if (!inv) return undefined
  return runAction(inv, () => store.revoke(inv.id), `The invitation to ${inv.email} was not revoked; they can still sign in. Try again.`)
}

onMounted(() => store.load())
</script>
