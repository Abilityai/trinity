<template>
  <div class="space-y-6">
    <!-- Header with Create Button -->
    <div class="flex justify-between items-center">
      <div>
        <h3 class="text-lg font-medium text-gray-900 dark:text-white">Scheduled Tasks</h3>
        <p class="text-sm text-gray-500 dark:text-gray-400">Automate agent tasks with cron schedules</p>
      </div>
      <button
        @click="showCreateForm = true"
        class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500"
      >
        <svg class="w-4 h-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6m0 0v6m0-6h6m-6 0H6" />
        </svg>
        New Schedule
      </button>
    </div>

    <!-- Create/Edit Form Modal -->
    <div v-if="showCreateForm || editingSchedule" class="fixed inset-0 z-50 overflow-y-auto">
      <div class="flex items-center justify-center min-h-screen px-4">
        <div class="fixed inset-0 bg-gray-500 bg-opacity-75" @click="closeForm"></div>
        <div class="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-md w-full relative z-10 p-6">
          <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-4">
            {{ editingSchedule ? 'Edit Schedule' : 'Create Schedule' }}
          </h3>

          <form @submit.prevent="saveSchedule" class="space-y-4">
            <div>
              <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Name</label>
              <input
                v-model="formData.name"
                type="text"
                required
                placeholder="Daily report"
                class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500"
              />
            </div>

            <div>
              <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Cron Expression</label>
              <input
                v-model="formData.cron_expression"
                type="text"
                required
                placeholder="0 9 * * *"
                class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white rounded-md font-mono focus:outline-none focus:ring-2 focus:ring-indigo-500"
              />
              <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">
                Format: minute hour day month day_of_week (e.g., "0 9 * * *" for 9 AM daily)
              </p>
              <div class="mt-1 flex flex-wrap gap-1">
                <button type="button" @click="setCronPreset('0 9 * * *')" class="text-xs px-2 py-0.5 bg-gray-100 dark:bg-gray-700 rounded hover:bg-gray-200 dark:hover:bg-gray-600 dark:text-gray-300">Daily 9 AM</button>
                <button type="button" @click="setCronPreset('0 9 * * 1')" class="text-xs px-2 py-0.5 bg-gray-100 dark:bg-gray-700 rounded hover:bg-gray-200 dark:hover:bg-gray-600 dark:text-gray-300">Weekly Mon</button>
                <button type="button" @click="setCronPreset('0 */6 * * *')" class="text-xs px-2 py-0.5 bg-gray-100 dark:bg-gray-700 rounded hover:bg-gray-200 dark:hover:bg-gray-600 dark:text-gray-300">Every 6h</button>
                <button type="button" @click="setCronPreset('*/30 * * * *')" class="text-xs px-2 py-0.5 bg-gray-100 dark:bg-gray-700 rounded hover:bg-gray-200 dark:hover:bg-gray-600 dark:text-gray-300">Every 30m</button>
              </div>
            </div>

            <div>
              <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Task Message</label>
              <textarea
                v-model="formData.message"
                required
                rows="3"
                placeholder="Generate and post the daily analytics report..."
                class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500"
              ></textarea>
              <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">This message will be sent to the agent when the schedule triggers</p>
            </div>

            <div>
              <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Description (optional)</label>
              <input
                v-model="formData.description"
                type="text"
                placeholder="Optional description"
                class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500"
              />
            </div>

            <!-- Model Selector (MODEL-001) -->
            <div>
              <ModelSelector v-model="formData.model" label="Model" />
            </div>

            <div class="grid grid-cols-2 gap-4">
              <div>
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Timezone</label>
                <select
                  v-model="formData.timezone"
                  class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500"
                >
                  <option value="UTC">UTC</option>
                  <option value="America/New_York">America/New_York (EST/EDT)</option>
                  <option value="America/Los_Angeles">America/Los_Angeles (PST/PDT)</option>
                  <option value="Europe/London">Europe/London (GMT/BST)</option>
                  <option value="Europe/Paris">Europe/Paris (CET/CEST)</option>
                  <option value="Asia/Tokyo">Asia/Tokyo (JST)</option>
                  <option value="Asia/Shanghai">Asia/Shanghai (CST)</option>
                </select>
              </div>
              <div>
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Timeout</label>
                <select
                  v-model="formData.timeout_seconds"
                  class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500"
                >
                  <option :value="300">5 minutes</option>
                  <option :value="900">15 minutes (default)</option>
                  <option :value="1800">30 minutes</option>
                  <option :value="3600">1 hour</option>
                  <option :value="7200">2 hours</option>
                </select>
              </div>
            </div>

            <!-- Retry Configuration (RETRY-001) -->
            <div class="grid grid-cols-2 gap-4">
              <div>
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Max Retries</label>
                <select
                  v-model="formData.max_retries"
                  class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500"
                >
                  <option :value="0">Disabled (default)</option>
                  <option :value="1">1 retry</option>
                  <option :value="2">2 retries</option>
                  <option :value="3">3 retries</option>
                  <option :value="5">5 retries</option>
                </select>
              </div>
              <div>
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Retry Delay</label>
                <select
                  v-model="formData.retry_delay_seconds"
                  class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500"
                >
                  <option :value="30">30 seconds</option>
                  <option :value="60">1 minute (default)</option>
                  <option :value="120">2 minutes</option>
                  <option :value="300">5 minutes</option>
                  <option :value="600">10 minutes</option>
                </select>
              </div>
            </div>
            <p class="text-xs text-gray-500 dark:text-gray-400 -mt-2">
              Auto-retry failed executions. Rate-limited (429) failures use 2x delay.
            </p>

            <!-- Allowed Tools Section -->
            <div>
              <div class="flex items-center justify-between mb-2">
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Allowed Tools</label>
                <button
                  type="button"
                  @click="toggleAllTools"
                  class="text-xs px-2 py-1 rounded"
                  :class="formData.allowed_tools === null ? 'bg-indigo-100 dark:bg-indigo-900/30 text-indigo-700 dark:text-indigo-300' : 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'"
                >
                  {{ formData.allowed_tools === null ? 'All Tools (Unrestricted)' : 'Enable All' }}
                </button>
              </div>
              <div v-if="formData.allowed_tools !== null" class="space-y-3">
                <div v-for="category in toolCategories" :key="category.name">
                  <p class="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">{{ category.name }}</p>
                  <div class="flex flex-wrap gap-2">
                    <label
                      v-for="tool in category.tools"
                      :key="tool.value"
                      class="inline-flex items-center px-2 py-1 rounded text-xs cursor-pointer transition-colors"
                      :class="isToolSelected(tool.value) ? 'bg-indigo-100 dark:bg-indigo-900/30 text-indigo-700 dark:text-indigo-300' : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-600'"
                    >
                      <input
                        type="checkbox"
                        :value="tool.value"
                        :checked="isToolSelected(tool.value)"
                        @change="toggleTool(tool.value)"
                        class="sr-only"
                      />
                      {{ tool.label }}
                    </label>
                  </div>
                </div>
              </div>
              <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">
                {{ formData.allowed_tools === null ? 'Agent can use any tool' : `${formData.allowed_tools.length} tool(s) selected` }}
              </p>
            </div>

            <div class="flex items-center">
              <input
                v-model="formData.enabled"
                type="checkbox"
                id="enabled"
                class="h-4 w-4 text-indigo-600 focus:ring-indigo-500 border-gray-300 rounded"
              />
              <label for="enabled" class="ml-2 text-sm text-gray-700 dark:text-gray-300">Enable schedule immediately</label>
            </div>

            <div v-if="formError" class="p-3 bg-status-danger-50 dark:bg-status-danger-900/30 text-status-danger-700 dark:text-status-danger-300 text-sm rounded-md">
              {{ formError }}
            </div>

            <div class="flex justify-end space-x-3 pt-4">
              <button
                type="button"
                @click="closeForm"
                class="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 border border-gray-300 dark:border-gray-600 rounded-md hover:bg-gray-50 dark:hover:bg-gray-600"
              >
                Cancel
              </button>
              <button
                type="submit"
                :disabled="formLoading"
                class="px-4 py-2 text-sm font-medium text-white bg-indigo-600 border border-transparent rounded-md hover:bg-indigo-700 disabled:bg-gray-400"
              >
                <span v-if="formLoading" class="flex items-center">
                  <svg class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
                  </svg>
                  Saving...
                </span>
                <span v-else>{{ editingSchedule ? 'Update' : 'Create' }}</span>
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>

    <!-- Schedules List -->
    <div v-if="loading" class="text-center py-8">
      <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-indigo-500 mx-auto"></div>
      <p class="text-sm text-gray-500 dark:text-gray-400 mt-2">Loading schedules...</p>
    </div>

    <div v-else-if="schedules.length === 0" class="text-center py-12 bg-gray-50 dark:bg-gray-800 rounded-lg">
      <svg class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
      </svg>
      <p class="mt-2 text-gray-500 dark:text-gray-400">No schedules configured</p>
      <p class="text-sm text-gray-400 dark:text-gray-500">Create a schedule to automate agent tasks</p>
    </div>

    <div v-else class="space-y-4">
      <div
        v-for="schedule in schedules"
        :key="schedule.id"
        class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-4 hover:shadow-sm transition-shadow"
      >
        <div class="flex justify-between items-start">
          <div class="flex-1">
            <div class="flex items-center space-x-2">
              <h4 class="font-medium text-gray-900 dark:text-white">{{ schedule.name }}</h4>
              <span
                :class="[
                  'px-2 py-0.5 text-xs font-medium rounded-full',
                  schedule.enabled ? 'bg-status-success-100 dark:bg-status-success-900/30 text-status-success-800 dark:text-status-success-300' : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400'
                ]"
              >
                {{ schedule.enabled ? 'Active' : 'Disabled' }}
              </span>
            </div>
            <p v-if="schedule.description" class="text-sm text-gray-500 dark:text-gray-400 mt-1">{{ schedule.description }}</p>

            <div class="flex items-center flex-wrap gap-x-4 gap-y-1 mt-2 text-xs text-gray-500 dark:text-gray-400">
              <span class="flex items-center">
                <svg class="w-3.5 h-3.5 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <code class="font-mono bg-gray-100 dark:bg-gray-700 px-1 rounded">{{ schedule.cron_expression }}</code>
              </span>
              <span class="flex items-center">
                <svg class="w-3.5 h-3.5 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3.055 11H5a2 2 0 012 2v1a2 2 0 002 2 2 2 0 012 2v2.945M8 3.935V5.5A2.5 2.5 0 0010.5 8h.5a2 2 0 012 2 2 2 0 104 0 2 2 0 012-2h1.064M15 20.488V18a2 2 0 012-2h3.064" />
                </svg>
                {{ schedule.timezone }}
              </span>
              <span class="flex items-center" :title="`Timeout: ${formatTimeout(schedule.timeout_seconds)}`">
                <svg class="w-3.5 h-3.5 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                {{ formatTimeout(schedule.timeout_seconds) }}
              </span>
              <span v-if="schedule.allowed_tools" class="flex items-center" :title="schedule.allowed_tools.join(', ')">
                <svg class="w-3.5 h-3.5 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
                </svg>
                {{ schedule.allowed_tools.length }} tools
              </span>
              <span v-if="schedule.model" class="flex items-center" :title="`Model: ${schedule.model}`">
                <svg class="w-3.5 h-3.5 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
                </svg>
                {{ schedule.model }}
              </span>
              <!-- RETRY-001: Retry configuration badge -->
              <span v-if="schedule.max_retries > 0" class="flex items-center" :title="`Retry: ${schedule.max_retries}x, ${schedule.retry_delay_seconds}s delay`">
                <svg class="w-3.5 h-3.5 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
                {{ schedule.max_retries }}x retry
              </span>
              <span v-if="schedule.next_run_at" class="flex items-center text-indigo-600 dark:text-indigo-400">
                <svg class="w-3.5 h-3.5 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 7l5 5m0 0l-5 5m5-5H6" />
                </svg>
                Next: {{ formatRelativeTime(schedule.next_run_at) }}
              </span>
              <span v-if="schedule.last_run_at" class="flex items-center">
                <svg class="w-3.5 h-3.5 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                </svg>
                Last: {{ formatRelativeTime(schedule.last_run_at) }}
              </span>
            </div>

            <div class="mt-2 p-2 bg-gray-50 dark:bg-gray-800/50 rounded text-xs text-gray-600 dark:text-gray-400 font-mono max-h-16 overflow-hidden">
              {{ schedule.message.substring(0, 150) }}{{ schedule.message.length > 150 ? '...' : '' }}
            </div>
          </div>

          <div class="flex items-center space-x-2 ml-4">
            <button
              @click="triggerSchedule(schedule)"
              :disabled="triggerLoading === schedule.id"
              class="p-1.5 text-gray-400 hover:text-indigo-600 rounded transition-colors"
              title="Run now"
            >
              <svg v-if="triggerLoading === schedule.id" class="animate-spin h-4 w-4" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
              </svg>
              <svg v-else class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            </button>
            <button
              @click="toggleSchedule(schedule)"
              :disabled="toggleLoading === schedule.id"
              class="p-1.5 rounded transition-colors"
              :class="schedule.enabled ? 'text-status-success-600 hover:text-gray-400' : 'text-gray-400 hover:text-status-success-600'"
              :title="schedule.enabled ? 'Disable' : 'Enable'"
            >
              <svg v-if="toggleLoading === schedule.id" class="animate-spin h-4 w-4" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
              </svg>
              <svg v-else class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" />
              </svg>
            </button>
            <button
              @click="editSchedule(schedule)"
              class="p-1.5 text-gray-400 hover:text-indigo-600 rounded transition-colors"
              title="Edit"
            >
              <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
              </svg>
            </button>
            <button
              @click="deleteSchedule(schedule)"
              :disabled="deleteLoading === schedule.id"
              class="p-1.5 text-gray-400 hover:text-status-danger-600 rounded transition-colors"
              title="Delete"
            >
              <svg v-if="deleteLoading === schedule.id" class="animate-spin h-4 w-4" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
              </svg>
              <svg v-else class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
              </svg>
            </button>
          </div>
        </div>

        <!-- Expand to show executions -->
        <button
          @click="toggleExecutions(schedule.id)"
          class="mt-3 text-xs text-indigo-600 dark:text-indigo-400 hover:text-indigo-800 dark:hover:text-indigo-300 flex items-center"
        >
          <svg
            class="w-3 h-3 mr-1 transform transition-transform"
            :class="expandedSchedule === schedule.id ? 'rotate-90' : ''"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
          </svg>
          {{ expandedSchedule === schedule.id ? 'Hide' : 'Show' }} execution history
        </button>

        <!-- Execution History -->
        <div v-if="expandedSchedule === schedule.id" class="mt-3 border-t border-gray-100 dark:border-gray-700 pt-3">
          <div v-if="executionsLoading" class="text-center py-4">
            <div class="animate-spin rounded-full h-5 w-5 border-b-2 border-indigo-500 mx-auto"></div>
          </div>
          <div v-else-if="!executions[schedule.id] || executions[schedule.id].length === 0" class="text-center py-4 text-xs text-gray-400 dark:text-gray-500">
            No executions yet
          </div>
          <div v-else class="space-y-2 max-h-60 overflow-y-auto">
            <div
              v-for="exec in executions[schedule.id]"
              :key="exec.id"
              @click="viewExecutionDetail(exec)"
              class="flex items-center justify-between text-xs p-2 bg-gray-50 dark:bg-gray-800/50 rounded hover:bg-gray-100 dark:hover:bg-gray-700 cursor-pointer transition-colors"
            >
              <div class="flex items-center space-x-2">
                <span
                  :class="[
                    'w-2 h-2 rounded-full flex-shrink-0',
                    exec.status === 'success' ? 'bg-status-success-500' : exec.status === 'failed' ? 'bg-status-danger-500' : exec.status === 'running' ? 'bg-status-warning-500 animate-pulse' : exec.status === 'skipped' ? 'bg-accent-purple-500' : 'bg-gray-400'
                  ]"
                ></span>
                <span class="text-gray-600 dark:text-gray-400">{{ formatDateTime(exec.started_at) }}</span>
                <span
                  :class="[
                    'px-1.5 py-0.5 rounded text-xs',
                    exec.triggered_by === 'manual' ? 'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300' : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400'
                  ]"
                >
                  {{ exec.triggered_by }}
                </span>
                <span v-if="exec.model_used" class="font-mono text-gray-400 dark:text-gray-500">{{ exec.model_used }}</span>
              </div>
              <div class="flex items-center space-x-3">
                <!-- Context usage progress bar -->
                <div v-if="exec.context_used && exec.context_max" class="flex items-center space-x-1.5">
                  <div class="w-16 h-1.5 bg-gray-200 rounded-full overflow-hidden">
                    <div
                      class="h-full rounded-full transition-all"
                      :class="getContextBarColor(exec.context_used, exec.context_max)"
                      :style="{ width: Math.min(100, (exec.context_used / exec.context_max) * 100) + '%' }"
                    ></div>
                  </div>
                  <span class="text-gray-400 dark:text-gray-500 w-8 text-right">{{ formatContextPercent(exec.context_used, exec.context_max) }}</span>
                </div>
                <!-- Cost -->
                <span v-if="exec.cost" class="text-gray-500 dark:text-gray-400 font-mono">
                  ${{ exec.cost.toFixed(4) }}
                </span>
                <span v-if="exec.duration_ms" class="text-gray-400 dark:text-gray-500">{{ formatDuration(exec.duration_ms) }}</span>
                <span
                  :class="[
                    'font-medium',
                    exec.status === 'success' ? 'text-status-success-600' : exec.status === 'failed' ? 'text-status-danger-600' : exec.status === 'skipped' ? 'text-accent-purple-600' : exec.status === 'pending_retry' ? 'text-status-urgent-600' : 'text-status-warning-600'
                  ]"
                >
                  {{ exec.status === 'pending_retry' ? 'retrying' : exec.status }}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Execution Detail Modal -->
    <div v-if="selectedExecution" class="fixed inset-0 z-50 overflow-y-auto">
      <div class="flex items-center justify-center min-h-screen px-4">
        <div class="fixed inset-0 bg-gray-500 bg-opacity-75" @click="selectedExecution = null"></div>
        <div class="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-3xl w-full relative z-10 max-h-[80vh] overflow-hidden flex flex-col">
          <!-- Header -->
          <div class="p-4 border-b border-gray-200 dark:border-gray-700 flex justify-between items-start">
            <div>
              <h3 class="text-lg font-medium text-gray-900 dark:text-white">Execution Details</h3>
              <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">{{ formatDateTime(selectedExecution.started_at) }}</p>
            </div>
            <div class="flex items-center space-x-3">
              <span
                :class="[
                  'px-2 py-1 rounded-full text-xs font-medium',
                  selectedExecution.status === 'success' ? 'bg-status-success-100 dark:bg-status-success-900/30 text-status-success-800 dark:text-status-success-300' :
                  selectedExecution.status === 'failed' ? 'bg-status-danger-100 dark:bg-status-danger-900/30 text-status-danger-800 dark:text-status-danger-300' :
                  'bg-status-warning-100 dark:bg-status-warning-900/30 text-status-warning-800 dark:text-status-warning-300'
                ]"
              >
                {{ selectedExecution.status }}
              </span>
              <button @click="selectedExecution = null" class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          </div>

          <!-- Stats Row -->
          <div class="p-4 bg-gray-50 dark:bg-gray-800/50 border-b border-gray-200 dark:border-gray-700 grid grid-cols-5 gap-4">
            <div>
              <p class="text-xs text-gray-500 dark:text-gray-400">Duration</p>
              <p class="text-sm font-medium dark:text-white">{{ formatDuration(selectedExecution.duration_ms) || '-' }}</p>
            </div>
            <div>
              <p class="text-xs text-gray-500 dark:text-gray-400">Cost</p>
              <p class="text-sm font-medium font-mono dark:text-white">${{ selectedExecution.cost?.toFixed(4) || '0.0000' }}</p>
            </div>
            <div>
              <p class="text-xs text-gray-500 dark:text-gray-400">Context Used</p>
              <div class="flex items-center space-x-2">
                <p class="text-sm font-medium dark:text-white">{{ formatTokens(selectedExecution.context_used) }}</p>
                <div v-if="selectedExecution.context_max" class="w-12 h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
                  <div
                    class="h-full rounded-full"
                    :class="getContextBarColor(selectedExecution.context_used, selectedExecution.context_max)"
                    :style="{ width: Math.min(100, (selectedExecution.context_used / selectedExecution.context_max) * 100) + '%' }"
                  ></div>
                </div>
              </div>
            </div>
            <div>
              <p class="text-xs text-gray-500 dark:text-gray-400">Model</p>
              <p class="text-sm font-medium font-mono dark:text-white">{{ selectedExecution.model_used || 'default' }}</p>
            </div>
            <div>
              <p class="text-xs text-gray-500 dark:text-gray-400">Triggered By</p>
              <p class="text-sm font-medium capitalize dark:text-white">{{ selectedExecution.triggered_by }}</p>
            </div>
          </div>

          <!-- Content -->
          <div class="flex-1 overflow-y-auto p-4 space-y-4">
            <!-- Message Sent -->
            <div>
              <h4 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">Message Sent</h4>
              <div class="bg-gray-100 dark:bg-gray-700 rounded p-3 text-sm font-mono whitespace-pre-wrap dark:text-gray-300">{{ selectedExecution.message }}</div>
            </div>

            <!-- Error (if any) -->
            <div v-if="selectedExecution.error">
              <h4 class="text-sm font-medium text-status-danger-700 dark:text-status-danger-400 mb-2">Error</h4>
              <div class="bg-status-danger-50 dark:bg-status-danger-900/30 border border-status-danger-200 dark:border-status-danger-800 rounded p-3 text-sm text-status-danger-700 dark:text-status-danger-300 whitespace-pre-wrap">{{ selectedExecution.error }}</div>
            </div>

            <!-- Tool Calls -->
            <div v-if="parsedToolCalls.length > 0">
              <h4 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">Tool Calls ({{ parsedToolCalls.length }})</h4>
              <div class="space-y-1">
                <div
                  v-for="(tool, idx) in parsedToolCalls"
                  :key="idx"
                  class="flex items-center justify-between text-xs bg-gray-100 dark:bg-gray-700 rounded px-2 py-1"
                >
                  <div class="flex items-center space-x-2">
                    <span class="font-medium text-indigo-600 dark:text-indigo-400">{{ tool.tool }}</span>
                    <span v-if="tool.input" class="text-gray-500 dark:text-gray-400 truncate max-w-xs">
                      {{ summarizeToolInput(tool) }}
                    </span>
                  </div>
                  <div class="flex items-center space-x-2">
                    <span v-if="tool.duration_ms" class="text-gray-400 dark:text-gray-500">{{ formatDuration(tool.duration_ms) }}</span>
                    <span v-if="tool.success !== undefined" :class="tool.success ? 'text-status-success-600' : 'text-status-danger-600'">
                      {{ tool.success ? '✓' : '✗' }}
                    </span>
                  </div>
                </div>
              </div>
            </div>

            <!-- Response -->
            <div v-if="selectedExecution.response">
              <h4 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">Response</h4>
              <div class="bg-gray-100 dark:bg-gray-700 rounded p-3 text-sm whitespace-pre-wrap max-h-60 overflow-y-auto dark:text-gray-300">{{ selectedExecution.response }}</div>
            </div>
          </div>

          <!-- Footer -->
          <div class="p-4 border-t border-gray-200 dark:border-gray-700 flex justify-end">
            <button
              @click="selectedExecution = null"
              class="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 border border-gray-300 dark:border-gray-600 rounded-md hover:bg-gray-50 dark:hover:bg-gray-600"
            >
              Close
            </button>
          </div>
        </div>
      </div>
    </div>

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
import { ref, reactive, computed, onMounted, onUnmounted, watch } from 'vue'
import axios from 'axios'
import ConfirmDialog from './ConfirmDialog.vue'
import ModelSelector from './ModelSelector.vue'
import { useAuthStore } from '../stores/auth'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  },
  initialMessage: {
    type: String,
    default: ''
  }
})

