<template>
  <div class="space-y-6">
    <!-- Header -->
    <div class="flex items-center justify-between">
      <div>
        <h3 class="text-lg font-medium text-gray-900 dark:text-white">Public Links</h3>
        <p class="text-sm text-gray-500 dark:text-gray-400">
          Generate shareable links that allow anyone to chat with this agent.
        </p>
      </div>
      <button
        @click="showCreateModal = true"
        class="inline-flex items-center px-3 py-2 border border-transparent text-sm font-medium rounded-md text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 dark:focus:ring-offset-gray-800"
      >
        <svg class="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
        </svg>
        Create Link
      </button>
    </div>

    <!-- Loading state -->
    <div v-if="loading" class="text-center py-8">
      <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-indigo-500 mx-auto"></div>
      <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">Loading links...</p>
    </div>

    <!-- Empty state -->
    <div v-else-if="links.length === 0" class="text-center py-12 bg-gray-50 dark:bg-gray-900/50 rounded-lg border border-dashed border-gray-300 dark:border-gray-700">
      <svg class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
      </svg>
      <h3 class="mt-2 text-sm font-medium text-gray-900 dark:text-white">No public links</h3>
      <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Create a link to share this agent with others.</p>
      <button
        @click="showCreateModal = true"
        class="mt-4 inline-flex items-center px-3 py-2 text-sm font-medium text-indigo-600 dark:text-indigo-400 hover:text-indigo-700 dark:hover:text-indigo-300"
      >
        <svg class="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
        </svg>
        Create your first link
      </button>
    </div>

    <!-- Links list -->
    <div v-else class="space-y-3">
      <div
        v-for="link in links"
        :key="link.id"
        class="bg-gray-50 dark:bg-gray-900/50 rounded-lg border border-gray-200 dark:border-gray-700 p-4"
      >
        <div class="flex items-start justify-between">
          <div class="flex-1 min-w-0">
            <!-- Link name and status -->
            <div class="flex items-center space-x-2">
              <h4 class="text-sm font-medium text-gray-900 dark:text-white truncate">
                {{ link.name || 'Unnamed Link' }}
              </h4>
              <span
                :class="[
                  'inline-flex items-center px-2 py-0.5 rounded text-xs font-medium',
                  link.enabled
                    ? 'bg-status-success-100 dark:bg-status-success-900/30 text-status-success-800 dark:text-status-success-300'
                    : 'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-300'
                ]"
              >
                {{ link.enabled ? 'Active' : 'Disabled' }}
              </span>
              <span
                v-if="link.link_type === 'site'"
                class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300"
              >
                Website
              </span>
            </div>

            <!-- URL preview -->
            <div class="mt-2 flex items-center space-x-2">
              <code class="flex-1 text-xs text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-gray-800 px-2 py-1 rounded truncate">
                {{ link.external_url || link.url }}
              </code>
              <button
                @click="copyLink(link)"
                class="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 rounded hover:bg-gray-200 dark:hover:bg-gray-700"
                title="Copy link"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                </svg>
              </button>
            </div>

            <!-- Stats -->
            <div v-if="link.usage_stats" class="mt-2 flex items-center space-x-4 text-xs text-gray-500 dark:text-gray-400">
              <span>{{ link.usage_stats.total_messages || 0 }} messages</span>
              <span>{{ link.usage_stats.unique_users || 0 }} users</span>
              <span v-if="link.usage_stats.last_used_at">
                Last used: {{ formatDate(link.usage_stats.last_used_at) }}
              </span>
            </div>

            <!-- Expiration -->
            <div v-if="link.expires_at" class="mt-2 text-xs text-gray-500 dark:text-gray-400">
              <span v-if="isExpired(link.expires_at)" class="text-status-danger-500 dark:text-status-danger-400">
                Expired {{ formatDate(link.expires_at) }}
              </span>
              <span v-else>
                Expires {{ formatDate(link.expires_at) }}
              </span>
            </div>

            <!-- Slack Integration Section -->
            <div class="mt-3 pt-3 border-t border-gray-200 dark:border-gray-700">
              <div class="flex items-center justify-between">
                <div class="flex items-center space-x-2">
                  <!-- Slack Icon -->
                  <svg class="w-4 h-4 text-gray-400" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zM18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312zM15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zM15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z"/>
                  </svg>
                  <span class="text-xs font-medium text-gray-600 dark:text-gray-400">Slack</span>
                </div>

                <!-- Not Connected State -->
                <div v-if="!slackConnections[link.id]?.connected">
                  <button
                    @click="connectSlack(link)"
                    :disabled="slackLoading[link.id]"
                    class="inline-flex items-center px-2 py-1 text-xs font-medium text-indigo-600 dark:text-indigo-400 hover:text-indigo-700 dark:hover:text-indigo-300 disabled:opacity-50"
                  >
                    <span v-if="slackLoading[link.id]">Connecting...</span>
                    <span v-else>Connect Slack</span>
                  </button>
                </div>

                <!-- Connected State -->
                <div v-else class="flex items-center space-x-2">
                  <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-status-success-100 dark:bg-status-success-900/30 text-status-success-800 dark:text-status-success-300">
                    <span class="w-1.5 h-1.5 mr-1.5 bg-status-success-500 rounded-full"></span>
                    {{ slackConnections[link.id].team_name || 'Connected' }}
                  </span>
                  <button
                    @click="toggleSlackEnabled(link)"
                    :disabled="slackLoading[link.id]"
                    class="p-1 rounded hover:bg-gray-200 dark:hover:bg-gray-700 disabled:opacity-50"
                    :title="slackConnections[link.id].enabled ? 'Disable Slack' : 'Enable Slack'"
                  >
                    <svg v-if="slackConnections[link.id].enabled" class="w-3.5 h-3.5 text-status-warning-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" />
                    </svg>
                    <svg v-else class="w-3.5 h-3.5 text-status-success-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                  </button>
                  <button
                    @click="disconnectSlack(link)"
                    :disabled="slackLoading[link.id]"
                    class="p-1 rounded hover:bg-gray-200 dark:hover:bg-gray-700 disabled:opacity-50"
                    title="Disconnect Slack"
                  >
                    <svg class="w-3.5 h-3.5 text-status-danger-400 hover:text-status-danger-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                    </svg>
                  </button>
                </div>
              </div>
              <!-- Connected By Info -->
              <div v-if="slackConnections[link.id]?.connected" class="mt-1 text-xs text-gray-400 dark:text-gray-500">
                Connected by {{ slackConnections[link.id].connected_by }} on {{ formatDate(slackConnections[link.id].connected_at) }}
              </div>
            </div>
          </div>

          <!-- Actions -->
          <div class="flex items-center space-x-2 ml-4">
            <button
              @click="toggleLink(link)"
              :disabled="actionLoading === link.id"
              class="p-1.5 rounded hover:bg-gray-200 dark:hover:bg-gray-700 disabled:opacity-50"
              :title="link.enabled ? 'Disable link' : 'Enable link'"
            >
              <svg v-if="link.enabled" class="w-4 h-4 text-gray-400 hover:text-status-warning-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" />
              </svg>
              <svg v-else class="w-4 h-4 text-gray-400 hover:text-status-success-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            </button>
            <button
              @click="editLink(link)"
              class="p-1.5 rounded hover:bg-gray-200 dark:hover:bg-gray-700"
              title="Edit link"
            >
              <svg class="w-4 h-4 text-gray-400 hover:text-indigo-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
              </svg>
            </button>
            <button
              @click="confirmDelete(link)"
              :disabled="actionLoading === link.id"
              class="p-1.5 rounded hover:bg-gray-200 dark:hover:bg-gray-700 disabled:opacity-50"
              title="Delete link"
            >
              <svg class="w-4 h-4 text-gray-400 hover:text-status-danger-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
              </svg>
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Create/Edit Modal -->
    <div v-if="showCreateModal || editingLink" class="fixed inset-0 z-50 overflow-y-auto">
      <div class="flex items-center justify-center min-h-screen px-4 pt-4 pb-20 text-center sm:p-0">
        <div class="fixed inset-0 transition-opacity" @click="closeModal">
          <div class="absolute inset-0 bg-gray-500 dark:bg-gray-900 opacity-75"></div>
        </div>

        <div class="relative inline-block w-full max-w-md bg-white dark:bg-gray-800 rounded-lg text-left overflow-hidden shadow-xl transform transition-all sm:my-8">
          <form @submit.prevent="saveLink">
            <div class="px-6 pt-5 pb-4">
              <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-4">
                {{ editingLink ? 'Edit Public Link' : 'Create Public Link' }}
              </h3>

              <div class="space-y-4">
                <!-- Name -->
                <div>
                  <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Name (optional)
                  </label>
                  <input
                    v-model="formData.name"
                    type="text"
                    placeholder="e.g., Customer Support Demo"
                    class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-white focus:ring-indigo-500 focus:border-indigo-500"
                  />
                </div>

                <!-- Link Type (create only — immutable after creation) -->
                <div v-if="!editingLink">
                  <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                    Link Type
                  </label>
                  <div class="grid grid-cols-2 gap-3">
                    <button
                      type="button"
                      @click="formData.link_type = 'chat'"
                      :class="[
                        'flex flex-col items-start p-3 rounded-lg border-2 text-left transition-colors',
                        formData.link_type === 'chat'
                          ? 'border-indigo-500 bg-indigo-50 dark:bg-indigo-900/20'
                          : 'border-gray-200 dark:border-gray-600 hover:border-gray-300 dark:hover:border-gray-500'
                      ]"
                    >
                      <div class="flex items-center mb-1">
                        <svg class="w-4 h-4 mr-1.5 text-indigo-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                        </svg>
                        <span class="text-sm font-medium text-gray-900 dark:text-white">Chat</span>
                      </div>
                      <span class="text-xs text-gray-500 dark:text-gray-400">Public chat interface</span>
                    </button>
                    <button
                      type="button"
                      @click="formData.link_type = 'site'"
                      :class="[
                        'flex flex-col items-start p-3 rounded-lg border-2 text-left transition-colors',
                        formData.link_type === 'site'
                          ? 'border-indigo-500 bg-indigo-50 dark:bg-indigo-900/20'
                          : 'border-gray-200 dark:border-gray-600 hover:border-gray-300 dark:hover:border-gray-500'
                      ]"
                    >
                      <div class="flex items-center mb-1">
                        <svg class="w-4 h-4 mr-1.5 text-indigo-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9" />
                        </svg>
                        <span class="text-sm font-medium text-gray-900 dark:text-white">Website</span>
                      </div>
                      <span class="text-xs text-gray-500 dark:text-gray-400">Proxy agent's web server</span>
                    </button>
                  </div>
                  <p v-if="formData.link_type === 'site'" class="mt-2 text-xs text-amber-600 dark:text-amber-400">
                    Requires the agent to run a web server on port 3000 (e.g. Next.js).
                  </p>
                </div>

                <!-- Access policy notice (email verification / open access is managed at the agent level, #311) -->
                <p class="text-xs text-gray-500 dark:text-gray-400">
                  Email verification and allow-list are controlled by the agent's
                  <span class="font-medium">Channel Access Policy</span> above —
                  the same policy applies across web, Telegram, and Slack.
                </p>

                <!-- Expiration -->
                <div>
                  <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Expiration (optional)
                  </label>
                  <input
                    v-model="formData.expires_at"
                    type="datetime-local"
                    class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-white focus:ring-indigo-500 focus:border-indigo-500"
                  />
                </div>

                <!-- Enabled (only for edit) -->
                <div v-if="editingLink" class="flex items-center">
                  <input
                    id="enabled"
                    v-model="formData.enabled"
                    type="checkbox"
                    class="h-4 w-4 text-indigo-600 focus:ring-indigo-500 border-gray-300 dark:border-gray-600 rounded"
                  />
                  <label for="enabled" class="ml-2 block text-sm text-gray-700 dark:text-gray-300">
                    Link enabled
                  </label>
                </div>
              </div>

              <!-- Error message -->
              <div v-if="formError" class="mt-4 p-3 bg-status-danger-100 dark:bg-status-danger-900/30 border border-status-danger-200 dark:border-status-danger-800 rounded-md">
                <p class="text-sm text-status-danger-600 dark:text-status-danger-400">{{ formError }}</p>
              </div>
            </div>

            <div class="px-6 py-3 bg-gray-50 dark:bg-gray-900/50 flex justify-end space-x-3">
              <button
                type="button"
                @click="closeModal"
                class="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white"
              >
                Cancel
              </button>
              <button
                type="submit"
                :disabled="formLoading"
                class="px-4 py-2 text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 rounded-md disabled:bg-indigo-400"
              >
                <span v-if="formLoading" class="flex items-center">
                  <svg class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                  </svg>
                  Saving...
                </span>
                <span v-else>{{ editingLink ? 'Save Changes' : 'Create Link' }}</span>
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>

    <!-- Delete Confirmation Modal -->
    <div v-if="deletingLink" class="fixed inset-0 z-50 overflow-y-auto">
      <div class="flex items-center justify-center min-h-screen px-4 pt-4 pb-20 text-center sm:p-0">
        <div class="fixed inset-0 transition-opacity" @click="deletingLink = null">
          <div class="absolute inset-0 bg-gray-500 dark:bg-gray-900 opacity-75"></div>
        </div>

        <div class="relative inline-block w-full max-w-sm bg-white dark:bg-gray-800 rounded-lg text-left overflow-hidden shadow-xl transform transition-all sm:my-8">
          <div class="px-6 pt-5 pb-4">
            <div class="flex items-center">
              <div class="flex-shrink-0 w-10 h-10 bg-status-danger-100 dark:bg-status-danger-900/30 rounded-full flex items-center justify-center">
                <svg class="w-6 h-6 text-status-danger-600 dark:text-status-danger-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                </svg>
              </div>
              <div class="ml-3">
                <h3 class="text-lg font-medium text-gray-900 dark:text-white">Delete Link</h3>
                <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                  Are you sure you want to delete this link? All sessions will be invalidated.
                </p>
              </div>
            </div>
          </div>

          <div class="px-6 py-3 bg-gray-50 dark:bg-gray-900/50 flex justify-end space-x-3">
            <button
              type="button"
              @click="deletingLink = null"
              class="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white"
            >
              Cancel
            </button>
            <button
              @click="deleteLink"
              :disabled="actionLoading === deletingLink?.id"
              class="px-4 py-2 text-sm font-medium text-white bg-status-danger-600 hover:bg-status-danger-700 rounded-md disabled:bg-status-danger-400"
            >
              Delete
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Copy/Status notification -->
    <div
      v-if="copyNotification"
      class="fixed bottom-4 right-4 px-4 py-2 bg-status-success-600 text-white rounded-lg shadow-lg text-sm z-50"
    >
      <span v-if="copyNotification === 'slack-connected'">Slack workspace connected successfully!</span>
      <span v-else>Link copied!</span>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, watch } from 'vue'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  }
})

