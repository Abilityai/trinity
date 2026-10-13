<template>
  <!--
    #2915 — the operator-queue aging bound (OPS-001-HONEST). Hours a pending
    item may wait before its card is marked as waiting and the filing agent
    receives a receipt; 0 disables. Read and written through the admin-gated
    ops-config endpoint, the same one the SSH toggle uses. Its own file, so
    `Settings.vue`'s raw-colour count cannot move; the field is the BaseInput
    primitive, never a hand-rolled input.
  -->
  <div class="flex items-start justify-between gap-4 pt-4 mt-4 border-t border-gray-200 dark:border-gray-700" data-testid="opq-aging-setting">
    <div class="flex-1 min-w-0">
      <BaseInput
        id="opq-aging-hours"
        v-model="draft"
        type="number"
        label="Operator queue aging bound (hours)"
        help="Hours a pending item may wait before its card is marked as waiting and the filing agent receives a receipt. 0 disables."
        :error="error"
        :disabled="saving || !loaded"
        min="0"
        max="8760"
        step="1"
        inputmode="numeric"
        data-testid="opq-aging-hours"
        @change="save"
      />
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import api from '../../api'
import BaseInput from '../base/BaseInput.vue'
import { readOpsInt, opsIntValue } from '../../utils/opsSettings'

const OPS_KEY = 'operator_queue_aging_hours'
const DEFAULT_HOURS = 24

const draft = ref(String(DEFAULT_HOURS))
const loaded = ref(false)
const saving = ref(false)
const error = ref('')

async function load() {
  try {
    const { data } = await api.get('/api/settings/ops/config')
    draft.value = String(readOpsInt(data, OPS_KEY, DEFAULT_HOURS))
  } catch (e) {
    // The field keeps its default and stays editable: a failed read must not
    // hide the control, only say so.
    error.value = 'Could not load the current value; showing the default.'
  } finally {
    loaded.value = true
  }
}

async function save() {
  saving.value = true
  error.value = ''
  try {
    await api.put('/api/settings/ops/config', { settings: { [OPS_KEY]: opsIntValue(draft.value) } })
    draft.value = opsIntValue(draft.value)
  } catch (e) {
    const detail = e?.response?.data?.detail
    error.value = (detail && typeof detail === 'object' ? detail.message : detail)
      || 'Could not save. Enter a whole number of hours between 0 and 8760 (0 disables).'
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>
