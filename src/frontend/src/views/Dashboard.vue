<template>
  <div class="h-screen flex flex-col bg-gray-100 dark:bg-gray-900 overflow-hidden">
    <NavBar />

    <main class="flex-1 flex flex-col overflow-hidden">
      <div class="flex flex-col flex-1 overflow-hidden">
        <!-- Compact Header -->
        <div class="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 px-4 py-2">
          <div class="flex items-center justify-between">
            <!-- Left: Stats -->
            <div class="flex items-center">
              <div class="flex items-center space-x-3 text-xs text-gray-500 dark:text-gray-400">
                <span class="flex items-center space-x-1">
                  <span class="font-medium text-gray-700 dark:text-gray-300">{{ agents.length }}</span>
                  <span>agents</span>
                </span>
                <span class="text-gray-300 dark:text-gray-600">·</span>
                <span class="flex items-center space-x-1">
                  <span class="w-1.5 h-1.5 rounded-full bg-green-500"></span>
                  <span class="font-medium text-green-600 dark:text-green-400">{{ runningCount }}</span>
                  <span>running</span>
                </span>
                <span class="text-gray-300 dark:text-gray-600">·</span>
                <span class="flex items-center space-x-1">
                  <span class="font-medium text-blue-600 dark:text-blue-400">{{ totalCollaborationCount }}</span>
                  <span>messages ({{ timeRangeHours }}h)</span>
                </span>
                <span v-if="activeCollaborationCount > 0" class="text-gray-300 dark:text-gray-600">·</span>
                <span v-if="activeCollaborationCount > 0" class="flex items-center space-x-1 text-green-600 dark:text-green-400">
                  <span class="w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse"></span>
                  <span class="font-medium">{{ activeCollaborationCount }}</span>
                  <span>active</span>
                </span>
                <!-- OTel Stats -->
                <template v-if="observabilityStore.isOperational && observabilityStore.hasData">
                  <span class="text-gray-300 dark:text-gray-600">·</span>
                  <span class="flex items-center space-x-1">
                    <svg class="w-3 h-3 text-emerald-500" fill="currentColor" viewBox="0 0 20 20">
                      <path d="M8.433 7.418c.155-.103.346-.196.567-.267v1.698a2.305 2.305 0 01-.567-.267C8.07 8.34 8 8.114 8 8c0-.114.07-.34.433-.582zM11 12.849v-1.698c.22.071.412.164.567.267.364.243.433.468.433.582 0 .114-.07.34-.433.582a2.305 2.305 0 01-.567.267z"/>
                      <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm1-13a1 1 0 10-2 0v.092a4.535 4.535 0 00-1.676.662C6.602 6.234 6 7.009 6 8c0 .99.602 1.765 1.324 2.246.48.32 1.054.545 1.676.662v1.941c-.391-.127-.68-.317-.843-.504a1 1 0 10-1.51 1.31c.562.649 1.413 1.076 2.353 1.253V15a1 1 0 102 0v-.092a4.535 4.535 0 001.676-.662C13.398 13.766 14 12.991 14 12c0-.99-.602-1.765-1.324-2.246A4.535 4.535 0 0011 9.092V7.151c.391.127.68.317.843.504a1 1 0 101.511-1.31c-.563-.649-1.413-1.076-2.354-1.253V5z" clip-rule="evenodd"/>
                    </svg>
                    <span class="font-medium text-emerald-600 dark:text-emerald-400">{{ observabilityStore.formattedTotalCost }}</span>
                  </span>
                  <span class="text-gray-300 dark:text-gray-600">·</span>
                  <span class="flex items-center space-x-1">
                    <span class="font-medium text-cyan-600 dark:text-cyan-400">{{ observabilityStore.formattedTotalTokens }}</span>
                    <span>tokens</span>
                  </span>
                </template>
                <span v-else-if="observabilityStore.enabled && !observabilityStore.available" class="text-gray-300 dark:text-gray-600">·</span>
                <span v-if="observabilityStore.enabled && !observabilityStore.available" class="flex items-center space-x-1 text-yellow-600 dark:text-yellow-400" title="OTel Collector unavailable">
                  <svg class="w-3 h-3" fill="currentColor" viewBox="0 0 20 20">
                    <path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd"/>
                  </svg>
                  <span>OTel</span>
                </span>
              </div>
            </div>

            <!-- Right: Controls -->
            <div class="flex items-center space-x-2">
              <!-- Mode Toggle -->
              <div class="flex rounded-md border border-gray-300 dark:border-gray-600 p-0.5 bg-gray-50 dark:bg-gray-700">
                <button
                  @click="toggleMode('live')"
                  :class="[
                    'px-2 py-1 rounded text-xs font-medium transition-all',
                    !isReplayMode ? 'bg-blue-600 text-white' : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'
                  ]"
                >
                  Live
                </button>
                <button
                  @click="toggleMode('replay')"
                  :class="[
                    'px-2 py-1 rounded text-xs font-medium transition-all',
                    isReplayMode ? 'bg-blue-600 text-white' : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'
                  ]"
                >
                  Replay
                </button>
              </div>

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
                  isConnected ? 'bg-green-500' : 'bg-red-500'
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

              <!-- Reset Layout -->
              <button
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

    <!-- Replay Controls (only visible in replay mode) -->
    <div v-if="isReplayMode" class="bg-gradient-to-r from-slate-50 to-gray-50 dark:from-gray-800 dark:to-gray-800 border-b-2 border-gray-300 dark:border-gray-600 px-6 py-4">
      <!-- Playback Controls -->
      <div class="flex items-center justify-between mb-4">
        <div class="flex items-center space-x-4">
          <!-- Play/Pause/Stop buttons -->
          <div class="flex items-center space-x-2">
            <button
              @click="handlePlay"
              :disabled="isPlaying || totalEvents === 0"
              class="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors duration-150 text-sm font-medium flex items-center space-x-1"
            >
              <svg class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                <path d="M6.3 2.841A1.5 1.5 0 004 4.11V15.89a1.5 1.5 0 002.3 1.269l9.344-5.89a1.5 1.5 0 000-2.538L6.3 2.84z" />
              </svg>
              <span>Play</span>
            </button>

            <button
              @click="handlePause"
              :disabled="!isPlaying"
              class="px-4 py-2 bg-yellow-600 text-white rounded-lg hover:bg-yellow-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors duration-150 text-sm font-medium flex items-center space-x-1"
            >
              <svg class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                <path d="M5.75 3a.75.75 0 00-.75.75v12.5c0 .414.336.75.75.75h1.5a.75.75 0 00.75-.75V3.75A.75.75 0 007.25 3h-1.5zM12.75 3a.75.75 0 00-.75.75v12.5c0 .414.336.75.75.75h1.5a.75.75 0 00.75-.75V3.75a.75.75 0 00-.75-.75h-1.5z" />
              </svg>
              <span>Pause</span>
            </button>

            <button
              @click="handleStop"
              class="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors duration-150 text-sm font-medium flex items-center space-x-1"
            >
              <svg class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                <path d="M5.25 3A2.25 2.25 0 003 5.25v9.5A2.25 2.25 0 005.25 17h9.5A2.25 2.25 0 0017 14.75v-9.5A2.25 2.25 0 0014.75 3h-9.5z" />
              </svg>
              <span>Stop</span>
            </button>
          </div>

          <!-- Speed selector -->
          <div class="flex items-center space-x-2">
            <label class="text-xs text-gray-600 dark:text-gray-400 font-medium">Speed:</label>
            <select
              :value="replaySpeed"
              @change="handleSpeedChange"
              class="text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white dark:bg-gray-700 dark:text-gray-200"
            >
              <option :value="1">1x</option>
              <option :value="2">2x</option>
              <option :value="5">5x</option>
              <option :value="10">10x</option>
              <option :value="20">20x</option>
              <option :value="50">50x</option>
            </select>
          </div>
        </div>

        <!-- Progress stats -->
        <div class="flex items-center space-x-6 text-sm text-gray-600 dark:text-gray-400">
          <div class="flex items-center space-x-2">
            <span class="text-xs text-gray-500 dark:text-gray-500">Event:</span>
            <span class="font-medium">{{ currentEventIndex }} / {{ totalEvents }}</span>
            <span class="text-gray-400 dark:text-gray-500">({{ Math.round((currentEventIndex / totalEvents) * 100) || 0 }}%)</span>
          </div>
          <div class="flex items-center space-x-2">
            <span class="text-xs text-gray-500 dark:text-gray-500">Time:</span>
            <span class="font-medium">{{ formatDuration(replayElapsedMs) }} / {{ formatDuration(totalDuration) }}</span>
          </div>
          <div v-if="isPlaying" class="flex items-center space-x-2">
            <span class="text-xs text-gray-500 dark:text-gray-500">Remaining:</span>
            <span class="font-medium">{{ formatDuration(totalDuration - replayElapsedMs) }}</span>
          </div>
        </div>
      </div>

      <!-- Timeline Scrubber -->
      <div class="timeline-scrubber">
        <div class="flex items-center justify-between text-xs text-gray-500 dark:text-gray-400 mb-2">
          <span>{{ formatTimestamp(timelineStart) }}</span>
          <span class="font-medium text-gray-700 dark:text-gray-300">{{ formatTimestamp(currentTime) }}</span>
          <span>{{ formatTimestamp(timelineEnd) }}</span>
        </div>

        <div
          class="timeline-track relative h-10 bg-gray-200 dark:bg-gray-700 rounded-lg cursor-pointer hover:bg-gray-250 dark:hover:bg-gray-600 transition-colors"
          @click="handleTimelineClick"
        >
          <!-- Event markers -->
          <div
            v-for="(event, i) in historicalCollaborations"
            :key="'marker-' + i"
            class="event-marker absolute top-1/2 -translate-y-1/2 w-2 h-2 bg-gray-400 dark:bg-gray-500 rounded-full hover:bg-blue-500 hover:w-3 hover:h-3 transition-all cursor-pointer"
            :style="{ left: networkStore.getEventPosition(event) + '%' }"
            :title="`${event.source_agent} → ${event.target_agent} at ${formatTimestamp(event.timestamp)}`"
          ></div>

          <!-- Playback position marker -->
          <div
            class="playback-marker absolute top-0 bottom-0 w-1 bg-blue-600 cursor-ew-resize"
            :style="{ left: playbackPosition + '%' }"
          >
            <div class="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-4 h-4 bg-blue-600 rounded-full border-2 border-white dark:border-gray-800 shadow-lg"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- Graph Canvas - Full Height (expands to fill remaining space) -->
    <div class="relative bg-white dark:bg-gray-800 shadow-sm dark:shadow-gray-900 flex-1 min-h-0">
      <!-- Empty state -->
      <div
        v-if="nodes.length === 0"
        class="absolute inset-0 flex items-center justify-center"
      >
        <div class="text-center">
          <svg class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
          </svg>
          <h3 class="mt-2 text-sm font-medium text-gray-900 dark:text-gray-100">No agents</h3>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Create an agent to get started.</p>
          <div class="mt-4">
            <router-link
              to="/agents"
              class="inline-flex items-center px-4 py-2 border border-transparent shadow-sm text-sm font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700"
            >
              <svg class="-ml-1 mr-2 h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6m0 0v6m0-6h6m-6 0H6" />
              </svg>
              Create Agent
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
        <!-- Custom node template -->
        <template #node-agent="nodeProps">
          <AgentNode v-bind="nodeProps" />
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
          <span class="text-xs bg-blue-100 dark:bg-blue-900 text-blue-800 dark:text-blue-200 px-2 py-1 rounded-full font-medium">
            {{ totalCollaborationCount }} total
          </span>
        </div>

        <!-- Real-time feed (last 10) -->
        <div v-if="collaborationHistory.length > 0" class="mb-3 pb-3 border-b border-gray-200 dark:border-gray-700">
          <div class="text-xs font-semibold text-gray-500 dark:text-gray-400 mb-2 flex items-center">
            <svg class="w-3 h-3 mr-1 text-green-500 animate-pulse" fill="currentColor" viewBox="0 0 20 20">
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
              <svg class="w-3 h-3 text-blue-500 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
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

      <!-- Observability Panel -->
      <ObservabilityPanel v-if="observabilityStore.enabled" />
    </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import NavBar from '@/components/NavBar.vue'
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { VueFlow, useVueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import { useNetworkStore } from '@/stores/network'
import { useObservabilityStore } from '@/stores/observability'
import { storeToRefs } from 'pinia'
import AgentNode from '@/components/AgentNode.vue'
import ObservabilityPanel from '@/components/ObservabilityPanel.vue'

// Import Vue Flow styles
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'
import '@vue-flow/minimap/dist/style.css'

const networkStore = useNetworkStore()
const observabilityStore = useObservabilityStore()
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
  // Replay state
  isReplayMode,
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

const selectedTimeRange = ref(24) // Default to 24 hours
const isHistoryPanelOpen = ref(false) // History panel starts closed

const { fitView } = useVueFlow()

// Computed stats
const runningCount = computed(() => {
  return agents.value.filter(a => a.status?.toLowerCase() === 'running').length
})

const stoppedCount = computed(() => {
  return agents.value.filter(a => a.status?.toLowerCase() !== 'running').length
})

onMounted(async () => {
  // Fetch agents first
  await networkStore.fetchAgents()

  // Fetch historical communication data from Activity Stream
  await networkStore.fetchHistoricalCommunications()

  // Connect WebSocket for real-time updates
  networkStore.connectWebSocket()

  // Start polling for context stats
  networkStore.startContextPolling()

  // Start polling for agent list updates
  networkStore.startAgentRefresh()

  // Initialize observability (checks if OTel is enabled)
  await observabilityStore.fetchStatus()

  // Fit view after initial load
  setTimeout(() => {
    fitView({ padding: 0.2, duration: 800 })
  }, 100)
})

onUnmounted(() => {
  networkStore.disconnectWebSocket()
  networkStore.stopContextPolling()
  networkStore.stopAgentRefresh()
})

async function refreshAll() {
  await networkStore.fetchAgents()
  await networkStore.fetchHistoricalCommunications()
  setTimeout(() => {
    fitView({ padding: 0.2, duration: 800 })
  }, 100)
}

async function onTimeRangeChange() {
  networkStore.timeRangeHours = selectedTimeRange.value
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

// Replay Mode Functions
function toggleMode(mode) {
  networkStore.setReplayMode(mode === 'replay')
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
