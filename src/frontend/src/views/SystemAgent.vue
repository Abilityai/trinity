<template>
  <div class="min-h-screen bg-gray-100 dark:bg-gray-900">
    <NavBar />

    <main class="max-w-6xl mx-auto py-6 px-4 sm:px-6 lg:px-8">
      <!-- Loading State -->
      <div v-if="loading" class="text-center py-12">
        <div class="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-500 mx-auto"></div>
        <p class="mt-4 text-gray-500 dark:text-gray-400">Loading System Agent...</p>
      </div>

      <!-- Not Found State -->
      <div v-else-if="!systemAgent" class="text-center py-12">
        <div class="bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded-lg p-6 max-w-md mx-auto">
          <svg class="w-12 h-12 text-yellow-500 mx-auto mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
          </svg>
          <h3 class="text-lg font-medium text-yellow-800 dark:text-yellow-200">System Agent Not Found</h3>
          <p class="mt-2 text-sm text-yellow-700 dark:text-yellow-300">
            The system agent (trinity-system) is not deployed. It should auto-deploy on backend restart.
          </p>
        </div>
      </div>

      <!-- Main Content -->
      <div v-else>
        <!-- Compact Header with OTel -->
        <div class="bg-white dark:bg-gray-800 rounded-lg shadow border border-gray-200 dark:border-gray-700 mb-6">
          <!-- Top Row: Agent Info + Status + Actions -->
          <div class="p-4 border-b border-gray-100 dark:border-gray-700/50">
            <div class="flex items-center justify-between">
              <div class="flex items-center space-x-3">
                <div class="bg-gray-100 dark:bg-gray-700 p-2 rounded-lg">
                  <svg class="w-6 h-6 text-gray-600 dark:text-gray-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z" />
                  </svg>
                </div>
                <div>
                  <h1 class="text-lg font-bold text-gray-900 dark:text-white flex items-center gap-2">
                    System Agent
                    <span class="px-1.5 py-0.5 text-[10px] font-medium bg-purple-100 dark:bg-purple-900/50 text-purple-700 dark:text-purple-300 rounded">
                      SYSTEM
                    </span>
                  </h1>
                  <p class="text-xs text-gray-500 dark:text-gray-400">Platform Operations Manager</p>
                </div>
              </div>
              <div class="flex items-center space-x-2">
                <span :class="[
                  'px-2 py-0.5 text-xs font-medium rounded-full',
                  systemAgent.status === 'running'
                    ? 'bg-green-100 dark:bg-green-900/50 text-green-700 dark:text-green-300'
                    : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400'
                ]">
                  {{ systemAgent.status }}
                </span>
                <button
                  v-if="systemAgent.status === 'stopped'"
                  @click="startSystemAgent"
                  :disabled="actionLoading"
                  class="text-xs bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-700 dark:text-gray-300 px-3 py-1.5 rounded-lg font-medium transition-colors disabled:opacity-50"
                >
                  {{ actionLoading ? 'Starting...' : 'Start' }}
                </button>
                <button
                  v-else
                  @click="restartSystemAgent"
                  :disabled="actionLoading"
                  class="text-xs bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-700 dark:text-gray-300 px-3 py-1.5 rounded-lg font-medium transition-colors disabled:opacity-50"
                >
                  {{ actionLoading ? 'Restarting...' : 'Restart' }}
                </button>
              </div>
            </div>
          </div>

          <!-- Middle Row: Fleet Stats (compact) -->
          <div class="px-4 py-3 border-b border-gray-100 dark:border-gray-700/50 bg-gray-50/50 dark:bg-gray-800/50">
            <div class="flex items-center space-x-6">
              <div class="flex items-center space-x-2">
                <span class="text-xs text-gray-500 dark:text-gray-400">Fleet:</span>
                <span class="text-sm font-semibold text-gray-900 dark:text-white">{{ fleetStatus.total }}</span>
                <span class="text-xs text-gray-400 dark:text-gray-500">agents</span>
              </div>
              <div class="h-4 w-px bg-gray-300 dark:bg-gray-600"></div>
              <div class="flex items-center space-x-1.5">
                <span class="w-2 h-2 rounded-full bg-green-500"></span>
                <span class="text-sm font-medium text-green-600 dark:text-green-400">{{ fleetStatus.running }}</span>
                <span class="text-xs text-gray-400 dark:text-gray-500">running</span>
              </div>
              <div class="flex items-center space-x-1.5">
                <span class="w-2 h-2 rounded-full bg-gray-400"></span>
                <span class="text-sm font-medium text-gray-600 dark:text-gray-400">{{ fleetStatus.stopped }}</span>
                <span class="text-xs text-gray-400 dark:text-gray-500">stopped</span>
              </div>
              <div v-if="fleetStatus.issues > 0" class="flex items-center space-x-1.5">
                <span class="w-2 h-2 rounded-full bg-red-500 animate-pulse"></span>
                <span class="text-sm font-medium text-red-600 dark:text-red-400">{{ fleetStatus.issues }}</span>
                <span class="text-xs text-gray-400 dark:text-gray-500">issues</span>
              </div>
              <button
                @click="refreshFleetStatus"
                :disabled="fleetLoading"
                class="ml-auto text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 p-1"
                title="Refresh fleet status"
              >
                <svg :class="['w-4 h-4', fleetLoading ? 'animate-spin' : '']" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
              </button>
            </div>
          </div>

          <!-- Bottom Row: OpenTelemetry Metrics -->
          <div class="px-4 py-3">
            <!-- OTel Status Indicator -->
            <div class="flex items-center justify-between mb-3">
              <div class="flex items-center space-x-2">
                <div :class="[
                  'w-2 h-2 rounded-full',
                  otelMetrics.available ? 'bg-green-500' : otelMetrics.enabled ? 'bg-yellow-500' : 'bg-gray-400'
                ]"></div>
                <span class="text-xs font-medium text-gray-700 dark:text-gray-300">OpenTelemetry</span>
                <span v-if="!otelMetrics.enabled" class="text-xs text-gray-400 dark:text-gray-500">(disabled)</span>
                <span v-else-if="!otelMetrics.available" class="text-xs text-yellow-600 dark:text-yellow-400">(unavailable)</span>
              </div>
              <button
                @click="refreshOtelMetrics"
                :disabled="otelLoading"
                class="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 flex items-center space-x-1"
              >
                <svg :class="['w-3 h-3', otelLoading ? 'animate-spin' : '']" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
                <span>Refresh</span>
              </button>
            </div>

            <!-- OTel Metrics Grid -->
            <div v-if="otelMetrics.available && otelMetrics.hasData" class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
              <!-- Total Cost -->
              <div class="bg-gray-50 dark:bg-gray-700/50 rounded-lg p-3">
                <div class="flex items-center justify-between mb-1">
                  <span class="text-xs text-gray-500 dark:text-gray-400">Total Cost</span>
                  <svg class="w-4 h-4 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                </div>
                <div class="text-lg font-bold text-gray-900 dark:text-white">${{ otelMetrics.totals.total_cost.toFixed(2) }}</div>
                <div class="mt-1 h-1 bg-gray-200 dark:bg-gray-600 rounded-full overflow-hidden">
                  <div class="h-full bg-green-500 rounded-full" :style="{ width: costBarWidth + '%' }"></div>
                </div>
              </div>

              <!-- Total Tokens -->
              <div class="bg-gray-50 dark:bg-gray-700/50 rounded-lg p-3">
                <div class="flex items-center justify-between mb-1">
                  <span class="text-xs text-gray-500 dark:text-gray-400">Tokens</span>
                  <svg class="w-4 h-4 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 21a4 4 0 01-4-4V5a2 2 0 012-2h4a2 2 0 012 2v12a4 4 0 01-4 4zm0 0h12a2 2 0 002-2v-4a2 2 0 00-2-2h-2.343M11 7.343l1.657-1.657a2 2 0 012.828 0l2.829 2.829a2 2 0 010 2.828l-8.486 8.485M7 17h.01" />
                  </svg>
                </div>
                <div class="text-lg font-bold text-gray-900 dark:text-white">{{ formatTokens(otelMetrics.totals.total_tokens) }}</div>
                <!-- Token type breakdown mini bar -->
                <div class="mt-1 h-1 bg-gray-200 dark:bg-gray-600 rounded-full overflow-hidden flex">
                  <div class="h-full bg-blue-500" :style="{ width: tokenInputPercent + '%' }" title="Input"></div>
                  <div class="h-full bg-purple-500" :style="{ width: tokenOutputPercent + '%' }" title="Output"></div>
                  <div class="h-full bg-teal-500" :style="{ width: tokenCachePercent + '%' }" title="Cache"></div>
                </div>
              </div>

              <!-- Sessions -->
              <div class="bg-gray-50 dark:bg-gray-700/50 rounded-lg p-3">
                <div class="flex items-center justify-between mb-1">
                  <span class="text-xs text-gray-500 dark:text-gray-400">Sessions</span>
                  <svg class="w-4 h-4 text-indigo-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                  </svg>
                </div>
                <div class="text-lg font-bold text-gray-900 dark:text-white">{{ otelMetrics.totals.sessions }}</div>
                <div class="text-xs text-gray-400 dark:text-gray-500 mt-1">conversations</div>
              </div>

              <!-- Active Time -->
              <div class="bg-gray-50 dark:bg-gray-700/50 rounded-lg p-3">
                <div class="flex items-center justify-between mb-1">
                  <span class="text-xs text-gray-500 dark:text-gray-400">Active Time</span>
                  <svg class="w-4 h-4 text-orange-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                </div>
                <div class="text-lg font-bold text-gray-900 dark:text-white">{{ formatActiveTime(otelMetrics.totals.active_time_seconds) }}</div>
                <div class="text-xs text-gray-400 dark:text-gray-500 mt-1">usage</div>
              </div>

              <!-- Commits -->
              <div class="bg-gray-50 dark:bg-gray-700/50 rounded-lg p-3">
                <div class="flex items-center justify-between mb-1">
                  <span class="text-xs text-gray-500 dark:text-gray-400">Commits</span>
                  <svg class="w-4 h-4 text-pink-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                </div>
                <div class="text-lg font-bold text-gray-900 dark:text-white">{{ otelMetrics.totals.commits }}</div>
                <div class="text-xs text-gray-400 dark:text-gray-500 mt-1">created</div>
              </div>

              <!-- Lines of Code -->
              <div class="bg-gray-50 dark:bg-gray-700/50 rounded-lg p-3">
                <div class="flex items-center justify-between mb-1">
                  <span class="text-xs text-gray-500 dark:text-gray-400">Lines</span>
                  <svg class="w-4 h-4 text-cyan-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4" />
                  </svg>
                </div>
                <div class="flex items-center space-x-2">
                  <span class="text-sm font-bold text-green-600 dark:text-green-400">+{{ otelMetrics.metrics.lines_of_code?.added || 0 }}</span>
                  <span class="text-sm font-bold text-red-600 dark:text-red-400">-{{ otelMetrics.metrics.lines_of_code?.removed || 0 }}</span>
                </div>
                <div class="text-xs text-gray-400 dark:text-gray-500 mt-1">changed</div>
              </div>
            </div>

            <!-- OTel Not Available State -->
            <div v-else-if="otelMetrics.enabled && !otelMetrics.available" class="text-center py-4">
              <p class="text-sm text-yellow-600 dark:text-yellow-400">{{ otelMetrics.error || 'Collector not reachable' }}</p>
            </div>

            <!-- OTel Not Enabled State -->
            <div v-else-if="!otelMetrics.enabled" class="text-center py-4">
              <p class="text-sm text-gray-500 dark:text-gray-400">Set <code class="bg-gray-100 dark:bg-gray-700 px-1 rounded">OTEL_ENABLED=1</code> to enable metrics</p>
            </div>

            <!-- No Data Yet -->
            <div v-else class="text-center py-4">
              <p class="text-sm text-gray-500 dark:text-gray-400">No metrics data yet. Chat with agents to generate data.</p>
            </div>
          </div>
        </div>

        <!-- Quick Actions and Chat -->
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <!-- Quick Actions -->
          <div class="bg-white dark:bg-gray-800 rounded-lg shadow">
            <div class="p-4 border-b border-gray-200 dark:border-gray-700">
              <h2 class="text-lg font-semibold text-gray-900 dark:text-white">Quick Actions</h2>
            </div>
            <div class="p-4 space-y-3">
              <!-- Emergency Stop -->
              <button
                @click="emergencyStop"
                :disabled="emergencyLoading"
                class="w-full flex items-center justify-center space-x-2 border-2 border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-900/20 hover:bg-red-100 dark:hover:bg-red-900/40 disabled:opacity-50 text-red-700 dark:text-red-400 font-medium py-2.5 px-4 rounded-lg transition-colors"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" />
                </svg>
                <span class="text-sm">{{ emergencyLoading ? 'Stopping...' : 'Emergency Stop' }}</span>
              </button>

              <!-- Restart All -->
              <button
                @click="restartAllAgents"
                :disabled="restartAllLoading"
                class="w-full flex items-center justify-center space-x-2 border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50 text-gray-700 dark:text-gray-300 font-medium py-2.5 px-4 rounded-lg transition-colors"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
                <span class="text-sm">{{ restartAllLoading ? 'Restarting...' : 'Restart All Agents' }}</span>
              </button>

              <!-- Pause Schedules -->
              <button
                @click="pauseAllSchedules"
                :disabled="pauseLoading"
                class="w-full flex items-center justify-center space-x-2 border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50 text-gray-700 dark:text-gray-300 font-medium py-2.5 px-4 rounded-lg transition-colors"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 9v6m4-6v6m7-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <span class="text-sm">{{ pauseLoading ? 'Pausing...' : 'Pause Schedules' }}</span>
              </button>

              <!-- Resume Schedules -->
              <button
                @click="resumeAllSchedules"
                :disabled="resumeLoading"
                class="w-full flex items-center justify-center space-x-2 border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50 text-gray-700 dark:text-gray-300 font-medium py-2.5 px-4 rounded-lg transition-colors"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <span class="text-sm">{{ resumeLoading ? 'Resuming...' : 'Resume Schedules' }}</span>
              </button>
            </div>
          </div>

          <!-- Terminal Panel -->
          <div
            :class="[
              'bg-gray-900 rounded-lg shadow overflow-hidden flex flex-col transition-all duration-300',
              isFullscreen
                ? 'fixed inset-0 z-50 rounded-none'
                : 'lg:col-span-2'
            ]"
            :style="isFullscreen ? {} : { height: '500px' }"
          >
            <!-- Terminal Header with Fullscreen Toggle -->
            <div class="flex items-center justify-between px-3 py-2 bg-gray-800 border-b border-gray-700 shrink-0">
              <div class="flex items-center space-x-2">
                <svg class="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
                </svg>
                <span class="text-sm font-medium text-gray-300">System Terminal</span>
              </div>
              <button
                @click="toggleFullscreen"
                class="p-1.5 text-gray-400 hover:text-gray-200 hover:bg-gray-700 rounded transition-colors"
                :title="isFullscreen ? 'Exit fullscreen (Esc)' : 'Fullscreen'"
              >
                <!-- Expand icon -->
                <svg v-if="!isFullscreen" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5l-5-5m5 5v-4m0 4h-4" />
                </svg>
                <!-- Collapse icon -->
                <svg v-else class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 9V4.5M9 9H4.5M9 9L3.75 3.75M9 15v4.5M9 15H4.5M9 15l-5.25 5.25M15 9h4.5M15 9V4.5M15 9l5.25-5.25M15 15h4.5M15 15v4.5m0-4.5l5.25 5.25" />
                </svg>
              </button>
            </div>
            <!-- Terminal Content -->
            <div class="flex-1 min-h-0">
              <SystemAgentTerminal
                v-if="systemAgent?.status === 'running' && isAdmin"
                ref="terminalRef"
                :auto-connect="true"
                @connected="onTerminalConnected"
                @disconnected="onTerminalDisconnected"
                @error="onTerminalError"
              />
              <!-- Not Running State -->
              <div v-else-if="systemAgent?.status !== 'running'" class="h-full flex items-center justify-center bg-gray-800">
                <div class="text-center">
                  <svg class="w-12 h-12 text-gray-500 mx-auto mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
                  </svg>
                  <p class="text-gray-400">System agent must be running</p>
                  <button
                    @click="startSystemAgent"
                    :disabled="actionLoading"
                    class="mt-3 text-sm bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white px-4 py-2 rounded-lg font-medium transition-colors"
                  >
                    {{ actionLoading ? 'Starting...' : 'Start Agent' }}
                  </button>
                </div>
              </div>
              <!-- Not Admin State -->
              <div v-else-if="!isAdmin" class="h-full flex items-center justify-center bg-gray-800">
                <div class="text-center">
                  <svg class="w-12 h-12 text-gray-500 mx-auto mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
                  </svg>
                  <p class="text-gray-400">Admin access required</p>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Notification Toast -->
        <div
          v-if="notification"
          :class="[
            'fixed bottom-4 right-4 px-4 py-3 rounded-lg shadow-lg transition-all duration-300 z-50',
            notification.type === 'success' ? 'bg-green-600 text-white' :
            notification.type === 'error' ? 'bg-red-600 text-white' :
            'bg-gray-700 text-white'
          ]"
        >
          {{ notification.message }}
        </div>

      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, onActivated, onDeactivated, nextTick } from 'vue'
