<template>
  <div class="min-h-screen bg-gray-100 dark:bg-gray-900">
    <NavBar />

    <main class="max-w-7xl mx-auto py-6 sm:px-6 lg:px-8">
      <div class="px-4 sm:px-0">
        <!-- Notification Toast -->
        <div v-if="notification"
          :class="[
            'fixed top-20 right-4 z-50 px-4 py-3 rounded-lg shadow-lg transition-all duration-300',
            notification.type === 'success' ? 'bg-green-100 dark:bg-green-900/50 border border-green-400 dark:border-green-700 text-green-700 dark:text-green-300' : 'bg-red-100 dark:bg-red-900/50 border border-red-400 dark:border-red-700 text-red-700 dark:text-red-300'
          ]"
        >
          {{ notification.message }}
        </div>

        <!-- Header + Filter Bar (single row) -->
        <div class="mb-3 flex items-center gap-3 flex-wrap">
          <h1 class="text-3xl font-bold text-gray-900 dark:text-white mr-1">Agents</h1>

          <!-- Name search -->
          <div class="relative">
            <svg class="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 dark:text-gray-500 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            <input
              v-model="filterName"
              type="text"
              placeholder="Search agents..."
              class="block w-44 rounded-md border-gray-300 dark:border-gray-600 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 text-sm py-2 pl-8 pr-3 bg-white dark:bg-gray-700 dark:text-gray-200 border"
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
                  ? 'bg-indigo-600 text-white'
                  : 'bg-white dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-600'
              ]"
            >
              {{ opt.label }}
            </button>
          </div>

          <!-- Tag filter dropdown -->
          <select
            v-if="availableTags.length > 0"
            v-model="selectedFilterTagDropdown"
            class="block rounded-md border-gray-300 dark:border-gray-600 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 text-sm py-2 px-3 bg-white dark:bg-gray-700 dark:text-gray-200 border"
          >
            <option value="">All Tags</option>
            <option v-for="tagInfo in availableTags" :key="tagInfo.tag" :value="tagInfo.tag">
              #{{ tagInfo.tag }} ({{ tagInfo.count }})
            </option>
          </select>

          <!-- Owner filter dropdown -->
          <select
            v-if="availableOwners.length > 1"
            v-model="selectedOwner"
            class="block rounded-md border-gray-300 dark:border-gray-600 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 text-sm py-2 px-3 bg-white dark:bg-gray-700 dark:text-gray-200 border"
          >
            <option value="">All Owners</option>
            <option v-for="owner in availableOwners" :key="owner.name || '__unassigned__'" :value="owner.name || '__unassigned__'">
              {{ owner.name || 'Unassigned' }} ({{ owner.count }})
            </option>
          </select>

          <!-- Clear all filters -->
          <button
            v-if="hasActiveFilters"
            @click="clearAllFilters"
            class="px-2.5 py-2 text-xs font-medium text-gray-500 dark:text-gray-400 hover:text-red-600 dark:hover:text-red-400 transition-colors flex items-center gap-1"
          >
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
            </svg>
            Clear
          </button>

          <!-- Agent count -->
          <span
            v-if="hasActiveFilters"
            class="text-xs text-gray-500 dark:text-gray-400"
          >
            {{ displayAgents.length }}/{{ totalAgentCount }}
          </span>

          <!-- Right side: sort + create -->
          <div class="flex items-center gap-3 ml-auto">
            <select
              v-model="agentsStore.sortBy"
              class="block rounded-md border-gray-300 dark:border-gray-600 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 text-sm py-2 px-3 bg-white dark:bg-gray-700 dark:text-gray-200 border"
            >
              <option value="created_desc">Newest First</option>
              <option value="created_asc">Oldest First</option>
              <option value="name_asc">Name (A-Z)</option>
              <option value="name_desc">Name (Z-A)</option>
              <option value="status">Running First</option>
              <option value="success_desc">Success Rate</option>
            </select>

            <button
              @click="showCreateModal = true"
              class="bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-2 px-4 rounded whitespace-nowrap"
            >
              Create Agent
            </button>
          </div>
        </div>

        <!-- Bulk Actions Toolbar -->
        <div
          v-if="selectedAgents.length > 0"
          class="mb-4 p-3 bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-700 rounded-lg flex items-center justify-between"
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
                class="px-3 py-1.5 bg-green-100 dark:bg-green-900/50 text-green-700 dark:text-green-300 rounded text-sm font-medium hover:bg-green-200 dark:hover:bg-green-900 transition-colors"
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
                class="px-3 py-1.5 bg-red-100 dark:bg-red-900/50 text-red-700 dark:text-red-300 rounded text-sm font-medium hover:bg-red-200 dark:hover:bg-red-900 transition-colors"
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
                      class="px-2 py-0.5 rounded-full text-xs bg-red-100 dark:bg-red-900/50 text-red-700 dark:text-red-300 hover:bg-red-200 dark:hover:bg-red-900 transition-colors"
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
          <div class="hidden lg:grid lg:grid-cols-[auto_auto_1fr_46px_22rem_180px_auto_auto] lg:gap-x-4 items-center px-4 py-2 text-xs font-medium text-gray-400 dark:text-gray-500 uppercase tracking-wider">
            <div class="w-4"></div>
            <div class="w-3"></div>
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
            :class="[
              'bg-white dark:bg-gray-800 rounded-lg',
              'transition-colors duration-150 hover:bg-gray-50 dark:hover:bg-gray-750',
              agent.is_system
                ? 'border-l-3 border-l-purple-500'
                : '',
              agent.status !== 'running' && !agent.is_system
                ? 'opacity-75'
                : ''
            ]"
          >
            <!-- Desktop layout (lg+) -->
            <div class="hidden lg:flex px-4 py-3">
              <!-- Two-row content block -->
              <div class="flex flex-col flex-1 min-w-0">
              <!-- Main grid (Row 1) -->
              <div class="grid grid-cols-[auto_auto_1fr_46px_22rem_180px_auto_auto] gap-x-4 items-center">
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

                <!-- #389 Sync health dot (visible only for git-enabled agents) -->
                <div
                  v-if="syncHealthEntry(agent.name)"
                  :class="['w-2 h-2 rounded-full flex-shrink-0', syncHealthColorClass(agent.name)]"
                  :title="syncHealthLabel(agent.name)"
                ></div>

                <!-- Name + badges -->
                <div class="flex items-center min-w-0 gap-2">
                  <AgentAvatar :name="agent.name" :avatar-url="agent.avatar_url" size="sm" />
                  <router-link
                    :to="`/agents/${agent.name}`"
                    class="text-gray-900 dark:text-white font-semibold text-sm truncate hover:text-indigo-600 dark:hover:text-indigo-400"
                    :title="agent.name"
                  >
                    {{ agent.name }}
                  </router-link>
                  <span
                    v-if="agent.is_system"
                    class="px-1.5 py-0.5 text-[10px] font-semibold bg-purple-100 text-purple-700 dark:bg-purple-900/50 dark:text-purple-300 rounded flex-shrink-0"
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

                <!-- Toggles -->
                <div class="flex items-center gap-1">
                  <div class="w-[7rem] flex-shrink-0 flex justify-end">
                    <RunningStateToggle
                      :model-value="agent.status === 'running'"
                      :loading="actionInProgress === agent.name"
                      size="sm"
                      @toggle="toggleAgentRunning(agent)"
                    />
                  </div>
                  <div class="w-[7.5rem] flex-shrink-0 flex justify-end" :class="{ 'invisible': agent.is_system || agent.is_shared }">
                    <ReadOnlyToggle
                      :model-value="getAgentReadOnlyState(agent.name)"
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
                  class="text-gray-400 dark:text-gray-500 hover:text-indigo-600 dark:hover:text-indigo-400 transition-colors"
                >
                  <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                  </svg>
                </router-link>
              </div>

              <!-- Bottom row: tags (always rendered for uniform height) -->
              <div class="flex items-center gap-1 pl-[3.625rem] min-h-[1.375rem] pt-1">
                <template v-if="getAgentTags(agent.name).length > 0">
                  <span
                    v-for="tag in getAgentTags(agent.name).slice(0, 3)"
                    :key="tag"
                    class="px-1.5 py-0.5 text-[10px] rounded-full bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400 truncate max-w-20"
                    :title="'#' + tag"
                  >
                    #{{ tag }}
                  </span>
                  <span
                    v-if="getAgentTags(agent.name).length > 3"
                    class="text-[10px] text-gray-400 dark:text-gray-500 whitespace-nowrap"
                  >
                    +{{ getAgentTags(agent.name).length - 3 }}
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
            <div class="hidden md:flex md:flex-col lg:hidden px-4 py-3 gap-2">
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
                <AgentAvatar :name="agent.name" :avatar-url="agent.avatar_url" size="sm" />
                <router-link
                  :to="`/agents/${agent.name}`"
                  class="text-gray-900 dark:text-white font-semibold text-sm truncate hover:text-indigo-600 dark:hover:text-indigo-400"
                  :title="agent.name"
                >
                  {{ agent.name }}
                </router-link>
                <span
                  v-if="agent.is_system"
                  class="px-1.5 py-0.5 text-[10px] font-semibold bg-purple-100 text-purple-700 dark:bg-purple-900/50 dark:text-purple-300 rounded flex-shrink-0"
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
                    class="text-gray-400 dark:text-gray-500 hover:text-indigo-600 dark:hover:text-indigo-400 transition-colors"
                  >
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                    </svg>
                  </router-link>
                </div>
              </div>
              <div class="flex items-center gap-3 pl-[3.25rem]">
                <div class="flex items-center gap-2">
                  <RunningStateToggle
                    :model-value="agent.status === 'running'"
                    :loading="actionInProgress === agent.name"
                    size="sm"
                    @toggle="toggleAgentRunning(agent)"
                  />
                  <ReadOnlyToggle
                    v-if="!agent.is_system && !agent.is_shared"
                    :model-value="getAgentReadOnlyState(agent.name)"
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
                      <span class="font-medium text-gray-700 dark:text-gray-300">${{ getExecutionStats(agent.name).totalCost.toFixed(2) }}</span>
                    </template>
                  </template>
                  <span v-else class="text-gray-400 dark:text-gray-500">--</span>
                </div>
              </div>
              <!-- Tags row (tablet) -->
              <div v-if="getAgentTags(agent.name).length > 0" class="flex items-center gap-1 pl-[3.25rem]">
                <span
                  v-for="tag in getAgentTags(agent.name).slice(0, 3)"
                  :key="tag"
                  class="px-1.5 py-0.5 text-[10px] rounded-full bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400 truncate max-w-20"
                  :title="'#' + tag"
                >
                  #{{ tag }}
                </span>
                <span
                  v-if="getAgentTags(agent.name).length > 3"
                  class="text-[10px] text-gray-400 dark:text-gray-500 whitespace-nowrap"
                >
                  +{{ getAgentTags(agent.name).length - 3 }}
                </span>
              </div>
            </div>

            <!-- Mobile layout (< md) -->
            <div class="flex flex-col md:hidden px-4 py-3 gap-2">
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
                <AgentAvatar :name="agent.name" :avatar-url="agent.avatar_url" size="sm" />
                <router-link
                  :to="`/agents/${agent.name}`"
                  class="text-gray-900 dark:text-white font-semibold text-sm truncate hover:text-indigo-600 dark:hover:text-indigo-400 flex-1 min-w-0"
                  :title="agent.name"
                >
                  {{ agent.name }}
                </router-link>
                <span
                  v-if="agent.is_system"
                  class="px-1.5 py-0.5 text-[10px] font-semibold bg-purple-100 text-purple-700 dark:bg-purple-900/50 dark:text-purple-300 rounded flex-shrink-0"
                >
                  SYS
                </span>
                <RuntimeBadge :runtime="agent.runtime" :show-label="false" class="flex-shrink-0" />
                <div class="flex items-center gap-2 flex-shrink-0">
                  <RunningStateToggle
                    :model-value="agent.status === 'running'"
                    :loading="actionInProgress === agent.name"
                    size="sm"
                    @toggle="toggleAgentRunning(agent)"
                  />
                  <router-link
                    :to="`/agents/${agent.name}`"
                    class="text-gray-400 dark:text-gray-500 hover:text-indigo-600 dark:hover:text-indigo-400 transition-colors"
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

        <!-- Empty state -->
        <div v-if="displayAgents.length === 0" class="text-center py-12 bg-white dark:bg-gray-800 rounded-xl shadow">
          <ServerIcon class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-500" />
          <template v-if="hasActiveFilters">
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
          </template>
          <template v-else>
            <h3 class="mt-2 text-sm font-medium text-gray-900 dark:text-gray-100">No agents</h3>
            <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Get started by creating a new agent.</p>
            <div class="mt-6">
              <button
                @click="showCreateModal = true"
                class="inline-flex items-center px-4 py-2 border border-transparent shadow-sm text-sm font-medium rounded-md text-white bg-indigo-600 hover:bg-indigo-700"
              >
                Create Agent
              </button>
            </div>
          </template>
        </div>

        <!-- Create Agent Modal -->
        <CreateAgentModal v-if="showCreateModal" @close="showCreateModal = false" />
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { useNetworkStore } from '../stores/network'
import NavBar from '../components/NavBar.vue'
import CreateAgentModal from '../components/CreateAgentModal.vue'
import AgentAvatar from '../components/AgentAvatar.vue'
import RuntimeBadge from '../components/RuntimeBadge.vue'
import RunningStateToggle from '../components/RunningStateToggle.vue'
import AutonomyToggle from '../components/AutonomyToggle.vue'
import ReadOnlyToggle from '../components/ReadOnlyToggle.vue'
import CapacityMeter from '../components/CapacityMeter.vue'
import { ServerIcon } from '@heroicons/vue/24/outline'
import axios from 'axios'
import { syncHealthColor, syncHealthLabel as syncHealthLabelFn } from '../utils/syncHealth'

