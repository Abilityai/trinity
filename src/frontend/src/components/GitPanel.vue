<template>
  <div class="space-y-6">
    <!-- Loading State -->
    <div v-if="loading" class="text-center py-8">
      <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-indigo-500 mx-auto"></div>
      <p class="text-gray-500 dark:text-gray-400 mt-2">Loading git status...</p>
    </div>

    <!-- Git Not Enabled -->
    <div v-else-if="!gitStatus?.git_enabled" class="text-center py-8">
      <svg class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4" />
      </svg>
      <p class="mt-2 text-gray-500 dark:text-gray-400">Git sync not enabled for this agent</p>
      <p class="text-sm text-gray-400 dark:text-gray-500 mt-1">Push this agent to a GitHub repository to enable sync</p>

      <!-- Initialize Button -->
      <button
        @click="showInitializeModal = true"
        class="mt-4 inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500"
      >
        <svg class="h-5 w-5 mr-2" fill="currentColor" viewBox="0 0 24 24">
          <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
        </svg>
        Initialize GitHub Sync
      </button>
    </div>

    <!-- Initialize GitHub Sync Modal -->
    <div v-if="showInitializeModal" class="fixed inset-0 z-50 overflow-y-auto" aria-labelledby="modal-title" role="dialog" aria-modal="true">
      <div class="flex items-center justify-center min-h-screen pt-4 px-4 pb-20 text-center sm:block sm:p-0">
        <!-- Background overlay -->
        <div class="fixed inset-0 bg-gray-500 bg-opacity-75 transition-opacity" aria-hidden="true" @click="closeInitializeModal"></div>

        <!-- Modal panel -->
        <div class="inline-block align-bottom bg-white dark:bg-gray-800 rounded-lg text-left overflow-hidden shadow-xl transform transition-all sm:my-8 sm:align-middle sm:max-w-lg sm:w-full">
          <div class="bg-white dark:bg-gray-800 px-4 pt-5 pb-4 sm:p-6 sm:pb-4">
            <div class="sm:flex sm:items-start">
              <div class="mx-auto flex-shrink-0 flex items-center justify-center h-12 w-12 rounded-full bg-indigo-100 dark:bg-indigo-900/30 sm:mx-0 sm:h-10 sm:w-10">
                <svg class="h-6 w-6 text-indigo-600 dark:text-indigo-400" fill="currentColor" viewBox="0 0 24 24">
                  <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
                </svg>
              </div>
              <div class="mt-3 text-center sm:mt-0 sm:ml-4 sm:text-left flex-1">
                <h3 class="text-lg leading-6 font-medium text-gray-900 dark:text-white" id="modal-title">
                  Initialize GitHub Sync
                </h3>
                <div class="mt-4 space-y-4">
                  <!-- Error Message -->
                  <div v-if="initializeError" class="rounded-md bg-status-danger-50 dark:bg-status-danger-900/30 p-4">
                    <p class="text-sm text-status-danger-800 dark:text-status-danger-300">{{ initializeError }}</p>
                  </div>

                  <!-- Repository Owner -->
                  <div>
                    <label for="repo-owner" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                      Repository Owner
                    </label>
                    <input
                      type="text"
                      id="repo-owner"
                      v-model="repoOwner"
                      placeholder="your-username"
                      :disabled="initializing"
                      class="mt-1 block w-full border border-gray-300 dark:border-gray-600 rounded-md shadow-sm py-2 px-3 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 sm:text-sm dark:bg-gray-700 dark:text-white"
                    />
                    <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">Your GitHub username or organization name</p>
                  </div>

                  <!-- Repository Name -->
                  <div>
                    <label for="repo-name" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                      Repository Name
                    </label>
                    <input
                      type="text"
                      id="repo-name"
                      v-model="repoName"
                      placeholder="my-agent"
                      :disabled="initializing"
                      class="mt-1 block w-full border border-gray-300 dark:border-gray-600 rounded-md shadow-sm py-2 px-3 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 sm:text-sm dark:bg-gray-700 dark:text-white"
                    />
                    <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">Name for the new repository</p>
                  </div>

                  <!-- Options -->
                  <div class="flex items-center">
                    <input
                      id="create-repo"
                      type="checkbox"
                      v-model="createRepo"
                      :disabled="initializing"
                      class="h-4 w-4 text-indigo-600 focus:ring-indigo-500 border-gray-300 rounded"
                    />
                    <label for="create-repo" class="ml-2 block text-sm text-gray-700 dark:text-gray-300">
                      Create repository if it doesn't exist
                    </label>
                  </div>

                  <div v-if="createRepo" class="flex items-center ml-6">
                    <input
                      id="private-repo"
                      type="checkbox"
                      v-model="privateRepo"
                      :disabled="initializing"
                      class="h-4 w-4 text-indigo-600 focus:ring-indigo-500 border-gray-300 rounded"
                    />
                    <label for="private-repo" class="ml-2 block text-sm text-gray-700 dark:text-gray-300">
                      Make repository private
                    </label>
                  </div>

                  <!-- Info Box -->
                  <div class="rounded-md bg-blue-50 dark:bg-blue-900/30 p-4">
                    <div class="flex">
                      <svg class="h-5 w-5 text-blue-400 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clip-rule="evenodd" />
                      </svg>
                      <div class="ml-3 flex-1">
                        <p class="text-xs text-blue-700 dark:text-blue-300">
                          This will:
                        </p>
                        <ul class="mt-1 text-xs text-blue-700 dark:text-blue-300 list-disc list-inside space-y-1">
                          <li>Initialize git in the agent workspace</li>
                          <li>Commit the current state</li>
                          <li>Push to GitHub</li>
                          <li>Enable bidirectional sync</li>
                        </ul>
                        <p class="mt-2 text-xs text-blue-700 dark:text-blue-300">
                          <strong>Note:</strong> GitHub PAT must be configured in Settings with <code class="bg-blue-100 dark:bg-blue-800 px-1 rounded">repo</code> scope.
                        </p>
                        <p class="mt-1 text-xs text-blue-700 dark:text-blue-300">
                          <strong>Timing:</strong> This may take 10-60 seconds depending on the size of your agent's files.
                        </p>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
          <div class="bg-gray-50 dark:bg-gray-700 px-4 py-3 sm:px-6 sm:flex sm:flex-row-reverse">
            <button
              type="button"
              @click="initializeGitHub"
              :disabled="!repoOwner || !repoName || initializing"
              class="w-full inline-flex justify-center rounded-md border border-transparent shadow-sm px-4 py-2 bg-indigo-600 text-base font-medium text-white hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 sm:ml-3 sm:w-auto sm:text-sm disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <svg v-if="initializing" class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
              </svg>
              {{ initializing ? 'Initializing...' : 'Initialize' }}
            </button>
            <button
              type="button"
              @click="closeInitializeModal"
              :disabled="initializing"
              class="mt-3 w-full inline-flex justify-center rounded-md border border-gray-300 dark:border-gray-600 shadow-sm px-4 py-2 bg-white dark:bg-gray-800 text-base font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 sm:mt-0 sm:ml-3 sm:w-auto sm:text-sm disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Cancel
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Agent Not Running -->
    <div v-else-if="gitStatus?.agent_running === false" class="text-center py-8 text-gray-500 dark:text-gray-400">
      <svg class="mx-auto h-12 w-12 text-status-warning-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
      </svg>
      <p class="mt-2">Agent must be running to view git status</p>
      <div v-if="gitStatus?.config" class="mt-4 text-left max-w-md mx-auto bg-gray-50 dark:bg-gray-800 p-4 rounded-lg">
        <p class="text-sm font-medium text-gray-700 dark:text-gray-300">Saved Configuration:</p>
        <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">Repo: {{ gitStatus.config.github_repo }}</p>
        <p class="text-sm text-gray-500 dark:text-gray-400">Branch: {{ gitStatus.config.working_branch }}</p>
        <p v-if="gitStatus.config.last_sync_at" class="text-sm text-gray-500 dark:text-gray-400">
          Last sync: {{ formatDate(gitStatus.config.last_sync_at) }}
        </p>
      </div>
    </div>

    <!-- Git Status Display -->
    <div v-else-if="gitStatus?.remote_url && gitStatus?.branch">
      <!-- Repository Info Header -->
      <div class="bg-gray-50 dark:bg-gray-800 rounded-lg p-4 border border-gray-200 dark:border-gray-700">
        <div class="flex items-center justify-between">
          <div class="flex items-center space-x-3">
            <svg class="h-6 w-6 text-gray-600 dark:text-gray-400" fill="currentColor" viewBox="0 0 24 24">
              <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
            </svg>
            <div>
              <a :href="gitStatus.remote_url" target="_blank" class="text-sm font-medium text-indigo-600 hover:text-indigo-800">
                {{ gitStatus.remote_url }}
              </a>
              <div class="flex items-center space-x-2 mt-1">
                <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-indigo-100 dark:bg-indigo-900/30 text-indigo-800 dark:text-indigo-300">
                  <svg class="w-3 h-3 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
                  </svg>
                  {{ gitStatus.branch }}
                </span>
                <span v-if="gitStatus.ahead > 0" class="text-xs text-status-success-600">
                  {{ gitStatus.ahead }} ahead
                </span>
                <span v-if="gitStatus.behind > 0" class="text-xs text-status-urgent-600">
                  {{ gitStatus.behind }} behind
                </span>
              </div>
            </div>
          </div>
          <div class="flex items-center space-x-2">
            <span
              :class="[
                'px-2 py-1 text-xs font-medium rounded-full',
                gitStatus.sync_status === 'up_to_date'
                  ? 'bg-status-success-100 dark:bg-status-success-900/30 text-status-success-800 dark:text-status-success-300'
                  : 'bg-status-warning-100 dark:bg-status-warning-900/30 text-status-warning-800 dark:text-status-warning-300'
              ]"
            >
              {{ gitStatus.sync_status === 'up_to_date' ? 'Synced' : 'Changes pending' }}
            </span>
          </div>
        </div>
      </div>

      <!-- Pending Changes -->
      <div v-if="gitStatus.changes && gitStatus.changes.length > 0">
        <h3 class="text-sm font-medium text-gray-900 dark:text-white mb-2">Pending Changes ({{ gitStatus.changes_count }})</h3>
        <div class="bg-gray-50 dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
          <ul class="divide-y divide-gray-200 dark:divide-gray-700 max-h-48 overflow-y-auto">
            <li v-for="change in gitStatus.changes" :key="change.path" class="px-4 py-2 flex items-center space-x-3 text-sm">
              <span
                :class="[
                  'w-5 h-5 flex items-center justify-center rounded text-xs font-mono font-bold',
                  getChangeStatusClass(change.status)
                ]"
              >
                {{ change.status }}
              </span>
              <span class="font-mono text-gray-700 dark:text-gray-300 truncate">{{ change.path }}</span>
            </li>
          </ul>
        </div>
      </div>

      <!-- Last Commit -->
      <div v-if="gitStatus.last_commit">
        <h3 class="text-sm font-medium text-gray-900 dark:text-white mb-2">Last Commit</h3>
        <div class="bg-gray-50 dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <div class="flex items-start space-x-3">
            <div class="flex-shrink-0">
              <div class="w-8 h-8 bg-gray-300 dark:bg-gray-600 rounded-full flex items-center justify-center">
                <span class="text-xs font-medium text-gray-600 dark:text-gray-300">
                  {{ (gitStatus.last_commit.author || '?')[0].toUpperCase() }}
                </span>
              </div>
            </div>
            <div class="flex-1 min-w-0">
              <p class="text-sm font-medium text-gray-900 dark:text-white">{{ gitStatus.last_commit.message }}</p>
              <div class="flex items-center space-x-2 mt-1 text-xs text-gray-500 dark:text-gray-400">
                <code class="bg-gray-200 dark:bg-gray-700 px-1.5 py-0.5 rounded">{{ gitStatus.last_commit.short_sha }}</code>
                <span>by {{ gitStatus.last_commit.author }}</span>
                <span>&bull;</span>
                <span>{{ formatDate(gitStatus.last_commit.date) }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Recent Commits -->
      <div v-if="gitLog && gitLog.commits && gitLog.commits.length > 1">
        <h3 class="text-sm font-medium text-gray-900 dark:text-white mb-2">Recent Commits</h3>
        <div class="bg-gray-50 dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
          <ul class="divide-y divide-gray-200 dark:divide-gray-700 max-h-64 overflow-y-auto">
            <li v-for="commit in gitLog.commits.slice(1)" :key="commit.sha" class="px-4 py-3">
              <div class="flex items-start space-x-3">
                <code class="flex-shrink-0 bg-gray-200 dark:bg-gray-700 px-1.5 py-0.5 rounded text-xs">{{ commit.short_sha }}</code>
                <div class="flex-1 min-w-0">
                  <p class="text-sm text-gray-700 dark:text-gray-300 truncate">{{ commit.message }}</p>
                  <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                    {{ commit.author }} &bull; {{ formatDate(commit.date) }}
                  </p>
                </div>
              </div>
            </li>
          </ul>
        </div>
      </div>

      <!-- Database Config (if different from live) -->
      <div v-if="gitStatus.db_config" class="text-xs text-gray-500 dark:text-gray-400 border-t dark:border-gray-700 pt-4">
        <p>Last sync: {{ gitStatus.db_config.last_sync_at ? formatDate(gitStatus.db_config.last_sync_at) : 'Never' }}</p>
        <p v-if="gitStatus.db_config.last_commit_sha">
          Last synced commit: <code class="bg-gray-100 dark:bg-gray-700 px-1 rounded">{{ gitStatus.db_config.last_commit_sha.substring(0, 7) }}</code>
        </p>
      </div>

      <!-- GitHub PAT Settings (#347) -->
      <div class="border-t dark:border-gray-700 pt-4 mt-4">
        <h3 class="text-sm font-medium text-gray-900 dark:text-white mb-3">GitHub Authentication</h3>

        <!-- PAT Status -->
        <div v-if="patStatus" class="bg-gray-50 dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <div class="flex items-center justify-between">
            <div class="flex items-center space-x-3">
              <svg class="h-5 w-5 text-gray-500 dark:text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
              </svg>
              <div>
                <p class="text-sm font-medium text-gray-900 dark:text-white">
                  {{ patStatus.configured ? 'Agent-specific PAT' : 'Using Global PAT' }}
                </p>
                <p class="text-xs text-gray-500 dark:text-gray-400">
                  <span v-if="patStatus.configured" class="text-status-success-600 dark:text-status-success-400">Custom PAT configured for this agent</span>
                  <span v-else-if="patStatus.has_global" class="text-blue-600 dark:text-blue-400">Using system-wide GitHub PAT from Settings</span>
                  <span v-else class="text-status-warning-600 dark:text-status-warning-400">No PAT configured - git operations may fail</span>
                </p>
              </div>
            </div>
            <button
              @click="showPatModal = true"
              class="inline-flex items-center px-3 py-1.5 border border-gray-300 dark:border-gray-600 shadow-sm text-xs font-medium rounded text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500"
            >
              {{ patStatus.configured ? 'Change' : 'Configure' }}
            </button>
          </div>

          <!-- Clear button if configured -->
          <div v-if="patStatus.configured" class="mt-3 pt-3 border-t border-gray-200 dark:border-gray-600">
            <button
              @click="clearPat"
              :disabled="patSaving"
              class="text-xs text-status-danger-600 dark:text-status-danger-400 hover:text-status-danger-800 dark:hover:text-status-danger-300"
            >
              Clear agent PAT (revert to global)
            </button>
          </div>
        </div>

        <!-- Loading PAT status -->
        <div v-else class="text-center py-4">
          <div class="animate-spin rounded-full h-5 w-5 border-b-2 border-indigo-500 mx-auto"></div>
        </div>
      </div>

      <!-- PAT Configuration Modal -->
      <div v-if="showPatModal" class="fixed inset-0 z-50 overflow-y-auto" aria-labelledby="pat-modal-title" role="dialog" aria-modal="true">
        <div class="flex items-center justify-center min-h-screen pt-4 px-4 pb-20 text-center sm:block sm:p-0">
          <div class="fixed inset-0 bg-gray-500 bg-opacity-75 transition-opacity" aria-hidden="true" @click="closePatModal"></div>
          <div class="inline-block align-bottom bg-white dark:bg-gray-800 rounded-lg text-left overflow-hidden shadow-xl transform transition-all sm:my-8 sm:align-middle sm:max-w-lg sm:w-full">
            <div class="bg-white dark:bg-gray-800 px-4 pt-5 pb-4 sm:p-6 sm:pb-4">
              <div class="sm:flex sm:items-start">
                <div class="mx-auto flex-shrink-0 flex items-center justify-center h-12 w-12 rounded-full bg-indigo-100 dark:bg-indigo-900/30 sm:mx-0 sm:h-10 sm:w-10">
                  <svg class="h-6 w-6 text-indigo-600 dark:text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
                  </svg>
                </div>
                <div class="mt-3 text-center sm:mt-0 sm:ml-4 sm:text-left flex-1">
                  <h3 class="text-lg leading-6 font-medium text-gray-900 dark:text-white" id="pat-modal-title">
                    Configure GitHub PAT
                  </h3>
                  <div class="mt-4 space-y-4">
                    <!-- Error Message -->
                    <div v-if="patError" class="rounded-md bg-status-danger-50 dark:bg-status-danger-900/30 p-4">
                      <p class="text-sm text-status-danger-800 dark:text-status-danger-300">{{ patError }}</p>
                    </div>

                    <!-- Success Message -->
                    <div v-if="patSuccess" class="rounded-md bg-status-success-50 dark:bg-status-success-900/30 p-4">
                      <p class="text-sm text-status-success-800 dark:text-status-success-300">{{ patSuccess }}</p>
                    </div>

                    <!-- PAT Input -->
                    <div>
                      <label for="github-pat" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                        Personal Access Token
                      </label>
                      <input
                        type="password"
                        id="github-pat"
                        v-model="newPat"
                        placeholder="ghp_xxxxxxxxxxxx"
                        :disabled="patSaving"
                        class="mt-1 block w-full border border-gray-300 dark:border-gray-600 rounded-md shadow-sm py-2 px-3 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 sm:text-sm dark:bg-gray-700 dark:text-white font-mono"
                      />
                      <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                        Token must have <code class="bg-gray-100 dark:bg-gray-700 px-1 rounded">repo</code> scope
                      </p>
                    </div>

                    <!-- Info Box -->
                    <div class="rounded-md bg-blue-50 dark:bg-blue-900/30 p-4">
                      <div class="flex">
                        <svg class="h-5 w-5 text-blue-400 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
                          <path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clip-rule="evenodd" />
                        </svg>
                        <div class="ml-3 flex-1 text-xs text-blue-700 dark:text-blue-300">
                          <p><strong>Per-agent PAT</strong> allows this agent to use a different GitHub account than other agents.</p>
                          <p class="mt-1">The PAT is validated against GitHub and encrypted at rest.</p>
                          <p class="mt-1"><strong>Note:</strong> Restart the agent for the new PAT to take effect in git operations.</p>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
            <div class="bg-gray-50 dark:bg-gray-700 px-4 py-3 sm:px-6 sm:flex sm:flex-row-reverse">
              <button
                type="button"
                @click="savePat"
                :disabled="!newPat || patSaving"
                class="w-full inline-flex justify-center rounded-md border border-transparent shadow-sm px-4 py-2 bg-indigo-600 text-base font-medium text-white hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 sm:ml-3 sm:w-auto sm:text-sm disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <svg v-if="patSaving" class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                  <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                </svg>
                {{ patSaving ? 'Validating...' : 'Save PAT' }}
              </button>
              <button
                type="button"
                @click="closePatModal"
                :disabled="patSaving"
                class="mt-3 w-full inline-flex justify-center rounded-md border border-gray-300 dark:border-gray-600 shadow-sm px-4 py-2 bg-white dark:bg-gray-800 text-base font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 sm:mt-0 sm:ml-3 sm:w-auto sm:text-sm disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, watch } from 'vue'
import { useAgentsStore } from '../stores/agents'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  },
  agentStatus: {
    type: String,
    default: 'stopped'
  }
})

