<template>
  <div class="min-h-screen bg-gray-50 dark:bg-gray-900">
    <!-- Header -->
    <div class="bg-white dark:bg-gray-800 shadow">
      <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4">
        <div class="flex items-center justify-between">
          <div class="flex items-center space-x-4">
            <!-- Back button -->
            <router-link
              :to="{ name: 'AgentDetail', params: { name: agentName }, query: { tab: 'tasks' } }"
              class="p-2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
              title="Back to Tasks"
            >
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 19l-7-7m0 0l7-7m-7 7h18" />
              </svg>
            </router-link>

            <div>
              <div class="flex items-center space-x-2">
                <h1 class="text-xl font-bold text-gray-900 dark:text-white">Execution Details</h1>
                <span
                  v-if="execution"
                  :class="statusClass"
                  class="px-2 py-0.5 text-xs font-medium rounded-full"
                >
                  {{ execution.status }}
                </span>
              </div>
              <p class="text-sm text-gray-500 dark:text-gray-400">
                <router-link
                  :to="{ name: 'AgentDetail', params: { name: agentName } }"
                  class="hover:text-action-primary-600 dark:hover:text-action-primary-400"
                >
                  {{ agentName }}
                </router-link>
                <span class="mx-2">/</span>
                <span class="font-mono text-xs">{{ executionId.substring(0, 8) }}...</span>
              </p>
            </div>
          </div>

          <!-- Quick actions -->
          <div class="flex items-center space-x-2">
            <!-- Stop button for running executions -->
            <button
              v-if="execution?.status === 'running'"
              @click="stopExecution"
              :disabled="isStopping"
              class="px-3 py-1.5 bg-status-danger-600 hover:bg-status-danger-700 text-white text-sm font-medium rounded-lg transition-colors disabled:opacity-50 flex items-center space-x-1"
              title="Stop execution"
            >
              <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 10a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z" />
              </svg>
              <span>{{ isStopping ? 'Stopping...' : 'Stop' }}</span>
            </button>
            <!-- Continue as Chat button (EXEC-023) -->
            <button
              v-if="execution?.claude_session_id && execution?.status !== 'running'"
              @click="continueAsChat"
              class="px-3 py-1.5 bg-action-primary-600 hover:bg-action-primary-700 text-white text-sm font-medium rounded-lg transition-colors flex items-center space-x-1"
              title="Continue this execution as an interactive chat"
            >
              <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
              </svg>
              <span>Continue as Chat</span>
            </button>
            <button
              @click="copyExecutionId"
              class="p-2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
              title="Copy execution ID"
            >
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
              </svg>
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Loading state -->
    <div v-if="loading" class="flex items-center justify-center py-20">
      <div class="animate-spin rounded-full h-12 w-12 border-b-2 border-action-primary-500"></div>
    </div>

    <!-- Error state -->
    <div v-else-if="error" class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      <div class="bg-status-danger-50 dark:bg-status-danger-900/20 border border-status-danger-200 dark:border-status-danger-800 rounded-lg p-6 text-center">
        <svg class="w-12 h-12 text-status-danger-400 mx-auto mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
        </svg>
        <h3 class="text-lg font-medium text-status-danger-800 dark:text-status-danger-300 mb-2">Failed to load execution</h3>
        <p class="text-status-danger-600 dark:text-status-danger-400">{{ error }}</p>
        <button
          @click="loadExecution"
          class="mt-4 px-4 py-2 bg-status-danger-100 dark:bg-status-danger-800 text-status-danger-700 dark:text-status-danger-200 rounded-lg hover:bg-status-danger-200 dark:hover:bg-status-danger-700 transition-colors"
        >
          Try Again
        </button>
      </div>
    </div>

    <!-- Content -->
    <div v-else-if="execution" class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 space-y-6">
      <!-- Metadata Cards Row -->
      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <!-- Timing Card -->
        <div class="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
          <div class="flex items-center space-x-3">
            <div class="flex-shrink-0 w-10 h-10 bg-blue-100 dark:bg-blue-900/50 rounded-lg flex items-center justify-center">
              <svg class="w-5 h-5 text-blue-600 dark:text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            </div>
            <div class="min-w-0 flex-1">
              <p class="text-xs font-medium text-gray-500 dark:text-gray-400">Duration</p>
              <p class="text-lg font-semibold text-gray-900 dark:text-white">{{ formatDuration(execution.duration_ms) }}</p>
            </div>
          </div>
        </div>

        <!-- Cost Card -->
        <div class="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
          <div class="flex items-center space-x-3">
            <div class="flex-shrink-0 w-10 h-10 bg-status-success-100 dark:bg-status-success-900/50 rounded-lg flex items-center justify-center">
              <svg class="w-5 h-5 text-status-success-600 dark:text-status-success-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            </div>
            <div class="min-w-0 flex-1">
              <p class="text-xs font-medium text-gray-500 dark:text-gray-400">Cost</p>
              <p class="text-lg font-semibold text-gray-900 dark:text-white">{{ formatCost(execution.cost || 0) }}</p>
            </div>
          </div>
        </div>

        <!-- Context Card -->
        <div class="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
          <div class="flex items-center space-x-3">
            <div class="flex-shrink-0 w-10 h-10 bg-accent-purple-100 dark:bg-accent-purple-900/50 rounded-lg flex items-center justify-center">
              <svg class="w-5 h-5 text-accent-purple-600 dark:text-accent-purple-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
              </svg>
            </div>
            <div class="min-w-0 flex-1">
              <p class="text-xs font-medium text-gray-500 dark:text-gray-400">Context</p>
              <p class="text-lg font-semibold text-gray-900 dark:text-white">
                {{ formatTokens(execution.context_used) }} / {{ formatTokens(execution.context_max) }}
              </p>
            </div>
          </div>
        </div>

        <!-- Trigger Card -->
        <div class="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
          <div class="flex items-center space-x-3">
            <div :class="triggerIconClass" class="flex-shrink-0 w-10 h-10 rounded-lg flex items-center justify-center">
              <svg class="w-5 h-5" :class="triggerIconColor" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path v-if="execution.triggered_by === 'schedule'" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
                <path v-else-if="execution.triggered_by === 'manual'" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 15l-2 5L9 9l11 4-5 2zm0 0l5 5M7.188 2.239l.777 2.897M5.136 7.965l-2.898-.777M13.95 4.05l-2.122 2.122m-5.657 5.656l-2.12 2.122" />
                <path v-else stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
              </svg>
            </div>
            <div class="min-w-0 flex-1">
              <p class="text-xs font-medium text-gray-500 dark:text-gray-400">Triggered By</p>
              <p class="text-lg font-semibold text-gray-900 dark:text-white capitalize">{{ execution.triggered_by }}</p>
            </div>
          </div>
        </div>
      </div>

      <!-- Origin Information (AUDIT-001) -->
      <div v-if="hasOriginInfo" class="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
        <h3 class="text-sm font-medium text-gray-500 dark:text-gray-400 mb-3">Execution Origin</h3>
        <div class="grid grid-cols-1 md:grid-cols-3 gap-4 text-sm">
          <!-- User -->
          <div v-if="execution.source_user_email">
            <span class="text-gray-500 dark:text-gray-400">User:</span>
            <span class="ml-2 text-gray-900 dark:text-white font-medium">{{ execution.source_user_email }}</span>
          </div>
          <!-- Source Agent (for agent-to-agent) -->
          <div v-if="execution.source_agent_name">
            <span class="text-gray-500 dark:text-gray-400">Source Agent:</span>
            <router-link
              :to="{ name: 'AgentDetail', params: { name: execution.source_agent_name } }"
              class="ml-2 text-action-primary-600 dark:text-action-primary-400 font-medium hover:underline"
            >
              {{ execution.source_agent_name }}
            </router-link>
          </div>
          <!-- MCP Key (for MCP calls) -->
          <div v-if="execution.source_mcp_key_name">
            <span class="text-gray-500 dark:text-gray-400">MCP Key:</span>
            <span class="ml-2 text-gray-900 dark:text-white font-medium">{{ execution.source_mcp_key_name }}</span>
            <span v-if="execution.source_mcp_key_id" class="ml-1 text-xs text-gray-400 dark:text-gray-500 font-mono">({{ execution.source_mcp_key_id.substring(0, 8) }}...)</span>
          </div>
        </div>
      </div>

      <!-- Timestamps -->
      <div class="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
        <div class="grid grid-cols-1 md:grid-cols-3 gap-4 text-sm">
          <div>
            <span class="text-gray-500 dark:text-gray-400">Started:</span>
            <span class="ml-2 text-gray-900 dark:text-white font-medium">
              {{ formatDate(execution.started_at) }}
            </span>
          </div>
          <div>
            <span class="text-gray-500 dark:text-gray-400">Completed:</span>
            <span class="ml-2 text-gray-900 dark:text-white font-medium">
              {{ execution.completed_at ? formatDate(execution.completed_at) : 'In progress...' }}
            </span>
          </div>
          <div>
            <span class="text-gray-500 dark:text-gray-400">Execution ID:</span>
            <span class="ml-2 text-gray-900 dark:text-white font-mono text-xs">{{ executionId }}</span>
          </div>
        </div>
      </div>

      <!-- Task Input -->
      <div class="bg-white dark:bg-gray-800 rounded-lg shadow">
        <div class="px-4 py-3 border-b border-gray-200 dark:border-gray-700">
          <h2 class="text-sm font-semibold text-gray-900 dark:text-white">Task Input</h2>
        </div>
        <div class="p-4">
          <div class="bg-gray-50 dark:bg-gray-900 rounded-lg p-4 font-mono text-sm text-gray-800 dark:text-gray-200 whitespace-pre-wrap">{{ execution.message }}</div>
        </div>
      </div>

      <!-- Error Panel (if failed) -->
      <div v-if="execution.error" class="bg-status-danger-50 dark:bg-status-danger-900/20 border border-status-danger-200 dark:border-status-danger-800 rounded-lg shadow">
        <div class="px-4 py-3 border-b border-status-danger-200 dark:border-status-danger-800">
          <h2 class="text-sm font-semibold text-status-danger-800 dark:text-status-danger-300 flex items-center">
            <svg class="w-4 h-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            Error
          </h2>
        </div>
        <div class="p-4">
          <pre class="text-sm text-status-danger-700 dark:text-status-danger-300 whitespace-pre-wrap font-mono">{{ execution.error }}</pre>
        </div>
      </div>

      <!-- Response Summary -->
      <div v-if="execution.response" class="bg-white dark:bg-gray-800 rounded-lg shadow">
        <div class="px-4 py-3 border-b border-gray-200 dark:border-gray-700">
          <h2 class="text-sm font-semibold text-gray-900 dark:text-white">Response Summary</h2>
        </div>
        <div class="p-4">
          <div class="prose prose-sm dark:prose-invert max-w-none" v-html="renderedResponse"></div>
        </div>
      </div>

      <!-- Stream Error Banner -->
      <div v-if="streamError" class="bg-state-autonomous-50 dark:bg-state-autonomous-900/20 border border-state-autonomous-200 dark:border-state-autonomous-800 rounded-lg p-4 flex items-start space-x-3">
        <svg class="w-5 h-5 text-state-autonomous-500 flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
        </svg>
        <div class="flex-1 min-w-0">
          <p class="text-sm text-state-autonomous-800 dark:text-state-autonomous-300">{{ streamError }}</p>
        </div>
        <button
          @click="retryStream"
          class="flex-shrink-0 px-3 py-1 text-xs font-medium text-state-autonomous-700 dark:text-state-autonomous-300 bg-state-autonomous-100 dark:bg-state-autonomous-800/50 rounded hover:bg-state-autonomous-200 dark:hover:bg-state-autonomous-700 transition-colors"
        >
          Retry
        </button>
      </div>

      <!-- Execution Log -->
      <div class="bg-white dark:bg-gray-800 rounded-lg shadow">
        <div class="px-4 py-3 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <div class="flex items-center space-x-3">
            <h2 class="text-sm font-semibold text-gray-900 dark:text-white">Execution Transcript</h2>
            <!-- Streaming indicator -->
            <div v-if="isStreaming" class="flex items-center space-x-2">
              <span class="relative flex h-2 w-2">
                <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-status-success-400 opacity-75"></span>
                <span class="relative inline-flex rounded-full h-2 w-2 bg-status-success-500"></span>
              </span>
              <span class="text-xs text-status-success-600 dark:text-status-success-400 font-medium">Live</span>
            </div>
          </div>
          <div class="flex items-center space-x-3">
            <!-- Auto-scroll toggle -->
            <button
              v-if="isStreaming"
              @click="toggleAutoScroll"
              :class="autoScroll ? 'text-action-primary-600 dark:text-action-primary-400' : 'text-gray-400 dark:text-gray-500'"
              class="text-xs font-medium hover:underline"
              title="Toggle auto-scroll"
            >
              {{ autoScroll ? 'Auto-scroll ON' : 'Auto-scroll OFF' }}
            </button>
            <span v-if="logEntries.length" class="text-xs text-gray-500 dark:text-gray-400">
              {{ logEntries.length }} entries
            </span>
          </div>
        </div>
        <div class="p-4 log-scroll-container" :class="{ 'max-h-[600px] overflow-y-auto': isStreaming }">
          <!-- Streaming - waiting for entries -->
          <div v-if="isStreaming && logEntries.length === 0" class="text-center py-8">
            <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-action-primary-500 mx-auto mb-4"></div>
            <p class="text-gray-500 dark:text-gray-400">Waiting for execution output...</p>
          </div>

          <!-- No log available (completed execution without log) -->
          <div v-else-if="!isStreaming && !logData?.has_log && logEntries.length === 0" class="text-center py-8">
            <svg class="w-12 h-12 text-gray-300 dark:text-gray-600 mx-auto mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
            </svg>
            <p class="text-gray-500 dark:text-gray-400">No execution transcript available for this task.</p>
          </div>

          <!-- Log entries -->
          <div v-else class="space-y-3">
            <template v-for="(entry, idx) in logEntries" :key="idx">
              <!-- Session Init -->
              <div v-if="entry.type === 'init'" class="bg-gray-100 dark:bg-gray-900 rounded-lg p-3 text-xs">
                <div class="flex items-center space-x-2 text-gray-500 dark:text-gray-400 mb-1">
                  <span class="font-semibold">Session Started</span>
                  <span>•</span>
                  <span>{{ entry.model }}</span>
                  <span>•</span>
                  <span>{{ entry.toolCount }} tools</span>
                </div>
                <div v-if="entry.mcpServers.length" class="text-gray-400 dark:text-gray-500">
                  MCP: {{ entry.mcpServers.join(', ') }}
                </div>
              </div>

              <!-- Assistant Message (thinking text) -->
              <div v-else-if="entry.type === 'assistant-text'" class="flex space-x-3">
                <div class="flex-shrink-0 w-8 h-8 bg-action-primary-100 dark:bg-action-primary-900/50 rounded-full flex items-center justify-center">
                  <svg class="w-4 h-4 text-action-primary-600 dark:text-action-primary-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
                  </svg>
                </div>
                <div class="flex-1 min-w-0 bg-action-primary-50 dark:bg-action-primary-900/20 rounded-lg p-3">
                  <div class="text-xs font-medium text-action-primary-700 dark:text-action-primary-300 mb-1">Claude</div>
                  <div class="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap break-words">{{ entry.text }}</div>
                </div>
              </div>

              <!-- Tool Call -->
              <div v-else-if="entry.type === 'tool-call'" class="flex space-x-3">
                <div class="flex-shrink-0 w-8 h-8 bg-state-autonomous-100 dark:bg-state-autonomous-900/50 rounded-full flex items-center justify-center">
                  <svg class="w-4 h-4 text-state-autonomous-600 dark:text-state-autonomous-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                  </svg>
                </div>
                <div class="flex-1 min-w-0 bg-state-autonomous-50 dark:bg-state-autonomous-900/20 rounded-lg p-3">
                  <div class="flex items-center space-x-2 mb-1">
                    <span class="text-xs font-medium text-state-autonomous-700 dark:text-state-autonomous-300">{{ entry.tool }}</span>
                  </div>
                  <pre class="text-xs text-gray-600 dark:text-gray-400 bg-white/50 dark:bg-black/20 rounded p-2 whitespace-pre-wrap break-words max-h-96 overflow-y-auto">{{ entry.input }}</pre>
                </div>
              </div>

              <!-- Tool Result -->
              <div v-else-if="entry.type === 'tool-result'" class="flex space-x-3">
                <div class="flex-shrink-0 w-8 h-8 bg-status-success-100 dark:bg-status-success-900/50 rounded-full flex items-center justify-center">
                  <svg class="w-4 h-4 text-status-success-600 dark:text-status-success-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                </div>
                <div class="flex-1 min-w-0 bg-status-success-50 dark:bg-status-success-900/20 rounded-lg p-3">
                  <div class="text-xs font-medium text-status-success-700 dark:text-status-success-300 mb-1">Result</div>
                  <pre class="text-xs text-gray-600 dark:text-gray-400 bg-white/50 dark:bg-black/20 rounded p-2 whitespace-pre-wrap break-words max-h-96 overflow-y-auto">{{ entry.content }}</pre>
                </div>
              </div>

              <!-- Final Result -->
              <div v-else-if="entry.type === 'result'" class="bg-gray-100 dark:bg-gray-900 rounded-lg p-3 text-xs border-t-2 border-gray-300 dark:border-gray-600">
                <div class="flex items-center justify-between text-gray-500 dark:text-gray-400">
                  <div class="flex items-center space-x-3">
                    <span class="font-semibold text-status-success-600 dark:text-status-success-400">Completed</span>
                    <span>{{ entry.numTurns }} turns</span>
                  </div>
                  <div class="flex items-center space-x-3 font-mono">
                    <span>{{ entry.duration }}</span>
                    <span class="text-action-primary-600 dark:text-action-primary-400">{{ entry.cost }}</span>
                  </div>
                </div>
              </div>
            </template>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import axios from 'axios'
