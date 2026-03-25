<template>
  <div class="min-h-screen bg-gray-100 dark:bg-gray-900">
    <NavBar />

    <main class="max-w-4xl mx-auto py-6 sm:px-6 lg:px-8">
      <div class="px-4 py-6 sm:px-0">
        <div class="mb-8">
          <h1 class="text-3xl font-bold text-gray-900 dark:text-white">Settings</h1>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
            System-wide configuration for the Trinity platform
          </p>
        </div>

        <!-- Loading State -->
        <div v-if="loading" class="bg-white dark:bg-gray-800 rounded-lg shadow dark:shadow-gray-900 p-8 text-center">
          <div class="animate-spin rounded-full h-12 w-12 border-b-2 border-indigo-600 mx-auto"></div>
          <p class="mt-4 text-gray-500 dark:text-gray-400">Loading settings...</p>
        </div>

        <!-- Settings Content -->
        <div v-else class="space-y-6">
          <!-- Platform Section -->
          <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
            <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <h2 class="text-lg font-medium text-gray-900 dark:text-white">Platform</h2>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                Core platform configuration.
              </p>
            </div>

            <div class="px-6 py-4">
              <div class="space-y-4">
                <!-- Public URL -->
                <div>
                  <label for="public-url" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                    Public URL
                  </label>
                  <div class="mt-1 flex gap-2">
                    <input
                      type="url"
                      id="public-url"
                      v-model="publicUrl"
                      :placeholder="publicUrlCurrent || 'https://your-domain.com'"
                      :disabled="savingPublicUrl"
                      class="block flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 dark:bg-gray-700 dark:text-white text-sm"
                    />
                    <button
                      @click="savePublicUrl"
                      :disabled="!publicUrl || savingPublicUrl"
                      class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <svg v-if="savingPublicUrl" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                      </svg>
                      Save
                    </button>
                  </div>
                  <!-- Status -->
                  <div class="mt-2 flex items-center text-sm">
                    <template v-if="publicUrlSaveSuccess">
                      <svg class="h-4 w-4 text-green-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                      </svg>
                      <span class="text-green-600 dark:text-green-400">Saved</span>
                    </template>
                    <template v-else-if="publicUrlCurrent">
                      <svg class="h-4 w-4 text-green-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                      </svg>
                      <span class="text-green-600 dark:text-green-400">
                        {{ publicUrlCurrent }}
                      </span>
                    </template>
                    <template v-else>
                      <svg class="h-4 w-4 text-amber-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                      </svg>
                      <span class="text-amber-600 dark:text-amber-400">
                        Not configured — required for Telegram bots and public links
                      </span>
                    </template>
                  </div>
                  <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">
                    The externally-accessible URL of this Trinity instance (e.g. <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs">https://your-domain.com</code>).
                    Used for Telegram webhooks, Slack OAuth callbacks, and shareable public links.
                  </p>
                </div>
              </div>
            </div>
          </div>

          <!-- API Keys Section -->
          <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
            <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <h2 class="text-lg font-medium text-gray-900 dark:text-white">API Keys</h2>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                Configure API keys required for agent operation.
              </p>
            </div>

            <div class="px-6 py-4">
              <div class="space-y-4">
                <!-- Anthropic API Key -->
                <div>
                  <label for="anthropic-key" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                    Anthropic API Key
                  </label>
                  <div class="mt-1 flex gap-2">
                    <div class="relative flex-1">
                      <input
                        :type="showApiKey ? 'text' : 'password'"
                        id="anthropic-key"
                        v-model="anthropicKey"
                        :placeholder="anthropicKeyStatus.configured ? anthropicKeyStatus.masked : 'sk-ant-...'"
                        :disabled="savingApiKey"
                        class="block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 dark:bg-gray-700 dark:text-white font-mono text-sm"
                      />
                      <button
                        type="button"
                        @click="showApiKey = !showApiKey"
                        class="absolute inset-y-0 right-0 pr-3 flex items-center text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                      >
                        <svg v-if="showApiKey" class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
                        </svg>
                        <svg v-else class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                        </svg>
                      </button>
                    </div>
                    <button
                      @click="testApiKey"
                      :disabled="!anthropicKey || testingApiKey"
                      class="inline-flex items-center px-4 py-2 border border-gray-300 dark:border-gray-600 shadow-sm text-sm font-medium rounded-md text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <svg v-if="testingApiKey" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                      </svg>
                      Test
                    </button>
                    <button
                      @click="saveApiKey"
                      :disabled="!anthropicKey || savingApiKey"
                      class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <svg v-if="savingApiKey" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                      </svg>
                      Save
                    </button>
                  </div>
                  <!-- Status/Result -->
                  <div class="mt-2 flex items-center text-sm">
                    <template v-if="apiKeyTestResult !== null">
                      <svg v-if="apiKeyTestResult" class="h-4 w-4 text-green-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                      </svg>
                      <svg v-else class="h-4 w-4 text-red-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                      </svg>
                      <span :class="apiKeyTestResult ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'">
                        {{ apiKeyTestMessage }}
                      </span>
                    </template>
                    <template v-else-if="anthropicKeyStatus.configured">
                      <svg class="h-4 w-4 text-green-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                      </svg>
                      <span class="text-green-600 dark:text-green-400">
                        Configured
                        <span class="text-gray-500 dark:text-gray-400">
                          ({{ anthropicKeyStatus.source === 'settings' ? 'from settings' : 'from environment' }})
                        </span>
                      </span>
                    </template>
                    <template v-else>
                      <svg class="h-4 w-4 text-amber-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                      </svg>
                      <span class="text-amber-600 dark:text-amber-400">
                        Not configured - required for agents
                      </span>
                    </template>
                  </div>
                  <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">
                    Required for agents to use Claude. Get your key at
                    <a href="https://console.anthropic.com" target="_blank" class="text-indigo-600 dark:text-indigo-400 hover:underline">
                      console.anthropic.com
                    </a>
                  </p>
                </div>

                <!-- GitHub Personal Access Token -->
                <div class="mt-6">
                  <label for="github-pat" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                    GitHub Personal Access Token (PAT)
                  </label>
                  <div class="mt-1 flex gap-2">
                    <div class="relative flex-1">
                      <input
                        :type="showGithubPat ? 'text' : 'password'"
                        id="github-pat"
                        v-model="githubPat"
                        :placeholder="githubPatStatus.configured ? githubPatStatus.masked : 'ghp_... or github_pat_...'"
                        :disabled="savingGithubPat"
                        class="block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 dark:bg-gray-700 dark:text-white font-mono text-sm"
                      />
                      <button
                        type="button"
                        @click="showGithubPat = !showGithubPat"
                        class="absolute inset-y-0 right-0 pr-3 flex items-center text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                      >
                        <svg v-if="showGithubPat" class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
                        </svg>
                        <svg v-else class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                        </svg>
                      </button>
                    </div>
                    <button
                      @click="testGithubPat"
                      :disabled="!githubPat || testingGithubPat"
                      class="inline-flex items-center px-4 py-2 border border-gray-300 dark:border-gray-600 shadow-sm text-sm font-medium rounded-md text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <svg v-if="testingGithubPat" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                      </svg>
                      Test
                    </button>
                    <button
                      @click="saveGithubPat"
                      :disabled="!githubPat || savingGithubPat"
                      class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <svg v-if="savingGithubPat" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                      </svg>
                      Save
                    </button>
                  </div>
                  <!-- Status/Result -->
                  <div class="mt-2 flex items-center text-sm">
                    <template v-if="githubPatTestResult !== null">
                      <svg v-if="githubPatTestResult" class="h-4 w-4 text-green-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                      </svg>
                      <svg v-else class="h-4 w-4 text-red-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                      </svg>
                      <span :class="githubPatTestResult ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'">
                        {{ githubPatTestMessage }}
                      </span>
                    </template>
                    <template v-else-if="githubPatStatus.configured">
                      <svg class="h-4 w-4 text-green-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                      </svg>
                      <span class="text-green-600 dark:text-green-400">
                        Configured
                        <span class="text-gray-500 dark:text-gray-400">
                          ({{ githubPatStatus.source === 'settings' ? 'from settings' : 'from environment' }})
                        </span>
                      </span>
                    </template>
                    <template v-else>
                      <svg class="h-4 w-4 text-gray-400 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                      </svg>
                      <span class="text-gray-600 dark:text-gray-400">
                        Optional - required for GitHub repository initialization
                      </span>
                    </template>
                  </div>
                  <!-- Propagation result (#211) -->
                  <div v-if="githubPatPropagation" class="mt-2 text-sm">
                    <template v-if="githubPatPropagation.error">
                      <div class="text-red-600 dark:text-red-400">
                        PAT saved, but propagation failed: {{ githubPatPropagation.error }}
                      </div>
                    </template>
                    <template v-else-if="githubPatPropagation.total_running === 0">
                      <div class="text-gray-600 dark:text-gray-400">
                        PAT updated. No running agents to propagate to.
                      </div>
                    </template>
                    <template v-else>
                      <div :class="githubPatPropagation.failed.length ? 'text-yellow-700 dark:text-yellow-400' : 'text-green-600 dark:text-green-400'">
                        PAT updated and applied to {{ githubPatPropagation.updated.length }} of {{ githubPatPropagation.total_running }} running agent{{ githubPatPropagation.total_running === 1 ? '' : 's' }}.
                      </div>
                      <div v-if="githubPatPropagation.failed.length" class="mt-1 text-red-600 dark:text-red-400">
                        Failed: {{ githubPatPropagation.failed.map(a => a.agent_name).join(', ') }}
                      </div>
                      <div v-if="githubPatPropagation.skipped.length" class="mt-1 text-gray-500 dark:text-gray-400">
                        Skipped: {{ githubPatPropagation.skipped.map(a => `${a.agent_name} (${a.status === 'skipped_per_agent_pat' ? 'per-agent PAT' : 'no GITHUB_PAT'})`).join(', ') }}
                      </div>
                    </template>
                  </div>
                  <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">
                    Required for creating and pushing agents to GitHub repositories. Get your token at
                    <a href="https://github.com/settings/tokens/new" target="_blank" class="text-indigo-600 dark:text-indigo-400 hover:underline">
                      github.com/settings/tokens
                    </a>
                    with <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs">repo</code> scope.
                  </p>
                </div>
              </div>
            </div>
          </div>

          <!-- Slack Integration Section (SLACK-001/002) -->
          <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
            <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <div class="flex items-center justify-between">
                <div>
                  <h2 class="text-lg font-medium text-gray-900 dark:text-white">Slack Integration</h2>
                  <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                    Connect your Slack workspace to route messages to Trinity agents.
                  </p>
                </div>
                <span class="flex items-center gap-2">
                  <span
                    class="inline-block w-2.5 h-2.5 rounded-full"
                    :class="slackTransportStatus.connected ? 'bg-green-500' : 'bg-red-500'"
                  ></span>
                  <span class="text-sm" :class="slackTransportStatus.connected ? 'text-green-700 dark:text-green-400' : 'text-gray-500 dark:text-gray-400'">
                    {{ slackTransportStatus.connected ? (slackTransportStatus.transport_mode === 'socket' ? 'Socket Mode' : 'Webhook') : 'Disconnected' }}
                  </span>
                </span>
              </div>
            </div>

            <div class="px-6 py-4">
              <div class="space-y-4">
                <!-- OAuth Credentials -->
                <h3 class="text-sm font-medium text-gray-900 dark:text-white">OAuth Credentials</h3>

                <!-- Slack Client ID -->
                <div>
                  <label for="slack-client-id" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                    Client ID
                  </label>
                  <div class="mt-1">
                    <input
                      type="text"
                      id="slack-client-id"
                      v-model="slackClientId"
                      :placeholder="slackSettings.client_id?.configured ? slackSettings.client_id.masked : 'Enter Slack Client ID'"
                      :disabled="savingSlackSettings"
                      class="block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 dark:bg-gray-700 dark:text-white font-mono text-sm"
                    />
                  </div>
                  <div v-if="slackSettings.client_id?.configured" class="mt-1 text-xs text-green-600 dark:text-green-400">
                    ✓ Configured ({{ slackSettings.client_id.source === 'settings' ? 'from settings' : 'from environment' }})
                  </div>
                </div>

                <!-- Slack Client Secret -->
                <div>
                  <label for="slack-client-secret" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                    Client Secret
                  </label>
                  <div class="mt-1 relative">
                    <input
                      :type="showSlackClientSecret ? 'text' : 'password'"
                      id="slack-client-secret"
                      v-model="slackClientSecret"
                      :placeholder="slackSettings.client_secret?.configured ? slackSettings.client_secret.masked : 'Enter Slack Client Secret'"
                      :disabled="savingSlackSettings"
                      class="block w-full px-3 py-2 pr-10 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 dark:bg-gray-700 dark:text-white font-mono text-sm"
                    />
                    <button
                      type="button"
                      @click="showSlackClientSecret = !showSlackClientSecret"
                      class="absolute inset-y-0 right-0 pr-3 flex items-center"
                    >
                      <svg v-if="showSlackClientSecret" class="h-5 w-5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
                      </svg>
                      <svg v-else class="h-5 w-5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                      </svg>
                    </button>
                  </div>
                  <div v-if="slackSettings.client_secret?.configured" class="mt-1 text-xs text-green-600 dark:text-green-400">
                    ✓ Configured ({{ slackSettings.client_secret.source === 'settings' ? 'from settings' : 'from environment' }})
                  </div>
                </div>

                <!-- Slack Signing Secret -->
                <div>
                  <label for="slack-signing-secret" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                    Signing Secret
                  </label>
                  <div class="mt-1 relative">
                    <input
                      :type="showSlackSigningSecret ? 'text' : 'password'"
                      id="slack-signing-secret"
                      v-model="slackSigningSecret"
                      :placeholder="slackSettings.signing_secret?.configured ? slackSettings.signing_secret.masked : 'Enter Slack Signing Secret'"
                      :disabled="savingSlackSettings"
                      class="block w-full px-3 py-2 pr-10 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 dark:bg-gray-700 dark:text-white font-mono text-sm"
                    />
                    <button
                      type="button"
                      @click="showSlackSigningSecret = !showSlackSigningSecret"
                      class="absolute inset-y-0 right-0 pr-3 flex items-center"
                    >
                      <svg v-if="showSlackSigningSecret" class="h-5 w-5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
                      </svg>
                      <svg v-else class="h-5 w-5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                      </svg>
                    </button>
                  </div>
                  <div v-if="slackSettings.signing_secret?.configured" class="mt-1 text-xs text-green-600 dark:text-green-400">
                    ✓ Configured ({{ slackSettings.signing_secret.source === 'settings' ? 'from settings' : 'from environment' }})
                  </div>
                </div>

                <!-- Save Credentials Button -->
                <div class="flex items-center gap-3">
                  <button
                    @click="saveSlackSettings"
                    :disabled="(!slackClientId && !slackClientSecret && !slackSigningSecret) || savingSlackSettings"
                    class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <svg v-if="savingSlackSettings" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    Save Credentials
                  </button>
                  <span v-if="slackSaveSuccess" class="text-sm text-green-600 dark:text-green-400">
                    ✓ Saved
                  </span>
                </div>

                <!-- Divider -->
                <div class="border-t border-gray-200 dark:border-gray-700 pt-4">
                  <h3 class="text-sm font-medium text-gray-900 dark:text-white mb-3">Transport Connection</h3>
                </div>

                <!-- Transport Mode (Socket Mode only for now) -->
                <div>
                  <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Transport Mode</label>
                  <p class="text-sm text-gray-600 dark:text-gray-400">Socket Mode <span class="text-xs text-gray-400">(outbound WebSocket, no public URL needed)</span></p>
                </div>

                <!-- App Token (for Socket Mode) -->
                <div v-if="slackTransportMode === 'socket'">
                  <label for="slack-app-token" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                    App Token
                  </label>
                  <div class="mt-1 relative">
                    <input
                      :type="showSlackAppToken ? 'text' : 'password'"
                      id="slack-app-token"
                      v-model="slackAppToken"
                      :placeholder="slackTransportStatus.app_token_configured ? slackTransportStatus.app_token_masked : 'xapp-1-...'"
                      :disabled="connectingSlack"
                      class="block w-full px-3 py-2 pr-10 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 dark:bg-gray-700 dark:text-white font-mono text-sm"
                    />
                    <button
                      type="button"
                      @click="showSlackAppToken = !showSlackAppToken"
                      class="absolute inset-y-0 right-0 pr-3 flex items-center"
                    >
                      <svg v-if="showSlackAppToken" class="h-5 w-5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
                      </svg>
                      <svg v-else class="h-5 w-5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                      </svg>
                    </button>
                  </div>
                  <div v-if="slackTransportStatus.app_token_configured" class="mt-1 text-xs text-green-600 dark:text-green-400">
                    ✓ App token configured
                  </div>
                  <p class="mt-1 text-xs text-gray-400">
                    From Slack App &gt; Basic Information &gt; App-Level Tokens (scope: <code class="px-0.5 bg-gray-200 dark:bg-gray-600 rounded">connections:write</code>)
                  </p>
                </div>

                <!-- Action Buttons -->
                <div class="flex items-center gap-3 flex-wrap">
                  <!-- Connect Socket Mode -->
                  <button
                    v-if="!slackTransportStatus.connected"
                    @click="connectSlackTransport"
                    :disabled="connectingSlack || (!slackTransportStatus.app_token_configured && !slackAppToken)"
                    class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-green-600 hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <svg v-if="connectingSlack" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    {{ connectingSlack ? 'Connecting...' : 'Connect' }}
                  </button>
                  <span v-if="slackTransportStatus.connected" class="text-sm text-green-600 dark:text-green-400">
                    ✓ Socket Mode active
                  </span>

                  <!-- Install to Workspace (OAuth) -->
                  <button
                    @click="installSlackWorkspace"
                    :disabled="installingSlackWorkspace || !slackSettings.configured"
                    class="inline-flex items-center px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm text-sm font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <svg v-if="installingSlackWorkspace" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    {{ slackTransportStatus.workspaces.length > 0 ? 'Reinstall to Workspace' : 'Install to Workspace' }}
                  </button>
                  <span v-if="slackInstallSuccess" class="text-sm text-green-600 dark:text-green-400">
                    ✓ Workspace installed
                  </span>
                </div>

                <!-- Connected Workspaces -->
                <div v-if="slackTransportStatus.workspaces.length > 0">
                  <p class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">Connected Workspaces</p>
                  <div class="space-y-2">
                    <div
                      v-for="ws in slackTransportStatus.workspaces"
                      :key="ws.team_id"
                      class="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded-lg"
                    >
                      <div>
                        <span class="text-sm font-medium text-gray-900 dark:text-white">{{ ws.team_name }}</span>
                        <span class="ml-2 text-xs text-gray-500 dark:text-gray-400">{{ ws.agent_count }} agent{{ ws.agent_count !== 1 ? 's' : '' }}</span>
                      </div>
                      <div class="flex gap-1 flex-wrap">
                        <span
                          v-for="agent in ws.agents"
                          :key="agent"
                          class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-indigo-100 text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-300"
                        >
                          {{ agent }}
                        </span>
                      </div>
                    </div>
                  </div>
                </div>

                <!-- Setup Instructions -->
                <details class="mt-2">
                  <summary class="text-sm font-medium text-gray-700 dark:text-gray-300 cursor-pointer hover:text-indigo-600 dark:hover:text-indigo-400">
                    Setup Instructions
                  </summary>
                  <div class="mt-3 p-4 bg-gray-50 dark:bg-gray-700 rounded-lg text-sm text-gray-600 dark:text-gray-300 space-y-2">
                    <p><strong>1.</strong> Create a Slack App at <a href="https://api.slack.com/apps" target="_blank" class="text-indigo-600 dark:text-indigo-400 hover:underline">api.slack.com/apps</a></p>
                    <p><strong>2.</strong> Copy <strong>Client ID</strong>, <strong>Client Secret</strong>, and <strong>Signing Secret</strong> from Basic Information and save above</p>
                    <p><strong>3.</strong> Add Bot Token Scopes: <code class="px-1 py-0.5 bg-gray-200 dark:bg-gray-600 rounded text-xs">im:history</code>, <code class="px-1 py-0.5 bg-gray-200 dark:bg-gray-600 rounded text-xs">im:read</code>, <code class="px-1 py-0.5 bg-gray-200 dark:bg-gray-600 rounded text-xs">im:write</code>, <code class="px-1 py-0.5 bg-gray-200 dark:bg-gray-600 rounded text-xs">chat:write</code>, <code class="px-1 py-0.5 bg-gray-200 dark:bg-gray-600 rounded text-xs">chat:write.customize</code>, <code class="px-1 py-0.5 bg-gray-200 dark:bg-gray-600 rounded text-xs">users:read.email</code>, <code class="px-1 py-0.5 bg-gray-200 dark:bg-gray-600 rounded text-xs">app_mentions:read</code>, <code class="px-1 py-0.5 bg-gray-200 dark:bg-gray-600 rounded text-xs">channels:read</code>, <code class="px-1 py-0.5 bg-gray-200 dark:bg-gray-600 rounded text-xs">channels:manage</code>, <code class="px-1 py-0.5 bg-gray-200 dark:bg-gray-600 rounded text-xs">reactions:write</code></p>
                    <p><strong>4.</strong> Enable <strong>Socket Mode</strong> and create an App-Level Token with <code class="px-1 py-0.5 bg-gray-200 dark:bg-gray-600 rounded text-xs">connections:write</code> scope. Paste it above as App Token.</p>
                    <p><strong>5.</strong> Subscribe to events: <code class="px-1 py-0.5 bg-gray-200 dark:bg-gray-600 rounded text-xs">message.im</code>, <code class="px-1 py-0.5 bg-gray-200 dark:bg-gray-600 rounded text-xs">app_mention</code></p>
                    <p><strong>6.</strong> Add OAuth Redirect URL: <code class="px-1 py-0.5 bg-gray-200 dark:bg-gray-600 rounded text-xs break-all">https://YOUR_DOMAIN/api/public/slack/oauth/callback</code></p>
                    <p><strong>7.</strong> Click <strong>Connect</strong> above to start receiving messages</p>
                    <p><strong>8.</strong> Install the app to your workspace, then bind agents to channels from each agent's Sharing tab</p>
                  </div>
                </details>
              </div>
            </div>
          </div>

          <!-- Claude Subscriptions Section (SUB-001) -->
          <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
            <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <h2 class="text-lg font-medium text-gray-900 dark:text-white">Claude Subscriptions</h2>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                Manage Claude Max/Pro subscription credentials. Register once, assign to multiple agents.
              </p>
            </div>

            <div class="px-6 py-4">
              <div class="space-y-4">
                <!-- Encryption Not Configured Warning -->
                <div v-if="!encryptionConfigured" class="bg-yellow-50 dark:bg-yellow-900/30 border border-yellow-200 dark:border-yellow-800 rounded-lg p-4">
                  <div class="flex">
                    <div class="flex-shrink-0">
                      <svg class="h-5 w-5 text-yellow-400" fill="currentColor" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd" />
                      </svg>
                    </div>
                    <div class="ml-3">
                      <h3 class="text-sm font-medium text-yellow-800 dark:text-yellow-300">Encryption not configured</h3>
                      <p class="mt-1 text-sm text-yellow-700 dark:text-yellow-400">
                        Subscription storage requires <code class="px-1 py-0.5 bg-yellow-100 dark:bg-yellow-900 rounded text-xs">CREDENTIAL_ENCRYPTION_KEY</code> in your <code class="px-1 py-0.5 bg-yellow-100 dark:bg-yellow-900 rounded text-xs">.env</code> file.
                        Generate with: <code class="px-1 py-0.5 bg-yellow-100 dark:bg-yellow-900 rounded text-xs">openssl rand -hex 32</code> and restart the backend.
                      </p>
                    </div>
                  </div>
                </div>

                <!-- Add Subscription Form -->
                <div class="bg-gray-50 dark:bg-gray-700/50 rounded-lg p-4">
                  <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">Add Subscription</h3>

                  <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <!-- Name Input -->
                    <div>
                      <label for="subscription-name" class="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                        Name
                      </label>
                      <input
                        type="text"
                        id="subscription-name"
                        v-model="newSubscription.name"
                        placeholder="e.g., eugene-max"
                        class="block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 dark:bg-gray-700 dark:text-white text-sm"
                        :disabled="addingSubscription"
                      />
                    </div>

                    <!-- Type Input -->
                    <div>
                      <label for="subscription-type" class="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                        Type
                      </label>
                      <select
                        id="subscription-type"
                        v-model="newSubscription.type"
                        class="block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 dark:bg-gray-700 dark:text-white text-sm"
                        :disabled="addingSubscription"
                      >
                        <option value="max">Claude Max</option>
                        <option value="pro">Claude Pro</option>
                        <option value="">Unknown</option>
                      </select>
                    </div>
                  </div>

                  <!-- Token Input (SUB-002) -->
                  <div class="mt-4">
                    <label class="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                      Token (from <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded">claude setup-token</code>)
                    </label>
                    <input
                      type="password"
                      v-model="newSubscription.token"
                      placeholder="sk-ant-oat01-..."
                      :disabled="addingSubscription"
                      class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white text-sm focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
                      :class="{ 'border-red-400 dark:border-red-500': newSubscription.token && !newSubscription.token.startsWith('sk-ant-oat01-') }"
                    />
                    <p v-if="newSubscription.token && !newSubscription.token.startsWith('sk-ant-oat01-')" class="mt-1 text-xs text-red-500">
                      Token must start with sk-ant-oat01-
                    </p>
                    <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">
                      Run <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded">claude setup-token</code> locally to generate a long-lived token (~1 year)
                    </p>
                  </div>

                  <!-- Add Button -->
                  <div class="mt-4 flex justify-end">
                    <button
                      @click="clearNewSubscription"
                      v-if="newSubscription.name || newSubscription.token"
                      class="mr-3 inline-flex items-center px-4 py-2 border border-gray-300 dark:border-gray-600 shadow-sm text-sm font-medium rounded-md text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600"
                    >
                      Clear
                    </button>
                    <button
                      @click="addSubscription"
                      :disabled="!newSubscription.name || !newSubscription.token.startsWith('sk-ant-oat01-') || addingSubscription || !encryptionConfigured"
                      class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <svg v-if="addingSubscription" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                      </svg>
                      Register Subscription
                    </button>
                  </div>
                </div>

                <!-- Subscriptions Table -->
                <div class="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
                  <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                    <thead class="bg-gray-50 dark:bg-gray-700">
                      <tr>
                        <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                          Name
                        </th>
                        <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                          Type
                        </th>
                        <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                          Agents
                        </th>
                        <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                          Created
                        </th>
                        <th scope="col" class="relative px-6 py-3">
                          <span class="sr-only">Actions</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody class="bg-white dark:bg-gray-800 divide-y divide-gray-200 dark:divide-gray-700">
                      <tr v-if="loadingSubscriptions">
                        <td colspan="5" class="px-6 py-4 text-center text-sm text-gray-500 dark:text-gray-400">
                          <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-indigo-600 mx-auto"></div>
                        </td>
                      </tr>
                      <tr v-else-if="subscriptions.length === 0">
                        <td colspan="5" class="px-6 py-4 text-center text-sm text-gray-500 dark:text-gray-400">
                          No subscriptions registered. Add one above using your Claude credentials.
                        </td>
                      </tr>
                      <template v-else v-for="sub in subscriptions" :key="sub.id">
                        <tr class="hover:bg-gray-50 dark:hover:bg-gray-700 cursor-pointer" @click="toggleSubscriptionDetails(sub.id)">
                          <td class="px-6 py-4 whitespace-nowrap">
                            <div class="flex items-center">
                              <svg class="h-4 w-4 text-gray-400 mr-2 transform transition-transform" :class="{ 'rotate-90': expandedSubscriptions.has(sub.id) }" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                              </svg>
                              <span class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ sub.name }}</span>
                            </div>
                          </td>
                          <td class="px-6 py-4 whitespace-nowrap">
                            <span v-if="sub.subscription_type" class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium"
                                  :class="sub.subscription_type === 'max' ? 'bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200' : 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200'">
                              {{ sub.subscription_type === 'max' ? 'Max' : sub.subscription_type === 'pro' ? 'Pro' : sub.subscription_type }}
                            </span>
                            <span v-else class="text-sm text-gray-500 dark:text-gray-400">—</span>
                          </td>
                          <td class="px-6 py-4 whitespace-nowrap">
                            <span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200">
                              {{ sub.agent_count || 0 }} agent{{ (sub.agent_count || 0) === 1 ? '' : 's' }}
                            </span>
                          </td>
                          <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                            {{ formatDate(sub.created_at) }}
                          </td>
                          <td class="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                            <button
                              @click.stop="deleteSubscription(sub)"
                              :disabled="deletingSubscription === sub.id"
                              class="text-red-600 hover:text-red-900 dark:text-red-400 dark:hover:text-red-300 disabled:opacity-50"
                            >
                              {{ deletingSubscription === sub.id ? 'Deleting...' : 'Delete' }}
                            </button>
                          </td>
                        </tr>
                        <!-- Expanded Details Row -->
                        <tr v-if="expandedSubscriptions.has(sub.id)" class="bg-gray-50 dark:bg-gray-700/50">
                          <td colspan="5" class="px-6 py-4">
                            <div class="text-sm">
                              <div class="mb-2 text-gray-600 dark:text-gray-400">
                                <strong>Owner:</strong> {{ sub.owner_email || 'Unknown' }}
                              </div>
                              <div v-if="sub.rate_limit_tier" class="mb-2 text-gray-600 dark:text-gray-400">
                                <strong>Rate Limit Tier:</strong> {{ sub.rate_limit_tier }}
                              </div>
                              <div>
                                <strong class="text-gray-600 dark:text-gray-400">Assigned Agents:</strong>
                                <div v-if="sub.agents && sub.agents.length > 0" class="mt-2 flex flex-wrap gap-2">
                                  <span v-for="agent in sub.agents" :key="agent"
                                        class="inline-flex items-center px-2.5 py-1 rounded-md text-xs font-medium bg-indigo-100 text-indigo-800 dark:bg-indigo-900 dark:text-indigo-200">
                                    <svg class="mr-1 h-3 w-3" fill="currentColor" viewBox="0 0 20 20">
                                      <path d="M9 2a1 1 0 000 2h2a1 1 0 100-2H9z" />
                                      <path fill-rule="evenodd" d="M4 5a2 2 0 012-2 3 3 0 003 3h2a3 3 0 003-3 2 2 0 012 2v11a2 2 0 01-2 2H6a2 2 0 01-2-2V5zm3 4a1 1 0 000 2h.01a1 1 0 100-2H7zm3 0a1 1 0 000 2h3a1 1 0 100-2h-3zm-3 4a1 1 0 100 2h.01a1 1 0 100-2H7zm3 0a1 1 0 100 2h3a1 1 0 100-2h-3z" clip-rule="evenodd" />
                                    </svg>
                                    {{ agent }}
                                    <button
                                      @click.stop="unassignAgentFromSubscription(agent)"
                                      :disabled="unassigningAgent === agent"
                                      class="ml-1.5 inline-flex items-center justify-center h-4 w-4 rounded-full hover:bg-indigo-200 dark:hover:bg-indigo-800 text-indigo-600 dark:text-indigo-300 disabled:opacity-50"
                                      title="Remove agent from subscription"
                                    >
                                      <svg v-if="unassigningAgent === agent" class="animate-spin h-3 w-3" fill="none" viewBox="0 0 24 24">
                                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                                      </svg>
                                      <svg v-else class="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                                      </svg>
                                    </button>
                                  </span>
                                </div>
                                <p v-else class="mt-1 text-gray-500 dark:text-gray-400 italic">
                                  No agents assigned yet.
                                </p>
                                <!-- Assign Agent Dropdown -->
                                <div class="mt-3 flex items-center gap-2">
                                  <select
                                    v-model="selectedAgentToAssign[sub.id]"
                                    :disabled="assigningAgent || loadingAgents"
                                    class="flex-1 max-w-xs px-3 py-1.5 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm text-sm focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 dark:bg-gray-700 dark:text-white"
                                    @click.stop
                                  >
                                    <option value="" disabled selected>{{ loadingAgents ? 'Loading agents...' : 'Select agent...' }}</option>
                                    <option
                                      v-for="agent in getAvailableAgents(sub.id)"
                                      :key="agent.name"
                                      :value="agent.name"
                                    >
                                      {{ agent.name }}{{ agentSubscriptionMap[agent.name] ? ` (on ${agentSubscriptionMap[agent.name]})` : '' }}
                                    </option>
                                  </select>
                                  <button
                                    @click.stop="assignAgentToSubscription(sub.name, selectedAgentToAssign[sub.id])"
                                    :disabled="!selectedAgentToAssign[sub.id] || assigningAgent"
                                    class="inline-flex items-center px-3 py-1.5 border border-transparent rounded-md shadow-sm text-xs font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
                                  >
                                    <svg v-if="assigningAgent" class="animate-spin -ml-0.5 mr-1.5 h-3 w-3" fill="none" viewBox="0 0 24 24">
                                      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                                      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                                    </svg>
                                    Assign
                                  </button>
                                </div>
                              </div>
                            </div>
                          </td>
                        </tr>
                      </template>
                    </tbody>
                  </table>
                </div>

                <p class="text-xs text-gray-500 dark:text-gray-400">
                  Expand a subscription row to assign or remove agents. Running agents will restart automatically.
                </p>

                <!-- Auto-Switch Toggle (SUB-003) -->
                <div class="mt-6 pt-4 border-t border-gray-200 dark:border-gray-700">
                  <div class="flex items-center justify-between">
                    <div class="flex-1 mr-4">
                      <label for="auto-switch-toggle" class="text-sm font-medium text-gray-700 dark:text-gray-300">
                        Automatically switch subscriptions when usage limits are reached
                      </label>
                      <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                        When enabled, agents will automatically try a different subscription after 2 consecutive rate-limit errors. Requires at least 2 registered subscriptions.
                      </p>
                    </div>
                    <button
                      id="auto-switch-toggle"
                      type="button"
                      :class="[
                        autoSwitchEnabled ? 'bg-indigo-600' : 'bg-gray-200 dark:bg-gray-600',
                        'relative inline-flex h-6 w-11 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:ring-offset-2'
                      ]"
                      :disabled="savingAutoSwitch"
                      @click="toggleAutoSwitch"
                    >
                      <span
                        :class="[
                          autoSwitchEnabled ? 'translate-x-5' : 'translate-x-0',
                          'pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out'
                        ]"
                      />
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- Trinity Prompt Section -->
          <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
            <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <h2 class="text-lg font-medium text-gray-900 dark:text-white">Trinity Prompt</h2>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                Custom instructions that are injected into all agents' CLAUDE.md at startup.
                Changes apply to newly started or restarted agents.
              </p>
            </div>

            <div class="px-6 py-4">
              <div class="space-y-4">
                <div>
                  <label for="trinity-prompt" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                    Custom Instructions
                  </label>
                  <div class="mt-1">
                    <textarea
                      id="trinity-prompt"
                      v-model="trinityPrompt"
                      rows="15"
                      class="shadow-sm focus:ring-indigo-500 focus:border-indigo-500 block w-full sm:text-sm border border-gray-300 dark:border-gray-600 rounded-md font-mono bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500"
                      placeholder="Enter custom instructions for all agents...

