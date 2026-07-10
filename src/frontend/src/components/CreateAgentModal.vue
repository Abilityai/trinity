<template>
  <div class="fixed z-10 inset-0 overflow-y-auto">
    <div class="flex items-end justify-center min-h-screen pt-4 px-4 pb-20 text-center sm:block sm:p-0">
      <div class="fixed inset-0 bg-gray-500 dark:bg-gray-900 bg-opacity-75 dark:bg-opacity-75 transition-opacity"></div>

      <span class="hidden sm:inline-block sm:align-middle sm:h-screen">&#8203;</span>

      <div class="inline-block align-bottom bg-white dark:bg-gray-800 rounded-lg text-left shadow-xl transform transition-all sm:my-8 sm:align-middle sm:max-w-lg sm:w-full max-h-[90vh] overflow-y-auto">
        <form @submit.prevent="createAgent">
          <div class="bg-white dark:bg-gray-800 px-4 pt-5 pb-4 sm:p-6 sm:pb-4">
            <h3 class="text-lg leading-6 font-medium text-gray-900 dark:text-white mb-4">Create New Agent</h3>

            <div class="space-y-4">
              <div>
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Agent Name</label>
                <input
                  v-model="form.name"
                  type="text"
                  required
                  class="mt-1 block w-full border border-gray-300 dark:border-gray-600 rounded-md shadow-sm px-3 py-2 focus:ring-action-primary-500 focus:border-action-primary-500 sm:text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500"
                  placeholder="my-agent"
                />
              </div>

              <div>
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Template</label>

                <!-- Loading state -->
                <div v-if="templatesLoading" class="mt-2 flex items-center justify-center py-4">
                  <svg class="animate-spin h-5 w-5 text-action-primary-500 mr-2" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                  </svg>
                  <span class="text-sm text-gray-500 dark:text-gray-400">Loading templates...</span>
                </div>

                <!-- Error state -->
                <div v-else-if="templatesError" class="mt-2 p-3 bg-status-danger-50 dark:bg-status-danger-900/30 border border-status-danger-200 dark:border-status-danger-800 rounded-lg">
                  <p class="text-sm text-status-danger-600 dark:text-status-danger-400">{{ templatesError }}</p>
                  <button @click="fetchTemplates" type="button" class="mt-1 text-sm text-status-danger-700 dark:text-status-danger-300 underline">
                    Try again
                  </button>
                </div>

                <div v-else class="mt-1 space-y-2 max-h-80 overflow-y-auto">
                  <!-- Featured fork-to-own templates (trinity-enterprise#93) -->
                  <div v-if="featuredTemplates.length > 0" class="pb-1">
                    <div
                      v-for="template in featuredTemplates"
                      :key="template.id"
                      @click="form.template = template.id"
                      :class="[
                        'relative flex items-center p-3 border-2 rounded-lg cursor-pointer transition-all',
                        form.template === template.id ? 'border-action-primary-500 bg-action-primary-50 dark:bg-action-primary-900/30 ring-2 ring-action-primary-500' : 'border-action-primary-300 dark:border-action-primary-700 hover:border-action-primary-400 dark:hover:border-action-primary-500 bg-action-primary-50/40 dark:bg-action-primary-900/10'
                      ]"
                    >
                      <div class="flex-shrink-0 w-8 h-8 rounded-full bg-action-primary-600 flex items-center justify-center">
                        <svg class="w-4 h-4 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
                        </svg>
                      </div>
                      <div class="ml-3 flex-1">
                        <p class="text-sm font-medium text-gray-900 dark:text-white">{{ template.display_name }}</p>
                        <p class="text-xs text-gray-500 dark:text-gray-400 truncate">{{ template.tagline || truncateDescription(template.description) }}</p>
                        <p class="text-[11px] text-action-primary-700 dark:text-action-primary-300 mt-0.5">Creates a copy in your own GitHub account</p>
                      </div>
                      <div v-if="form.template === template.id" class="flex-shrink-0 text-action-primary-500 dark:text-action-primary-400">
                        <svg class="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                          <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
                        </svg>
                      </div>
                    </div>
                  </div>

                  <!-- Blank agent option -->
                  <div
                    @click="form.template = ''"
                    :class="[
                      'relative flex items-center p-3 border rounded-lg cursor-pointer transition-all',
                      form.template === '' ? 'border-action-primary-500 bg-action-primary-50 dark:bg-action-primary-900/30 ring-2 ring-action-primary-500' : 'border-gray-300 dark:border-gray-600 hover:border-gray-400 dark:hover:border-gray-500'
                    ]"
                  >
                    <div class="flex-shrink-0 w-8 h-8 rounded-full bg-gray-200 dark:bg-gray-600 flex items-center justify-center">
                      <svg class="w-4 h-4 text-gray-500 dark:text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6m0 0v6m0-6h6m-6 0H6" />
                      </svg>
                    </div>
                    <div class="ml-3 flex-1">
                      <p class="text-sm font-medium text-gray-900 dark:text-white">Blank Agent (Claude Code)</p>
                      <p class="text-xs text-gray-500 dark:text-gray-400">Start with empty config using Claude Code runtime</p>
                    </div>
                    <div v-if="form.template === ''" class="flex-shrink-0 text-action-primary-500 dark:text-action-primary-400">
                      <svg class="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
                      </svg>
                    </div>
                  </div>

                  <!-- GitHub Repository URL option -->
                  <div
                    @click="form.template = 'github-custom'"
                    :class="[
                      'relative flex items-center p-3 border rounded-lg cursor-pointer transition-all',
                      form.template === 'github-custom' ? 'border-action-primary-500 bg-action-primary-50 dark:bg-action-primary-900/30 ring-2 ring-action-primary-500' : 'border-gray-300 dark:border-gray-600 hover:border-gray-400 dark:hover:border-gray-500'
                    ]"
                  >
                    <div class="flex-shrink-0 w-8 h-8 rounded-full bg-gray-900 dark:bg-gray-700 flex items-center justify-center">
                      <svg class="w-4 h-4 text-white" fill="currentColor" viewBox="0 0 24 24">
                        <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
                      </svg>
                    </div>
                    <div class="ml-3 flex-1">
                      <p class="text-sm font-medium text-gray-900 dark:text-white">GitHub Repository</p>
                      <p class="text-xs text-gray-500 dark:text-gray-400">Create from any GitHub repository URL</p>
                    </div>
                    <div v-if="form.template === 'github-custom'" class="flex-shrink-0 text-action-primary-500 dark:text-action-primary-400">
                      <svg class="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
                      </svg>
                    </div>
                  </div>
                  <!-- GitHub repo URL input (shown when GitHub Repository is selected) -->
                  <div v-if="form.template === 'github-custom'" class="pl-11">
                    <input
                      v-model="githubRepoUrl"
                      type="text"
                      ref="githubRepoInput"
                      class="block w-full border border-gray-300 dark:border-gray-600 rounded-md shadow-sm px-3 py-2 focus:ring-action-primary-500 focus:border-action-primary-500 sm:text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500"
                      placeholder="owner/repo or https://github.com/owner/repo"
                      @click.stop
                    />
                    <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">Enter a GitHub repository in <code>owner/repo</code> format</p>
                  </div>

                  <!-- Local templates section (shown first after Blank Agent) -->
                  <div v-if="localTemplates.length > 0" class="pt-2">
                    <p class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2 flex items-center">
                      <svg class="w-4 h-4 mr-1" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 19a2 2 0 01-2-2V7a2 2 0 012-2h4l2 2h4a2 2 0 012 2v1M5 19h14a2 2 0 002-2v-5a2 2 0 00-2-2H9a2 2 0 00-2 2v5a2 2 0 01-2 2z" />
                      </svg>
                      Local Templates
                    </p>
                    <div
                      v-for="template in localTemplates"
                      :key="template.id"
                      @click="form.template = template.id"
                      :class="[
                        'relative flex items-center p-3 border rounded-lg cursor-pointer transition-all',
                        form.template === template.id ? 'border-action-primary-500 bg-action-primary-50 dark:bg-action-primary-900/30 ring-2 ring-action-primary-500' : 'border-gray-300 dark:border-gray-600 hover:border-gray-400 dark:hover:border-gray-500'
                      ]"
                    >
                      <div class="flex-shrink-0 w-8 h-8 rounded-full bg-action-primary-100 dark:bg-action-primary-900/50 flex items-center justify-center">
                        <svg class="w-4 h-4 text-action-primary-600 dark:text-action-primary-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                        </svg>
                      </div>
                      <div class="ml-3 flex-1">
                        <p class="text-sm font-medium text-gray-900 dark:text-white">{{ template.display_name }}</p>
                        <p class="text-xs text-gray-500 dark:text-gray-400 truncate">{{ truncateDescription(template.description) }}</p>
                      </div>
                      <div v-if="form.template === template.id" class="flex-shrink-0 text-action-primary-500 dark:text-action-primary-400">
                        <svg class="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                          <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
                        </svg>
                      </div>
                    </div>
                  </div>

                  <!-- GitHub templates section -->
                  <div v-if="githubTemplates.length > 0" class="pt-2">
                    <p class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2 flex items-center">
                      <svg class="w-4 h-4 mr-1" fill="currentColor" viewBox="0 0 24 24">
                        <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
                      </svg>
                      GitHub Templates
                    </p>
                    <div
                      v-for="template in githubTemplates"
                      :key="template.id"
                      @click="form.template = template.id"
                      :class="[
                        'relative flex items-center p-3 border rounded-lg cursor-pointer transition-all',
                        form.template === template.id ? 'border-action-primary-500 bg-action-primary-50 dark:bg-action-primary-900/30 ring-2 ring-action-primary-500' : 'border-gray-300 dark:border-gray-600 hover:border-gray-400 dark:hover:border-gray-500'
                      ]"
                    >
                      <div class="flex-shrink-0 w-8 h-8 rounded-full bg-gray-900 dark:bg-gray-700 flex items-center justify-center">
                        <svg class="w-4 h-4 text-white" fill="currentColor" viewBox="0 0 24 24">
                          <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
                        </svg>
                      </div>
                      <div class="ml-3 flex-1">
                        <p class="text-sm font-medium text-gray-900 dark:text-white">{{ template.display_name }}</p>
                        <p class="text-xs text-gray-500 dark:text-gray-400">{{ template.github_repo }}</p>
                      </div>
                      <div v-if="form.template === template.id" class="flex-shrink-0 text-action-primary-500 dark:text-action-primary-400">
                        <svg class="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                          <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
                        </svg>
                      </div>
                    </div>
                  </div>
                </div>

                <!-- Fork-to-own fields (trinity-enterprise#93) -->
                <div v-if="isForkToOwn" class="mt-3 p-3 border border-action-primary-200 dark:border-action-primary-800 rounded-lg space-y-3">
                  <p class="text-xs text-gray-600 dark:text-gray-300">
                    This template is copied into a repository <span class="font-medium">you own</span> — your agent's
                    knowledge lives there, and template updates stay one <code class="text-[11px]">git pull upstream</code> away.
                  </p>
                  <div>
                    <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Your repository <span class="text-status-danger-500">*</span></label>
                    <input
                      v-model="forkDestination"
                      type="text"
                      class="mt-1 block w-full border border-gray-300 dark:border-gray-600 rounded-md shadow-sm px-3 py-2 focus:ring-action-primary-500 focus:border-action-primary-500 sm:text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500"
                      placeholder="your-github-username/my-agent-brain"
                    />
                    <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">Doesn't need to exist — it will be created for you. (If you pre-create it, leave it empty: no README.)</p>
                  </div>
                  <div>
                    <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">GitHub token <span class="text-status-danger-500">*</span></label>
                    <input
                      v-model="forkPat"
                      type="password"
                      autocomplete="off"
                      class="mt-1 block w-full border border-gray-300 dark:border-gray-600 rounded-md shadow-sm px-3 py-2 focus:ring-action-primary-500 focus:border-action-primary-500 sm:text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500"
                      placeholder="ghp_… or github_pat_…"
                    />
                    <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                      <span class="font-medium">Recommended:</span> a fine-grained token scoped to just this repository
                      (Administration + Contents write). A classic PAT with <code>repo</code> scope also works but grants
                      access to <em>all</em> your repositories — and the agent can read its own git credential, so prefer
                      the narrow token. Stored encrypted as this agent's git identity.
                    </p>
                  </div>
                  <div>
                    <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Visibility</label>
                    <div class="mt-1 flex gap-4">
                      <label class="inline-flex items-center text-sm text-gray-700 dark:text-gray-300">
                        <input type="radio" v-model="forkPrivate" :value="true" class="text-action-primary-600 focus:ring-action-primary-500" />
                        <span class="ml-1.5">Private (recommended)</span>
                      </label>
                      <label class="inline-flex items-center text-sm text-gray-700 dark:text-gray-300">
                        <input type="radio" v-model="forkPrivate" :value="false" class="text-action-primary-600 focus:ring-action-primary-500" />
                        <span class="ml-1.5">Public</span>
                      </label>
                    </div>
                    <div v-if="!forkPrivate" class="mt-2 p-2 bg-status-danger-50 dark:bg-status-danger-900/30 border border-status-danger-300 dark:border-status-danger-700 rounded-md">
                      <p class="text-xs font-medium text-status-danger-700 dark:text-status-danger-300">
                        ⚠ Everything this agent captures — notes, insights, potentially personal information — will be
                        publicly visible on GitHub. Choose Private unless you are certain.
                      </p>
                    </div>
                  </div>
                </div>

                <!-- Selected template description -->
                <div v-if="selectedTemplate" class="mt-3 p-3 bg-gray-50 dark:bg-gray-700 rounded-lg">
                  <p class="text-sm text-gray-700 dark:text-gray-300">{{ selectedTemplate.description }}</p>
                  <div v-if="selectedTemplate.mcp_servers && selectedTemplate.mcp_servers.length > 0" class="mt-2">
                    <p class="text-xs text-gray-500 dark:text-gray-400 mb-1">MCP Servers:</p>
                    <div class="flex flex-wrap gap-1">
                      <span v-for="server in selectedTemplate.mcp_servers" :key="typeof server === 'string' ? server : server.name" class="px-2 py-0.5 text-xs bg-gray-200 dark:bg-gray-600 text-gray-700 dark:text-gray-300 rounded">
                        {{ typeof server === 'string' ? server : server.name }}
                      </span>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <div v-if="error" class="mt-4 text-status-danger-600 dark:text-status-danger-400 text-sm">
              {{ error }}
            </div>
          </div>

          <div class="bg-gray-50 dark:bg-gray-900 px-4 py-3 sm:px-6 sm:flex sm:flex-row-reverse">
            <button
              type="submit"
              :disabled="loading"
              class="w-full inline-flex justify-center rounded-md border border-transparent shadow-sm px-4 py-2 bg-action-primary-600 text-base font-medium text-white hover:bg-action-primary-700 focus:outline-none focus:ring-2 focus:ring-offset-2 dark:focus:ring-offset-gray-800 focus:ring-action-primary-500 sm:ml-3 sm:w-auto sm:text-sm disabled:opacity-50"
            >
              <svg v-if="loading" class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
              </svg>
              {{ loading ? (isForkToOwn ? 'Creating your repository…' : 'Creating...') : 'Create Agent' }}
            </button>
            <button
              type="button"
              @click="$emit('close')"
              class="mt-3 w-full inline-flex justify-center rounded-md border border-gray-300 dark:border-gray-600 shadow-sm px-4 py-2 bg-white dark:bg-gray-700 text-base font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-600 focus:outline-none focus:ring-2 focus:ring-offset-2 dark:focus:ring-offset-gray-800 focus:ring-action-primary-500 sm:mt-0 sm:ml-3 sm:w-auto sm:text-sm"
            >
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, computed, watch, nextTick } from 'vue'
import { useAgentsStore } from '../stores/agents'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'