const agentsStore = useAgentsStore()
const networkStore = useNetworkStore()
const showCreateModal = ref(false)
const notification = ref(null)
const actionInProgress = ref(null)
const autonomyLoading = ref(null)
const readOnlyLoading = ref(null)
const agentReadOnlyStates = ref({}) // Map of agent_name -> boolean
const isAdmin = ref(false)

// Filter state
const filterName = ref(localStorage.getItem('trinity-agents-filter-name') || '')
const filterStatus = ref(localStorage.getItem('trinity-agents-filter-status') || 'all')
const statusOptions = [
  { value: 'all', label: 'All' },
  { value: 'running', label: 'Running' },
  { value: 'stopped', label: 'Stopped' }
]

// Tag-related state
const availableTags = ref([])
const agentTags = ref({}) // Map of agent_name -> tags[]
// Migrate old formats to single-tag dropdown
const legacyTag = localStorage.getItem('trinity-agents-filter-tag')
const legacyTags = localStorage.getItem('trinity-agents-filter-tags')
const initialFilterTag = legacyTag || (legacyTags ? JSON.parse(legacyTags)[0] || '' : '') || localStorage.getItem('trinity-agents-filter-tag-dropdown') || ''
if (legacyTag) localStorage.removeItem('trinity-agents-filter-tag')
if (legacyTags) localStorage.removeItem('trinity-agents-filter-tags')
const selectedFilterTagDropdown = ref(initialFilterTag)
const selectedOwner = ref(localStorage.getItem('trinity-agents-filter-owner') || '')
const selectedAgents = ref([])
const showBulkAddTag = ref(false)
const showBulkRemoveTag = ref(false)
const bulkTagInput = ref('')

