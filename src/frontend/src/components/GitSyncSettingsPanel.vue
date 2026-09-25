<template>
  <!--
    Git sync settings (#3010) — the two per-agent flags the #1108 growth path
    promised: the 15-min auto-sync heartbeat, and pausing schedules while sync
    keeps failing. Both apply live: the agent's loop reads the auto-sync flag
    every cycle, and the scheduler reads the freeze flag at every fire.
  -->
  <section class="rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden p-6">
    <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Git sync</h3>
    <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
      Keep this agent's work in its GitHub repository automatically.
    </p>

    <div
      v-if="view.state === 'loading'"
      aria-busy="true"
      data-testid="git-sync-loading"
    >
      <span class="sr-only">Loading git sync settings…</span>
      <SkeletonLoader :count="2" height="2.75rem" gap="1rem" />
    </div>

    <LoadFailed
      v-else-if="view.state === 'failed'"
      dense
      title="Couldn't load git sync settings"
      message="The settings are saved on the platform; loading them failed. Try again."
      :detail="loadError"
      :retrying="retrying"
      @retry="retry"
    />

    <p
      v-else-if="view.state === 'empty'"
      class="text-sm text-gray-600 dark:text-gray-300"
      data-testid="git-sync-unbound"
    >
      This agent isn't connected to a GitHub repository, so there is nothing to
      sync. Connect one from the <span class="font-medium">Git</span> tab, then
      come back to turn auto-sync on.
    </p>

    <div v-else class="space-y-4" data-testid="git-sync-ready">
      <div>
        <BaseToggle
          :model-value="autoSync"
          :disabled="savingAutoSync"
          label="Auto-sync to GitHub every 15 minutes"
          data-testid="auto-sync-toggle"
          @update:model-value="setAutoSync"
        />
        <p class="mt-1 pl-[46px] text-xs text-gray-500 dark:text-gray-400">
          Commits and pushes the agent's changes on its own. Takes effect on the
          next cycle — no restart needed.
        </p>
        <InlineError
          class="mt-2"
          :message="autoSyncError"
          retryable
          @retry="setAutoSync(!autoSync)"
          @dismiss="autoSyncError = ''"
        />
      </div>

      <div>
        <BaseToggle
          :model-value="pull"
          :disabled="savingPull"
          label="Pull changes from GitHub every 15 minutes"
          data-testid="pull-sync-toggle"
          @update:model-value="setPull"
        />
        <p class="mt-1 pl-[46px] text-xs text-gray-500 dark:text-gray-400">
          Brings in edits people and other agents push to the repository. Never
          runs while the agent is working, and never discards its own changes.
        </p>
        <InlineError
          class="mt-2"
          :message="pullError"
          retryable
          @retry="setPull(!pull)"
          @dismiss="pullError = ''"
        />
      </div>

      <div>
        <BaseToggle
          :model-value="freeze"
          :disabled="savingFreeze"
          label="Pause schedules while sync is failing"
          data-testid="freeze-toggle"
          @update:model-value="setFreeze"
        />
        <p class="mt-1 pl-[46px] text-xs text-gray-500 dark:text-gray-400">
          After three failed syncs in a row, scheduled runs wait until sync
          recovers, so no work piles up where it can't be saved.
        </p>
        <InlineError
          class="mt-2"
          :message="freezeError"
          retryable
          @retry="setFreeze(!freeze)"
          @dismiss="freezeError = ''"
        />
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { viewState } from '../utils/loadingState'
import BaseToggle from './base/BaseToggle.vue'
import InlineError from './InlineError.vue'
import LoadFailed from './LoadFailed.vue'
import SkeletonLoader from './SkeletonLoader.vue'

const props = defineProps({
  agentName: { type: String, required: true },
  notify: { type: Function, default: null },
})

const agents = useAgentsStore()

const hasLoaded = ref(false)
const bound = ref(false)
const loadError = ref('')
const retrying = ref(false)
const autoSync = ref(false)
const freeze = ref(false)
const pull = ref(false)
const savingPull = ref(false)
const pullError = ref('')
const savingAutoSync = ref(false)
const savingFreeze = ref(false)
const autoSyncError = ref('')
const freezeError = ref('')

// Not connected to GitHub is the successful-empty case: both endpoints answer
// 404 "Git not configured" for an agent with no git binding.
const view = computed(() =>
  viewState({ hasLoaded: hasLoaded.value, error: loadError.value, count: bound.value ? 1 : 0 })
)

function errorDetail(e) {
  return e?.response?.data?.detail || e?.message || 'Request failed'
}

function isUnbound(e) {
  return e?.response?.status === 404 && e?.response?.data?.detail === 'Git not configured'
}

async function load() {
  try {
    const [a, f, p] = await Promise.all([
      agents.getGitAutoSync(props.agentName),
      agents.getGitFreezeSchedules(props.agentName),
      agents.getGitPullSync(props.agentName),
    ])
    autoSync.value = !!a?.auto_sync_enabled
    freeze.value = !!f?.freeze_schedules_if_sync_failing
    pull.value = !!p?.pull_sync_enabled
    bound.value = true
    loadError.value = ''
    hasLoaded.value = true
  } catch (e) {
    if (isUnbound(e)) {
      bound.value = false
      loadError.value = ''
      hasLoaded.value = true
    } else {
      loadError.value = String(errorDetail(e))
    }
  }
}

async function retry() {
  retrying.value = true
  try {
    await load()
  } finally {
    retrying.value = false
  }
}

async function setAutoSync(next) {
  savingAutoSync.value = true
  autoSyncError.value = ''
  try {
    const data = await agents.setGitAutoSync(props.agentName, next)
    autoSync.value = !!data?.auto_sync_enabled
    if (props.notify) {
      props.notify(next ? 'Auto-sync on — the next cycle will push.' : 'Auto-sync off — the next cycle is skipped.', 'success')
    }
  } catch (e) {
    autoSyncError.value = `Couldn't turn auto-sync ${next ? 'on' : 'off'}: ${errorDetail(e)}`
  } finally {
    savingAutoSync.value = false
  }
}

async function setPull(next) {
  savingPull.value = true
  pullError.value = ''
  try {
    const data = await agents.setGitPullSync(props.agentName, next)
    pull.value = !!data?.pull_sync_enabled
    if (props.notify) {
      props.notify(next ? 'Pulling on — the next cycle brings in new commits.' : 'Pulling off — the next cycle is skipped.', 'success')
    }
  } catch (e) {
    pullError.value = `Couldn't turn pulling ${next ? 'on' : 'off'}: ${errorDetail(e)}`
  } finally {
    savingPull.value = false
  }
}

async function setFreeze(next) {
  savingFreeze.value = true
  freezeError.value = ''
  try {
    const data = await agents.setGitFreezeSchedules(props.agentName, next)
    freeze.value = !!data?.freeze_schedules_if_sync_failing
    if (props.notify) {
      props.notify(next ? 'Schedules will pause while sync is failing.' : 'Schedules keep running when sync fails.', 'success')
    }
  } catch (e) {
    freezeError.value = `Couldn't change the schedule pause: ${errorDetail(e)}`
  } finally {
    savingFreeze.value = false
  }
}

onMounted(load)
</script>