import { renderMarkdown } from '../utils/markdown'
import { formatCost } from '../composables/useFormatters'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const agentName = computed(() => route.params.name)
const executionId = computed(() => route.params.executionId)

const loading = ref(true)
const error = ref(null)
const execution = ref(null)
const logData = ref(null)

// Live streaming state
const isStreaming = ref(false)
const streamingEntries = ref([])  // Raw entries from stream
const eventSource = ref(null)
const autoScroll = ref(true)
const logContainer = ref(null)
const isStopping = ref(false)
const streamError = ref(null)  // User-visible stream error message
const pollingInterval = ref(null)  // Fallback polling timer
const streamRetryCount = ref(0)  // Track retry attempts
const MAX_STREAM_RETRIES = 12  // Max retries (12 * 5s = 60s)

// Computed
const statusClass = computed(() => {
  if (!execution.value) return ''
  const status = execution.value.status
  if (status === 'success') return 'bg-status-success-100 text-status-success-800 dark:bg-status-success-900/50 dark:text-status-success-300'
  if (status === 'failed') return 'bg-status-danger-100 text-status-danger-800 dark:bg-status-danger-900/50 dark:text-status-danger-300'
  if (status === 'running') return 'bg-blue-100 text-blue-800 dark:bg-blue-900/50 dark:text-blue-300'
  if (status === 'queued') return 'bg-state-autonomous-100 text-state-autonomous-800 dark:bg-state-autonomous-900/50 dark:text-state-autonomous-300'
  if (status === 'skipped') return 'bg-accent-purple-100 text-accent-purple-800 dark:bg-accent-purple-900/50 dark:text-accent-purple-300'
  return 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300'
})