const agentsStore = useAgentsStore()

const loading = ref(false)
const gitStatus = ref(null)
const gitLog = ref(null)

// Initialize GitHub modal state
const showInitializeModal = ref(false)
const initializing = ref(false)
const initializeError = ref(null)
const repoOwner = ref('')
const repoName = ref('')
const createRepo = ref(true)
const privateRepo = ref(true)

// PAT configuration state (#347)
const patStatus = ref(null)
const showPatModal = ref(false)
const newPat = ref('')
const patSaving = ref(false)
const patError = ref(null)
const patSuccess = ref(null)

const loadGitStatus = async () => {
  loading.value = true
  try {
    gitStatus.value = await agentsStore.getGitStatus(props.agentName)
    // Also load git log if status shows git is enabled
    if (gitStatus.value?.git_enabled && gitStatus.value?.branch) {
      gitLog.value = await agentsStore.getGitLog(props.agentName, 10)
      // Load PAT status when git is enabled (#347)
      await loadPatStatus()
    }
  } catch (err) {
    console.error('Failed to load git status:', err)
    gitStatus.value = { git_enabled: false }
  } finally {
    loading.value = false
  }
}

const initializeGitHub = async () => {
  if (!repoOwner.value || !repoName.value) return

  initializing.value = true
  initializeError.value = null

  console.log('[GitPanel] Starting GitHub initialization...')

  try {
    const response = await agentsStore.initializeGitHub(props.agentName, {
      repo_owner: repoOwner.value,
      repo_name: repoName.value,
      create_repo: createRepo.value,
      private: privateRepo.value
    })

    console.log('[GitPanel] GitHub initialization successful:', response)

    // Success! Reload git status and close modal
    console.log('[GitPanel] Reloading git status...')
    await loadGitStatus()

    console.log('[GitPanel] Closing modal...')
    showInitializeModal.value = false

    // Clear form
    repoOwner.value = ''
    repoName.value = ''
  } catch (error) {
    console.error('[GitPanel] GitHub initialization failed:', error)
    initializeError.value = error.response?.data?.detail || error.message || 'Failed to initialize GitHub sync'
  } finally {
    console.log('[GitPanel] Initialization complete, setting initializing = false')
    initializing.value = false
  }
}

