<template>
  <!--
    The agent page header (trinity-enterprise#887, decision on trinity#966,
    amended by trinity-enterprise#950): identity, the running state, the
    autonomy chip, the bound repository with its git state, the tags and a
    small context-window chart — plus two in-place edits an owner reaches for
    daily (trinity-enterprise#955): the pencil beside the name edits the
    DISPLAY LABEL inline, and hovering the avatar offers the generate /
    change-prompt / next-emotion affordance. Both rows stay in Settings ›
    General too — the header is the quick path, Settings the full one. Every
    other control has one home:
      - the id rename (stops the agent, re-keys ~20 tables, #1664), the full
        tag editor, funding, read-only and memory/CPU → Settings › General
      - delete → Settings › General › Danger zone (never a header button)
      - git pull / push / refresh → the Git tab
      - live CPU / memory and cost → Overview
      - the Workspace and the voice call → the Workspace (nav), where the call
        lives in the composer (ent#547); ent#950 removed the header doors
  -->
  <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg mb-4 relative">
    <!-- Overlapping Avatar (AVATAR-001) - centered on left edge of card (50% in, 50% out) -->
    <div class="absolute left-0 top-3 z-10 group -translate-x-1/2">
      <div class="rounded-full border-[3px] border-action-primary-400 dark:border-action-primary-500 shadow-lg overflow-hidden">
        <div class="relative w-28 h-28">
          <Transition name="avatar-crossfade">
            <div :key="emotionAvatarUrl || agent.avatar_url" class="absolute inset-0">
              <AgentAvatar :name="agent.name" :avatar-url="emotionAvatarUrl || agent.avatar_url" size="3xl" />
            </div>
          </Transition>
        </div>
      </div>
      <!-- ent#955: the in-place avatar affordance, owners only. Hover (or focus
           within, for the keyboard) dims the portrait and shows the verbs; the
           same events Settings › General's Avatar row emits, so AgentDetail
           keeps one handler per verb. `bg-black/40` and `text-white` are the
           overlay's own ink on top of a photograph — a scrim has no theme. -->
      <div
        v-if="canEditIdentity"
        class="absolute inset-0 flex items-center justify-center rounded-full bg-black/0 group-hover:bg-black/40 group-focus-within:bg-black/40 transition-colors border-[3px] border-transparent"
        data-testid="agent-header-avatar-overlay"
      >
        <!-- An avatar generated from a prompt: change the prompt, or show the next emotion -->
        <div v-if="agent.avatar_url && hasAvatarPrompt" class="flex items-center gap-3 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 transition-opacity">
          <button
            type="button"
            class="p-1.5 rounded-full bg-white/20 hover:bg-white/40 text-white transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-white/70"
            title="Next emotion"
            aria-label="Show the next emotion"
            data-testid="agent-header-avatar-cycle"
            @click="$emit('cycle-emotion')"
          >
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </button>
          <button
            type="button"
            class="p-1.5 rounded-full bg-white/20 hover:bg-white/40 text-white transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-white/70"
            title="Change avatar prompt"
            aria-label="Change the avatar prompt"
            data-testid="agent-header-avatar-change"
            @click="$emit('open-avatar-modal')"
          >
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
            </svg>
          </button>
        </div>
        <!-- No avatar yet (or a bundled default with no prompt): generate one -->
        <button
          v-else
          type="button"
          class="opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 transition-opacity cursor-pointer text-white rounded-full focus:outline-none focus-visible:ring-2 focus-visible:ring-white/70"
          title="Generate an avatar"
          aria-label="Generate an avatar"
          data-testid="agent-header-avatar-add"
          @click="$emit('open-avatar-modal')"
        >
          <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z" />
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 13a3 3 0 11-6 0 3 3 0 016 0z" />
          </svg>
        </button>
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
        <!-- Left: Agent Identity. While the label is being edited the block
             takes the row's remaining width (`flex-1`) so the editor's
             `w-full` resolves against something definite — a content-sized
             block would size the input by its intrinsic ~20ch and clip a
             long name on select-all. At rest it stays content-sized, so the
             pencil sits beside the name and nothing else moves. -->
        <div class="min-w-0" :class="isEditingLabel ? 'flex-1' : ''">
          <!-- ent#955: the name edits in place. The editor renders INSIDE the
               h1 so it inherits the heading's type and ink (one footprint,
               nothing shifts), and it edits the LABEL (`display_label`) — the
               slug never moves from here; its rename is the heavyweight path
               in Settings › General (ent#181, #1664). Pre-filled with the
               label, never the slug: pre-filling the slug invites someone to
               "edit" a name they never chose. -->
          <div class="flex items-center gap-2 min-w-0">
            <h1 class="text-2xl font-bold text-gray-900 dark:text-white min-w-0" :class="isEditingLabel ? 'flex-1' : ''">
              <!-- `-mb-0.5` swallows the 2px underline so the rows below do
                   not drop while editing; `text-inherit` keeps the heading's
                   ink (preflight already gives the input the heading's font). -->
              <input
                v-if="isEditingLabel"
                ref="labelInput"
                v-model="editedLabel"
                type="text"
                class="block w-full max-w-md -mb-0.5 bg-transparent border-0 border-b-2 border-action-primary-500 focus:border-action-primary-600 dark:focus:border-action-primary-400 focus:outline-none focus:ring-0 py-0 px-0 text-inherit"
                :placeholder="agent.name"
                aria-label="Display name"
                data-testid="agent-header-label-input"
                @keydown.enter.prevent="saveLabel"
                @keydown.escape.prevent="cancelEditLabel"
                @blur="saveLabel"
              />
              <template v-else>{{ displayName }}</template>
            </h1>
            <button
              v-if="canEditIdentity && !isEditingLabel"
              type="button"
              class="shrink-0 p-1 rounded text-action-primary-600 dark:text-action-primary-400 hover:bg-action-primary-50 dark:hover:bg-action-primary-500/16 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
              title="Edit display name"
              aria-label="Edit display name"
              data-testid="agent-header-edit-label"
              @click="startEditLabel"
            >
              <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
              </svg>
            </button>
          </div>
          <!-- ent#181 FR-4: the slug is what URLs, MCP keys, containers and
               volumes key on — keep it visible when a label hides it. While
               the label is being edited this line carries the hint instead. -->
          <div v-if="isEditingLabel || showsSlug || description" class="mt-0.5 flex flex-wrap items-baseline gap-x-2 text-[12.5px] text-gray-600 dark:text-gray-400">
            <template v-if="isEditingLabel">
              <span data-testid="agent-header-label-hint">
                Display name — Enter saves, Esc cancels; leave it empty to show the id
                <code class="font-mono">{{ agent.name }}</code>.
              </span>
            </template>
            <template v-else>
              <code v-if="showsSlug" class="font-mono">{{ agent.name }}</code>
              <span v-if="showsSlug && description" aria-hidden="true">·</span>
              <span v-if="description" class="line-clamp-1" :title="description" data-testid="agent-header-description">{{ description }}</span>
            </template>
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
import { computed, nextTick, ref } from 'vue'
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
  // ent#955 — whether an identity prompt exists (AVATAR-001): with one, the
  // avatar overlay offers change-prompt + next-emotion; without, the camera.
  hasAvatarPrompt: {
    type: Boolean,
    default: false
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

// ent#955: `set-label`, `open-avatar-modal` and `cycle-emotion` are the SAME
// three events Settings › General's Identity rows emit — AgentDetail owns one
// handler per verb and both surfaces rejoin it.
const emit = defineEmits(['toggle', 'toggle-autonomy', 'update-tags', 'add-tag', 'remove-tag', 'set-label', 'open-avatar-modal', 'cycle-emotion'])

// ent#181: one resolution helper, never `label || name` inline — a name resolved
// differently per surface shows one agent under two names (§1.3.1 FR-3).
const displayName = computed(() => agentDisplayName(props.agent))
const showsSlug = computed(() => hasDistinctLabel(props.agent))

// Who may edit the identity in place: the same predicate Settings › General
// uses for its Identity rows (owner, and never a system agent).
const canEditIdentity = computed(() => !!props.agent.can_share && !props.agent.is_system)

// ent#955 — the in-place label edit. `display_label` only; one column, no
// restart, no re-key (ent#181).
const isEditingLabel = ref(false)
const editedLabel = ref('')
const labelInput = ref(null)

function startEditLabel() {
  editedLabel.value = props.agent.display_label || ''
  isEditingLabel.value = true
  nextTick(() => {
    labelInput.value?.focus()
    labelInput.value?.select()
  })
}

function cancelEditLabel() {
  // Esc abandons: the saving `@blur` that follows the editor unmounting must
  // find the editor already closed, so close first.
  isEditingLabel.value = false
  editedLabel.value = ''
}

function saveLabel() {
  if (!isEditingLabel.value) return
  const trimmed = editedLabel.value.trim()
  const current = props.agent.display_label || ''
  isEditingLabel.value = false
  if (trimmed === current) return
  // Empty CLEARS the label (the agent renders under its slug again) — it is
  // not an error. "No label" is a valid state.
  emit('set-label', trimmed || null)
}

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
