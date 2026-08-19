<template>
  <!-- Dashboard List view (trinity-enterprise#260) — the Agents page's row list
       extracted into a props-driven panel. The Dashboard chassis owns fleet
       fetching (networkStore), the mode toggle, tag/owner filters, Create
       Agent, and the loading/error/true-empty ladder; this panel owns the
       rows, the name/status/sort toolbar, selection + bulk tag ops, and the
       filtered-empty state.

       Pane shell: the chassis column is overflow-hidden, so this root IS the
       scroll container (h-full overflow-y-auto). The horizontal padding
       replaces the old page shell's — it is what gives the half-out-of-card
       avatar (32px, 16px overhang) room without clipping or an h-scrollbar. -->
  <div class="h-full overflow-y-auto px-4 sm:px-6 lg:px-8 py-4">
    <!-- Notification Toast -->
    <div v-if="notification"
      :class="[
        'fixed top-20 right-4 z-50 px-4 py-3 rounded-lg shadow-lg transition-all duration-300',
        notification.type === 'success' ? 'bg-status-success-100 dark:bg-status-success-900/50 border border-status-success-400 dark:border-status-success-700 text-status-success-700 dark:text-status-success-300' : 'bg-status-danger-100 dark:bg-status-danger-900/50 border border-status-danger-400 dark:border-status-danger-700 text-status-danger-700 dark:text-status-danger-300'
      ]"
    >
      <span>{{ notification.message }}</span>
      <button
        v-if="notification.type === 'error'"
        type="button"
        class="ml-3 font-medium opacity-70 hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-status-danger-500/40 rounded"
        aria-label="Dismiss notification"
        @click="dismissNotification"
      >✕</button>
    </div>

    <!-- Filter toolbar (name / status / sort — tag + owner filters live in the
         chassis header and apply to all three views) -->
    <div class="mb-3 flex items-center gap-3 flex-wrap">
      <!-- Name search -->
      <div class="relative">
        <svg class="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 dark:text-gray-500 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
        </svg>
        <input
          v-model="filterName"
          type="text"
          placeholder="Search agents..."
          class="block w-44 rounded-md border-gray-300 dark:border-gray-600 shadow-sm focus:border-action-primary-500 focus:ring-action-primary-500 text-sm py-2 pl-8 pr-3 bg-white dark:bg-gray-700 dark:text-gray-200 border"
        />
      </div>

      <!-- Status filter -->
      <div class="flex rounded-md border border-gray-300 dark:border-gray-600 overflow-hidden text-sm">
        <button
          v-for="opt in statusOptions"
          :key="opt.value"
          @click="filterStatus = opt.value"
          :class="[
            'px-3 py-2 font-medium transition-colors',
            filterStatus === opt.value
              ? 'bg-action-primary-600 text-white'
              : 'bg-white dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-600'
          ]"
        >
          {{ opt.label }}
        </button>
      </div>

      <!-- Clear all filters (local name/status AND the chassis tag/owner layer) -->
      <button
        v-if="hasActiveFilters"
        @click="clearAllFilters"
        class="px-2.5 py-2 text-xs font-medium text-gray-500 dark:text-gray-400 hover:text-status-danger-600 dark:hover:text-status-danger-400 transition-colors flex items-center gap-1"
      >
        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
        </svg>
        Clear
      </button>

      <!-- Agent count — Y is the FULL fleet count, honest about chassis
           (tag/owner) narrowing on top of the local filters. Suppressed while
           the chassis type-to-filter query is active (ent#261 D9): the filter
           pill already shows "X of Y match" against a different denominator —
           two disagreeing counters must never render simultaneously. -->
      <span
        v-if="hasActiveFilters && !chassisQueryActive"
        class="text-xs text-gray-500 dark:text-gray-400"
      >
        {{ displayAgents.length }}/{{ totalAgentCount }}
      </span>

      <!-- Right side: sort -->
      <div class="flex items-center gap-3 ml-auto">
        <select
          v-model="agentsStore.sortBy"
          class="block rounded-md border-gray-300 dark:border-gray-600 shadow-sm focus:border-action-primary-500 focus:ring-action-primary-500 text-sm py-2 px-3 bg-white dark:bg-gray-700 dark:text-gray-200 border"
        >
          <option value="created_desc">Newest First</option>
          <option value="created_asc">Oldest First</option>
          <option value="name_asc">Name (A-Z)</option>
          <option value="name_desc">Name (Z-A)</option>
          <option value="status">Running First</option>
          <option value="success_desc">Success Rate</option>
        </select>
      </div>
    </div>

    <!-- Bulk Actions Toolbar — sticky inside the scroll pane so it stays
         reachable mid-selection on long lists -->
    <div
      v-if="selectedAgents.length > 0"
      class="sticky top-0 z-30 mb-4 p-3 bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-700 rounded-lg flex items-center justify-between"
    >
      <div class="flex items-center space-x-3">
        <span class="text-sm font-medium text-blue-700 dark:text-blue-300">
          {{ selectedAgents.length }} agent{{ selectedAgents.length > 1 ? 's' : '' }} selected
        </span>
        <button
          @click="clearSelection"
          class="text-xs text-blue-600 dark:text-blue-400 hover:underline"
        >
          Clear
        </button>
      </div>
      <div class="flex items-center space-x-2">
        <!-- Add Tag -->
        <div class="relative">
          <button
            @click="showBulkAddTag = !showBulkAddTag"
            class="px-3 py-1.5 bg-status-success-100 dark:bg-status-success-900/50 text-status-success-700 dark:text-status-success-300 rounded text-sm font-medium hover:bg-status-success-200 dark:hover:bg-status-success-900 transition-colors"
          >
            + Add Tag
          </button>
          <div
            v-if="showBulkAddTag"
            class="absolute right-0 top-full mt-1 bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-gray-200 dark:border-gray-700 p-3 z-50 min-w-64"
          >
            <div class="mb-2">
              <input
                v-model="bulkTagInput"
                @keyup.enter="applyBulkTag"
                type="text"
                placeholder="Enter tag name..."
                class="w-full px-3 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 dark:text-gray-200 focus:ring-1 focus:ring-blue-500 focus:border-blue-500"
              />
            </div>
            <div v-if="availableTags.length > 0" class="mb-2 max-h-32 overflow-y-auto">
              <div class="text-xs text-gray-500 dark:text-gray-400 mb-1">Or select existing:</div>
              <div class="flex flex-wrap gap-1">
                <button
                  v-for="tagInfo in availableTags"
                  :key="tagInfo.tag"
                  @click="bulkTagInput = tagInfo.tag"
                  :class="[
                    'px-2 py-0.5 rounded-full text-xs transition-colors',
                    bulkTagInput === tagInfo.tag
                      ? 'bg-blue-600 text-white'
                      : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
                  ]"
                >
                  #{{ tagInfo.tag }}
                </button>
              </div>
            </div>
            <div class="flex justify-end space-x-2">
              <button
                @click="showBulkAddTag = false; bulkTagInput = ''"
                class="px-3 py-1 text-sm text-gray-600 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200"
              >
                Cancel
              </button>
              <button
                @click="applyBulkTag"
                :disabled="!bulkTagInput.trim()"
                class="px-3 py-1 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Apply
              </button>
            </div>
          </div>
        </div>
        <!-- Remove Tag -->
        <div class="relative">
          <button
            @click="showBulkRemoveTag = !showBulkRemoveTag"
            class="px-3 py-1.5 bg-status-danger-100 dark:bg-status-danger-900/50 text-status-danger-700 dark:text-status-danger-300 rounded text-sm font-medium hover:bg-status-danger-200 dark:hover:bg-status-danger-900 transition-colors"
          >
            - Remove Tag
          </button>
          <div
            v-if="showBulkRemoveTag"
            class="absolute right-0 top-full mt-1 bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-gray-200 dark:border-gray-700 p-3 z-50 min-w-48"
          >
            <div v-if="commonTagsInSelection.length > 0">
              <div class="text-xs text-gray-500 dark:text-gray-400 mb-2">Tags in selected agents:</div>
              <div class="flex flex-wrap gap-1 mb-2">
                <button
                  v-for="tag in commonTagsInSelection"
                  :key="tag"
                  @click="removeBulkTag(tag)"
                  class="px-2 py-0.5 rounded-full text-xs bg-status-danger-100 dark:bg-status-danger-900/50 text-status-danger-700 dark:text-status-danger-300 hover:bg-status-danger-200 dark:hover:bg-status-danger-900 transition-colors"
                >
                  #{{ tag }} ×
                </button>
              </div>
            </div>
            <div v-else class="text-xs text-gray-500 dark:text-gray-400">
              No tags found on selected agents
            </div>
            <button
              @click="showBulkRemoveTag = false"
              class="mt-2 text-xs text-gray-600 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200"
            >
              Close
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Agents List -->
    <div class="flex flex-col gap-1.5">
      <!-- Column Header (lg+ only) -->
      <div class="hidden lg:grid lg:grid-cols-[auto_auto_auto_1fr_46px_22rem_180px_auto_auto] lg:gap-x-4 items-center pl-8 pr-4 py-2 text-xs font-medium text-gray-400 dark:text-gray-500 uppercase tracking-wider">
        <div class="w-4"></div>
        <div class="w-3"></div>
        <div class="w-2"></div>
        <div>Name</div>
        <div>Status</div>
        <div>Controls</div>
        <div>Success</div>
        <div>Exec / Sched</div>
        <div class="w-6"></div>
      </div>

      <!-- Agent Rows -->
      <div
        v-for="agent in displayAgents"
        :key="agent.name"
        :data-agent="agent.name"
        :class="[
          'relative overflow-visible bg-white dark:bg-gray-800 rounded-lg',
          'transition-colors duration-150 hover:bg-gray-50 dark:hover:bg-gray-750',
          agent.is_system
            ? 'border-l-3 border-l-purple-500'
            : '',
          agent.status !== 'running' && !agent.is_system
            ? 'opacity-75'
            : ''
        ]"
      >
        <!-- Avatar: half out of the box (all breakpoints) -->
        <div class="absolute left-0 top-1/2 -translate-x-1/2 -translate-y-1/2 z-10">
          <div class="rounded-full border-2 shadow-md overflow-hidden"
               :class="agent.is_system ? 'border-accent-purple-400 dark:border-accent-purple-500' : 'border-action-primary-400 dark:border-action-primary-500'">
            <AgentAvatar :name="agent.name" :avatar-url="agent.avatar_url" size="md" />
          </div>
        </div>

        <!-- Desktop layout (lg+) -->
        <div class="hidden lg:flex pl-8 pr-4 py-3">
          <!-- Two-row content block -->
          <div class="flex flex-col flex-1 min-w-0">
          <!-- Main grid (Row 1) -->
          <div class="grid grid-cols-[auto_auto_auto_1fr_46px_22rem_180px_auto_auto] gap-x-4 items-center">
            <!-- Checkbox -->
            <input
              type="checkbox"
              :checked="selectedAgents.includes(agent.name)"
              @change="toggleSelection(agent.name)"
              class="w-4 h-4 text-blue-600 bg-gray-100 dark:bg-gray-700 border-gray-300 dark:border-gray-600 rounded focus:ring-blue-500 cursor-pointer flex-shrink-0"
            />

            <!-- Status dot -->
            <div
              :class="[
                'w-2.5 h-2.5 rounded-full flex-shrink-0',
                isActive(agent.name) ? 'active-pulse' : ''
              ]"
              :style="{ backgroundColor: getStatusDotColor(agent.name) }"
            ></div>

            <!-- #389 Sync health dot (always in grid to preserve column positions) -->
            <div
              :class="['w-2 h-2 rounded-full flex-shrink-0', syncHealthEntry(agent.name) ? syncHealthColorClass(agent.name) : 'invisible']"
              :title="syncHealthEntry(agent.name) ? syncHealthLabel(agent.name) : ''"
            ></div>

            <!-- Name + badges -->
            <div class="flex items-center min-w-0 gap-2">
              <router-link
                :to="`/agents/${agent.name}`"
                class="text-gray-900 dark:text-white font-semibold text-sm truncate hover:text-action-primary-600 dark:hover:text-action-primary-400"
                :title="agentNameTooltip(agent)"
              >
                {{ agentDisplayName(agent) }}
              </router-link>
              <span
                v-if="agent.is_system"
                class="px-1.5 py-0.5 text-[10px] font-semibold bg-accent-purple-100 text-accent-purple-700 dark:bg-accent-purple-900/50 dark:text-accent-purple-300 rounded flex-shrink-0"
              >
                SYSTEM
              </span>
              <span
                v-if="agent.ephemeral"
                class="px-1.5 py-0.5 text-[10px] font-semibold bg-gray-200 text-gray-600 dark:bg-gray-700 dark:text-gray-300 rounded flex-shrink-0"
                title="Ephemeral agent — budgeted, auto-discarded when its executions or TTL run out (no recovery)"
              >
                GHOST
              </span>
              <RuntimeBadge :runtime="agent.runtime" :show-label="false" class="flex-shrink-0" />
              <span
                v-if="agent.is_shared"
                class="px-1.5 py-0.5 text-[10px] font-medium bg-blue-100 dark:bg-blue-900/50 text-blue-700 dark:text-blue-300 rounded flex-shrink-0"
                :title="'Shared by ' + agent.owner"
              >
                Shared
              </span>
              <!-- #471: subscription-pressure badge (shared predicate) -->
              <span
                v-if="pressureBadgeFor(agent.name)"
                class="px-1.5 py-0.5 text-[10px] font-semibold rounded flex-shrink-0"
                :class="pressureBadgeFor(agent.name).level === 'crit'
                  ? 'bg-status-danger-100 text-status-danger-700 dark:bg-status-danger-900/50 dark:text-status-danger-300'
                  : 'bg-status-warning-100 text-status-warning-700 dark:bg-status-warning-900/50 dark:text-status-warning-300'"
                :title="pressureBadgeFor(agent.name).title"
              >
                {{ pressureBadgeFor(agent.name).text }}
              </span>
            </div>

            <!-- Activity label -->
            <div
              :class="[
                'text-xs font-medium capitalize whitespace-nowrap',
                getActivityLabelClass(agent.name)
              ]"
            >
              {{ getActivityState(agent.name) }}
            </div>

            <!-- Toggles. System rows: the Run toggle is hidden (grid-tile
                 guard adopted, ent#260 item B) — stopping the system agent
                 stays on its Agent Detail page. -->
            <div class="flex items-center gap-1">
              <div class="w-[7rem] flex-shrink-0 flex justify-end" :class="{ 'invisible': agent.is_system }">
                <RunningStateToggle
                  :model-value="agent.status === 'running'"
                  :loading="networkStore.isTogglingRunning(agent.name)"
                  size="sm"
                  @toggle="handleRunningToggle(agent)"
                />
              </div>
              <div class="w-[7.5rem] flex-shrink-0 flex justify-end" :class="{ 'invisible': agent.is_system || agent.is_shared }">
                <ReadOnlyToggle
                  :model-value="!!agent.read_only_enabled"
                  :loading="readOnlyLoading === agent.name"
                  size="sm"
                  @toggle="handleReadOnlyToggle(agent)"
                />
              </div>
              <div class="w-[7rem] flex-shrink-0 flex justify-end" :class="{ 'invisible': agent.is_system }">
                <AutonomyToggle
                  :model-value="agent.autonomy_enabled"
                  :loading="autonomyLoading === agent.name"
                  size="sm"
                  @toggle="handleAutonomyToggle(agent)"
                />
              </div>
            </div>

            <!-- Success rate bar -->
            <div class="flex items-center gap-2">
              <template v-if="hasSuccessData(agent.name)">
                <div class="w-20 flex-shrink-0 bg-gray-200 dark:bg-gray-700 rounded-full h-1.5 overflow-hidden">
                  <div
                    class="h-full rounded-full transition-all duration-500"
                    :class="getSuccessBarColor(agent.name)"
                    :style="{ width: getSuccessBarPercent(agent.name) + '%' }"
                  ></div>
                </div>
                <span class="text-[10px] font-semibold tabular-nums" :class="getSuccessBarColor(agent.name).replace('bg-', 'text-')">{{ getSuccessBarPercent(agent.name) }}%</span>
                <span v-if="has7dStats(agent.name)" class="text-[9px] text-gray-400 dark:text-gray-500 tabular-nums">(7d: {{ get7dSuccessRate(agent.name) }}%)</span>
              </template>
              <template v-else-if="has7dOnlyStats(agent.name)">
                <div class="w-20 flex-shrink-0 bg-gray-200 dark:bg-gray-700 rounded-full h-1.5 overflow-hidden">
                  <div
                    class="h-full rounded-full transition-all duration-500"
                    :class="get7dSuccessBarColor(agent.name)"
                    :style="{ width: get7dSuccessRate(agent.name) + '%' }"
                  ></div>
                </div>
                <span class="text-[10px] font-semibold tabular-nums" :class="get7dSuccessBarColor(agent.name).replace('bg-', 'text-')">{{ get7dSuccessRate(agent.name) }}%</span>
                <span class="text-[9px] text-gray-400 dark:text-gray-500">(7d)</span>
              </template>
              <template v-else>
                <div class="w-20 flex-shrink-0 bg-gray-200 dark:bg-gray-700 rounded-full h-1.5 overflow-hidden">
                  <div class="h-full rounded-full bg-gray-300 dark:bg-gray-600" style="width: 0%"></div>
                </div>
                <span class="text-[10px] text-gray-400 dark:text-gray-500">&mdash;</span>
              </template>
            </div>

            <!-- Stats: executions + schedules -->
            <div class="flex items-center text-[11px] text-gray-500 dark:text-gray-400 gap-x-2 whitespace-nowrap">
              <!-- Executions count -->
              <div class="flex items-center gap-1">
                <svg class="w-3 h-3 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
                </svg>
                <span class="font-medium text-gray-700 dark:text-gray-300 tabular-nums">{{ hasExecutionStats(agent.name) ? getExecutionStats(agent.name).taskCount : 0 }}</span>
              </div>
              <!-- Schedules -->
              <div class="flex items-center gap-1">
                <svg class="w-3 h-3 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <span class="tabular-nums" :class="[hasSchedules(agent.name) ? 'font-medium text-gray-700 dark:text-gray-300' : '', agent.autonomy_enabled ? '' : hasSchedules(agent.name) ? 'line-through' : '']">{{ getSchedulesEnabled(agent.name) }}/{{ getSchedulesTotal(agent.name) }}</span>
              </div>
            </div>

            <!-- Arrow link -->
            <router-link
              :to="`/agents/${agent.name}`"
              class="text-gray-400 dark:text-gray-500 hover:text-action-primary-600 dark:hover:text-action-primary-400 transition-colors"
            >
              <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
              </svg>
            </router-link>
          </div>

          <!-- Bottom row: tags (always rendered for uniform height) -->
          <div class="flex items-center gap-1 pl-[5.125rem] min-h-[1.375rem] pt-1">
            <template v-if="getAgentTags(agent).length > 0">
              <span
                v-for="tag in getAgentTags(agent).slice(0, 3)"
                :key="tag"
                class="px-1.5 py-0.5 text-[10px] rounded-full bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400 truncate max-w-20"
                :title="'#' + tag"
              >
                #{{ tag }}
              </span>
              <span
                v-if="getAgentTags(agent).length > 3"
                class="text-[10px] text-gray-400 dark:text-gray-500 whitespace-nowrap"
              >
                +{{ getAgentTags(agent).length - 3 }}
              </span>
            </template>
          </div>
          </div><!-- end flex-col wrapper -->

          <!-- Capacity meter — full tile height -->
          <CapacityMeter
            :active="getSlotStats(agent.name) ? getSlotStats(agent.name).active : 0"
            :max="getSlotStats(agent.name) ? getSlotStats(agent.name).max : 3"
            :height="48"
            :width="6"
            class="ml-1 flex-shrink-0 self-stretch"
          />
        </div>

        <!-- Tablet layout (md, < lg) -->
        <div class="hidden md:flex md:flex-col lg:hidden pl-8 pr-4 py-3 gap-2">
          <div class="flex items-center gap-3">
            <input
              type="checkbox"
              :checked="selectedAgents.includes(agent.name)"
              @change="toggleSelection(agent.name)"
              class="w-4 h-4 text-blue-600 bg-gray-100 dark:bg-gray-700 border-gray-300 dark:border-gray-600 rounded focus:ring-blue-500 cursor-pointer flex-shrink-0"
            />
            <div
              :class="[
                'w-2.5 h-2.5 rounded-full flex-shrink-0',
                isActive(agent.name) ? 'active-pulse' : ''
              ]"
              :style="{ backgroundColor: getStatusDotColor(agent.name) }"
            ></div>
            <router-link
              :to="`/agents/${agent.name}`"
              class="text-gray-900 dark:text-white font-semibold text-sm truncate hover:text-action-primary-600 dark:hover:text-action-primary-400"
              :title="agentNameTooltip(agent)"
            >
              {{ agentDisplayName(agent) }}
            </router-link>
            <span
              v-if="agent.is_system"
              class="px-1.5 py-0.5 text-[10px] font-semibold bg-accent-purple-100 text-accent-purple-700 dark:bg-accent-purple-900/50 dark:text-accent-purple-300 rounded flex-shrink-0"
            >
              SYSTEM
            </span>
            <RuntimeBadge :runtime="agent.runtime" :show-label="false" class="flex-shrink-0" />
            <span
              v-if="agent.is_shared"
              class="px-1.5 py-0.5 text-[10px] font-medium bg-blue-100 dark:bg-blue-900/50 text-blue-700 dark:text-blue-300 rounded flex-shrink-0"
              :title="'Shared by ' + agent.owner"
            >
              Shared
            </span>
            <div class="ml-auto flex items-center gap-3">
              <div
                :class="[
                  'text-xs font-medium capitalize',
                  getActivityLabelClass(agent.name)
                ]"
              >
                {{ getActivityState(agent.name) }}
              </div>
              <router-link
                :to="`/agents/${agent.name}`"
                class="text-gray-400 dark:text-gray-500 hover:text-action-primary-600 dark:hover:text-action-primary-400 transition-colors"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                </svg>
              </router-link>
            </div>
          </div>
          <div class="flex items-center gap-3 pl-[3.125rem]">
            <div class="flex items-center gap-2">
              <RunningStateToggle
                v-if="!agent.is_system"
                :model-value="agent.status === 'running'"
                :loading="networkStore.isTogglingRunning(agent.name)"
                size="sm"
                @toggle="handleRunningToggle(agent)"
              />
              <ReadOnlyToggle
                v-if="!agent.is_system && !agent.is_shared"
                :model-value="!!agent.read_only_enabled"
                :loading="readOnlyLoading === agent.name"
                size="sm"
                @toggle="handleReadOnlyToggle(agent)"
              />
              <AutonomyToggle
                v-if="!agent.is_system"
                :model-value="agent.autonomy_enabled"
                :loading="autonomyLoading === agent.name"
                size="sm"
                @toggle="handleAutonomyToggle(agent)"
              />
            </div>
            <div class="flex items-center gap-2 flex-1 min-w-0">
              <template v-if="hasSuccessData(agent.name)">
                <div class="w-24 bg-gray-200 dark:bg-gray-700 rounded-full h-1.5 overflow-hidden">
                  <div
                    class="h-full rounded-full transition-all duration-500"
                    :class="getSuccessBarColor(agent.name)"
                    :style="{ width: getSuccessBarPercent(agent.name) + '%' }"
                  ></div>
                </div>
                <span class="text-[10px] font-semibold tabular-nums" :class="getSuccessBarColor(agent.name).replace('bg-', 'text-')">{{ getSuccessBarPercent(agent.name) }}%</span>
              </template>
              <template v-else-if="has7dOnlyStats(agent.name)">
                <div class="w-24 bg-gray-200 dark:bg-gray-700 rounded-full h-1.5 overflow-hidden">
                  <div
                    class="h-full rounded-full transition-all duration-500"
                    :class="get7dSuccessBarColor(agent.name)"
                    :style="{ width: get7dSuccessRate(agent.name) + '%' }"
                  ></div>
                </div>
                <span class="text-[10px] font-semibold tabular-nums" :class="get7dSuccessBarColor(agent.name).replace('bg-', 'text-')">{{ get7dSuccessRate(agent.name) }}%</span>
              </template>
              <template v-else>
                <div class="w-24 bg-gray-200 dark:bg-gray-700 rounded-full h-1.5 overflow-hidden"></div>
                <span class="text-[10px] text-gray-400 dark:text-gray-500">&mdash;</span>
              </template>
            </div>
            <CapacityMeter
              v-if="getSlotStats(agent.name)"
              :active="getSlotStats(agent.name).active"
              :max="getSlotStats(agent.name).max"
              :height="28"
              :width="10"
            />
            <div class="flex items-center text-[11px] text-gray-500 dark:text-gray-400 gap-x-1.5 whitespace-nowrap">
              <template v-if="hasExecutionStats(agent.name)">
                <span class="font-medium text-gray-700 dark:text-gray-300">{{ getExecutionStats(agent.name).taskCount }} tasks</span>
                <template v-if="getExecutionStats(agent.name).totalCost > 0">
                  <span class="text-gray-300 dark:text-gray-600">·</span>
                  <span class="font-medium text-gray-700 dark:text-gray-300" :title="costIsApproximate(agent.name) ? 'API-price equivalent of subscription usage — not a bill' : null">{{ costIsApproximate(agent.name) ? '≈' : '' }}{{ formatCostCompact(getExecutionStats(agent.name).totalCost) }}</span>
                </template>
              </template>
              <span v-else class="text-gray-400 dark:text-gray-500">--</span>
            </div>
          </div>
          <!-- Tags row (tablet) -->
          <div v-if="getAgentTags(agent).length > 0" class="flex items-center gap-1 pl-[3.125rem]">
            <span
              v-for="tag in getAgentTags(agent).slice(0, 3)"
              :key="tag"
              class="px-1.5 py-0.5 text-[10px] rounded-full bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400 truncate max-w-20"
              :title="'#' + tag"
            >
              #{{ tag }}
            </span>
            <span
              v-if="getAgentTags(agent).length > 3"
              class="text-[10px] text-gray-400 dark:text-gray-500 whitespace-nowrap"
            >
              +{{ getAgentTags(agent).length - 3 }}
            </span>
          </div>
        </div>

        <!-- Mobile layout (< md) -->
        <div class="flex flex-col md:hidden pl-8 pr-4 py-3 gap-2">
          <div class="flex items-center gap-3">
            <input
              type="checkbox"
              :checked="selectedAgents.includes(agent.name)"
              @change="toggleSelection(agent.name)"
              class="w-4 h-4 text-blue-600 bg-gray-100 dark:bg-gray-700 border-gray-300 dark:border-gray-600 rounded focus:ring-blue-500 cursor-pointer flex-shrink-0"
            />
            <div
              :class="[
                'w-2.5 h-2.5 rounded-full flex-shrink-0',
                isActive(agent.name) ? 'active-pulse' : ''
              ]"
              :style="{ backgroundColor: getStatusDotColor(agent.name) }"
            ></div>
            <router-link
              :to="`/agents/${agent.name}`"
              class="text-gray-900 dark:text-white font-semibold text-sm truncate hover:text-action-primary-600 dark:hover:text-action-primary-400 flex-1 min-w-0"
              :title="agentNameTooltip(agent)"
            >
              {{ agentDisplayName(agent) }}
            </router-link>
            <span
              v-if="agent.is_system"
              class="px-1.5 py-0.5 text-[10px] font-semibold bg-accent-purple-100 text-accent-purple-700 dark:bg-accent-purple-900/50 dark:text-accent-purple-300 rounded flex-shrink-0"
            >
              SYS
            </span>
            <RuntimeBadge :runtime="agent.runtime" :show-label="false" class="flex-shrink-0" />
            <div class="flex items-center gap-2 flex-shrink-0">
              <RunningStateToggle
                v-if="!agent.is_system"
                :model-value="agent.status === 'running'"
                :loading="networkStore.isTogglingRunning(agent.name)"
                size="sm"
                @toggle="handleRunningToggle(agent)"
              />
              <router-link
                :to="`/agents/${agent.name}`"
                class="text-gray-400 dark:text-gray-500 hover:text-action-primary-600 dark:hover:text-action-primary-400 transition-colors"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                </svg>
              </router-link>
            </div>
          </div>
          <div class="flex items-center gap-3 pl-[3.25rem] text-[11px] text-gray-500 dark:text-gray-400">
            <div
              :class="[
                'font-medium capitalize',
                getActivityLabelClass(agent.name)
              ]"
            >
              {{ getActivityState(agent.name) }}
            </div>
            <template v-if="hasSuccessData(agent.name)">
              <span class="text-gray-300 dark:text-gray-600">·</span>
              <span class="font-medium" :class="getSuccessBarColor(agent.name).replace('bg-', 'text-')">{{ getSuccessBarPercent(agent.name) }}%</span>
              <span class="text-gray-300 dark:text-gray-600">·</span>
              <span class="font-medium">{{ getExecutionStats(agent.name).taskCount }} tasks</span>
            </template>
            <template v-else-if="has7dOnlyStats(agent.name)">
              <span class="text-gray-300 dark:text-gray-600">·</span>
              <span class="font-medium" :class="get7dSuccessBarColor(agent.name).replace('bg-', 'text-')">{{ get7dSuccessRate(agent.name) }}% (7d)</span>
            </template>
          </div>
        </div>
      </div>
    </div>

    <!-- Filtered-empty state (the true-empty "no agents at all" state is
         chassis-owned in Dashboard.vue). `agents.length > 0` (ent#261): since
         the type-to-filter, the panel DOES mount with a zero-agent prop when
         the chassis query matches nothing — the chassis query-empty overlay
         owns that messaging, and rendering this card too would show two
         contradicting CTAs at once. This card renders only when the panel's
         OWN name/status filters narrowed a non-empty prop to zero. -->
    <div v-if="displayAgents.length === 0 && agents.length > 0" class="text-center py-12 bg-white dark:bg-gray-800 rounded-xl shadow">
      <ServerIcon class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-500" />
      <h3 class="mt-2 text-sm font-medium text-gray-900 dark:text-gray-100">No matching agents</h3>
      <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Try adjusting your filters.</p>
      <div class="mt-4">
        <button
          @click="clearAllFilters"
          class="inline-flex items-center px-4 py-2 border border-gray-300 dark:border-gray-600 shadow-sm text-sm font-medium rounded-md text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600"
        >
          Clear all filters
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { formatCostCompact } from '../composables/useFormatters'
import { useNotification } from '../composables/useNotification'
import { useAgentsStore } from '../stores/agents'
import { useNetworkStore } from '../stores/network'
import { agentDisplayName, agentNameTooltip } from '../utils/agentName'
import { isOrgTag } from '../utils/gridOrg'
import { sortAgents } from '../utils/agentSort'
import AgentAvatar from './AgentAvatar.vue'
import RuntimeBadge from './RuntimeBadge.vue'
import RunningStateToggle from './RunningStateToggle.vue'
import AutonomyToggle from './AutonomyToggle.vue'
import ReadOnlyToggle from './ReadOnlyToggle.vue'
import CapacityMeter from './CapacityMeter.vue'
import { ServerIcon } from '@heroicons/vue/24/outline'
import axios from 'axios'
import { syncHealthColor, syncHealthLabel as syncHealthLabelFn } from '../utils/syncHealth'
import { pressureBadge, isSubscriptionFunded } from '../utils/subscriptionPressure'

const props = defineProps({
  // The chassis-visible fleet (networkStore.visibleAgents — server-side tag
  // filter ∘ owner filter). Rows read `tags` / `read_only_enabled` /
  // `display_label` straight off these objects — they ride every
  // GET /api/agents row, so the panel does ZERO per-agent fetches.
  agents: {
    type: Array,
    required: true
  },
  // /api/tags entries [{tag, count}] — fetched by the chassis (it already
  // needs them for the quick-tag filter); used here for the bulk-add picker.
  availableTags: {
    type: Array,
    default: () => []
  }
})

const emit = defineEmits(['tags-changed', 'clear-chassis-filters'])

// Store composition contract (ent#260 D1): networkStore is the data spine
// (fleet rows via the `agents` prop, contextStats/executionStats/slotStats,
// Run/Autonomy actions + isTogglingRunning). agentsStore is composed in for
// exactly two members — `sortBy` (session-lived sort state; a panel-local ref
// would reset on every v-if teardown) and `syncHealth`/`fetchSyncHealth`
// (#389). Do not widen this surface.
const agentsStore = useAgentsStore()
const networkStore = useNetworkStore()

const autonomyLoading = ref(null)
const readOnlyLoading = ref(null)

// Filter state — NEW dashboard-scoped keys (ent#260 D11): a deliberate clean
// break from the retired page's `trinity-agents-filter-*` keys, so a filter
// set on the dead Agents page can never silently narrow the List tab.
const filterName = ref(localStorage.getItem('trinity-dashboard-list-filter-name') || '')
const filterStatus = ref(localStorage.getItem('trinity-dashboard-list-filter-status') || 'all')
const statusOptions = [
  { value: 'all', label: 'All' },
  { value: 'running', label: 'Running' },
  { value: 'stopped', label: 'Stopped' }
]

// Selection + bulk-op state (panel-local; wiped on mode switch by design —
// documented in feature-flows/dashboard-list-view.md)
const selectedAgents = ref([])
const showBulkAddTag = ref(false)
const showBulkRemoveTag = ref(false)
const bulkTagInput = ref('')

// Persist filter state across reloads
watch(filterName, (val) => {
  if (val) {
    localStorage.setItem('trinity-dashboard-list-filter-name', val)
  } else {
    localStorage.removeItem('trinity-dashboard-list-filter-name')
  }
})

watch(filterStatus, (val) => {
  if (val && val !== 'all') {
    localStorage.setItem('trinity-dashboard-list-filter-status', val)
  } else {
    localStorage.removeItem('trinity-dashboard-list-filter-status')
  }
})

const hasActiveFilters = computed(() => {
  return filterName.value.trim() !== '' || filterStatus.value !== 'all'
})

// ent#261 D9: the chassis type-to-filter query state — read to suppress the
// local "N/M" badge while the chassis pill shows its own "X of Y match".
const chassisQueryActive = computed(() => networkStore.filterQuery.trim() !== '')

function clearAllFilters() {
  // Clears BOTH layers (ent#260 strategy F6): the local name/status filters
  // here, and the chassis quick-tag + owner filters via the emit — the old
  // page's button cleared all four, half-clearing would be a regression.
  filterName.value = ''
  filterStatus.value = 'all'
  emit('clear-chassis-filters')
}

// Total agent count before ANY filtering (full fleet — honest "X of Y" even
// when the chassis tag/owner filters narrowed the visible set).
const totalAgentCount = computed(() => networkStore.agents.length)

// Rows: local name/status filters over the chassis-visible set, then the
// shared comparator (system rows pinned first; executionStats read here so
// the computed tracks the stats poll).
const displayAgents = computed(() => {
  let agents = props.agents

  // Filter by name — #1642: match BOTH the slug and the display name, so
  // typing "TOM" finds an agent labelled "TOM" whose slug is `tom-marketing-ops`.
  const nameQuery = filterName.value.trim().toLowerCase()
  if (nameQuery) {
    agents = agents.filter(agent =>
      agent.name.toLowerCase().includes(nameQuery) ||
      agentDisplayName(agent).toLowerCase().includes(nameQuery)
    )
  }

  // Filter by status
  if (filterStatus.value === 'running') {
    agents = agents.filter(agent => agent.status === 'running')
  } else if (filterStatus.value === 'stopped') {
    agents = agents.filter(agent => agent.status !== 'running')
  }

  return sortAgents(agents, agentsStore.sortBy, networkStore.executionStats)
})

// Tags now ride the fleet payload (agent.tags) — union across the selection
// resolved from the prop rows, no per-agent fetch.
const commonTagsInSelection = computed(() => {
  if (selectedAgents.value.length === 0) return []
  const allTags = new Set()
  const byName = new Map(props.agents.map(a => [a.name, a]))
  selectedAgents.value.forEach(agentName => {
    const tags = byName.get(agentName)?.tags || []
    tags.forEach(tag => allTags.add(tag))
  })
  return Array.from(allTags).sort()
})

// #1926: was a local copy of the toast helper with a hardcoded 3s
// auto-dismiss, so an error toast vanished before it could be read or acted
// on. Now the shared composable, which keeps errors up until dismissed
// (principle 18).
const { notification, showNotification, dismissNotification } = useNotification()

// #389 sync-health: mount fetch + 60s visibility-aware refresh while the List
// mode is active (mirrors the grid's chip-poll discipline — a parked List tab
// must not show stale dots). The interval dies with the v-if teardown.
let syncHealthInterval = null
onMounted(() => {
  agentsStore.fetchSyncHealth()
  agentsStore.fetchSubscriptionPressure()  // #471: same poll discipline
  syncHealthInterval = setInterval(() => {
    if (document.hidden) return
    agentsStore.fetchSyncHealth()
    agentsStore.fetchSubscriptionPressure()
  }, 60000)
})

onUnmounted(() => {
  if (syncHealthInterval) {
    clearInterval(syncHealthInterval)
    syncHealthInterval = null
  }
})

// Chassis refresh hook (Dashboard refreshAll → list branch). Fleet + tags
// refresh are chassis-side; sync health is the panel's only own fetch.
function refresh() {
  return Promise.allSettled([
    agentsStore.fetchSyncHealth(),
    agentsStore.fetchSubscriptionPressure(),  // #471
  ])
}
defineExpose({ refresh })

// #471: badge + Tier 0 ≈ helpers (shared predicate — utils/subscriptionPressure.js)
function pressureBadgeFor(agentName) {
  return pressureBadge(agentsStore.subscriptionPressure[agentName])
}
function costIsApproximate(agentName) {
  return isSubscriptionFunded(agentsStore.subscriptionPressure[agentName])
}

// #389 Sync health helpers
const syncHealthEntry = (agentName) => agentsStore.syncHealth[agentName] || null
const syncHealthColorClass = (agentName) => syncHealthColor(syncHealthEntry(agentName))
const syncHealthLabel = (agentName) => syncHealthLabelFn(syncHealthEntry(agentName))

// Activity state helpers
const getActivityState = (agentName) => {
  const stats = networkStore.contextStats[agentName]
  if (!stats) return 'Offline'
  const state = stats.activityState
  if (state === 'active') return 'Active'
  if (state === 'idle') return 'Idle'
  return 'Offline'
}

const isActive = (agentName) => {
  return getActivityState(agentName) === 'Active'
}

const getStatusDotColor = (agentName) => {
  const state = getActivityState(agentName)
  if (state === 'Active') return '#10b981' // green-500
  if (state === 'Idle') return '#10b981' // green-500
  return '#9ca3af' // gray-400
}

const getActivityLabelClass = (agentName) => {
  const state = getActivityState(agentName)
  if (state === 'Active' || state === 'Idle') return 'text-status-success-600 dark:text-status-success-400'
  return 'text-gray-500 dark:text-gray-400'
}

// Success rate bar helpers
const getSuccessBarPercent = (agentName) => {
  const stats = networkStore.executionStats[agentName]
  return stats ? Math.round(stats.successRate || 0) : 0
}

const getSuccessBarColor = (agentName) => {
  const percent = getSuccessBarPercent(agentName)
  if (percent >= 90) return 'bg-status-success-500'
  if (percent >= 50) return 'bg-status-warning-500'
  return 'bg-status-danger-500'
}

const hasSuccessData = (agentName) => {
  const stats = networkStore.executionStats[agentName]
  return stats && stats.taskCount > 0
}

const has7dOnlyStats = (agentName) => {
  const stats = networkStore.executionStats[agentName]
  return stats && stats.taskCount === 0 && stats.taskCount7d > 0
}

const has7dStats = (agentName) => {
  const stats = networkStore.executionStats[agentName]
  return stats && stats.taskCount7d > 0
}

const get7dSuccessRate = (agentName) => {
  const stats = networkStore.executionStats[agentName]
  return stats ? Math.round(stats.successRate7d || 0) : 0
}

const get7dSuccessBarColor = (agentName) => {
  const percent = get7dSuccessRate(agentName)
  if (percent >= 90) return 'bg-status-success-500'
  if (percent >= 50) return 'bg-status-warning-500'
  return 'bg-status-danger-500'
}

// Slot stats helpers (for capacity meters)
const getSlotStats = (agentName) => {
  return networkStore.slotStats[agentName] || null
}

// Execution stats helpers
const getExecutionStats = (agentName) => {
  return networkStore.executionStats[agentName] || null
}

const hasExecutionStats = (agentName) => {
  const stats = getExecutionStats(agentName)
  return stats && stats.taskCount > 0
}

// Schedule stats helpers
const getSchedulesTotal = (agentName) => {
  const stats = getExecutionStats(agentName)
  return stats?.schedulesTotal || 0
}

const getSchedulesEnabled = (agentName) => {
  const stats = getExecutionStats(agentName)
  return stats?.schedulesEnabled || 0
}

const hasSchedules = (agentName) => {
  return getSchedulesTotal(agentName) > 0
}

// Tags ride the fleet payload — the per-agent N+1 fetch is gone (ent#260).
function getAgentTags(agent) {
  // Org-overlay namespaces (dept-*/reports-to-*) render as zones/lines on the
  // Grid, not as browse chips here; the AgentDetail tag editor shows all
  // (trinity-enterprise#305).
  return (agent.tags || []).filter((t) => !isOrgTag(t))
}

// Autonomy toggle — networkStore.toggleAutonomy RETURNS {success, error}
// (it never throws, unlike the old agentsStore path), so the failure toast
// keys off the result object (ent#260 eng F6).
const handleAutonomyToggle = async (agent) => {
  if (autonomyLoading.value === agent.name) return
  autonomyLoading.value = agent.name
  try {
    const result = await networkStore.toggleAutonomy(agent.name)
    if (!result.success) {
      showNotification(result.error || 'Failed to toggle autonomy mode', 'error')
    }
  } finally {
    autonomyLoading.value = null
  }
}

// Read-only state rides the fleet payload (agent.read_only_enabled) — the old
// per-agent GET (which 404'd on stopped containers and was coerced to false)
// is gone. The PUT toggle updates the row object in place on success, the
// same in-place mutation pattern the networkStore toggles use.
async function handleReadOnlyToggle(agent) {
  if (readOnlyLoading.value === agent.name) return
  readOnlyLoading.value = agent.name

  const newState = !agent.read_only_enabled

  try {
    const token = localStorage.getItem('token')
    const response = await axios.put(`/api/agents/${agent.name}/read-only`, {
      enabled: newState
    }, {
      headers: { Authorization: `Bearer ${token}` }
    })

    if (response.data) {
      agent.read_only_enabled = newState
      showNotification(
        newState
          ? `Read-only mode enabled for ${agentDisplayName(agent)}`
          : `Read-only mode disabled for ${agentDisplayName(agent)}`,
        'success'
      )
    }
  } catch (error) {
    console.error('Failed to toggle read-only mode:', error)
    showNotification('Failed to toggle read-only mode', 'error')
  } finally {
    readOnlyLoading.value = null
  }
}

// Running toggle — networkStore action (mutates the row in place, keeps a
// per-agent loading map read via isTogglingRunning) + result-object toast.
// System rows never reach here (toggle hidden — grid guard, item B).
const handleRunningToggle = async (agent) => {
  if (networkStore.isTogglingRunning(agent.name) || agent.is_system) return

  const result = await networkStore.toggleAgentRunning(agent.name)
  if (result.success) {
    const action = result.status === 'running' ? 'started' : 'stopped'
    showNotification(`Agent ${agentDisplayName(agent)} ${action}`, 'success')
  } else {
    showNotification(result.error || 'Failed to toggle agent', 'error')
  }
}

function toggleSelection(agentName) {
  const index = selectedAgents.value.indexOf(agentName)
  if (index === -1) {
    selectedAgents.value.push(agentName)
  } else {
    selectedAgents.value.splice(index, 1)
  }
}

function clearSelection() {
  selectedAgents.value = []
  showBulkAddTag.value = false
  showBulkRemoveTag.value = false
}

async function applyBulkTag() {
  const tag = bulkTagInput.value.toLowerCase().trim()
  if (!tag) return

  // Validate tag format
  if (!/^[a-z0-9-]+$/.test(tag) || tag.length > 50) {
    showNotification('Invalid tag format. Use lowercase letters, numbers, and hyphens only.', 'error')
    return
  }

  try {
    await Promise.all(
      selectedAgents.value.map(agentName =>
        axios.post(`/api/agents/${agentName}/tags/${tag}`)
      )
    )
    showNotification(`Added tag "${tag}" to ${selectedAgents.value.length} agent(s)`, 'success')
    bulkTagInput.value = ''
    showBulkAddTag.value = false
    // Emit BEFORE the refresh await (ent#260 eng F15): an emit after an await
    // inside a component the user has meanwhile torn down (mode switch) is
    // dropped — and the chassis needs it to refetch /api/tags counts.
    emit('tags-changed')
    await networkStore.fetchAgents() // row tags ride the fleet payload
  } catch (err) {
    console.error('Failed to add tag:', err)
    showNotification('Failed to add tag to some agents', 'error')
  }
}

async function removeBulkTag(tag) {
  try {
    await Promise.all(
      selectedAgents.value.map(agentName =>
        axios.delete(`/api/agents/${agentName}/tags/${tag}`)
      )
    )
    showNotification(`Removed tag "${tag}" from ${selectedAgents.value.length} agent(s)`, 'success')
    showBulkRemoveTag.value = false
    // Emit-before-await: same rationale as applyBulkTag.
    emit('tags-changed')
    await networkStore.fetchAgents()
  } catch (err) {
    console.error('Failed to remove tag:', err)
    showNotification('Failed to remove tag from some agents', 'error')
  }
}
</script>

<style scoped>
/* Pulsing animation for active agents */
.active-pulse {
  animation: active-pulse-animation 0.8s ease-in-out infinite;
  box-shadow: 0 0 8px 2px rgba(16, 185, 129, 0.6);
}

@keyframes active-pulse-animation {
  0%, 100% {
    transform: scale(1);
    opacity: 1;
    box-shadow: 0 0 8px 2px rgba(16, 185, 129, 0.6);
  }
  50% {
    transform: scale(1.3);
    opacity: 0.8;
    box-shadow: 0 0 16px 4px rgba(16, 185, 129, 0.9);
  }
}

/* Custom border width for system agent accent */
.border-l-3 {
  border-left-width: 3px;
}
</style>
