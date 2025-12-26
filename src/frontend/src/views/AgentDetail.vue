<template>
  <div class="min-h-screen bg-gray-100 dark:bg-gray-900">
    <NavBar />

    <main class="max-w-7xl mx-auto py-6 sm:px-6 lg:px-8">
      <div class="px-4 py-6 sm:px-0">
        <div v-if="loading" class="text-center py-8">
          <div class="animate-spin rounded-full h-12 w-12 border-b-2 border-indigo-500 mx-auto"></div>
        </div>

        <!-- Notification Toast -->
        <div v-if="notification"
          :class="[
            'fixed top-20 right-4 z-50 px-4 py-3 rounded-lg shadow-lg transition-all duration-300',
            notification.type === 'success' ? 'bg-green-100 dark:bg-green-900/50 border border-green-400 dark:border-green-700 text-green-700 dark:text-green-300' : 'bg-red-100 dark:bg-red-900/50 border border-red-400 dark:border-red-700 text-red-700 dark:text-red-300'
          ]"
        >
          {{ notification.message }}
        </div>

        <div v-if="error && !agent" class="bg-red-100 dark:bg-red-900/50 border border-red-400 dark:border-red-700 text-red-700 dark:text-red-300 px-4 py-3 rounded mb-4">
          {{ error }}
        </div>

        <div v-if="agent">
          <!-- Agent Header - Compact -->
          <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg p-4 mb-4">
            <!-- Top row: Name, status, and actions -->
            <div class="flex justify-between items-center">
              <div class="flex items-center space-x-3">
                <h1 class="text-xl font-bold text-gray-900 dark:text-white">{{ agent.name }}</h1>
                <span :class="[
                  'px-2 py-0.5 text-xs font-medium rounded-full',
                  agent.status === 'running' ? 'bg-green-100 dark:bg-green-900/50 text-green-800 dark:text-green-300' : 'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-300'
                ]">
                  {{ agent.status }}
                </span>
                <!-- System agent badge -->
                <span
                  v-if="agent.is_system"
                  class="px-2 py-0.5 text-xs font-semibold rounded-full bg-purple-100 text-purple-700 dark:bg-purple-900/50 dark:text-purple-300"
                  title="System Agent - Platform Orchestrator with full access"
                >
                  SYSTEM
                </span>
                <span v-if="agent.is_shared" class="px-2 py-0.5 text-xs font-medium bg-blue-100 dark:bg-blue-900/50 text-blue-700 dark:text-blue-300 rounded-full">
                  Shared by {{ agent.owner }}
                </span>
                <span class="text-xs text-gray-400 dark:text-gray-500">{{ agent.type }}</span>
              </div>
              <div class="flex items-center space-x-2">
                <button
                  v-if="agent.status === 'stopped'"
                  @click="startAgent"
                  :disabled="actionLoading"
                  class="bg-green-600 hover:bg-green-700 disabled:bg-green-400 text-white text-sm font-medium py-1.5 px-3 rounded flex items-center"
                >
                  <svg v-if="actionLoading" class="animate-spin -ml-1 mr-1.5 h-3 w-3 text-white" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                  </svg>
                  {{ actionLoading ? 'Starting...' : 'Start' }}
                </button>
                <button
                  v-else
                  @click="stopAgent"
                  :disabled="actionLoading"
                  class="bg-red-600 hover:bg-red-700 disabled:bg-red-400 text-white text-sm font-medium py-1.5 px-3 rounded flex items-center"
                >
                  <svg v-if="actionLoading" class="animate-spin -ml-1 mr-1.5 h-3 w-3 text-white" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                  </svg>
                  {{ actionLoading ? 'Stopping...' : 'Stop' }}
                </button>
                <!-- Git Sync Controls (only for GitHub-native agents when running) -->
                <template v-if="hasGitSync && agent.status === 'running'">
                  <div class="h-4 w-px bg-gray-300 dark:bg-gray-600 mx-1"></div>
                  <button
                    @click="syncToGithub"
                    :disabled="gitSyncing"
                    class="inline-flex items-center text-sm font-medium py-1.5 px-3 rounded transition-colors"
                    :class="gitHasChanges
                      ? 'bg-orange-600 hover:bg-orange-700 disabled:bg-orange-400 text-white'
                      : 'bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 disabled:bg-gray-100 dark:disabled:bg-gray-800 text-gray-600 dark:text-gray-300'"
                    :title="gitHasChanges ? 'Push changes to GitHub' : 'No changes to sync'"
                  >
                    <svg v-if="gitSyncing" class="animate-spin -ml-0.5 mr-1.5 h-3.5 w-3.5" fill="none" viewBox="0 0 24 24">
                      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    <svg v-else class="w-3.5 h-3.5 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
                    </svg>
                    {{ gitSyncing ? 'Syncing...' : (gitHasChanges ? `Sync (${gitChangesCount})` : 'Synced') }}
                  </button>
                  <button
                    @click="refreshGitStatus"
                    :disabled="gitLoading"
                    class="text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 p-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-700"
                    title="Refresh git status"
                  >
                    <svg :class="['w-4 h-4', gitLoading ? 'animate-spin' : '']" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                    </svg>
                  </button>
                </template>
                <!-- Git status indicator (when stopped but has git) -->
                <template v-else-if="hasGitSync && agent.status === 'stopped'">
                  <div class="h-4 w-px bg-gray-300 dark:bg-gray-600 mx-1"></div>
                  <span class="text-xs text-gray-400 dark:text-gray-500 flex items-center">
                    <svg class="w-3.5 h-3.5 mr-1" fill="currentColor" viewBox="0 0 24 24">
                      <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
                    </svg>
                    Git enabled
                  </span>
                </template>
                <button
                  v-if="agent.can_delete"
                  @click="deleteAgent"
                  class="text-gray-400 dark:text-gray-500 hover:text-red-600 dark:hover:text-red-400 p-1.5 rounded"
                  title="Delete agent"
                >
                  <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                  </svg>
                </button>
              </div>
            </div>

            <!-- Live Stats Row (only when running) -->
            <div v-if="agent.status === 'running'" class="mt-3 pt-3 border-t border-gray-100 dark:border-gray-700">
              <div v-if="statsLoading && !agentStats" class="flex items-center space-x-2 text-xs text-gray-400 dark:text-gray-500">
                <div class="animate-spin h-3 w-3 border border-gray-300 dark:border-gray-600 border-t-gray-600 dark:border-t-gray-300 rounded-full"></div>
                <span>Loading stats...</span>
              </div>
              <div v-else-if="agentStats" class="flex items-center space-x-6 text-xs">
                <!-- CPU -->
                <div class="flex items-center space-x-2">
                  <span class="text-gray-400 dark:text-gray-500 w-8">CPU</span>
                  <div class="w-20 h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
                    <div
                      class="h-full rounded-full transition-all duration-500 animate-progress-pulse"
                      :class="agentStats.cpu_percent > 80 ? 'bg-red-500' : agentStats.cpu_percent > 50 ? 'bg-yellow-500' : 'bg-green-500'"
                      :style="{ width: `${Math.min(100, agentStats.cpu_percent)}%` }"
                    ></div>
                  </div>
                  <span class="text-gray-600 dark:text-gray-400 font-mono w-12 text-right">{{ agentStats.cpu_percent }}%</span>
                </div>
                <!-- Memory -->
                <div class="flex items-center space-x-2">
                  <span class="text-gray-400 dark:text-gray-500 w-8">MEM</span>
                  <div class="w-20 h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
                    <div
                      class="h-full rounded-full transition-all duration-500 animate-progress-pulse"
                      :class="agentStats.memory_percent > 80 ? 'bg-red-500' : agentStats.memory_percent > 50 ? 'bg-yellow-500' : 'bg-green-500'"
                      :style="{ width: `${Math.min(100, agentStats.memory_percent)}%` }"
                    ></div>
                  </div>
                  <span class="text-gray-600 dark:text-gray-400 font-mono w-20 text-right">{{ formatBytes(agentStats.memory_used_bytes) }}</span>
                </div>
                <!-- Network -->
                <div class="flex items-center space-x-1.5 text-gray-500 dark:text-gray-400">
                  <span class="text-gray-400 dark:text-gray-500">NET</span>
                  <span class="text-green-600 dark:text-green-400">↓{{ formatBytes(agentStats.network_rx_bytes) }}</span>
                  <span class="text-blue-600 dark:text-blue-400">↑{{ formatBytes(agentStats.network_tx_bytes) }}</span>
                </div>
                <!-- Uptime -->
                <div class="flex items-center space-x-1.5 text-gray-500 dark:text-gray-400">
                  <span class="text-gray-400 dark:text-gray-500">UP</span>
                  <span class="font-mono">{{ formatUptime(agentStats.uptime_seconds) }}</span>
                </div>
              </div>
              <div v-else class="text-xs text-gray-400 dark:text-gray-500">Stats unavailable</div>
            </div>

            <!-- Stopped state info -->
            <div v-else class="mt-2 text-xs text-gray-400 dark:text-gray-500">
              Created {{ formatRelativeTime(agent.created) }}
            </div>
          </div>

          <!-- Tabs -->
          <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
            <div class="border-b border-gray-200 dark:border-gray-700 overflow-x-auto">
              <nav class="-mb-px flex whitespace-nowrap">
                <button
                  @click="activeTab = 'info'"
                  :class="[
                    'px-4 py-3 border-b-2 font-medium text-sm transition-colors whitespace-nowrap',
                    activeTab === 'info'
                      ? 'border-indigo-500 text-indigo-600 dark:text-indigo-400'
                      : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-600'
                  ]"
                >
                  Info
                </button>
                <button
                  @click="activeTab = 'metrics'"
                  :class="[
                    'px-4 py-3 border-b-2 font-medium text-sm transition-colors whitespace-nowrap',
                    activeTab === 'metrics'
                      ? 'border-indigo-500 text-indigo-600 dark:text-indigo-400'
                      : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-600'
                  ]"
                  title="Agent-specific KPIs and stats"
                >
                  Metrics
                </button>
                <button
                  @click="activeTab = 'terminal'"
                  :class="[
                    'px-4 py-3 border-b-2 font-medium text-sm transition-colors whitespace-nowrap',
                    activeTab === 'terminal'
                      ? 'border-indigo-500 text-indigo-600 dark:text-indigo-400'
                      : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-600'
                  ]"
                >
                  Terminal
                </button>
                <button
                  @click="activeTab = 'logs'"
                  :class="[
                    'px-4 py-3 border-b-2 font-medium text-sm transition-colors whitespace-nowrap',
                    activeTab === 'logs'
                      ? 'border-indigo-500 text-indigo-600 dark:text-indigo-400'
                      : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-600'
                  ]"
                >
                  Logs
                </button>
                <button
                  @click="activeTab = 'credentials'"
                  :class="[
                    'inline-flex items-center px-4 py-3 border-b-2 font-medium text-sm transition-colors whitespace-nowrap',
                    activeTab === 'credentials'
                      ? 'border-indigo-500 text-indigo-600 dark:text-indigo-400'
                      : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-600'
                  ]"
                >
                  Credentials
                  <span v-if="credentialsData && credentialsData.missing_count > 0" class="ml-1.5 px-1.5 py-0.5 text-[10px] font-semibold bg-red-100 dark:bg-red-900/50 text-red-700 dark:text-red-300 rounded-full leading-none">
                    {{ credentialsData.missing_count }}
                  </span>
                </button>
                <button
                  v-if="agent.can_share"
                  @click="activeTab = 'sharing'"
                  :class="[
                    'inline-flex items-center px-4 py-3 border-b-2 font-medium text-sm transition-colors whitespace-nowrap',
                    activeTab === 'sharing'
                      ? 'border-indigo-500 text-indigo-600 dark:text-indigo-400'
                      : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-600'
                  ]"
                >
                  Sharing
                </button>
                <button
                  v-if="agent.can_share"
                  @click="activeTab = 'permissions'"
                  :class="[
                    'inline-flex items-center px-4 py-3 border-b-2 font-medium text-sm transition-colors whitespace-nowrap',
                    activeTab === 'permissions'
                      ? 'border-indigo-500 text-indigo-600 dark:text-indigo-400'
                      : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-600'
                  ]"
                >
                  Permissions
                </button>
                <button
                  @click="activeTab = 'schedules'"
                  :class="[
                    'px-4 py-3 border-b-2 font-medium text-sm transition-colors whitespace-nowrap',
                    activeTab === 'schedules'
                      ? 'border-indigo-500 text-indigo-600 dark:text-indigo-400'
                      : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-600'
                  ]"
                >
                  Schedules
                </button>
                <button
                  @click="activeTab = 'executions'"
                  :class="[
                    'px-4 py-3 border-b-2 font-medium text-sm transition-colors whitespace-nowrap',
                    activeTab === 'executions'
                      ? 'border-indigo-500 text-indigo-600 dark:text-indigo-400'
                      : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-600'
                  ]"
                >
                  Executions
                </button>
                <button
                  v-if="hasGitSync"
                  @click="activeTab = 'git'"
                  :class="[
                    'px-4 py-3 border-b-2 font-medium text-sm transition-colors whitespace-nowrap',
                    activeTab === 'git'
                      ? 'border-indigo-500 text-indigo-600 dark:text-indigo-400'
                      : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-600'
                  ]"
                >
                  Git
                </button>
                <button
                  @click="activeTab = 'files'"
                  :class="[
                    'px-4 py-3 border-b-2 font-medium text-sm transition-colors whitespace-nowrap',
                    activeTab === 'files'
                      ? 'border-indigo-500 text-indigo-600 dark:text-indigo-400'
                      : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-600'
                  ]"
                >
                  Files
                </button>
                <button
                  v-if="agent.can_share"
                  @click="activeTab = 'folders'"
                  :class="[
                    'px-4 py-3 border-b-2 font-medium text-sm transition-colors whitespace-nowrap',
                    activeTab === 'folders'
                      ? 'border-indigo-500 text-indigo-600 dark:text-indigo-400'
                      : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-600'
                  ]"
                  title="Shared folders for file-based collaboration"
                >
                  Folders
                </button>
                <button
                  v-if="agent.can_share"
                  @click="activeTab = 'public-links'"
                  :class="[
                    'px-4 py-3 border-b-2 font-medium text-sm transition-colors whitespace-nowrap',
                    activeTab === 'public-links'
                      ? 'border-indigo-500 text-indigo-600 dark:text-indigo-400'
                      : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-600'
                  ]"
                  title="Generate public shareable links"
                >
                  Public Links
                </button>
              </nav>
            </div>

            <!-- Info Tab Content -->
            <div v-if="activeTab === 'info'" class="p-6">
              <InfoPanel :agent-name="agent.name" :agent-status="agent.status" @use-case-click="handleUseCaseClick" />
            </div>

            <!-- Metrics Tab Content -->
            <div v-if="activeTab === 'metrics'" class="p-6">
              <MetricsPanel :agent-name="agent.name" :agent-status="agent.status" />
            </div>

            <!-- Terminal Tab Content -->
            <div
              v-if="activeTab === 'terminal'"
              :class="[
                'transition-all duration-300',
                isTerminalFullscreen
                  ? 'fixed inset-0 z-50 bg-gray-900'
                  : ''
              ]"
              @keydown="handleTerminalKeydown"
            >
              <!-- Terminal Panel -->
              <div
                :class="[
                  'bg-gray-900 overflow-hidden flex flex-col',
                  isTerminalFullscreen ? 'h-full' : 'rounded-b-lg'
                ]"
                :style="isTerminalFullscreen ? {} : { height: '600px' }"
              >
                <!-- Fullscreen Header (only in fullscreen mode) -->
                <div
                  v-if="isTerminalFullscreen"
                  class="flex items-center justify-between px-4 py-2 bg-gray-800 border-b border-gray-700"
                >
                  <span class="text-sm font-medium text-gray-300">{{ agent.name }} - Terminal</span>
                  <button
                    @click="toggleTerminalFullscreen"
                    class="p-1.5 text-gray-400 hover:text-gray-200 rounded transition-colors"
                    title="Exit Fullscreen (Esc)"
                  >
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 9V4.5M9 9H4.5M9 9L3.75 3.75M9 15v4.5M9 15H4.5M9 15l-5.25 5.25M15 9h4.5M15 9V4.5M15 9l5.25-5.25M15 15h4.5M15 15v4.5m0-4.5l5.25 5.25" />
                    </svg>
                  </button>
                </div>

                <!-- Terminal Content -->
                <div class="flex-1 min-h-0">
                  <AgentTerminal
                    v-if="agent.status === 'running'"
                    ref="terminalRef"
                    :agent-name="agent.name"
                    :auto-connect="true"
                    :show-fullscreen-toggle="!isTerminalFullscreen"
                    :is-fullscreen="isTerminalFullscreen"
                    @connected="onTerminalConnected"
                    @disconnected="onTerminalDisconnected"
                    @error="onTerminalError"
                    @toggle-fullscreen="toggleTerminalFullscreen"
                  />
                  <div
                    v-else
                    class="flex items-center justify-center h-full text-gray-400"
                  >
                    <div class="text-center max-w-md">
                      <svg class="w-12 h-12 mx-auto mb-4 text-gray-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
                      </svg>
                      <p class="text-lg font-medium mb-2">Agent is not running</p>
                      <p class="text-sm text-gray-500 mb-4">Start the agent to access the terminal</p>

                      <!-- API Key Setting -->
                      <div v-if="agent.can_share" class="mb-4 p-4 bg-gray-800 rounded-lg text-left">
                        <div class="flex items-center justify-between mb-2">
                          <span class="text-sm font-medium text-gray-300">Authentication</span>
                          <span v-if="apiKeySetting.restart_required" class="text-xs text-yellow-400">Restart required</span>
                        </div>
                        <div class="space-y-2">
                          <label class="flex items-center space-x-3 cursor-pointer">
                            <input
                              type="radio"
                              :checked="apiKeySetting.use_platform_api_key"
                              @change="updateApiKeySetting(true)"
                              :disabled="apiKeySettingLoading"
                              class="text-indigo-500 focus:ring-indigo-500"
                            />
                            <div>
                              <span class="text-sm text-gray-200">Use Platform API Key</span>
                              <p class="text-xs text-gray-500">Claude uses Trinity's configured Anthropic API key</p>
                            </div>
                          </label>
                          <label class="flex items-center space-x-3 cursor-pointer">
                            <input
                              type="radio"
                              :checked="!apiKeySetting.use_platform_api_key"
                              @change="updateApiKeySetting(false)"
                              :disabled="apiKeySettingLoading"
                              class="text-indigo-500 focus:ring-indigo-500"
                            />
                            <div>
                              <span class="text-sm text-gray-200">Authenticate in Terminal</span>
                              <p class="text-xs text-gray-500">Run "claude login" after starting to use your own subscription</p>
                            </div>
                          </label>
                        </div>
                      </div>

                      <button
                        @click="startAgent"
                        :disabled="actionLoading"
                        class="bg-green-600 hover:bg-green-700 disabled:bg-green-400 text-white text-sm font-medium py-2 px-4 rounded-lg"
                      >
                        {{ actionLoading ? 'Starting...' : 'Start Agent' }}
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <!-- Logs Tab Content -->
            <div v-if="activeTab === 'logs'" class="p-6">
              <div
                ref="logsContainer"
                @scroll="handleLogsScroll"
                class="bg-gray-900 text-gray-300 p-4 rounded-lg h-96 overflow-auto mb-4"
              >
                <pre class="text-sm font-mono whitespace-pre-wrap">{{ logs || 'No logs available' }}</pre>
              </div>
              <div class="flex justify-between items-center">
                <div class="flex items-center space-x-4">
                  <button
                    @click="refreshLogs"
                    class="bg-blue-600 hover:bg-blue-700 text-white font-bold py-2 px-4 rounded"
                  >
                    Refresh Logs
                  </button>
                  <label class="flex items-center space-x-2 text-sm text-gray-600 dark:text-gray-400">
                    <input type="checkbox" v-model="autoRefreshLogs" class="rounded border-gray-300 dark:border-gray-600 dark:bg-gray-700" />
                    <span>Auto-refresh (10s)</span>
                  </label>
                </div>
                <div class="flex items-center space-x-2">
                  <label class="text-sm text-gray-600 dark:text-gray-400">Lines:</label>
                  <select v-model="logLines" @change="refreshLogs" class="border-gray-300 dark:border-gray-600 rounded-md shadow-sm text-sm bg-white dark:bg-gray-800 dark:text-gray-200">
                    <option value="50">50</option>
                    <option value="100">100</option>
                    <option value="200">200</option>
                    <option value="500">500</option>
                  </select>
                </div>
              </div>
            </div>

            <!-- Credentials Tab Content -->
            <div v-if="activeTab === 'credentials'" class="p-6">
              <div v-if="credentialsLoading" class="text-center py-8">
                <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-indigo-500 mx-auto"></div>
                <p class="text-gray-500 dark:text-gray-400 mt-2">Loading credential requirements...</p>
              </div>

              <div v-else-if="!credentialsData || !credentialsData.template">
                <div class="text-center py-8 text-gray-500 dark:text-gray-400">
                  <p>No template associated with this agent.</p>
                  <p class="text-sm mt-2">Credential requirements are based on agent templates.</p>
                </div>
              </div>

              <div v-else>
                <div class="mb-6">
                  <div class="flex justify-between items-center">
                    <div>
                      <h3 class="text-lg font-medium text-gray-900 dark:text-white">Required Credentials</h3>
                      <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">
                        Template: <span class="font-mono bg-gray-100 dark:bg-gray-700 px-2 py-0.5 rounded">{{ credentialsData.template }}</span>
                      </p>
                    </div>
                    <div class="text-right">
                      <div class="flex items-center space-x-4">
                        <span class="text-sm">
                          <span class="text-green-600 dark:text-green-400 font-semibold">{{ credentialsData.configured_count }}</span>
                          <span class="text-gray-500 dark:text-gray-400"> configured</span>
                        </span>
                        <span class="text-sm">
                          <span class="text-red-600 dark:text-red-400 font-semibold">{{ credentialsData.missing_count }}</span>
                          <span class="text-gray-500 dark:text-gray-400"> missing</span>
                        </span>
                      </div>
                    </div>
                  </div>
                </div>

                <!-- Progress bar -->
                <div class="mb-6">
                  <div class="flex justify-between text-sm text-gray-600 dark:text-gray-400 mb-1">
                    <span>Configuration Progress</span>
                    <span>{{ Math.round((credentialsData.configured_count / credentialsData.total) * 100) }}%</span>
                  </div>
                  <div class="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2">
                    <div
                      class="bg-green-600 h-2 rounded-full transition-all duration-300"
                      :style="{ width: `${(credentialsData.configured_count / credentialsData.total) * 100}%` }"
                    ></div>
                  </div>
                </div>

                <!-- Credentials list -->
                <div class="space-y-3">
                  <div
                    v-for="cred in credentialsData.required_credentials"
                    :key="cred.name"
                    class="flex items-center justify-between p-4 bg-gray-50 dark:bg-gray-900 rounded-lg border"
                    :class="cred.configured ? 'border-green-200 dark:border-green-800' : 'border-red-200 dark:border-red-800'"
                  >
                    <div class="flex items-center space-x-3">
                      <!-- Status icon -->
                      <div :class="cred.configured ? 'text-green-500' : 'text-red-500'">
                        <svg v-if="cred.configured" class="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                          <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
                        </svg>
                        <svg v-else class="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                          <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd" />
                        </svg>
                      </div>
                      <div>
                        <p class="font-mono text-sm text-gray-900 dark:text-gray-100">{{ cred.name }}</p>
                        <p class="text-xs text-gray-500 dark:text-gray-400">{{ formatSource(cred.source) }}</p>
                      </div>
                    </div>
                    <div>
                      <span
                        :class="[
                          'px-2 py-1 text-xs font-medium rounded',
                          cred.configured
                            ? 'bg-green-100 dark:bg-green-900/50 text-green-800 dark:text-green-300'
                            : 'bg-red-100 dark:bg-red-900/50 text-red-800 dark:text-red-300'
                        ]"
                      >
                        {{ cred.configured ? 'Configured' : 'Missing' }}
                      </span>
                    </div>
                  </div>
                </div>

                <!-- Action button -->
                <div v-if="credentialsData.missing_count > 0" class="mt-6 flex justify-center">
                  <router-link
                    to="/credentials"
                    class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500"
                  >
                    <svg class="w-4 h-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6m0 0v6m0-6h6m-6 0H6" />
                    </svg>
                    Configure Missing Credentials
                  </router-link>
                </div>
              </div>

              <!-- Hot Reload Section -->
              <div class="mt-8 border-t dark:border-gray-700 pt-6">
                <div class="flex items-center justify-between mb-4">
                  <div>
                    <h3 class="text-lg font-medium text-gray-900 dark:text-white">Hot Reload Credentials</h3>
                    <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">
                      Paste .env-style credentials to update the running agent instantly
                    </p>
                  </div>
                  <span
                    v-if="agent.status !== 'running'"
                    class="px-3 py-1 text-sm bg-yellow-100 dark:bg-yellow-900/50 text-yellow-800 dark:text-yellow-300 rounded-full"
                  >
                    Agent must be running
                  </span>
                </div>

                <div class="space-y-4">
                  <textarea
                    v-model="hotReloadText"
                    :disabled="agent.status !== 'running' || hotReloadLoading"
                    rows="8"
                    placeholder="# Paste credentials in KEY=VALUE format
