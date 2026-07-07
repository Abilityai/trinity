<template>
  <div class="space-y-6">
    <!-- Header with Queue Status -->
    <div class="flex justify-between items-center">
      <div>
        <h3 class="text-lg font-medium text-gray-900 dark:text-white">Tasks</h3>
        <p class="text-sm text-gray-500 dark:text-gray-400">All executions — chats, tasks, schedules, and agent-to-agent</p>
      </div>
      <div class="flex items-center space-x-3">
        <!-- Trigger Type Filter -->
        <select
          v-model="triggerFilter"
          class="text-xs px-2 py-1.5 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-300 focus:outline-none focus:ring-1 focus:ring-action-primary-500"
        >
          <option value="all">All triggers</option>
          <option value="chat">Chat</option>
          <option value="session">Session</option>
          <option value="manual">Manual</option>
          <option value="schedule">Schedule</option>
          <option value="mcp">MCP</option>
          <option value="agent">Agent</option>
          <option value="paid">Paid</option>
          <option value="public">Public</option>
        </select>
        <!-- Queue Status Indicator -->
        <div v-if="queueStatus" class="flex items-center space-x-2">
          <span
            :class="[
              'flex items-center px-2.5 py-1 rounded-full text-xs font-medium',
              queueStatus.is_busy
                ? 'bg-status-warning-100 dark:bg-status-warning-900/30 text-status-warning-800 dark:text-status-warning-300'
                : 'bg-status-success-100 dark:bg-status-success-900/30 text-status-success-800 dark:text-status-success-300'
            ]"
          >
            <span
              :class="[
                'w-1.5 h-1.5 rounded-full mr-1.5',
                queueStatus.is_busy ? 'bg-status-warning-500 animate-pulse' : 'bg-status-success-500'
              ]"
            ></span>
            {{ queueStatus.is_busy ? 'Busy' : 'Idle' }}
          </span>
          <span v-if="queueStatus.queue_length > 0" class="text-xs text-gray-500 dark:text-gray-400">
            {{ queueStatus.queue_length }} queued
          </span>
        </div>
        <button
          @click="loadAllData"
          :disabled="loading"
          class="inline-flex items-center px-3 py-1.5 border border-gray-300 dark:border-gray-600 text-sm font-medium rounded-md text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50"
        >
          <svg v-if="loading" class="animate-spin -ml-0.5 mr-1.5 h-3 w-3" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
          </svg>
          <svg v-else class="w-3 h-3 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          Refresh
        </button>
      </div>
    </div>

    <!-- Summary Stats (only if we have history) -->
    <div v-if="executions.length > 0" class="grid grid-cols-4 gap-3">
      <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2">
        <p class="text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wide">Total</p>
        <p class="text-base font-semibold text-gray-900 dark:text-white">{{ executions.length }}</p>
      </div>
      <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2">
        <p class="text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wide">Success Rate</p>
        <p class="text-base font-semibold" :class="successRate >= 90 ? 'text-status-success-600' : successRate >= 70 ? 'text-status-warning-600' : 'text-status-danger-600'">
          {{ successRate }}%
        </p>
      </div>
      <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2">
        <p class="text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wide">Total Cost</p>
        <p class="text-base font-semibold text-gray-900 dark:text-white font-mono">{{ formatCostCompact(totalCost) }}</p>
      </div>
      <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2">
        <p class="text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wide">Avg Duration</p>
        <p class="text-base font-semibold text-gray-900 dark:text-white">{{ formatDuration(avgDuration) || '-' }}</p>
      </div>
    </div>

    <!-- New Task Input -->
    <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
      <!-- Model & Timeout Selectors -->
      <div class="flex items-center space-x-3 mb-3">
        <div class="flex-1">
          <ModelSelector v-model="selectedModel" label="Model" compact />
        </div>
        <div>
          <label class="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Timeout</label>
          <select
            v-model="taskTimeout"
            class="px-2 py-1.5 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-300 focus:outline-none focus:ring-1 focus:ring-action-primary-500"
          >
            <option :value="300">5 min</option>
            <option :value="900">15 min</option>
            <option :value="1800">30 min</option>
            <option :value="3600">1 hour</option>
            <option :value="7200">2 hours</option>
          </select>
        </div>
      </div>
      <div class="flex items-stretch space-x-3">
        <div class="flex-1">
          <textarea
            v-model="newTaskMessage"
            :disabled="taskLoading || agentStatus !== 'running'"
            rows="2"
            placeholder="Enter task message... (Enter to run, Shift+Enter for newline)"
            @keydown.enter.exact.prevent="runNewTask"
            @keydown.meta.enter="runNewTask"
            @keydown.ctrl.enter="runNewTask"
            class="w-full h-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-action-primary-500 disabled:bg-gray-100 dark:disabled:bg-gray-900 disabled:cursor-not-allowed resize-none"
          ></textarea>
        </div>
        <button
          @click="runNewTask"
          :disabled="taskLoading || !newTaskMessage.trim() || agentStatus !== 'running'"
          class="inline-flex items-center justify-center px-4 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-action-primary-600 hover:bg-action-primary-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-action-primary-500 disabled:bg-gray-400 dark:disabled:bg-gray-600 disabled:cursor-not-allowed"
        >
          <svg class="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          Run
        </button>
      </div>
      <p v-if="agentStatus !== 'running'" class="text-xs text-status-warning-600 dark:text-status-warning-400 mt-2">
        Agent must be running to execute tasks
      </p>
    </div>

    <!-- Task History -->
    <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
      <!-- Loading State -->
      <div v-if="loading && allTasks.length === 0" class="text-center py-8">
        <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-action-primary-500 mx-auto"></div>
        <p class="text-sm text-gray-500 dark:text-gray-400 mt-2">Loading tasks...</p>
      </div>

      <!-- Empty State -->
      <div v-else-if="allTasks.length === 0" class="text-center py-12">
        <svg class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
        </svg>
        <p class="mt-2 text-gray-500 dark:text-gray-400">No tasks yet</p>
        <p class="text-sm text-gray-400 dark:text-gray-500">Run a task above or configure schedules</p>
      </div>

      <!-- Task List with vertical scroll -->
      <div v-else class="divide-y divide-gray-200 dark:divide-gray-700 max-h-96 overflow-y-auto">
        <div
          v-for="task in allTasks"
          :key="task.id"
          :ref="el => { if (isHighlightedTask(task.id)) highlightedTaskRef = el }"
          class="p-4 hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors"
          :class="{
            'bg-status-warning-50/50 dark:bg-status-warning-900/10': task.status === 'running',
            'bg-status-danger-50/30 dark:bg-status-danger-900/10': task.status === 'failed',
            'bg-status-urgent-50/30 dark:bg-status-urgent-900/10': task.status === 'cancelled',
            'ring-2 ring-action-primary-500 ring-inset bg-action-primary-50/50 dark:bg-action-primary-900/20': isHighlightedTask(task.id)
          }"
        >
          <div class="flex items-start justify-between">
            <!-- Left side: Status, Message, Meta -->
            <div class="flex-1 min-w-0">
              <div class="flex items-center space-x-2 mb-1">
                <!-- Status badge -->
                <span
                  :class="[
                    'inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium',
                    task.status === 'success' ? 'bg-status-success-100 dark:bg-status-success-900/30 text-status-success-800 dark:text-status-success-300' :
                    task.status === 'failed' ? 'bg-status-danger-100 dark:bg-status-danger-900/30 text-status-danger-800 dark:text-status-danger-300' :
                    task.status === 'cancelled' ? 'bg-status-urgent-100 dark:bg-status-urgent-900/30 text-status-urgent-800 dark:text-status-urgent-300' :
                    task.status === 'running' ? 'bg-status-warning-100 dark:bg-status-warning-900/30 text-status-warning-800 dark:text-status-warning-300' :
                    task.status === 'queued' ? 'bg-state-autonomous-100 dark:bg-state-autonomous-900/30 text-state-autonomous-800 dark:text-state-autonomous-300' :
                    task.status === 'skipped' ? 'bg-accent-purple-100 dark:bg-accent-purple-900/30 text-accent-purple-800 dark:text-accent-purple-300' :
                    'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-300'
                  ]"
                >
                  <span
                    :class="[
                      'w-1.5 h-1.5 mr-1.5 rounded-full',
                      task.status === 'success' ? 'bg-status-success-500' :
                      task.status === 'failed' ? 'bg-status-danger-500' :
                      task.status === 'cancelled' ? 'bg-status-urgent-500' :
                      task.status === 'running' ? 'bg-status-warning-500 animate-pulse' :
                      task.status === 'queued' ? 'bg-state-autonomous-500' :
                      task.status === 'skipped' ? 'bg-accent-purple-500' :
                      'bg-gray-500'
                    ]"
                  ></span>
                  {{ task.status }}
                </span>
                <!-- Trigger badge -->
                <span
                  :class="[
                    'px-1.5 py-0.5 rounded text-xs',
                    task.triggered_by === 'chat' ? 'bg-sky-100 dark:bg-sky-900/30 text-sky-700 dark:text-sky-300' :
                    task.triggered_by === 'session' ? 'bg-state-autonomous-100 dark:bg-state-autonomous-900/30 text-state-autonomous-700 dark:text-state-autonomous-300' :
                    task.triggered_by === 'manual' ? 'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300' :
                    task.triggered_by === 'schedule' ? 'bg-accent-purple-100 dark:bg-accent-purple-900/30 text-accent-purple-700 dark:text-accent-purple-300' :
                    task.triggered_by === 'paid' ? 'bg-status-warning-100 dark:bg-status-warning-900/30 text-status-warning-700 dark:text-status-warning-300' :
                    task.triggered_by === 'public' ? 'bg-teal-100 dark:bg-teal-900/30 text-teal-700 dark:text-teal-300' :
                    'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400'
                  ]"
                >
                  {{ task.triggered_by }}
                </span>
                <!-- Auto-compact badge (Bundle B) — fires when Claude Code summarized history mid-turn -->
                <span
                  v-if="taskCompactCount(task) > 0"
                  class="px-1.5 py-0.5 rounded text-xs bg-violet-100 dark:bg-violet-900/30 text-violet-700 dark:text-violet-300"
                  :title="taskCompactTooltip(task)"
                >
                  compacted{{ taskCompactCount(task) > 1 ? ` ×${taskCompactCount(task)}` : '' }}
                </span>
                <!-- Time -->
                <span class="text-xs text-gray-500 dark:text-gray-400">
                  {{ formatRelativeTime(task.started_at) }}
                </span>
              </div>

              <!-- Message -->
              <p
                class="text-sm text-gray-700 dark:text-gray-300 cursor-pointer hover:text-action-primary-600 dark:hover:text-action-primary-400"
                @click="toggleTaskExpand(task.id)"
              >
                {{ task.message.substring(0, 120) }}{{ task.message.length > 120 ? '...' : '' }}
              </p>

              <!-- Stats row -->
              <div class="flex items-center space-x-4 mt-2 text-xs text-gray-500 dark:text-gray-400">
                <span v-if="task.model_used" class="font-mono bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded">{{ task.model_used }}</span>
                <span v-if="task.duration_ms" class="font-mono">{{ formatDuration(task.duration_ms) }}</span>
                <span v-if="task.cost" class="font-mono">{{ formatCost(task.cost) }}</span>
                <div v-if="task.context_used && task.context_max" class="flex items-center space-x-1">
                  <div class="w-16 h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
                    <div
                      class="h-full rounded-full"
                      :class="getContextBarColor(task.context_used, task.context_max)"
                      :style="{ width: Math.min(100, (task.context_used / task.context_max) * 100) + '%' }"
                    ></div>
                  </div>
                  <span>{{ Math.round((task.context_used / task.context_max) * 100) }}%</span>
                </div>
              </div>

              <!-- Expanded content: Response or Error (loaded on demand) -->
              <div v-if="expandedTaskId === task.id" class="mt-3">
                <!-- Loading state -->
                <div v-if="expandLoadingTaskId === task.id" class="flex items-center space-x-2 text-sm text-gray-500 dark:text-gray-400">
                  <svg class="animate-spin h-4 w-4" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
                  </svg>
                  <span>Loading details...</span>
                </div>
                <!-- Error/Response display (from cache or local task) -->
                <template v-else-if="getTaskDetails(task.id)?.error || getTaskDetails(task.id)?.response || task.error || task.response">
                  <div v-if="getTaskDetails(task.id)?.error || task.error" class="bg-status-danger-50 dark:bg-status-danger-900/20 border border-status-danger-200 dark:border-status-danger-800 rounded p-3 text-sm text-status-danger-700 dark:text-status-danger-300 whitespace-pre-wrap font-mono max-h-48 overflow-y-auto">{{ getTaskDetails(task.id)?.error || task.error }}</div>
                  <div v-else-if="getTaskDetails(task.id)?.response || task.response" class="bg-gray-100 dark:bg-gray-700 rounded p-3 text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap font-mono max-h-48 overflow-y-auto">{{ getTaskDetails(task.id)?.response || task.response }}</div>
                </template>
                <!-- No details available -->
                <div v-else-if="taskDetailsCache[task.id] !== undefined" class="text-sm text-gray-500 dark:text-gray-400 italic">
                  No response or error recorded for this execution.
                </div>
              </div>
            </div>

            <!-- Right side: Actions -->
            <div class="flex items-center space-x-2 ml-4">
              <!-- Open Execution Detail Page (Live for running, Details for completed) -->
              <!-- For server executions: task.id IS the database UUID -->
              <!-- For local tasks: task.id is 'local-xxx', must use task.execution_id instead -->
              <router-link
                v-if="!task.id.startsWith('local-') || task.execution_id"
                :to="{ name: 'ExecutionDetail', params: { name: agentName, executionId: task.id.startsWith('local-') ? task.execution_id : task.id } }"
                :class="[
                  'p-1.5 rounded transition-colors flex items-center space-x-1',
                  task.status === 'running'
                    ? 'text-status-success-600 dark:text-status-success-400 hover:text-status-success-700 dark:hover:text-status-success-300 bg-status-success-50 dark:bg-status-success-900/20'
                    : 'text-gray-400 hover:text-action-primary-600 dark:hover:text-action-primary-400'
                ]"
                :title="task.status === 'running' ? 'View live execution' : 'Open execution details'"
              >
                <!-- Live indicator for running tasks -->
                <span v-if="task.status === 'running'" class="flex items-center">
                  <span class="w-1.5 h-1.5 bg-status-success-500 rounded-full animate-pulse mr-1"></span>
                  <span class="text-xs font-medium">Live</span>
                </span>
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                </svg>
              </router-link>
              <!-- View Log Button (Modal) -->
              <button
                v-if="task.status !== 'running' && !task.id.startsWith('local-')"
                @click="viewExecutionLog(task)"
                class="p-1.5 text-gray-400 hover:text-blue-600 dark:hover:text-blue-400 rounded transition-colors"
                title="View execution log (modal)"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                </svg>
              </button>
              <!-- Copy Input Button -->
              <button
                @click="copyTaskMessage(task)"
                class="p-1.5 text-gray-400 hover:text-action-primary-600 dark:hover:text-action-primary-400 rounded transition-colors"
                title="Copy task input"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                </svg>
              </button>
              <!-- Stop Button (for running tasks) -->
              <button
                v-if="task.status === 'running' && task.execution_id"
                @click="terminateTask(task)"
                :disabled="terminatingTaskId === task.id"
                class="p-1.5 text-gray-400 hover:text-status-danger-600 dark:hover:text-status-danger-400 rounded transition-colors disabled:opacity-50"
                title="Stop execution"
              >
                <svg v-if="terminatingTaskId === task.id" class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                  <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
                </svg>
                <svg v-else class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 10a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z" />
                </svg>
              </button>
              <!-- Re-run Button -->
              <button
                v-if="task.status !== 'running'"
                @click="rerunTask(task)"
                :disabled="taskLoading || agentStatus !== 'running'"
                class="p-1.5 text-gray-400 hover:text-status-success-600 dark:hover:text-status-success-400 rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                title="Re-run this task"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
              </button>
              <!-- Make Repeatable Button -->
              <button
                v-if="task.status !== 'running'"
                @click="makeRepeatable(task)"
                class="p-1.5 text-gray-400 hover:text-accent-purple-600 dark:hover:text-accent-purple-400 rounded transition-colors"
                title="Create schedule from this task"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
                </svg>
              </button>
              <!-- Expand/Collapse Button -->
              <button
                @click="toggleTaskExpand(task.id)"
                class="p-1.5 text-gray-400 hover:text-action-primary-600 dark:hover:text-action-primary-400 rounded transition-colors"
                :title="expandedTaskId === task.id ? 'Collapse' : 'Expand'"
              >
                <svg
                  class="w-4 h-4 transition-transform"
                  :class="expandedTaskId === task.id ? 'rotate-180' : ''"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
                </svg>
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Execution Log Modal -->
    <Teleport to="body">
      <div
        v-if="showLogModal"
        class="fixed inset-0 z-50 overflow-y-auto"
        @click.self="closeLogModal"
      >
        <div class="flex items-center justify-center min-h-screen p-4">
          <div class="fixed inset-0 bg-black/50 transition-opacity"></div>
          <div class="relative bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-4xl w-full max-h-[80vh] flex flex-col">
            <!-- Modal Header -->
            <div class="flex items-center justify-between px-4 py-3 border-b border-gray-200 dark:border-gray-700">
              <div>
                <h3 class="text-lg font-medium text-gray-900 dark:text-white">Execution Log</h3>
                <p v-if="logData" class="text-xs text-gray-500 dark:text-gray-400">
                  {{ logData.status }} • {{ logData.started_at ? new Date(logData.started_at).toLocaleString() : '' }}
                </p>
              </div>
              <button
                @click="closeLogModal"
                class="p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 rounded"
              >
                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
            <!-- Modal Body -->
            <div class="flex-1 overflow-y-auto overflow-x-hidden p-4">
              <div v-if="logLoading" class="flex items-center justify-center py-12">
                <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-action-primary-500"></div>
              </div>
              <div v-else-if="logError" class="text-center py-8">
                <p class="text-status-danger-500 dark:text-status-danger-400">{{ logError }}</p>
              </div>
              <div v-else-if="logData && !logData.has_log" class="text-center py-8">
                <p class="text-gray-500 dark:text-gray-400">No execution log available for this task.</p>
              </div>
              <!-- Formatted execution log transcript -->
              <div v-else-if="logData && logData.log" class="space-y-3">
                <template v-for="(entry, idx) in parseExecutionLog(logData.log)" :key="idx">
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
                      <pre class="text-xs text-gray-600 dark:text-gray-400 bg-white/50 dark:bg-black/20 rounded p-2 whitespace-pre-wrap break-words max-h-48 overflow-y-auto">{{ entry.input }}</pre>
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
                      <pre class="text-xs text-gray-600 dark:text-gray-400 bg-white/50 dark:bg-black/20 rounded p-2 whitespace-pre-wrap break-words max-h-48 overflow-y-auto">{{ entry.content }}</pre>
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
    </Teleport>

    <!-- Queue Management (collapsible) -->
    <div v-if="queueStatus?.queue_length > 0 || queueStatus?.current_execution" class="text-right">
      <button
        v-if="queueStatus?.current_execution"
        @click="forceReleaseQueue"
        :disabled="releaseLoading"
        class="text-xs text-gray-500 hover:text-status-danger-600 dark:hover:text-status-danger-400 mr-4"
      >
        {{ releaseLoading ? 'Releasing...' : 'Force Release Queue' }}
      </button>
      <button
        v-if="queueStatus?.queue_length > 0"
        @click="clearQueue"
        :disabled="clearLoading"
        class="text-xs text-gray-500 hover:text-status-danger-600 dark:hover:text-status-danger-400"
      >
        {{ clearLoading ? 'Clearing...' : 'Clear Queued' }}
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import { formatCost, formatCostCompact } from '../composables/useFormatters'
import ModelSelector from './ModelSelector.vue'