const closeInitializeModal = () => {
  if (initializing.value) return
  showInitializeModal.value = false
  initializeError.value = null
  repoOwner.value = ''
  repoName.value = ''
}

// PAT management methods (#347)
const loadPatStatus = async () => {
  try {
    patStatus.value = await agentsStore.getGitHubPATStatus(props.agentName)
  } catch (err) {
    console.error('Failed to load PAT status:', err)
    patStatus.value = { configured: false, source: 'global', has_global: false }
  }
}

const savePat = async () => {
  if (!newPat.value) return

  patSaving.value = true
  patError.value = null
  patSuccess.value = null

  try {
    const response = await agentsStore.setGitHubPAT(props.agentName, newPat.value)
    patSuccess.value = `PAT configured successfully for GitHub user: ${response.github_username}`
    await loadPatStatus()
    // Close modal after short delay to show success message
    setTimeout(() => {
      closePatModal()
    }, 1500)
  } catch (error) {
    patError.value = error.response?.data?.detail || error.message || 'Failed to save PAT'
  } finally {
    patSaving.value = false
  }
}

const clearPat = async () => {
  if (!confirm('Clear the agent-specific PAT and revert to using the global PAT?')) return

  patSaving.value = true
  try {
    await agentsStore.clearGitHubPAT(props.agentName)
    await loadPatStatus()
  } catch (error) {
    console.error('Failed to clear PAT:', error)
  } finally {
    patSaving.value = false
  }
}

