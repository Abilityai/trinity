<template>
  <!-- System Credential Vault (trinity-enterprise#279). Entitlement-gated at the
       tab level (ALL_TABS `requires: 'credential_vault'`); the backend
       `require_vault_admin` (admin AND an interactive human) is the real
       boundary, so an entitled non-admin who reaches this tab gets a named
       admin-required state below rather than a wall of error toasts. -->
  <div class="space-y-8">
    <!-- Header -->
    <div>
      <h3 class="text-lg font-semibold text-gray-900 dark:text-gray-100">System Credential Vault</h3>
      <p class="mt-1 text-sm text-gray-600 dark:text-gray-400">
        Store a shared secret once and grant it to agents. An agent fetches a granted value by
        name at runtime; the value is scrubbed from its saved transcript. Values are encrypted
        (AES-256-GCM) and are never shown again after you save them.
      </p>
    </div>

    <!-- Admin-required state — the panel's honest 403 (an entitled non-admin, or
         an MCP-key principal, reaches the tab but not the control plane). -->
    <div
      v-if="adminRequired"
      class="p-4 border border-gray-200 dark:border-gray-700 rounded-lg bg-gray-50 dark:bg-gray-800/50"
    >
      <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Administrator access required</p>
      <p class="mt-1 text-sm text-gray-600 dark:text-gray-400">
        Managing vault credentials and grants requires an administrator signed in interactively
        (not an API key). Ask an administrator to add credentials or grant one to your agent.
      </p>
    </div>

    <template v-else>
      <!-- Decrypt-failure state — surfaced ONLY when a rewrap has REPORTED
           failures (the backend exposes no other decrypt-health read). This is
           the loud remediation banner; the routine Rewrap action lives in the
           tucked maintenance disclosure below. -->
      <div
        v-if="decryptFailure"
        class="p-4 border rounded-lg border-status-warning-300 dark:border-status-warning-700 bg-status-warning-50 dark:bg-status-warning-900/30"
      >
        <p class="text-sm font-medium text-status-warning-800 dark:text-status-warning-200">
          {{ rewrapResult.failed }} credential(s) could not be decrypted
        </p>
        <p class="mt-1 text-sm text-status-warning-700 dark:text-status-warning-300">
          These entries were encrypted under a key this instance no longer has. Restore the
          previous key as <code>CREDENTIAL_ENCRYPTION_KEY_SECONDARY</code>, then re-encrypt the
          vault. Until then, agents fetching those credentials get a decrypt error.
        </p>
        <button
          type="button"
          @click="rewrap"
          :disabled="busy"
          class="mt-3 text-xs px-3 py-1.5 rounded bg-action-primary-600 text-white hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {{ busy ? 'Re-encrypting…' : 'Re-encrypt vault entries' }}
        </button>
      </div>

      <p v-if="error" class="text-sm text-status-danger-600 dark:text-status-danger-400">{{ error }}</p>

      <!-- Entries -->
      <section class="space-y-3">
        <h4 class="text-sm font-medium text-gray-800 dark:text-gray-200">Credentials</h4>

        <p v-if="loading && !entries.length" class="text-sm text-gray-500 dark:text-gray-400">Loading…</p>

        <div v-else-if="!entries.length" class="text-sm text-gray-500 dark:text-gray-400">
          No credentials yet — add one below.
        </div>

        <div
          v-for="e in entries"
          :key="e.id"
          class="p-3 border border-gray-200 dark:border-gray-700 rounded-lg"
        >
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <p class="font-medium text-gray-900 dark:text-gray-100 truncate">
                {{ e.name }}
                <span class="ml-1 text-xs text-gray-400">{{ e.kind }}</span>
              </p>
              <p v-if="e.description" class="text-xs text-gray-500 dark:text-gray-400 truncate">{{ e.description }}</p>
              <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                Granted to {{ e.grant_count }} agent{{ e.grant_count === 1 ? '' : 's' }}
              </p>
            </div>
            <div class="flex items-center gap-2 shrink-0">
              <button
                type="button"
                @click="toggleEdit(e)"
                class="text-xs px-2 py-1 border border-gray-300 dark:border-gray-600 rounded hover:bg-gray-50 dark:hover:bg-gray-700 text-gray-700 dark:text-gray-200"
              >Update value</button>
              <button
                type="button"
                @click="toggleGrants(e)"
                class="text-xs px-2 py-1 border border-gray-300 dark:border-gray-600 rounded hover:bg-gray-50 dark:hover:bg-gray-700 text-gray-700 dark:text-gray-200"
              >Grants</button>
              <button
                type="button"
                @click="remove(e)"
                :disabled="busy"
                class="text-xs px-2 py-1 rounded text-status-danger-600 dark:text-status-danger-400 hover:text-status-danger-800 disabled:opacity-50"
              >Delete</button>
            </div>
          </div>

          <!-- Inline value update (write-only) -->
          <div v-if="editingId === e.id" class="mt-3 flex items-center gap-2">
            <input
              v-model="editValue"
              type="password"
              autocomplete="off"
              placeholder="New value (min 8 chars)"
              :class="inputCls"
            />
            <button
              type="button"
              @click="saveValue(e)"
              :disabled="busy || !editValue"
              class="text-xs px-3 py-1.5 rounded bg-action-primary-600 text-white hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >Save</button>
            <button
              type="button"
              @click="cancelEdit"
              class="text-xs px-2 py-1 border border-gray-300 dark:border-gray-600 rounded hover:bg-gray-50 dark:hover:bg-gray-700 text-gray-700 dark:text-gray-200"
            >Cancel</button>
          </div>

          <!-- Per-entry grants -->
          <div v-if="grantsOpenId === e.id" class="mt-3 pt-3 border-t border-gray-100 dark:border-gray-700 space-y-2">
            <p v-if="grantsLoading && !grants.length" class="text-xs text-gray-500 dark:text-gray-400">Loading grants…</p>
            <div v-else-if="!grants.length" class="text-xs text-gray-500 dark:text-gray-400">
              Not granted to any agent yet.
            </div>
            <div
              v-for="g in grants"
              :key="g.id"
              class="flex items-center justify-between"
            >
              <span class="text-sm text-gray-700 dark:text-gray-300">{{ g.agent_name }}</span>
              <button
                type="button"
                @click="revoke(e, g.agent_name)"
                :disabled="busy"
                class="text-xs text-status-danger-600 dark:text-status-danger-400 hover:text-status-danger-800 disabled:opacity-50"
              >Revoke</button>
            </div>
            <div class="flex items-center gap-2 pt-1">
              <input
                v-model="newGrantAgent"
                placeholder="Agent name"
                :class="inputCls"
                @keyup.enter="grant(e)"
              />
              <button
                type="button"
                @click="grant(e)"
                :disabled="busy || !newGrantAgent.trim()"
                class="text-xs px-3 py-1.5 rounded bg-action-primary-600 text-white hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >Grant</button>
            </div>
          </div>
        </div>
      </section>

      <!-- Add credential -->
      <section class="space-y-3 p-4 border border-gray-200 dark:border-gray-700 rounded-lg">
        <h4 class="text-sm font-medium text-gray-800 dark:text-gray-200">Add a credential</h4>
        <form @submit.prevent="add" class="space-y-3">
          <input v-model="form.name" placeholder="Name (e.g. openai-api-key)" required :class="inputCls" />
          <input
            v-model="form.value"
            type="password"
            autocomplete="off"
            placeholder="Value (min 8 chars, max 32 KiB) — stored encrypted, shown once"
            required
            :class="inputCls"
          />
          <input v-model="form.description" placeholder="Description (optional)" :class="inputCls" />
          <input v-model="form.kind" placeholder="Kind (default: secret)" :class="inputCls" />
          <button type="submit" :disabled="busy || !form.name || !form.value" :class="btnCls">
            {{ busy ? 'Saving…' : 'Add credential' }}
          </button>
        </form>
        <p v-if="addError" class="text-xs text-status-danger-600 dark:text-status-danger-400">{{ addError }}</p>
        <p class="text-xs text-gray-500 dark:text-gray-400">
          The value is encrypted and never shown again. Grant it to an agent below, then the agent
          fetches it by name at runtime.
        </p>
      </section>

      <!-- Key-rotation maintenance — the deliberate post-rotation entry point.
           Tucked away (collapsed) so Rewrap is not a permanent primary fixture;
           running it reports counts, and any failures raise the banner above. -->
      <details class="text-sm">
        <summary class="cursor-pointer text-gray-700 dark:text-gray-300 select-none">Key-rotation maintenance</summary>
        <div class="mt-3 space-y-2 pl-1">
          <p class="text-xs text-gray-500 dark:text-gray-400">
            After rotating <code>CREDENTIAL_ENCRYPTION_KEY</code>, re-encrypt every entry onto the
            new key. Keep the previous key as <code>CREDENTIAL_ENCRYPTION_KEY_SECONDARY</code> until
            this reports 0 failures.
          </p>
          <button
            type="button"
            @click="rewrap"
            :disabled="busy"
            class="text-xs px-3 py-1.5 rounded border border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700 text-gray-700 dark:text-gray-200 disabled:opacity-50"
          >
            {{ busy ? 'Re-encrypting…' : 'Re-encrypt vault entries' }}
          </button>
          <p v-if="rewrapResult" class="text-xs text-gray-600 dark:text-gray-400">
            {{ rewrapResult.rewrapped }} re-encrypted, {{ rewrapResult.failed }} failed.
          </p>
        </div>
      </details>

      <p
        v-if="message"
        class="text-xs"
        :class="message.type === 'error'
          ? 'text-status-danger-600 dark:text-status-danger-400'
          : 'text-status-success-600 dark:text-status-success-400'"
      >
        {{ message.text }}
      </p>
    </template>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import axios from 'axios'