Example:
- Always use TypeScript for new files
- Follow the project's coding conventions
- Check for security vulnerabilities before committing"
                      :disabled="saving"
                    ></textarea>
                  </div>
                  <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">
                    This content will appear under a "## Custom Instructions" section in each agent's CLAUDE.md.
                    Supports Markdown formatting.
                  </p>
                </div>

                <!-- Character Count -->
                <div class="flex justify-between text-sm text-gray-500 dark:text-gray-400">
                  <span>{{ trinityPrompt.length }} characters</span>
                  <span v-if="hasChanges" class="text-amber-600 dark:text-amber-400">Unsaved changes</span>
                </div>

                <!-- Action Buttons -->
                <div class="flex justify-end space-x-3">
                  <button
                    @click="clearPrompt"
                    :disabled="saving || !trinityPrompt"
                    class="inline-flex items-center px-4 py-2 border border-gray-300 dark:border-gray-600 shadow-sm text-sm font-medium rounded-md text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    Clear
                  </button>
                  <button
                    @click="savePrompt"
                    :disabled="saving || !hasChanges"
                    class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <svg v-if="saving" class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
                      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    {{ saving ? 'Saving...' : 'Save Changes' }}
                  </button>
                </div>
              </div>
            </div>
          </div>

          <!-- Email Whitelist Section (Phase 12.4) -->
          <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
            <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <h2 class="text-lg font-medium text-gray-900 dark:text-white">Email Whitelist</h2>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                Manage whitelisted emails for email-based authentication.
                Only whitelisted users can login with email verification codes.
              </p>
            </div>

            <div class="px-6 py-4">
              <div class="space-y-4">
                <!-- Add Email Form -->
                <div class="flex gap-2">
                  <input
                    v-model="newEmail"
                    type="email"
                    placeholder="user@example.com"
                    class="flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 dark:bg-gray-700 dark:text-white text-sm"
                    :disabled="addingEmail"
                    @keyup.enter="addEmailToWhitelist"
                  />
                  <button
                    @click="addEmailToWhitelist"
                    :disabled="!newEmail || addingEmail"
                    class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <svg v-if="addingEmail" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    Add Email
                  </button>
                </div>

                <!-- Whitelist Table -->
                <div class="mt-4 border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
                  <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                    <thead class="bg-gray-50 dark:bg-gray-700">
                      <tr>
                        <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                          Email
                        </th>
                        <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                          Source
                        </th>
                        <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                          Added
                        </th>
                        <th scope="col" class="relative px-6 py-3">
                          <span class="sr-only">Actions</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody class="bg-white dark:bg-gray-800 divide-y divide-gray-200 dark:divide-gray-700">
                      <tr v-if="loadingWhitelist">
                        <td colspan="4" class="px-6 py-4 text-center text-sm text-gray-500 dark:text-gray-400">
                          <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-indigo-600 mx-auto"></div>
                        </td>
                      </tr>
                      <tr v-else-if="emailWhitelist.length === 0">
                        <td colspan="4" class="px-6 py-4 text-center text-sm text-gray-500 dark:text-gray-400">
                          No whitelisted emails. Add one above to get started.
                        </td>
                      </tr>
                      <tr v-else v-for="entry in emailWhitelist" :key="entry.id" class="hover:bg-gray-50 dark:hover:bg-gray-700">
                        <td class="px-6 py-4 whitespace-nowrap text-sm font-medium text-gray-900 dark:text-gray-100">
                          {{ entry.email }}
                        </td>
                        <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                          <span v-if="entry.source === 'agent_sharing'" class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200">
                            🤝 Auto (Agent Sharing)
                          </span>
                          <span v-else class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200">
                            ✋ Manual
                          </span>
                        </td>
                        <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                          {{ formatDate(entry.added_at) }}
                        </td>
                        <td class="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                          <button
                            @click="removeEmailFromWhitelist(entry.email)"
                            :disabled="removingEmail === entry.email"
                            class="text-red-600 hover:text-red-900 dark:text-red-400 dark:hover:text-red-300 disabled:opacity-50"
                          >
                            {{ removingEmail === entry.email ? 'Removing...' : 'Remove' }}
                          </button>
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>

                <p class="text-xs text-gray-500 dark:text-gray-400 mt-2">
                  💡 Tip: When you share an agent with someone by email, they're automatically added to this whitelist.
                </p>
              </div>
            </div>
          </div>

          <!-- User Management Section (ROLE-001) -->
          <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
            <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <h2 class="text-lg font-medium text-gray-900 dark:text-white">User Management</h2>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                Manage user roles. Roles control what actions each user can perform on the platform.
              </p>
            </div>

            <div class="px-6 py-4">
              <!-- Role legend -->
              <div class="flex flex-wrap gap-2 mb-4 text-xs text-gray-500 dark:text-gray-400">
                <span class="font-medium">Roles:</span>
                <span class="inline-flex items-center px-2 py-0.5 rounded-full bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200">admin — full control</span>
                <span class="inline-flex items-center px-2 py-0.5 rounded-full bg-indigo-100 text-indigo-800 dark:bg-indigo-900 dark:text-indigo-200">creator — create &amp; manage agents</span>
                <span class="inline-flex items-center px-2 py-0.5 rounded-full bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200">operator — run existing agents</span>
                <span class="inline-flex items-center px-2 py-0.5 rounded-full bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200">user — public links only</span>
              </div>

              <!-- Users Table -->
              <div class="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
                <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                  <thead class="bg-gray-50 dark:bg-gray-700">
                    <tr>
                      <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">User</th>
                      <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">Email</th>
                      <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">Role</th>
                      <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">Last Login</th>
                    </tr>
                  </thead>
                  <tbody class="bg-white dark:bg-gray-800 divide-y divide-gray-200 dark:divide-gray-700">
                    <tr v-if="loadingUsers">
                      <td colspan="4" class="px-6 py-4 text-center text-sm text-gray-500 dark:text-gray-400">
                        <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-indigo-600 mx-auto"></div>
                      </td>
                    </tr>
                    <tr v-else-if="usersList.length === 0">
                      <td colspan="4" class="px-6 py-4 text-center text-sm text-gray-500 dark:text-gray-400">No users found.</td>
                    </tr>
                    <tr v-else v-for="u in usersList" :key="u.username" class="hover:bg-gray-50 dark:hover:bg-gray-700">
                      <td class="px-6 py-4 whitespace-nowrap text-sm font-medium text-gray-900 dark:text-gray-100">
                        {{ u.name || u.username }}
                      </td>
                      <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                        {{ u.email || u.username }}
                      </td>
                      <td class="px-6 py-4 whitespace-nowrap text-sm">
                        <select
                          v-if="u.username !== currentUsername"
                          :value="u.role"
                          @change="updateUserRole(u.username, $event.target.value)"
                          class="text-sm border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500"
                        >
                          <option value="admin">admin</option>
                          <option value="creator">creator</option>
                          <option value="operator">operator</option>
                          <option value="user">user</option>
                        </select>
                        <span v-else class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200">
                          {{ u.role }} (you)
                        </span>
                      </td>
                      <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                        {{ u.last_login ? formatDate(u.last_login) : 'Never' }}
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          <!-- MCP Server URL Section (#76) -->
          <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
            <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <h2 class="text-lg font-medium text-gray-900 dark:text-white">MCP Server URL</h2>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                Configure the external MCP server URL shown on the API Keys page. Leave empty to auto-detect from hostname.
              </p>
            </div>
            <div class="px-6 py-4">
              <div class="flex items-center gap-2 mb-4">
                <span
                  :class="[
                    'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium',
                    mcpUrlConfig.url
                      ? 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-300'
                      : 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300'
                  ]"
                >
                  {{ mcpUrlConfig.url ? 'Custom' : 'Auto-detect' }}
                </span>
                <span v-if="mcpUrlConfig.url" class="text-sm text-gray-500 dark:text-gray-400 truncate">
                  {{ mcpUrlConfig.url }}
                </span>
                <span v-else class="text-sm text-gray-500 dark:text-gray-400 truncate">
                  {{ mcpUrlConfig.default_url || 'Loading...' }}
                </span>
              </div>

              <div class="flex gap-3">
                <input
                  v-model="mcpUrlInput"
                  type="url"
                  :placeholder="mcpUrlConfig.default_url || 'https://your-domain.com/mcp'"
                  class="flex-1 block rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
                />
                <button
                  @click="saveMcpUrl"
                  :disabled="!mcpUrlInput || savingMcpUrl"
                  class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {{ savingMcpUrl ? 'Saving...' : 'Save' }}
                </button>
                <button
                  v-if="mcpUrlConfig.url"
                  @click="resetMcpUrl"
                  :disabled="savingMcpUrl"
                  class="inline-flex items-center px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm text-sm font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50"
                >
                  Reset to Default
                </button>
              </div>

              <p v-if="mcpUrlError" class="mt-2 text-sm text-red-600 dark:text-red-400">
                {{ mcpUrlError }}
              </p>
              <p v-if="mcpUrlSuccess" class="mt-2 text-sm text-green-600 dark:text-green-400">
                {{ mcpUrlSuccess }}
              </p>
            </div>
          </div>

          <!-- GitHub Templates Section (TMPL-001) -->
          <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
            <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <h2 class="text-lg font-medium text-gray-900 dark:text-white">GitHub Templates</h2>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                Configure which GitHub repositories appear as agent templates.
                <span v-if="githubTemplatesSource === 'defaults'" class="inline-flex items-center ml-2 px-2 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300">
                  Using defaults
                </span>
                <span v-else class="inline-flex items-center ml-2 px-2 py-0.5 rounded-full text-xs font-medium bg-indigo-100 text-indigo-700 dark:bg-indigo-900 dark:text-indigo-300">
                  Custom config
                </span>
              </p>
            </div>

            <div class="px-6 py-4">
              <div class="space-y-4">
                <!-- Add Template Form -->
                <div class="flex gap-2">
                  <input
                    v-model="newTemplateRepo"
                    type="text"
                    placeholder="owner/repo"
                    class="flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 dark:bg-gray-700 dark:text-white font-mono text-sm"
                    :disabled="savingGithubTemplates"
                    @keyup.enter="addGithubTemplate"
                  />
                  <input
                    v-model="newTemplateName"
                    type="text"
                    placeholder="Display name (optional)"
                    class="w-48 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 dark:bg-gray-700 dark:text-white text-sm"
                    :disabled="savingGithubTemplates"
                    @keyup.enter="addGithubTemplate"
                  />
                  <button
                    @click="addGithubTemplate"
                    :disabled="!newTemplateRepo || savingGithubTemplates"
                    class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    Add
                  </button>
                </div>
                <p v-if="templateValidationError" class="text-sm text-red-600 dark:text-red-400">
                  {{ templateValidationError }}
                </p>

                <!-- Templates Table -->
                <div class="mt-4 border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
                  <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                    <thead class="bg-gray-50 dark:bg-gray-700">
                      <tr>
                        <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                          Repository
                        </th>
                        <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                          Display Name
                        </th>
                        <th scope="col" class="relative px-6 py-3">
                          <span class="sr-only">Actions</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody class="bg-white dark:bg-gray-800 divide-y divide-gray-200 dark:divide-gray-700">
                      <tr v-if="loadingGithubTemplates">
                        <td colspan="3" class="px-6 py-4 text-center text-sm text-gray-500 dark:text-gray-400">
                          <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-indigo-600 mx-auto"></div>
                        </td>
                      </tr>
                      <tr v-else-if="githubTemplates.length === 0">
                        <td colspan="3" class="px-6 py-4 text-center text-sm text-gray-500 dark:text-gray-400">
                          No templates configured. Add a GitHub repo above or reset to defaults.
                        </td>
                      </tr>
                      <tr v-else v-for="(tmpl, index) in githubTemplates" :key="tmpl.github_repo" class="hover:bg-gray-50 dark:hover:bg-gray-700">
                        <td class="px-6 py-4 whitespace-nowrap text-sm font-mono text-gray-900 dark:text-gray-100">
                          {{ tmpl.github_repo }}
                        </td>
                        <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                          {{ tmpl.resolved_name || tmpl.display_name || '-' }}
                          <span v-if="tmpl.display_name" class="ml-1 text-xs text-indigo-500">(custom)</span>
                        </td>
                        <td class="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                          <button
                            @click="removeGithubTemplate(index)"
                            :disabled="savingGithubTemplates"
                            class="text-red-600 hover:text-red-900 dark:text-red-400 dark:hover:text-red-300 disabled:opacity-50"
                          >
                            Remove
                          </button>
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>

                <!-- Action Buttons -->
                <div class="flex justify-between items-center">
                  <button
                    @click="resetGithubTemplates"
                    :disabled="savingGithubTemplates || githubTemplatesSource === 'defaults'"
                    class="inline-flex items-center px-4 py-2 border border-gray-300 dark:border-gray-600 shadow-sm text-sm font-medium rounded-md text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    Reset to Defaults
                  </button>
                  <button
                    @click="saveGithubTemplates"
                    :disabled="savingGithubTemplates || !githubTemplatesDirty"
                    class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <svg v-if="savingGithubTemplates" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    Save Templates
                  </button>
                </div>
              </div>
            </div>
          </div>

          <!-- SSH Access Section -->
          <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
            <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <h2 class="text-lg font-medium text-gray-900 dark:text-white">SSH Access</h2>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                Allow generating ephemeral SSH credentials for direct terminal access to agent containers.
              </p>
            </div>

            <div class="px-6 py-4">
              <div class="flex items-center justify-between">
                <div>
                  <label for="ssh-access-toggle" class="text-sm font-medium text-gray-700 dark:text-gray-300">
                    Enable SSH Access
                  </label>
                  <p class="text-sm text-gray-500 dark:text-gray-400">
                    When enabled, the MCP tool <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs">get_agent_ssh_access</code> can generate temporary SSH credentials.
                  </p>
                </div>
                <button
                  id="ssh-access-toggle"
                  type="button"
                  :class="[
                    sshAccessEnabled ? 'bg-indigo-600' : 'bg-gray-200 dark:bg-gray-600',
                    'relative inline-flex h-6 w-11 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:ring-offset-2'
                  ]"
                  :disabled="savingSshAccess"
                  @click="toggleSshAccess"
                >
                  <span
                    :class="[
                      sshAccessEnabled ? 'translate-x-5' : 'translate-x-0',
                      'pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out'
                    ]"
                  />
                </button>
              </div>
            </div>
          </div>

          <!-- Agent Quotas Section (QUOTA-001) -->
          <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
            <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <h2 class="text-lg font-medium text-gray-900 dark:text-white">Agent Quotas</h2>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                Set the maximum number of agents each role can create. Set to 0 for unlimited.
              </p>
            </div>

            <div class="px-6 py-4 space-y-4">
              <!-- Admin role - always unlimited -->
              <div class="flex items-center justify-between">
                <div>
                  <label class="text-sm font-medium text-gray-700 dark:text-gray-300">Admin</label>
                  <p class="text-sm text-gray-500 dark:text-gray-400">Admins can always create unlimited agents</p>
                </div>
                <span class="text-sm font-medium text-green-600 dark:text-green-400">Unlimited</span>
              </div>

              <!-- Creator role -->
              <div class="flex items-center justify-between">
                <div>
                  <label for="quota-creator" class="text-sm font-medium text-gray-700 dark:text-gray-300">Creator</label>
                  <p class="text-sm text-gray-500 dark:text-gray-400">{{ agentQuotas.max_agents_creator?.description || 'Maximum agents a creator can own' }}</p>
                </div>
                <input
                  type="number"
                  id="quota-creator"
                  v-model="agentQuotaValues.max_agents_creator"
                  min="0"
                  class="w-20 rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm text-center"
                />
              </div>

              <!-- Operator role -->
              <div class="flex items-center justify-between">
                <div>
                  <label for="quota-operator" class="text-sm font-medium text-gray-700 dark:text-gray-300">Operator</label>
                  <p class="text-sm text-gray-500 dark:text-gray-400">{{ agentQuotas.max_agents_operator?.description || 'Maximum agents an operator can own' }}</p>
                </div>
                <input
                  type="number"
                  id="quota-operator"
                  v-model="agentQuotaValues.max_agents_operator"
                  min="0"
                  class="w-20 rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm text-center"
                />
              </div>

              <!-- User role -->
              <div class="flex items-center justify-between">
                <div>
                  <label for="quota-user" class="text-sm font-medium text-gray-700 dark:text-gray-300">User</label>
                  <p class="text-sm text-gray-500 dark:text-gray-400">{{ agentQuotas.max_agents_user?.description || 'Maximum agents a regular user can own' }}</p>
                </div>
                <input
                  type="number"
                  id="quota-user"
                  v-model="agentQuotaValues.max_agents_user"
                  min="0"
                  class="w-20 rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm text-center"
                />
              </div>

              <!-- Legacy setting warning -->
              <div v-if="agentQuotaLegacy" class="rounded-md bg-yellow-50 dark:bg-yellow-900/20 p-3">
                <p class="text-sm text-yellow-700 dark:text-yellow-400">
                  Legacy setting <code class="px-1 py-0.5 bg-yellow-100 dark:bg-yellow-900/40 rounded text-xs">max_agents_per_user={{ agentQuotaLegacy }}</code> is active and used as fallback. Save per-role quotas to override it.
                </p>
              </div>

              <!-- Save button -->
              <div class="flex justify-end">
                <button
                  type="button"
                  class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 disabled:opacity-50"
                  :disabled="savingQuotas"
                  @click="saveAgentQuotas"
                >
                  <svg v-if="savingQuotas" class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                  </svg>
                  Save Quotas
                </button>
              </div>
            </div>
          </div>

          <!-- Skills Library Section -->
          <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
            <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <h2 class="text-lg font-medium text-gray-900 dark:text-white">Skills Library</h2>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                Configure a GitHub repository containing reusable agent skills.
                Skills are stored in <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs">.claude/skills/&lt;name&gt;/SKILL.md</code>.
              </p>
            </div>

            <div class="px-6 py-4 space-y-4">
              <!-- Repository URL -->
              <div>
                <label for="skills-library-url" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                  Repository URL
                </label>
                <div class="mt-1">
                  <input
                    type="text"
                    id="skills-library-url"
                    v-model="skillsLibraryUrl"
                    placeholder="github.com/owner/skills-library"
                    class="block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 dark:bg-gray-700 dark:text-white text-sm"
                  />
                </div>
                <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  Use format: <code>github.com/owner/repo</code> or <code>https://github.com/owner/repo</code>
                </p>
              </div>

              <!-- Branch -->
              <div>
                <label for="skills-library-branch" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                  Branch
                </label>
                <div class="mt-1">
                  <input
                    type="text"
                    id="skills-library-branch"
                    v-model="skillsLibraryBranch"
                    placeholder="main"
                    class="block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 dark:bg-gray-700 dark:text-white text-sm"
                  />
                </div>
              </div>

              <!-- Status -->
              <div v-if="skillsLibraryStatus.cloned" class="bg-gray-50 dark:bg-gray-700/50 rounded-lg p-3">
                <div class="flex items-center justify-between text-sm">
                  <div class="flex items-center gap-4 text-gray-600 dark:text-gray-300">
                    <span>
                      <svg class="h-4 w-4 text-green-500 inline mr-1" fill="currentColor" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
                      </svg>
                      {{ skillsLibraryStatus.skill_count }} skills available
                    </span>
                    <span v-if="skillsLibraryStatus.commit_sha">
                      Commit: <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-600 rounded text-xs">{{ skillsLibraryStatus.commit_sha }}</code>
                    </span>
                  </div>
                  <span v-if="skillsLibraryStatus.last_sync" class="text-gray-500 dark:text-gray-400">
                    Last synced: {{ formatDate(skillsLibraryStatus.last_sync) }}
                  </span>
                </div>
              </div>

              <!-- Actions -->
              <div class="flex justify-end gap-3">
                <button
                  @click="syncSkillsLibrary"
                  :disabled="syncingSkillsLibrary || !skillsLibraryUrl"
                  class="inline-flex items-center px-4 py-2 border border-gray-300 dark:border-gray-600 shadow-sm text-sm font-medium rounded-md text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  <svg v-if="syncingSkillsLibrary" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                  </svg>
                  <svg v-else class="-ml-1 mr-2 h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                  </svg>
                  {{ syncingSkillsLibrary ? 'Syncing...' : 'Sync Library' }}
                </button>
                <button
                  @click="saveSkillsLibrarySettings"
                  :disabled="savingSkillsLibrary"
                  class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  <svg v-if="savingSkillsLibrary" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                  </svg>
                  {{ savingSkillsLibrary ? 'Saving...' : 'Save Settings' }}
                </button>
              </div>
            </div>
          </div>

          <!-- Default Avatars (AVATAR-003) -->
          <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
            <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <h2 class="text-lg font-medium text-gray-900 dark:text-white">Default Avatars</h2>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                Generate AI avatars for all agents that don't have a custom one yet.
                Uses the same Gemini image generation pipeline as custom avatars.
              </p>
            </div>
            <div class="px-6 py-4 space-y-4">
              <!-- Result message -->
              <div v-if="defaultAvatarResult" class="rounded-md p-3" :class="{
                'bg-green-50 dark:bg-green-900/30 text-green-700 dark:text-green-300': defaultAvatarResult.generated > 0 && defaultAvatarResult.failed === 0,
                'bg-yellow-50 dark:bg-yellow-900/30 text-yellow-700 dark:text-yellow-300': defaultAvatarResult.failed > 0,
                'bg-gray-50 dark:bg-gray-700 text-gray-600 dark:text-gray-300': defaultAvatarResult.generated === 0 && defaultAvatarResult.failed === 0
              }">
                <p class="text-sm font-medium">{{ defaultAvatarResult.message }}</p>
                <ul v-if="defaultAvatarResult.agents.length" class="mt-1 text-xs space-y-0.5">
                  <li v-for="name in defaultAvatarResult.agents" :key="name">Generated: {{ name }}</li>
                </ul>
                <ul v-if="defaultAvatarResult.errors.length" class="mt-1 text-xs space-y-0.5">
                  <li v-for="err in defaultAvatarResult.errors" :key="err.agent" class="text-red-600 dark:text-red-400">Failed: {{ err.agent }} - {{ err.error }}</li>
                </ul>
              </div>

              <!-- Generate button -->
              <button
                @click="generateDefaultAvatars"
                :disabled="generatingDefaultAvatars"
                class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <svg v-if="generatingDefaultAvatars" class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                  <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                </svg>
                {{ generatingDefaultAvatars ? 'Generating...' : 'Generate Default Avatars' }}
              </button>
            </div>
          </div>

          <!-- Info Box -->
          <div class="bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-800 rounded-lg p-4">
            <div class="flex">
              <div class="flex-shrink-0">
                <svg class="h-5 w-5 text-blue-400" fill="currentColor" viewBox="0 0 20 20">
                  <path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clip-rule="evenodd" />
                </svg>
              </div>
              <div class="ml-3">
                <h3 class="text-sm font-medium text-blue-800 dark:text-blue-300">How it works</h3>
                <div class="mt-2 text-sm text-blue-700 dark:text-blue-400">
                  <ul class="list-disc list-inside space-y-1">
                    <li>The Trinity Prompt is injected into each agent's CLAUDE.md when the agent starts</li>
                    <li>Existing agents need to be restarted to receive the updated prompt</li>
                    <li>The prompt appears as a "## Custom Instructions" section after the Trinity Planning System section</li>
                    <li>Use Markdown formatting for structured instructions</li>
                  </ul>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Error Display -->
        <div v-if="error" class="mt-4 bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 rounded-lg p-4">
          <div class="flex">
            <div class="flex-shrink-0">
              <svg class="h-5 w-5 text-red-400" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd" />
              </svg>
            </div>
            <div class="ml-3">
              <h3 class="text-sm font-medium text-red-800 dark:text-red-300">Error</h3>
              <p class="mt-1 text-sm text-red-700 dark:text-red-400">{{ error }}</p>
            </div>
          </div>
        </div>

        <!-- Success Message -->
        <div v-if="showSuccess" class="mt-4 bg-green-50 dark:bg-green-900/30 border border-green-200 dark:border-green-800 rounded-lg p-4">
          <div class="flex">
            <div class="flex-shrink-0">
              <svg class="h-5 w-5 text-green-400" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
              </svg>
            </div>
            <div class="ml-3">
              <p class="text-sm font-medium text-green-800 dark:text-green-300">Settings saved successfully!</p>
            </div>
          </div>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import { useSettingsStore } from '../stores/settings'
