<template>
  <div>
    <div class="flex justify-between items-start mb-6">
      <div>
        <h2 class="text-lg font-medium text-gray-900 dark:text-white">MCP API Keys</h2>
        <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
          Manage API keys for accessing the Trinity MCP server
        </p>
      </div>
      <button
        @click="showCreateModal = true"
        class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700"
      >
        <svg class="-ml-1 mr-2 h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
        </svg>
        Create API Key
      </button>
    </div>

    <!-- MCP Connection Info -->
    <div class="bg-status-info-50 dark:bg-status-info-900/30 border border-status-info-200 dark:border-status-info-800 rounded-lg p-4 mb-6">
      <div class="flex">
        <div class="flex-shrink-0">
          <svg class="h-5 w-5 text-status-info-400" fill="currentColor" viewBox="0 0 20 20">
            <path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clip-rule="evenodd" />
          </svg>
        </div>
        <div class="ml-3 flex-1">
          <h3 class="text-sm font-medium text-status-info-800 dark:text-status-info-300">Connect to MCP Server</h3>
          <p class="text-sm text-status-info-700 dark:text-status-info-400 mt-1">
            Add this to your <code class="bg-status-info-100 dark:bg-status-info-800 px-1 rounded">.mcp.json</code> configuration:
          </p>
          <pre class="mt-2 bg-status-info-100 dark:bg-status-info-800 rounded p-3 text-xs overflow-x-auto text-status-info-900 dark:text-status-info-100">{
  "mcpServers": {
    "trinity": {
      "type": "http",
      "url": "{{ mcpServerUrl }}",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}</pre>
        </div>
      </div>
    </div>

    <!-- API Keys List -->
    <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg overflow-hidden">
      <div v-if="loading" class="p-8 text-center">
        <div class="animate-spin rounded-full h-12 w-12 border-b-2 border-action-primary-600 mx-auto"></div>
        <p class="mt-4 text-gray-500 dark:text-gray-400">Loading API keys...</p>
      </div>

      <div v-else-if="displayedKeys.length === 0" class="text-center py-12">
        <svg class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
        </svg>
        <h3 class="mt-2 text-sm font-medium text-gray-900 dark:text-white">No API keys</h3>
        <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Create an API key to start using the MCP server.</p>
      </div>

      <ul v-else class="divide-y divide-gray-200 dark:divide-gray-700">
        <!-- #1848: the dark hover surface is the chrome shade gray-750, not
             gray-700 (which is border-strong). On gray-700 the row's tertiary
             meta ink sits at 4.06:1 — below AA — whenever the cursor is on the
             row, and the gray-700 prefix chip below becomes invisible (1.00:1).
             On gray-750 they read 5.21:1 and 1.28:1. -->
        <li v-for="key in displayedKeys" :key="key.id" class="p-6 hover:bg-gray-50 dark:hover:bg-gray-750">
          <div class="flex items-center justify-between">
            <div class="flex items-center flex-1">
              <div class="flex-shrink-0">
                <div class="h-10 w-10 rounded-full flex items-center justify-center"
                     :class="key.is_active ? 'bg-status-success-100 dark:bg-status-success-900/50' : 'bg-status-danger-100 dark:bg-status-danger-900/50'">
                  <svg class="h-6 w-6" :class="key.is_active ? 'text-status-success-600 dark:text-status-success-400' : 'text-status-danger-600 dark:text-status-danger-400'" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
                  </svg>
                </div>
              </div>
              <div class="ml-4 flex-1">
                <div class="flex items-center">
                  <h3 class="text-sm font-medium text-gray-900 dark:text-white">{{ key.name }}</h3>
                  <span class="ml-3 inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium"
                        :class="key.is_active ? 'bg-status-success-100 dark:bg-status-success-900/50 text-status-success-800 dark:text-status-success-300' : 'bg-status-danger-100 dark:bg-status-danger-900/50 text-status-danger-800 dark:text-status-danger-300'">
                    {{ key.is_active ? 'Active' : 'Revoked' }}
                  </span>
                  <span v-if="key.scope === 'agent'" class="ml-2 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-accent-purple-100 dark:bg-accent-purple-900/50 text-accent-purple-800 dark:text-accent-purple-300">
                    Agent
                  </span>
                  <!-- #2323: a bounded read-only machine credential. Badged
                       because an unbadged ops key is visually identical to an
                       unbounded personal key here, which defeats the point of
                       having a bounded tier at all. -->
                  <span v-else-if="key.scope === 'ops'"
                        class="ml-2 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-status-info-100 dark:bg-status-info-900/50 text-status-info-800 dark:text-status-info-300"
                        title="Read-only machine credential — fleet health, telemetry, execution and subscription reads only">
                    Ops (read-only)
                  </span>
                  <span v-else-if="key.scope === 'system'" class="ml-2 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-status-urgent-100 dark:bg-status-urgent-900/50 text-status-urgent-800 dark:text-status-urgent-300">
                    System
                  </span>
                  <!-- ent#163: this key can act as ANY end user with portal
                       access, so it must never render like an ordinary user
                       key on the page where an admin audits keys. -->
                  <span v-else-if="key.scope === 'portal_delegate'"
                        class="ml-2 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-status-warning-100 dark:bg-status-warning-900/50 text-status-warning-800 dark:text-status-warning-300"
                        title="Can exchange an end-user email for a portal session — treat as a delegated-identity credential">
                    Portal Delegate
                  </span>
                </div>
                <p v-if="key.description" class="mt-1 text-sm text-gray-500 dark:text-gray-400">{{ key.description }}</p>
                <!-- #1848: uniform inline flow. `space-x-4` is margin-left on every
                     item after the first, which indents continuation lines the moment
                     the row wraps — so a wrapping row needs `gap`, not `space-x`. Each
                     label+value atom is nowrap so a timestamp can never orphan its
                     AM/PM; the email instead gets min-w-0 + break-all, because an
                     address has no spaces and so would otherwise be an unbreakable
                     token that overflows the row. The Last-used slot always renders so
                     every row has the same item set — that, not nowrap alone, is what
                     stops rows breaking at different points. Ink is the tertiary ladder
                     rung (gray-500/gray-400), which was inverted and failed AA in both
                     themes at rest.

                     The owner span is the one item that can now go multi-line (break-all
                     is what made that reachable), so its icon is items-start + mt-0.5
                     rather than items-center — it must sit on the FIRST line, not float
                     to the vertical centre of two. text-xs is a 16px line box and the
                     glyph is 12px, so mt-0.5 (2px) is exactly the offset items-center
                     was already producing: single-line rows are pixel-identical. -->
                <div class="mt-1 flex items-center flex-wrap gap-x-4 gap-y-1 text-xs text-gray-500 dark:text-gray-400">
                  <span>
                    <code class="bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded font-mono">{{ key.key_prefix }}...</code>
                  </span>
                  <span v-if="key.user_email" class="flex items-start min-w-0 break-all">
                    <svg class="h-3 w-3 mr-1 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
                    </svg>
                    {{ key.user_email }}
                  </span>
                  <span class="whitespace-nowrap tabular-nums">Created {{ formatDate(key.created_at) }}</span>
                  <span class="whitespace-nowrap tabular-nums">Last used {{ key.last_used_at ? formatDate(key.last_used_at) : 'never' }}</span>
                  <span class="flex items-center whitespace-nowrap tabular-nums">
                    <svg class="h-3 w-3 mr-1" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
                    </svg>
                    {{ key.usage_count }} requests
                  </span>
                </div>
              </div>
            </div>
            <div class="ml-4 flex items-center space-x-2">
              <button
                v-if="key.is_active"
                @click="revokeKey(key.id)"
                class="inline-flex items-center px-3 py-1.5 border border-status-warning-300 dark:border-status-warning-600 shadow-sm text-xs font-medium rounded text-status-warning-700 dark:text-status-warning-300 bg-white dark:bg-gray-700 hover:bg-status-warning-50 dark:hover:bg-status-warning-900/30"
              >
                Revoke
              </button>
              <button
                @click="deleteKey(key.id)"
                class="inline-flex items-center px-3 py-1.5 border border-status-danger-300 dark:border-status-danger-600 shadow-sm text-xs font-medium rounded text-status-danger-700 dark:text-status-danger-300 bg-white dark:bg-gray-700 hover:bg-status-danger-50 dark:hover:bg-status-danger-900/30"
              >
                Delete
              </button>
            </div>
          </div>
        </li>
      </ul>
    </div>

    <!-- Create API Key Modal -->
    <div v-if="showCreateModal" class="fixed z-10 inset-0 overflow-y-auto">
      <div class="flex items-center justify-center min-h-screen pt-4 px-4 pb-20 text-center sm:p-0">
        <div class="fixed inset-0 bg-gray-500 dark:bg-gray-900 bg-opacity-75 dark:bg-opacity-75 transition-opacity" @click="showCreateModal = false"></div>

        <div class="inline-block align-bottom bg-white dark:bg-gray-800 rounded-lg text-left overflow-hidden shadow-xl transform transition-all sm:my-8 sm:align-middle sm:max-w-lg sm:w-full">
          <div class="bg-white dark:bg-gray-800 px-4 pt-5 pb-4 sm:p-6 sm:pb-4">
            <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-4">Create MCP API Key</h3>

            <div class="space-y-4">
              <div>
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Name</label>
                <input
                  v-model="newKey.name"
                  type="text"
                  class="mt-1 block w-full border border-gray-300 dark:border-gray-600 rounded-md shadow-sm py-2 px-3 focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 sm:text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500"
                  placeholder="My Claude Code Key"
                />
              </div>

              <div>
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Description (optional)</label>
                <textarea
                  v-model="newKey.description"
                  rows="2"
                  class="mt-1 block w-full border border-gray-300 dark:border-gray-600 rounded-md shadow-sm py-2 px-3 focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 sm:text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500"
                  placeholder="Used for..."
                ></textarea>
              </div>

              <!-- ent#163 / #2323. Admin-only, and deliberately an explicit
                   choice rather than a default: both non-standard scopes are
                   consequential (one acts as other people, one is a machine
                   credential), so neither should be a stray click.

                   #2389 — a radio group, not the two independent checkboxes this
                   started as: the scopes are mutually exclusive (one column), so
                   a checkbox pair can express a state the backend cannot store. -->
              <div v-if="isAdmin" class="space-y-2">
                <span class="block text-sm font-medium scope-label">Key scope</span>

                <label class="flex items-start gap-2 cursor-pointer">
                  <input type="radio" value="user" v-model="newKey.scope" class="mt-0.5 text-action-primary-600 focus:ring-action-primary-500" />
                  <span class="text-sm scope-label">
                    Standard
                    <span class="scope-hint">
                      Carries your own role in full, including admin. Use for your
                      own tooling.
                    </span>
                  </span>
                </label>

                <label class="flex items-start gap-2 cursor-pointer">
                  <input type="radio" value="ops" v-model="newKey.scope" class="mt-0.5 text-action-primary-600 focus:ring-action-primary-500" />
                  <span class="text-sm scope-label">
                    Ops (read-only)
                    <span class="scope-hint">
                      Bounded machine credential for a monitoring integration: fleet
                      health, telemetry, roster, execution and subscription
                      <em>reads</em> only — every write and every other endpoint is
                      refused, and it gets no MCP tools. It stays bound to this
                      account: if your admin role is removed or the account is
                      suspended, the key stops working, so mint it under an account
                      that is not offboarded with a person.
                    </span>
                  </span>
                </label>

                <label class="flex items-start gap-2 cursor-pointer">
                  <input type="radio" value="portal_delegate" v-model="newKey.scope" class="mt-0.5 text-action-primary-600 focus:ring-action-primary-500" />
                  <span class="text-sm scope-label">
                    Portal delegate
                    <span class="scope-hint">
                      Lets a trusted backend exchange one of your client emails for a
                      portal session — it acts as that person. It can do nothing else:
                      every other endpoint is refused. Revoke it to stop delegation.
                    </span>
                  </span>
                </label>
              </div>
            </div>
          </div>

          <div class="bg-gray-50 dark:bg-gray-900 px-4 py-3 sm:px-6 sm:flex sm:flex-row-reverse">
            <button
              @click="createKey"
              :disabled="creating || !newKey.name"
              class="w-full inline-flex justify-center rounded-md border border-transparent shadow-sm px-4 py-2 bg-action-primary-600 text-base font-medium text-white hover:bg-action-primary-700 focus:outline-none sm:ml-3 sm:w-auto sm:text-sm disabled:opacity-50"
            >
              {{ creating ? 'Creating...' : 'Create' }}
            </button>
            <button
              @click="showCreateModal = false"
              class="mt-3 w-full inline-flex justify-center rounded-md border border-gray-300 dark:border-gray-600 shadow-sm px-4 py-2 bg-white dark:bg-gray-700 text-base font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-600 focus:outline-none sm:mt-0 sm:ml-3 sm:w-auto sm:text-sm"
            >
              Cancel
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Show Created Key Modal -->
    <div v-if="showKeyModal" class="fixed z-10 inset-0 overflow-y-auto">
      <div class="flex items-center justify-center min-h-screen pt-4 px-4 pb-20 text-center sm:p-0">
        <div class="fixed inset-0 bg-gray-500 dark:bg-gray-900 bg-opacity-75 dark:bg-opacity-75 transition-opacity"></div>

        <div class="inline-block align-bottom bg-white dark:bg-gray-800 rounded-lg text-left overflow-hidden shadow-xl transform transition-all sm:my-8 sm:align-middle sm:max-w-2xl sm:w-full">
          <div class="bg-white dark:bg-gray-800 px-4 pt-5 pb-4 sm:p-6 sm:pb-4">
            <div class="flex items-center mb-4">
              <div class="flex-shrink-0 flex items-center justify-center h-12 w-12 rounded-full bg-status-success-100 dark:bg-status-success-900/50">
                <svg class="h-6 w-6 text-status-success-600 dark:text-status-success-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                </svg>
              </div>
              <h3 class="ml-4 text-lg font-medium text-gray-900 dark:text-white">Your MCP API Key is Ready!</h3>
            </div>

            <div class="bg-status-warning-50 dark:bg-status-warning-900/30 border border-status-warning-200 dark:border-status-warning-800 rounded-lg p-4 mb-4">
              <div class="flex">
                <svg class="h-5 w-5 text-status-warning-400 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
                  <path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd" />
                </svg>
                <div class="ml-3">
                  <p class="text-sm font-medium text-status-warning-800 dark:text-status-warning-300">
                    Copy the configuration below before closing - the key won't be shown again!
                  </p>
                </div>
              </div>
            </div>

            <div class="space-y-4">
              <div>
                <div class="flex items-center justify-between mb-1">
                  <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                    MCP Configuration
                    <span class="ml-2 text-xs text-gray-500 dark:text-gray-400">(add to your .mcp.json)</span>
                  </label>
                  <button
                    @click="copyMcpConfig"
                    class="inline-flex items-center px-3 py-1 text-xs font-medium rounded-md"
                    :class="copiedConfig
                      ? 'bg-status-success-100 dark:bg-status-success-900/50 text-status-success-700 dark:text-status-success-300'
                      : 'bg-action-primary-100 dark:bg-action-primary-900/50 text-action-primary-700 dark:text-action-primary-300 hover:bg-action-primary-200 dark:hover:bg-action-primary-900'"
                  >
                    <svg v-if="!copiedConfig" class="h-3.5 w-3.5 mr-1" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                    </svg>
                    <svg v-else class="h-3.5 w-3.5 mr-1" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                    </svg>
                    {{ copiedConfig ? 'Copied!' : 'Copy Config' }}
                  </button>
                </div>
                <pre class="bg-gray-900 dark:bg-gray-950 rounded-lg p-4 text-xs overflow-x-auto text-status-success-400 font-mono border border-gray-700">{{ getMcpConfig(createdApiKey) }}</pre>
              </div>

              <div>
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">API Key Only</label>
                <div class="flex items-center space-x-2">
                  <input
                    :type="showApiKey ? 'text' : 'password'"
                    :value="createdApiKey"
                    readonly
                    class="flex-1 block w-full border border-gray-300 dark:border-gray-600 rounded-md shadow-sm py-2 px-3 bg-gray-50 dark:bg-gray-700 font-mono text-sm focus:outline-none text-gray-900 dark:text-gray-100"
                  />
                  <button
                    @click="showApiKey = !showApiKey"
                    class="p-2 border border-gray-300 dark:border-gray-600 rounded-md hover:bg-gray-50 dark:hover:bg-gray-700"
                    title="Show/hide key"
                  >
                    <svg v-if="!showApiKey" class="h-5 w-5 text-gray-500 dark:text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                    </svg>
                    <svg v-else class="h-5 w-5 text-gray-500 dark:text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
                    </svg>
                  </button>
                  <button
                    @click="copyApiKey"
                    class="p-2 border border-gray-300 dark:border-gray-600 rounded-md hover:bg-gray-50 dark:hover:bg-gray-700"
                    title="Copy key"
                  >
                    <svg v-if="!copied" class="h-5 w-5 text-gray-500 dark:text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                    </svg>
                    <svg v-else class="h-5 w-5 text-status-success-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                    </svg>
                  </button>
                </div>
              </div>
            </div>
          </div>

          <div class="bg-gray-50 dark:bg-gray-900 px-4 py-3 sm:px-6">
            <button
              @click="closeKeyModal"
              class="w-full inline-flex justify-center rounded-md border border-transparent shadow-sm px-4 py-2 bg-action-primary-600 text-base font-medium text-white hover:bg-action-primary-700 focus:outline-none sm:text-sm"
            >
              I've copied the configuration
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
// MCP Keys settings tab — extracted from views/ApiKeys.vue (#302).
// Lives at /settings?tab=mcp-keys; the old /api-keys route now redirects
// here. Visible to ALL authenticated users (non-admin too); other tabs
// in Settings.vue are admin-only.

import { ref, reactive, onMounted, computed } from 'vue'
import axios from 'axios'
import ConfirmDialog from '../ConfirmDialog.vue'
import { useAuthStore } from '../../stores/auth'
import { useRole } from '../../composables/useRole'
import { copyToClipboard } from '../../utils/clipboard'

const authStore = useAuthStore()
const { isAdmin } = useRole()

const apiKeys = ref([])
const loading = ref(true)
const showCreateModal = ref(false)
const showKeyModal = ref(false)
const creating = ref(false)
const createdApiKey = ref('')
const showApiKey = ref(false)
const copied = ref(false)
const copiedConfig = ref(false)

// #2389 — one `scope` string, not a boolean per special scope: the column holds
// exactly one value, so independent booleans can express a state that cannot be
// stored. Non-admins never see the selector and keep the backend default.
const newKey = ref({
  name: '',
  description: '',
  scope: 'user'
})

const confirmDialog = reactive({
  visible: false,
  title: '',
  message: '',
  confirmText: 'Confirm',
  variant: 'danger',
  onConfirm: () => {}
})

// MCP server URL: use admin-configured URL if set, otherwise auto-detect
const configuredMcpUrl = ref(null)

const mcpServerUrl = computed(() => {
  if (configuredMcpUrl.value) {
    return configuredMcpUrl.value
  }
  const host = window.location.hostname
  if (host === 'localhost' || host === '127.0.0.1') {
    return 'http://localhost:8080/mcp'
  }
  return `http://${host}:8080/mcp`
})

const fetchMcpUrl = async () => {
  try {
    const response = await axios.get('/api/settings/mcp-url')
    configuredMcpUrl.value = response.data.url || null
  } catch (error) {
    console.error('Failed to fetch MCP URL setting:', error)
  }
}

// Filter out agent-scoped keys for non-admin users
const displayedKeys = computed(() => {
  if (isAdmin.value) {
    return apiKeys.value
  }
  return apiKeys.value.filter(k => k.scope !== 'agent')
})

const getMcpConfig = (apiKey) => {
  return JSON.stringify({
    mcpServers: {
      trinity: {
        type: "http",
        url: mcpServerUrl.value,
        headers: {
          Authorization: `Bearer ${apiKey}`
        }
      }
    }
  }, null, 2)
}

const fetchApiKeys = async () => {
  try {
    const response = await fetch('/api/mcp/keys', {
      headers: {
        'Authorization': `Bearer ${authStore.token}`
      }
    })
    if (response.ok) {
      apiKeys.value = await response.json()
    }
  } catch (error) {
    console.error('Failed to fetch API keys:', error)
  } finally {
    loading.value = false
  }
}

const ensureDefaultKey = async () => {
  try {
    const response = await fetch('/api/mcp/keys/ensure-default', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${authStore.token}`
      }
    })
    if (response.ok) {
      const data = await response.json()
      if (data && data.api_key) {
        createdApiKey.value = data.api_key
        showKeyModal.value = true
        await fetchApiKeys()
      }
    }
  } catch (error) {
    console.error('Failed to ensure default key:', error)
  }
}

const createKey = async () => {
  creating.value = true
  try {
    const response = await fetch('/api/mcp/keys', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${authStore.token}`
      },
      body: JSON.stringify({
        name: newKey.value.name,
        description: newKey.value.description || null,
        // Omitted (not `null`) for an ordinary key so the backend
        // default stands and nothing changes for existing callers.
        ...(newKey.value.scope && newKey.value.scope !== 'user'
          ? { scope: newKey.value.scope }
          : {})
      })
    })

    if (response.ok) {
      const data = await response.json()
      createdApiKey.value = data.api_key
      showCreateModal.value = false
      showKeyModal.value = true
      newKey.value = { name: '', description: '', scope: 'user' }
      await fetchApiKeys()
    } else {
      const error = await response.json()
      alert(`Failed to create API key: ${error.detail}`)
    }
  } catch (error) {
    console.error('Failed to create API key:', error)
    alert('Failed to create API key')
  } finally {
    creating.value = false
  }
}