TWITTER_API_KEY=your_api_key_here
TWITTER_API_SECRET=your_secret_here
HEYGEN_API_KEY=your_heygen_key

# Lines starting with # are ignored"
                    class="w-full font-mono text-sm border border-gray-300 dark:border-gray-600 rounded-lg p-4 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:bg-gray-100 dark:disabled:bg-gray-800 disabled:cursor-not-allowed"
                  ></textarea>

                  <div class="flex items-center justify-between">
                    <div class="text-sm text-gray-500 dark:text-gray-400">
                      {{ hotReloadText ? countCredentials(hotReloadText) : 0 }} credentials detected
                    </div>
                    <button
                      @click="performHotReload"
                      :disabled="agent.status !== 'running' || hotReloadLoading || !hotReloadText.trim()"
                      class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-orange-600 hover:bg-orange-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-orange-500 disabled:bg-gray-400 disabled:cursor-not-allowed"
                    >
                      <svg v-if="hotReloadLoading" class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                      </svg>
                      <svg v-else class="w-4 h-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                      </svg>
                      {{ hotReloadLoading ? 'Updating...' : 'Hot Reload' }}
                    </button>
                  </div>

                  <!-- Hot reload result -->
                  <div
                    v-if="hotReloadResult"
                    :class="[
                      'p-4 rounded-lg',
                      hotReloadResult.success ? 'bg-green-50 dark:bg-green-900/30 border border-green-200 dark:border-green-800' : 'bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800'
                    ]"
                  >
                    <div class="flex items-start">
                      <svg v-if="hotReloadResult.success" class="w-5 h-5 text-green-500 mr-2 mt-0.5" fill="currentColor" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
                      </svg>
                      <svg v-else class="w-5 h-5 text-red-500 mr-2 mt-0.5" fill="currentColor" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd" />
                      </svg>
                      <div>
                        <p :class="hotReloadResult.success ? 'text-green-800 dark:text-green-300' : 'text-red-800 dark:text-red-300'" class="font-medium">
                          {{ hotReloadResult.message }}
                        </p>
                        <p v-if="hotReloadResult.credentials" class="text-sm text-gray-600 dark:text-gray-400 mt-1">
                          Updated: {{ hotReloadResult.credentials.join(', ') }}
                        </p>
                        <p v-if="hotReloadResult.note" class="text-sm text-gray-500 dark:text-gray-400 mt-1">
                          {{ hotReloadResult.note }}
                        </p>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <!-- Sharing Tab Content -->
            <div v-if="activeTab === 'sharing' && agent.can_share" class="p-6">
              <div class="mb-6">
                <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Share Agent</h3>
                <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
                  Share this agent with other team members by entering their email address.
                </p>

                <!-- Share Form -->
                <form @submit.prevent="shareWithUser" class="flex items-center space-x-3">
                  <input
                    v-model="shareEmail"
                    type="email"
                    placeholder="user@ability.ai"
                    :disabled="shareLoading"
                    class="flex-1 max-w-md px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:bg-gray-100 dark:disabled:bg-gray-900"
                  />
                  <button
                    type="submit"
                    :disabled="shareLoading || !shareEmail.trim()"
                    class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 dark:focus:ring-offset-gray-800 disabled:bg-gray-400 dark:disabled:bg-gray-600 disabled:cursor-not-allowed"
                  >
                    <svg v-if="shareLoading" class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
                      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    {{ shareLoading ? 'Sharing...' : 'Share' }}
                  </button>
                </form>

                <!-- Share Error/Success Message -->
                <div v-if="shareMessage" :class="[
                  'mt-3 p-3 rounded-lg text-sm',
                  shareMessage.type === 'success' ? 'bg-green-50 dark:bg-green-900/30 text-green-700 dark:text-green-300' : 'bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-300'
                ]">
                  {{ shareMessage.text }}
                </div>
              </div>

              <!-- Shared Users List -->
              <div>
                <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-4">Shared With</h3>

                <div v-if="!agent.shares || agent.shares.length === 0" class="text-center py-8 text-gray-500 dark:text-gray-400">
                  <svg class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
                  </svg>
                  <p class="mt-2">Not shared with anyone</p>
                </div>

                <ul v-else class="divide-y divide-gray-200 dark:divide-gray-700 border border-gray-200 dark:border-gray-700 rounded-lg">
                  <li v-for="share in agent.shares" :key="share.id" class="px-4 py-3 flex items-center justify-between">
                    <div class="flex items-center space-x-3">
                      <div class="flex-shrink-0 h-8 w-8 bg-gray-200 dark:bg-gray-700 rounded-full flex items-center justify-center">
                        <span class="text-sm font-medium text-gray-600 dark:text-gray-300">
                          {{ (share.shared_with_name || share.shared_with_email || '?')[0].toUpperCase() }}
                        </span>
                      </div>
                      <div>
                        <p class="text-sm font-medium text-gray-900 dark:text-gray-100">
                          {{ share.shared_with_name || share.shared_with_email }}
                        </p>
                        <p v-if="share.shared_with_name" class="text-xs text-gray-500 dark:text-gray-400">
                          {{ share.shared_with_email }}
                        </p>
                      </div>
                    </div>
                    <button
                      @click="removeShare(share.shared_with_email)"
                      :disabled="unshareLoading === share.shared_with_email"
                      class="text-red-600 dark:text-red-400 hover:text-red-800 dark:hover:text-red-300 text-sm font-medium disabled:opacity-50"
                    >
                      <span v-if="unshareLoading === share.shared_with_email">Removing...</span>
                      <span v-else>Remove</span>
                    </button>
                  </li>
                </ul>
              </div>
            </div>

            <!-- Permissions Tab Content (Phase 9.10) -->
            <div v-if="activeTab === 'permissions' && agent.can_share" class="p-6">
              <div class="mb-6">
                <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Agent Collaboration Permissions</h3>
                <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
                  Control which other agents this agent can communicate with via the Trinity MCP tools.
                </p>

                <!-- Loading State -->
                <div v-if="permissionsLoading" class="text-center py-4">
                  <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-indigo-500 mx-auto"></div>
                  <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">Loading permissions...</p>
                </div>

                <!-- Permissions List -->
                <div v-else-if="availableAgents.length > 0">
                  <!-- Bulk Actions -->
                  <div class="flex items-center space-x-3 mb-4">
                    <button
                      @click="allowAllAgents"
                      :disabled="permissionsSaving"
                      class="text-sm text-indigo-600 dark:text-indigo-400 hover:text-indigo-800 dark:hover:text-indigo-300 font-medium disabled:opacity-50"
                    >
                      Allow All
                    </button>
                    <span class="text-gray-300 dark:text-gray-600">|</span>
                    <button
                      @click="allowNoAgents"
                      :disabled="permissionsSaving"
                      class="text-sm text-red-600 dark:text-red-400 hover:text-red-800 dark:hover:text-red-300 font-medium disabled:opacity-50"
                    >
                      Allow None
                    </button>
                    <span v-if="permissionsDirty" class="text-amber-600 dark:text-amber-400 text-xs ml-4">
                      Unsaved changes
                    </span>
                  </div>

                  <!-- Agent Checkboxes -->
                  <ul class="divide-y divide-gray-200 dark:divide-gray-700 border border-gray-200 dark:border-gray-700 rounded-lg mb-4">
                    <li v-for="targetAgent in availableAgents" :key="targetAgent.name" class="px-4 py-3 flex items-center justify-between hover:bg-gray-50 dark:hover:bg-gray-700">
                      <label :for="'perm-' + targetAgent.name" class="flex items-center space-x-3 cursor-pointer flex-1">
                        <input
                          :id="'perm-' + targetAgent.name"
                          type="checkbox"
                          v-model="targetAgent.permitted"
                          @change="permissionsDirty = true"
                          :disabled="permissionsSaving"
                          class="h-4 w-4 text-indigo-600 focus:ring-indigo-500 border-gray-300 dark:border-gray-600 rounded dark:bg-gray-700"
                        />
                        <div class="flex-1">
                          <span class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ targetAgent.name }}</span>
                          <span v-if="targetAgent.type" class="ml-2 text-xs text-gray-500 dark:text-gray-400">[{{ targetAgent.type }}]</span>
                        </div>
                        <span :class="[
                          'px-2 py-0.5 text-xs font-medium rounded-full',
                          targetAgent.status === 'running' ? 'bg-green-100 dark:bg-green-900/50 text-green-800 dark:text-green-300' : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400'
                        ]">
                          {{ targetAgent.status }}
                        </span>
                      </label>
                    </li>
                  </ul>

                  <!-- Save Button -->
                  <div class="flex items-center space-x-3">
                    <button
                      @click="savePermissions"
                      :disabled="permissionsSaving || !permissionsDirty"
                      class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 dark:focus:ring-offset-gray-800 disabled:bg-gray-400 dark:disabled:bg-gray-600 disabled:cursor-not-allowed"
                    >
                      <svg v-if="permissionsSaving" class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                      </svg>
                      {{ permissionsSaving ? 'Saving...' : 'Save Permissions' }}
                    </button>

                    <!-- Status Message -->
                    <div v-if="permissionsMessage" :class="[
                      'text-sm',
                      permissionsMessage.type === 'success' ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'
                    ]">
                      {{ permissionsMessage.text }}
                    </div>
                  </div>
                </div>

                <!-- No Other Agents -->
                <div v-else class="text-center py-8 text-gray-500 dark:text-gray-400">
                  <svg class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
                  </svg>
                  <p class="mt-2">No other agents available</p>
                  <p class="text-xs">Create more agents to enable collaboration permissions.</p>
                </div>
              </div>
            </div>

            <!-- Schedules Tab Content -->
            <div v-if="activeTab === 'schedules'" class="p-6">
              <SchedulesPanel :agent-name="agent.name" />
            </div>

            <!-- Executions Tab Content -->
            <div v-if="activeTab === 'executions'" class="p-6">
              <ExecutionsPanel :agent-name="agent.name" />
            </div>

            <!-- Git Tab Content -->
            <div v-if="activeTab === 'git'" class="p-6">
              <GitPanel :agent-name="agent.name" :agent-status="agent.status" />
            </div>

            <!-- Files Tab Content -->
            <div v-if="activeTab === 'files'" class="p-6">
              <div v-if="agent.status !== 'running'" class="text-center py-12">
                <svg class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                </svg>
                <p class="mt-4 text-gray-500 dark:text-gray-400">Agent must be running to browse files</p>
              </div>
              <div v-else>
                <!-- Search Box -->
                <div class="mb-4">
                  <input
                    v-model="fileSearchQuery"
                    type="text"
                    placeholder="Search files by name..."
                    class="w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  />
                </div>

                <!-- File Tree -->
                <div v-if="filesLoading" class="text-center py-8">
                  <svg class="animate-spin h-8 w-8 mx-auto text-indigo-600" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                  </svg>
                  <p class="mt-2 text-gray-500 dark:text-gray-400">Loading files...</p>
                </div>

                <div v-else-if="filesError" class="text-center py-8">
                  <p class="text-red-600 dark:text-red-400">{{ filesError }}</p>
                  <button @click="loadFiles" class="mt-4 px-4 py-2 bg-indigo-600 text-white rounded hover:bg-indigo-700">
                    Retry
                  </button>
                </div>

                <div v-else-if="fileTree.length === 0" class="text-center py-8 text-gray-500 dark:text-gray-400">
                  <p>No files found in workspace</p>
                </div>

                <div v-else>
                  <div class="flex items-center justify-between mb-4 text-sm text-gray-600 dark:text-gray-400">
                    <span>{{ totalFileCount }} file(s)</span>
                    <button @click="loadFiles" class="text-indigo-600 dark:text-indigo-400 hover:text-indigo-800 dark:hover:text-indigo-300">
                      <svg class="w-4 h-4 inline" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                      </svg>
                      Refresh
                    </button>
                  </div>

                  <!-- Recursive File Tree Component -->
                  <div class="space-y-1">
                    <FileTreeNode
                      v-for="item in filteredFileTree"
                      :key="item.path"
                      :item="item"
                      :depth="0"
                      :search-query="fileSearchQuery"
                      :expanded-folders="expandedFolders"
                      @toggle-folder="toggleFolder"
                      @download="downloadFile"
                    />
                  </div>
                </div>
              </div>
            </div>

            <!-- Shared Folders Tab Content -->
            <div v-if="activeTab === 'folders'" class="p-6">
              <FoldersPanel :agent-name="agent.name" :agent-status="agent.status" :can-share="agent.can_share" />
            </div>

            <!-- Public Links Tab Content -->
            <div v-if="activeTab === 'public-links'" class="p-6">
              <PublicLinksPanel :agent-name="agent.name" />
            </div>
          </div>
        </div>
      </div>
    </main>

    <!-- Confirm Dialog -->
    <ConfirmDialog
      v-model:visible="confirmDialog.visible"
      :title="confirmDialog.title"
      :message="confirmDialog.message"
      :confirm-text="confirmDialog.confirmText"
      :variant="confirmDialog.variant"
      @confirm="confirmDialog.onConfirm"
    />
  </div>

  </template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted, watch, nextTick, defineComponent, h } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAgentsStore } from '../stores/agents'