import NavBar from '../components/NavBar.vue'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const settingsStore = useSettingsStore()

const loading = ref(true)
const saving = ref(false)
const error = ref(null)
const showSuccess = ref(false)

// Email whitelist state (Phase 12.4)
const emailWhitelist = ref([])
const newEmail = ref('')
const addingEmail = ref(false)
const removingEmail = ref(null)
const loadingWhitelist = ref(false)

// User management state (ROLE-001)
const usersList = ref([])
const loadingUsers = ref(false)
const currentUsername = computed(() => {
  const u = authStore.user
  // admin login sets name=username, email=username@localhost
  // email login sets email=actual_email (which is also the username)
  if (u?.email?.endsWith('@localhost')) return u.name || u.email.replace('@localhost', '')
  return u?.email || null
})

// MCP Server URL state (#76)
const mcpUrlConfig = ref({ url: null, default_url: '' })
const mcpUrlInput = ref('')
const savingMcpUrl = ref(false)
const mcpUrlError = ref(null)
const mcpUrlSuccess = ref('')

// GitHub Templates state (TMPL-001)
const githubTemplates = ref([])
const githubTemplatesOriginal = ref([])
const githubTemplatesSource = ref('defaults')
const newTemplateRepo = ref('')
const newTemplateName = ref('')
const templateValidationError = ref('')
const loadingGithubTemplates = ref(false)
const savingGithubTemplates = ref(false)
const githubTemplatesDirty = computed(() => {
  return JSON.stringify(githubTemplates.value) !== JSON.stringify(githubTemplatesOriginal.value)
})