// Template ref for highlighted task element
const highlightedTaskRef = ref(null)

const props = defineProps({
  agentName: {
    type: String,
    required: true
  },
  agentStatus: {
    type: String,
    default: 'stopped'
  },
  highlightExecutionId: {
    type: String,
    default: null
  },
  initialMessage: {
    type: String,
    default: ''
  }
})

const emit = defineEmits(['create-schedule'])

const authStore = useAuthStore()

// State
const executions = ref([])
const pendingTasks = ref([]) // Local tasks we've submitted
const queueStatus = ref(null)
const loading = ref(true)
const newTaskMessage = ref('')
const taskLoading = ref(false)
const releaseLoading = ref(false)
const clearLoading = ref(false)
const expandedTaskId = ref(null)
const expandLoadingTaskId = ref(null) // PERF-001: Track which task is loading details
const taskDetailsCache = ref({}) // PERF-001: Cache for task details (response/error)
const terminatingTaskId = ref(null)
const triggerFilter = ref('all') // Filter by triggered_by type (AUDIT-001)

// Model selection (MODEL-001)
const taskModelKey = computed(() => `trinity-task-model-${props.agentName}`)
const selectedModel = ref(localStorage.getItem(`trinity-task-model-${props.agentName}`) || 'claude-sonnet-4-6')
watch(selectedModel, (val) => {
  localStorage.setItem(taskModelKey.value, val)
})

