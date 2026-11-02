<template>
  <!--
    trinity-enterprise#1068 — what a system apply does when it cannot write the
    approval gate a manifest declares on a skill the agent already holds. On
    (the default): the skill is unassigned, no gate, no skill. Off: the apply
    reports the skill as running ungated and leaves it assigned. A skill held
    through a skill set stays report-only either way. Read and written through
    the admin-gated ops-config endpoint, beside the SSH toggle. Its own file, so
    `Settings.vue`'s raw-colour count cannot move.
  -->
  <div class="flex items-start justify-between gap-4 pt-4 mt-4 border-t border-gray-200 dark:border-gray-700" data-testid="apply-unassign-setting">
    <div class="flex-1 min-w-0">
      <p id="apply-unassign-label" class="text-sm font-medium text-gray-700 dark:text-gray-300">
        Unassign a skill whose gate cannot be written
      </p>
      <p class="text-sm text-gray-500 dark:text-gray-400">
        When a system apply cannot write the approval gate a manifest declares on a skill the agent already holds,
        unassign the skill instead of leaving it running with no gate. Off: the apply only reports it.
        A skill held through a skill set is always report-only.
      </p>
      <p v-if="error" class="mt-1 text-sm text-status-danger-700 dark:text-status-danger-300" data-testid="apply-unassign-error">{{ error }}</p>
    </div>
    <BaseToggle
      :model-value="enabled"
      aria-label="Unassign a skill whose gate cannot be written"
      :disabled="saving || !loaded"
      data-testid="apply-unassign-toggle"
      @update:model-value="save"
    />
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import api from '../../api'
import BaseToggle from '../base/BaseToggle.vue'
import { readOpsBool, opsBoolValue } from '../../utils/opsSettings'

const OPS_KEY = 'apply_unassign_on_failed_gate'
// The server default; shown only when the payload carries no value for the key.
const DEFAULT_ON = true

const enabled = ref(DEFAULT_ON)
const loaded = ref(false)
const saving = ref(false)
const error = ref('')

async function load() {
  try {
    const { data } = await api.get('/api/settings/ops/config')
    enabled.value = readOpsBool(data, OPS_KEY, DEFAULT_ON)
    loaded.value = true
  } catch (e) {
    // A switch that cannot read its state must not claim one: it stays
    // disabled and says why, rather than inviting a click on a guess.
    error.value = 'Could not load the current value; reload to change it.'
  }
}

async function save(next) {
  saving.value = true
  error.value = ''
  try {
    await api.put('/api/settings/ops/config', { settings: { [OPS_KEY]: opsBoolValue(next) } })
    enabled.value = next
  } catch (e) {
    const detail = e?.response?.data?.detail
    error.value = (detail && typeof detail === 'object' ? detail.message : detail)
      || 'Could not save the setting.'
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>