const triggerIconClass = computed(() => {
  if (!execution.value) return 'bg-gray-100 dark:bg-gray-700'
  const trigger = execution.value.triggered_by
  if (trigger === 'schedule') return 'bg-accent-purple-100 dark:bg-accent-purple-900/50'
  if (trigger === 'manual') return 'bg-state-autonomous-100 dark:bg-state-autonomous-900/50'
  if (trigger === 'paid') return 'bg-status-warning-100 dark:bg-status-warning-900/50'
  if (trigger === 'public') return 'bg-teal-100 dark:bg-teal-900/50'
  return 'bg-cyan-100 dark:bg-cyan-900/50'
})

const triggerIconColor = computed(() => {
  if (!execution.value) return 'text-gray-600 dark:text-gray-400'
  const trigger = execution.value.triggered_by
  if (trigger === 'schedule') return 'text-accent-purple-600 dark:text-accent-purple-400'
  if (trigger === 'manual') return 'text-state-autonomous-600 dark:text-state-autonomous-400'
  if (trigger === 'paid') return 'text-status-warning-600 dark:text-status-warning-400'
  if (trigger === 'public') return 'text-teal-600 dark:text-teal-400'
  return 'text-cyan-600 dark:text-cyan-400'
})

// Check if execution has origin tracking info (AUDIT-001)
const hasOriginInfo = computed(() => {
  if (!execution.value) return false
  return execution.value.source_user_email ||
         execution.value.source_agent_name ||
         execution.value.source_mcp_key_name
})