const revokeKey = (keyId) => {
  confirmDialog.title = 'Revoke API Key'
  confirmDialog.message = 'Are you sure you want to revoke this API key? It will no longer work for authentication.'
  confirmDialog.confirmText = 'Revoke'
  confirmDialog.variant = 'warning'
  confirmDialog.onConfirm = async () => {
    try {
      const response = await fetch(`/api/mcp/keys/${keyId}/revoke`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${authStore.token}`
        }
      })

      if (response.ok) {
        await fetchApiKeys()
      }
    } catch (error) {
      console.error('Failed to revoke API key:', error)
    }
  }
  confirmDialog.visible = true
}

const deleteKey = (keyId) => {
  confirmDialog.title = 'Delete API Key'
  confirmDialog.message = 'Are you sure you want to permanently delete this API key?'
  confirmDialog.confirmText = 'Delete'
  confirmDialog.variant = 'danger'
  confirmDialog.onConfirm = async () => {
    try {
      const response = await fetch(`/api/mcp/keys/${keyId}`, {
        method: 'DELETE',
        headers: {
          'Authorization': `Bearer ${authStore.token}`
        }
      })

      if (response.ok) {
        await fetchApiKeys()
      }
    } catch (error) {
      console.error('Failed to delete API key:', error)
    }
  }
  confirmDialog.visible = true
}

const copyApiKey = async () => {
  const ok = await copyToClipboard(createdApiKey.value)
  if (!ok) {
    alert('Failed to copy API key. Please select the key text and copy it manually.')
    return
  }
  copied.value = true
  setTimeout(() => {
    copied.value = false
  }, 2000)
}

const copyMcpConfig = async () => {
  const ok = await copyToClipboard(getMcpConfig(createdApiKey.value))
  if (!ok) {
    alert('Failed to copy config. Please select the config text and copy it manually.')
    return
  }
  copiedConfig.value = true
  setTimeout(() => {
    copiedConfig.value = false
  }, 2000)
}

const closeKeyModal = () => {
  showKeyModal.value = false
  createdApiKey.value = ''
  showApiKey.value = false
  copied.value = false
  copiedConfig.value = false
}

const formatDate = (dateString) => {
  if (!dateString) return 'Never'
  const date = new Date(dateString)
  return date.toLocaleDateString() + ' ' + date.toLocaleTimeString()
}

onMounted(async () => {
  await Promise.all([fetchMcpUrl(), fetchApiKeys()])
  await ensureDefaultKey()
})
</script>

<style scoped>
/* #2389 — the three scope options share one chrome style instead of repeating
   the gray palette classes per option. There is no semantic neutral token
   family (`status-/state-/brand-/accent-/action-` are the five), so gray IS the
   sanctioned neutral here — it is simply counted, and the raw-color ratchet
   (`scripts/scan-raw-colors.mjs` against `raw-color-baseline.json`) may only
   shrink per file. Hoisting keeps this file's count flat while adding a
   three-option control. */
.scope-label { @apply text-gray-700 dark:text-gray-300; }
.scope-hint { @apply block text-xs text-gray-500 dark:text-gray-400; }
</style>