const props = defineProps({
  initialTemplate: {
    type: String,
    default: ''
  }
})

const emit = defineEmits(['close', 'created'])
const agentsStore = useAgentsStore()
const authStore = useAuthStore()

const form = reactive({
  name: '',
  template: props.initialTemplate || ''
})

const githubRepoUrl = ref('')
const githubRepoInput = ref(null)

// Fork-to-own inputs (trinity-enterprise#93)
const forkDestination = ref('')
const forkPat = ref('')
const forkPrivate = ref(true)

// Watch for initialTemplate changes (in case modal is reused)
watch(() => props.initialTemplate, (newVal) => {
  form.template = newVal || ''
})

// Auto-focus GitHub repo input when selected
watch(() => form.template, (newVal) => {
  if (newVal === 'github-custom') {
    nextTick(() => {
      githubRepoInput.value?.focus()
    })
  }
})

const templates = ref([])
const loading = ref(false)
const error = ref('')
const templatesLoading = ref(true)
const templatesError = ref('')

// Computed properties to separate GitHub and local templates.
// Fork-to-own templates (trinity-enterprise#93) get the featured section and
// are excluded from the plain GitHub list.
// Only 'required' is load-bearing in v1 — the backend enforces exactly that
// value, so the UI features (and demands destination+PAT for) the same set.
const featuredTemplates = computed(() => {
  return templates.value.filter(t => t.fork_to_own === 'required')
})