// Persist filter state across reloads
watch(filterName, (val) => {
  if (val) {
    localStorage.setItem('trinity-agents-filter-name', val)
  } else {
    localStorage.removeItem('trinity-agents-filter-name')
  }
})

watch(filterStatus, (val) => {
  if (val && val !== 'all') {
    localStorage.setItem('trinity-agents-filter-status', val)
  } else {
    localStorage.removeItem('trinity-agents-filter-status')
  }
})

watch(selectedFilterTagDropdown, (val) => {
  if (val) {
    localStorage.setItem('trinity-agents-filter-tag-dropdown', val)
  } else {
    localStorage.removeItem('trinity-agents-filter-tag-dropdown')
  }
})

watch(selectedOwner, (val) => {
  if (val) {
    localStorage.setItem('trinity-agents-filter-owner', val)
  } else {
    localStorage.removeItem('trinity-agents-filter-owner')
  }
})

// Derive distinct owners from the full (unfiltered) agent list
const availableOwners = computed(() => {
  const agents = isAdmin.value ? agentsStore.sortedAgentsWithSystem : agentsStore.sortedAgents
  const counts = {}
  for (const agent of agents) {
    const owner = agent.owner || null
    counts[owner] = (counts[owner] || 0) + 1
  }
  return Object.entries(counts)
    .map(([name, count]) => ({ name: name === 'null' ? null : name, count }))
    .sort((a, b) => {
      if (a.name === null) return 1
      if (b.name === null) return -1
      return a.name.localeCompare(b.name)
    })
})