import { marked } from 'marked'
import NavBar from '../components/NavBar.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import UnifiedActivityPanel from '../components/UnifiedActivityPanel.vue'
import SchedulesPanel from '../components/SchedulesPanel.vue'
import ExecutionsPanel from '../components/ExecutionsPanel.vue'
import GitPanel from '../components/GitPanel.vue'
import InfoPanel from '../components/InfoPanel.vue'
import MetricsPanel from '../components/MetricsPanel.vue'
import FoldersPanel from '../components/FoldersPanel.vue'
import PublicLinksPanel from '../components/PublicLinksPanel.vue'
import AgentTerminal from '../components/AgentTerminal.vue'

// Configure marked for safe rendering
marked.setOptions({
  breaks: true,
  gfm: true
})

// Helper function for file sizes
const formatFileSizeHelper = (bytes) => {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i]
}

// File Tree Node Component (Recursive)
const FileTreeNode = defineComponent({
  name: 'FileTreeNode',
  props: {
    item: Object,
    depth: Number,
    searchQuery: String,
    expandedFolders: Set
  },
  emits: ['toggle-folder', 'download'],
  setup(props, { emit }) {
    const isExpanded = () => props.expandedFolders.has(props.item.path)
    const indent = props.depth * 20

    const toggleFolder = () => {
      emit('toggle-folder', props.item.path)
    }

    const downloadFile = () => {
      emit('download', props.item.path, props.item.name)
    }

    return () => {
      if (props.item.type === 'directory') {
        return h('div', [
          // Folder row
          h('div', {
            class: 'flex items-center py-1.5 px-2 hover:bg-gray-50 dark:hover:bg-gray-700 rounded cursor-pointer group',
            style: { paddingLeft: `${indent}px` },
            onClick: toggleFolder
          }, [
            // Expand/collapse icon
            h('svg', {
              class: `w-4 h-4 mr-1 text-gray-500 dark:text-gray-400 transition-transform ${isExpanded() ? 'rotate-90' : ''}`,
              fill: 'currentColor',
              viewBox: '0 0 20 20'
            }, [
              h('path', {
                'fill-rule': 'evenodd',
                d: 'M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z',
                'clip-rule': 'evenodd'
              })
            ]),
            // Folder icon
            h('svg', {
              class: `w-5 h-5 mr-2 ${isExpanded() ? 'text-indigo-500 dark:text-indigo-400' : 'text-gray-400 dark:text-gray-500'}`,
              fill: 'currentColor',
              viewBox: '0 0 20 20'
            }, [
              h('path', {
                d: 'M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z'
              })
            ]),
            // Folder name
            h('span', {
              class: 'font-medium text-gray-700 dark:text-gray-200 flex-1'
            }, props.item.name),
            // File count badge
            h('span', {
              class: 'text-xs text-gray-500 dark:text-gray-400 mr-2'
            }, `${props.item.file_count}`)
          ]),
          // Children (when expanded)
          isExpanded() && props.item.children && props.item.children.length > 0
            ? h('div', props.item.children.map(child =>
                h(FileTreeNode, {
                  key: child.path,
                  item: child,
                  depth: props.depth + 1,
                  searchQuery: props.searchQuery,
                  expandedFolders: props.expandedFolders,
                  onToggleFolder: (path) => emit('toggle-folder', path),
                  onDownload: (path, name) => emit('download', path, name)
                })
              ))
            : null
        ])
      } else {
        // File row
        return h('div', {
          class: 'flex items-center py-1.5 px-2 hover:bg-gray-50 dark:hover:bg-gray-700 rounded group',
          style: { paddingLeft: `${indent + 20}px` }
        }, [
          // File icon
          h('svg', {
            class: 'w-4 h-4 mr-2 text-gray-400 dark:text-gray-500',
            fill: 'currentColor',
            viewBox: '0 0 20 20'
          }, [
            h('path', {
              'fill-rule': 'evenodd',
              d: 'M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4z',
              'clip-rule': 'evenodd'
            })
          ]),
          // File name
          h('span', {
            class: 'text-gray-700 dark:text-gray-200 flex-1 truncate'
          }, props.item.name),
          // File size
          h('span', {
            class: 'text-xs text-gray-500 dark:text-gray-400 mr-4'
          }, formatFileSizeHelper(props.item.size)),
          // Download button
          h('button', {
            class: 'p-1 text-indigo-600 dark:text-indigo-400 opacity-0 group-hover:opacity-100 transition-opacity hover:bg-indigo-50 dark:hover:bg-indigo-900/30 rounded',
            title: 'Download file',
            onClick: downloadFile
          }, [
            h('svg', {
              class: 'w-4 h-4',
              fill: 'none',
              stroke: 'currentColor',
              viewBox: '0 0 24 24'
            }, [
              h('path', {
                'stroke-linecap': 'round',
                'stroke-linejoin': 'round',
                'stroke-width': '2',
                d: 'M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z'
              })
            ])
          ])
        ])
      }
    }
  }
})

