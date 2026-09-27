<template>
  <div class="rounded-lg bg-white p-6 shadow dark:bg-gray-800 dark:shadow-gray-900">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="text-lg font-medium text-gray-900 dark:text-white">Skill Runner</h2>
        <p class="mt-1 max-w-2xl text-sm text-gray-500 dark:text-gray-400">
          One agent runs library skills on behalf of others. Grant an agent access to a
          skill and it can call <code class="rounded bg-gray-100 px-1 dark:bg-gray-700">run_skill</code>
          for that skill only.
        </p>
      </div>
      <BaseToggle
        v-model="enabled"
        label="Enabled"
        :disabled="actions.toggle.disabled"
        @update:modelValue="onToggle"
      />
    </div>

    <InlineError v-if="view.stale" :message="staleMessage" class="mt-4" @retry="load" />

    <!-- #2540: the scanline beam is CHART loading only. A panel loads with a
         skeleton keyed on "no data yet" (`view.state`), never a bare
         `loading` flag (#1927 ratchet). -->
    <SkeletonLoader v-if="view.state === 'loading'" class="mt-4" :count="4" />
    <template v-else>
      <div v-if="view.state === 'failed'" class="mt-4 text-sm text-status-danger-600 dark:text-status-danger-400">
        Skill Runner status could not be loaded.
        <button type="button" class="underline" @click="load">Retry</button>
      </div>

      <div v-else-if="status" class="mt-4 space-y-6">
        <!-- Runner state + counts. Counts show even when disabled: dormant is
             not empty, and they answer "what happens if I turn this on". -->
        <div class="flex flex-wrap items-center gap-x-6 gap-y-2">
          <BaseBadge :variant="summary.variant" dot>{{ summary.text }}</BaseBadge>
          <dl class="flex flex-wrap gap-x-6 gap-y-1">
            <div v-for="c in counts" :key="c.label" class="flex items-baseline gap-1.5">
              <dt class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ c.value }}</dt>
              <dd class="text-xs text-gray-500 dark:text-gray-400">{{ c.label }}</dd>
            </div>
          </dl>
        </div>

        <!-- Blocked? Say why, and where to go. Never a dead end. -->
        <p v-if="actions.provision.reason" class="text-sm text-gray-600 dark:text-gray-300">
          {{ actions.provision.reason }}
          <button
            v-if="blockAction"
            type="button"
            class="underline"
            @click="$emit('navigate-tab', blockAction.tab)"
          >{{ blockAction.label }}</button>
        </p>

        <div class="flex flex-wrap gap-2">
          <BaseButton
            variant="primary"
            :disabled="actions.provision.disabled"
            :loading="actions.provision.busy"
            loading-label="Provisioning…"
            @click="run('provision')"
          >{{ actions.provision.label }}</BaseButton>
          <BaseButton
            variant="secondary"
            :disabled="actions.sync.disabled"
            :loading="actions.sync.busy"
            loading-label="Syncing…"
            @click="run('sync')"
          >Sync library onto runner</BaseButton>
        </div>

        <p v-if="actionError" class="text-sm text-status-danger-600 dark:text-status-danger-400">
          {{ actionError }}
        </p>
        <p v-else-if="actionNote" class="text-sm text-status-success-600 dark:text-status-success-400">
          {{ actionNote }}
        </p>

        <!-- Access grants -->
        <section>
          <div class="flex flex-wrap items-center justify-between gap-2">
            <h3 class="text-sm font-medium text-gray-900 dark:text-white">Access grants</h3>
            <BaseInput
              v-if="grants.length > FILTER_THRESHOLD"
              v-model="query"
              placeholder="Filter by agent or skill"
              class="w-56"
            />
          </div>

          <p v-if="!grants.length" class="mt-2 text-sm text-gray-500 dark:text-gray-400">
            No agent can run any skill yet. Grant access below to let one start.
          </p>
          <p v-else-if="!visibleGrants.length" class="mt-2 text-sm text-gray-500 dark:text-gray-400">
            No grant matches “{{ query }}”.
          </p>

          <ul v-if="visibleGrants.length" class="mt-2 divide-y divide-gray-200 dark:divide-gray-700">
            <li
              v-for="g in visibleGrants"
              :key="`${g.caller_agent}:${g.skill_name}`"
              class="flex items-center justify-between gap-3 py-2"
            >
              <span class="min-w-0 text-sm text-gray-700 dark:text-gray-200">
                <span class="font-medium">{{ g.caller_agent }}</span>
                <span class="text-gray-500 dark:text-gray-400"> may run </span>
                <span class="font-medium">{{ g.skill_name }}</span>
              </span>
              <BaseButton
                variant="ghost"
                size="sm"
                :disabled="busy !== null"
                @click="revoke(g)"
              >Revoke</BaseButton>
            </li>
          </ul>

          <div class="mt-4 flex flex-wrap items-end gap-2">
            <BaseSelect v-model="newCaller" label="Agent" class="w-52">
              <option value="">Select an agent…</option>
              <option v-for="a in agentNames" :key="a" :value="a">{{ a }}</option>
            </BaseSelect>
            <BaseSelect v-model="newSkill" label="Skill" class="w-52">
              <option value="">Select a skill…</option>
              <option v-for="s in skillNames" :key="s" :value="s">{{ s }}</option>
            </BaseSelect>
            <BaseButton
              variant="secondary"
              :disabled="!grantForm.canSubmit"
              :loading="busy === 'grant'"
              loading-label="Granting…"
              @click="grant"
            >Grant access</BaseButton>
          </div>
          <p v-if="grantForm.reason" class="mt-1 text-sm text-gray-500 dark:text-gray-400">
            {{ grantForm.reason }}
          </p>
        </section>
      </div>
    </template>
  </div>
