<template>
  <!--
    The agent page header (trinity-enterprise#887, decision on trinity#966,
    amended by trinity-enterprise#950): identity, the running state, the
    autonomy chip, the bound repository with its git state, the tags and a
    small context-window chart — nothing else. Every other control has one
    home:
      - label and id edits, the avatar prompt, the full tag editor, funding,
        read-only and memory/CPU → Settings › General
      - delete → Settings › General › Danger zone (never a header button)
      - git pull / push / refresh → the Git tab
      - live CPU / memory and cost → Overview
      - the Workspace and the voice call → the Workspace (nav), where the call
        lives in the composer (ent#547); ent#950 removed the header doors
  -->
  <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg mb-4 relative">
    <!-- Overlapping Avatar (AVATAR-001) - centered on left edge of card (50% in, 50% out) -->
    <div class="absolute left-0 top-3 z-10 -translate-x-1/2">
      <div class="rounded-full border-[3px] border-action-primary-400 dark:border-action-primary-500 shadow-lg overflow-hidden">
        <div class="relative w-28 h-28">
          <Transition name="avatar-crossfade">
            <div :key="emotionAvatarUrl || agent.avatar_url" class="absolute inset-0">
              <AgentAvatar :name="agent.name" :avatar-url="emotionAvatarUrl || agent.avatar_url" size="3xl" />
            </div>
          </Transition>
        </div>
      </div>
    </div>

    <!-- Identity + doors -->
    <div class="p-4 pl-16 min-h-[8rem]">
      <!-- #2197 — `flex-wrap` + `gap-y-3`. Identity and the action buttons are
           both rigid (a name, the context chart, a Running toggle), so on a
           single non-wrapping row their combined min-content simply widened the
           PAGE at every width below ~1024px. Wrapping lets the actions drop to
           their own line; `min-w-0` on the identity block lets that flex item
           shrink below its content width so the actions wrap instead of the
           page widening (the name itself is not truncated). -->
      <div class="flex flex-wrap justify-between items-start gap-y-3">
        <!-- Left: Agent Identity -->
        <div class="min-w-0">
          <h1 class="text-2xl font-bold text-gray-900 dark:text-white">{{ displayName }}</h1>
          <!-- ent#181 FR-4: the slug is what URLs, MCP keys, containers and
               volumes key on — keep it visible when a label hides it. -->
          <div v-if="showsSlug || description" class="mt-0.5 flex flex-wrap items-baseline gap-x-2 text-[12.5px] text-gray-600 dark:text-gray-400">
            <code v-if="showsSlug" class="font-mono">{{ agent.name }}</code>
            <span v-if="showsSlug && description" aria-hidden="true">·</span>
            <span v-if="description" class="line-clamp-1" :title="description" data-testid="agent-header-description">{{ description }}</span>
          </div>
          <div class="flex flex-wrap items-center gap-2 mt-1.5">
            <BaseBadge :variant="agent.status === 'running' ? 'success' : 'neutral'" size="sm" dot>{{ agent.status }}</BaseBadge>
            <!-- #526: dispatch circuit breaker open badge (distinct danger styling) -->
            <BaseBadge
              v-if="agent.circuit_breaker_state === 'open'"
              variant="danger"
              size="sm"
              data-testid="circuit-open-badge"
              title="Dispatch circuit breaker OPEN — agent unhealthy; new tasks fast-fail until it recovers"
            >⚡ circuit open</BaseBadge>
            <!-- Runtime badge (Claude/Gemini) -->
            <RuntimeBadge :runtime="agent.runtime" />
            <!-- Ephemeral ghost badge (trinity-enterprise#69) -->
            <BaseBadge
              v-if="agent.ephemeral"
              variant="neutral"
              size="sm"
              title="Ephemeral agent — budgeted, auto-discarded when its executions or TTL run out (no recovery)"
            >GHOST</BaseBadge>
            <!-- System agent badge -->
            <BaseBadge
              v-if="agent.is_system"
              variant="purple"
              size="sm"
              title="System Agent - Platform Orchestrator with full access"
            >SYSTEM</BaseBadge>
            <BaseBadge v-if="agent.is_shared" variant="info" size="sm">Shared by {{ agent.owner }}</BaseBadge>
            <!-- The autonomy chip: an operating mode flipped daily, like Start
                 and Stop, so it stays in the header (trinity#966). -->
            <AutonomyToggle
              v-if="!agent.is_system && agent.can_share"
              :model-value="agent.autonomy_enabled"
              :loading="autonomyLoading"
              size="sm"
              @toggle="$emit('toggle-autonomy')"
            />
          </div>
          <AgentHeaderMeta
            :can-edit="!!agent.can_share"
            :git-status="gitStatus"
            :git-changes-count="gitChangesCount"
            :git-behind="gitBehind"
            :tags="tags"
            :all-tags="allTags"
            @update-tags="$emit('update-tags', $event)"
            @add-tag="$emit('add-tag', $event)"
            @remove-tag="$emit('remove-tag', $event)"
          />
        </div>
        <!-- Right: Primary Actions -->
        <!-- `gap-3`, not `space-x-3`: once this can wrap, a `space-x` row
             mis-indents every line after the first. -->
        <div class="flex flex-wrap items-center gap-3">
          <!-- Brain Orb logo (#60) — opens the agent's self-rendering mind page -->
          <button
            v-if="brainAvailable"
            @click="goToBrain"
            :disabled="agent.status !== 'running'"
            class="flex items-center justify-center w-8 h-8 rounded-full transition-colors"
            :class="agent.status === 'running'
              ? 'text-state-autonomous-500 dark:text-state-autonomous-400 hover:bg-state-autonomous-50 dark:hover:bg-state-autonomous-900/30 border border-state-autonomous-300 dark:border-state-autonomous-600'
              : 'text-gray-300 dark:text-gray-600 border border-gray-200 dark:border-gray-700 cursor-not-allowed'"
            title="Open Brain Orb — the agent's self-rendering mind"
          >
            <svg class="w-[18px] h-[18px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <circle cx="12" cy="12" r="9" stroke-width="1.6" />
              <path stroke-width="1.4" stroke-linecap="round" d="M3 12h18" opacity="0.7" />
              <path stroke-width="1.4" stroke-linecap="round" d="M12 3c3.2 2.4 3.2 15.6 0 18M12 3c-3.2 2.4-3.2 15.6 0 18" opacity="0.7" />
            </svg>
          </button>
          <!-- Context window (ent#950): running agents only — a stopped agent
               has no context to measure. -->
          <AgentHeaderContext v-if="agent.status === 'running'" :stats="contextStats" />
          <!-- Running State Toggle -->
          <RunningStateToggle
            :model-value="agent.status === 'running'"
            :loading="actionLoading"
            size="sm"
            @toggle="$emit('toggle')"
          />
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import AgentAvatar from './AgentAvatar.vue'
import RuntimeBadge from './RuntimeBadge.vue'
import BaseBadge from './base/BaseBadge.vue'
import { agentDisplayName, hasDistinctLabel } from '../utils/agentName'
import RunningStateToggle from './RunningStateToggle.vue'
import AutonomyToggle from './AutonomyToggle.vue'
import AgentHeaderMeta from './AgentHeaderMeta.vue'
import AgentHeaderContext from './AgentHeaderContext.vue'

const props = defineProps({
  agent: {
    type: Object,
    required: true
  },
  actionLoading: {
    type: Boolean,
    default: false
  },
  autonomyLoading: {
    type: Boolean,
    default: false
  },
  // What the agent is, in one line — read from its template (Settings ›
  // General shows the same text, as the one source).
  description: {
    type: String,
    default: ''
  },
  // Avatar emotion variant currently shown (AVATAR-002)
  emotionAvatarUrl: {
    type: String,
    default: null
  },
  // #60 — Brain Orb: platform flag AND the agent's brain-orb capability (resolved
  // in AgentDetail). Gates the header logo that opens the orb page.
  brainAvailable: {
    type: Boolean,
    default: false
  },
  // ent#950 — the bound repo line. The live git status AgentDetail polls
  // (useGitSync); null while stopped or not yet fetched.
  gitStatus: {
    type: Object,
    default: null
  },
  gitChangesCount: {
    type: Number,
    default: 0
  },
  gitBehind: {
    type: Number,
    default: 0
  },
  // ent#950 — tags (ORG-001), same events the pre-#3798 header emitted.
  tags: {
    type: Array,
    default: () => []
  },
  allTags: {
    type: Array,
    default: () => []
  },
  // ent#950 — this agent's entry in `stores/network.js::contextStats`, or null.
  contextStats: {
    type: Object,
    default: null
  }
})

defineEmits(['toggle', 'toggle-autonomy', 'update-tags', 'add-tag', 'remove-tag'])

// ent#181: one resolution helper, never `label || name` inline — a name resolved
// differently per surface shows one agent under two names (§1.3.1 FR-3).
const displayName = computed(() => agentDisplayName(props.agent))
const showsSlug = computed(() => hasDistinctLabel(props.agent))

const router = useRouter()

function goToBrain() {
  router.push({ name: 'AgentBrainOrb', params: { name: props.agent.name } })
}
</script>

<style scoped>
.avatar-crossfade-enter-active,
.avatar-crossfade-leave-active {
  transition: opacity 1s ease;
}
.avatar-crossfade-enter-from {
  opacity: 0;
}
.avatar-crossfade-leave-to {
  opacity: 0;
}
</style>