import axios from 'axios'
import NavBar from '../components/NavBar.vue'
import SystemAgentTerminal from '../components/SystemAgentTerminal.vue'
import { useAuthStore } from '../stores/auth'
const authStore = useAuthStore()

// Component name for KeepAlive matching
defineOptions({
  name: 'SystemAgent'
})

// State
const loading = ref(true)
const systemAgent = ref(null)
const actionLoading = ref(false)

// Fleet status
const fleetStatus = ref({
  total: 0,
  running: 0,
  stopped: 0,
  issues: 0
})
const fleetLoading = ref(false)

// OpenTelemetry metrics
const otelMetrics = ref({
  enabled: false,
  available: false,
  error: null,
  hasData: false,
  metrics: {
    cost_by_model: {},
    tokens_by_model: {},
    lines_of_code: {},
    sessions: 0,
    active_time_seconds: 0,
    commits: 0,
    pull_requests: 0
  },
  totals: {
    total_cost: 0,
    total_tokens: 0,
    tokens_by_type: {},
    total_lines: 0,
    sessions: 0,
    active_time_seconds: 0,
    commits: 0,
    pull_requests: 0
  }
})
const otelLoading = ref(false)

// Quick action loading states
const emergencyLoading = ref(false)
const restartAllLoading = ref(false)
const pauseLoading = ref(false)
const resumeLoading = ref(false)