const logEntries = computed(() => {
  // If streaming, use streaming entries
  if (isStreaming.value && streamingEntries.value.length > 0) {
    return parseExecutionLog(streamingEntries.value)
  }
  // Otherwise use loaded log data
  if (!logData.value?.log) return []
  return parseExecutionLog(logData.value.log)
})

const renderedResponse = computed(() => {
  return renderMarkdown(execution.value?.response)
})

// Methods
async function loadExecution() {
  loading.value = true
  error.value = null

  try {
    // Fetch execution details first
    const execResponse = await axios.get(
      `/api/agents/${agentName.value}/executions/${executionId.value}`,
      { headers: authStore.authHeader }
    )
    execution.value = execResponse.data

    // If execution is running, start streaming instead of fetching static log
    if (execution.value.status === 'running') {
      startStreaming()
      loading.value = false
      return
    }

    // For completed executions, fetch the log
    const logResponse = await axios.get(
      `/api/agents/${agentName.value}/executions/${executionId.value}/log`,
      { headers: authStore.authHeader }
    )
    logData.value = logResponse.data
  } catch (err) {
    error.value = err.response?.data?.detail || err.message || 'Failed to load execution'
  } finally {
    loading.value = false
  }
}

function startStreaming() {
  isStreaming.value = true
  // Only clear streaming entries on first attempt (not retries)
  if (streamRetryCount.value === 0) {
    streamingEntries.value = []
  }

  // Use fetch with ReadableStream for SSE (EventSource doesn't support custom headers)
  const url = `/api/agents/${agentName.value}/executions/${executionId.value}/stream`

  fetch(url, {
    headers: {
      'Authorization': `Bearer ${authStore.token}`,
      'Accept': 'text/event-stream'
    }
  }).then(response => {
    if (!response.ok) {
      throw new Error(`Stream failed: ${response.status}`)
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    function processStream() {
      reader.read().then(({ done, value }) => {
        if (done) {
          handleStreamEnd()
          return
        }

        buffer += decoder.decode(value, { stream: true })

        // Process complete SSE messages
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''  // Keep incomplete line in buffer

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6))

              if (data.type === 'stream_end') {
                handleStreamEnd()
                return
              }

              if (data.type === 'error') {
                console.error('Stream error:', data.message)
                // Show retryable errors subtly (polling will handle reconnect)
                // Show non-retryable errors prominently
                if (!data.retryable) {
                  streamError.value = data.message || 'Stream error occurred'
                }
                continue
              }

              // Add entry to streaming entries
              streamingEntries.value.push(data)

              // Auto-scroll to bottom
              if (autoScroll.value) {
                nextTick(() => scrollToBottom())
              }
            } catch (e) {
              // Ignore parse errors for comments/keepalives
            }
          }
        }

        processStream()
      }).catch(err => {
        console.error('Stream read error:', err)
        handleStreamEnd()
      })
    }

    processStream()
  }).catch(err => {
    console.error('Failed to start stream:', err)
    isStreaming.value = false
    // Fall back to polling for log
    loadExecutionLog()
  })
}