const authStore = useAuthStore()

// State
const schedules = ref([])
const loading = ref(true)
const showCreateForm = ref(false)
const editingSchedule = ref(null)
const formLoading = ref(false)
const formError = ref('')
const triggerLoading = ref(null)
const toggleLoading = ref(null)
const deleteLoading = ref(null)
const expandedSchedule = ref(null)
const executions = ref({})
const executionsLoading = ref(false)
const selectedExecution = ref(null)
let executionPollTimer = null

// Confirm dialog state
const confirmDialog = reactive({
  visible: false,
  title: '',
  message: '',
  confirmText: 'Delete',
  variant: 'danger',
  onConfirm: () => {}
})

// Computed: Parse tool calls from selected execution
const parsedToolCalls = computed(() => {
  if (!selectedExecution.value?.tool_calls) return []
  try {
    const calls = JSON.parse(selectedExecution.value.tool_calls)
    // Filter to only tool_use entries (not tool_result)
    return calls.filter(c => c.type === 'tool_use')
  } catch {
    return []
  }
})

const formData = ref({
  name: '',
  cron_expression: '',
  message: '',
  description: '',
  timezone: 'UTC',
  enabled: true,
  timeout_seconds: 900,
  allowed_tools: null,  // null = all tools allowed
  model: 'claude-opus-4-5-20251101',  // MODEL-001
  // RETRY-001: Retry configuration. 0 = disabled (default, #476); 1-5 opt-in.
  max_retries: 0,
  retry_delay_seconds: 60  // Seconds between retries (30-600 range)
})