const trinityPrompt = ref('')
const originalPrompt = ref('')

// Public URL state
const publicUrl = ref('')
const publicUrlCurrent = ref('')
const savingPublicUrl = ref(false)
const publicUrlSaveSuccess = ref(false)

// API Key state
const anthropicKey = ref('')
const showApiKey = ref(false)
const testingApiKey = ref(false)
const savingApiKey = ref(false)
const apiKeyTestResult = ref(null)
const apiKeyTestMessage = ref('')
const anthropicKeyStatus = ref({
  configured: false,
  masked: null,
  source: null
})

// GitHub PAT state
const githubPat = ref('')
const showGithubPat = ref(false)
const testingGithubPat = ref(false)
const savingGithubPat = ref(false)
const githubPatTestResult = ref(null)
const githubPatTestMessage = ref('')
const githubPatStatus = ref({
  configured: false,
  masked: null,
  source: null
})
const githubPatPropagation = ref(null)

// Slack Integration state (SLACK-001)
const slackClientId = ref('')
const slackClientSecret = ref('')
const slackSigningSecret = ref('')
const showSlackClientSecret = ref(false)
const showSlackSigningSecret = ref(false)
const savingSlackSettings = ref(false)
const slackSaveSuccess = ref(false)
const slackSettings = ref({
  configured: false,
  client_id: { configured: false, masked: null, source: null },
  client_secret: { configured: false, masked: null, source: null },
  signing_secret: { configured: false, masked: null, source: null }
})