const route = useRoute()
const router = useRouter()
const agentsStore = useAgentsStore()

const agent = ref(null)
const logs = ref('')
const logLines = ref(100)
const loading = ref(true)
const error = ref('')
const chatMessages = ref([])
const chatInput = ref('')
const chatLoading = ref(false)
const activeTab = ref('info')
const notification = ref(null)
const actionLoading = ref(false)
const autoRefreshLogs = ref(false)
const logsContainer = ref(null)
const userScrolledUp = ref(false)
const credentialsData = ref(null)
const credentialsLoading = ref(false)
const hotReloadText = ref('')
const hotReloadLoading = ref(false)
const hotReloadResult = ref(null)
const newSessionLoading = ref(false)
const sessionInfo = ref({
  context_tokens: 0,
  context_window: 200000,
  context_percent: 0,
  total_cost_usd: 0,
  message_count: 0
})

// Confirm dialog state
const confirmDialog = reactive({
  visible: false,
  title: '',
  message: '',
  confirmText: 'Confirm',
  variant: 'danger',
  onConfirm: () => {}
})

// API Key settings
const apiKeySetting = ref({
  use_platform_api_key: true,
  restart_required: false
})
const apiKeySettingLoading = ref(false)
const currentModel = ref('')  // Model selection: '', 'sonnet', 'opus', 'haiku', etc.
const modelLoading = ref(false)
let logsRefreshInterval = null

