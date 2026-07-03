<template>
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
      <!-- Hover overlay (owner only) -->
      <div
        v-if="agent.can_share && !agent.is_system"
        class="absolute inset-0 flex items-center justify-center rounded-full bg-black/0 group-hover:bg-black/40 transition-colors border-[3px] border-transparent"
      >
        <!-- Two-button hover UI when avatar exists with prompt -->
        <div v-if="agent.avatar_url && hasAvatarPrompt" class="flex items-center gap-3 opacity-0 group-hover:opacity-100 transition-opacity">
          <!-- Cycle emotion button -->
          <button
            @click="$emit('cycle-emotion')"
            class="p-1.5 rounded-full bg-white/20 hover:bg-white/40 transition-colors"
            title="Next emotion"
          >
            <svg class="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </button>
          <!-- Edit prompt button -->
          <button
            @click="$emit('open-avatar-modal')"
            class="p-1.5 rounded-full bg-white/20 hover:bg-white/40 transition-colors"
            title="Change avatar prompt"
          >
            <svg class="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
            </svg>
          </button>
        </div>
        <!-- Camera icon when no avatar (open modal) -->
        <button v-else @click="$emit('open-avatar-modal')" class="opacity-0 group-hover:opacity-100 transition-opacity cursor-pointer">
          <svg class="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z" />
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 13a3 3 0 11-6 0 3 3 0 016 0z" />
          </svg>
        </button>
      </div>
    </div>

    <!-- ROW 1: Identity + Primary Action -->
    <div class="p-4 pb-3 pl-16">
      <div class="flex justify-between items-start">
        <!-- Left: Agent Identity -->
        <div>
          <div class="flex items-center gap-2">
            <!-- Editable agent name -->
            <template v-if="isEditingName">
              <input
                ref="nameInput"
                v-model="editedName"
                type="text"
                class="text-2xl font-bold text-gray-900 dark:text-white bg-transparent border-b-2 border-action-primary-500 focus:outline-none focus:border-action-primary-600 py-0 px-0"
                :class="{ 'border-status-danger-500': nameError }"
                @keydown.enter="saveName"
                @keydown.escape="cancelEditName"
                @blur="saveName"
              />
              <span v-if="nameError" class="text-xs text-status-danger-500">{{ nameError }}</span>
            </template>
            <template v-else>
              <h1 class="text-2xl font-bold text-gray-900 dark:text-white">{{ agent.name }}</h1>
              <!-- Rename pencil icon (only for owners/admins, not system agents) -->
              <button
                v-if="agent.can_share && !agent.is_system"
                @click="startEditName"
                class="text-gray-400 dark:text-gray-500 hover:text-action-primary-600 dark:hover:text-action-primary-400 p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                title="Rename agent"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
                </svg>
              </button>
            </template>
          </div>
          <div class="flex items-center space-x-2 mt-1.5">
            <!-- Status badge -->
            <span :class="[
              'px-2 py-0.5 text-xs font-medium rounded-full',
              agent.status === 'running' ? 'bg-status-success-100 dark:bg-status-success-900/50 text-status-success-800 dark:text-status-success-300' : 'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-300'
            ]">
              {{ agent.status }}
            </span>
            <!-- #526: dispatch circuit breaker open badge (distinct danger styling) -->
            <span
              v-if="agent.circuit_breaker_state === 'open'"
              data-testid="circuit-open-badge"
              class="px-2 py-0.5 text-xs font-semibold rounded-full bg-status-danger-100 dark:bg-status-danger-900/50 text-status-danger-700 dark:text-status-danger-300"
              title="Dispatch circuit breaker OPEN — agent unhealthy; new tasks fast-fail until it recovers"
            >
              ⚡ circuit open
            </span>
            <!-- Runtime badge (Claude/Gemini) -->
            <RuntimeBadge :runtime="agent.runtime" />
            <!-- System agent badge -->
            <span
              v-if="agent.is_system"
              class="px-2 py-0.5 text-xs font-semibold rounded-full bg-accent-purple-100 text-accent-purple-700 dark:bg-accent-purple-900/50 dark:text-accent-purple-300"
              title="System Agent - Platform Orchestrator with full access"
            >
              SYSTEM
            </span>
            <!-- Shared badge -->
            <span v-if="agent.is_shared" class="px-2 py-0.5 text-xs font-medium bg-blue-100 dark:bg-blue-900/50 text-blue-700 dark:text-blue-300 rounded-full">
              Shared by {{ agent.owner }}
            </span>
            <!-- Auth method badge / subscription switcher -->
            <div v-if="authStatus" class="relative inline-flex items-center">
              <span
                class="px-2 py-0.5 text-xs font-medium rounded-full flex items-center gap-0.5"
                :class="authStatus.auth_mode === 'subscription'
                  ? 'bg-state-autonomous-100 dark:bg-state-autonomous-900/50 text-state-autonomous-700 dark:text-state-autonomous-300'
                  : authStatus.auth_mode === 'api_key'
                    ? 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300'
                    : 'bg-status-danger-100 dark:bg-status-danger-900/50 text-status-danger-600 dark:text-status-danger-400'"
                :title="authStatus.auth_mode === 'subscription'
                  ? `Using subscription: ${authStatus.subscription_name}`
                  : authStatus.auth_mode === 'api_key'
                    ? 'Using platform API key'
                    : 'No auth configured'"
              >
                <span v-if="subscriptionChanging" class="inline-block w-2 h-2 border border-current border-t-transparent rounded-full animate-spin mr-0.5"></span>
                {{ authStatus.auth_mode === 'subscription'
                  ? authStatus.subscription_name
                  : authStatus.auth_mode === 'api_key'
                    ? 'API Key'
                    : 'No Auth' }}
                <svg v-if="subscriptions !== null && agent.can_share" class="w-2.5 h-2.5 ml-0.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
                </svg>
              </span>
              <!-- Invisible native select overlaid on badge for switching -->
              <select
                v-if="subscriptions !== null && agent.can_share"
                class="absolute inset-0 opacity-0 cursor-pointer w-full"
                :disabled="subscriptionChanging"
                :value="authStatus.auth_mode === 'subscription' ? authStatus.subscription_name : ''"
                @change="$emit('change-subscription', $event.target.value)"
              >
                <option value="">API Key</option>
                <option v-for="sub in subscriptions" :key="sub.id" :value="sub.name">{{ sub.name }}</option>
              </select>
            </div>
          </div>
        </div>
        <!-- Right: Primary Actions -->
        <div class="flex items-center space-x-3">
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
          <!-- Workspace button (voice + canvas, BETA) -->
          <button
            v-if="workspaceAvailable"
            @click="goToWorkspace"
            :disabled="agent.status !== 'running'"
            class="flex items-center gap-1.5 px-2.5 py-1.5 rounded text-xs font-medium transition-colors"
            :class="agent.status === 'running'
              ? 'text-action-primary-600 dark:text-action-primary-400 hover:bg-action-primary-50 dark:hover:bg-action-primary-900/30 border border-action-primary-200 dark:border-action-primary-700'
              : 'text-gray-300 dark:text-gray-600 border border-gray-200 dark:border-gray-700 cursor-not-allowed'"
            title="Open Workspace — voice + canvas (Beta)"
          >
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4M12 15a3 3 0 003-3V5a3 3 0 00-6 0v7a3 3 0 003 3z" />
            </svg>
            Workspace
            <span class="px-1 py-0.5 text-[10px] font-semibold rounded bg-state-autonomous-100 dark:bg-state-autonomous-900/40 text-state-autonomous-700 dark:text-state-autonomous-400 leading-none">BETA</span>
          </button>
          <!-- Running State Toggle -->
          <RunningStateToggle
            :model-value="agent.status === 'running'"
            :loading="actionLoading"
            size="sm"
            @toggle="$emit('toggle')"
          />
          <!-- Delete button -->
          <button
            v-if="agent.can_delete"
            @click="$emit('delete')"
            class="text-gray-400 dark:text-gray-500 hover:text-status-danger-600 dark:hover:text-status-danger-400 p-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
            title="Delete agent"
          >
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
            </svg>
          </button>
        </div>
      </div>
    </div>

    <!-- ROW 2: Settings + Stats (combined) -->
    <div class="pl-16 pr-4 py-2.5 border-t border-gray-100 dark:border-gray-700 flex items-center">
      <!-- Left side: Mode toggles + Tags -->
      <div class="flex items-center">
        <!-- Autonomy Toggle (not for system agents) -->
        <div v-if="!agent.is_system && agent.can_share" class="flex items-center">
          <AutonomyToggle
            :model-value="agent.autonomy_enabled"
            :loading="autonomyLoading"
            size="sm"
            @toggle="$emit('toggle-autonomy')"
          />
        </div>
        <!-- Read-Only Toggle (not for system agents) -->
        <div v-if="!agent.is_system && agent.can_share" class="flex items-center ml-4">
          <ReadOnlyToggle
            :model-value="agent.read_only_enabled"
            :loading="readOnlyLoading"
            size="sm"
            @toggle="$emit('toggle-read-only')"
          />
        </div>
        <!-- Divider -->
        <div v-if="!agent.is_system && agent.can_share" class="h-4 w-px bg-gray-300 dark:bg-gray-600 mx-4"></div>
        <!-- Tags -->
        <div class="flex items-center">
          <span class="text-xs text-gray-400 dark:text-gray-500 mr-2 flex-shrink-0">Tags:</span>
          <TagsEditor
            :model-value="tags"
            :editable="agent.can_share"
            :all-tags="allTags"
            @update:model-value="$emit('update-tags', $event)"
            @add="$emit('add-tag', $event)"
            @remove="$emit('remove-tag', $event)"
          />
        </div>
      </div>

      <!-- Right side: Stats (running) or Resource info (stopped) -->
      <div class="flex items-center ml-auto space-x-4 text-xs">
        <!-- When running: Show live stats with sparklines -->
        <template v-if="agent.status === 'running' && agentStats">
          <!-- CPU -->
          <div class="flex items-center space-x-1.5">
            <span class="text-gray-400 dark:text-gray-500">CPU</span>
            <SparklineChart
              :data="cpuHistory"
              color="#3b82f6"
              :y-max="100"
              :width="40"
              :height="16"
            />
            <span
              class="font-mono w-10 text-right"
              :class="agentStats.cpu_percent > 80 ? 'text-status-danger-500' : agentStats.cpu_percent > 50 ? 'text-status-warning-500' : 'text-status-success-500'"
            >{{ agentStats.cpu_percent }}%</span>
            <!-- #1126: configured core ceiling, so live % reads against capacity -->
            <span class="text-gray-400 dark:text-gray-500 font-mono">/ {{ resourceLimits.current_cpu || '2' }} cores</span>
          </div>
          <!-- Memory -->
          <div class="flex items-center space-x-1.5">
            <span class="text-gray-400 dark:text-gray-500">MEM</span>
            <SparklineChart
              :data="memoryHistory"
              color="#a855f7"
              :y-max="100"
              :width="40"
              :height="16"
            />
            <span
              class="font-mono w-14 text-right"
              :class="agentStats.memory_percent > 80 ? 'text-status-danger-500' : agentStats.memory_percent > 50 ? 'text-status-warning-500' : 'text-status-success-500'"
            >{{ formatBytes(agentStats.memory_used_bytes) }}</span>
            <!-- #1126: configured max memory, so live usage reads against the ceiling -->
            <span class="text-gray-400 dark:text-gray-500 font-mono">/ {{ (resourceLimits.current_memory || '4g').toUpperCase() }}</span>
          </div>
          <!-- Uptime -->
          <div class="text-gray-500 dark:text-gray-400 font-mono w-16 text-right">
            {{ formatUptime(agentStats.uptime_seconds) }}
          </div>
        </template>
        <!-- When running but stats loading -->
        <template v-else-if="agent.status === 'running' && statsLoading">
          <div class="flex items-center space-x-2 text-gray-400 dark:text-gray-500">
            <div class="animate-spin h-3 w-3 border border-gray-300 dark:border-gray-600 border-t-gray-600 dark:border-t-gray-300 rounded-full"></div>
            <span>Loading...</span>
          </div>
        </template>
        <!-- When stopped: Show resource allocation -->
        <template v-else>
          <span class="text-gray-400 dark:text-gray-500">{{ formatRelativeTime(agent.created) }}</span>
          <span class="text-gray-300 dark:text-gray-600">|</span>
          <span class="text-gray-400 dark:text-gray-500 font-mono">{{ resourceLimits.current_cpu || '2' }} CPU</span>
          <span class="text-gray-400 dark:text-gray-500 font-mono">{{ (resourceLimits.current_memory || '4g').toUpperCase() }}</span>
        </template>
        <!-- Resource Config Button -->
        <button
          v-if="agent.can_share"
          @click="$emit('open-resource-modal')"
          class="p-1.5 text-gray-400 dark:text-gray-500 hover:text-action-primary-600 dark:hover:text-action-primary-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded transition-colors flex-shrink-0"
          title="Configure resources (Memory/CPU)"
        >
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
          </svg>
        </button>
      </div>
    </div>

    <!-- TOKEN USAGE ROW: 7-day sparkline + today vs average trend -->
    <div
      v-if="tokenStats && (tokenStats.lifetime_executions > 0)"
      class="px-4 py-2 border-t border-gray-100 dark:border-gray-700 flex items-center space-x-4 text-xs"
    >
      <!-- 7-day cost sparkline -->
      <div class="flex items-center space-x-1.5">
        <span class="text-gray-400 dark:text-gray-500">7d</span>
        <SparklineChart
          :data="tokenCostSparkline"
          color="#f59e0b"
          :y-max="tokenCostSparklineMax"
          :width="56"
          :height="16"
        />
      </div>
      <!-- Today's cost -->
      <div class="flex items-center space-x-1">
        <span class="text-gray-400 dark:text-gray-500">Today</span>
        <span class="font-mono text-gray-700 dark:text-gray-300">${{ formatCost(tokenStats.cost_24h) }}</span>
      </div>
      <!-- Trend vs 7d average -->
      <div v-if="tokenStats.avg_daily_cost > 0" class="flex items-center space-x-1">
        <span
          :class="trendClass"
          class="flex items-center space-x-0.5 font-mono"
          :title="`7d avg: $${formatCost(tokenStats.avg_daily_cost)}/day`"
        >
          <!-- Arrow icon -->
          <svg v-if="tokenStats.trend_cost_pct > 5" class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 15l7-7 7 7" />
          </svg>
          <svg v-else-if="tokenStats.trend_cost_pct < -5" class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M19 9l-7 7-7-7" />
          </svg>
          <svg v-else class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 12h14" />
          </svg>
          <span>{{ formatTrendPct(tokenStats.trend_cost_pct) }} vs avg</span>
        </span>
      </div>
      <!-- Lifetime cost -->
      <div class="ml-auto flex items-center space-x-1 text-gray-400 dark:text-gray-500">
        <span>Lifetime</span>
        <span class="font-mono text-gray-600 dark:text-gray-400">${{ formatCost(tokenStats.lifetime_cost) }}</span>
        <span class="text-gray-300 dark:text-gray-600">·</span>
        <span class="font-mono">{{ tokenStats.lifetime_executions }} runs</span>
      </div>
    </div>

    <!-- ROW 3: Git Controls (only when hasGitSync) -->
    <div v-if="hasGitSync" class="px-4 py-2.5 border-t border-gray-100 dark:border-gray-700 flex items-center flex-nowrap min-w-0">
      <!-- When running: Full git controls -->
      <template v-if="agent.status === 'running'">
        <!-- GitHub icon link -->
        <a
          v-if="gitStatus?.remote_url"
          :href="gitStatus.remote_url"
          target="_blank"
          rel="noopener noreferrer"
          class="flex-shrink-0 text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 transition-colors"
          title="Open GitHub repository"
        >
          <svg class="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
            <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
          </svg>
        </a>
        <!-- Branch name -->
        <span v-if="gitStatus?.branch" class="ml-2 text-xs font-mono text-gray-500 dark:text-gray-400 truncate max-w-[120px]">
          {{ gitStatus.branch }}
        </span>
        <!-- Commit hash -->
        <span
          v-if="gitStatus?.last_commit?.short_sha"
          class="ml-2 text-xs font-mono text-gray-400 dark:text-gray-500 flex-shrink-0"
          :title="`Commit: ${gitStatus.last_commit.message}\nAuthor: ${gitStatus.last_commit.author}\nDate: ${gitStatus.last_commit.date}`"
        >{{ gitStatus.last_commit.short_sha }}</span>
        <div class="flex-1"></div>
        <!-- Pull Latest button -->
        <button
          @click="$emit('git-pull')"
          :disabled="gitPulling || gitSyncing"
          class="flex-shrink-0 inline-flex items-center text-sm font-medium py-1 px-2.5 rounded transition-colors"
          :class="gitBehind > 0
            ? 'bg-blue-600 hover:bg-blue-700 disabled:bg-blue-400 text-white'
            : 'bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 disabled:bg-gray-50 dark:disabled:bg-gray-800 text-gray-600 dark:text-gray-300'"
          :title="gitBehind > 0 ? `Pull ${gitBehind} commit(s) from GitHub` : 'Already up to date'"
        >
          <svg v-if="gitPulling" class="animate-spin -ml-0.5 mr-1 h-3 w-3" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
          </svg>
          <svg v-else class="w-3 h-3 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
          </svg>
          {{ gitPulling ? 'Pulling...' : (gitBehind > 0 ? `Pull (${gitBehind})` : 'Pull') }}
        </button>
        <!-- Push button -->
        <button
          @click="$emit('git-push')"
          :disabled="gitSyncing || gitPulling"
          class="flex-shrink-0 ml-2 inline-flex items-center text-sm font-medium py-1 px-2.5 rounded transition-colors"
          :class="gitHasChanges
            ? 'bg-status-urgent-600 hover:bg-status-urgent-700 disabled:bg-status-urgent-400 text-white'
            : 'bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 disabled:bg-gray-50 dark:disabled:bg-gray-800 text-gray-600 dark:text-gray-300'"
          :title="gitHasChanges ? 'Push changes to GitHub' : 'No changes to push'"
        >
          <svg v-if="gitSyncing" class="animate-spin -ml-0.5 mr-1 h-3 w-3" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
          </svg>
          <svg v-else class="w-3 h-3 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
          </svg>
          {{ gitSyncing ? 'Pushing...' : (gitHasChanges ? `Push (${gitChangesCount})` : 'Push') }}
        </button>
        <!-- Refresh button -->
        <button
          @click="$emit('git-refresh')"
          :disabled="gitLoading"
          class="flex-shrink-0 ml-2 text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
          title="Refresh git status"
        >
          <svg :class="['w-4 h-4', gitLoading ? 'animate-spin' : '']" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
        </button>
      </template>
      <!-- When stopped: Show minimal indicator -->
      <template v-else>
        <svg class="w-4 h-4 text-gray-400 dark:text-gray-500" fill="currentColor" viewBox="0 0 24 24">
          <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
        </svg>
        <span class="ml-2 text-xs text-gray-400 dark:text-gray-500">Git enabled - start agent to sync</span>
      </template>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import AgentAvatar from './AgentAvatar.vue'