// Notifications
const notification = ref(null)

// Terminal ref
const terminalRef = ref(null)

// Fullscreen state
const isFullscreen = ref(false)

// User role for admin checks
const userRole = ref(null)
const isAdmin = computed(() => userRole.value === 'admin')

// Polling intervals
let pollingInterval = null
let otelPollingInterval = null

// Computed properties for OTel visualizations
const costBarWidth = computed(() => {
  // Scale cost to a percentage (assume $10 as max for visualization)
  const cost = otelMetrics.value.totals.total_cost
  return Math.min(cost / 10 * 100, 100)
})

const tokenInputPercent = computed(() => {
  const types = otelMetrics.value.totals.tokens_by_type
  const total = otelMetrics.value.totals.total_tokens
  if (total === 0) return 0
  const input = (types.input || 0)
  return Math.round(input / total * 100)
})

const tokenOutputPercent = computed(() => {
  const types = otelMetrics.value.totals.tokens_by_type
  const total = otelMetrics.value.totals.total_tokens
  if (total === 0) return 0
  const output = (types.output || 0)
  return Math.round(output / total * 100)
})

const tokenCachePercent = computed(() => {
  const types = otelMetrics.value.totals.tokens_by_type
  const total = otelMetrics.value.totals.total_tokens
  if (total === 0) return 0
  const cache = (types.cacheRead || 0) + (types.cacheCreation || 0)
  return Math.round(cache / total * 100)
})