function handleStreamEnd() {
  isStreaming.value = false

  // Check if execution is still running — if so, start polling fallback
  // This handles the race condition where the stream ends before the agent
  // has registered the execution (404 from agent)
  if (execution.value?.status === 'running') {
    startPollingFallback()
  } else {
    loadExecutionFinal()
  }
}

function startPollingFallback() {
  // Don't start if already polling
  if (pollingInterval.value) return

  // Show a waiting state while polling
  isStreaming.value = true

  pollingInterval.value = setInterval(async () => {
    try {
      // Check execution status
      const execResponse = await axios.get(
        `/api/agents/${agentName.value}/executions/${executionId.value}`,
        { headers: authStore.authHeader }
      )
      execution.value = execResponse.data

      if (execution.value.status !== 'running') {
        // Execution completed while we were polling — load final state
        stopPolling()
        isStreaming.value = false
        loadExecutionFinal()
        return
      }

      // Execution is still running — try to reconnect the stream
      if (streamRetryCount.value < MAX_STREAM_RETRIES) {
        streamRetryCount.value++
        stopPolling()
        streamError.value = null
        startStreaming()
      } else {
        // Max retries exceeded — stop polling, keep showing what we have
        stopPolling()
        isStreaming.value = false
        streamError.value = 'Unable to connect to live stream. The execution is still running — refresh the page to check for updates.'
      }
    } catch (err) {
      console.error('Polling error:', err)
    }
  }, 5000)
}