// Timeout selection
const taskTimeoutKey = computed(() => `trinity-task-timeout-${props.agentName}`)
const taskTimeout = ref(parseInt(localStorage.getItem(`trinity-task-timeout-${props.agentName}`)) || 3600)  // #665: 60min default
watch(taskTimeout, (val) => {
  localStorage.setItem(taskTimeoutKey.value, val)
})

// Execution log modal state
const showLogModal = ref(false)
const logData = ref(null)
const logLoading = ref(false)
const logError = ref(null)

// Running executions from agent (for termination)
const runningExecutions = ref([])

// Polling interval
let pollInterval = null

// Check if a task should be highlighted
function isHighlightedTask(taskId) {
  return props.highlightExecutionId && taskId === props.highlightExecutionId
}

// Combine pending tasks with server executions
const allTasks = computed(() => {
  // Pending tasks first (they're running/just completed), then server executions
  const pending = pendingTasks.value.filter(p => {
    // Only show if not already in executions (to avoid duplicates after refresh)
    return !executions.value.some(e => e.message === p.message && e.started_at === p.started_at)
  })

  // Enhance running tasks with execution_id from runningExecutions
  const enhanceWithExecutionId = (task) => {
    if (task.status !== 'running' || task.execution_id) {
      return task
    }
    // Try to match by message preview (first 100 chars match metadata.message_preview)
    const messagePreview = task.message.substring(0, 100)
    const match = runningExecutions.value.find(e =>
      e.metadata?.message_preview === messagePreview ||
      e.metadata?.message_preview?.startsWith(messagePreview.substring(0, 50))
    )
    if (match) {
      return { ...task, execution_id: match.execution_id }
    }
    // If only one running execution, assume it's this task
    if (runningExecutions.value.length === 1) {
      return { ...task, execution_id: runningExecutions.value[0].execution_id }
    }
    return task
  }

  let tasks = [...pending.map(enhanceWithExecutionId), ...executions.value.map(enhanceWithExecutionId)]

  // Apply trigger type filter (AUDIT-001)
  if (triggerFilter.value !== 'all') {
    tasks = tasks.filter(t => t.triggered_by === triggerFilter.value)
  }

  return tasks
})