const BASE = '/api/enterprise/credential-vault'

const inputCls = 'block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 text-sm'
const btnCls = 'w-full py-2 px-4 rounded-lg text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed text-sm'

const entries = ref([])
const loading = ref(false)
const busy = ref(false)
const adminRequired = ref(false)
const error = ref('')
const addError = ref('')
const message = ref(null)

const form = reactive({ name: '', value: '', description: '', kind: 'secret' })

const editingId = ref(null)
const editValue = ref('')

const grantsOpenId = ref(null)
const grants = ref([])
const grantsLoading = ref(false)
const newGrantAgent = ref('')

const rewrapResult = ref(null)
// The backend has no non-rewrap decrypt-health read, so the decrypt-failure
// state is derived from the last rewrap's own reported `failed` count.
const decryptFailure = computed(() => !!rewrapResult.value && rewrapResult.value.failed > 0)

function flash(type, text) {
  message.value = { type, text }
  if (type === 'success') setTimeout(() => { message.value = null }, 3000)
}

// Every VaultError refusal is a value-free `{code, message}` detail; the
// entitlement/admin gates use a plain-string detail. Surface the named message
// so the operator sees the actual floor/cap/name reason, not a generic failure.
function describe(e, fallback) {
  const d = e?.response?.data?.detail
  return (d && (d.message || d)) || fallback
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await axios.get(`${BASE}/entries`)
    entries.value = Array.isArray(data) ? data : []
    adminRequired.value = false
  } catch (e) {
    if (e?.response?.status === 403) {
      // Entitled tab, but not an admin (or an MCP-key principal) — named state.
      adminRequired.value = true
    } else {
      error.value = describe(e, 'Failed to load vault credentials')
    }
  } finally {
    loading.value = false
  }
}

