<!--
  The instance autonomy level (trinity-enterprise#641, canon tandem-06 §2.3).

  The CEILING on everything a companion may do unprompted, fleet-wide. Below it
  each (seat, ask class) earns its own state from the decision record — which is
  why this panel sets one thing and explains what it does NOT do: raising the
  level never promotes anything, it only stops capping what a seat already
  earned; lowering it takes nothing away, because the earned state is stored and
  the level is ANDed at read time.

  Shown in every edition, like the session policy beside it: the level is in
  force whether or not anyone has ever set it, and an operator who cannot see
  the ceiling cannot reason about what their fleet is doing on its own.
-->
<template>
  <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
    <div class="px-6 py-5">
      <div class="flex items-center justify-between gap-3">
        <div>
          <h3 class="text-lg font-medium text-gray-900 dark:text-gray-100">Autonomy level</h3>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
            How much a companion may do without being asked. This is the ceiling for
            the whole instance — each seat still earns its own classes underneath it.
          </p>
        </div>
        <BaseBadge v-if="dial" :variant="dial.allows_unprompted ? 'success' : 'neutral'" dot
                   data-testid="autonomy-dial-current">{{ dial.level }}</BaseBadge>
      </div>

      <!-- #1927: gated on "no data yet" (`view.state`), never on the in-flight
           flag — a retry with the dial already on screen must not replace it
           with a spinner. -->
      <p v-if="view.state === 'loading'" class="mt-4 text-sm text-gray-500 dark:text-gray-400">Loading…</p>

      <!-- A failure to LOAD is not a failure to SAVE and not "you may not set
           this". Collapsing them tells an admin the wrong thing to do next. -->
      <div v-else-if="view.state === 'failed'"
           class="mt-4 rounded-md border border-status-error-200 dark:border-status-error-500/30 bg-status-error-50 dark:bg-status-error-500/10 p-4">
        <p class="text-sm text-status-error-700 dark:text-status-error-300">{{ loadError }}</p>
        <button type="button" class="mt-2 text-sm font-medium underline text-status-error-700 dark:text-status-error-300"
                @click="load">Retry</button>
      </div>

      <div v-else-if="dial" class="mt-5 space-y-3">
        <!-- A refresh that failed with the dial on screen keeps the dial and
             says so, rather than throwing the answer away. -->
        <p v-if="view.stale" class="text-xs text-status-warning-700 dark:text-status-warning-300"
           data-testid="autonomy-dial-stale">{{ staleBannerMessage('the autonomy level', loadedAt) }}</p>
        <label v-for="lv in dial.levels" :key="lv.level"
               class="flex items-start gap-3 rounded-lg border px-4 py-3 cursor-pointer"
               :class="choice === lv.level
                 ? 'border-action-primary-500 bg-action-primary-50 dark:bg-action-primary-500/10'
                 : 'border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600'"
               :data-testid="'autonomy-dial-option-' + lv.level">
          <input v-model="choice" type="radio" :value="lv.level" :disabled="saving"
                 class="mt-1 text-action-primary-600 focus:ring-action-primary-500" />
          <span class="min-w-0">
            <span class="flex items-center gap-2">
              <span class="font-mono text-[12px] text-gray-900 dark:text-gray-100">{{ lv.level }}</span>
              <!-- The one thing that actually changes behaviour, said on the
                   row rather than inferred from the ordering. -->
              <BaseBadge v-if="lv.allows_unprompted" variant="success" dot>unprompted possible</BaseBadge>
              <BaseBadge v-else variant="neutral" dot>always asks first</BaseBadge>
            </span>
            <span class="mt-0.5 block text-sm text-gray-600 dark:text-gray-300">{{ lv.label }}</span>
          </span>
        </label>

        <div v-if="saveError"
             class="rounded-md border border-status-error-200 dark:border-status-error-500/30 bg-status-error-50 dark:bg-status-error-500/10 p-3">
          <p class="text-sm text-status-error-700 dark:text-status-error-300">{{ saveError }}</p>
        </div>

        <div class="flex items-center gap-3 pt-1">
          <button type="button" :disabled="saving || !dirty" :class="SETTINGS_PRIMARY_BUTTON_CLASS"
                  data-testid="autonomy-dial-save" @click="save">
            {{ saving ? 'Saving…' : 'Set level' }}
          </button>
          <span v-if="saved" class="text-sm text-status-success-600 dark:text-status-success-400">Saved — applied live.</span>
          <button v-if="dirty" type="button" class="text-sm text-gray-600 dark:text-gray-400 underline"
                  @click="choice = dial.level">Discard</button>
        </div>

        <!-- Why this control is safe to move in either direction. Without it an
             admin reasonably fears that lowering the level throws away what
             every seat earned, and that raising it hands the fleet autonomy it
             did not earn. Neither is true, and the panel should say so. -->
        <p class="text-xs text-gray-500 dark:text-gray-400 pt-1">
          Raising the level never promotes anything — a seat's classes still have to be
          earned from its decision record. Lowering it takes nothing away: what each seat
          earned is kept, and comes back exactly as it was if you raise the level again.
        </p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import api from '../../api'
import BaseBadge from '../base/BaseBadge.vue'
import { SETTINGS_PRIMARY_BUTTON_CLASS } from './fieldStyles'
import { staleBannerMessage, viewState } from '../../utils/loadingState'

const dial = ref(null)
const choice = ref(null)
const hasLoaded = ref(false)
const loadedAt = ref(null)
const loadError = ref(null)
const saving = ref(false)
const saveError = ref(null)
const saved = ref(false)

const view = computed(() => viewState({ hasLoaded: hasLoaded.value, error: loadError.value }))
const dirty = computed(() => !!dial.value && choice.value !== dial.value.level)

function adopt(data) {
  // MERGE: the GET carries `levels` and the PUT does not, so replacing would
  // empty the option list on the first successful save (the ent#375 lesson,
  // one panel over).
  dial.value = { ...(dial.value || {}), ...data }
  choice.value = dial.value.level
}

async function load() {
  loadError.value = null
  try {
    const { data } = await api.get('/api/settings/autonomy-dial')
    adopt(data)
    hasLoaded.value = true
    loadedAt.value = new Date()
  } catch (e) {
    loadError.value = e.response?.data?.detail || 'Could not load the autonomy level.'
  }
}

async function save() {
  saving.value = true
  saveError.value = null
  saved.value = false
  try {
    const { data } = await api.put('/api/settings/autonomy-dial', { level: choice.value })
    adopt(data)
    saved.value = true
    setTimeout(() => { saved.value = false }, 4000)
  } catch (e) {
    const d = e.response?.data?.detail
    saveError.value = (typeof d === 'string' ? d : d?.message) || 'Could not set the autonomy level.'
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>