// Computed stats (from server executions only)
const successRate = computed(() => {
  if (executions.value.length === 0) return 0
  const successful = executions.value.filter(e => e.status === 'success').length
  return Math.round((successful / executions.value.length) * 100)
})

const totalCost = computed(() => {
  return executions.value.reduce((sum, e) => sum + (e.cost || 0), 0)
})

const avgDuration = computed(() => {
  const withDuration = executions.value.filter(e => e.duration_ms)
  if (withDuration.length === 0) return 0
  return Math.round(withDuration.reduce((sum, e) => sum + e.duration_ms, 0) / withDuration.length)
})

// Load all data
async function loadAllData() {
  loading.value = true
  await Promise.all([
    loadExecutions(),
    loadQueueStatus()
  ])
  loading.value = false
}

// Load executions from server
async function loadExecutions() {
  try {
    const response = await axios.get(`/api/agents/${props.agentName}/executions?limit=100`, {
      headers: authStore.authHeader
    })
    executions.value = response.data

    // If highlightExecutionId is provided, expand and scroll to that task
    if (props.highlightExecutionId) {
      scrollToHighlightedTask()
    }
  } catch (error) {
    console.error('Failed to load executions:', error)
  }
}

// Scroll to highlighted task and expand it
function scrollToHighlightedTask() {
  if (!props.highlightExecutionId) return

  // Auto-expand the highlighted task
  expandedTaskId.value = props.highlightExecutionId

  // Scroll to the highlighted task after DOM update
  nextTick(() => {
    if (highlightedTaskRef.value) {
      highlightedTaskRef.value.scrollIntoView({
        behavior: 'smooth',
        block: 'center'
      })
    }
  })
}

