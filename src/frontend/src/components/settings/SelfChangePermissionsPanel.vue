<template>
  <!--
    Permissions to change itself (trinity-enterprise#756) — the self-change grants of
    trinity-enterprise#164 as admin-only toggles. Visible to the owner (and
    admins); anyone else gets a 404 from the read and the card does not render.
    Every state shares the card's footprint: skeleton → toggles, or LoadFailed.
  -->
  <section
    v-if="!store.hidden"
    class="rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden p-6"
    data-testid="self-change-permissions"
  >
    <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Permissions to change itself</h3>
    <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
      What this agent may change about itself and other agents on its own. Every one is off until an
      instance admin turns it on; people and the system agent never need them.
    </p>

    <div v-if="view.state === 'loading'" aria-busy="true" data-testid="self-change-loading">
      <span class="sr-only">Loading permissions…</span>
      <SkeletonLoader :count="4" height="3.5rem" gap="1rem" />
    </div>

    <LoadFailed
      v-else-if="view.state === 'failed'"
      dense
      title="Couldn't load this agent's permissions"
      message="The permissions are saved on the platform; loading them failed. Try again."
      :detail="store.loadError"
      :retrying="retrying"
      @retry="retry"
    />

    <div v-else class="space-y-4" data-testid="self-change-ready">
      <!-- A failed refresh keeps what is on screen and says so (ent#253). -->
      <InlineError
        v-if="view.stale"
        :message="`Couldn't refresh the permissions — showing the last ones that loaded. ${store.loadError}`"
        retryable
        data-testid="self-change-stale"
        @retry="retry"
      />
      <p
        v-if="!canEdit"
        class="text-xs text-gray-600 dark:text-gray-300"
        data-testid="self-change-admin-only"
      >
        Only an instance admin can change these.
      </p>
      <p
        v-if="noneHeld"
        class="text-xs text-gray-600 dark:text-gray-300"
        data-testid="self-change-all-off"
      >
        None granted. Without them the agent is refused, by name, when it tries any of the changes below.
      </p>

      <div v-for="cap in rows" :key="cap.id" :data-testid="`self-change-${cap.id}`">
        <div class="flex items-start justify-between gap-3">
          <BaseToggle
            :model-value="cap.granted"
            :disabled="!canEdit || !!store.busy[cap.id]"
            :label="cap.label"
            :data-testid="`self-change-toggle-${cap.id}`"
            @update:model-value="(v) => store.setGranted(agentName, cap.id, v)"
          />
          <span
            class="shrink-0 text-xs text-gray-500 dark:text-gray-400 tabular-nums"
            :title="cap.granted ? absolute(cap.granted_at) : ''"
            :data-testid="`self-change-granted-${cap.id}`"
          >
            <template v-if="cap.granted">
              Granted by {{ cap.granted_by || 'an admin' }} · {{ relative(cap.granted_at) }}
            </template>
            <template v-else>Off</template>
          </span>
        </div>
        <p class="mt-1 pl-[46px] text-xs text-gray-500 dark:text-gray-400">
          {{ cap.can }} {{ cap.without }}
        </p>
        <InlineError
          class="mt-2"
          :message="store.actionErrors[cap.id]"
          retryable
          @retry="store.setGranted(agentName, cap.id, !cap.granted)"
          @dismiss="store.clearActionError(cap.id)"
        />
      </div>

      <div class="border-t border-gray-200 dark:border-gray-700 pt-4 space-y-3">
        <p class="text-xs text-gray-600 dark:text-gray-300" data-testid="self-change-autonomy">
          <span class="font-medium">Autonomy</span> ·
          <template v-if="store.autonomyEnabled === null">unknown</template>
          <template v-else>{{ store.autonomyEnabled ? 'on' : 'off' }}</template>
          — set by a person from the agent's header, and never something an agent can be granted.
        </p>

        <div data-testid="self-change-requests">
          <p v-if="store.requestsError" class="text-xs text-gray-600 dark:text-gray-300">
            {{ store.requestsError }}
          </p>
          <p v-else-if="!store.requests.length" class="text-xs text-gray-500 dark:text-gray-400">
            No open permission requests from this agent.
          </p>
          <template v-else>
            <p class="text-xs font-medium text-gray-700 dark:text-gray-300">
              Open permission requests · {{ store.requests.length }}
            </p>
            <ul class="mt-1 max-h-40 overflow-y-auto space-y-1">
              <li
                v-for="r in store.requests"
                :key="r.id"
                class="flex items-center justify-between gap-2 text-xs text-gray-600 dark:text-gray-300"
                :data-testid="`self-change-request-${r.id}`"
              >
                <span class="min-w-0 truncate" :title="r.title">
                  <code class="font-mono">{{ r.capability }}</code> · {{ r.title }}
                </span>
                <router-link
                  to="/operations"
                  class="shrink-0 text-action-primary-600 dark:text-action-primary-400 hover:underline"
                >Open in Operations</router-link>
              </li>
            </ul>
          </template>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useAuthStore } from '../../stores/auth'
import { useCapabilityGrantsStore } from '../../stores/capabilityGrants'
import { SELF_CHANGE_CAPABILITIES } from '../../utils/capabilityGrants'
import { viewState } from '../../utils/loadingState'
import { formatLocalDateTime, formatRelativeTime } from '../../utils/timestamps'
import BaseToggle from '../base/BaseToggle.vue'
import InlineError from '../InlineError.vue'
import LoadFailed from '../LoadFailed.vue'
import SkeletonLoader from '../SkeletonLoader.vue'

const props = defineProps({
  agentName: { type: String, required: true },
})

const store = useCapabilityGrantsStore()
const auth = useAuthStore()
const retrying = ref(false)

// The backend is the gate (admin + a signed-in session); this only decides
// whether the toggles look usable, so an owner is not offered a 403.
const canEdit = computed(() => auth.role === 'admin')

const view = computed(() =>
  viewState({ hasLoaded: store.hasLoaded, error: store.loadError, count: SELF_CHANGE_CAPABILITIES.length })
)

const rows = computed(() => SELF_CHANGE_CAPABILITIES.map((c) => {
  const g = store.grants.find((x) => x.capability === c.id) || {}
  return { ...c, granted: Boolean(g.granted), granted_by: g.granted_by, granted_at: g.granted_at }
}))
const noneHeld = computed(() => rows.value.every((r) => !r.granted))

function relative (ts) { return ts ? formatRelativeTime(ts) : '' }
function absolute (ts) { return ts ? formatLocalDateTime(ts) : '' }

async function retry () {
  retrying.value = true
  try { await store.load(props.agentName) } finally { retrying.value = false }
}

watch(() => props.agentName, (name) => store.load(name))
onMounted(() => store.load(props.agentName))
</script>