// Live stats
const agentStats = ref(null)
const statsLoading = ref(false)
let statsRefreshInterval = null

// Chat container ref
const chatContainer = ref(null)

// Terminal state
const isTerminalFullscreen = ref(false)
const terminalRef = ref(null)

// Sharing state
const shareEmail = ref('')
const shareLoading = ref(false)
const shareMessage = ref(null)
const unshareLoading = ref(null)

// Permissions state (Phase 9.10)
const availableAgents = ref([])
const permissionsLoading = ref(false)
const permissionsSaving = ref(false)
const permissionsDirty = ref(false)
const permissionsMessage = ref(null)
const permittedAgentsCount = computed(() => availableAgents.value.filter(a => a.permitted).length)

// Session Activity state
const sessionActivity = ref({
  status: 'idle',
  active_tool: null,
  tool_counts: {},
  timeline: [],
  totals: { calls: 0, duration_ms: 0, started_at: null }
})

// Git Sync - show tab for GitHub-native agents (Phase 7)
const hasGitSync = computed(() => {
  return agent.value?.template?.startsWith('github:')
})

// Git sync state (header controls)
const gitStatus = ref(null)
const gitLoading = ref(false)
const gitSyncing = ref(false)
const gitSyncResult = ref(null)

const gitHasChanges = computed(() => {
  return (gitStatus.value?.changes_count > 0) || (gitStatus.value?.ahead > 0)
})