// Slack Transport state (SLACK-002)
const slackAppToken = ref('')
const showSlackAppToken = ref(false)
const slackTransportMode = ref('socket')
const slackTransportStatus = ref({
  connected: false,
  transport_mode: null,
  app_token_configured: false,
  app_token_masked: null,
  workspaces: []
})
const connectingSlack = ref(false)
const installingSlackWorkspace = ref(false)
const slackInstallSuccess = ref(false)

// SSH Access state
const sshAccessEnabled = ref(false)
const savingSshAccess = ref(false)

// Agent Quotas state (QUOTA-001)
const agentQuotas = ref({})
const agentQuotaValues = ref({ max_agents_creator: '10', max_agents_operator: '3', max_agents_user: '1' })
const agentQuotaLegacy = ref(null)
const savingQuotas = ref(false)

// Auto-Switch Subscriptions state (SUB-003)
const autoSwitchEnabled = ref(false)
const savingAutoSwitch = ref(false)

// Skills Library state
const skillsLibraryUrl = ref('')
const skillsLibraryBranch = ref('main')
const skillsLibraryStatus = ref({
  configured: false,
  cloned: false,
  skill_count: 0,
  commit_sha: null,
  last_sync: null
})
const syncingSkillsLibrary = ref(false)
const savingSkillsLibrary = ref(false)