const githubTemplates = computed(() => {
  return templates.value.filter(t => t.source === 'github' && t.fork_to_own !== 'required')
})

const localTemplates = computed(() => {
  return templates.value.filter(t => t.source === 'local' || !t.source)
})

const isForkToOwn = computed(() => selectedTemplate.value?.fork_to_own === 'required')

const selectedTemplate = computed(() => {
  if (!form.template) return null
  return templates.value.find(t => t.id === form.template)
})

// Helper function to truncate long descriptions
const truncateDescription = (description) => {
  if (!description) return ''
  const firstLine = description.split('\n')[0]
  if (firstLine.length > 60) {
    return firstLine.substring(0, 57) + '...'
  }
  return firstLine
}

// Parse GitHub repo from various input formats into owner/repo
const parseGithubRepo = (input) => {
  if (!input) return null
  let repo = input.trim()
  // Handle full URLs: https://github.com/owner/repo(.git)
  const urlMatch = repo.match(/github\.com\/([^/]+\/[^/\s#?.]+)/)
  if (urlMatch) {
    repo = urlMatch[1].replace(/\.git$/, '')
  }
  // Validate owner/repo format
  if (/^[a-zA-Z0-9._-]+\/[a-zA-Z0-9._-]+$/.test(repo)) {
    return repo
  }
  return null
}

const onTemplateChange = () => {
  // Template selection changed - no action needed
  // All config comes from backend based on template
}

const fetchTemplates = async () => {
  templatesLoading.value = true
  templatesError.value = ''
  try {
    const response = await axios.get('/api/templates', {
      headers: authStore.authHeader
    })
    templates.value = response.data
    // If a caller injected an initialTemplate that doesn't exist in this
    // deploy (e.g. the onboarding wizard prefilling `local:scout` on an
    // install without that template), fall back to the blank agent so the
    // form never points at a missing template.
    if (
      form.template &&
      form.template !== 'github-custom' &&
      !templates.value.some(t => t.id === form.template)
    ) {
      form.template = ''
    }
  } catch (err) {
    console.error('Failed to fetch templates:', err)
    templatesError.value = 'Failed to load templates'
  } finally {
    templatesLoading.value = false
  }
}

const createAgent = async () => {
  loading.value = true
  error.value = ''

  try {
    // Only send name and template - backend handles everything else
    const payload = {
      name: form.name
    }

    if (form.template === 'github-custom') {
      const repo = parseGithubRepo(githubRepoUrl.value)
      if (!repo) {
        error.value = 'Please enter a valid GitHub repository (e.g., owner/repo)'
        loading.value = false
        return
      }
      payload.template = `github:${repo}`
    } else if (form.template) {
      payload.template = form.template
    }

    // Fork-to-own (trinity-enterprise#93): destination + token are required
    // for templates that declare it. Shape check only — the backend owns the
    // authoritative validation.
    if (isForkToOwn.value) {
      const dest = forkDestination.value.trim()
      if (!/^[A-Za-z0-9][A-Za-z0-9-]*\/[A-Za-z0-9._-]+$/.test(dest)) {
        error.value = 'Enter your repository as owner/name (e.g., your-username/my-agent-brain)'
        loading.value = false
        return
      }
      if (!forkPat.value.trim()) {
        error.value = 'A GitHub token is required to create the repository in your account'
        loading.value = false
        return
      }
      payload.fork_to_own = {
        destination_repo: dest,
        github_pat: forkPat.value.trim(),
        private: forkPrivate.value
      }
    }

    const agent = await agentsStore.createAgent(payload)
    forkPat.value = ''  // hygiene: don't keep the token in the reactive ref
    emit('created', agent)
    emit('close')
  } catch (err) {
    const detail = err.response?.data?.detail
    if (detail && typeof detail === 'object' && (detail.code === 'QUOTA_EXCEEDED' || detail.error)) {
      // Structured errors (quota, FORK_* codes) carry a user-facing message.
      error.value = `${detail.error}`
    } else if (typeof detail === 'string') {
      error.value = detail
    } else if (Array.isArray(detail)) {
      // FastAPI 422 validation errors
      error.value = detail[0]?.msg || 'Invalid input'
    } else {
      error.value = 'Failed to create agent'
    }
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  fetchTemplates()
})
</script>