// Tool categories for allowed tools selection
const toolCategories = [
  {
    name: 'Files',
    tools: [
      { value: 'Read', label: 'Read' },
      { value: 'Write', label: 'Write' },
      { value: 'Edit', label: 'Edit' },
      { value: 'NotebookEdit', label: 'NotebookEdit' }
    ]
  },
  {
    name: 'Search',
    tools: [
      { value: 'Glob', label: 'Glob' },
      { value: 'Grep', label: 'Grep' }
    ]
  },
  {
    name: 'System',
    tools: [
      { value: 'Bash', label: 'Bash' }
    ]
  },
  {
    name: 'Web',
    tools: [
      { value: 'WebFetch', label: 'WebFetch' },
      { value: 'WebSearch', label: 'WebSearch' }
    ]
  },
  {
    name: 'Advanced',
    tools: [
      { value: 'Task', label: 'Task (Agents)' }
    ]
  }
]

// Tool selection helpers
function isToolSelected(tool) {
  return formData.value.allowed_tools !== null && formData.value.allowed_tools.includes(tool)
}

function toggleTool(tool) {
  if (formData.value.allowed_tools === null) {
    formData.value.allowed_tools = [tool]
  } else if (formData.value.allowed_tools.includes(tool)) {
    formData.value.allowed_tools = formData.value.allowed_tools.filter(t => t !== tool)
  } else {
    formData.value.allowed_tools = [...formData.value.allowed_tools, tool]
  }
}