const gitChangesCount = computed(() => {
  return gitStatus.value?.changes_count || 0
})

let activityRefreshInterval = null
let gitStatusInterval = null

// File browser state
const fileTree = ref([])
const filesLoading = ref(false)
const filesError = ref(null)
const fileSearchQuery = ref('')
const expandedFolders = ref(new Set())
const totalFileCount = ref(0)

const filteredFileTree = computed(() => {
  if (!fileSearchQuery.value) return fileTree.value

  const query = fileSearchQuery.value.toLowerCase()

  const filterTree = (items) => {
    return items.filter(item => {
      if (item.type === 'file') {
        return item.name.toLowerCase().includes(query)
      } else {
        // For directories, include if name matches or has matching children
        const nameMatches = item.name.toLowerCase().includes(query)
        const filteredChildren = filterTree(item.children || [])
        if (nameMatches || filteredChildren.length > 0) {
          // Auto-expand folders when searching
          if (fileSearchQuery.value) {
            expandedFolders.value.add(item.path)
          }
          return true
        }
        return false
      }
    }).map(item => {
      if (item.type === 'directory') {
        return {
          ...item,
          children: filterTree(item.children || [])
        }
      }
      return item
    })
  }

  return filterTree(fileTree.value)
})

const showNotification = (message, type = 'success') => {
  notification.value = { message, type }
  setTimeout(() => {
    notification.value = null
  }, 3000)
}

// Watch for auto-refresh toggle
watch(autoRefreshLogs, (enabled) => {
  if (enabled) {
    logsRefreshInterval = setInterval(refreshLogs, 10000)
  } else {
    if (logsRefreshInterval) {
      clearInterval(logsRefreshInterval)
      logsRefreshInterval = null
    }
  }
})

// Watch agent status for stats, activity, and git polling
watch(() => agent.value?.status, (newStatus, oldStatus) => {
  if (newStatus === 'running') {
    startStatsPolling()
    startActivityPolling()
    if (hasGitSync.value) {
      startGitStatusPolling()
    }
  } else {
    stopStatsPolling()
    stopActivityPolling()
    stopGitStatusPolling()
    agentStats.value = null
    gitStatus.value = null
    sessionActivity.value = {
      status: 'idle',
      active_tool: null,
      tool_counts: {},
      timeline: [],
      totals: { calls: 0, duration_ms: 0, started_at: null }
    }
  }
})

// Watch for Files tab activation to load files
watch(activeTab, (newTab) => {
  if (newTab === 'files' && agent.value?.status === 'running') {
    loadFiles()
  }
  // Load permissions when permissions tab is selected (Phase 9.10)
  if (newTab === 'permissions' && agent.value?.can_share) {
    loadPermissions()
  }
})

// Clean up intervals on unmount
onUnmounted(() => {
  if (logsRefreshInterval) {
    clearInterval(logsRefreshInterval)
  }
  stopStatsPolling()
  stopActivityPolling()
  stopGitStatusPolling()
})

onMounted(async () => {
  await loadAgent()
  await refreshLogs()
  await loadChatHistory()
  await loadCredentials()
  await loadSessionInfo()
  await loadApiKeySetting()
  // Start stats, activity, and git polling if agent is running
  if (agent.value?.status === 'running') {
    startStatsPolling()
    startActivityPolling()
    if (hasGitSync.value) {
      startGitStatusPolling()
    }
  }
})

const loadAgent = async () => {
  loading.value = true
  error.value = ''
  try {
    agent.value = await agentsStore.fetchAgent(route.params.name)
  } catch (err) {
    error.value = 'Failed to load agent details'
  } finally {
    loading.value = false
  }
}

const refreshLogs = async () => {
  if (!agent.value) return
  try {
    logs.value = await agentsStore.getAgentLogs(agent.value.name, logLines.value)
    // Auto-scroll to bottom if user hasn't scrolled up
    if (!userScrolledUp.value) {
      await nextTick()
      scrollLogsToBottom()
    }
  } catch (err) {
    console.error('Failed to fetch logs:', err)
  }
}

const scrollLogsToBottom = () => {
  if (logsContainer.value) {
    logsContainer.value.scrollTop = logsContainer.value.scrollHeight
  }
}

const handleLogsScroll = () => {
  if (!logsContainer.value) return
  const { scrollTop, scrollHeight, clientHeight } = logsContainer.value
  // User is considered "scrolled up" if not near the bottom (within 50px)
  userScrolledUp.value = scrollTop + clientHeight < scrollHeight - 50
}

const startAgent = async () => {
  if (actionLoading.value) return
  actionLoading.value = true
  try {
    const result = await agentsStore.startAgent(agent.value.name)
    agent.value.status = 'running'
    showNotification(result.message, 'success')
  } catch (err) {
    showNotification(err.message || 'Failed to start agent', 'error')
  } finally {
    actionLoading.value = false
  }
}

const stopAgent = async () => {
  if (actionLoading.value) return
  actionLoading.value = true
  try {
    const result = await agentsStore.stopAgent(agent.value.name)
    agent.value.status = 'stopped'
    showNotification(result.message, 'success')
  } catch (err) {
    showNotification(err.message || 'Failed to stop agent', 'error')
  } finally {
    actionLoading.value = false
  }
}

// API Key Setting Methods
const loadApiKeySetting = async () => {
  if (!agent.value) return
  try {
    apiKeySetting.value = await agentsStore.getAgentApiKeySetting(agent.value.name)
  } catch (err) {
    console.error('Failed to load API key setting:', err)
  }
}

const updateApiKeySetting = async (usePlatformKey) => {
  if (apiKeySettingLoading.value) return
  apiKeySettingLoading.value = true
  try {
    const result = await agentsStore.updateAgentApiKeySetting(agent.value.name, usePlatformKey)
    apiKeySetting.value = {
      use_platform_api_key: result.use_platform_api_key,
      restart_required: result.restart_required
    }
    showNotification(result.message, 'success')
  } catch (err) {
    showNotification(err.message || 'Failed to update API key setting', 'error')
  } finally {
    apiKeySettingLoading.value = false
  }
}

const deleteAgent = () => {
  confirmDialog.title = 'Delete Agent'
  confirmDialog.message = 'Are you sure you want to delete this agent?'
  confirmDialog.confirmText = 'Delete'
  confirmDialog.variant = 'danger'
  confirmDialog.onConfirm = async () => {
    try {
      await agentsStore.deleteAgent(agent.value.name)
      router.push('/agents')
    } catch (err) {
      error.value = 'Failed to delete agent'
    }
  }
  confirmDialog.visible = true
}

// Terminal handlers
const toggleTerminalFullscreen = () => {
  isTerminalFullscreen.value = !isTerminalFullscreen.value
  nextTick(() => {
    if (terminalRef.value?.fit) {
      terminalRef.value.fit()
    }
  })
}

const handleTerminalKeydown = (event) => {
  if (event.key === 'Escape' && isTerminalFullscreen.value) {
    toggleTerminalFullscreen()
  }
}

const onTerminalConnected = () => {
  showNotification('Terminal connected', 'success')
}

const onTerminalDisconnected = () => {
  showNotification('Terminal disconnected', 'info')
}

const onTerminalError = (errorMsg) => {
  showNotification(`Terminal error: ${errorMsg}`, 'error')
}

const loadChatHistory = async () => {
  if (!agent.value || agent.value.status !== 'running') return
  try {
    const history = await agentsStore.getChatHistory(agent.value.name)
    chatMessages.value = history
  } catch (err) {
    console.error('Failed to load chat history:', err)
  }
}