// Load queue status
async function loadQueueStatus() {
  if (props.agentStatus !== 'running') {
    queueStatus.value = null
    runningExecutions.value = []
    return
  }
  try {
    const response = await axios.get(`/api/agents/${props.agentName}/queue`, {
      headers: authStore.authHeader
    })
    queueStatus.value = response.data

    // Fetch running executions for termination support if:
    // 1. Queue is busy (scheduled/queued tasks), OR
    // 2. We have local running tasks (manual tasks via /task endpoint bypass queue)
    const hasLocalRunningTasks = pendingTasks.value.some(t => t.status === 'running')
    if (response.data.is_busy || hasLocalRunningTasks) {
      await loadRunningExecutions()
    } else {
      runningExecutions.value = []
    }
  } catch (error) {
    console.error('Failed to load queue status:', error)
  }
}

// Load running executions from agent (for termination)
async function loadRunningExecutions() {
  if (props.agentStatus !== 'running') {
    runningExecutions.value = []
    return
  }
  try {
    const response = await axios.get(`/api/agents/${props.agentName}/executions/running`, {
      headers: authStore.authHeader
    })
    runningExecutions.value = response.data.executions || []
  } catch (error) {
    console.error('Failed to load running executions:', error)
    runningExecutions.value = []
  }
}