function toggleAllTools() {
  if (formData.value.allowed_tools === null) {
    // Switch to restricted mode (empty list)
    formData.value.allowed_tools = []
  } else {
    // Switch to unrestricted mode
    formData.value.allowed_tools = null
  }
}

// Load schedules
async function loadSchedules() {
  loading.value = true
  try {
    const response = await axios.get(`/api/agents/${props.agentName}/schedules`, {
      headers: authStore.authHeader
    })
    schedules.value = response.data
  } catch (error) {
    console.error('Failed to load schedules:', error)
  } finally {
    loading.value = false
  }
}

// Save schedule (create or update)
async function saveSchedule() {
  formLoading.value = true
  formError.value = ''

  try {
    if (editingSchedule.value) {
      // Update
      await axios.put(
        `/api/agents/${props.agentName}/schedules/${editingSchedule.value.id}`,
        formData.value,
        { headers: authStore.authHeader }
      )
    } else {
      // Create
      await axios.post(
        `/api/agents/${props.agentName}/schedules`,
        formData.value,
        { headers: authStore.authHeader }
      )
    }
    closeForm()
    await loadSchedules()
  } catch (error) {
    formError.value = error.response?.data?.detail || 'Failed to save schedule'
  } finally {
    formLoading.value = false
  }
}