const hasActiveFilters = computed(() => {
  return filterName.value.trim() !== '' || filterStatus.value !== 'all' || selectedFilterTagDropdown.value !== '' || selectedOwner.value !== ''
})

function clearAllFilters() {
  filterName.value = ''
  filterStatus.value = 'all'
  selectedFilterTagDropdown.value = ''
  selectedOwner.value = ''
}

// Total agent count before filtering (for "Showing X of Y")
const totalAgentCount = computed(() => {
  const agents = isAdmin.value ? agentsStore.sortedAgentsWithSystem : agentsStore.sortedAgents
  return agents.length
})

// Computed to show system agent for admins, with combined filtering (AND logic)
const displayAgents = computed(() => {
  let agents = isAdmin.value ? agentsStore.sortedAgentsWithSystem : agentsStore.sortedAgents

  // Filter by name
  const nameQuery = filterName.value.trim().toLowerCase()
  if (nameQuery) {
    agents = agents.filter(agent => agent.name.toLowerCase().includes(nameQuery))
  }

  // Filter by status
  if (filterStatus.value === 'running') {
    agents = agents.filter(agent => agent.status === 'running')
  } else if (filterStatus.value === 'stopped') {
    agents = agents.filter(agent => agent.status !== 'running')
  }

  // Filter by tag (single tag dropdown)
  if (selectedFilterTagDropdown.value) {
    agents = agents.filter(agent => {
      const tags = agentTags.value[agent.name] || []
      return tags.includes(selectedFilterTagDropdown.value)
    })
  }

  // Filter by owner
  if (selectedOwner.value) {
    if (selectedOwner.value === '__unassigned__') {
      agents = agents.filter(agent => !agent.owner)
    } else {
      agents = agents.filter(agent => agent.owner === selectedOwner.value)
    }
  }

  return agents
})