async function add() {
  busy.value = true
  addError.value = ''
  try {
    await axios.post(`${BASE}/entries`, {
      name: form.name.trim(),
      value: form.value,
      description: form.description.trim() || null,
      kind: (form.kind || 'secret').trim(),
    })
    Object.assign(form, { name: '', value: '', description: '', kind: 'secret' })
    flash('success', 'Credential added')
    await load()
  } catch (e) {
    addError.value = describe(e, 'Failed to add credential')
  } finally {
    busy.value = false
  }
}

function toggleEdit(e) {
  editingId.value = editingId.value === e.id ? null : e.id
  editValue.value = ''
}

function cancelEdit() {
  editingId.value = null
  editValue.value = ''
}

async function saveValue(e) {
  busy.value = true
  try {
    await axios.put(`${BASE}/entries/${e.id}`, { value: editValue.value })
    cancelEdit()
    flash('success', `Value updated for "${e.name}"`)
    await load()
  } catch (err) {
    flash('error', describe(err, 'Failed to update value'))
  } finally {
    busy.value = false
  }
}

async function remove(e) {
  const extra = e.grant_count > 0 ? ` This will also revoke ${e.grant_count} grant(s).` : ''
  if (!confirm(`Delete credential "${e.name}"?${extra}`)) return
  busy.value = true
  try {
    await axios.delete(`${BASE}/entries/${e.id}`)
    if (grantsOpenId.value === e.id) grantsOpenId.value = null
    if (editingId.value === e.id) cancelEdit()
    flash('success', `Deleted "${e.name}"`)
    await load()
  } catch (err) {
    flash('error', describe(err, 'Failed to delete credential'))
  } finally {
    busy.value = false
  }
}