const authStore = useAuthStore()

// State
const loading = ref(false)
const links = ref([])
const showCreateModal = ref(false)
const editingLink = ref(null)
const deletingLink = ref(null)
const actionLoading = ref(null)
const copyNotification = ref(false)

// Slack state
const slackConnections = ref({})  // { linkId: { connected, team_name, enabled, ... } }
const slackLoading = ref({})      // { linkId: true/false }

// Form
const formData = ref({
  name: '',
  expires_at: '',
  enabled: true,
  link_type: 'chat',
})
const formLoading = ref(false)
const formError = ref(null)

// Load links
const loadLinks = async () => {
  loading.value = true
  try {
    const response = await axios.get(`/api/agents/${props.agentName}/public-links`, {
      headers: authStore.authHeader
    })
    links.value = response.data
    // Load Slack connections for all links
    await loadAllSlackConnections()
  } catch (err) {
    console.error('Failed to load public links:', err)
  } finally {
    loading.value = false
  }
}

// Save link (create or update)
const saveLink = async () => {
  formLoading.value = true
  formError.value = null

  try {
    const payload = {
      name: formData.value.name || null,
    }

    if (formData.value.expires_at) {
      payload.expires_at = new Date(formData.value.expires_at).toISOString()
    }

    if (editingLink.value) {
      payload.enabled = formData.value.enabled
      await axios.put(
        `/api/agents/${props.agentName}/public-links/${editingLink.value.id}`,
        payload,
        { headers: authStore.authHeader }
      )
    } else {
      payload.link_type = formData.value.link_type
      await axios.post(
        `/api/agents/${props.agentName}/public-links`,
        payload,
        { headers: authStore.authHeader }
      )
    }

    closeModal()
    await loadLinks()
  } catch (err) {
    console.error('Failed to save link:', err)
    formError.value = err.response?.data?.detail || 'Failed to save link'
  } finally {
    formLoading.value = false
  }
}