// Helper functions
function formatTokens(num) {
  if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`
  if (num >= 1000) return `${(num / 1000).toFixed(1)}K`
  return num.toString()
}

function formatActiveTime(seconds) {
  if (!seconds || seconds === 0) return '0s'
  if (seconds < 60) return `${Math.round(seconds)}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  const hours = Math.floor(seconds / 3600)
  const mins = Math.round((seconds % 3600) / 60)
  return `${hours}h ${mins}m`
}

// Load system agent status
async function loadSystemAgent() {
  try {
    const response = await axios.get('/api/system-agent/status', {
      headers: authStore.authHeader
    })
    systemAgent.value = response.data
  } catch (error) {
    console.error('Failed to load system agent:', error)
    systemAgent.value = null
  } finally {
    loading.value = false
  }
}

// Fetch fleet status from ops API
async function refreshFleetStatus() {
  fleetLoading.value = true
  try {
    const response = await axios.get('/api/ops/fleet/status', {
      headers: authStore.authHeader
    })

    // Use the summary from the API (already excludes system agent)
    const summary = response.data.summary || {}
    const agents = response.data.agents || []

    // Count non-system agents for display
    const nonSystemAgents = agents.filter(a => !a.is_system)

    fleetStatus.value = {
      total: nonSystemAgents.length,
      running: nonSystemAgents.filter(a => a.status === 'running').length,
      stopped: nonSystemAgents.filter(a => a.status !== 'running').length,
      issues: summary.high_context || 0
    }
  } catch (error) {
    console.error('Failed to fetch fleet status:', error)
  } finally {
    fleetLoading.value = false
  }
}