async function toggleGrants(e) {
  if (grantsOpenId.value === e.id) {
    grantsOpenId.value = null
    return
  }
  grantsOpenId.value = e.id
  newGrantAgent.value = ''
  await loadGrants(e)
}

async function loadGrants(e) {
  grantsLoading.value = true
  grants.value = []
  try {
    const { data } = await axios.get(`${BASE}/entries/${e.id}/grants`)
    grants.value = Array.isArray(data) ? data : []
  } catch (err) {
    flash('error', describe(err, 'Failed to load grants'))
  } finally {
    grantsLoading.value = false
  }
}

async function grant(e) {
  const agent = newGrantAgent.value.trim()
  if (!agent) return
  // Escalated-consent confirm — a grant lets the agent read the plaintext VALUE.
  if (!confirm(
    `Grant "${agent}" access to "${e.name}"?\n\n` +
    `Agent "${agent}" will be able to read the VALUE of "${e.name}" at any time while the grant is active.`
  )) return
  busy.value = true
  try {
    await axios.post(`${BASE}/entries/${e.id}/grants`, { agent_name: agent })
    newGrantAgent.value = ''
    flash('success', `Granted "${e.name}" to ${agent}`)
    await Promise.all([loadGrants(e), load()])
  } catch (err) {
    flash('error', describe(err, 'Failed to grant'))
  } finally {
    busy.value = false
  }
}

async function revoke(e, agentName) {
  if (!confirm(`Revoke "${agentName}"'s access to "${e.name}"?`)) return
  busy.value = true
  try {
    await axios.delete(`${BASE}/entries/${e.id}/grants/${encodeURIComponent(agentName)}`)
    flash('success', `Revoked ${agentName}`)
    await Promise.all([loadGrants(e), load()])
  } catch (err) {
    flash('error', describe(err, 'Failed to revoke'))
  } finally {
    busy.value = false
  }
}

async function rewrap() {
  busy.value = true
  try {
    const { data } = await axios.post(`${BASE}/rewrap`, {})
    rewrapResult.value = {
      rewrapped: Number(data?.rewrapped ?? 0),
      failed: Number(data?.failed ?? 0),
    }
    if (rewrapResult.value.failed > 0) {
      flash('error', `${rewrapResult.value.failed} entr${rewrapResult.value.failed === 1 ? 'y' : 'ies'} could not be decrypted`)
    } else {
      flash('success', `Re-encrypted ${rewrapResult.value.rewrapped} entr${rewrapResult.value.rewrapped === 1 ? 'y' : 'ies'}`)
    }
  } catch (err) {
    flash('error', describe(err, 'Rewrap failed'))
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>