import RuntimeBadge from './RuntimeBadge.vue'
import SparklineChart from './SparklineChart.vue'
import RunningStateToggle from './RunningStateToggle.vue'
import AutonomyToggle from './AutonomyToggle.vue'
import ReadOnlyToggle from './ReadOnlyToggle.vue'
import TagsEditor from './TagsEditor.vue'
import { useFormatters } from '../composables'

// Name editing state
const isEditingName = ref(false)
const editedName = ref('')
const nameError = ref('')
const nameInput = ref(null)

const props = defineProps({
  agent: {
    type: Object,
    required: true
  },
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
  // Stats props
  agentStats: {
    type: Object,
    default: null
  },
  statsLoading: {
    type: Boolean,
    default: false
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
  // Git props
  gitStatus: {
    type: Object,
    default: null
  },
  hasGitSync: {
    type: Boolean,
    default: false
  },
  gitLoading: {
    type: Boolean,
    default: false
  },
  gitSyncing: {
    type: Boolean,
    default: false
  },
  gitPulling: {
    type: Boolean,
    default: false
  },
  gitHasChanges: {
    type: Boolean,
    default: false
  },
  gitChangesCount: {
    type: Number,
    default: 0
  },
  gitBehind: {
    type: Number,
    default: 0
  },
  // Tags props (ORG-001)
  tags: {
    type: Array,
    default: () => []
  },
  allTags: {
    type: Array,
    default: () => []
  },
  // Avatar props (AVATAR-001, AVATAR-002)
  hasAvatarPrompt: {
    type: Boolean,
    default: false
  },
  emotionAvatarUrl: {
    type: String,
    default: null
  },
  // Token usage stats (issue #250) — DB-sourced, persists across restarts
  tokenStats: {
    type: Object,
    default: null
  },
  voiceAvailable: {
    type: Boolean,
    default: false
  },
  workspaceAvailable: {
    type: Boolean,
    default: false
  },
  // #60 — Brain Orb: platform flag AND the agent's brain-orb capability (resolved
  // in AgentDetail). Gates the header logo that opens the orb page.
  brainAvailable: {
    type: Boolean,
    default: false
  }
})

const emit = defineEmits([
  'toggle',
  'delete',
  'toggle-autonomy',
  'toggle-read-only',
  'open-resource-modal',
  'git-pull',
  'git-push',
  'git-refresh',
  'update-tags',
  'add-tag',
  'remove-tag',
  'rename',
  'open-avatar-modal',
  'cycle-emotion',
  'change-subscription'
])

const router = useRouter()

function goToWorkspace() {
  router.push(`/agents/${props.agent.name}/workspace`)
}

function goToBrain() {
  router.push({ name: 'AgentBrainOrb', params: { name: props.agent.name } })
}

// Name editing functions
function startEditName() {
  editedName.value = props.agent.name
  nameError.value = ''
  isEditingName.value = true
  nextTick(() => {
    nameInput.value?.focus()
    nameInput.value?.select()
  })
}

function cancelEditName() {
  isEditingName.value = false
  editedName.value = ''
  nameError.value = ''
}

function saveName() {
  const trimmed = editedName.value.trim()

  // Validate
  if (!trimmed) {
    nameError.value = 'Name cannot be empty'
    return
  }

  if (trimmed === props.agent.name) {
    cancelEditName()
    return
  }

  // Emit rename event for parent to handle
  emit('rename', trimmed)
  isEditingName.value = false
  nameError.value = ''
}

const { formatBytes, formatUptime, formatRelativeTime } = useFormatters()

// Token stats helpers (issue #250)
const tokenCostSparkline = computed(() => {
  if (!props.tokenStats?.daily_breakdown) return []
  return props.tokenStats.daily_breakdown.map(d => d.cost)
})

const tokenCostSparklineMax = computed(() => {
  const vals = tokenCostSparkline.value
  if (!vals.length) return 1
  return Math.max(...vals, 0.0001)
})

function formatCost(val) {
  if (!val || val === 0) return '0.00'
  if (val < 0.01) return val.toFixed(4)
  return val.toFixed(2)
}

function formatTrendPct(pct) {
  if (!pct) return '—'
  const abs = Math.abs(pct)
  return `${abs >= 1 ? Math.round(abs) : abs.toFixed(1)}%`
}

const trendClass = computed(() => {
  const pct = props.tokenStats?.trend_cost_pct ?? 0
  if (pct > 5) return 'text-status-warning-600 dark:text-status-warning-400'
  if (pct < -5) return 'text-status-success-600 dark:text-status-success-400'
  return 'text-gray-400 dark:text-gray-500'
})
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