// Get common tags across all selected agents (for removal)
const commonTagsInSelection = computed(() => {
  if (selectedAgents.value.length === 0) return []
  const allTags = new Set()
  selectedAgents.value.forEach(agentName => {
    const tags = agentTags.value[agentName] || []
    tags.forEach(tag => allTags.add(tag))
  })
  return Array.from(allTags).sort()
})

const showNotification = (message, type = 'success') => {
  notification.value = { message, type }
  setTimeout(() => {
    notification.value = null
  }, 3000)
}

onMounted(async () => {
  // PERF-269: Parallelize independent mount calls
  const fetchUserRole = async () => {
    try {
      const token = localStorage.getItem('token')
      if (token) {
        const response = await axios.get('/api/users/me', {
          headers: { Authorization: `Bearer ${token}` }
        })
        isAdmin.value = response.data.role === 'admin'
      }
    } catch (e) {
      console.warn('Failed to fetch user role:', e)
      isAdmin.value = false
    }
  }

  await Promise.allSettled([
    agentsStore.fetchAgents(),
    agentsStore.fetchSyncHealth(),  // #389
    fetchUserRole(),
    fetchAvailableTags(),
    fetchAllAgentTags(),
    fetchAllReadOnlyStates()
  ])

  networkStore.startContextPolling()
})

onUnmounted(() => {
  networkStore.stopContextPolling()
})

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
  if (state === 'Active' || state === 'Idle') return 'text-green-600 dark:text-green-400'
  return 'text-gray-500 dark:text-gray-400'
}

// Success rate bar helpers
const getSuccessBarPercent = (agentName) => {
  const stats = networkStore.executionStats[agentName]
  return stats ? Math.round(stats.successRate || 0) : 0
}