function stopPolling() {
  if (pollingInterval.value) {
    clearInterval(pollingInterval.value)
    pollingInterval.value = null
  }
}

async function loadExecutionFinal() {
  try {
    const [execResponse, logResponse] = await Promise.all([
      axios.get(`/api/agents/${agentName.value}/executions/${executionId.value}`, {
        headers: authStore.authHeader
      }),
      axios.get(`/api/agents/${agentName.value}/executions/${executionId.value}/log`, {
        headers: authStore.authHeader
      })
    ])
    execution.value = execResponse.data
    logData.value = logResponse.data
    // Clear streaming entries since we now have the full log
    streamingEntries.value = []
  } catch (err) {
    console.error('Failed to load final execution state:', err)
  }
}

function retryStream() {
  streamError.value = null
  streamRetryCount.value = 0
  stopPolling()
  // Re-check execution status and restart streaming if still running
  loadExecution()
}

async function loadExecutionLog() {
  try {
    const logResponse = await axios.get(
      `/api/agents/${agentName.value}/executions/${executionId.value}/log`,
      { headers: authStore.authHeader }
    )
    logData.value = logResponse.data
  } catch (err) {
    console.error('Failed to load execution log:', err)
  }
}

function scrollToBottom() {
  const container = document.querySelector('.log-scroll-container')
  if (container) {
    container.scrollTop = container.scrollHeight
  }
}