// Default Avatars state (AVATAR-003)
const generatingDefaultAvatars = ref(false)
const defaultAvatarResult = ref(null)

// Subscriptions state (SUB-002)
const subscriptions = ref([])
const loadingSubscriptions = ref(false)
const addingSubscription = ref(false)
const deletingSubscription = ref(null)
const expandedSubscriptions = ref(new Set())
const encryptionConfigured = ref(true)
const newSubscription = ref({
  name: '',
  type: 'max',
  token: ''
})

// Agent assignment state (for subscription expanded rows)
const allAgents = ref([])
const loadingAgents = ref(false)
const assigningAgent = ref(null)
const unassigningAgent = ref(null)
const selectedAgentToAssign = ref({})

const agentSubscriptionMap = computed(() => {
  const map = {}
  for (const sub of subscriptions.value) {
    if (sub.agents) {
      for (const agentName of sub.agents) {
        map[agentName] = sub.name
      }
    }
  }
  return map
})

const hasChanges = computed(() => {
  return trinityPrompt.value !== originalPrompt.value
})

async function loadSettings() {
  loading.value = true
  error.value = null

  try {
    await settingsStore.fetchSettings()
    trinityPrompt.value = settingsStore.trinityPrompt || ''
    originalPrompt.value = trinityPrompt.value

    // Load independent settings in parallel
    await Promise.all([
      loadPublicUrl(),
      loadApiKeyStatus(),
      loadSlackSettings(),
      loadSlackTransportStatus(),
      loadMcpUrl(),
    ])
  } catch (e) {
    if (e.response?.status === 403) {
      error.value = 'Access denied. Admin privileges required.'
      router.push('/')
    } else {
      error.value = e.response?.data?.detail || 'Failed to load settings'
    }
  } finally {
    loading.value = false
  }
}

async function loadApiKeyStatus() {
  try {
    const response = await axios.get('/api/settings/api-keys')
    anthropicKeyStatus.value = response.data.anthropic || { configured: false }
    githubPatStatus.value = response.data.github || { configured: false }
  } catch (e) {
    console.error('Failed to load API key status:', e)
  }
}

async function testApiKey() {
  if (!anthropicKey.value) return

  testingApiKey.value = true
  apiKeyTestResult.value = null
  apiKeyTestMessage.value = ''

  try {
    const response = await axios.post('/api/settings/api-keys/anthropic/test', {
      api_key: anthropicKey.value
    })

    apiKeyTestResult.value = response.data.valid
    apiKeyTestMessage.value = response.data.valid ? 'API key is valid!' : (response.data.error || 'Invalid API key')
  } catch (e) {
    apiKeyTestResult.value = false
    apiKeyTestMessage.value = e.response?.data?.detail || 'Failed to test API key'
  } finally {
    testingApiKey.value = false
  }
}