</template>

<script setup>
/**
 * Skill-runner admin surface (ent#242).
 *
 * The six admin endpoints shipped with no writer outside curl, so the ACL table
 * could not be populated by a human at all. This is that writer.
 *
 * It is a DISPATCHER: every decidable rule lives in `skillRunnerPanel.js`,
 * because vitest runs `environment: 'node'` with no mount harness and a rule
 * inside an SFC is one no test can reach (the ent#392 precedent).
 *
 * The panel is a client of routes the server already gates
 * (`requires_entitlement("skill_runner")` + `require_human_admin`). The tab's
 * `requires:` gate is therefore UX, not containment — a stale entitlement list
 * cannot become an escalation, because the server refuses regardless.
 */
import { ref, computed, onMounted } from 'vue'
import axios from 'axios'
import { useAuthStore } from '../../stores/auth'
import { viewState, staleBannerMessage } from '../../utils/loadingState'
import SkeletonLoader from '../SkeletonLoader.vue'
import InlineError from '../InlineError.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseToggle from '../base/BaseToggle.vue'
import BaseSelect from '../base/BaseSelect.vue'
import BaseInput from '../base/BaseInput.vue'
import BaseBadge from '../base/BaseBadge.vue'
import {
  blockingReason, blockingAction, runnerSummary, actionState,
  headerCounts, grantFormState, revokePrompt, filterGrants, grantsFrom,
} from './skillRunnerPanel.js'

defineEmits(['navigate-tab'])

const BASE = '/api/enterprise/skill-runner'
// Below this many grants a filter box is noise, not help.
const FILTER_THRESHOLD = 8

const auth = useAuthStore()
const hdr = () => ({ headers: auth.authHeader })

const status = ref(null)
const grants = ref([])
const agentNames = ref([])
const skillNames = ref([])
const hasLoaded = ref(false)
const loadError = ref(null)
const lastLoadedAt = ref(null)

const busy = ref(null)
const actionError = ref('')
const actionNote = ref('')
const query = ref('')
const newCaller = ref('')
const newSkill = ref('')
const enabled = ref(false)