// Close form and reset
function closeForm() {
  showCreateForm.value = false
  editingSchedule.value = null
  formError.value = ''
  formData.value = {
    name: '',
    cron_expression: '',
    message: '',
    description: '',
    timezone: 'UTC',
    enabled: true,
    timeout_seconds: 900,
    allowed_tools: null,
    model: 'claude-opus-4-5-20251101',
    // RETRY-001
    max_retries: 0,
    retry_delay_seconds: 60
  }
}

// Edit schedule
function editSchedule(schedule) {
  editingSchedule.value = schedule
  formData.value = {
    name: schedule.name,
    cron_expression: schedule.cron_expression,
    message: schedule.message,
    description: schedule.description || '',
    timezone: schedule.timezone,
    enabled: schedule.enabled,
    timeout_seconds: schedule.timeout_seconds || 900,
    allowed_tools: schedule.allowed_tools || null,
    model: schedule.model || 'claude-opus-4-6',
    // RETRY-001
    max_retries: schedule.max_retries ?? 0,
    retry_delay_seconds: schedule.retry_delay_seconds ?? 60
  }
}

// Delete schedule
function deleteSchedule(schedule) {
  confirmDialog.title = 'Delete Schedule'
  confirmDialog.message = `Are you sure you want to delete the schedule "${schedule.name}"?`
  confirmDialog.confirmText = 'Delete'
  confirmDialog.variant = 'danger'
  confirmDialog.onConfirm = async () => {
    deleteLoading.value = schedule.id
    try {
      await axios.delete(`/api/agents/${props.agentName}/schedules/${schedule.id}`, {
        headers: authStore.authHeader
      })
      await loadSchedules()
    } catch (error) {
      console.error('Failed to delete schedule:', error)
      alert(error.response?.data?.detail || 'Failed to delete schedule')
    } finally {
      deleteLoading.value = null
    }
  }
  confirmDialog.visible = true
}

