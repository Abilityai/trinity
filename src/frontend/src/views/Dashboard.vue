<template>
  <div class="h-screen flex flex-col bg-gray-100 dark:bg-gray-900 overflow-hidden">
    <NavBar />

    <main class="flex-1 flex overflow-hidden">
      <!-- System Views Sidebar -->
      <SystemViewsSidebar
        @create="openCreateModal"
        @edit="openEditModal"
      />

      <div class="flex flex-col flex-1 overflow-hidden">
        <!-- Compact Header -->
        <div class="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 px-4 py-2">
          <div class="flex items-center justify-between">
            <!-- Left: Stats -->
            <div class="flex items-center min-w-0 overflow-hidden">
              <div class="flex items-center space-x-3 text-xs text-gray-500 dark:text-gray-400 whitespace-nowrap">
                <span class="flex items-center space-x-1">
                  <span class="w-1.5 h-1.5 rounded-full bg-status-success-500"></span>
                  <span class="font-medium text-status-success-600 dark:text-status-success-400">{{ runningCount }}/{{ agents.length }}</span>
                  <span>agents</span>
                </span>
                <!-- Working-now count (trinity-enterprise#47) -->
                <span class="text-gray-300 dark:text-gray-600">·</span>
                <span class="flex items-center space-x-1">
                  <span class="font-medium text-status-info-600 dark:text-status-info-400">{{ workingNowCount }}</span>
                  <span>working now</span>
                </span>
                <span class="text-gray-300 dark:text-gray-600">·</span>
                <span class="flex items-center space-x-1">
                  <span class="font-medium text-status-info-600 dark:text-status-info-400">{{ totalCollaborationCount }}</span>
                  <span>messages ({{ timeRangeHours }}h)</span>
                </span>
                <!-- Host Telemetry (inline) -->
                <span class="text-gray-300 dark:text-gray-600">·</span>
                <HostTelemetry />
              </div>
            </div>

            <!-- Right: Controls -->
            <div class="flex items-center space-x-2 flex-shrink-0">
              <!-- Quick Tag Filter Dropdown -->
              <div v-if="availableTags.length > 0" class="relative">
                <button
                  @click="showTagDropdown = !showTagDropdown"
                  :class="[
                    'flex items-center space-x-1 px-2 py-0.5 rounded text-xs font-medium transition-all whitespace-nowrap',
                    selectedQuickTags.length > 0
                      ? 'bg-blue-600 text-white'
                      : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
                  ]"
                >
                  <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A2 2 0 013 12V7a4 4 0 014-4z"/>
                  </svg>
                  <span class="whitespace-nowrap">{{ selectedQuickTags.length > 0 ? selectedQuickTags.length + ' tag' + (selectedQuickTags.length > 1 ? 's' : '') : 'Tags' }}</span>
                  <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                  </svg>
                </button>
                <div
                  v-if="showTagDropdown"
                  class="absolute right-0 top-full mt-1 bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-gray-200 dark:border-gray-700 py-1 z-50 max-h-48 overflow-y-auto min-w-36"
                >
                  <button
                    v-if="selectedQuickTags.length > 0"
                    @click="clearQuickTags(); showTagDropdown = false"
                    class="w-full px-3 py-1.5 text-left text-xs text-status-danger-600 dark:text-status-danger-400 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors border-b border-gray-200 dark:border-gray-700"
                  >
                    Clear all
                  </button>
                  <button
                    v-for="tagInfo in availableTags"
                    :key="tagInfo.tag"
                    @click="toggleQuickTag(tagInfo.tag)"
                    :class="[
                      'w-full px-3 py-1.5 text-left text-xs hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors flex items-center justify-between',
                      selectedQuickTags.includes(tagInfo.tag) ? 'text-blue-600 dark:text-blue-400 font-medium' : 'text-gray-700 dark:text-gray-300'
                    ]"
                  >
                    <span>#{{ tagInfo.tag }}</span>
                    <span class="text-gray-400 dark:text-gray-500 text-[10px]">{{ tagInfo.count }}</span>
                  </button>
                </div>
              </div>

              <!-- Tag Clouds Toggle -->
              <button
                v-if="availableTags.length > 0"
                @click="toggleTagClouds"
                :class="[
                  'flex items-center space-x-1 px-2 py-0.5 rounded text-xs font-medium transition-all',
                  showTagClouds
                    ? 'bg-accent-purple-600 text-white'
                    : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
                ]"
                title="Toggle tag grouping clouds"
              >
                <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 15a4 4 0 004 4h9a5 5 0 10-.1-9.999 5.002 5.002 0 10-9.78 2.096A4.001 4.001 0 003 15z"/>
                </svg>
                <span>Clouds</span>
              </button>

              <!-- Owner filter dropdown -->
              <select
                v-if="availableOwners.length > 1"
                v-model="selectedOwner"
                @change="onOwnerFilterChange"
                class="text-xs border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-700 dark:text-gray-200 focus:outline-none focus:ring-1 focus:ring-blue-500"
              >
                <option value="">All Owners</option>
                <option v-for="owner in availableOwners" :key="owner.name || '__unassigned__'" :value="owner.name || '__unassigned__'">
                  {{ owner.name || 'Unassigned' }} ({{ owner.count }})
                </option>
              </select>

              <span v-if="availableTags.length > 0 || availableOwners.length > 1" class="text-gray-300 dark:text-gray-600">|</span>

              <!-- Mode Toggle (Grid / Graph / Timeline — trinity-enterprise#47) -->
              <div class="flex rounded-md border border-gray-300 dark:border-gray-600 p-0.5 bg-gray-50 dark:bg-gray-700">
                <button
                  v-for="mode in ['grid', 'graph', 'timeline']"
                  :key="mode"
                  @click="toggleMode(mode)"
                  :class="[
                    'px-2 py-1 rounded text-xs font-medium transition-all capitalize',
                    viewMode === mode ? 'bg-blue-600 text-white' : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'
                  ]"
                >
                  {{ mode }}
                </button>
              </div>

              <!-- Grid-mode controls (trinity-enterprise#47) -->
              <template v-if="viewMode === 'grid'">
                <button
                  @click="fleetGridRef?.tidyUp()"
                  class="text-xs border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-gray-100 transition-colors"
                  title="Compact tiles row-by-row, preserving reading order"
                >
                  Tidy up
                </button>
                <button
                  @click="fleetGridRef?.resetToDefault()"
                  class="text-xs border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-gray-100 transition-colors"
                  title="Restore the default tile layout"
                >
                  Reset
                </button>
              </template>

              <!-- Time Range -->
              <select
                v-model="selectedTimeRange"
                @change="onTimeRangeChange"
                class="text-xs border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-700 dark:text-gray-200 focus:outline-none focus:ring-1 focus:ring-blue-500"
              >
                <option :value="1">1h</option>
                <option :value="6">6h</option>
                <option :value="24">24h</option>
                <option :value="72">3d</option>
                <option :value="168">7d</option>
              </select>

              <!-- Connection indicator -->
              <div
                :class="[
                  'w-2 h-2 rounded-full',
                  isConnected ? 'bg-status-success-500' : 'bg-status-danger-500'
                ]"
                :title="isConnected ? 'Connected' : 'Disconnected'"
              ></div>

              <!-- Loading -->
              <svg v-if="isLoadingHistory" class="animate-spin h-4 w-4 text-blue-600 dark:text-blue-400" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
              </svg>

              <!-- Refresh -->
              <button
                @click="refreshAll"
                :disabled="isLoadingHistory"
                class="p-1.5 text-gray-500 dark:text-gray-400 hover:text-blue-600 dark:hover:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/30 rounded transition-colors disabled:opacity-50"
                title="Refresh"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
              </button>

              <!-- Reset Layout (graph mode only — grid has its own Reset pill) -->
              <button
                v-if="viewMode === 'graph'"
                @click="resetLayout"
                class="p-1.5 text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700 rounded transition-colors"
                title="Reset Layout"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z" />
                </svg>
              </button>
            </div>
          </div>
        </div>

    <!-- Timeline View (only visible in timeline mode) -->
    <template v-if="isTimelineMode">
      <!-- Loading skeleton (#1266): immediate feedback while fleet/timeline data loads -->
      <div
        v-if="isFleetLoading && agents.length === 0"
        class="flex-1 min-h-0 overflow-hidden bg-white dark:bg-gray-800 px-4 py-4"
      >
        <SkeletonLoader variant="rows" :count="8" height="2.5rem" gap="0.5rem" />
      </div>
      <!-- Error state (#1266): distinct from an empty timeline / infinite skeleton -->
      <div
        v-else-if="fleetLoadError && agents.length === 0"
        class="flex-1 min-h-0 flex flex-col items-center justify-center text-center px-4"
      >
        <p class="text-sm text-gray-600 dark:text-gray-300">Couldn't load timeline data.</p>
        <button
          @click="refreshAll"
          class="mt-2 text-sm font-medium text-blue-600 dark:text-blue-400 hover:underline"
        >Retry</button>
      </div>
      <ReplayTimeline
        v-else
        :agents="agents"
        :nodes="nodes"
        :events="historicalCollaborations"
        :timeline-start="timelineStart"
      :timeline-end="timelineEnd"
      :current-event-index="currentEventIndex"
      :total-events="totalEvents"
      :total-duration="totalDuration"
      :replay-elapsed-ms="replayElapsedMs"
      :replay-speed="replaySpeed"
      :is-playing="isPlaying"
      :context-stats="contextStats"
      :execution-stats="executionStats"
      :is-live-mode="true"
      :time-range-hours="selectedTimeRange"
      :schedules="schedules"
      :slot-stats="slotStats"
      @play="handlePlay"
      @pause="handlePause"
      @stop="handleStop"
      @speed-change="handleSpeedChange"
      @toggle-autonomy="handleToggleAutonomy"
      />
    </template>

    <!-- Grid View (trinity-enterprise#47) — magnetic tile canvas. v-if so its
         polling and timers tear down whenever the mode is not active. -->
    <div v-if="viewMode === 'grid'" class="relative bg-white dark:bg-gray-800 shadow-sm dark:shadow-gray-900 flex-1 min-h-0">
      <!-- Loading skeleton: immediate feedback while the fleet list loads -->
      <div
        v-if="isFleetLoading && agents.length === 0"
        class="absolute inset-0 flex items-center justify-center"
      >
        <SkeletonLoader variant="nodes" :count="5" />
      </div>
      <!-- Error state -->
      <div
        v-else-if="fleetLoadError && agents.length === 0"
        class="absolute inset-0 flex items-center justify-center"
      >
        <div class="text-center">
          <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">Couldn't load agents</h3>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Something went wrong fetching the fleet.</p>
          <button
            @click="refreshAll"
            class="mt-4 inline-flex items-center px-4 py-2 border border-transparent shadow-sm text-sm font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700"
          >Retry</button>
        </div>
      </div>
      <!-- Empty state -->
      <div
        v-else-if="gridAgents.length === 0"
        class="absolute inset-0 flex items-center justify-center"
      >
        <div class="text-center">
          <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">No agents yet</h3>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Launch your first agent in a couple of clicks.</p>
          <button
            @click="openOnboarding"
            class="mt-4 inline-flex items-center px-4 py-2 border border-transparent shadow-sm text-sm font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700"
          >
            Get started
          </button>
        </div>
      </div>
      <FleetGrid v-else ref="fleetGridRef" :agents="gridAgents" />
    </div>

    <!-- Graph Canvas - Full Height (expands to fill remaining space) - Hidden in Timeline/Grid mode -->
    <div v-if="viewMode === 'graph'" class="relative bg-white dark:bg-gray-800 shadow-sm dark:shadow-gray-900 flex-1 min-h-0">
      <!-- Loading skeleton (#1266): graph node placeholders instead of the "No agents" empty state -->
      <div
        v-if="isFleetLoading && nodes.length === 0"
        class="absolute inset-0 flex items-center justify-center"
      >
        <SkeletonLoader variant="nodes" :count="5" />
      </div>
      <!-- Error state (#1266): distinct from loading and from a genuinely empty fleet -->
      <div
        v-else-if="fleetLoadError && nodes.length === 0"
        class="absolute inset-0 flex items-center justify-center"
      >
        <div class="text-center">
          <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">Couldn't load agents</h3>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Something went wrong fetching the fleet.</p>
          <button
            @click="refreshAll"
            class="mt-4 inline-flex items-center px-4 py-2 border border-transparent shadow-sm text-sm font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700"
          >Retry</button>
        </div>
      </div>
      <!-- Empty state -->
      <div
        v-else-if="nodes.length === 0"
        class="absolute inset-0 flex items-center justify-center"
      >
        <div class="text-center">
          <svg class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
          </svg>
          <h3 class="mt-2 text-sm font-medium text-gray-900 dark:text-gray-100">No agents yet</h3>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Launch your first agent in a couple of clicks.</p>
          <div class="mt-4 flex items-center justify-center gap-3">
            <button
              @click="openOnboarding"
              class="inline-flex items-center px-4 py-2 border border-transparent shadow-sm text-sm font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700"
            >
              <svg class="-ml-1 mr-2 h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
              </svg>
              Get started
            </button>
            <router-link
              to="/agents"
              class="text-sm font-medium text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
            >
              Browse manually →
            </router-link>
          </div>
        </div>
      </div>

      <!-- Vue Flow -->
      <VueFlow
        v-else
        v-model:nodes="nodes"
        v-model:edges="edges"
        :default-zoom="0.8"
        :min-zoom="0.1"
        :max-zoom="2"
        :fit-view-on-init="true"
        @node-drag-stop="onNodeDragStop"
        class="bg-gradient-to-br from-slate-50 to-slate-100 dark:from-gray-800 dark:to-gray-900"
      >
        <!-- Custom node templates -->
        <template #node-agent="nodeProps">
          <AgentNode v-bind="nodeProps" />
        </template>
        <template #node-system-agent="nodeProps">
          <SystemAgentNode v-bind="nodeProps" />
        </template>

        <!-- SVG Definitions for gradients -->
        <svg style="position: absolute; width: 0; height: 0;">
          <defs>
            <!-- Collaboration edge gradient (cyan to purple) -->
            <linearGradient id="collaboration-gradient" x1="0%" y1="0%" x2="100%" y2="0%">
              <stop offset="0%" style="stop-color:#06b6d4;stop-opacity:1" />
              <stop offset="50%" style="stop-color:#3b82f6;stop-opacity:1" />
              <stop offset="100%" style="stop-color:#8b5cf6;stop-opacity:1" />
            </linearGradient>

            <!-- Glow filter for active edges -->
            <filter id="edge-glow">
              <feGaussianBlur stdDeviation="3" result="coloredBlur"/>
              <feMerge>
                <feMergeNode in="coloredBlur"/>
                <feMergeNode in="SourceGraphic"/>
              </feMerge>
            </filter>
          </defs>
        </svg>

        <!-- Tag Clouds Layer (rendered in viewport coordinates) -->
        <div
          v-if="showTagClouds && nodes.length > 0"
          class="vue-flow__tag-clouds"
          :style="{
            position: 'absolute',
            top: 0,
            left: 0,
            transformOrigin: '0 0',
            transform: `translate(${viewport.x}px, ${viewport.y}px) scale(${viewport.zoom})`,
            pointerEvents: 'none',
            zIndex: 0
          }"
        >
          <TagClouds
            :nodes="nodes"
            :padding="50"
            :cloud-opacity="0.12"
            :blur-amount="25"
            :cloud-border-radius="40"
            :show-labels="true"
            :node-height="260"
          />
        </div>

        <!-- Background -->
        <Background
          pattern-color="#cbd5e1"
          :gap="20"
          :size="1.5"
          variant="dots"
        />

        <!-- Controls -->
        <Controls
          :show-zoom="true"
          :show-fit-view="true"
          :show-interactive="false"
        />

        <!-- Minimap -->
        <MiniMap
          :node-color="getNodeColor"
          :node-stroke-color="() => '#fff'"
          :node-stroke-width="2"
          :mask-color="'rgba(241, 245, 249, 0.7)'"
          pannable
          zoomable
        />
      </VueFlow>

      <!-- Collaboration History Toggle Button (always visible if there's data) -->
      <button
        v-if="historicalCollaborations.length > 0"
        @click="isHistoryPanelOpen = !isHistoryPanelOpen"
        class="absolute bottom-4 right-4 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 rounded-full shadow-lg border border-gray-200 dark:border-gray-600 p-3 transition-all duration-200"
        :class="{ 'right-96': isHistoryPanelOpen }"
      >
        <svg v-if="!isHistoryPanelOpen" class="w-5 h-5 text-gray-600 dark:text-gray-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
        </svg>
        <svg v-else class="w-5 h-5 text-gray-600 dark:text-gray-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>

      <!-- Collaboration History Panel (collapsible) -->
      <div
        v-if="historicalCollaborations.length > 0 && isHistoryPanelOpen"
        class="absolute bottom-4 right-16 bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-gray-200 dark:border-gray-700 p-4 max-w-sm max-h-80 overflow-y-auto transition-all duration-300"
      >
        <div class="flex items-center justify-between mb-3">
          <h3 class="text-sm font-semibold text-gray-900 dark:text-gray-100">Message History</h3>
          <span class="text-xs bg-status-info-100 dark:bg-status-info-900 text-status-info-800 dark:text-status-info-200 px-2 py-1 rounded-full font-medium">
            {{ totalCollaborationCount }} total
          </span>
        </div>

        <!-- Real-time feed (last 10) -->
        <div v-if="collaborationHistory.length > 0" class="mb-3 pb-3 border-b border-gray-200 dark:border-gray-700">
          <div class="text-xs font-semibold text-gray-500 dark:text-gray-400 mb-2 flex items-center">
            <svg class="w-3 h-3 mr-1 text-status-success-500 animate-pulse" fill="currentColor" viewBox="0 0 20 20">
              <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
            </svg>
            Live Feed
          </div>
          <div class="space-y-2">
            <div
              v-for="(event, index) in collaborationHistory.slice(0, 5)"
              :key="'live-' + index"
              class="text-xs text-gray-600 dark:text-gray-400 flex items-center space-x-2 animate-fade-in"
            >
              <svg class="w-3 h-3 text-status-info-500 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M12.293 5.293a1 1 0 011.414 0l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414-1.414L14.586 11H3a1 1 0 110-2h11.586l-2.293-2.293a1 1 0 010-1.414z" clip-rule="evenodd" />
              </svg>
              <span class="truncate flex-1">
                <span class="font-medium">{{ event.source_agent }}</span>
                →
                <span class="font-medium">{{ event.target_agent }}</span>
              </span>
              <span class="text-gray-400 dark:text-gray-500 flex-shrink-0">{{ formatTime(event.timestamp) }}</span>
            </div>
          </div>
        </div>

        <!-- Historical data -->
        <div>
          <div class="text-xs font-semibold text-gray-500 dark:text-gray-400 mb-2">
            Last {{ timeRangeHours }}h
          </div>
          <div class="space-y-2">
            <div
              v-for="(event, index) in historicalCollaborations.slice(0, 15)"
              :key="'hist-' + index"
              class="text-xs text-gray-600 dark:text-gray-400 flex items-center space-x-2"
            >
              <svg class="w-3 h-3 text-gray-400 dark:text-gray-500 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M12.293 5.293a1 1 0 011.414 0l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414-1.414L14.586 11H3a1 1 0 110-2h11.586l-2.293-2.293a1 1 0 010-1.414z" clip-rule="evenodd" />
              </svg>
              <span class="truncate flex-1">
                <span class="font-medium">{{ event.source_agent }}</span>
                →
                <span class="font-medium">{{ event.target_agent }}</span>
              </span>
              <span class="text-gray-400 dark:text-gray-500 flex-shrink-0">{{ formatTime(event.timestamp) }}</span>
            </div>
          </div>
        </div>
      </div>

    </div>
      </div>
    </main>

    <!-- System View Editor Modal -->
    <SystemViewEditor
      :is-open="isEditorOpen"
      :editing-view="editingView"
      @close="closeEditor"
      @saved="onViewSaved"
    />

    <!-- First-run onboarding wizard (trinity-enterprise#52) -->
    <OnboardingWizard
      v-if="showOnboarding"
      :claude-auth-configured="sessionsStore.claudeAuthConfigured"
      @close="closeOnboarding"
      @deployed="onAgentDeployed"
    />
  </div>
</template>

<script setup>
import NavBar from '@/components/NavBar.vue'
import HostTelemetry from '@/components/HostTelemetry.vue'
import ReplayTimeline from '@/components/ReplayTimeline.vue'
import SkeletonLoader from '@/components/SkeletonLoader.vue'
import SystemViewsSidebar from '@/components/SystemViewsSidebar.vue'
import SystemViewEditor from '@/components/SystemViewEditor.vue'
import OnboardingWizard from '@/components/OnboardingWizard.vue'
import { useSessionsStore } from '@/stores/sessions'
import axios from 'axios'
import { ref, onMounted, onUnmounted, computed, watch } from 'vue'
import { useRoute } from 'vue-router'
import { VueFlow, useVueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import { useNetworkStore } from '@/stores/network'
import { useSystemViewsStore } from '@/stores/systemViews'
import { storeToRefs } from 'pinia'
import AgentNode from '@/components/AgentNode.vue'
import SystemAgentNode from '@/components/SystemAgentNode.vue'
import FleetGrid from '@/components/FleetGrid.vue'
import TagClouds from '@/components/TagClouds.vue'
import { useNotification } from '@/composables/useNotification'

// Import Vue Flow styles
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'
import '@vue-flow/minimap/dist/style.css'

const networkStore = useNetworkStore()
const systemViewsStore = useSystemViewsStore()
const sessionsStore = useSessionsStore()
const route = useRoute()

// First-run onboarding (trinity-enterprise#52). Auto-opens once for a fresh
// install with zero agents; dismissal is remembered so it never nags.
const ONBOARDING_DISMISSED_KEY = 'trinity_onboarding_dismissed_v1'
const showOnboarding = ref(false)
function openOnboarding() {
  showOnboarding.value = true
}
function closeOnboarding() {
  showOnboarding.value = false
  try { localStorage.setItem(ONBOARDING_DISMISSED_KEY, '1') } catch { /* ignore */ }
}
function onAgentDeployed() {
  // Agent was created via the wizard's real create modal. Keep the wizard open
  // (it advances to the credential step on its own) and refresh the fleet so
  // the new agent appears on the graph without a manual page reload — the
  // WebSocket agent_created event can lag while the container spins up.
  networkStore.fetchAgents()
}
function maybeAutoOpenOnboarding() {
  if (showOnboarding.value) return
  // Explicit ?onboarding=1 re-opens the wizard any time (re-run / QA preview),
  // regardless of fleet size or prior dismissal.
  if (route.query.onboarding === '1') {
    showOnboarding.value = true
    return
  }
  if (isFleetLoading.value) return
  // Count only user-created agents — `trinity-system` exists on every install,
  // so counting it would mean a fresh fleet is never "empty" and auto-open
  // would never fire.
  if (agents.value.filter(a => !a.is_system).length > 0) return
  if (localStorage.getItem(ONBOARDING_DISMISSED_KEY) === '1') return
  showOnboarding.value = true
}

// System View Editor Modal State
const isEditorOpen = ref(false)
const editingView = ref(null)

function openCreateModal() {
  editingView.value = null
  isEditorOpen.value = true
}

function openEditModal(view) {
  editingView.value = view
  isEditorOpen.value = true
}

function closeEditor() {
  isEditorOpen.value = false
  editingView.value = null
}

async function onViewSaved() {
  // Refresh views after save
  await systemViewsStore.fetchViews()
}

const {
  agents,
  nodes,
  edges,
  collaborationHistory,
  isConnected,
  activeCollaborationCount,
  lastEventTimeFormatted,
  historicalCollaborations,
  totalCollaborationCount,
  timeRangeHours,
  isLoadingHistory,
  loading: isFleetLoading,
  loadError: fleetLoadError,
  contextStats,
  executionStats,
  slotStats,
  workingState,
  schedules,
  // Timeline/Replay state
  viewMode,
  isTimelineMode,
  isPlaying,
  replaySpeed,
  currentEventIndex,
  replayElapsedMs,
  totalEvents,
  totalDuration,
  playbackPosition,
  timelineStart,
  timelineEnd,
  currentTime
} = storeToRefs(networkStore)

// Persisted state: Time range (default 24h, persisted to localStorage)
const savedTimeRange = localStorage.getItem('trinity-dashboard-time-range')
const selectedTimeRange = ref(savedTimeRange ? parseInt(savedTimeRange) : 24)

const isHistoryPanelOpen = ref(false) // History panel starts closed

// Quick Tag Filter state (persisted to localStorage when not using System View)
const availableTags = ref([])
const savedQuickTags = localStorage.getItem('trinity-dashboard-quick-tags')
const selectedQuickTags = ref(savedQuickTags ? JSON.parse(savedQuickTags) : [])
const showTagDropdown = ref(false)

// Owner filter state (persisted to localStorage, synced with network store)
const selectedOwner = ref(networkStore.filterOwner || '')

// Derive distinct owners from the full (unfiltered) agent list
const availableOwners = computed(() => {
  const allAgents = agents.value
  const counts = {}
  for (const agent of allAgents) {
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

function onOwnerFilterChange() {
  networkStore.setFilterOwner(selectedOwner.value)
}

// Computed: First 5 tags for inline display
const displayedTags = computed(() => availableTags.value.slice(0, 5))

// Watch for active view changes and update network filter
const { activeFilterTags, activeViewId } = storeToRefs(systemViewsStore)
watch(activeFilterTags, (tags) => {
  networkStore.setFilterTags(tags)
  // Sync quick tags with system view selection
  if (activeViewId.value) {
    selectedQuickTags.value = [...tags]
    // Clear persisted quick tags when using a system view
    localStorage.removeItem('trinity-dashboard-quick-tags')
  }
}, { immediate: true })

const { fitView, viewport } = useVueFlow()

// Tag clouds visibility toggle (persisted)
const showTagClouds = ref(localStorage.getItem('trinity-show-tag-clouds') !== 'false')
function toggleTagClouds() {
  showTagClouds.value = !showTagClouds.value
  localStorage.setItem('trinity-show-tag-clouds', showTagClouds.value)
}

// Computed stats
const runningCount = computed(() => {
  return agents.value.filter(a => a.status?.toLowerCase() === 'running').length
})

// Grid view (trinity-enterprise#47)
const fleetGridRef = ref(null)

// Agents shown on the grid: same owner filter the graph applies to nodes
// (the tag filter is already applied server-side by fetchAgents).
const gridAgents = computed(() => {
  if (!selectedOwner.value) return agents.value
  const owner = selectedOwner.value === '__unassigned__' ? null : selectedOwner.value
  return agents.value.filter(a => (a.owner || null) === owner)
})

// Agents executing right now: WS-observed in-flight work unioned with the
// polled context-stats activity state.
const workingNowCount = computed(() => {
  const working = new Set(Object.keys(workingState.value))
  for (const [name, s] of Object.entries(contextStats.value)) {
    if (s.activityState === 'active') working.add(name)
  }
  const names = new Set(agents.value.map(a => a.name))
  return [...working].filter(n => names.has(n)).length
})

const stoppedCount = computed(() => {
  return agents.value.filter(a => a.status?.toLowerCase() !== 'running').length
})

onMounted(async () => {
  // Initialize system views store (restores persisted view selection)
  systemViewsStore.initialize()

  // Apply persisted time range to network store
  networkStore.timeRangeHours = selectedTimeRange.value

  // Apply persisted quick tags filter (only if no system view is active)
  if (!systemViewsStore.activeViewId && selectedQuickTags.value.length > 0) {
    networkStore.setFilterTags([...selectedQuickTags.value])
  }

  // PERF-269: Parallelize independent mount calls
  await Promise.allSettled([
    systemViewsStore.fetchViews(),
    networkStore.fetchAgents(),
    networkStore.fetchHistoricalCommunications(),
    networkStore.fetchSchedules(),
    fetchAvailableTags()
  ])

  // First-run onboarding: load the Claude-auth flag (for the wizard's setup
  // hint) and auto-open the wizard if this is a fresh, empty install.
  sessionsStore.loadFeatureFlags().catch(() => {})
  maybeAutoOpenOnboarding()

  // Connect WebSocket for real-time updates
  networkStore.connectWebSocket()

  // Start polling (PERF-269: reduced frequencies, visibility-aware)
  networkStore.startContextPolling()
  networkStore.startAgentRefresh()

  // Start activity refresh polling if in timeline mode (fallback for WebSocket gaps)
  if (networkStore.isTimelineMode) {
    networkStore.startActivityRefresh()
  }

  // Add click outside listener for tag dropdown
  document.addEventListener('click', handleClickOutside)

  // Fit view after initial load (graph mode only — grid fits itself,
  // and calling Vue Flow's fitView with no viewport just warns)
  if (networkStore.viewMode === 'graph') {
    setTimeout(() => {
      fitView({ padding: 0.2, duration: 800 })
    }, 100)
  }
})

onUnmounted(() => {
  networkStore.disconnectWebSocket()
  networkStore.stopContextPolling()
  networkStore.stopAgentRefresh()
  networkStore.stopActivityRefresh()
  document.removeEventListener('click', handleClickOutside)
})

async function refreshAll() {
  await networkStore.fetchAgents()
  await networkStore.fetchHistoricalCommunications()
  if (networkStore.viewMode === 'grid') {
    // Grid mode: re-pull chip batch data + re-hydrate visible tiles.
    fleetGridRef.value?.refresh()
    return
  }
  setTimeout(() => {
    fitView({ padding: 0.2, duration: 800 })
  }, 100)
}

async function onTimeRangeChange() {
  networkStore.timeRangeHours = selectedTimeRange.value
  // Persist time range to localStorage
  localStorage.setItem('trinity-dashboard-time-range', selectedTimeRange.value)
  await networkStore.fetchHistoricalCommunications()
}

function resetLayout() {
  networkStore.resetNodePositions()
  setTimeout(() => {
    fitView({ padding: 0.2, duration: 800 })
  }, 100)
}

function onNodeDragStop(event) {
  networkStore.onNodeDragStop(event)
}

function getNodeColor(node) {
  // System agent gets purple color
  if (node.data?.is_system) {
    return '#a855f7' // purple-500
  }

  const status = node.data?.status?.toLowerCase() || 'stopped'

  const colors = {
    running: '#06b6d4', // cyan-500
    stopped: '#94a3b8', // slate-400
    starting: '#f59e0b', // amber-500
    error: '#ef4444', // red-500
    exited: '#6b7280' // gray-500
  }

  return colors[status] || colors.stopped
}

function formatTime(timestamp) {
  const date = new Date(timestamp)
  const now = new Date()
  const diff = now - date

  if (diff < 60000) return `${Math.floor(diff / 1000)}s`
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m`
  return date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
}

// View Mode Functions
function toggleMode(mode) {
  networkStore.setViewMode(mode)
}

function handlePlay() {
  networkStore.startReplay()
}

function handlePause() {
  networkStore.pauseReplay()
}

function handleStop() {
  networkStore.stopReplay()
}

function handleSpeedChange(event) {
  const speed = parseInt(event.target.value)
  networkStore.setReplaySpeed(speed)
}

async function handleToggleAutonomy(agentName) {
  await networkStore.toggleAutonomy(agentName)
}

function handleTimelineClick(event) {
  const rect = event.currentTarget.getBoundingClientRect()
  const clickX = event.clientX - rect.left
  const timelineWidth = rect.width
  networkStore.handleTimelineClick(clickX, timelineWidth)
}

function formatDuration(ms) {
  if (!ms) return '00:00'
  const minutes = Math.floor(ms / 60000)
  const seconds = Math.floor((ms % 60000) / 1000)
  return `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`
}

function formatTimestamp(timestamp) {
  if (!timestamp) return '--:--'
  const date = new Date(timestamp)
  return date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
}

// Quick Tag Filter functions
async function fetchAvailableTags() {
  try {
    const response = await axios.get('/api/tags')
    availableTags.value = response.data.tags || []
  } catch (err) {
    console.error('Failed to fetch tags:', err)
    availableTags.value = []
  }
}

function toggleQuickTag(tag) {
  const index = selectedQuickTags.value.indexOf(tag)
  if (index === -1) {
    selectedQuickTags.value.push(tag)
  } else {
    selectedQuickTags.value.splice(index, 1)
  }
  // Clear system view selection when using quick tags
  systemViewsStore.clearSelection()
  // Apply filter to network store
  networkStore.setFilterTags([...selectedQuickTags.value])
  // Persist quick tags to localStorage
  localStorage.setItem('trinity-dashboard-quick-tags', JSON.stringify(selectedQuickTags.value))
}

function clearQuickTags() {
  selectedQuickTags.value = []
  networkStore.setFilterTags([])
  // Clear persisted quick tags
  localStorage.removeItem('trinity-dashboard-quick-tags')
}

// Close dropdown when clicking outside
function handleClickOutside(event) {
  if (showTagDropdown.value && !event.target.closest('.relative')) {
    showTagDropdown.value = false
  }
}
</script>

<style scoped>
/* Ensure Vue Flow takes full height */
:deep(.vue-flow) {
  height: 100%;
  width: 100%;
}

/* Style for minimap */
:deep(.vue-flow__minimap) {
  background: linear-gradient(135deg, rgba(255, 255, 255, 0.95), rgba(248, 250, 252, 0.95));
  border: 2px solid rgba(148, 163, 184, 0.3);
  border-radius: 0.75rem;
  backdrop-filter: blur(8px);
  box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -1px rgb(0 0 0 / 0.06);
}

:root.dark :deep(.vue-flow__minimap) {
  background: linear-gradient(135deg, rgba(31, 41, 55, 0.95), rgba(17, 24, 39, 0.95));
  border-color: rgba(75, 85, 99, 0.5);
}

/* Style for controls */
:deep(.vue-flow__controls) {
  background: linear-gradient(135deg, rgba(255, 255, 255, 0.95), rgba(248, 250, 252, 0.95));
  border: 2px solid rgba(148, 163, 184, 0.3);
  border-radius: 0.75rem;
  backdrop-filter: blur(8px);
  box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -1px rgb(0 0 0 / 0.06);
}

:root.dark :deep(.vue-flow__controls) {
  background: linear-gradient(135deg, rgba(31, 41, 55, 0.95), rgba(17, 24, 39, 0.95));
  border-color: rgba(75, 85, 99, 0.5);
}

:deep(.vue-flow__controls-button) {
  background-color: transparent;
  border: none;
  border-bottom: 1px solid rgba(226, 232, 240, 0.8);
  color: #475569;
  transition: all 0.2s ease;
}

:root.dark :deep(.vue-flow__controls-button) {
  border-bottom-color: rgba(55, 65, 81, 0.8);
  color: #9ca3af;
}

:deep(.vue-flow__controls-button:hover) {
  background: linear-gradient(135deg, rgba(59, 130, 246, 0.1), rgba(139, 92, 246, 0.1));
  color: #3b82f6;
}

:root.dark :deep(.vue-flow__controls-button:hover) {
  background: linear-gradient(135deg, rgba(59, 130, 246, 0.2), rgba(139, 92, 246, 0.2));
  color: #60a5fa;
}

:deep(.vue-flow__controls-button:last-child) {
  border-bottom: none;
}

/* Enhanced edge styles with animations */
:deep(.vue-flow__edge-path) {
  transition: all 0.5s ease-in-out;
}

:deep(.collaboration-edge-active .vue-flow__edge-path) {
  stroke-dasharray: 8 4;
  animation: edge-flow 0.6s linear infinite;
}

@keyframes edge-flow {
  0% {
    stroke-dashoffset: 24;
  }
  100% {
    stroke-dashoffset: 0;
  }
}

:deep(.collaboration-edge-inactive .vue-flow__edge-path) {
  opacity: 0.4;
}

/* Smooth edge markers */
:deep(.vue-flow__edge .vue-flow__edge-textwrapper) {
  transition: all 0.3s ease;
}

/* Make edges appear more organic and less schematic */
:deep(.vue-flow__edge) {
  pointer-events: all;
  cursor: pointer;
}

:deep(.vue-flow__edge:hover .vue-flow__edge-path) {
  stroke-width: 3px !important;
  filter: drop-shadow(0 0 4px currentColor);
}

/* Particle effect for flowing dots on animated edges */
:deep(.collaboration-edge-active::after) {
  content: '';
  position: absolute;
  width: 6px;
  height: 6px;
  background: radial-gradient(circle, #67e8f9 0%, transparent 70%);
  border-radius: 50%;
  animation: particle-flow 2s ease-in-out infinite;
}

@keyframes particle-flow {
  0% {
    opacity: 0;
    transform: translateX(0);
  }
  10% {
    opacity: 1;
  }
  90% {
    opacity: 1;
  }
  100% {
    opacity: 0;
    transform: translateX(100%);
  }
}

/* Replay Mode Styles */
.mode-toggle {
  transition: all 0.2s ease;
}

.mode-toggle button {
  transition: all 0.2s ease;
}

/* Timeline Scrubber Styles */
.timeline-scrubber {
  user-select: none;
}

.timeline-track {
  position: relative;
  transition: background-color 0.2s ease;
}

.timeline-track:hover {
  background-color: #e5e7eb;
}

.event-marker {
  transition: all 0.15s ease;
  z-index: 10;
}

.event-marker:hover {
  z-index: 20;
  transform: translate(-50%, -50%) scale(1.3);
}

.playback-marker {
  z-index: 30;
  pointer-events: none;
  transition: left 0.3s ease-out;
}

.playback-marker > div {
  pointer-events: all;
  transition: all 0.2s ease;
}

.playback-marker > div:hover {
  transform: translate(-50%, -50%) scale(1.2);
}

/* Replay controls hover effects */
button:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

button:not(:disabled):hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
}

button:not(:disabled):active {
  transform: translateY(0);
}

/* Loading animation for timeline */
@keyframes timeline-pulse {
  0%, 100% {
    opacity: 1;
  }
  50% {
    opacity: 0.5;
  }
}

.timeline-loading {
  animation: timeline-pulse 2s ease-in-out infinite;
}
</style>
