<template>
  <!-- Operator controls over signed-in portal clients (ent#281). Entitlement-gated:
       hidden entirely in OSS / unentitled builds, never a blank or broken section. -->
  <div v-if="entitled" class="mt-6 pt-6 border-t border-gray-200 dark:border-gray-700">
    <div class="flex items-start justify-between gap-3">
      <div class="min-w-0">
        <h4 class="text-sm font-medium text-gray-900 dark:text-gray-100">Portal clients</h4>
        <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
          People you've shared this agent with, who sign in to the client portal by email.
          Sharing an agent to an email <em>is</em> their portal account.
        </p>
      </div>
      <button
        @click="load"
        :disabled="loading"
        class="shrink-0 text-xs px-2 py-1 rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
      >
        {{ loading ? 'Loading…' : 'Refresh' }}
      </button>
    </div>

    <p v-if="error" class="mt-3 text-xs text-status-error-700 dark:text-status-error-300">
      {{ error }}
    </p>

    <div
      v-if="!loading && clients.length === 0"
      class="mt-4 text-center py-6 text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-900/50 rounded-lg border border-dashed border-gray-300 dark:border-gray-700"
    >
      <p class="text-sm">No portal clients yet</p>
      <p class="text-xs mt-1">Share this agent with someone's email and they'll appear here.</p>
    </div>

    <ul
      v-else-if="clients.length"
      class="mt-4 divide-y divide-gray-200 dark:divide-gray-700 border border-gray-200 dark:border-gray-700 rounded-lg"
    >
      <li v-for="c in clients" :key="c.email" class="px-4 py-3">
        <div class="flex items-start justify-between gap-3 flex-wrap">
          <div class="min-w-0">
            <p class="text-sm font-medium text-gray-900 dark:text-gray-100 flex items-center gap-2">
              <span class="truncate">{{ c.email }}</span>
              <span
                v-if="c.blocked"
                class="shrink-0 inline-flex items-center text-xs font-medium rounded px-2 py-0.5 text-status-error-700 dark:text-status-error-300 bg-status-error-50 dark:bg-status-error-900/30"
              >Blocked</span>
            </p>
            <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
              <template v-if="c.blocked">
                Blocked {{ formatWhen(c.blocked_at) }}<template v-if="c.blocked_by_email"> by {{ c.blocked_by_email }}</template>
                <template v-if="c.block_reason"> — “{{ c.block_reason }}”</template>
              </template>
              <template v-else>
                Last active {{ formatWhen(c.last_active) }} · {{ c.message_count }} message{{ c.message_count === 1 ? '' : 's' }}
              </template>
            </p>
            <!-- Honest status: there is no live-session count to show. Portal
                 sessions are stateless JWTs with no server-side store, so the
                 only truthful signal is when sessions were last cut off. -->
            <p v-if="c.sessions_revoked_at" class="text-xs text-gray-400 dark:text-gray-500 mt-0.5">
              Sessions ended {{ formatWhen(c.sessions_revoked_at) }}
            </p>
          </div>

          <div class="flex items-center gap-2 shrink-0">
            <button
              v-if="!c.blocked"
              @click="logout(c)"
              :disabled="busy === c.email"
              class="text-xs px-2 py-1 rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
              title="End this client's active portal sessions. They can sign in again."
            >Log out</button>

            <!-- Block is platform-wide, so it is admin-only server-side. Hiding
                 it for non-admins avoids offering a button that always 403s. -->
            <button
              v-if="isAdmin && !c.blocked"
              @click="block(c)"
              :disabled="busy === c.email"
              class="text-xs px-2 py-1 rounded border border-status-error-300 dark:border-status-error-800 text-status-error-700 dark:text-status-error-300 hover:bg-status-error-50 dark:hover:bg-status-error-900/20 disabled:opacity-50"
              title="Stop this person signing in again, on any agent"
            >Block</button>

            <button
              v-if="isAdmin && c.blocked"
              @click="unblock(c)"
              :disabled="busy === c.email"
              class="text-xs px-2 py-1 rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
            >Unblock</button>
          </div>
        </div>

        <p v-if="notes[c.email]" class="mt-2 text-xs" :class="notes[c.email].ok
          ? 'text-status-success-700 dark:text-status-success-300'
          : 'text-status-error-700 dark:text-status-error-300'">
          {{ notes[c.email].text }}
        </p>
      </li>
    </ul>

    <p class="mt-3 text-xs text-gray-500 dark:text-gray-400">
      <strong>Log out</strong> ends this person's live sessions everywhere — a portal sign-in covers
      every agent shared with them, so there's no per-agent session to end. They can sign back in.
      <strong>Block</strong> keeps them out of the whole platform until an admin unblocks them; their
      chat history and memory are kept. To remove someone from <em>this agent only</em>, unshare them above.
    </p>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import api from '../api'
import { useEnterpriseStore } from '../stores/enterprise'
import { useRole } from '../composables/useRole'

const props = defineProps({
  agentName: { type: String, required: true },
})

const enterprise = useEnterpriseStore()
const entitled = computed(() => enterprise.isEntitled('client_portal'))
const { isAdmin } = useRole()

const clients = ref([])
const loading = ref(false)
const busy = ref(null)
const error = ref('')
const notes = ref({})

function setNote(email, text, ok) {
  notes.value = { ...notes.value, [email]: { text, ok } }
}

function formatWhen(ts) {
  if (!ts) return 'never'
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? 'unknown' : d.toLocaleString()
}

async function load() {
  if (!entitled.value || !props.agentName) return
  loading.value = true
  error.value = ''
  try {
    const { data } = await api.get(
      `/api/enterprise/client-portal/agents/${props.agentName}/clients`
    )
    clients.value = data.clients || []
  } catch (e) {
    // 404 = module not mounted; anything else is worth surfacing.
    clients.value = []
    if (e?.response?.status !== 404) {
      error.value = e?.response?.data?.detail || 'Could not load portal clients.'
    }
  } finally {
    loading.value = false
  }
}

async function logout(c) {
  busy.value = c.email
  try {
    const { data } = await api.post(
      `/api/enterprise/client-portal/agents/${props.agentName}/clients/${encodeURIComponent(c.email)}/logout`
    )
    // The backend reports honestly when the revoke did not land (Redis down);
    // saying "signed out" regardless is exactly the lie this feature must avoid.
    if (data.revoked) {
      setNote(c.email, 'Signed out of the portal everywhere.', true)
    } else {
      setNote(c.email, 'Could not end the session — their sign-in is still active. Block them to be certain.', false)
    }
    await load()
  } catch (e) {
    setNote(c.email, e?.response?.data?.detail || 'Log out failed.', false)
  } finally {
    busy.value = null
  }
}

async function block(c) {
  const reason = window.prompt(
    `Block ${c.email} from signing in to this Trinity — on every agent, not just this one.\n\n` +
    'Their chat history and memory are kept, and an admin can unblock them at any time.\n\n' +
    'Reason (optional):'
  )
  if (reason === null) return   // cancelled
  busy.value = c.email
  try {
    const { data } = await api.post(
      `/api/enterprise/client-portal/agents/${props.agentName}/clients/${encodeURIComponent(c.email)}/block`,
      { reason: reason || null }
    )
    setNote(
      c.email,
      data.sessions_revoked
        ? 'Blocked, and their active sessions were ended.'
        : 'Blocked. Their current session could not be ended, so it stays live until it expires — they cannot sign in again.',
      data.sessions_revoked,
    )
    await load()
  } catch (e) {
    setNote(c.email, e?.response?.data?.detail || 'Block failed.', false)
  } finally {
    busy.value = null
  }
}

async function unblock(c) {
  busy.value = c.email
  try {
    const { data } = await api.delete(
      `/api/enterprise/client-portal/agents/${props.agentName}/clients/${encodeURIComponent(c.email)}/block`
    )
    setNote(c.email, data.was_blocked ? 'Unblocked — they can sign in again.' : 'They were not blocked.', true)
    await load()
  } catch (e) {
    setNote(c.email, e?.response?.data?.detail || 'Unblock failed.', false)
  } finally {
    busy.value = null
  }
}

onMounted(load)
watch(() => props.agentName, load)
watch(entitled, (v) => { if (v) load() })
</script>