// Run new task
async function runNewTask() {
  if (!newTaskMessage.value.trim()) return

  const taskId = 'local-' + Date.now()
  const taskMessage = newTaskMessage.value
  const startTime = new Date().toISOString()

  // Create a local pending task that shows immediately
  const localTask = {
    id: taskId,
    message: taskMessage,
    status: 'running',
    triggered_by: 'manual',
    started_at: startTime,
    duration_ms: null,
    cost: null,
    context_used: null,
    context_max: null,
    response: null,
    error: null
  }

  // Add to pending tasks (shows at top of list)
  pendingTasks.value.unshift(localTask)
  newTaskMessage.value = ''
  taskLoading.value = true

  // Poll for running executions shortly after task starts (gives agent time to register process)
  // This enables the Stop button to appear while task is running
  setTimeout(() => loadQueueStatus(), 500)
  setTimeout(() => loadQueueStatus(), 2000)  // Second poll in case first was too early

  const startMs = Date.now()

  try {
    // Use /task endpoint for parallel execution (doesn't block queue)
    const payload = { message: taskMessage, timeout_seconds: taskTimeout.value }
    if (selectedModel.value) {
      payload.model = selectedModel.value
    }
    const response = await axios.post(`/api/agents/${props.agentName}/task`, payload, {
      headers: authStore.authHeader
    })

    const durationMs = Date.now() - startMs

    // Update the local task with success
    const idx = pendingTasks.value.findIndex(t => t.id === taskId)
    if (idx !== -1) {
      pendingTasks.value[idx] = {
        ...pendingTasks.value[idx],
        status: 'success',
        duration_ms: durationMs,
        cost: response.data.cost || null,
        context_used: response.data.context_used || null,
        context_max: response.data.context_max || null,
        response: response.data.response || JSON.stringify(response.data, null, 2)
      }
    }
  } catch (error) {
    const durationMs = Date.now() - startMs

    // Update the local task with failure
    const idx = pendingTasks.value.findIndex(t => t.id === taskId)
    if (idx !== -1) {
      pendingTasks.value[idx] = {
        ...pendingTasks.value[idx],
        status: 'failed',
        duration_ms: durationMs,
        error: error.response?.data?.detail || error.message || 'Task failed'
      }
    }
  } finally {
    taskLoading.value = false
    // Refresh server data - this will load the persisted execution
    await loadExecutions()
    loadQueueStatus()
    // Remove the local pending task since it's now in server data
    const idx = pendingTasks.value.findIndex(t => t.id === taskId)
    if (idx !== -1) {
      pendingTasks.value.splice(idx, 1)
    }
  }
}