const sendChatMessage = async () => {
  if (!chatInput.value.trim() || chatLoading.value) return

  const userMessage = chatInput.value.trim()
  chatInput.value = ''
  chatLoading.value = true

  // Add user message to UI
  chatMessages.value.push({
    role: 'user',
    content: userMessage
  })
  scrollChatToBottom()

  try {
    const response = await agentsStore.sendChatMessage(agent.value.name, userMessage)

    // Update session info from response
    if (response.session) {
      sessionInfo.value = {
        context_tokens: response.session.context_tokens || 0,
        context_window: response.session.context_window || 200000,
        context_percent: response.session.context_window > 0
          ? Math.round((response.session.context_tokens / response.session.context_window) * 1000) / 10
          : 0,
        total_cost_usd: response.session.total_cost_usd || 0,
        message_count: response.session.message_count || 0
      }
    }

    // Add assistant response with execution info
    chatMessages.value.push({
      role: 'assistant',
      content: response.response,
      execution_log: response.execution_log || [],
      metadata: response.metadata || {}
    })
    scrollChatToBottom()
  } catch (err) {
    error.value = 'Failed to send message'
    console.error('Chat error:', err)

    // Add error message
    chatMessages.value.push({
      role: 'assistant',
      content: '❌ Error: Failed to get response from agent'
    })
    scrollChatToBottom()
  } finally {
    chatLoading.value = false
  }
}

const loadCredentials = async () => {
  if (!agent.value) return
  credentialsLoading.value = true
  try {
    credentialsData.value = await agentsStore.getAgentCredentials(agent.value.name)
  } catch (err) {
    console.error('Failed to load credentials:', err)
    credentialsData.value = null
  } finally {
    credentialsLoading.value = false
  }
}

// Stats polling
const loadStats = async () => {
  if (!agent.value || agent.value.status !== 'running') return
  statsLoading.value = true
  try {
    agentStats.value = await agentsStore.getAgentStats(agent.value.name)
  } catch (err) {
    // Don't log 400 errors (agent not running)
    if (err.response?.status !== 400) {
      console.error('Failed to load stats:', err)
    }
    agentStats.value = null
  } finally {
    statsLoading.value = false
  }
}

const startStatsPolling = () => {
  loadStats() // Load immediately
  statsRefreshInterval = setInterval(loadStats, 5000) // Then every 5 seconds
}

const stopStatsPolling = () => {
  if (statsRefreshInterval) {
    clearInterval(statsRefreshInterval)
    statsRefreshInterval = null
  }
}

// Session Activity polling
const loadSessionActivity = async () => {
  if (!agent.value || agent.value.status !== 'running') return
  try {
    sessionActivity.value = await agentsStore.getSessionActivity(agent.value.name)
  } catch (err) {
    // Don't log errors - activity endpoint may fail during startup
    console.debug('Failed to load session activity:', err)
  }
}

const startActivityPolling = () => {
  loadSessionActivity() // Load immediately
  activityRefreshInterval = setInterval(loadSessionActivity, 2000) // Then every 2 seconds
}

const stopActivityPolling = () => {
  if (activityRefreshInterval) {
    clearInterval(activityRefreshInterval)
    activityRefreshInterval = null
  }
}

// Format bytes to human readable
const formatBytes = (bytes) => {
  if (!bytes && bytes !== 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let unitIndex = 0
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex++
  }
  return `${value.toFixed(unitIndex > 0 ? 1 : 0)} ${units[unitIndex]}`
}