const getSuccessBarColor = (agentName) => {
  const percent = getSuccessBarPercent(agentName)
  if (percent >= 90) return 'bg-green-500'
  if (percent >= 50) return 'bg-yellow-500'
  return 'bg-red-500'
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
  if (percent >= 90) return 'bg-green-500'
  if (percent >= 50) return 'bg-yellow-500'
  return 'bg-red-500'
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

const getSuccessRateColorClass = (agentName) => {
  const stats = getExecutionStats(agentName)
  if (!stats) return 'text-gray-500 dark:text-gray-400'
  const rate = stats.successRate
  if (rate >= 80) return 'text-green-600 dark:text-green-400'
  if (rate >= 50) return 'text-yellow-600 dark:text-yellow-400'
  return 'text-red-600 dark:text-red-400'
}

const getLastExecutionDisplay = (agentName) => {
  const stats = getExecutionStats(agentName)
  if (!stats?.lastExecutionAt) return null
  const lastTime = new Date(stats.lastExecutionAt)
  const now = new Date()
  const diffMs = now - lastTime
  const diffMins = Math.floor(diffMs / 60000)
  const diffHours = Math.floor(diffMs / 3600000)

  if (diffMins < 1) return 'just now'
  if (diffMins < 60) return `${diffMins}m ago`
  if (diffHours < 24) return `${diffHours}h ago`
  return `${Math.floor(diffHours / 24)}d ago`
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

// Autonomy toggle handler
const handleAutonomyToggle = async (agent) => {
  if (autonomyLoading.value === agent.name) return
  autonomyLoading.value = agent.name
  try {
    await agentsStore.toggleAutonomy(agent.name)
  } catch (error) {
    showNotification('Failed to toggle autonomy mode', 'error')
  } finally {
    autonomyLoading.value = null
  }
}

// Read-only mode functions
function getAgentReadOnlyState(agentName) {
  return agentReadOnlyStates.value[agentName] || false
}

async function fetchAllReadOnlyStates() {
  const agents = isAdmin.value ? agentsStore.sortedAgentsWithSystem : agentsStore.sortedAgents
  const states = {}

  await Promise.all(
    agents.filter(a => !a.is_system && !a.is_shared).map(async (agent) => {
      try {
        const token = localStorage.getItem('token')
        const response = await axios.get(`/api/agents/${agent.name}/read-only`, {
          headers: { Authorization: `Bearer ${token}` }
        })
        states[agent.name] = response.data.enabled || false
      } catch (err) {
        states[agent.name] = false
      }
    })
  )

  agentReadOnlyStates.value = states
}

async function handleReadOnlyToggle(agent) {
  if (readOnlyLoading.value === agent.name) return
  readOnlyLoading.value = agent.name

  const newState = !getAgentReadOnlyState(agent.name)

  try {
    const token = localStorage.getItem('token')
    const response = await axios.put(`/api/agents/${agent.name}/read-only`, {
      enabled: newState
    }, {
      headers: { Authorization: `Bearer ${token}` }
    })

    if (response.data) {
      agentReadOnlyStates.value[agent.name] = newState
      showNotification(
        newState
          ? `Read-only mode enabled for ${agent.name}`
          : `Read-only mode disabled for ${agent.name}`,
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

const toggleAgentRunning = async (agent) => {
  if (actionInProgress.value === agent.name) return
  actionInProgress.value = agent.name

  try {
    const result = await agentsStore.toggleAgentRunning(agent.name)
    if (result.success) {
      const action = result.status === 'running' ? 'started' : 'stopped'
      showNotification(`Agent ${agent.name} ${action}`, 'success')
    } else {
      showNotification(result.error || 'Failed to toggle agent', 'error')
    }
  } catch (error) {
    showNotification(error.message || 'Failed to toggle agent', 'error')
  } finally {
    actionInProgress.value = null
  }
}

// Tag-related functions
async function fetchAvailableTags() {
  try {
    const response = await axios.get('/api/tags')
    availableTags.value = response.data.tags || []
  } catch (err) {
    console.error('Failed to fetch tags:', err)
    availableTags.value = []
  }
}

async function fetchAllAgentTags() {
  // Fetch tags for all agents
  const agents = isAdmin.value ? agentsStore.sortedAgentsWithSystem : agentsStore.sortedAgents
  const tagsMap = {}

  await Promise.all(
    agents.map(async (agent) => {
      try {
        const response = await axios.get(`/api/agents/${agent.name}/tags`)
        tagsMap[agent.name] = response.data.tags || []
      } catch (err) {
        tagsMap[agent.name] = []
      }
    })
  )

  agentTags.value = tagsMap
}

function getAgentTags(agentName) {
  return agentTags.value[agentName] || []
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
    await fetchAllAgentTags()
    await fetchAvailableTags()
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
    await fetchAllAgentTags()
    await fetchAvailableTags()
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

/* Dark hover shade between gray-700 and gray-800 */
@media (prefers-color-scheme: dark) {
  .dark\:hover\:bg-gray-750:hover {
    background-color: rgb(42, 48, 60);
  }
}
</style>