// Toggle schedule enabled/disabled
async function toggleSchedule(schedule) {
  toggleLoading.value = schedule.id
  try {
    const endpoint = schedule.enabled ? 'disable' : 'enable'
    await axios.post(`/api/agents/${props.agentName}/schedules/${schedule.id}/${endpoint}`, {}, {
      headers: authStore.authHeader
    })
    await loadSchedules()
  } catch (error) {
    console.error('Failed to toggle schedule:', error)
  } finally {
    toggleLoading.value = null
  }
}

// Trigger schedule manually
async function triggerSchedule(schedule) {
  triggerLoading.value = schedule.id
  try {
    await axios.post(`/api/agents/${props.agentName}/schedules/${schedule.id}/trigger`, {}, {
      headers: authStore.authHeader
    })
    // Reload executions if expanded
    if (expandedSchedule.value === schedule.id) {
      await loadExecutions(schedule.id)
    }
  } catch (error) {
    console.error('Failed to trigger schedule:', error)
    alert(error.response?.data?.detail || 'Failed to trigger schedule')
  } finally {
    triggerLoading.value = null
  }
}

// Toggle execution history
async function toggleExecutions(scheduleId) {
  if (expandedSchedule.value === scheduleId) {
    expandedSchedule.value = null
    stopExecutionPolling()
  } else {
    expandedSchedule.value = scheduleId
    await loadExecutions(scheduleId)
    startExecutionPolling(scheduleId)
  }
}