// Toggle link enabled/disabled
const toggleLink = async (link) => {
  actionLoading.value = link.id
  try {
    await axios.put(
      `/api/agents/${props.agentName}/public-links/${link.id}`,
      { enabled: !link.enabled },
      { headers: authStore.authHeader }
    )
    await loadLinks()
  } catch (err) {
    console.error('Failed to toggle link:', err)
  } finally {
    actionLoading.value = null
  }
}

// Edit link
const editLink = (link) => {
  editingLink.value = link
  formData.value = {
    name: link.name || '',
    expires_at: link.expires_at ? link.expires_at.slice(0, 16) : '',
    enabled: link.enabled
  }
}

// Confirm delete
const confirmDelete = (link) => {
  deletingLink.value = link
}

// Delete link
const deleteLink = async () => {
  if (!deletingLink.value) return

  actionLoading.value = deletingLink.value.id
  try {
    await axios.delete(
      `/api/agents/${props.agentName}/public-links/${deletingLink.value.id}`,
      { headers: authStore.authHeader }
    )
    deletingLink.value = null
    await loadLinks()
  } catch (err) {
    console.error('Failed to delete link:', err)
  } finally {
    actionLoading.value = null
  }
}

// Load Slack connection status for a link
const loadSlackConnection = async (linkId) => {
  try {
    const response = await axios.get(
      `/api/agents/${props.agentName}/public-links/${linkId}/slack`,
      { headers: authStore.authHeader }
    )
    slackConnections.value[linkId] = response.data
  } catch (err) {
    console.error('Failed to load Slack connection:', err)
    slackConnections.value[linkId] = { connected: false }
  }
}