// Fetch OpenTelemetry metrics
async function refreshOtelMetrics() {
  otelLoading.value = true
  try {
    const response = await axios.get('/api/observability/metrics', {
      headers: authStore.authHeader
    })

    const data = response.data
    otelMetrics.value.enabled = data.enabled
    otelMetrics.value.available = data.available || false
    otelMetrics.value.error = data.error || null

    if (data.metrics) {
      otelMetrics.value.metrics = data.metrics
    }

    if (data.totals) {
      otelMetrics.value.totals = data.totals
    }

    // Check if we have any meaningful data
    otelMetrics.value.hasData = data.totals &&
      (data.totals.total_cost > 0 ||
       data.totals.total_tokens > 0 ||
       data.totals.sessions > 0)

  } catch (error) {
    console.error('Failed to fetch OTel metrics:', error)
    otelMetrics.value.error = 'Failed to fetch metrics'
    otelMetrics.value.available = false
  } finally {
    otelLoading.value = false
  }
}

// System agent actions
async function startSystemAgent() {
  actionLoading.value = true
  try {
    await axios.post('/api/system-agent/restart', {}, {
      headers: authStore.authHeader
    })
    showNotification('System agent started', 'success')
    await loadSystemAgent()
  } catch (error) {
    showNotification(error.response?.data?.detail || 'Failed to start system agent', 'error')
  } finally {
    actionLoading.value = false
  }
}