// Auto-refresh execution list when there are running executions
function startExecutionPolling(scheduleId) {
  stopExecutionPolling()
  executionPollTimer = setInterval(async () => {
    const execs = executions.value[scheduleId]
    const hasRunning = execs && execs.some(e => e.status === 'running')
    if (hasRunning && expandedSchedule.value === scheduleId) {
      await loadExecutions(scheduleId)
    } else if (!hasRunning) {
      stopExecutionPolling()
    }
  }, 10000) // Poll every 10 seconds
}

function stopExecutionPolling() {
  if (executionPollTimer) {
    clearInterval(executionPollTimer)
    executionPollTimer = null
  }
}

// Load executions for a schedule
async function loadExecutions(scheduleId) {
  executionsLoading.value = true
  try {
    const response = await axios.get(
      `/api/agents/${props.agentName}/schedules/${scheduleId}/executions?limit=20`,
      { headers: authStore.authHeader }
    )
    executions.value[scheduleId] = response.data
  } catch (error) {
    console.error('Failed to load executions:', error)
  } finally {
    executionsLoading.value = false
  }
}

// Set cron preset
function setCronPreset(preset) {
  formData.value.cron_expression = preset
}

// Format helpers
function formatRelativeTime(dateStr) {
  if (!dateStr) return ''
  const date = new Date(dateStr)
  const now = new Date()
  const diff = (date - now) / 1000

  if (diff > 0) {
    // Future
    if (diff < 60) return `in ${Math.round(diff)}s`
    if (diff < 3600) return `in ${Math.round(diff / 60)}m`
    if (diff < 86400) return `in ${Math.round(diff / 3600)}h`
    return `in ${Math.round(diff / 86400)}d`
  } else {
    // Past
    const absDiff = Math.abs(diff)
    if (absDiff < 60) return `${Math.round(absDiff)}s ago`
    if (absDiff < 3600) return `${Math.round(absDiff / 60)}m ago`
    if (absDiff < 86400) return `${Math.round(absDiff / 3600)}h ago`
    return `${Math.round(absDiff / 86400)}d ago`
  }
}