// Load all Slack connections
const loadAllSlackConnections = async () => {
  for (const link of links.value) {
    await loadSlackConnection(link.id)
  }
}

// Connect Slack to a link
const connectSlack = async (link) => {
  slackLoading.value[link.id] = true
  try {
    const response = await axios.post(
      `/api/agents/${props.agentName}/public-links/${link.id}/slack/connect`,
      {},
      { headers: authStore.authHeader }
    )
    const data = response.data

    if (data.status === 'connected') {
      // Workspace already connected — channel was created and bound
      await loadSlackConnection(link.id)
      copyNotification.value = 'slack-connected'
      setTimeout(() => { copyNotification.value = null }, 3000)
    } else if (data.status === 'oauth_required' && data.oauth_url) {
      // Need OAuth — redirect to Slack
      window.location.href = data.oauth_url
    } else if (data.oauth_url) {
      // Legacy response format
      window.location.href = data.oauth_url
    }
  } catch (err) {
    console.error('Failed to initiate Slack connection:', err)
    alert(err.response?.data?.detail || 'Failed to connect Slack')
  } finally {
    slackLoading.value[link.id] = false
  }
}

// Toggle Slack enabled/disabled
const toggleSlackEnabled = async (link) => {
  slackLoading.value[link.id] = true
  try {
    const currentEnabled = slackConnections.value[link.id]?.enabled ?? true
    await axios.put(
      `/api/agents/${props.agentName}/public-links/${link.id}/slack`,
      { enabled: !currentEnabled },
      { headers: authStore.authHeader }
    )
    await loadSlackConnection(link.id)
  } catch (err) {
    console.error('Failed to toggle Slack:', err)
  } finally {
    slackLoading.value[link.id] = false
  }
}

