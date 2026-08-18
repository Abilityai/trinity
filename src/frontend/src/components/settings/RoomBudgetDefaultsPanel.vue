<!--
  Room budget defaults (ent#387).

  ent#381 retired the Sessions page, and `NewRoomDialog` — the only surface that
  ever SET a room budget — went with it. This is where budgets live now.

  The panel is deliberately an OPERATOR surface and not a field in the Workspace:
  a budget bounds what a client conversation may spend, so putting the dial in the
  client's "start a chat" dialog hands it to the person being bounded. The backend
  enforces the same line — a workspace client's supplied budget is ignored, and
  these defaults apply instead.

  Enterprise-gated as a whole (rooms are an enterprise module), unlike the ent#375
  session panel whose mechanism is OSS: there is nothing here for a community
  install to look at.
-->
<template>
  <div v-if="entitled" class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
    <div class="px-6 py-5">
      <div>
        <h3 class="text-lg font-medium text-gray-900 dark:text-gray-100">Room budgets</h3>
        <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
          The limits applied to a multi-agent room started from the Workspace. A room
          closes with a visible reason when it reaches one. Clients cannot change these.
        </p>
      </div>

      <div v-if="loading" class="mt-4 text-sm text-gray-500 dark:text-gray-400">Loading…</div>

      <!-- A failure to LOAD is a different fact from a failure to SAVE. -->
      <div
        v-else-if="loadError"
        class="mt-4 rounded-md bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 p-4"
      >
        <p class="text-sm text-red-700 dark:text-red-300">{{ loadError }}</p>
        <button type="button" class="mt-2 text-sm font-medium text-red-700 dark:text-red-300 underline" @click="load">Retry</button>
      </div>

      <div v-else-if="state" class="mt-5 space-y-4">
        <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div>
            <label for="rbd-messages" class="block text-sm font-medium text-gray-700 dark:text-gray-300">Messages</label>
            <div class="mt-1 flex items-center gap-2">
              <input
                id="rbd-messages"
                v-model.number="form.max_messages"
                type="number" min="1" :max="state.max_messages_ceiling" step="1"
                :disabled="saving"
                data-testid="rbd-max-messages"
                :class="SETTINGS_NUMBER_INPUT_CLASS"
              />
              <span class="text-xs text-gray-400 dark:text-gray-500">{{ sourceLabel('room_default_max_messages') }}</span>
            </div>
          </div>

          <div>
            <label for="rbd-cost" class="block text-sm font-medium text-gray-700 dark:text-gray-300">Cost cap (USD)</label>
            <div class="mt-1 flex items-center gap-2">
              <input
                id="rbd-cost"
                v-model="form.max_cost_usd"
                type="number" min="0" step="0.5"
                placeholder="none"
                :disabled="saving"
                data-testid="rbd-max-cost"
                :class="SETTINGS_NUMBER_INPUT_CLASS"
              />
              <span class="text-xs text-gray-400 dark:text-gray-500">{{ sourceLabel('room_default_max_cost_usd') }}</span>
            </div>
            <p class="mt-1 text-xs text-gray-400 dark:text-gray-500">Empty means no cap.</p>
          </div>

          <div>
            <label for="rbd-ttl" class="block text-sm font-medium text-gray-700 dark:text-gray-300">Expires after</label>
            <div class="mt-1 flex items-center gap-2">
              <input
                id="rbd-ttl"
                v-model.number="form.ttl_hours"
                type="number" min="0" :max="state.max_ttl_hours" step="1"
                :disabled="saving"
                data-testid="rbd-ttl"
                :class="SETTINGS_NUMBER_INPUT_CLASS"
              />
              <span class="text-sm text-gray-500 dark:text-gray-400">hours</span>
              <span class="text-xs text-gray-400 dark:text-gray-500">{{ sourceLabel('room_default_ttl_hours') }}</span>
            </div>
            <p class="mt-1 text-xs text-gray-400 dark:text-gray-500">0 means no expiry.</p>
          </div>
        </div>

        <div class="flex items-center gap-3">
          <button
            type="button"
            :disabled="saving || !dirty"
            data-testid="rbd-save"
            class="inline-flex items-center px-3 py-1.5 text-sm font-medium rounded-md text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50"
            @click="save"
          >{{ saving ? 'Saving…' : 'Save' }}</button>
          <button
            v-if="dirty"
            type="button"
            class="text-sm text-gray-500 dark:text-gray-400 underline"
            @click="reset"
          >Reset</button>
          <span v-if="saved" class="text-sm text-status-success-600 dark:text-status-success-400">Saved</span>
        </div>

        <p v-if="saveError" class="text-sm text-red-600 dark:text-red-400">{{ saveError }}</p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import api from '../../api'
import { useEnterpriseStore } from '../../stores/enterprise'
import { SETTINGS_NUMBER_INPUT_CLASS } from './fieldStyles'
import { buildBudgetUpdate, isBudgetDirty } from '../../utils/roomBudgets'

const enterpriseStore = useEnterpriseStore()
const entitled = computed(() => enterpriseStore.isEntitled('shared_sessions'))

const state = ref(null)
const loading = ref(false)
const loadError = ref(null)
const saving = ref(false)
const saveError = ref(null)
const saved = ref(false)
const form = reactive({ max_messages: null, max_cost_usd: '', ttl_hours: null })

// "Configured to 60" and "defaulted to 60" are different facts; without this an
// operator cannot tell a deliberate setting from a shipped value (#1638).
function sourceLabel(key) {
  return state.value?.sources?.[key] === 'db-row' ? 'configured' : 'default'
}

function adopt(data) {
  state.value = data
  form.max_messages = data.max_messages
  form.max_cost_usd = data.max_cost_usd ?? ''
  form.ttl_hours = data.ttl_hours
}

const dirty = computed(() => isBudgetDirty(form, state.value))

async function load() {
  if (!entitled.value) return
  loading.value = true
  loadError.value = null
  try {
    const { data } = await api.get('/api/enterprise/room-budget-defaults')
    adopt(data)
  } catch (e) {
    loadError.value = e.response?.data?.detail || 'Could not load the room budget defaults.'
  } finally {
    loading.value = false
  }
}

function reset() {
  if (state.value) adopt(state.value)
  saveError.value = null
  saved.value = false
}

async function save() {
  saving.value = true
  saveError.value = null
  saved.value = false
  try {
    // Send only what changed; "no cost cap" is an explicit clear, since an
    // omitted field means "leave it alone" (see utils/roomBudgets.js).
    const body = buildBudgetUpdate(form, state.value)
    const { data } = await api.put('/api/enterprise/room-budget-defaults', body)
    adopt(data)
    saved.value = true
  } catch (e) {
    saveError.value = e.response?.data?.detail || 'Could not save the room budget defaults.'
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>