const closePatModal = () => {
  if (patSaving.value) return
  showPatModal.value = false
  newPat.value = ''
  patError.value = null
  patSuccess.value = null
}

const formatDate = (dateString) => {
  if (!dateString) return 'Unknown'
  const date = new Date(dateString)
  const now = new Date()
  const diffSeconds = Math.floor((now - date) / 1000)

  if (diffSeconds < 60) return 'just now'
  if (diffSeconds < 3600) return `${Math.floor(diffSeconds / 60)}m ago`
  if (diffSeconds < 86400) return `${Math.floor(diffSeconds / 3600)}h ago`
  if (diffSeconds < 604800) return `${Math.floor(diffSeconds / 86400)}d ago`
  return date.toLocaleDateString()
}

const getChangeStatusClass = (status) => {
  switch (status) {
    case 'M': return 'bg-status-warning-100 dark:bg-status-warning-900/30 text-status-warning-800 dark:text-status-warning-300'  // Modified
    case 'A': return 'bg-status-success-100 dark:bg-status-success-900/30 text-status-success-800 dark:text-status-success-300'   // Added
    case 'D': return 'bg-status-danger-100 dark:bg-status-danger-900/30 text-status-danger-800 dark:text-status-danger-300'       // Deleted
    case '?': return 'bg-blue-100 dark:bg-blue-900/30 text-blue-800 dark:text-blue-300'     // Untracked
    case 'R': return 'bg-accent-purple-100 dark:bg-accent-purple-900/30 text-accent-purple-800 dark:text-accent-purple-300' // Renamed
    default: return 'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-300'
  }
}

// Reload when agent name changes (navigating between agents)
watch(() => props.agentName, (newName, oldName) => {
  if (newName && newName !== oldName) {
    // Reset state for new agent
    gitStatus.value = null
    gitLog.value = null
    loadGitStatus()
  }
})

// Watch for agent status changes
watch(() => props.agentStatus, (newStatus) => {
  if (newStatus === 'running') {
    loadGitStatus()
  }
})

onMounted(() => {
  loadGitStatus()
})
</script>