function toggleAutoScroll() {
  autoScroll.value = !autoScroll.value
  if (autoScroll.value) {
    scrollToBottom()
  }
}

async function stopExecution() {
  if (isStopping.value) return
  isStopping.value = true

  try {
    // Use the execution ID from the execution record
    await axios.post(
      `/api/agents/${agentName.value}/executions/${executionId.value}/terminate`,
      { task_execution_id: executionId.value },
      { headers: authStore.authHeader }
    )
    // Stream will end naturally, which will trigger loadExecutionFinal
  } catch (err) {
    console.error('Failed to stop execution:', err)
    // Force reload anyway
    handleStreamEnd()
  } finally {
    isStopping.value = false
  }
}

function formatDuration(ms) {
  if (!ms) return '-'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  const mins = Math.floor(ms / 60000)
  const secs = Math.floor((ms % 60000) / 1000)
  return `${mins}m ${secs}s`
}

function formatTokens(count) {
  if (!count) return '-'
  if (count >= 1000) return `${(count / 1000).toFixed(1)}K`
  return count.toString()
}

function formatDate(dateStr) {
  if (!dateStr) return '-'
  const date = new Date(dateStr)
  return date.toLocaleString()
}

function copyExecutionId() {
  navigator.clipboard.writeText(executionId.value)
}