async function saveApiKey() {
  if (!anthropicKey.value) return

  savingApiKey.value = true
  error.value = null

  try {
    const response = await axios.put('/api/settings/api-keys/anthropic', {
      api_key: anthropicKey.value
    })

    // Update status
    anthropicKeyStatus.value = {
      configured: true,
      masked: response.data.masked,
      source: 'settings'
    }

    // Clear input and show success
    anthropicKey.value = ''
    apiKeyTestResult.value = null
    showSuccess.value = true
    setTimeout(() => {
      showSuccess.value = false
    }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to save API key'
  } finally {
    savingApiKey.value = false
  }
}

async function testGithubPat() {
  if (!githubPat.value) return

  testingGithubPat.value = true
  githubPatTestResult.value = null
  githubPatTestMessage.value = ''

  try {
    const response = await axios.post('/api/settings/api-keys/github/test', {
      api_key: githubPat.value
    })

    githubPatTestResult.value = response.data.valid
    if (response.data.valid) {
      const tokenType = response.data.token_type || 'unknown'
      const hasRepoAccess = response.data.has_repo_access || false

      let message = `Valid! GitHub user: ${response.data.username}`

      if (tokenType === 'fine-grained') {
        message += hasRepoAccess
          ? '. ✓ Fine-grained PAT with repository permissions'
          : '. ⚠️ Missing repository permissions (need Administration + Contents)'
      } else {
        message += hasRepoAccess
          ? '. ✓ Has repo scope'
          : '. ⚠️ Missing repo scope'
      }

      githubPatTestMessage.value = message
    } else {
      githubPatTestMessage.value = response.data.error || 'Invalid PAT'
    }
  } catch (e) {
    githubPatTestResult.value = false
    githubPatTestMessage.value = e.response?.data?.detail || 'Failed to test PAT'
  } finally {
    testingGithubPat.value = false
  }
}

async function saveGithubPat() {
  if (!githubPat.value) return

  savingGithubPat.value = true
  error.value = null

  try {
    const response = await axios.put('/api/settings/api-keys/github', {
      api_key: githubPat.value
    })

    // Update status
    githubPatStatus.value = {
      configured: true,
      masked: response.data.masked,
      source: 'settings'
    }

    // Propagation result (#211): backend auto-pushes the new PAT to running agents
    githubPatPropagation.value = response.data.propagation || null

    // Clear input and show success
    githubPat.value = ''
    githubPatTestResult.value = null
    showSuccess.value = true
    setTimeout(() => {
      showSuccess.value = false
    }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to save GitHub PAT'
  } finally {
    savingGithubPat.value = false
  }
}

// Public URL methods
async function loadPublicUrl() {
  try {
    const value = await settingsStore.getSetting('public_chat_url')
    publicUrlCurrent.value = value || ''
  } catch (e) {
    console.error('Failed to load public URL:', e)
  }
}

async function savePublicUrl() {
  if (!publicUrl.value) return

  savingPublicUrl.value = true
  publicUrlSaveSuccess.value = false
  error.value = null

  try {
    // Strip trailing slash
    const url = publicUrl.value.replace(/\/+$/, '')
    await settingsStore.updateSetting('public_chat_url', url)
    publicUrlCurrent.value = url
    publicUrl.value = ''
    publicUrlSaveSuccess.value = true
    setTimeout(() => {
      publicUrlSaveSuccess.value = false
    }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to save public URL'
  } finally {
    savingPublicUrl.value = false
  }
}

// Slack Integration methods (SLACK-001)
async function loadSlackSettings() {
  try {
    const response = await axios.get('/api/settings/slack')
    slackSettings.value = response.data
  } catch (e) {
    console.error('Failed to load Slack settings:', e)
  }
}

async function saveSlackSettings() {
  if (!slackClientId.value && !slackClientSecret.value && !slackSigningSecret.value) return

  savingSlackSettings.value = true
  slackSaveSuccess.value = false
  error.value = null

  try {
    const payload = {}
    if (slackClientId.value) payload.client_id = slackClientId.value
    if (slackClientSecret.value) payload.client_secret = slackClientSecret.value
    if (slackSigningSecret.value) payload.signing_secret = slackSigningSecret.value

    await axios.put('/api/settings/slack', payload)

    // Reload settings to get updated status
    await loadSlackSettings()

    // Clear inputs and show success
    slackClientId.value = ''
    slackClientSecret.value = ''
    slackSigningSecret.value = ''
    slackSaveSuccess.value = true
    setTimeout(() => {
      slackSaveSuccess.value = false
    }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to save Slack settings'
  } finally {
    savingSlackSettings.value = false
  }
}

async function loadSlackTransportStatus() {
  try {
    const response = await axios.get('/api/settings/slack/status')
    slackTransportStatus.value = response.data
    if (response.data.transport_mode) {
      slackTransportMode.value = response.data.transport_mode
    }
  } catch (e) {
    console.error('Failed to load Slack transport status:', e)
  }
}

async function connectSlackTransport() {
  connectingSlack.value = true
  error.value = null
  try {
    const payload = { transport_mode: slackTransportMode.value }
    if (slackAppToken.value) {
      payload.app_token = slackAppToken.value
    }
    await axios.post('/api/settings/slack/connect', payload)
    slackAppToken.value = ''
    await loadSlackTransportStatus()
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to connect Slack transport'
  } finally {
    connectingSlack.value = false
  }
}

async function installSlackWorkspace() {
  installingSlackWorkspace.value = true
  error.value = null
  try {
    const response = await axios.post('/api/settings/slack/install')
    if (response.data.oauth_url) {
      window.location.href = response.data.oauth_url
    }
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to start Slack installation'
    installingSlackWorkspace.value = false
  }
}

async function savePrompt() {
  saving.value = true
  error.value = null
  showSuccess.value = false

  try {
    if (trinityPrompt.value.trim()) {
      await settingsStore.updateSetting('trinity_prompt', trinityPrompt.value)
    } else {
      await settingsStore.deleteSetting('trinity_prompt')
      trinityPrompt.value = ''
    }
    originalPrompt.value = trinityPrompt.value
    showSuccess.value = true
    setTimeout(() => {
      showSuccess.value = false
    }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to save settings'
  } finally {
    saving.value = false
  }
}

async function clearPrompt() {
  trinityPrompt.value = ''
  await savePrompt()
}

// Email whitelist methods (Phase 12.4)
async function loadEmailWhitelist() {
  loadingWhitelist.value = true
  try {
    const response = await axios.get('/api/settings/email-whitelist', {
      headers: authStore.authHeader
    })
    emailWhitelist.value = response.data.whitelist || []
  } catch (e) {
    console.error('Failed to load email whitelist:', e)
    // Non-fatal error - just log it
  } finally {
    loadingWhitelist.value = false
  }
}

async function addEmailToWhitelist() {
  if (!newEmail.value) return

  addingEmail.value = true
  error.value = null

  try {
    await axios.post('/api/settings/email-whitelist', {
      email: newEmail.value,
      source: 'manual'
    }, {
      headers: authStore.authHeader
    })

    newEmail.value = ''
    await loadEmailWhitelist()
    showSuccess.value = true
    setTimeout(() => {
      showSuccess.value = false
    }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to add email to whitelist'
  } finally {
    addingEmail.value = false
  }
}

async function removeEmailFromWhitelist(email) {
  if (!confirm(`Remove ${email} from whitelist?`)) return

  removingEmail.value = email
  error.value = null

  try {
    await axios.delete(`/api/settings/email-whitelist/${encodeURIComponent(email)}`, {
      headers: authStore.authHeader
    })

    await loadEmailWhitelist()
    showSuccess.value = true
    setTimeout(() => {
      showSuccess.value = false
    }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to remove email from whitelist'
  } finally {
    removingEmail.value = null
  }
}

function formatDate(dateString) {
  if (!dateString) return 'N/A'
  const date = new Date(dateString)
  const now = new Date()
  const diffInMs = now - date
  const diffInDays = Math.floor(diffInMs / (1000 * 60 * 60 * 24))

  if (diffInDays === 0) return 'Today'
  if (diffInDays === 1) return 'Yesterday'
  if (diffInDays < 7) return `${diffInDays} days ago`
  if (diffInDays < 30) return `${Math.floor(diffInDays / 7)} weeks ago`

  return date.toLocaleDateString()
}

// User management methods (ROLE-001)
async function loadUsers() {
  loadingUsers.value = true
  try {
    const response = await axios.get('/api/users', {
      headers: authStore.authHeader
    })
    usersList.value = response.data || []
  } catch (e) {
    console.error('Failed to load users:', e)
  } finally {
    loadingUsers.value = false
  }
}

async function updateUserRole(username, role) {
  try {
    await axios.put(`/api/users/${encodeURIComponent(username)}/role`, { role }, {
      headers: authStore.authHeader
    })
    await loadUsers()
  } catch (e) {
    alert(e.response?.data?.detail || 'Failed to update role')
    await loadUsers() // refresh to reset select
  }
}

// MCP Server URL methods (#76)
async function loadMcpUrl() {
  try {
    const response = await axios.get('/api/settings/mcp-url')
    mcpUrlConfig.value = response.data
  } catch (e) {
    console.error('Failed to load MCP URL:', e)
  }
}

async function _submitMcpUrl(action, successMsg) {
  savingMcpUrl.value = true
  mcpUrlError.value = null
  mcpUrlSuccess.value = ''

  try {
    await action()
    await loadMcpUrl()
    mcpUrlInput.value = ''
    mcpUrlSuccess.value = successMsg
    setTimeout(() => { mcpUrlSuccess.value = '' }, 3000)
  } catch (e) {
    mcpUrlError.value = e.response?.data?.detail || 'Failed to update MCP URL'
  } finally {
    savingMcpUrl.value = false
  }
}

async function saveMcpUrl() {
  if (!mcpUrlInput.value) return
  await _submitMcpUrl(
    () => axios.put('/api/settings/mcp-url', { url: mcpUrlInput.value }),
    'MCP server URL updated successfully.'
  )
}

async function resetMcpUrl() {
  await _submitMcpUrl(
    () => axios.delete('/api/settings/mcp-url'),
    'MCP server URL reset to auto-detect.'
  )
}

// GitHub Templates methods (TMPL-001)
const REPO_PATTERN = /^[a-zA-Z0-9._-]+\/[a-zA-Z0-9._-]+$/

async function loadGithubTemplates() {
  loadingGithubTemplates.value = true
  try {
    const response = await axios.get('/api/settings/github-templates', {
      headers: authStore.authHeader
    })
    githubTemplates.value = response.data.templates || []
    githubTemplatesOriginal.value = JSON.parse(JSON.stringify(githubTemplates.value))
    githubTemplatesSource.value = response.data.source || 'defaults'
  } catch (e) {
    console.error('Failed to load GitHub templates:', e)
  } finally {
    loadingGithubTemplates.value = false
  }
}

function addGithubTemplate() {
  templateValidationError.value = ''
  const repo = newTemplateRepo.value.trim()
  if (!repo) return

  if (!REPO_PATTERN.test(repo)) {
    templateValidationError.value = "Invalid format. Use 'owner/repo' (e.g., 'octocat/hello-world')."
    return
  }

  // Check for duplicates
  if (githubTemplates.value.some(t => t.github_repo === repo)) {
    templateValidationError.value = `'${repo}' is already in the list.`
    return
  }

  githubTemplates.value.push({
    github_repo: repo,
    display_name: newTemplateName.value.trim(),
    description: ''
  })

  newTemplateRepo.value = ''
  newTemplateName.value = ''
}

function removeGithubTemplate(index) {
  githubTemplates.value.splice(index, 1)
}

async function saveGithubTemplates() {
  savingGithubTemplates.value = true
  error.value = null

  try {
    await axios.put('/api/settings/github-templates', {
      templates: githubTemplates.value.map(t => ({
        github_repo: t.github_repo,
        display_name: t.display_name || '',
        description: t.description || ''
      }))
    }, {
      headers: authStore.authHeader
    })

    githubTemplatesOriginal.value = JSON.parse(JSON.stringify(githubTemplates.value))
    githubTemplatesSource.value = 'settings'
    showSuccess.value = true
    setTimeout(() => {
      showSuccess.value = false
    }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to save GitHub templates'
  } finally {
    savingGithubTemplates.value = false
  }
}

async function resetGithubTemplates() {
  if (!confirm('Reset GitHub templates to hardcoded defaults? This will remove your custom configuration.')) return

  savingGithubTemplates.value = true
  error.value = null

  try {
    await axios.delete('/api/settings/github-templates', {
      headers: authStore.authHeader
    })

    await loadGithubTemplates()
    showSuccess.value = true
    setTimeout(() => {
      showSuccess.value = false
    }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to reset GitHub templates'
  } finally {
    savingGithubTemplates.value = false
  }
}

// Agent Quotas methods (QUOTA-001)
async function loadAgentQuotas() {
  try {
    const response = await axios.get('/api/settings/agent-quotas', {
      headers: authStore.authHeader
    })
    agentQuotas.value = response.data.quotas || {}
    agentQuotaLegacy.value = response.data.legacy_setting || null
    agentQuotaValues.value = {
      max_agents_creator: agentQuotas.value.max_agents_creator?.value || '10',
      max_agents_operator: agentQuotas.value.max_agents_operator?.value || '3',
      max_agents_user: agentQuotas.value.max_agents_user?.value || '1'
    }
  } catch (e) {
    console.error('Failed to load agent quotas:', e)
  }
}

async function saveAgentQuotas() {
  savingQuotas.value = true
  error.value = null

  try {
    await axios.put('/api/settings/agent-quotas', {
      max_agents_creator: String(agentQuotaValues.value.max_agents_creator),
      max_agents_operator: String(agentQuotaValues.value.max_agents_operator),
      max_agents_user: String(agentQuotaValues.value.max_agents_user)
    }, {
      headers: authStore.authHeader
    })

    showSuccess.value = true
    setTimeout(() => {
      showSuccess.value = false
    }, 3000)

    await loadAgentQuotas()
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to save agent quotas'
  } finally {
    savingQuotas.value = false
  }
}

// SSH Access methods
async function loadOpsSettings() {
  try {
    const response = await axios.get('/api/settings/ops/config', {
      headers: authStore.authHeader
    })
    sshAccessEnabled.value = response.data.ssh_access_enabled === 'true'
  } catch (e) {
    console.error('Failed to load ops settings:', e)
  }
}

async function toggleSshAccess() {
  savingSshAccess.value = true
  error.value = null

  try {
    const newValue = !sshAccessEnabled.value
    await axios.put('/api/settings/ops/config', {
      settings: {
        ssh_access_enabled: newValue ? 'true' : 'false'
      }
    }, {
      headers: authStore.authHeader
    })

    sshAccessEnabled.value = newValue
    showSuccess.value = true
    setTimeout(() => {
      showSuccess.value = false
    }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to update SSH access setting'
  } finally {
    savingSshAccess.value = false
  }
}

// Auto-Switch Subscriptions methods (SUB-003)
async function loadAutoSwitchSetting() {
  try {
    const response = await axios.get('/api/subscriptions/settings/auto-switch', {
      headers: authStore.authHeader
    })
    autoSwitchEnabled.value = response.data.enabled
  } catch (e) {
    console.error('Failed to load auto-switch setting:', e)
  }
}

async function toggleAutoSwitch() {
  savingAutoSwitch.value = true
  error.value = null

  try {
    const newValue = !autoSwitchEnabled.value
    await axios.put(`/api/subscriptions/settings/auto-switch?enabled=${newValue}`, null, {
      headers: authStore.authHeader
    })

    autoSwitchEnabled.value = newValue
    showSuccess.value = true
    setTimeout(() => {
      showSuccess.value = false
    }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to update auto-switch setting'
  } finally {
    savingAutoSwitch.value = false
  }
}

// Skills Library methods
async function loadSkillsLibrarySettings() {
  try {
    const response = await axios.get('/api/skills/library/status', {
      headers: authStore.authHeader
    })
    skillsLibraryStatus.value = response.data
    skillsLibraryUrl.value = response.data.url || ''
    skillsLibraryBranch.value = response.data.branch || 'main'
  } catch (e) {
    console.error('Failed to load skills library status:', e)
  }
}

async function saveSkillsLibrarySettings() {
  savingSkillsLibrary.value = true
  error.value = null

  try {
    // Save URL setting
    if (skillsLibraryUrl.value.trim()) {
      await settingsStore.updateSetting('skills_library_url', skillsLibraryUrl.value.trim())
    } else {
      await settingsStore.deleteSetting('skills_library_url')
    }

    // Save branch setting
    if (skillsLibraryBranch.value.trim() && skillsLibraryBranch.value !== 'main') {
      await settingsStore.updateSetting('skills_library_branch', skillsLibraryBranch.value.trim())
    } else {
      await settingsStore.deleteSetting('skills_library_branch')
    }

    showSuccess.value = true
    setTimeout(() => {
      showSuccess.value = false
    }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to save skills library settings'
  } finally {
    savingSkillsLibrary.value = false
  }
}

async function syncSkillsLibrary() {
  syncingSkillsLibrary.value = true
  error.value = null

  try {
    // Save settings first
    await saveSkillsLibrarySettings()

    // Then sync
    const response = await axios.post('/api/skills/library/sync', {}, {
      headers: authStore.authHeader
    })

    // Reload status
    await loadSkillsLibrarySettings()

    showSuccess.value = true
    setTimeout(() => {
      showSuccess.value = false
    }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to sync skills library'
  } finally {
    syncingSkillsLibrary.value = false
  }
}

// Default Avatars methods (AVATAR-003)
async function generateDefaultAvatars() {
  generatingDefaultAvatars.value = true
  defaultAvatarResult.value = null
  error.value = null
  try {
    const response = await axios.post('/api/agents/avatars/generate-defaults', {}, {
      headers: authStore.authHeader,
      timeout: 300000 // 5 min timeout for sequential generation
    })
    defaultAvatarResult.value = response.data
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to generate default avatars'
  } finally {
    generatingDefaultAvatars.value = false
  }
}

// Subscription methods (SUB-001)
async function loadSubscriptions() {
  loadingSubscriptions.value = true
  try {
    // Check encryption status first
    try {
      const statusResponse = await axios.get('/api/subscriptions/encryption-status', {
        headers: authStore.authHeader
      })
      encryptionConfigured.value = statusResponse.data?.configured ?? true
    } catch {
      // Endpoint may not exist on older backends - assume configured
      encryptionConfigured.value = true
    }

    const response = await axios.get('/api/subscriptions', {
      headers: authStore.authHeader
    })
    subscriptions.value = response.data || []
  } catch (e) {
    console.error('Failed to load subscriptions:', e)
    // Non-admin users will get 403 - that's ok, just hide the section
    if (e.response?.status !== 403) {
      error.value = e.response?.data?.detail || 'Failed to load subscriptions'
    }
  } finally {
    loadingSubscriptions.value = false
  }
}

function clearNewSubscription() {
  newSubscription.value = {
    name: '',
    type: 'max',
    token: ''
  }
}

async function addSubscription() {
  if (!newSubscription.value.name || !newSubscription.value.token.startsWith('sk-ant-oat01-')) return

  addingSubscription.value = true
  error.value = null

  try {
    await axios.post('/api/subscriptions', {
      name: newSubscription.value.name,
      token: newSubscription.value.token,
      subscription_type: newSubscription.value.type || null
    }, {
      headers: authStore.authHeader
    })

    // Clear form and reload list
    clearNewSubscription()
    await loadSubscriptions()

    showSuccess.value = true
    setTimeout(() => {
      showSuccess.value = false
    }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to register subscription'
  } finally {
    addingSubscription.value = false
  }
}

async function deleteSubscription(subscription) {
  if (!confirm(`Delete subscription "${subscription.name}"?\n\nThis will clear the subscription from all ${subscription.agent_count || 0} assigned agent(s).`)) {
    return
  }

  deletingSubscription.value = subscription.id
  error.value = null

  try {
    await axios.delete(`/api/subscriptions/${subscription.id}`, {
      headers: authStore.authHeader
    })

    // Remove from expanded set if it was expanded
    expandedSubscriptions.value.delete(subscription.id)

    // Reload list
    await loadSubscriptions()

    showSuccess.value = true
    setTimeout(() => {
      showSuccess.value = false
    }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to delete subscription'
  } finally {
    deletingSubscription.value = null
  }
}

function toggleSubscriptionDetails(subscriptionId) {
  if (expandedSubscriptions.value.has(subscriptionId)) {
    expandedSubscriptions.value.delete(subscriptionId)
  } else {
    expandedSubscriptions.value.add(subscriptionId)
    fetchAgentList()
  }
  // Force reactivity update
  expandedSubscriptions.value = new Set(expandedSubscriptions.value)
}

async function fetchAgentList() {
  if (allAgents.value.length > 0 || loadingAgents.value) return
  loadingAgents.value = true
  try {
    const response = await axios.get('/api/agents', {
      headers: authStore.authHeader
    })
    allAgents.value = response.data || []
  } catch (e) {
    console.error('Failed to fetch agent list:', e)
  } finally {
    loadingAgents.value = false
  }
}

function getAvailableAgents(subId) {
  const sub = subscriptions.value.find(s => s.id === subId)
  const assignedHere = sub?.agents || []
  return allAgents.value
    .filter(a => !assignedHere.includes(a.name))
    .sort((a, b) => {
      const aOnOther = agentSubscriptionMap.value[a.name] ? 1 : 0
      const bOnOther = agentSubscriptionMap.value[b.name] ? 1 : 0
      return aOnOther - bOnOther || a.name.localeCompare(b.name)
    })
}

async function assignAgentToSubscription(subName, agentName) {
  assigningAgent.value = agentName
  error.value = null
  try {
    await axios.put(`/api/subscriptions/agents/${encodeURIComponent(agentName)}?subscription_name=${encodeURIComponent(subName)}`, {}, {
      headers: authStore.authHeader
    })
    await loadSubscriptions()
    // Clear dropdown selection for all subs
    selectedAgentToAssign.value = {}
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to assign agent'
  } finally {
    assigningAgent.value = null
  }
}

async function unassignAgentFromSubscription(agentName) {
  if (!confirm(`Remove "${agentName}" from this subscription?\n\nIf the agent is running, it will be restarted.`)) return
  unassigningAgent.value = agentName
  error.value = null
  try {
    await axios.delete(`/api/subscriptions/agents/${encodeURIComponent(agentName)}`, {
      headers: authStore.authHeader
    })
    await loadSubscriptions()
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to unassign agent'
  } finally {
    unassigningAgent.value = null
  }
}

onMounted(() => {
  // Check if user is admin
  const userData = authStore.user
  // For now, allow access - backend will reject if not admin
  loadSettings()
  loadEmailWhitelist()
  loadUsers()
  loadGithubTemplates()
  loadOpsSettings()
  loadAgentQuotas()
  loadSkillsLibrarySettings()
  loadSubscriptions()
  loadAutoSwitchSetting()

  // Handle Slack OAuth callback
  if (route.query.slack === 'installed') {
    slackInstallSuccess.value = true
    setTimeout(() => { slackInstallSuccess.value = false }, 3000)
    router.replace({ query: {} })
  } else if (route.query.slack === 'error') {
    error.value = `Slack installation failed: ${route.query.reason || 'unknown error'}`
    router.replace({ query: {} })
  }
})
</script>