// Re-run a previous task
function rerunTask(task) {
  newTaskMessage.value = task.message
  // Scroll to top and focus (optional UX improvement)
  runNewTask()
}

// Create schedule from task (make repeatable)
function makeRepeatable(task) {
  emit('create-schedule', task.message)
}

// Copy task message to clipboard
async function copyTaskMessage(task) {
  try {
    await navigator.clipboard.writeText(task.message)
    // Could add a toast notification here if desired
  } catch (error) {
    console.error('Failed to copy:', error)
  }
}

// Toggle task expansion and load details on demand (PERF-001)
async function toggleTaskExpand(taskId) {
  // If already expanded, collapse
  if (expandedTaskId.value === taskId) {
    expandedTaskId.value = null
    return
  }

  // Expand the task
  expandedTaskId.value = taskId

  // For local tasks (not persisted yet), details are already available
  if (taskId.startsWith('local-')) {
    return
  }

  // Check if we already have cached details for this task
  if (taskDetailsCache.value[taskId] !== undefined) {
    return
  }

  // Fetch details from the server
  expandLoadingTaskId.value = taskId
  try {
    const response = await axios.get(`/api/agents/${props.agentName}/executions/${taskId}`, {
      headers: authStore.authHeader
    })
    // Cache the response and error fields
    taskDetailsCache.value[taskId] = {
      response: response.data.response,
      error: response.data.error
    }
  } catch (error) {
    console.error('Failed to load task details:', error)
    // Cache empty to avoid re-fetching on error
    taskDetailsCache.value[taskId] = { response: null, error: null }
  } finally {
    expandLoadingTaskId.value = null
  }
}

// Get cached task details (PERF-001)
function getTaskDetails(taskId) {
  return taskDetailsCache.value[taskId]
}

// Terminate a running task
async function terminateTask(task) {
  if (!task.execution_id) {
    console.error('No execution_id available for termination')
    return
  }

  terminatingTaskId.value = task.id

  try {
    // Pass task_execution_id as query param so backend can update database record
    await axios.post(
      `/api/agents/${props.agentName}/executions/${task.execution_id}/terminate?task_execution_id=${task.execution_id}`,
      {},
      { headers: authStore.authHeader }
    )

    // Update task status locally while we wait for refresh
    const idx = pendingTasks.value.findIndex(t => t.id === task.id)
    if (idx !== -1) {
      pendingTasks.value[idx].status = 'cancelled'
      pendingTasks.value[idx].error = 'Execution terminated by user'
    }

    // Refresh data to get updated status
    await loadAllData()
  } catch (error) {
    console.error('Failed to terminate task:', error)
    // Show error in task if possible
    const idx = pendingTasks.value.findIndex(t => t.id === task.id)
    if (idx !== -1) {
      pendingTasks.value[idx].error = error.response?.data?.detail || 'Failed to terminate'
    }
  } finally {
    terminatingTaskId.value = null
  }
}

// View execution log
async function viewExecutionLog(task) {
  showLogModal.value = true
  logLoading.value = true
  logError.value = null
  logData.value = null

  try {
    const response = await axios.get(`/api/agents/${props.agentName}/executions/${task.id}/log`, {
      headers: authStore.authHeader
    })
    logData.value = response.data
  } catch (error) {
    logError.value = error.response?.data?.detail || error.message || 'Failed to load execution log'
  } finally {
    logLoading.value = false
  }
}