// Continue execution as chat (EXEC-023)
function continueAsChat() {
  if (!execution.value?.claude_session_id) return

  router.push({
    name: 'AgentDetail',
    params: { name: agentName.value },
    query: {
      tab: 'chat',
      resumeSessionId: execution.value.claude_session_id,
      executionId: executionId.value
    }
  })
}

function parseExecutionLog(log) {
  if (!log) return []

  // Handle string log (legacy format)
  if (typeof log === 'string') {
    try {
      log = JSON.parse(log)
    } catch {
      return [{ type: 'assistant-text', text: log }]
    }
  }

  // Must be an array
  if (!Array.isArray(log)) {
    return [{ type: 'assistant-text', text: JSON.stringify(log, null, 2) }]
  }

  const entries = []

  for (const msg of log) {
    // Session init message
    if (msg.type === 'system' && msg.subtype === 'init') {
      entries.push({
        type: 'init',
        model: msg.model || 'unknown',
        toolCount: msg.tools?.length || 0,
        mcpServers: msg.mcp_servers?.map(s => s.name) || []
      })
      continue
    }

    // Assistant message (can contain text and/or tool_use)
    if (msg.type === 'assistant') {
      const content = msg.message?.content || []
      for (const block of content) {
        if (block.type === 'text' && block.text) {
          entries.push({ type: 'assistant-text', text: block.text })
        } else if (block.type === 'tool_use') {
          entries.push({
            type: 'tool-call',
            tool: block.name || 'unknown',
            input: typeof block.input === 'string'
              ? block.input
              : JSON.stringify(block.input, null, 2)
          })
        }
      }
      continue
    }

    // User message (typically tool results)
    if (msg.type === 'user') {
      const content = msg.message?.content || []
      for (const block of content) {
        if (block.type === 'tool_result') {
          let resultContent = block.content
          if (Array.isArray(resultContent)) {
            resultContent = resultContent
              .map(c => c.text || c.content || JSON.stringify(c))
              .join('\n')
          }
          // Truncate very long results (increased limit for full page view)
          if (resultContent && resultContent.length > 5000) {
            resultContent = resultContent.substring(0, 5000) + '\n... (truncated)'
          }
          entries.push({
            type: 'tool-result',
            content: resultContent || '(empty result)'
          })
        }
      }
      continue
    }

    // Final result message
    if (msg.type === 'result') {
      entries.push({
        type: 'result',
        numTurns: msg.num_turns || msg.numTurns || '-',
        duration: msg.duration_ms ? formatDuration(msg.duration_ms) : (msg.duration || '-'),
        cost: formatCost(msg.cost_usd ?? msg.total_cost_usd ?? 0)
      })
      continue
    }
  }

  return entries
}

// Lifecycle
onMounted(() => {
  loadExecution()
})

onUnmounted(() => {
  // Clean up stream if active
  if (eventSource.value) {
    eventSource.value.close()
    eventSource.value = null
  }
  stopPolling()
  isStreaming.value = false
})
</script>