// Format uptime to human readable
const formatUptime = (seconds) => {
  if (!seconds && seconds !== 0) return '0s'
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86400) {
    const hours = Math.floor(seconds / 3600)
    const mins = Math.floor((seconds % 3600) / 60)
    return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`
  }
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  return hours > 0 ? `${days}d ${hours}h` : `${days}d`
}

// Format relative time (e.g., "2 hours ago")
const formatRelativeTime = (dateString) => {
  if (!dateString) return 'Unknown'
  const date = new Date(dateString)
  const now = new Date()
  const diffSeconds = Math.floor((now - date) / 1000)

  if (diffSeconds < 60) return 'just now'
  if (diffSeconds < 3600) return `${Math.floor(diffSeconds / 60)} minutes ago`
  if (diffSeconds < 86400) return `${Math.floor(diffSeconds / 3600)} hours ago`
  if (diffSeconds < 604800) return `${Math.floor(diffSeconds / 86400)} days ago`
  return date.toLocaleDateString()
}

const formatSource = (source) => {
  if (!source) return 'Unknown source'

  if (source.startsWith('mcp:')) {
    const mcpServer = source.replace('mcp:', '')
    return `MCP Server: ${mcpServer}`
  }

  if (source === 'env_file' || source === 'template:env_file') {
    return 'Environment variable'
  }

  if (source.startsWith('template:mcp:')) {
    const mcpServer = source.replace('template:mcp:', '')
    return `MCP Server: ${mcpServer}`
  }

  return source
}

const countCredentials = (text) => {
  if (!text) return 0
  let count = 0
  for (const line of text.split('\n')) {
    const trimmed = line.trim()
    if (trimmed && !trimmed.startsWith('#') && trimmed.includes('=')) {
      count++
    }
  }
  return count
}

const performHotReload = async () => {
  if (!agent.value || agent.value.status !== 'running') return
  if (!hotReloadText.value.trim()) return

  hotReloadLoading.value = true
  hotReloadResult.value = null

  try {
    const result = await agentsStore.hotReloadCredentials(agent.value.name, hotReloadText.value)
    hotReloadResult.value = {
      success: true,
      message: result.message,
      credentials: result.credential_names,
      note: result.note
    }
    // Clear the textarea on success
    hotReloadText.value = ''
    // Refresh credentials list
    await loadCredentials()
  } catch (err) {
    console.error('Hot reload failed:', err)
    hotReloadResult.value = {
      success: false,
      message: err.response?.data?.detail || err.message || 'Failed to hot-reload credentials'
    }
  } finally {
    hotReloadLoading.value = false
  }
}

// Start a new session (clear context)
const startNewSession = () => {
  if (!agent.value || newSessionLoading.value) return

  confirmDialog.title = 'New Session'
  confirmDialog.message = 'Start a new session? This will clear the conversation history and reset the context window.'
  confirmDialog.confirmText = 'Start New Session'
  confirmDialog.variant = 'warning'
  confirmDialog.onConfirm = async () => {
    newSessionLoading.value = true
    try {
      const result = await agentsStore.clearSession(agent.value.name)
      chatMessages.value = []

      // Clear session activity
      await agentsStore.clearSessionActivity(agent.value.name)
      sessionActivity.value = {
        status: 'idle',
        active_tool: null,
        tool_counts: {},
        timeline: [],
        totals: { calls: 0, duration_ms: 0, started_at: null }
      }

      // Reset session info
      if (result.session) {
        sessionInfo.value = {
          context_tokens: result.session.context_tokens || 0,
          context_window: result.session.context_window || 200000,
          context_percent: 0,
          total_cost_usd: result.session.total_cost_usd || 0,
          message_count: 0
        }
      } else {
        sessionInfo.value = {
          context_tokens: 0,
          context_window: 200000,
          context_percent: 0,
          total_cost_usd: 0,
          message_count: 0
        }
      }

      showNotification('New session started', 'success')
    } catch (err) {
      console.error('Failed to start new session:', err)
      showNotification('Failed to start new session', 'error')
    } finally {
      newSessionLoading.value = false
    }
  }
  confirmDialog.visible = true
}

// Sharing methods
const shareWithUser = async () => {
  if (!agent.value || !shareEmail.value.trim()) return

  shareLoading.value = true
  shareMessage.value = null

  try {
    const result = await agentsStore.shareAgent(agent.value.name, shareEmail.value.trim())
    shareMessage.value = {
      type: 'success',
      text: `Agent shared with ${shareEmail.value.trim()}`
    }
    shareEmail.value = ''
    // Refresh agent data to update shares list
    await loadAgent()
  } catch (err) {
    console.error('Failed to share agent:', err)
    shareMessage.value = {
      type: 'error',
      text: err.response?.data?.detail || err.message || 'Failed to share agent'
    }
  } finally {
    shareLoading.value = false
    // Clear message after 5 seconds
    setTimeout(() => {
      shareMessage.value = null
    }, 5000)
  }
}

const removeShare = async (email) => {
  if (!agent.value) return

  unshareLoading.value = email

  try {
    await agentsStore.unshareAgent(agent.value.name, email)
    showNotification(`Sharing removed for ${email}`, 'success')
    // Refresh agent data to update shares list
    await loadAgent()
  } catch (err) {
    console.error('Failed to remove share:', err)
    showNotification(err.response?.data?.detail || 'Failed to remove sharing', 'error')
  } finally {
    unshareLoading.value = null
  }
}

// Permissions methods (Phase 9.10)
const loadPermissions = async () => {
  if (!agent.value) return

  permissionsLoading.value = true
  permissionsMessage.value = null

  try {
    const response = await agentsStore.getAgentPermissions(agent.value.name)
    availableAgents.value = response.available_agents || []
    permissionsDirty.value = false
  } catch (err) {
    console.error('Failed to load permissions:', err)
    permissionsMessage.value = {
      type: 'error',
      text: err.response?.data?.detail || 'Failed to load permissions'
    }
  } finally {
    permissionsLoading.value = false
  }
}

const savePermissions = async () => {
  if (!agent.value || !permissionsDirty.value) return

  permissionsSaving.value = true
  permissionsMessage.value = null

  const permittedAgentNames = availableAgents.value
    .filter(a => a.permitted)
    .map(a => a.name)

  try {
    await agentsStore.setAgentPermissions(agent.value.name, permittedAgentNames)
    permissionsDirty.value = false
    permissionsMessage.value = {
      type: 'success',
      text: `Permissions saved (${permittedAgentNames.length} agents allowed)`
    }
    setTimeout(() => { permissionsMessage.value = null }, 3000)
  } catch (err) {
    console.error('Failed to save permissions:', err)
    permissionsMessage.value = {
      type: 'error',
      text: err.response?.data?.detail || 'Failed to save permissions'
    }
  } finally {
    permissionsSaving.value = false
  }
}

const allowAllAgents = () => {
  availableAgents.value.forEach(a => { a.permitted = true })
  permissionsDirty.value = true
}

const allowNoAgents = () => {
  availableAgents.value.forEach(a => { a.permitted = false })
  permissionsDirty.value = true
}

// Git sync methods (header controls)
const loadGitStatus = async () => {
  if (!agent.value || agent.value.status !== 'running' || !hasGitSync.value) return
  gitLoading.value = true
  try {
    gitStatus.value = await agentsStore.getGitStatus(agent.value.name)
  } catch (err) {
    console.debug('Failed to load git status:', err)
    gitStatus.value = null
  } finally {
    gitLoading.value = false
  }
}

const refreshGitStatus = () => {
  gitSyncResult.value = null
  loadGitStatus()
}

const syncToGithub = async () => {
  if (!agent.value || gitSyncing.value) return
  gitSyncing.value = true
  gitSyncResult.value = null
  try {
    const result = await agentsStore.syncToGithub(agent.value.name)
    gitSyncResult.value = result
    if (result.success) {
      if (result.files_changed > 0) {
        showNotification(`Synced ${result.files_changed} file(s) to GitHub`, 'success')
      } else {
        showNotification(result.message || 'Already up to date', 'success')
      }
    } else {
      showNotification(result.message || 'Sync failed', 'error')
    }
    // Refresh status after sync
    await loadGitStatus()
  } catch (err) {
    console.error('Git sync failed:', err)
    showNotification(err.response?.data?.detail || 'Failed to sync to GitHub', 'error')
  } finally {
    gitSyncing.value = false
  }
}

const startGitStatusPolling = () => {
  if (!hasGitSync.value) return
  loadGitStatus() // Load immediately
  gitStatusInterval = setInterval(loadGitStatus, 30000) // Then every 30 seconds
}

const stopGitStatusPolling = () => {
  if (gitStatusInterval) {
    clearInterval(gitStatusInterval)
    gitStatusInterval = null
  }
}

// File browser functions
const loadFiles = async () => {
  if (!agent.value || agent.value.status !== 'running') return
  filesLoading.value = true
  filesError.value = null
  try {
    const response = await agentsStore.listAgentFiles(agent.value.name)
    fileTree.value = response.tree || []
    totalFileCount.value = response.total_files || 0
  } catch (err) {
    console.error('Failed to load files:', err)
    filesError.value = err.response?.data?.detail || 'Failed to load files'
  } finally {
    filesLoading.value = false
  }
}

const toggleFolder = (path) => {
  if (expandedFolders.value.has(path)) {
    expandedFolders.value.delete(path)
  } else {
    expandedFolders.value.add(path)
  }
  // Trigger reactivity
  expandedFolders.value = new Set(expandedFolders.value)
}

const downloadFile = async (filePath, fileName) => {
  if (!agent.value) return
  try {
    const content = await agentsStore.downloadAgentFile(agent.value.name, filePath)
    // Create blob and download
    const blob = new Blob([content], { type: 'text/plain' })
    const url = window.URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = fileName
    document.body.appendChild(a)
    a.click()
    window.URL.revokeObjectURL(url)
    document.body.removeChild(a)
    showNotification(`Downloaded ${fileName}`, 'success')
  } catch (err) {
    console.error('Failed to download file:', err)
    showNotification(err.response?.data?.detail || 'Failed to download file', 'error')
  }
}

const formatFileSize = (bytes) => {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i]
}

const formatDate = (dateString) => {
  const date = new Date(dateString)
  const now = new Date()
  const diffMs = now - date
  const diffMins = Math.floor(diffMs / 60000)
  const diffHours = Math.floor(diffMs / 3600000)
  const diffDays = Math.floor(diffMs / 86400000)

  if (diffMins < 1) return 'Just now'
  if (diffMins < 60) return `${diffMins}m ago`
  if (diffHours < 24) return `${diffHours}h ago`
  if (diffDays < 7) return `${diffDays}d ago`
  return date.toLocaleDateString()
}

// Format token count for display (e.g., 45000 → "45K")
const formatTokenCount = (count) => {
  if (!count && count !== 0) return '0'
  if (count < 1000) return count.toString()
  if (count < 1000000) return `${(count / 1000).toFixed(1)}K`
  return `${(count / 1000000).toFixed(2)}M`
}

// Get context bar color based on percentage
const getContextBarColor = (percent) => {
  if (percent >= 90) return 'bg-red-500'
  if (percent >= 70) return 'bg-yellow-500'
  return 'bg-green-500'
}

// Load session info
const loadSessionInfo = async () => {
  if (!agent.value || agent.value.status !== 'running') return
  try {
    const info = await agentsStore.getSessionInfo(agent.value.name)
    sessionInfo.value = {
      context_tokens: info.context_tokens || 0,
      context_window: info.context_window || 200000,
      context_percent: info.context_percent || 0,
      total_cost_usd: info.total_cost_usd || 0,
      message_count: info.message_count || 0
    }
    // Also update model from session info
    if (info.model) {
      currentModel.value = info.model
    }
  } catch (err) {
    console.error('Failed to load session info:', err)
  }
}

// Load model info
const loadModelInfo = async () => {
  if (!agent.value || agent.value.status !== 'running') return
  try {
    const info = await agentsStore.getAgentModel(agent.value.name)
    currentModel.value = info.model || ''
  } catch (err) {
    console.error('Failed to load model info:', err)
  }
}

// Change model
const changeModel = async () => {
  if (!agent.value || modelLoading.value) return
  modelLoading.value = true
  try {
    await agentsStore.setAgentModel(agent.value.name, currentModel.value || null)
    showNotification(`Model changed to ${currentModel.value || 'default'}`, 'success')
  } catch (err) {
    console.error('Failed to change model:', err)
    showNotification('Failed to change model', 'error')
    // Reload to get actual state
    await loadModelInfo()
  } finally {
    modelLoading.value = false
  }
}


// Format duration for display
const formatDuration = (ms) => {
  if (!ms && ms !== 0) return ''
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60000)}m ${Math.floor((ms % 60000) / 1000)}s`
}

// Get tool sequence from execution log (just tool names)
const getToolSequence = (executionLog) => {
  if (!executionLog) return []
  return executionLog
    .filter(e => e.type === 'tool_use')
    .slice(0, 5)  // Max 5 tools in preview
    .map(e => e.tool)
}

// Current tool display for loading indicator
const currentToolDisplay = computed(() => {
  if (sessionActivity.value?.active_tool) {
    return `${sessionActivity.value.active_tool.name}...`
  }
  return 'Processing...'
})

// Scroll chat to bottom
const scrollChatToBottom = () => {
  if (chatContainer.value) {
    nextTick(() => {
      chatContainer.value.scrollTop = chatContainer.value.scrollHeight
    })
  }
}

// Render markdown to HTML
const renderMarkdown = (text) => {
  if (!text) return ''
  return marked(text)
}

// Handle use case click from Info tab - switch to terminal tab
const handleUseCaseClick = (text) => {
  activeTab.value = 'terminal'
  // Focus the terminal after switching tabs
  nextTick(() => {
    if (terminalRef.value?.focus) {
      terminalRef.value.focus()
    }
  })
}
</script>

<style scoped>
/* Animated progress bar pulse effect */
@keyframes progress-pulse {
  0%, 100% {
    opacity: 1;
  }
  50% {
    opacity: 0.7;
  }
}

.animate-progress-pulse {
  animation: progress-pulse 2s ease-in-out infinite;
}

/* Shimmer effect for progress bars */
@keyframes shimmer {
  0% {
    background-position: -200% 0;
  }
  100% {
    background-position: 200% 0;
  }
}

.animate-shimmer {
  background: linear-gradient(
    90deg,
    transparent 0%,
    rgba(255, 255, 255, 0.3) 50%,
    transparent 100%
  );
  background-size: 200% 100%;
  animation: shimmer 2s ease-in-out infinite;
}
</style>