// Close log modal
function closeLogModal() {
  showLogModal.value = false
  logData.value = null
  logError.value = null
}

// Format log data for display
function formatLogData(log) {
  if (!log) return ''
  if (typeof log === 'string') return log
  // Pretty print JSON array/object
  return JSON.stringify(log, null, 2)
}

// Parse execution log JSON into structured entries for display
// Expects raw Claude Code stream-json format: {type: "system/assistant/user/result", ...}
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
          entries.push({
            type: 'assistant-text',
            text: block.text
          })
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
          // Content can be string or array of content blocks
          let resultContent = block.content
          if (Array.isArray(resultContent)) {
            resultContent = resultContent
              .map(c => c.text || c.content || JSON.stringify(c))
              .join('\n')
          }
          // Truncate very long results
          if (resultContent && resultContent.length > 2000) {
            resultContent = resultContent.substring(0, 2000) + '\n... (truncated)'
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

// Force release queue
async function forceReleaseQueue() {
  releaseLoading.value = true
  try {
    await axios.post(`/api/agents/${props.agentName}/queue/release`, {}, {
      headers: authStore.authHeader
    })
    await loadQueueStatus()
  } catch (error) {
    console.error('Failed to release queue:', error)
  } finally {
    releaseLoading.value = false
  }
}

// Clear queue
async function clearQueue() {
  clearLoading.value = true
  try {
    await axios.post(`/api/agents/${props.agentName}/queue/clear`, {}, {
      headers: authStore.authHeader
    })
    await loadQueueStatus()
  } catch (error) {
    console.error('Failed to clear queue:', error)
  } finally {
    clearLoading.value = false
  }
}

// Format helpers
function formatRelativeTime(dateStr) {
  if (!dateStr) return ''
  const date = new Date(dateStr)
  const now = new Date()
  const diff = (now - date) / 1000

  if (diff < 5) return 'just now'
  if (diff < 60) return `${Math.round(diff)}s ago`
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`
  return `${Math.round(diff / 86400)}d ago`
}

function formatDuration(ms) {
  if (!ms) return ''
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${(ms / 60000).toFixed(1)}m`
}

function getContextBarColor(used, max) {
  if (!used || !max) return 'bg-gray-400'
  const percent = (used / max) * 100
  if (percent < 50) return 'bg-status-success-500'
  if (percent < 75) return 'bg-status-warning-500'
  if (percent < 90) return 'bg-status-urgent-500'
  return 'bg-status-danger-500'
}

// Auto-compact observability (Bundle B). The agent server captures
// `compact_boundary` events from Claude Code's stream-json output and persists
// them as a JSON list on schedule_executions.compact_metadata. Each event has
// {trigger, pre_tokens, post_tokens, duration_ms, timestamp}.
function _parseCompactEvents(task) {
  if (!task?.compact_metadata) return []
  try {
    const parsed = JSON.parse(task.compact_metadata)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

function taskCompactCount(task) {
  return _parseCompactEvents(task).length
}

function taskCompactTooltip(task) {
  const events = _parseCompactEvents(task)
  if (events.length === 0) return ''
  const lines = events.map((e, i) => {
    const pre = e.pre_tokens ?? '?'
    const post = e.post_tokens ?? '?'
    const dur = e.duration_ms ? `${(e.duration_ms / 1000).toFixed(1)}s` : '?'
    return `#${i + 1}: ${pre} → ${post} tokens (${dur})`
  })
  return `Conversation auto-compacted mid-turn:\n${lines.join('\n')}`
}

// Start polling
function startPolling() {
  stopPolling()
  if (props.agentStatus === 'running') {
    pollInterval = setInterval(() => {
      loadQueueStatus()
    }, 5000)
  }
}

// Stop polling
function stopPolling() {
  if (pollInterval) {
    clearInterval(pollInterval)
    pollInterval = null
  }
}

// Watch for agent status changes
watch(() => props.agentStatus, (newStatus) => {
  if (newStatus === 'running') {
    loadQueueStatus()
    startPolling()
  } else {
    queueStatus.value = null
    stopPolling()
  }
})

// Watch for agent name changes
watch(() => props.agentName, () => {
  pendingTasks.value = [] // Clear local tasks when switching agents
  taskDetailsCache.value = {} // Clear cached details (PERF-001)
  expandedTaskId.value = null // Collapse any expanded task
  loadAllData()
  startPolling()
})

// Watch for initial message changes (from Info tab clicks)
// immediate: true ensures it fires on mount when component is conditionally rendered
watch(() => props.initialMessage, (newMessage) => {
  if (newMessage) {
    newTaskMessage.value = newMessage
  }
}, { immediate: true })

onMounted(() => {
  loadAllData()
  startPolling()
})

onUnmounted(() => {
  stopPolling()
})
</script>