const view = computed(() => viewState({
  hasLoaded: hasLoaded.value,
  error: loadError.value,
  count: status.value ? 1 : 0,
}))
const staleMessage = computed(() => staleBannerMessage('Skill Runner', lastLoadedAt.value))
const summary = computed(() => runnerSummary(status.value))
const counts = computed(() => headerCounts(status.value))
const actions = computed(() => actionState(status.value, { busy: busy.value }))
const blockAction = computed(() => blockingAction(status.value))
const visibleGrants = computed(() => filterGrants(grants.value, query.value))
const grantForm = computed(() =>
  grantFormState(grants.value, newCaller.value, newSkill.value, { busy: busy.value === 'grant' })
)

async function load() {
  loadError.value = null
  try {
    const [s, a] = await Promise.all([
      axios.get(`${BASE}/status`, hdr()),
      axios.get(`${BASE}/access`, hdr()),
    ])
    status.value = s.data
    enabled.value = Boolean(s.data?.enabled)
    grants.value = grantsFrom(a.data)
    lastLoadedAt.value = new Date()
  } catch (e) {
    loadError.value = e
  } finally {
    hasLoaded.value = true
  }
  // Picker sources are OSS endpoints and are best-effort: an empty picker is a
  // smaller failure than a panel that will not render, so they never set
  // `loadError`. There is no endpoint listing the library for a picker on the
  // skill-runner side — `/available` is agent-facing (ACL ∩ library for ONE
  // caller) — so the library listing is the OSS one.
  try {
    const r = await axios.get('/api/agents', hdr())
    const rows = Array.isArray(r.data) ? r.data : r.data?.agents || []
    agentNames.value = rows.filter((x) => !x.is_system).map((x) => x.name).sort()
  } catch { /* picker stays empty */ }
  try {
    const r = await axios.get('/api/skills/library', hdr())
    skillNames.value = (Array.isArray(r.data) ? r.data : []).map((s) => s.name).sort()
  } catch { /* picker stays empty */ }
}

function failureText(e, fallback) {
  const d = e?.response?.data?.detail
  if (typeof d === 'string') return d
  if (d && typeof d.message === 'string') return d.message
  return e?.message || fallback
}

async function onToggle(next) {
  busy.value = 'toggle'
  actionError.value = ''; actionNote.value = ''
  try {
    const r = await axios.put(`${BASE}/enabled`, { enabled: next }, hdr())
    status.value = r.data
    enabled.value = Boolean(r.data?.enabled)
  } catch (e) {
    // Put the switch back where the server still has it.
    enabled.value = Boolean(status.value?.enabled)
    actionError.value = failureText(e, 'Could not change the setting.')
  } finally {
    busy.value = null
  }
}

async function run(which) {
  busy.value = which
  actionError.value = ''; actionNote.value = ''
  try {
    const r = await axios.post(`${BASE}/${which}`, {}, hdr())
    actionNote.value = which === 'sync'
      ? `Synced ${r.data?.skills_synced ?? 0} skills onto ${r.data?.agent || 'the runner'}.`
      : 'Runner provisioned.'
    await load()
  } catch (e) {
    actionError.value = failureText(e, `Could not ${which}.`)
  } finally {
    busy.value = null
  }
}

async function grant() {
  busy.value = 'grant'
  actionError.value = ''; actionNote.value = ''
  try {
    await axios.post(`${BASE}/access`,
      { caller_agent: newCaller.value, skill_name: newSkill.value }, hdr())
    newSkill.value = ''
    await load()
  } catch (e) {
    actionError.value = failureText(e, 'Could not grant access.')
  } finally {
    busy.value = null
  }
}

async function revoke(g) {
  if (!window.confirm(revokePrompt(g))) return
  busy.value = 'revoke'
  actionError.value = ''; actionNote.value = ''
  try {
    await axios.delete(`${BASE}/access`, {
      ...hdr(),
      params: { caller_agent: g.caller_agent, skill_name: g.skill_name },
    })
    await load()
  } catch (e) {
    actionError.value = failureText(e, 'Could not revoke access.')
  } finally {
    busy.value = null
  }
}

onMounted(load)
</script>
