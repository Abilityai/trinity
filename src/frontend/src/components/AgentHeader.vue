<template>
  <!--
    The agent page header. trinity-enterprise#887 (decision on trinity#966)
    cut it to identity, state and a few chips and moved the rest to Settings ›
    General and Overview; ent#950 brought repo, tags and a context ring back;
    trinity-enterprise#955 (operator, 2026-10-19) restores the pre-#3798 shape
    almost whole — the operator reads this card on every tab and wants the
    agent's working state here, not a click away:

      row 1  identity — avatar (hover: generate / change prompt / next
             emotion), name (pencil edits the DISPLAY LABEL in place), slug,
             description, badges incl. the funding badge that doubles as the
             subscription switcher; right: Brain door, context ring, Start/Stop
      row 2  autonomy and read-only toggles, tags; right: live CPU / memory /
             uptime sparklines (a stopped agent shows its allocation) and a
             gear that opens Settings › General for memory / CPU
      row 3  the 7-day spend row (sparkline, today, trend vs avg, lifetime)
      row 4  the GitHub line — repo, branch, commit, changed / ahead / behind —
             only when a repository is connected; Pull / Push / Refresh stay
             on the Git tab, deliberately (the operator's one exclusion)

    Still elsewhere: the id rename (stops the agent, re-keys ~20 tables, #1664)
    and memory / CPU editing → Settings › General; delete → Settings › General
    › Danger zone; the Workspace and the voice call → the Workspace (ent#950).
  -->
  <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg mb-4 relative" data-testid="agent-header-container">
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

    <!-- ROW 1: Identity + Primary Action -->
    <div class="p-4 pb-3 pl-16">
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
            <!-- Funding badge / subscription switcher (restored, ent#955): the
                 badge names what pays for this agent; for an owner a native
                 select is overlaid on it, so switching is one click here as
                 well as in Settings › General › Model and funding. -->
            <div v-if="authStatus" class="relative inline-flex items-center" data-testid="agent-header-funding">
              <BaseBadge
                :variant="authStatus.auth_mode === 'subscription' ? 'autonomous' : authStatus.auth_mode === 'api_key' ? 'neutral' : 'danger'"
                size="sm"
                :title="authStatus.auth_mode === 'subscription'
                  ? `Using subscription: ${authStatus.subscription_name}`
                  : authStatus.auth_mode === 'api_key'
                    ? 'Using platform API key'
                    : 'No auth configured'"
              >
                <span v-if="subscriptionChanging" class="inline-block w-2 h-2 border border-current border-t-transparent rounded-full animate-spin motion-reduce:animate-none mr-0.5" aria-hidden="true"></span>
                {{ fundingLabel }}
              </BaseBadge>
              <!-- Invisible native select overlaid on the badge for switching -->
              <select
                v-if="subscriptions !== null && canEditIdentity"
                class="absolute inset-0 opacity-0 cursor-pointer w-full"
                :disabled="subscriptionChanging"
                :value="authStatus.auth_mode === 'subscription' ? authStatus.subscription_name : ''"
                aria-label="Funding"
                title="Switch funding"
                data-testid="agent-header-funding-select"
                @change="$emit('change-subscription', $event.target.value)"
              >
                <option value="">API Key</option>
                <option v-for="sub in subscriptions" :key="sub.id" :value="sub.name">{{ sub.name }}</option>
              </select>
            </div>
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

    <!-- ROW 2: Settings + Stats (combined) — the pre-#3798 row, restored. -->
    <!-- #2197 — same treatment as row 1, and this is the row that actually
         dominated the measurement: toggles + Tags on the left and the live
         CPU/MEM/uptime cluster on the right add up to ~790px of min-content,
         so the page body scrolled horizontally from 1023px down. -->
    <div class="pl-16 pr-4 py-2.5 border-t border-gray-100 dark:border-gray-750 flex flex-wrap items-center gap-y-2" data-testid="agent-header-row-settings">
      <!-- Left side: Mode toggles + Tags -->
      <div class="flex flex-wrap items-center gap-y-2 min-w-0">
        <!-- Autonomy Toggle (not for system agents) -->
        <div v-if="canEditIdentity" class="flex items-center">
          <AutonomyToggle
            :model-value="agent.autonomy_enabled"
            :loading="autonomyLoading"
            size="sm"
            @toggle="$emit('toggle-autonomy')"
          />
        </div>
        <!-- Read-Only Toggle (not for system agents) -->
        <div v-if="canEditIdentity" class="flex items-center ml-4">
          <ReadOnlyToggle
            :model-value="agent.read_only_enabled"
            :loading="readOnlyLoading"
            size="sm"
            @toggle="$emit('toggle-read-only')"
          />
        </div>
        <!-- Divider -->
        <div v-if="canEditIdentity" class="h-4 w-px bg-gray-300 dark:bg-gray-600 mx-4" aria-hidden="true"></div>
        <!-- Tags — ent#950's chips + counted "+N" + dialog editor -->
        <AgentHeaderMeta
          :show-repo="false"
          :can-edit="!!agent.can_share"
          :tags="tags"
          :all-tags="allTags"
          @update-tags="$emit('update-tags', $event)"
          @add-tag="$emit('add-tag', $event)"
          @remove-tag="$emit('remove-tag', $event)"
        />
      </div>

      <!-- Right side: Stats (running) or Resource info (stopped) -->
      <!-- `ml-auto` still right-aligns this while both clusters share a line;
           once the row wraps it simply starts the second line. The inner
           `flex-wrap` matters on its own: the running-stats set is ~374px, wider
           than a 375px phone's content box all by itself. -->
      <div class="flex flex-wrap items-center ml-auto gap-x-4 gap-y-1 text-xs min-h-[20px]" data-testid="agent-header-live">
        <!-- When running: live stats with sparklines -->
        <template v-if="agent.status === 'running' && agentStats">
          <div class="flex items-center gap-1.5">
            <span class="text-gray-600 dark:text-gray-400">CPU</span>
            <SparklineChart :data="cpuHistory" :color="CPU_COLOR" :y-max="100" :width="40" :height="16" />
            <span class="font-mono tabular-nums w-10 text-right" :class="loadClass(agentStats.cpu_percent)">{{ agentStats.cpu_percent }}%</span>
            <!-- #1126: configured core ceiling, so live % reads against capacity -->
            <span class="font-mono text-gray-600 dark:text-gray-400">/ {{ resourceLimits.current_cpu || '2' }} cores</span>
          </div>
          <div class="flex items-center gap-1.5">
            <span class="text-gray-600 dark:text-gray-400">MEM</span>
            <SparklineChart :data="memoryHistory" :color="MEMORY_COLOR" :y-max="100" :width="40" :height="16" />
            <span class="font-mono tabular-nums w-14 text-right" :class="loadClass(agentStats.memory_percent)">{{ formatBytes(agentStats.memory_used_bytes) }}</span>
            <!-- #1126: configured max memory, so live usage reads against the ceiling -->
            <span class="font-mono text-gray-600 dark:text-gray-400">/ {{ (resourceLimits.current_memory || '4g').toUpperCase() }}</span>
          </div>
          <span class="font-mono tabular-nums text-gray-600 dark:text-gray-400 w-16 text-right" title="Uptime">{{ formatUptime(agentStats.uptime_seconds) }}</span>
        </template>
        <!-- Running, first sample not in yet: a skeleton the width of the cluster -->
        <span
          v-else-if="agent.status === 'running'"
          class="block h-3 w-64 rounded bg-gray-200 dark:bg-gray-750 animate-pulse motion-reduce:animate-none"
          aria-busy="true"
        ><span class="sr-only">Loading live usage…</span></span>
        <!-- When stopped: resource allocation -->
        <template v-else>
          <span v-if="agent.created" class="text-gray-600 dark:text-gray-400" :title="agent.created">{{ formatRelativeTime(agent.created) }}</span>
          <span class="text-gray-300 dark:text-gray-600" aria-hidden="true">|</span>
          <span class="font-mono text-gray-600 dark:text-gray-400">{{ resourceLimits.current_cpu || '2' }} CPU</span>
          <span class="font-mono text-gray-600 dark:text-gray-400">{{ (resourceLimits.current_memory || '4g').toUpperCase() }}</span>
        </template>
        <!-- Resource config: memory / CPU live in Settings › General since ent#887 -->
        <button
          v-if="canEditIdentity"
          type="button"
          class="p-1.5 text-gray-600 dark:text-gray-400 hover:text-action-primary-600 dark:hover:text-action-primary-400 hover:bg-gray-100 dark:hover:bg-gray-750 rounded transition-colors shrink-0 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
          title="Memory and CPU — opens Settings › General"
          aria-label="Configure memory and CPU"
          data-testid="agent-header-resources"
          @click="$emit('open-resources')"
        >
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
          </svg>
        </button>
      </div>
    </div>

    <!-- ROW 3: the 7-day spend row — sparkline + today vs average trend -->
    <div
      v-if="tokenStats && tokenStats.lifetime_executions > 0"
      class="px-4 py-2 border-t border-gray-100 dark:border-gray-750 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs"
      data-testid="agent-header-cost"
    >
      <!-- #471 Tier 0: subscription usage is shown as API-price equivalents,
           never presented as a bill. -->
      <BaseBadge
        v-if="isSubscriptionFunded"
        variant="purple"
        size="sm"
        :title="`Figures are API-price equivalents of subscription usage — not a bill. Funded by subscription: ${authStatus?.subscription_name || 'unknown'}`"
      >≈ API-equiv</BaseBadge>
      <div class="flex items-center gap-1.5">
        <span class="text-gray-600 dark:text-gray-400">7d</span>
        <SparklineChart :data="costSparkline" :color="COST_COLOR" :y-max="costSparklineMax" :width="56" :height="16" />
      </div>
      <div class="flex items-center gap-1">
        <span class="text-gray-600 dark:text-gray-400">Today</span>
        <span class="font-mono tabular-nums text-gray-800 dark:text-gray-200" :title="costUnreported ? UNREPORTED : null">{{ costUnreported ? '—' : costPrefix + formatCost(tokenStats.cost_24h) }}</span>
      </div>
      <span
        v-if="tokenStats.avg_daily_cost > 0"
        class="font-mono tabular-nums"
        :class="trendClass"
        :title="`7d avg: ${formatCost(tokenStats.avg_daily_cost)}/day`"
      >{{ trendArrow }} {{ formatTrendPct(tokenStats.trend_cost_pct) }} vs avg</span>
      <!-- #2197 — the cost row is the third that had to wrap: at 375px the
           lifetime-spend cluster is the last thing still pushing the page. -->
      <div class="ml-auto flex flex-wrap items-center gap-1 text-gray-600 dark:text-gray-400">
        <span>Lifetime</span>
        <span class="font-mono tabular-nums text-gray-800 dark:text-gray-200" :title="costUnreported ? UNREPORTED : null">{{ costUnreported ? '—' : costPrefix + formatCost(tokenStats.lifetime_cost) }}</span>
        <span aria-hidden="true">·</span>
        <span class="font-mono tabular-nums">{{ tokenStats.lifetime_executions }} runs</span>
      </div>
    </div>

    <!-- ROW 4: the GitHub line — only when a repository is connected. No
         Pull / Push / Refresh here (the Git tab owns the verbs). -->
    <div
      v-if="gitBound"
      class="px-4 py-2 border-t border-gray-100 dark:border-gray-750 flex items-center min-w-0"
      data-testid="agent-header-git"
    >
      <AgentHeaderMeta
        :show-tags="false"
        :git-status="gitStatus"
        :git-changes-count="gitChangesCount"
        :git-behind="gitBehind"
      />
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
import ReadOnlyToggle from './ReadOnlyToggle.vue'
import AgentHeaderMeta from './AgentHeaderMeta.vue'
import AgentHeaderContext from './AgentHeaderContext.vue'
import SparklineChart from './SparklineChart.vue'
import { useFormatters } from '../composables'
import { formatRelativeTime } from '../utils/timestamps'
import { CANVAS_PALETTE } from '../utils/canvasPalette'

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
  readOnlyLoading: {
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
  // Funding: `GET /api/agents/{name}/auth-status` and the owner's subscriptions
  // (null until fetched — the switcher only renders once the list is known).
  authStatus: {
    type: Object,
    default: null
  },
  subscriptions: {
    type: Array,
    default: null
  },
  subscriptionChanging: {
    type: Boolean,
    default: false
  },
  // Live container stats (AgentDetail's stats poll) and the memory / CPU
  // ceilings they read against (#1126).
  agentStats: {
    type: Object,
    default: null
  },
  cpuHistory: {
    type: Array,
    default: () => []
  },
  memoryHistory: {
    type: Array,
    default: () => []
  },
  resourceLimits: {
    type: Object,
    default: () => ({})
  },
  // 7-day spend (`GET /api/agents/{name}/token-stats`)
  tokenStats: {
    type: Object,
    default: null
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

// ent#955: `set-label`, `open-avatar-modal`, `cycle-emotion`, `toggle-read-only`
// and `change-subscription` are the SAME events Settings › General's rows emit
// — AgentDetail owns one handler per verb and both surfaces rejoin it.
// `open-resources` opens Settings › General (memory / CPU live there, ent#887).
const emit = defineEmits([
  'toggle', 'toggle-autonomy', 'toggle-read-only', 'update-tags', 'add-tag', 'remove-tag',
  'set-label', 'open-avatar-modal', 'cycle-emotion', 'change-subscription', 'open-resources',
])

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

// Funding badge text
const fundingLabel = computed(() => {
  const a = props.authStatus
  if (!a) return ''
  return a.auth_mode === 'subscription' ? a.subscription_name : a.auth_mode === 'api_key' ? 'API Key' : 'No Auth'
})

// uPlot strokes take a colour value, not a class; the palette module states
// why hex is unavoidable there (ent#536). Action-primary, accent-purple and
// state-autonomous — the three families these rows have always drawn in.
const CPU_COLOR = CANVAS_PALETTE[0]
const MEMORY_COLOR = CANVAS_PALETTE[7]
const COST_COLOR = CANVAS_PALETTE[2]
const UNREPORTED = 'Cost is not reported under this auth — token-based tracking only'

const { formatBytes, formatUptime, formatCost } = useFormatters()

// Status text is the 700 tier in light and the 400 tier in dark (#2201).
function loadClass(pct) {
  if (pct > 80) return 'text-status-danger-700 dark:text-status-danger-400'
  if (pct > 50) return 'text-status-warning-700 dark:text-status-warning-400'
  return 'text-status-success-700 dark:text-status-success-400'
}

const isSubscriptionFunded = computed(() => props.authStatus?.auth_mode === 'subscription')
const costPrefix = computed(() => (isSubscriptionFunded.value ? '≈' : ''))
// A subscription that never reports cost would render `≈$0.00` — a meaningless
// number — so an honest dash instead (the Step-0 fork, unchanged).
const costUnreported = computed(() =>
  isSubscriptionFunded.value
  && (props.tokenStats?.lifetime_executions || 0) > 0
  && !(props.tokenStats?.lifetime_cost > 0)
)
const costSparkline = computed(() => (props.tokenStats?.daily_breakdown || []).map((d) => d.cost))
const costSparklineMax = computed(() => Math.max(...costSparkline.value, 0.0001))

function formatTrendPct(pct) {
  if (!pct) return '—'
  const abs = Math.abs(pct)
  return `${abs >= 1 ? Math.round(abs) : abs.toFixed(1)}%`
}
const trendArrow = computed(() => {
  const pct = props.tokenStats?.trend_cost_pct ?? 0
  return pct > 5 ? '↑' : pct < -5 ? '↓' : '→'
})
const trendClass = computed(() => {
  const pct = props.tokenStats?.trend_cost_pct ?? 0
  if (pct > 5) return 'text-status-warning-700 dark:text-status-warning-400'
  if (pct < -5) return 'text-status-success-700 dark:text-status-success-400'
  return 'text-gray-600 dark:text-gray-400'
})

// A repository is "connected" when the live status says git is enabled AND
// names an origin. AgentDetail clears `gitStatus` on stop and on agent switch,
// so a stopped agent never shows a previous snapshot.
const gitBound = computed(() => !!(props.gitStatus?.git_enabled && props.gitStatus?.remote_url))

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