async function restartSystemAgent() {
  actionLoading.value = true
  try {
    await axios.post('/api/system-agent/restart', {}, {
      headers: authStore.authHeader
    })
    showNotification('System agent restarted', 'success')
    await loadSystemAgent()
  } catch (error) {
    showNotification(error.response?.data?.detail || 'Failed to restart system agent', 'error')
  } finally {
    actionLoading.value = false
  }
}

// Quick actions
async function emergencyStop() {
  if (!confirm('This will stop ALL agents and pause ALL schedules. Continue?')) return

  emergencyLoading.value = true
  try {
    const response = await axios.post('/api/ops/emergency-stop', {}, {
      headers: authStore.authHeader
    })
    showNotification(`Emergency stop: ${response.data.agents_stopped} agents stopped, ${response.data.schedules_paused} schedules paused`, 'success')
    await refreshFleetStatus()
  } catch (error) {
    showNotification(error.response?.data?.detail || 'Emergency stop failed', 'error')
  } finally {
    emergencyLoading.value = false
  }
}

async function restartAllAgents() {
  if (!confirm('This will restart ALL agents. Continue?')) return

  restartAllLoading.value = true
  try {
    const response = await axios.post('/api/ops/fleet/restart', {}, {
      headers: authStore.authHeader
    })
    showNotification(`Restarted ${response.data.restarted?.length || 0} agents`, 'success')
    await refreshFleetStatus()
  } catch (error) {
    showNotification(error.response?.data?.detail || 'Restart failed', 'error')
  } finally {
    restartAllLoading.value = false
  }
}