function formatDateTime(dateStr) {
  if (!dateStr) return ''
  return new Date(dateStr).toLocaleString()
}

function formatDuration(ms) {
  if (!ms) return ''
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${(ms / 60000).toFixed(1)}m`
}

function formatTimeout(seconds) {
  if (!seconds) return '15m'
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  return `${Math.round(seconds / 3600)}h`
}

function formatTokens(tokens) {
  if (!tokens) return '-'
  if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}K`
  return tokens.toString()
}

function formatContextPercent(used, max) {
  if (!used || !max) return ''
  return `${Math.round((used / max) * 100)}%`
}

function getContextBarColor(used, max) {
  if (!used || !max) return 'bg-gray-400'
  const percent = (used / max) * 100
  if (percent < 50) return 'bg-status-success-500'
  if (percent < 75) return 'bg-status-warning-500'
  if (percent < 90) return 'bg-status-urgent-500'
  return 'bg-status-danger-500'
}

function viewExecutionDetail(exec) {
  selectedExecution.value = exec
}

function summarizeToolInput(tool) {
  if (!tool.input) return ''
  const input = tool.input

  // Common patterns for different tools
  if (input.file_path) {
    const parts = input.file_path.split('/')
    return parts.slice(-2).join('/')
  }
  if (input.pattern) return input.pattern
  if (input.command) return input.command.substring(0, 40) + (input.command.length > 40 ? '...' : '')
  if (input.query) return input.query.substring(0, 40)
  if (input.url) return input.url.substring(0, 40)

  // Fallback: first string value
  for (const [key, value] of Object.entries(input)) {
    if (typeof value === 'string' && value.length < 50) {
      return `${key}: ${value}`
    }
  }
  return ''
}

// Watch for agent name changes
watch(() => props.agentName, () => {
  loadSchedules()
})

// Watch for initial message to pre-fill create form
watch(() => props.initialMessage, (newMessage) => {
  if (newMessage) {
    // Pre-fill the form and open create modal
    formData.value.message = newMessage
    formData.value.name = ''
    formData.value.cron_expression = ''
    formData.value.description = ''
    formData.value.timezone = 'UTC'
    formData.value.enabled = true
    formData.value.timeout_seconds = 900
    formData.value.allowed_tools = null
    showCreateForm.value = true
  }
}, { immediate: true })

onMounted(() => {
  loadSchedules()
})

onUnmounted(() => {
  stopExecutionPolling()
})
</script>
