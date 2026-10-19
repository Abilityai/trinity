<template>
  <!--
    The agent page header (trinity-enterprise#887, decision on trinity#966):
    identity, the running state, the autonomy chip and the doors — nothing
    else. Every control that used to sit here has one home now:
      - label and id edits, the avatar prompt, tags, funding, read-only and
        memory/CPU → Settings › General
      - delete → Settings › General › Danger zone (never a header button)
      - git pull / push → the Git tab
      - live CPU / memory and cost → Overview
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
           both rigid (a name, a Talk/Workspace pair, a Running toggle), so on a
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
          <!-- Open in Workspace (ent#438). Was a voice-orb-plus-canvas page of
               its own, gated on VOICE_ENABLED && GEMINI_API_KEY; the page is
               retired and this now opens THE Workspace scoped to this agent.
               No longer flag-gated and no longer BETA: the Workspace is a
               first-class surface and needs no Gemini key, so gating this on
               `workspaceAvailable` would hide a working link on every install
               without one. Also no longer disabled while the agent is stopped —
               the Workspace page reports availability itself (#2196), and a
               dead button is a worse answer than a page that says why. -->
          <button
            @click="goToWorkspace"
            class="flex items-center gap-1.5 px-2.5 py-1.5 rounded text-xs font-medium transition-colors text-action-primary-600 dark:text-action-primary-400 hover:bg-action-primary-50 dark:hover:bg-action-primary-900/30 border border-action-primary-200 dark:border-action-primary-700"
            title="Open this agent in the Workspace"
          >
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 10h8M8 14h5M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            Workspace
          </button>
          <!-- Talk (#2559). A DOOR, not a surface: the voice call lives in the
               Workspace conversation, and this opens it there with the call
               starting. The chat-panel overlay that used to run a parallel call
               from this page — its own start route, its own transcript home —
               is retired; the mic glyph moved here with the affordance.

               Same tab, deliberately: the navigation is same-document, which is
               what lets `armVoiceAutoStart()`'s module-scoped one-shot survive
               to the Workspace (a new tab is a fresh document, where it is
               false by construction) and what keeps the Workspace's
               `AudioContext` resumable from the click that started the call.

               NOT gated, on purpose. The instance-wide voice flag this page
               could gate on is the same boolean the Workspace itself checks, so
               a gate here would buy nothing and cost two defects: the flag
               arrives after first paint (a button that pops in a beat late) and
               a failed flags fetch sets it false with no retry (a working
               feature hidden, indistinguishable from an instance without
               voice). The Workspace reports availability in words instead —
               ent#438's ruling, applied consistently. -->
          <button
            @click="goToTalk"
            data-testid="agent-talk"
            class="flex items-center gap-1.5 px-2.5 py-1.5 rounded text-xs font-medium transition-colors text-action-primary-600 dark:text-action-primary-400 hover:bg-action-primary-50 dark:hover:bg-action-primary-900/30 border border-action-primary-200 dark:border-action-primary-700"
            title="Talk to this agent by voice. The call opens in the Workspace, and the conversation lives there — not in this chat."
          >
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4M12 15a3 3 0 003-3V5a3 3 0 00-6 0v7a3 3 0 003 3z" />
            </svg>
            Talk
          </button>
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
import { armVoiceAutoStart } from './portal/portalVoiceMode'

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
  }
})

defineEmits(['toggle', 'toggle-autonomy'])

// ent#181: one resolution helper, never `label || name` inline — a name resolved
// differently per surface shows one agent under two names (§1.3.1 FR-3).
const displayName = computed(() => agentDisplayName(props.agent))
const showsSlug = computed(() => hasDistinctLabel(props.agent))

const router = useRouter()

function goToWorkspace() {
  // ent#438 — one workspace. `?agent=` is the Workspace's own selection param;
  // the retired per-agent route redirects here too, so an old bookmark and this
  // button land in the same place.
  router.push({ path: '/workspace', query: { agent: props.agent.name } })
}

function goToTalk() {
  // The intent is armed IN THE APP, never by the URL alone (#2559). A pasted,
  // bookmarked or mailed `?voice=1` arrives on a fresh document where `armed`
  // is false, so it opens the conversation and starts nothing.
  armVoiceAutoStart()
  router.push({ path: '/workspace', query: { agent: props.agent.name, voice: '1' } })
}

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