// Disconnect Slack
const disconnectSlack = async (link) => {
  if (!confirm('Are you sure you want to disconnect Slack from this link?')) return

  slackLoading.value[link.id] = true
  try {
    await axios.delete(
      `/api/agents/${props.agentName}/public-links/${link.id}/slack`,
      { headers: authStore.authHeader }
    )
    slackConnections.value[link.id] = { connected: false }
  } catch (err) {
    console.error('Failed to disconnect Slack:', err)
  } finally {
    slackLoading.value[link.id] = false
  }
}

// Copy link to clipboard
const copyLink = async (link) => {
  try {
    const url = link.external_url || link.url
    await navigator.clipboard.writeText(url)
    copyNotification.value = 'copied'
    setTimeout(() => {
      copyNotification.value = false
    }, 2000)
  } catch (err) {
    console.error('Failed to copy link:', err)
  }
}

// Close modal
const closeModal = () => {
  showCreateModal.value = false
  editingLink.value = null
  formData.value = {
    name: '',
    expires_at: '',
    enabled: true,
    link_type: 'chat',
  }
  formError.value = null
}

// Format date
const formatDate = (dateStr) => {
  if (!dateStr) return ''
  const date = new Date(dateStr)
  return date.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit'
  })
}

// Check if expired
const isExpired = (dateStr) => {
  if (!dateStr) return false
  return new Date(dateStr) < new Date()
}

// Watch for agent name changes
watch(() => props.agentName, () => {
  loadLinks()
}, { immediate: true })

onMounted(() => {
  loadLinks()

  // Check for Slack OAuth callback result
  const urlParams = new URLSearchParams(window.location.search)
  const slackStatus = urlParams.get('slack')
  if (slackStatus === 'connected') {
    copyNotification.value = 'slack-connected'
    setTimeout(() => {
      copyNotification.value = false
    }, 3000)
    // Clean up URL
    urlParams.delete('slack')
    const newUrl = window.location.pathname + (urlParams.toString() ? '?' + urlParams.toString() : '')
    window.history.replaceState({}, '', newUrl)
  } else if (slackStatus === 'error') {
    const reason = urlParams.get('reason') || 'unknown'
    alert(`Failed to connect Slack: ${reason}`)
    // Clean up URL
    urlParams.delete('slack')
    urlParams.delete('reason')
    const newUrl = window.location.pathname + (urlParams.toString() ? '?' + urlParams.toString() : '')
    window.history.replaceState({}, '', newUrl)
  }
})
</script>