async function pauseAllSchedules() {
  pauseLoading.value = true
  try {
    const response = await axios.post('/api/ops/schedules/pause', {}, {
      headers: authStore.authHeader
    })
    showNotification(`Paused ${response.data.paused_count || 0} schedules`, 'success')
  } catch (error) {
    showNotification(error.response?.data?.detail || 'Failed to pause schedules', 'error')
  } finally {
    pauseLoading.value = false
  }
}

async function resumeAllSchedules() {
  resumeLoading.value = true
  try {
    const response = await axios.post('/api/ops/schedules/resume', {}, {
      headers: authStore.authHeader
    })
    showNotification(`Resumed ${response.data.resumed_count || 0} schedules`, 'success')
  } catch (error) {
    showNotification(error.response?.data?.detail || 'Failed to resume schedules', 'error')
  } finally {
    resumeLoading.value = false
  }
}

// Notifications
function showNotification(message, type = 'info') {
  notification.value = { message, type }
  setTimeout(() => {
    notification.value = null
  }, 3000)
}

// Terminal event handlers
function onTerminalConnected() {
  showNotification('Terminal connected', 'success')
}

function onTerminalDisconnected() {
  showNotification('Terminal disconnected', 'info')
}

function onTerminalError(error) {
  showNotification(`Terminal error: ${error}`, 'error')
}

// Fullscreen toggle
function toggleFullscreen() {
  isFullscreen.value = !isFullscreen.value
  // Refit terminal after layout change
  nextTick(() => {
    if (terminalRef.value?.fit) {
      terminalRef.value.fit()
    }
  })
}

// ESC key handler for fullscreen
function handleKeydown(event) {
  if (event.key === 'Escape' && isFullscreen.value) {
    toggleFullscreen()
  }
}

// Start polling intervals
function startPolling() {
  // Start polling for fleet status
  if (!pollingInterval) {
    pollingInterval = setInterval(() => {
      loadSystemAgent()
      refreshFleetStatus()
    }, 10000)
  }

  // Poll OTel metrics less frequently (every 30 seconds)
  if (!otelPollingInterval) {
    otelPollingInterval = setInterval(() => {
      refreshOtelMetrics()
    }, 30000)
  }
}

// Stop polling intervals
function stopPolling() {
  if (pollingInterval) {
    clearInterval(pollingInterval)
    pollingInterval = null
  }
  if (otelPollingInterval) {
    clearInterval(otelPollingInterval)
    otelPollingInterval = null
  }
}

// Lifecycle - onMounted fires once when first created
onMounted(async () => {
  // Add ESC key listener for fullscreen
  window.addEventListener('keydown', handleKeydown)

  // Fetch user role for admin checks
  try {
    const response = await axios.get('/api/users/me')
    userRole.value = response.data.role
  } catch (e) {
    console.warn('Failed to fetch user role:', e)
  }

  await loadSystemAgent()
  await refreshFleetStatus()
  await refreshOtelMetrics()

  startPolling()
})

// onActivated fires when component is shown (after being cached by KeepAlive)
onActivated(() => {
  // Restart polling when returning to this view
  startPolling()
  // Refresh data when returning
  loadSystemAgent()
  refreshFleetStatus()
  // Refit terminal if visible
  nextTick(() => {
    if (terminalRef.value?.fit) {
      terminalRef.value.fit()
    }
  })
})

// onDeactivated fires when navigating away (component is cached, not destroyed)
onDeactivated(() => {
  // Stop polling when navigating away (but keep WebSocket connection alive)
  stopPolling()
})

// onUnmounted fires when component is actually destroyed (e.g., logout)
onUnmounted(() => {
  // Remove ESC key listener
  window.removeEventListener('keydown', handleKeydown)
  stopPolling()
})
</script>
