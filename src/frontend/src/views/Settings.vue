<template>
  <div class="min-h-screen bg-gray-100 dark:bg-gray-900">
    <NavBar />

    <!-- #1862: `max-w-4xl` (896px) was too narrow for this view's own tab
         strip — the ten tabs measure ~925px, so `main` overflowed by ~61px
         and scrolled horizontally at any desktop width. Settings was also
         the ONLY top-level view on 4xl; Agents, Operations, Templates and
         ExecutionDetail all use 7xl. Matching them fixes the overflow and
         removes the odd one out rather than inventing a bespoke width.
         `max-w-*` is a ceiling, so narrow viewports are unaffected. -->
    <main class="max-w-7xl mx-auto py-6 sm:px-6 lg:px-8">
      <div class="px-4 py-6 sm:px-0">
        <div class="mb-8">
          <h1 class="text-3xl font-bold text-gray-900 dark:text-white">Settings</h1>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
            System-wide configuration for the Trinity platform
          </p>
        </div>

        <!-- Tab strip (#302) -->
        <div class="mb-6 border-b border-gray-200 dark:border-gray-700" role="tablist" aria-label="Settings sections">
          <nav class="-mb-px flex space-x-6" aria-label="Tabs">
            <button
              v-for="tab in visibleTabs"
              :key="tab.id"
              role="tab"
              :aria-selected="activeTab === tab.id"
              :class="[
                'whitespace-nowrap py-2 px-1 border-b-2 text-sm font-medium',
                activeTab === tab.id
                  ? 'border-action-primary-500 text-action-primary-600 dark:text-action-primary-400'
                  : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300 dark:text-gray-400 dark:hover:text-gray-200'
              ]"
              type="button"
              @click="selectTab(tab.id)"
            >{{ tab.label }}</button>
          </nav>
        </div>

        <!-- Loading State -->
        <div v-if="loading" class="bg-white dark:bg-gray-800 rounded-lg shadow dark:shadow-gray-900 p-8 text-center">
          <div class="animate-spin rounded-full h-12 w-12 border-b-2 border-action-primary-600 mx-auto"></div>
          <p class="mt-4 text-gray-500 dark:text-gray-400">Loading settings...</p>
        </div>

        <!-- Settings Content -->
        <div v-else class="space-y-6">
          <!-- MCP Keys Tab Content (extracted to component, #302) -->
          <template v-if="activeTab === 'mcp-keys'">
            <McpKeysTab />
            <!-- ent#162: personal GitHub token lives with the user's other
                 personal credentials on this non-admin tab. -->
            <div class="mt-6">
              <UserGitHubPatPanel />
            </div>
          </template>

          <!-- ent#84 — Fleet-wide agent-to-agent permissions matrix -->
          <div v-if="activeTab === 'agent-permissions'" class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
            <AgentPermissionsMatrix />
          </div>

          <!-- #5 — Security / Two-Factor (enterprise, gated by `2fa`) -->
          <TwoFactorPanel v-if="activeTab === 'security'" />

          <!-- #32 — Single Sign-On (enterprise, gated by `sso`) -->
          <SsoPanel v-if="activeTab === 'sso'" />

          <!-- Retention Tab Content (#1039) -->
          <div v-if="activeTab === 'retention'" class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
            <div class="px-6 py-5">
              <div class="flex items-center justify-between">
                <div>
                  <h3 class="text-lg font-medium text-gray-900 dark:text-gray-100">Data Retention</h3>
                  <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                    How long Trinity keeps logs, executions, health checks, and soft-deleted agents/schedules.
                  </p>
                </div>
                <span
                  v-if="retention"
                  class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium"
                  :class="retention.edition === 'enterprise'
                    ? 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900 dark:text-indigo-200'
                    : 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-200'"
                >{{ retention.edition === 'enterprise' ? 'Enterprise' : 'Community' }}</span>
              </div>

              <div v-if="retentionLoading" class="mt-4 text-sm text-gray-500 dark:text-gray-400">Loading…</div>
              <div v-else-if="retentionError" class="mt-4 text-sm text-red-600 dark:text-red-400">{{ retentionError }}</div>

              <div v-else-if="retention" class="mt-5 space-y-4">
                <!-- #1709: prunes a cleanup cycle would REFUSE right now, awaiting
                     an admin's explicit approval. Irreversible (destroys volumes),
                     so we name exactly what will be deleted before offering approve. -->
                <div v-if="(retention.pending_acknowledgements || []).length" class="rounded-md bg-amber-50 dark:bg-amber-900/30 border border-amber-300 dark:border-amber-700 p-4">
                  <h4 class="text-sm font-semibold text-amber-900 dark:text-amber-200">⚠ Deletion awaiting your approval</h4>
                  <p class="mt-1 text-xs text-amber-800 dark:text-amber-300">
                    A cleanup cycle wants to permanently delete more than the safety threshold at once. Nothing is deleted until you approve — and approving is <strong>irreversible</strong>. Each approval is single-use.
                  </p>
                  <ul class="mt-3 space-y-3">
                    <li v-for="p in retention.pending_acknowledgements" :key="p.key" class="rounded-md bg-white dark:bg-gray-800 border border-amber-200 dark:border-amber-800 p-3">
                      <p class="text-sm text-gray-900 dark:text-gray-100">{{ p.label }}</p>
                      <p class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
                        <strong>{{ p.candidate_count }}</strong> item{{ p.candidate_count === 1 ? '' : 's' }} past the <strong>{{ p.window_days }}-day</strong> window
                        (<code class="text-[11px]">{{ p.key }}</code>).
                      </p>
                      <div class="mt-2 flex items-center gap-3">
                        <button
                          @click="acknowledgePrune(p)" :disabled="acknowledgingKey === p.key"
                          class="inline-flex items-center px-3 py-1.5 text-xs font-medium rounded-md text-white bg-red-600 hover:bg-red-700 disabled:opacity-50"
                        >{{ acknowledgingKey === p.key ? 'Approving…' : 'Approve deletion' }}</button>
                        <span v-if="ackErrorKey === p.key && ackError" class="text-xs text-red-600 dark:text-red-400">{{ ackError }}</span>
                      </div>
                    </li>
                  </ul>
                </div>
                <!-- Honest empty state (AC #1709) -->
                <div v-else class="rounded-md bg-gray-50 dark:bg-gray-900/40 border border-gray-200 dark:border-gray-700 p-3">
                  <p class="text-xs text-gray-500 dark:text-gray-400">
                    ✓ No deletions are awaiting approval. If a cleanup cycle ever needs to delete more than the safety threshold at once (e.g. several soft-deleted agents reaching their purge date — which destroys their data volumes), it pauses and asks here first.
                  </p>
                </div>
                <p v-if="ackDone" class="text-xs text-green-600 dark:text-green-400">Approved — the next cleanup cycle will delete these, then the guard re-arms (single-use).</p>

                <!-- Community: read-only fixed floor + upgrade hint -->
                <div v-if="!retentionEntitled" class="rounded-md bg-indigo-50 dark:bg-indigo-900/30 border border-indigo-200 dark:border-indigo-800 p-4">
                  <p class="text-sm text-indigo-800 dark:text-indigo-200">
                    The community edition keeps a fixed
                    <strong>{{ retention.community_floor_days }}-day</strong> retention floor.
                    An enterprise license unlocks configurable, longer windows — set per class, applied live with no restart.
                  </p>
                </div>

                <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <div v-for="f in RETENTION_FIELDS" :key="f.key">
                    <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">{{ f.label }}</label>
                    <div class="mt-1 flex items-center gap-2">
                      <input
                        type="number" min="0" max="3650"
                        v-model.number="retentionForm[f.key]"
                        :disabled="!retentionEntitled || retentionSaving"
                        :class="RETENTION_INPUT_CLASS"
                      />
                      <span class="text-sm text-gray-500 dark:text-gray-400">days</span>
                    </div>
                  </div>
                  <!-- Audit log — always shown, never editable (integrity floor) -->
                  <div>
                    <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Audit log</label>
                    <div class="mt-1 flex items-center gap-2">
                      <input type="number" :value="retention.windows.audit_log_retention_days" disabled
                        :class="RETENTION_INPUT_CLASS" />
                      <span class="text-xs text-gray-400">days (365-day integrity floor)</span>
                    </div>
                  </div>
                </div>

                <div v-if="retentionEntitled" class="flex items-center gap-3 pt-2">
                  <button
                    @click="saveRetention" :disabled="retentionSaving"
                    class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50"
                  >{{ retentionSaving ? 'Saving…' : 'Save retention' }}</button>
                  <span v-if="retentionSaved" class="text-sm text-green-600 dark:text-green-400">Saved — applied live.</span>
                  <span class="text-xs text-gray-400">0 disables a sweep · values below the {{ retention.community_floor_days }}-day floor are raised to it.</span>
                </div>

              </div>
            </div>
          </div>

          <!-- ent#184 — Activation funnel (local product events). Capture is
               OSS-core; this operator view is entitlement-gated (`telemetry`).
               The panel fetches the gated enterprise endpoint itself. -->
          <ActivationFunnelPanel v-if="activeTab === 'activation'" />

          <!-- ent#12 — Tier-2 opt-in usage sharing. OSS-core, default-off,
               reversible. Admin-only (General tab), visible in every edition. -->
          <div v-if="activeTab === 'general'" class="mb-6">
            <TelemetrySharingPanel />
          </div>

          <!-- Workspace session policy (ent#375). On the Retention tab because it
               is the same question that tab already answers — how long something
               is kept — and an operator reasoning about client data lifetime is
               the one who wants to know how long a client stays signed in. Shown
               in EVERY edition: the sliding session is OSS and enforcing here
               regardless, so hiding it would hide a live security control from
               the operator it applies to. Only the inputs are entitled. -->
          <div v-if="activeTab === 'retention'" class="mb-6">
            <PortalSessionPolicyPanel />
          </div>

          <!-- Room budgets (ent#387) — beside the session policy for the same
               reason: both bound what a client engagement consumes, and this is
               the only surface that sets them since ent#381 retired the Sessions
               page. Self-hiding when the rooms module is not entitled. -->
          <div v-if="activeTab === 'retention'" class="mb-6">
            <RoomBudgetDefaultsPanel />
          </div>

          <!-- Platform Section -->
          <!-- Admin sign-in email (#82 Phase 1) — lets an existing admin bind a
               real email so they can sign in with email + password, matching
               what a fresh install captures at first-run setup. -->
          <div v-if="activeTab === 'general'" class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg mb-6">
            <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <h2 class="text-lg font-medium text-gray-900 dark:text-white">Admin sign-in email</h2>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                Sign in with this email and your password instead of the <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs">admin</code> username. No verification email is sent.
              </p>
            </div>
            <div class="px-6 py-4">
              <label for="admin-email" class="block text-sm font-medium text-gray-700 dark:text-gray-300">Email</label>
              <div class="mt-1 flex gap-2">
                <input
                  type="email"
                  id="admin-email"
                  v-model="adminEmailInput"
                  :placeholder="adminEmailCurrent || 'you@company.com'"
                  :disabled="savingAdminEmail"
                  class="block flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white text-sm"
                />
                <button
                  @click="saveAdminEmail"
                  :disabled="!adminEmailInput || savingAdminEmail"
                  class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  <svg v-if="savingAdminEmail" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                  </svg>
                  Save
                </button>
              </div>
              <div class="mt-2 flex items-center text-sm">
                <template v-if="adminEmailSaveSuccess">
                  <svg class="h-4 w-4 text-status-success-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                  </svg>
                  <span class="text-status-success-600 dark:text-status-success-400">Saved — you can now sign in with this email</span>
                </template>
                <template v-else-if="adminEmailError">
                  <span class="text-status-danger-600 dark:text-status-danger-400">{{ adminEmailError }}</span>
                </template>
                <template v-else-if="adminEmailCurrent">
                  <span class="text-gray-500 dark:text-gray-400">Current: {{ adminEmailCurrent }}</span>
                </template>
                <template v-else>
                  <span class="text-state-autonomous-600 dark:text-state-autonomous-400">No email set — you currently sign in as <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs">admin</code></span>
                </template>
              </div>
            </div>
          </div>

          <div v-if="activeTab === 'general'" class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
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
                      class="block flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white text-sm"
                    />
                    <button
                      @click="savePublicUrl"
                      :disabled="!publicUrl || savingPublicUrl"
                      class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
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
                      <svg class="h-4 w-4 text-status-success-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                      </svg>
                      <span class="text-status-success-600 dark:text-status-success-400">Saved</span>
                    </template>
                    <template v-else-if="publicUrlCurrent">
                      <svg class="h-4 w-4 text-status-success-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                      </svg>
                      <span class="text-status-success-600 dark:text-status-success-400">
                        {{ publicUrlCurrent }}
                      </span>
                    </template>
                    <template v-else>
                      <svg class="h-4 w-4 text-state-autonomous-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                      </svg>
                      <span class="text-state-autonomous-600 dark:text-state-autonomous-400">
                        Not configured — required for Telegram bots and public links
                      </span>
                    </template>
                  </div>
                  <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">
                    The externally-accessible URL of this Trinity instance (e.g. <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs">https://your-domain.com</code>).
                    Used for Telegram webhooks, Slack OAuth callbacks, and shareable public links.
                  </p>
                </div>

                <!-- Platform Default Model (#831) -->
                <div v-if="isAdmin">
                  <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                    Default Model
                  </label>
                  <div class="mt-1 flex gap-2 items-center">
                    <select
                      v-model="platformDefaultModelValue"
                      :disabled="savingPlatformDefaultModel"
                      class="block flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white text-sm"
                    >
                      <!-- Options derive from the single source of truth
                           (src/constants/modelCatalog.js, generated from
                           services/model_catalog.py, #2086). "(recommended)" is
                           pinned to the catalog `recommended` flag == the
                           platform default (#831), NOT the loaded value. -->
                      <option
                        v-for="m in adminDefaultModels"
                        :key="m.id"
                        :value="m.id"
                      >{{ m.label }} — {{ m.note }}{{ m.recommended ? ' (recommended)' : '' }}</option>
                    </select>
                    <button
                      @click="savePlatformDefaultModel"
                      :disabled="savingPlatformDefaultModel"
                      class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <svg v-if="savingPlatformDefaultModel" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                      </svg>
                      Save
                    </button>
                  </div>
                  <div v-if="platformDefaultModelSaveSuccess" class="mt-1 flex items-center text-sm text-status-success-600 dark:text-status-success-400">
                    <svg class="h-4 w-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                    </svg>
                    Saved
                  </div>
                  <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">
                    Model used for schedules and chats where no model is explicitly selected.
                    Changes take effect on the next execution — no restart required.
                  </p>
                </div>

                <!-- Default Access Policy (#1129) — secure-by-default require_email -->
                <div v-if="isAdmin" class="mt-6 pt-6 border-t border-gray-200 dark:border-gray-700">
                  <label class="flex items-center justify-between cursor-pointer">
                    <span class="text-sm font-medium text-gray-700 dark:text-gray-300">
                      Require verified email for new agents
                    </span>
                    <input
                      type="checkbox"
                      v-model="defaultRequireEmail"
                      :disabled="savingDefaultAccessPolicy"
                      @change="saveDefaultAccessPolicy"
                      class="h-4 w-4 text-action-primary-600 border-gray-300 dark:border-gray-600 rounded focus:ring-action-primary-500 disabled:opacity-50"
                    />
                  </label>
                  <div v-if="defaultAccessPolicySaveSuccess" class="mt-1 flex items-center text-sm text-status-success-600 dark:text-status-success-400">
                    <svg class="h-4 w-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                    </svg>
                    Saved
                  </div>
                  <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">
                    Secure-by-default. When on, newly created agents require a verified email on
                    incoming DMs / public chat / shared access. Applies to <strong>new agents
                    only</strong> — existing agents keep their current setting, and owners can
                    override per agent in the agent's Sharing tab.
                  </p>
                </div>

                <!-- Fleet Capacity (#506) — admin-set ceiling on per-agent max_parallel_tasks -->
                <div v-if="isAdmin" class="mt-6 pt-6 border-t border-gray-200 dark:border-gray-700">
                  <label for="max-parallel-ceiling" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                    Fleet capacity ceiling
                  </label>
                  <div class="mt-1 flex gap-2 items-center">
                    <input
                      id="max-parallel-ceiling"
                      type="number"
                      v-model.number="maxParallelTasksCeiling"
                      :min="ceilingMin"
                      :max="ceilingMax"
                      :disabled="savingCeiling"
                      class="block w-32 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white text-sm"
                    />
                    <button
                      @click="saveMaxParallelTasksCeiling"
                      :disabled="savingCeiling"
                      class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      Save
                    </button>
                  </div>
                  <div v-if="ceilingSaveSuccess" class="mt-1 flex items-center text-sm text-status-success-600 dark:text-status-success-400">
                    <svg class="h-4 w-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                    </svg>
                    Saved
                  </div>
                  <p v-if="ceilingError" class="mt-1 text-sm text-status-danger-600 dark:text-status-danger-400">
                    {{ ceilingError }}
                  </p>
                  <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">
                    Maximum parallel tasks any single agent is allowed to consume on this host
                    ({{ ceilingMin }}–{{ ceilingMax }}). Owners pick a per-agent value within this
                    ceiling; existing agents above it are clamped at runtime.
                  </p>
                </div>

                <!-- Proactive message limits (#1609) — admin-tunable per-hour caps on agent-INITIATED channel sends -->
                <div v-if="isAdmin" class="mt-6 pt-6 border-t border-gray-200 dark:border-gray-700">
                  <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300">Proactive message limits</h3>
                  <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                    Per-hour caps on messages an agent <span class="font-medium">initiates</span> to Slack, Telegram, and direct messages (anti-spam).
                    Replies to inbound messages are never limited by these. Set <span class="font-medium">0</span> to disable a cap (unlimited).
                  </p>
                  <div class="mt-3 space-y-2.5">
                    <div v-for="row in PROACTIVE_ROWS" :key="row.key" class="flex items-center gap-3">
                      <input
                        type="number"
                        min="0"
                        :max="proactiveMax"
                        v-model.number="proactiveLimits[row.key]"
                        :disabled="savingProactive"
                        class="block w-28 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white text-sm"
                      />
                      <div class="min-w-0">
                        <div class="text-sm text-gray-700 dark:text-gray-300">{{ row.label }}</div>
                        <div class="text-xs text-gray-400">{{ row.hint }}</div>
                      </div>
                    </div>
                  </div>
                  <div class="mt-3 flex items-center gap-3">
                    <button
                      @click="saveProactiveLimits"
                      :disabled="savingProactive"
                      class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
                    >Save</button>
                    <span v-if="proactiveSaveSuccess" class="inline-flex items-center text-sm text-status-success-600 dark:text-status-success-400">
                      <svg class="h-4 w-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" /></svg>
                      Saved
                    </span>
                  </div>
                  <p v-for="w in proactiveWarnings" :key="w" class="mt-1 text-xs text-status-warning-600 dark:text-status-warning-400">⚠ {{ w }}</p>
                  <p v-if="proactiveError" class="mt-1 text-sm text-status-danger-600 dark:text-status-danger-400">{{ proactiveError }}</p>
                </div>

                <!-- Brain Orb platform flags (trinity-enterprise#85) -->
                <div v-if="isAdmin" class="mt-6 pt-6 border-t border-gray-200 dark:border-gray-700">
                  <h3 class="text-sm font-medium text-gray-900 dark:text-white">Brain Orb</h3>
                  <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                    Per-agent 3D knowledge-graph surface for agents with the
                    <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs">brain-orb</code>
                    capability. Changes apply immediately — no restart; users with open
                    sessions pick them up on the next page load.
                  </p>
                  <div class="mt-3 space-y-3">
                    <div v-for="flag in brainOrbFlagRows" :key="flag.key">
                      <label class="flex items-center justify-between cursor-pointer">
                        <span class="text-sm font-medium text-gray-700 dark:text-gray-300">
                          {{ flag.label }}
                          <span
                            v-if="brainOrb[flag.key].source === 'override'"
                            class="ml-2 inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-action-primary-100 text-action-primary-800 dark:bg-action-primary-900 dark:text-action-primary-200"
                            title="A stored setting overrides the environment variable"
                          >override</span>
                          <span
                            v-else-if="brainOrb[flag.key].source === 'env'"
                            class="ml-2 inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300"
                            title="Enabled by the environment variable; no stored override"
                          >env</span>
                        </span>
                        <input
                          type="checkbox"
                          v-model="brainOrb[flag.key].value"
                          :disabled="savingBrainOrb"
                          @change="saveBrainOrbFlag(flag.key, brainOrb[flag.key].value)"
                          class="h-4 w-4 text-action-primary-600 border-gray-300 dark:border-gray-600 rounded focus:ring-action-primary-500 disabled:opacity-50"
                        />
                      </label>
                      <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                        {{ flag.hint }}
                        <template v-if="flag.key === 'voice_enabled' && !brainOrbGeminiKey">
                          <span class="text-state-autonomous-600 dark:text-state-autonomous-400">
                            GEMINI_API_KEY is not configured (env-only) — voice stays unavailable even when on.
                          </span>
                        </template>
                        <button
                          v-if="brainOrb[flag.key].source === 'override'"
                          @click="clearBrainOrbFlag(flag.key)"
                          :disabled="savingBrainOrb"
                          class="ml-1 text-action-primary-600 dark:text-action-primary-400 hover:underline disabled:opacity-50"
                        >Reset to env/default</button>
                      </p>
                    </div>
                  </div>
                  <div v-if="brainOrbSaveSuccess" class="mt-2 flex items-center text-sm text-status-success-600 dark:text-status-success-400">
                    <svg class="h-4 w-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                    </svg>
                    Saved
                  </div>
                  <p v-if="brainOrbError" class="mt-2 text-sm text-status-danger-600 dark:text-status-danger-400">
                    {{ brainOrbError }}
                  </p>
                </div>

                <!-- ElevenLabs / Voice platform settings (trinity-enterprise#117) -->
                <div v-if="isAdmin" class="mt-6 pt-6 border-t border-gray-200 dark:border-gray-700">
                  <h3 class="text-sm font-medium text-gray-900 dark:text-white">Voice (ElevenLabs)</h3>
                  <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                    API key + default voice for outbound voice replies. Agents with voice enabled can
                    reply with a spoken voice note on messaging channels. Changes apply immediately — no restart.
                  </p>

                  <!-- API key -->
                  <div class="mt-3">
                    <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      ElevenLabs API key
                      <span
                        v-if="elevenLabs.keyConfigured"
                        class="ml-2 inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-status-success-100 text-status-success-800 dark:bg-status-success-900 dark:text-status-success-200"
                      >{{ elevenLabs.keySource === 'env' ? 'configured (env)' : 'configured' }}</span>
                      <span
                        v-else
                        class="ml-2 inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300"
                      >not set</span>
                    </label>
                    <div class="flex gap-2">
                      <input
                        v-model="elevenLabs.apiKeyInput"
                        type="password"
                        :placeholder="elevenLabs.keyConfigured ? 'Enter a new key to replace' : 'Paste your ElevenLabs API key'"
                        :disabled="savingElevenLabs"
                        class="flex-1 text-sm border border-gray-300 dark:border-gray-600 rounded-md px-3 py-1.5 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-action-primary-500 disabled:opacity-50"
                      />
                      <button
                        type="button"
                        @click="saveElevenLabsKey"
                        :disabled="savingElevenLabs || !elevenLabs.apiKeyInput.trim()"
                        class="px-3 py-1.5 text-sm font-medium rounded-md text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50"
                      >Save</button>
                      <button
                        v-if="elevenLabs.keyConfigured && elevenLabs.keySource === 'override'"
                        type="button"
                        @click="clearElevenLabsKey"
                        :disabled="savingElevenLabs"
                        class="px-3 py-1.5 text-sm font-medium rounded-md border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50"
                      >Clear</button>
                    </div>
                  </div>

                  <!-- Default voice id -->
                  <div class="mt-3">
                    <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Default voice ID</label>
                    <div class="flex gap-2">
                      <input
                        v-model="elevenLabs.defaultVoiceId"
                        type="text"
                        placeholder="e.g. 21m00Tcm4TlvDq8ikWAM"
                        :disabled="savingElevenLabs"
                        class="flex-1 text-sm border border-gray-300 dark:border-gray-600 rounded-md px-3 py-1.5 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-action-primary-500 disabled:opacity-50"
                      />
                      <button
                        type="button"
                        @click="saveElevenLabsDefaultVoice"
                        :disabled="savingElevenLabs"
                        class="px-3 py-1.5 text-sm font-medium rounded-md text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50"
                      >Save</button>
                    </div>
                    <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                      Agents without their own voice ID fall back to this one.
                    </p>
                  </div>

                  <div v-if="elevenLabsSaveSuccess" class="mt-2 flex items-center text-sm text-status-success-600 dark:text-status-success-400">
                    <svg class="h-4 w-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                    </svg>
                    Saved
                  </div>
                  <p v-if="elevenLabsError" class="mt-2 text-sm text-status-danger-600 dark:text-status-danger-400">
                    {{ elevenLabsError }}
                  </p>
                </div>
              </div>
            </div>
          </div>

          <!-- API Keys Section -->
          <div v-if="activeTab === 'integrations'" class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
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
                        class="block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white font-mono text-sm"
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
                      class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <svg v-if="savingApiKey" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                      </svg>
                      Save
                    </button>
                    <button
                      v-if="anthropicKeyStatus.configured && anthropicKeyStatus.source === 'settings'"
                      @click="removeAnthropicKey"
                      :disabled="removingApiKey"
                      class="inline-flex items-center px-4 py-2 border border-status-danger-300 dark:border-status-danger-700 rounded-md shadow-sm text-sm font-medium text-status-danger-700 dark:text-status-danger-300 bg-white dark:bg-gray-700 hover:bg-status-danger-50 dark:hover:bg-status-danger-900/30 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <svg v-if="removingApiKey" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                      </svg>
                      Remove
                    </button>
                  </div>
                  <!-- Status/Result -->
                  <div class="mt-2 flex items-center text-sm">
                    <template v-if="apiKeyTestResult !== null">
                      <svg v-if="apiKeyTestResult" class="h-4 w-4 text-status-success-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                      </svg>
                      <svg v-else class="h-4 w-4 text-status-danger-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                      </svg>
                      <span :class="apiKeyTestResult ? 'text-status-success-600 dark:text-status-success-400' : 'text-status-danger-600 dark:text-status-danger-400'">
                        {{ apiKeyTestMessage }}
                      </span>
                    </template>
                    <template v-else-if="anthropicKeyStatus.configured">
                      <svg class="h-4 w-4 text-status-success-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                      </svg>
                      <span class="text-status-success-600 dark:text-status-success-400">
                        Configured
                        <span class="text-gray-500 dark:text-gray-400">
                          ({{ anthropicKeyStatus.source === 'settings' ? 'from settings' : 'from environment' }})
                        </span>
                      </span>
                    </template>
                    <template v-else>
                      <svg class="h-4 w-4 text-state-autonomous-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                      </svg>
                      <span class="text-state-autonomous-600 dark:text-state-autonomous-400">
                        Not configured - required for agents
                      </span>
                    </template>
                  </div>
                  <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">
                    Required for agents to use Claude. Get your key at
                    <a href="https://console.anthropic.com" target="_blank" class="text-action-primary-600 dark:text-action-primary-400 hover:underline">
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
                        class="block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white font-mono text-sm"
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
                      class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <svg v-if="savingGithubPat" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                      </svg>
                      Save
                    </button>
                    <button
                      v-if="githubPatStatus.configured && githubPatStatus.source === 'settings'"
                      @click="removeGithubPat"
                      :disabled="removingGithubPat"
                      class="inline-flex items-center px-4 py-2 border border-status-danger-300 dark:border-status-danger-700 rounded-md shadow-sm text-sm font-medium text-status-danger-700 dark:text-status-danger-300 bg-white dark:bg-gray-700 hover:bg-status-danger-50 dark:hover:bg-status-danger-900/30 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <svg v-if="removingGithubPat" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                      </svg>
                      Remove
                    </button>
                  </div>
                  <!-- Status/Result -->
                  <div class="mt-2 flex items-center text-sm">
                    <template v-if="githubPatTestResult !== null">
                      <svg v-if="githubPatTestResult" class="h-4 w-4 text-status-success-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                      </svg>
                      <svg v-else class="h-4 w-4 text-status-danger-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                      </svg>
                      <span :class="githubPatTestResult ? 'text-status-success-600 dark:text-status-success-400' : 'text-status-danger-600 dark:text-status-danger-400'">
                        {{ githubPatTestMessage }}
                      </span>
                    </template>
                    <template v-else-if="githubPatStatus.configured">
                      <svg class="h-4 w-4 text-status-success-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                      </svg>
                      <span class="text-status-success-600 dark:text-status-success-400">
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
                      <div class="text-status-danger-600 dark:text-status-danger-400">
                        PAT saved, but propagation failed: {{ githubPatPropagation.error }}
                      </div>
                    </template>
                    <template v-else-if="githubPatPropagation.total_running === 0">
                      <div class="text-gray-600 dark:text-gray-400">
                        PAT updated. No running agents to propagate to.
                      </div>
                    </template>
                    <template v-else>
                      <!-- #1967: "0 of N" used to render success-green whenever
                           nothing outright failed, so a rotation that reached
                           no agent at all looked identical to one that worked.
                           Reaching nothing is the loudest state here, not the
                           quietest. -->
                      <div v-if="!githubPatPropagation.updated.length" class="text-status-danger-600 dark:text-status-danger-400">
                        PAT saved, but applied to <strong>0 of {{ githubPatPropagation.total_running }}</strong> running agent{{ githubPatPropagation.total_running === 1 ? '' : 's' }} — they keep using the previous token until restarted.
                      </div>
                      <div v-else :class="githubPatPropagation.failed.length ? 'text-status-warning-700 dark:text-status-warning-400' : 'text-status-success-600 dark:text-status-success-400'">
                        PAT updated and applied to {{ githubPatPropagation.updated.length }} of {{ githubPatPropagation.total_running }} running agent{{ githubPatPropagation.total_running === 1 ? '' : 's' }}.
                      </div>
                      <!-- The .env write only lands on the next restart; the
                           remote rewrite is what makes git work NOW. Surfaced
                           because "applied" without it is the silent half of
                           this bug. -->
                      <div
                        v-if="githubPatPropagation.updated.length && (githubPatPropagation.remotes_updated ?? 0) < githubPatPropagation.updated.length"
                        class="mt-1 text-status-warning-700 dark:text-status-warning-400"
                      >
                        Git remotes re-templated on {{ githubPatPropagation.remotes_updated ?? 0 }} of {{ githubPatPropagation.updated.length }} — the rest pick the token up for git on their next restart.
                      </div>
                      <div v-if="githubPatPropagation.failed.length" class="mt-1 text-status-danger-600 dark:text-status-danger-400">
                        Failed: {{ githubPatPropagation.failed.map(a => a.agent_name).join(', ') }}
                      </div>
                      <div v-if="githubPatPropagation.skipped.length" class="mt-1 text-gray-500 dark:text-gray-400">
                        Skipped: {{ githubPatPropagation.skipped.map(a => `${a.agent_name} (${a.status === 'skipped_per_agent_pat' ? 'per-agent PAT' : 'no GITHUB_PAT'})`).join(', ') }}
                      </div>
                    </template>
                  </div>
                  <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">
                    Required for creating and pushing agents to GitHub repositories. Get your token at
                    <a href="https://github.com/settings/tokens/new" target="_blank" class="text-action-primary-600 dark:text-action-primary-400 hover:underline">
                      github.com/settings/tokens
                    </a>
                    with <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs">repo</code> scope.
                  </p>
                </div>
              </div>
            </div>
          </div>

          <!-- Slack Integration Section (SLACK-001/002) -->
          <div v-if="activeTab === 'integrations'" class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
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
                    :class="slackTransportStatus.connected ? 'bg-status-success-500' : 'bg-status-danger-500'"
                  ></span>
                  <span class="text-sm" :class="slackTransportStatus.connected ? 'text-status-success-700 dark:text-status-success-400' : 'text-gray-500 dark:text-gray-400'">
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
                      class="block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white font-mono text-sm"
                    />
                  </div>
                  <div v-if="slackSettings.client_id?.configured" class="mt-1 text-xs text-status-success-600 dark:text-status-success-400">
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
                      class="block w-full px-3 py-2 pr-10 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white font-mono text-sm"
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
                  <div v-if="slackSettings.client_secret?.configured" class="mt-1 text-xs text-status-success-600 dark:text-status-success-400">
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
                      class="block w-full px-3 py-2 pr-10 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white font-mono text-sm"
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
                  <div v-if="slackSettings.signing_secret?.configured" class="mt-1 text-xs text-status-success-600 dark:text-status-success-400">
                    ✓ Configured ({{ slackSettings.signing_secret.source === 'settings' ? 'from settings' : 'from environment' }})
                  </div>
                </div>

                <!-- Save Credentials Button -->
                <div class="flex items-center gap-3">
                  <button
                    @click="saveSlackSettings"
                    :disabled="(!slackClientId && !slackClientSecret && !slackSigningSecret) || savingSlackSettings"
                    class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <svg v-if="savingSlackSettings" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    Save Credentials
                  </button>
                  <button
                    v-if="slackHasStoredCredentials"
                    @click="removeSlackSettings"
                    :disabled="removingSlackSettings"
                    class="inline-flex items-center px-4 py-2 border border-status-danger-300 dark:border-status-danger-700 rounded-md shadow-sm text-sm font-medium text-status-danger-700 dark:text-status-danger-300 bg-white dark:bg-gray-700 hover:bg-status-danger-50 dark:hover:bg-status-danger-900/30 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <svg v-if="removingSlackSettings" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    Remove Credentials
                  </button>
                  <span v-if="slackSaveSuccess" class="text-sm text-status-success-600 dark:text-status-success-400">
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
                      class="block w-full px-3 py-2 pr-10 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white font-mono text-sm"
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
                  <div v-if="slackTransportStatus.app_token_configured" class="mt-1 text-xs text-status-success-600 dark:text-status-success-400">
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
                    class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-status-success-600 hover:bg-status-success-700 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <svg v-if="connectingSlack" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    {{ connectingSlack ? 'Connecting...' : 'Connect' }}
                  </button>
                  <span v-if="slackTransportStatus.connected" class="text-sm text-status-success-600 dark:text-status-success-400">
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
                  <span v-if="slackInstallSuccess" class="text-sm text-status-success-600 dark:text-status-success-400">
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
                          class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-action-primary-100 text-action-primary-800 dark:bg-action-primary-900/40 dark:text-action-primary-300"
                        >
                          {{ agent }}
                        </span>
                      </div>
                    </div>
                  </div>
                </div>

                <!-- Setup Instructions -->
                <details class="mt-2">
                  <summary class="text-sm font-medium text-gray-700 dark:text-gray-300 cursor-pointer hover:text-action-primary-600 dark:hover:text-action-primary-400">
                    Setup Instructions
                  </summary>
                  <div class="mt-3 p-4 bg-gray-50 dark:bg-gray-700 rounded-lg text-sm text-gray-600 dark:text-gray-300 space-y-2">
                    <p><strong>1.</strong> Create a Slack App at <a href="https://api.slack.com/apps" target="_blank" class="text-action-primary-600 dark:text-action-primary-400 hover:underline">api.slack.com/apps</a></p>
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

          <!-- Claude Subscriptions (SUB-001/002; #471 usage observability) —
               extracted to components/settings/SubscriptionsPanel.vue (partial
               paydown of #717/#1030; the section owns its own data loads). -->
          <SubscriptionsPanel v-if="activeTab === 'integrations'" />

          <!-- Trinity Prompt Section -->
          <div v-if="activeTab === 'general'" class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
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
                      class="shadow-sm focus:ring-action-primary-500 focus:border-action-primary-500 block w-full sm:text-sm border border-gray-300 dark:border-gray-600 rounded-md font-mono bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500"
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
                  <span v-if="hasChanges" class="text-state-autonomous-600 dark:text-state-autonomous-400">Unsaved changes</span>
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
                    class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
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

          <!-- Build Info Section (#926) -->
          <div v-if="activeTab === 'general'" class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
            <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <h2 class="text-lg font-medium text-gray-900 dark:text-white">Build Info</h2>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                Provenance of the currently-running backend image. Populated at <code>docker compose build</code> time
                via <code>scripts/deploy/start.sh</code> (#926).
              </p>
            </div>
            <div class="px-6 py-4">
              <div v-if="buildInfo.loading.value" class="text-sm text-gray-500 dark:text-gray-400">
                Loading…
              </div>
              <div v-else-if="buildInfo.error.value" class="text-sm text-status-danger-600 dark:text-status-danger-400">
                Failed to load build info.
              </div>
              <div
                v-else-if="buildInfo.isMissing.value"
                class="text-sm text-gray-600 dark:text-gray-400"
              >
                Build metadata not available — rebuild with
                <code class="font-mono">scripts/deploy/start.sh</code> to populate.
              </div>
              <dl v-else-if="buildInfo.info.value" class="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-3 text-sm">
                <div>
                  <dt class="text-gray-500 dark:text-gray-400">Version</dt>
                  <dd class="font-mono text-gray-900 dark:text-white">{{ buildInfo.displayVersion.value }}</dd>
                </div>
                <div>
                  <dt class="text-gray-500 dark:text-gray-400">Branch</dt>
                  <dd class="font-mono text-gray-900 dark:text-white">{{ buildInfo.info.value.git_branch }}</dd>
                </div>
                <div class="sm:col-span-2">
                  <dt class="text-gray-500 dark:text-gray-400">Commit</dt>
                  <dd class="font-mono text-gray-900 dark:text-white">
                    <span>{{ buildInfo.info.value.git_commit_short }}</span>
                    <span class="ml-2 text-xs opacity-60 break-all">{{ buildInfo.info.value.git_commit }}</span>
                  </dd>
                </div>
                <div class="sm:col-span-2">
                  <dt class="text-gray-500 dark:text-gray-400">Commit subject</dt>
                  <dd class="text-gray-900 dark:text-white break-words">{{ buildInfo.info.value.git_commit_subject }}</dd>
                </div>
                <div>
                  <dt class="text-gray-500 dark:text-gray-400">Commit timestamp</dt>
                  <dd class="font-mono text-gray-900 dark:text-white text-xs">{{ buildInfo.info.value.git_commit_timestamp }}</dd>
                </div>
                <div>
                  <dt class="text-gray-500 dark:text-gray-400">Build date</dt>
                  <dd class="font-mono text-gray-900 dark:text-white text-xs">{{ buildInfo.info.value.build_date }}</dd>
                </div>
              </dl>
            </div>
          </div>

          <!-- Email Whitelist Section (Phase 12.4) -->
          <div v-if="activeTab === 'access'" class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
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
                    class="flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white text-sm"
                    :disabled="addingEmail"
                    @keyup.enter="addEmailToWhitelist"
                  />
                  <button
                    @click="addEmailToWhitelist"
                    :disabled="!newEmail || addingEmail"
                    class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
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
                          <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-action-primary-600 mx-auto"></div>
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
                            class="text-status-danger-600 hover:text-status-danger-900 dark:text-status-danger-400 dark:hover:text-status-danger-300 disabled:opacity-50"
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
          <div v-if="activeTab === 'access'" class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
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
                <span class="inline-flex items-center px-2 py-0.5 rounded-full bg-accent-purple-100 text-accent-purple-800 dark:bg-accent-purple-900 dark:text-accent-purple-200">admin — full control</span>
                <span class="inline-flex items-center px-2 py-0.5 rounded-full bg-action-primary-100 text-action-primary-800 dark:bg-action-primary-900 dark:text-action-primary-200">creator — create &amp; manage agents</span>
                <span class="inline-flex items-center px-2 py-0.5 rounded-full bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200">operator — run existing agents</span>
                <span class="inline-flex items-center px-2 py-0.5 rounded-full bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200">user — public links only</span>
              </div>

              <!-- #995 — enterprise: invite users (gated by user_management) -->
              <div v-if="umEntitled" class="mb-4">
                <button
                  v-if="!showInvite"
                  @click="showInvite = true"
                  class="px-3 py-2 text-sm font-medium rounded-lg bg-action-primary-600 hover:bg-action-primary-700 text-white"
                >
                  + Invite user
                  <span class="ml-1 px-1.5 py-0.5 text-[9px] font-bold rounded bg-purple-200/70 text-purple-800 align-middle">PRO</span>
                </button>
                <form v-else @submit.prevent="createInvite" class="flex flex-wrap items-center gap-2 p-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-700/40">
                  <input v-model="inviteEmail" type="email" required placeholder="email@company.com" :disabled="umBusy"
                    class="flex-1 min-w-[200px] px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
                  <select v-model="inviteRole" :disabled="umBusy"
                    class="px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100">
                    <option value="user">user</option>
                    <option value="operator">operator</option>
                    <option value="creator">creator</option>
                    <option value="admin">admin</option>
                  </select>
                  <button type="submit" :disabled="umBusy || !inviteEmail"
                    class="px-3 py-2 text-sm font-medium rounded-lg bg-action-primary-600 hover:bg-action-primary-700 text-white disabled:opacity-50">Send invite</button>
                  <button type="button" @click="showInvite = false; inviteEmail = ''" :disabled="umBusy"
                    class="px-3 py-2 text-sm rounded-lg text-gray-600 dark:text-gray-300 hover:underline">Cancel</button>
                  <span v-if="inviteMsg" class="text-xs" :class="inviteErr ? 'text-status-danger-600 dark:text-status-danger-400' : 'text-status-success-600 dark:text-status-success-400'">{{ inviteMsg }}</span>
                </form>
              </div>

              <!-- Users Table — padding trimmed + compact actions so the
                   entitlement-gated Management column fits without a horizontal
                   scrollbar (#995). That trimming was sized against the old
                   max-w-4xl container; the view is max-w-7xl since #1862, so
                   there is now headroom here rather than a tight fit. -->
              <div class="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
                <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                  <thead class="bg-gray-50 dark:bg-gray-700">
                    <tr>
                      <th scope="col" class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">User</th>
                      <th scope="col" class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">Email</th>
                      <th scope="col" class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">Role</th>
                      <th scope="col" class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">Last Login</th>
                      <!-- #995 — enterprise user management actions; column only when entitled -->
                      <th v-if="umEntitled" scope="col" class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                        Management
                        <span class="ml-1 px-1.5 py-0.5 text-[9px] font-bold rounded bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-200 align-middle">PRO</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody class="bg-white dark:bg-gray-800 divide-y divide-gray-200 dark:divide-gray-700">
                    <tr v-if="loadingUsers">
                      <td :colspan="umEntitled ? 5 : 4" class="px-6 py-4 text-center text-sm text-gray-500 dark:text-gray-400">
                        <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-action-primary-600 mx-auto"></div>
                      </td>
                    </tr>
                    <tr v-else-if="usersList.length === 0">
                      <td :colspan="umEntitled ? 5 : 4" class="px-6 py-4 text-center text-sm text-gray-500 dark:text-gray-400">No users found.</td>
                    </tr>
                    <tr v-else v-for="u in usersList" :key="u.username" class="hover:bg-gray-50 dark:hover:bg-gray-700">
                      <td class="px-4 py-4 whitespace-nowrap text-sm font-medium text-gray-900 dark:text-gray-100">
                        {{ u.name || u.username }}
                      </td>
                      <td class="px-4 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                        {{ u.email || u.username }}
                      </td>
                      <td class="px-4 py-4 whitespace-nowrap text-sm">
                        <select
                          v-if="u.username !== currentUsername"
                          :value="u.role"
                          @change="updateUserRole(u.username, $event.target.value)"
                          class="text-sm border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500"
                        >
                          <option value="admin">admin</option>
                          <option value="creator">creator</option>
                          <option value="operator">operator</option>
                          <option value="user">user</option>
                        </select>
                        <span v-else class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-accent-purple-100 text-accent-purple-800 dark:bg-accent-purple-900 dark:text-accent-purple-200">
                          {{ u.role }} (you)
                        </span>
                      </td>
                      <td class="px-4 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                        {{ u.last_login ? formatDate(u.last_login) : 'Never' }}
                      </td>
                      <td v-if="umEntitled" class="px-4 py-4 text-sm">
                        <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
                          <span v-if="u.suspended_at" class="px-2 py-0.5 font-medium rounded-full bg-status-danger-100 text-status-danger-700 dark:bg-status-danger-900/50 dark:text-status-danger-300">
                            Deactivated
                          </span>
                          <button @click="openActivity(u)" class="text-action-primary-600 dark:text-action-primary-400 hover:underline">Activity</button>
                          <template v-if="u.username !== currentUsername && u.username !== 'admin'">
                            <button v-if="u.suspended_at" @click="reactivateUser(u)" :disabled="umBusy" class="text-status-success-600 dark:text-status-success-400 hover:underline disabled:opacity-50">Reactivate</button>
                            <button v-else @click="suspendUser(u)" :disabled="umBusy" class="text-status-danger-600 dark:text-status-danger-400 hover:underline disabled:opacity-50">Deactivate</button>
                          </template>
                        </div>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          <!-- #995 — Per-user activity audit drawer (enterprise, gated by user_management) -->
          <div v-if="activityUser" class="fixed inset-0 z-50 flex justify-end" @click.self="closeActivity">
            <div class="absolute inset-0 bg-black/40" @click="closeActivity"></div>
            <div class="relative w-full max-w-md h-full bg-white dark:bg-gray-800 shadow-xl overflow-y-auto">
              <div class="px-5 py-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between sticky top-0 bg-white dark:bg-gray-800">
                <div>
                  <h3 class="text-base font-medium text-gray-900 dark:text-white">Activity</h3>
                  <p class="text-xs text-gray-500 dark:text-gray-400">{{ activityUser.name || activityUser.username }}</p>
                </div>
                <button @click="closeActivity" class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 text-xl leading-none">&times;</button>
              </div>

              <div class="p-5">
                <div v-if="activityLoading" class="text-center py-8">
                  <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-action-primary-600 mx-auto"></div>
                </div>
                <div v-else-if="activityError" class="text-sm text-status-danger-600 dark:text-status-danger-400">{{ activityError }}</div>
                <template v-else-if="activityData">
                  <div class="mb-4 rounded-lg bg-gray-50 dark:bg-gray-700/40 p-3 text-sm">
                    <div class="flex justify-between"><span class="text-gray-500 dark:text-gray-400">Total events</span><span class="font-medium text-gray-900 dark:text-gray-100">{{ activityData.summary.total }}</span></div>
                    <div v-if="activityData.summary.last_seen" class="flex justify-between mt-1"><span class="text-gray-500 dark:text-gray-400">Last seen</span><span class="text-gray-900 dark:text-gray-100">{{ formatDate(activityData.summary.last_seen) }}</span></div>
                    <div v-if="activityData.summary.first_seen" class="flex justify-between mt-1"><span class="text-gray-500 dark:text-gray-400">First seen</span><span class="text-gray-900 dark:text-gray-100">{{ formatDate(activityData.summary.first_seen) }}</span></div>
                    <div v-for="(n, et) in activityData.summary.by_event_type" :key="et" class="flex justify-between mt-1">
                      <span class="text-gray-500 dark:text-gray-400">{{ et }}</span><span class="text-gray-900 dark:text-gray-100">{{ n }}</span>
                    </div>
                  </div>

                  <p v-if="!activityData.entries.length" class="text-sm text-gray-500 dark:text-gray-400">No recorded activity.</p>
                  <ul v-else class="space-y-2">
                    <li v-for="e in activityData.entries" :key="e.event_id" class="text-sm border-l-2 border-gray-200 dark:border-gray-600 pl-3 py-1">
                      <div class="flex items-center gap-2">
                        <span class="font-medium text-gray-900 dark:text-gray-100">{{ e.event_type }}</span>
                        <span class="text-xs text-gray-500 dark:text-gray-400">{{ e.event_action }}</span>
                      </div>
                      <div class="text-xs text-gray-400">
                        {{ formatDate(e.timestamp) }}<template v-if="e.target_id"> · {{ e.target_type }}:{{ e.target_id }}</template>
                      </div>
                    </li>
                  </ul>
                </template>
              </div>
            </div>
          </div>

          <!-- MCP Server URL Section (#76) -->
          <div v-if="activeTab === 'mcp-keys' && isAdmin" class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
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
                  class="flex-1 block rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white shadow-sm focus:border-action-primary-500 focus:ring-action-primary-500 sm:text-sm"
                />
                <button
                  @click="saveMcpUrl"
                  :disabled="!mcpUrlInput || savingMcpUrl"
                  class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
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

              <p v-if="mcpUrlError" class="mt-2 text-sm text-status-danger-600 dark:text-status-danger-400">
                {{ mcpUrlError }}
              </p>
              <p v-if="mcpUrlSuccess" class="mt-2 text-sm text-status-success-600 dark:text-status-success-400">
                {{ mcpUrlSuccess }}
              </p>
            </div>
          </div>

          <!-- GitHub Templates Section (TMPL-001) -->
          <div v-if="activeTab === 'agents'" class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
            <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <h2 class="text-lg font-medium text-gray-900 dark:text-white">GitHub Templates</h2>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                Configure which GitHub repositories appear as agent templates.
                <!-- #1931: "Using defaults" is a lie about an empty set — the
                     shipped default list is now []. Say what is actually true. -->
                <span v-if="githubTemplatesSource === 'defaults'" class="inline-flex items-center ml-2 px-2 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300">
                  No defaults configured
                </span>
                <span v-else class="inline-flex items-center ml-2 px-2 py-0.5 rounded-full text-xs font-medium bg-action-primary-100 text-action-primary-700 dark:bg-action-primary-900 dark:text-action-primary-300">
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
                    class="flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white font-mono text-sm"
                    :disabled="savingGithubTemplates"
                    @keyup.enter="addGithubTemplate"
                  />
                  <input
                    v-model="newTemplateName"
                    type="text"
                    placeholder="Display name (optional)"
                    class="w-48 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white text-sm"
                    :disabled="savingGithubTemplates"
                    @keyup.enter="addGithubTemplate"
                  />
                  <button
                    @click="addGithubTemplate"
                    :disabled="!newTemplateRepo || savingGithubTemplates"
                    class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    Add
                  </button>
                </div>
                <p v-if="templateValidationError" class="text-sm text-status-danger-600 dark:text-status-danger-400">
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
                          <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-action-primary-600 mx-auto"></div>
                        </td>
                      </tr>
                      <!-- #1931: dropped "or reset to defaults" — the Reset
                           button is :disabled in exactly this state, and the
                           shipped default list is now empty, so resetting
                           would land right back here. Naming an action the
                           user cannot take is the dead end this issue is
                           about, one click from the Library that sends them
                           here. -->
                      <tr v-else-if="githubTemplates.length === 0">
                        <td colspan="3" class="px-6 py-4 text-center text-sm text-gray-500 dark:text-gray-400">
                          No GitHub templates configured. Trinity ships no defaults — add an <span class="font-mono">owner/repo</span> above to publish it to the Library.
                        </td>
                      </tr>
                      <tr v-else v-for="(tmpl, index) in githubTemplates" :key="tmpl.github_repo" class="hover:bg-gray-50 dark:hover:bg-gray-700">
                        <td class="px-6 py-4 whitespace-nowrap text-sm font-mono text-gray-900 dark:text-gray-100">
                          {{ tmpl.github_repo }}
                        </td>
                        <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                          {{ tmpl.resolved_name || tmpl.display_name || '-' }}
                          <span v-if="tmpl.display_name" class="ml-1 text-xs text-action-primary-500">(custom)</span>
                        </td>
                        <td class="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                          <button
                            @click="removeGithubTemplate(index)"
                            :disabled="savingGithubTemplates"
                            class="text-status-danger-600 hover:text-status-danger-900 dark:text-status-danger-400 dark:hover:text-status-danger-300 disabled:opacity-50"
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
                    class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
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

          <!-- Template Registry (TMPL-002, trinity-enterprise#14). Its own file
               so this view's raw-color count cannot move; the tag adds none. -->
          <TemplateRegistryPanel v-if="activeTab === 'agents'" />

          <!-- SSH Access Section -->
          <div v-if="activeTab === 'access'" class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
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
                    sshAccessEnabled ? 'bg-action-primary-600' : 'bg-gray-200 dark:bg-gray-600',
                    'relative inline-flex h-6 w-11 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-action-primary-500 focus:ring-offset-2'
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
          <div v-if="activeTab === 'agents'" class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
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
                <span class="text-sm font-medium text-status-success-600 dark:text-status-success-400">Unlimited</span>
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
                  class="w-20 rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white shadow-sm focus:border-action-primary-500 focus:ring-action-primary-500 sm:text-sm text-center"
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
                  class="w-20 rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white shadow-sm focus:border-action-primary-500 focus:ring-action-primary-500 sm:text-sm text-center"
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
                  class="w-20 rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white shadow-sm focus:border-action-primary-500 focus:ring-action-primary-500 sm:text-sm text-center"
                />
              </div>

              <!-- Legacy setting warning -->
              <div v-if="agentQuotaLegacy" class="rounded-md bg-status-warning-50 dark:bg-status-warning-900/20 p-3">
                <p class="text-sm text-status-warning-700 dark:text-status-warning-400">
                  Legacy setting <code class="px-1 py-0.5 bg-status-warning-100 dark:bg-status-warning-900/40 rounded text-xs">max_agents_per_user={{ agentQuotaLegacy }}</code> is active and used as fallback. Save per-role quotas to override it.
                </p>
              </div>

              <!-- Save button -->
              <div class="flex justify-end">
                <button
                  type="button"
                  class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-action-primary-600 hover:bg-action-primary-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-action-primary-500 disabled:opacity-50"
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

          <!-- Skills Library sources (ent#237) -->
          <SkillSourcesPanel v-if="activeTab === 'agents'" />

          <!-- Skills Library automation (ent#236). Kept in Settings, NOT folded
               into SkillSourcesPanel: auto-sync and fleet re-inject are
               library-WIDE policy over every source, while the panel is a
               per-source list. -->
          <div v-if="activeTab === 'agents'" class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
            <div class="px-6 py-4 space-y-4">
          <!-- Lifecycle automation (ent#236) -->
          <div class="border-t border-gray-200 dark:border-gray-700 pt-4 space-y-3">
            <h3 class="text-sm font-medium text-gray-900 dark:text-white">Automation</h3>

            <label class="flex items-start gap-3">
              <input
                type="checkbox"
                v-model="skillsAutomation.auto_sync_enabled"
                class="mt-0.5 h-4 w-4 rounded border-gray-300 dark:border-gray-600 text-action-primary-600 focus:ring-action-primary-500"
              />
              <span class="text-sm">
                <span class="font-medium text-gray-700 dark:text-gray-300">Scheduled auto-sync</span>
                <span class="block text-xs text-gray-500 dark:text-gray-400">
                  Pull the library on a schedule instead of clicking Sync Library.
                </span>
              </span>
            </label>

            <div v-if="skillsAutomation.auto_sync_enabled" class="pl-7">
              <label for="skills-sync-interval" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                Interval (seconds)
              </label>
              <input
                type="number"
                id="skills-sync-interval"
                v-model.number="skillsAutomation.auto_sync_interval_seconds"
                :min="skillsAutomation.interval_min"
                :max="skillsAutomation.interval_max"
                class="mt-1 block w-40 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white text-sm"
              />
              <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                Between {{ skillsAutomation.interval_min }} and {{ skillsAutomation.interval_max }} seconds.
              </p>
            </div>

            <label class="flex items-start gap-3">
              <input
                type="checkbox"
                v-model="skillsAutomation.auto_reinject_enabled"
                class="mt-0.5 h-4 w-4 rounded border-gray-300 dark:border-gray-600 text-action-primary-600 focus:ring-action-primary-500"
              />
              <span class="text-sm">
                <span class="font-medium text-gray-700 dark:text-gray-300">Re-inject across the fleet after a sync</span>
                <span class="block text-xs text-gray-500 dark:text-gray-400">
                  When the library commit changes, push updated skills to running agents.
                  Stopped agents update on their next start.
                </span>
              </span>
            </label>

            <!-- Last fleet run -->
            <div
              v-if="skillsAutomation.last_fleet_reinject"
              class="bg-gray-50 dark:bg-gray-700/50 rounded-lg p-3 text-xs text-gray-600 dark:text-gray-300"
            >
              <div class="font-medium text-gray-700 dark:text-gray-200">Last fleet re-inject</div>
              <div class="mt-1">
                {{ formatDate(skillsAutomation.last_fleet_reinject.finished_at) }} —
                {{ skillsAutomation.last_fleet_reinject.agents_injected }} updated,
                {{ skillsAutomation.last_fleet_reinject.agents_skipped }} skipped,
                <span :class="skillsAutomation.last_fleet_reinject.agents_failed > 0 ? 'text-status-danger-600 dark:text-status-danger-400 font-medium' : ''">
                  {{ skillsAutomation.last_fleet_reinject.agents_failed }} failed
                </span>
                of {{ skillsAutomation.last_fleet_reinject.agents_total }}
              </div>
              <div
                v-if="skillsAutomation.last_fleet_reinject.agents_failed > 0"
                class="mt-1 text-status-danger-600 dark:text-status-danger-400 break-all"
              >
                Affected: {{ Object.keys(skillsAutomation.last_fleet_reinject.failures || {}).join(', ') }}
              </div>
            </div>
          </div>

              <!-- Actions -->
              <div class="flex justify-end">
                <button
                  @click="saveSkillsAutomation"
                  :disabled="savingSkillsAutomation"
                  class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  <svg v-if="savingSkillsAutomation" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                  </svg>
                  {{ savingSkillsAutomation ? 'Saving...' : 'Save Automation' }}
                </button>
              </div>
            </div>
          </div>

          <!-- Default Avatars (AVATAR-003) -->
          <div v-if="activeTab === 'general'" class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
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
                'bg-status-success-50 dark:bg-status-success-900/30 text-status-success-700 dark:text-status-success-300': defaultAvatarResult.generated > 0 && defaultAvatarResult.failed === 0,
                'bg-status-warning-50 dark:bg-status-warning-900/30 text-status-warning-700 dark:text-status-warning-300': defaultAvatarResult.failed > 0,
                'bg-gray-50 dark:bg-gray-700 text-gray-600 dark:text-gray-300': defaultAvatarResult.generated === 0 && defaultAvatarResult.failed === 0
              }">
                <p class="text-sm font-medium">{{ defaultAvatarResult.message }}</p>
                <ul v-if="defaultAvatarResult.agents.length" class="mt-1 text-xs space-y-0.5">
                  <li v-for="name in defaultAvatarResult.agents" :key="name">Generated: {{ name }}</li>
                </ul>
                <ul v-if="defaultAvatarResult.errors.length" class="mt-1 text-xs space-y-0.5">
                  <li v-for="err in defaultAvatarResult.errors" :key="err.agent" class="text-status-danger-600 dark:text-status-danger-400">Failed: {{ err.agent }} - {{ err.error }}</li>
                </ul>
              </div>

              <!-- Generate button -->
              <button
                @click="generateDefaultAvatars"
                :disabled="generatingDefaultAvatars"
                class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
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
        <div v-if="error" class="mt-4 bg-status-danger-50 dark:bg-status-danger-900/30 border border-status-danger-200 dark:border-status-danger-800 rounded-lg p-4">
          <div class="flex">
            <div class="flex-shrink-0">
              <svg class="h-5 w-5 text-status-danger-400" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd" />
              </svg>
            </div>
            <div class="ml-3">
              <h3 class="text-sm font-medium text-status-danger-800 dark:text-status-danger-300">Error</h3>
              <p class="mt-1 text-sm text-status-danger-700 dark:text-status-danger-400">{{ error }}</p>
            </div>
          </div>
        </div>

        <!-- Success Message -->
        <div v-if="showSuccess" class="mt-4 bg-status-success-50 dark:bg-status-success-900/30 border border-status-success-200 dark:border-status-success-800 rounded-lg p-4">
          <div class="flex">
            <div class="flex-shrink-0">
              <svg class="h-5 w-5 text-status-success-400" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
              </svg>
            </div>
            <div class="ml-3">
              <p class="text-sm font-medium text-status-success-800 dark:text-status-success-300">Settings saved successfully!</p>
            </div>
          </div>
        </div>
      </div>
    </main>

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
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useRole } from '../composables/useRole'
import { useBuildInfo } from '../composables/useBuildInfo'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import { useSettingsStore } from '../stores/settings'
import { useSessionsStore } from '../stores/sessions'
import { apiErrorMessage } from '../utils/apiError'
import { useEnterpriseStore } from '../stores/enterprise'
import NavBar from '../components/NavBar.vue'
import McpKeysTab from '../components/settings/McpKeysTab.vue'
import SubscriptionsPanel from '../components/settings/SubscriptionsPanel.vue'
import UserGitHubPatPanel from '../components/settings/UserGitHubPatPanel.vue'
import AgentPermissionsMatrix from '../components/AgentPermissionsMatrix.vue'
import SkillSourcesPanel from '../components/SkillSourcesPanel.vue'
import TwoFactorPanel from '../components/settings/TwoFactorPanel.vue'
import SsoPanel from '../components/settings/SsoPanel.vue'
import ActivationFunnelPanel from '../components/settings/ActivationFunnelPanel.vue'
import TelemetrySharingPanel from '../components/settings/TelemetrySharingPanel.vue'
import PortalSessionPolicyPanel from '../components/settings/PortalSessionPolicyPanel.vue'
import RoomBudgetDefaultsPanel from '../components/settings/RoomBudgetDefaultsPanel.vue'
import { SETTINGS_NUMBER_INPUT_CLASS } from '../components/settings/fieldStyles'
import { MODEL_CATALOG } from '../constants/modelCatalog'
import TemplateRegistryPanel from '../components/settings/TemplateRegistryPanel.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const settingsStore = useSettingsStore()
// trinity-enterprise#85: refreshed after a Brain Orb flag change so the
// admin's own Brain tab / route gating updates without a page reload.
const sessionsStore = useSessionsStore()
// Declared early: visibleTabs (and thus the activeTab initializer below) reads
// it during setup to gate the enterprise-only Security tab (#5). Declaring it
// later would hit the temporal dead zone and blank the whole Settings page.
const enterpriseStore = useEnterpriseStore()

// #926: cached fetch of /api/version (singleton shared with NavBar).
const buildInfo = useBuildInfo()

const loading = ref(true)
const saving = ref(false)
const error = ref(null)
const showSuccess = ref(false)

// Tab state (#302). Tabs are role-gated:
//   MCP Keys      — visible to any authenticated user (matches today's /api-keys page).
//   General/Access/Integrations/Agents — admin only.
// Backend require_admin on each endpoint stays as the actual security boundary;
// hiding tabs is convenience.
// activeTab syncs with the ?tab= URL query param so deep links work.
const ALL_TABS = [
  { id: 'general',      label: 'General',      adminOnly: true  },
  { id: 'access',       label: 'Access',       adminOnly: true  },
  { id: 'integrations', label: 'Integrations', adminOnly: true  },
  { id: 'mcp-keys',     label: 'MCP Keys',     adminOnly: false },
  { id: 'agent-permissions', label: 'Agent Permissions', adminOnly: false, requires: 'permissions_matrix' },
  { id: 'security',     label: 'Security',     adminOnly: false, requires: '2fa' },
  { id: 'sso',          label: 'SSO',          adminOnly: true,  requires: 'sso' },
  { id: 'agents',       label: 'Agents',       adminOnly: true  },
  { id: 'retention',    label: 'Retention',    adminOnly: true  },
  // ent#184 — local product-event activation funnel. Capture is OSS-core;
  // this operator view is entitlement-gated (`telemetry`), so the tab is hidden
  // in OSS-only builds. Local-only data, admin-only.
  { id: 'activation',   label: 'Activation',   adminOnly: true, requires: 'telemetry' },
]
const { isAdmin } = useRole()
const visibleTabs = computed(() =>
  ALL_TABS.filter(t => {
    // #5 — the Security (2FA) tab only appears when the enterprise `2fa`
    // feature is entitled; otherwise it's hidden in OSS-only builds.
    if (t.requires) return enterpriseStore.isEntitled(t.requires)
    return isAdmin.value || !t.adminOnly
  })
)
const validTabIds = computed(() => visibleTabs.value.map(t => t.id))
const DEFAULT_TAB = computed(() =>
  isAdmin.value ? 'general' : 'mcp-keys'
)
function resolveTabFromQuery(q) {
  return validTabIds.value.includes(q) ? q : DEFAULT_TAB.value
}
const activeTab = ref(resolveTabFromQuery(route.query.tab))

// Click handler — push a new history entry so browser back/forward
// navigates between tabs. Pushes only when the tab actually changes,
// to avoid duplicate entries on re-clicks.
function selectTab(id) {
  if (!validTabIds.value.includes(id)) return
  if (id === activeTab.value) return
  activeTab.value = id
  router.push({ query: { ...route.query, tab: id } })
}

// Sync activeTab when the URL changes externally (back/forward, deep link).
watch(() => route.query.tab, (newTab) => {
  activeTab.value = resolveTabFromQuery(newTab)
})

// Email whitelist state (Phase 12.4)
const emailWhitelist = ref([])
const newEmail = ref('')
const addingEmail = ref(false)
const removingEmail = ref(null)
const loadingWhitelist = ref(false)

// User management state (ROLE-001)
const usersList = ref([])
const loadingUsers = ref(false)

// #995 — enterprise per-user activity audit (gated by user_management).
// (enterpriseStore is declared near the top — visibleTabs needs it during setup.)
const umEntitled = computed(() => enterpriseStore.isEntitled('user_management'))

// #1039 — data-retention. The read surface (GET /api/settings/retention) is
// available in every edition; editing is enterprise-only (PUT to the gated
// /api/enterprise/retention/config). Community shows the fixed 5-day floor +
// an upgrade hint.
const retentionEntitled = computed(() => enterpriseStore.isEntitled('retention'))
const RETENTION_FIELDS = [
  { key: 'log_retention_days', label: 'Log archival' },
  { key: 'execution_log_retention_days', label: 'Execution logs' },
  { key: 'execution_row_retention_days', label: 'Execution rows' },
  { key: 'health_check_retention_days', label: 'Health checks' },
  { key: 'agent_soft_delete_retention_days', label: 'Soft-deleted agents' },
  { key: 'schedule_soft_delete_retention_days', label: 'Soft-deleted schedules' },
]
const retention = ref(null)        // { edition, community_floor_days, windows{} }
const retentionForm = reactive({}) // editable copy of the OPS/log windows

// Shared styling for this panel's number inputs.
//
// The panel's inputs specified `border-gray-300` WITHOUT the `border` class, so
// no border-width was ever applied and browsers fell back to their default
// number-input chrome — which is what made them look unstyled. They also had no
// padding and no focus ring. This matches the app-wide convention used by ~17
// other inputs (`px-3 py-2 border … focus:ring-action-primary-500`).
//
// The `[appearance:textfield]` + `::-webkit-*-spin-button` triple removes the
// native steppers: nobody nudges a retention window to 90 one click at a time,
// and the arrows were the loudest thing in a panel whose numbers are typed.
// ent#375: moved to components/settings/fieldStyles.js so the Workspace-session
// panel on this same tab uses the identical field styling by construction
// rather than by a copied string that drifts.
const RETENTION_INPUT_CLASS = SETTINGS_NUMBER_INPUT_CLASS

const retentionLoading = ref(false)
const retentionSaving = ref(false)
const retentionError = ref('')
const retentionSaved = ref(false)

// #1709: in-product approval of a guard-refused (over-threshold) retention prune.
// POST /api/settings/retention/acknowledge is the GATE (admin + human only,
// window-bound: a 409 means the window in force changed under us). Single-use —
// after the next cleanup cycle prunes, the guard re-arms and loadRetention()
// shows the item gone (no stale "approved" state).
const acknowledgingKey = ref('')
const ackError = ref('')
const ackErrorKey = ref('')
const ackDone = ref(false)

async function acknowledgePrune(item) {
  acknowledgingKey.value = item.key
  ackError.value = ''
  ackErrorKey.value = ''
  ackDone.value = false
  try {
    await axios.post(
      '/api/settings/retention/acknowledge',
      { key: item.key, window_days: item.window_days },
      { headers: authStore.authHeader }
    )
    ackDone.value = true
    await loadRetention()   // the acked sweep is now allowed → drops off pending
  } catch (e) {
    ackErrorKey.value = item.key
    // 409 = window mismatch (the window in force moved); surface the server's
    // readable message rather than a generic failure.
    ackError.value = apiErrorMessage(e, 'Failed to approve the deletion.')
  } finally {
    acknowledgingKey.value = ''
  }
}

async function loadRetention() {
  retentionLoading.value = true
  retentionError.value = ''
  try {
    const r = await axios.get('/api/settings/retention', { headers: authStore.authHeader })
    retention.value = r.data
    for (const f of RETENTION_FIELDS) {
      retentionForm[f.key] = r.data?.windows?.[f.key]
    }
  } catch (e) {
    retentionError.value = apiErrorMessage(e, 'Failed to load retention settings.')
  } finally {
    retentionLoading.value = false
  }
}


async function saveRetention() {
  if (!retentionEntitled.value) return
  retentionSaving.value = true
  retentionError.value = ''
  retentionSaved.value = false
  try {
    const body = {}
    for (const f of RETENTION_FIELDS) {
      const n = parseInt(retentionForm[f.key], 10)
      if (!Number.isNaN(n)) body[f.key] = n
    }
    await axios.put('/api/enterprise/retention/config', body, { headers: authStore.authHeader })
    retentionSaved.value = true
    await loadRetention()
  } catch (e) {
    retentionError.value = apiErrorMessage(e, 'Failed to save retention settings.')
  } finally {
    retentionSaving.value = false
  }
}
const activityUser = ref(null)
const activityData = ref(null)
const activityLoading = ref(false)
const activityError = ref('')

async function openActivity(u) {
  activityUser.value = u
  activityData.value = null
  activityError.value = ''
  activityLoading.value = true
  try {
    const r = await axios.get(
      `/api/enterprise/user-management/users/${u.id}/activity?limit=50`,
      { headers: authStore.authHeader }
    )
    activityData.value = r.data
  } catch (e) {
    activityError.value = e.response?.data?.detail || e.message
  } finally {
    activityLoading.value = false
  }
}

function closeActivity() {
  activityUser.value = null
  activityData.value = null
}

// #995 — enterprise user lifecycle: deactivate / reactivate / invite.
const UM_BASE = '/api/enterprise/user-management'
const umBusy = ref(false)
const showInvite = ref(false)
const inviteEmail = ref('')
const inviteRole = ref('user')
const inviteMsg = ref('')
const inviteErr = ref(false)

async function suspendUser(u) {
  if (umBusy.value) return
  if (!confirm(`Deactivate ${u.email || u.username}? They will be signed out and unable to log in until reactivated.`)) return
  umBusy.value = true
  try {
    await axios.post(`${UM_BASE}/users/${u.id}/suspend`, {}, { headers: authStore.authHeader })
    await loadUsers()
  } catch (e) {
    alert(e.response?.data?.detail || e.message)
  } finally {
    umBusy.value = false
  }
}

async function reactivateUser(u) {
  if (umBusy.value) return
  umBusy.value = true
  try {
    await axios.post(`${UM_BASE}/users/${u.id}/reactivate`, {}, { headers: authStore.authHeader })
    await loadUsers()
  } catch (e) {
    alert(e.response?.data?.detail || e.message)
  } finally {
    umBusy.value = false
  }
}

async function createInvite() {
  if (umBusy.value || !inviteEmail.value) return
  umBusy.value = true
  inviteMsg.value = ''
  inviteErr.value = false
  try {
    await axios.post(`${UM_BASE}/invites`, { email: inviteEmail.value, role: inviteRole.value }, { headers: authStore.authHeader })
    inviteMsg.value = `Invited ${inviteEmail.value} (${inviteRole.value}) — they can now sign in by email.`
    inviteEmail.value = ''
    inviteRole.value = 'user'
  } catch (e) {
    inviteErr.value = true
    inviteMsg.value = e.response?.data?.detail || e.message
  } finally {
    umBusy.value = false
  }
}

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

// Admin sign-in email (#82 Phase 1) — existing-admin migration to email login.
const adminEmailInput = ref('')
const savingAdminEmail = ref(false)
const adminEmailSaveSuccess = ref(false)
const adminEmailError = ref('')
// Only a real email (with @) counts as "set"; the legacy admin row stores the
// placeholder 'admin' until one is registered.
const adminEmailCurrent = computed(() => {
  const e = authStore.user?.email || ''
  return e.includes('@') ? e : ''
})

// Platform default model (#831)
const platformDefaultModelValue = ref('claude-sonnet-4-6')
// Admin fleet-default dropdown options — the catalog filtered to models an admin
// may set as the platform default. Haiku is deliberately excluded (#1080). Order
// follows the catalog; "(recommended)" rides the `recommended` flag in the template.
const adminDefaultModels = MODEL_CATALOG.filter((m) => m.adminDefaultSelectable)
const savingPlatformDefaultModel = ref(false)
const platformDefaultModelSaveSuccess = ref(false)

// #1129: fleet-wide default access policy (require verified email for new agents)
const defaultRequireEmail = ref(true)
const savingDefaultAccessPolicy = ref(false)
const defaultAccessPolicySaveSuccess = ref(false)

// #506: fleet-wide ceiling on per-agent max_parallel_tasks
const maxParallelTasksCeiling = ref(10)
const ceilingMin = ref(1)
const ceilingMax = ref(32)
const savingCeiling = ref(false)

// #1609: admin-tunable proactive channel-message caps (per hour; 0 = unlimited).
const PROACTIVE_ROWS = [
  { key: 'slack_proactive_per_channel', label: 'Slack — per channel', hint: 'Proactive messages/hour to one Slack channel' },
  { key: 'slack_proactive_per_agent', label: 'Slack — per agent', hint: 'Across all Slack channels for one agent' },
  { key: 'telegram_proactive_per_group', label: 'Telegram — per group', hint: 'Proactive messages/hour to one Telegram group' },
  { key: 'telegram_proactive_per_agent', label: 'Telegram — per agent', hint: 'Across all Telegram groups for one agent' },
  { key: 'proactive_dm_per_recipient', label: 'Direct messages — per recipient', hint: 'Proactive DMs/hour to one recipient' },
]
const proactiveLimits = ref({})
const proactiveMax = ref(1000000)
const savingProactive = ref(false)
const proactiveSaveSuccess = ref(false)
const proactiveError = ref('')
const proactiveWarnings = ref([])
const ceilingSaveSuccess = ref(false)
const ceilingError = ref('')

// trinity-enterprise#85: Brain Orb platform flags (value + source per flag;
// source is override|env|default — "override" means the env var is ignored)
const brainOrb = reactive({
  enabled: { value: false, source: 'default' },
  voice_enabled: { value: false, source: 'default' },
  write_enabled: { value: false, source: 'default' },
})
const brainOrbFlagRows = [
  { key: 'enabled', label: 'Enable Brain Orb', hint: 'Gates the Brain tab, the /brain page, and every brain-orb API route.' },
  { key: 'voice_enabled', label: 'Voice tile', hint: 'Client-held Gemini Live voice inside the orb. Effective only while Brain Orb is enabled.' },
  { key: 'write_enabled', label: 'KB-write actions', hint: 'Owner-gated capture/link — enables an exec-adjacent surface on the agent (its action hook). Effective only while Brain Orb is enabled.' },
]
const brainOrbGeminiKey = ref(false)
const savingBrainOrb = ref(false)
const brainOrbSaveSuccess = ref(false)
const brainOrbError = ref('')

// ElevenLabs / Voice platform settings (ent#117)
const elevenLabs = reactive({
  keyConfigured: false,
  keySource: 'none',   // override | env | none
  apiKeyInput: '',
  defaultVoiceId: '',
})
const savingElevenLabs = ref(false)
const elevenLabsSaveSuccess = ref(false)
const elevenLabsError = ref('')
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
const removingApiKey = ref(false)
const removingGithubPat = ref(false)
const removingSlackSettings = ref(false)

const slackHasStoredCredentials = computed(() => {
  const s = slackSettings.value
  return Boolean(
    (s?.client_id?.configured && s.client_id.source === 'settings') ||
    (s?.client_secret?.configured && s.client_secret.source === 'settings') ||
    (s?.signing_secret?.configured && s.signing_secret.source === 'settings')
  )
})

const confirmDialog = reactive({
  visible: false,
  title: '',
  message: '',
  confirmText: 'Confirm',
  variant: 'danger',
  onConfirm: () => {}
})

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
// (SUB-003 auto-switch + subscription state moved to SubscriptionsPanel.vue, #471)

// Skills Library state now lives in SkillSourcesPanel / stores/skillSources
// (ent#237): the single skills_library_url setting became a list of sources.
// Only ent#236's library-wide automation config stays here.
const savingSkillsAutomation = ref(false)

// Skills library lifecycle automation (ent#236). Defaults mirror the backend's
// (OFF / 3600s) so the panel renders correctly before the fetch resolves.
const skillsAutomation = ref({
  auto_sync_enabled: false,
  auto_sync_interval_seconds: 3600,
  auto_reinject_enabled: false,
  interval_min: 300,
  interval_max: 86400,
  last_fleet_reinject: null
})

// Default Avatars state (AVATAR-003)
const generatingDefaultAvatars = ref(false)
const defaultAvatarResult = ref(null)

// Subscriptions state moved to components/settings/SubscriptionsPanel.vue (#471).

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
      loadPlatformDefaultModel(),
      loadDefaultAccessPolicy(),
      loadMaxParallelTasksCeiling(),
      loadProactiveLimits(),
      loadBrainOrbSettings(),
      loadElevenLabsSettings(),
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

function removeAnthropicKey() {
  confirmDialog.title = 'Remove Anthropic API Key'
  confirmDialog.message = 'Remove the stored Anthropic API key? Agents will fall back to the ANTHROPIC_API_KEY environment variable if set, otherwise they will stop working until a key is re-added.'
  confirmDialog.confirmText = 'Remove'
  confirmDialog.variant = 'danger'
  confirmDialog.onConfirm = async () => {
    removingApiKey.value = true
    error.value = null
    try {
      await axios.delete('/api/settings/api-keys/anthropic')
      await loadApiKeyStatus()
      anthropicKey.value = ''
      apiKeyTestResult.value = null
    } catch (e) {
      error.value = e.response?.data?.detail || 'Failed to remove API key'
    } finally {
      removingApiKey.value = false
    }
  }
  confirmDialog.visible = true
}

function removeGithubPat() {
  confirmDialog.title = 'Remove GitHub PAT'
  confirmDialog.message = 'Remove the stored GitHub Personal Access Token? Agents will fall back to the GITHUB_PAT environment variable if set. Repository creation and push will fail until a PAT is re-added.'
  confirmDialog.confirmText = 'Remove'
  confirmDialog.variant = 'danger'
  confirmDialog.onConfirm = async () => {
    removingGithubPat.value = true
    error.value = null
    try {
      await axios.delete('/api/settings/api-keys/github')
      await loadApiKeyStatus()
      githubPat.value = ''
      githubPatTestResult.value = null
      githubPatPropagation.value = null
    } catch (e) {
      error.value = e.response?.data?.detail || 'Failed to remove GitHub PAT'
    } finally {
      removingGithubPat.value = false
    }
  }
  confirmDialog.visible = true
}

// Platform default model methods (#831)
async function loadPlatformDefaultModel() {
  try {
    const value = await settingsStore.getSetting('platform_default_model')
    if (value) platformDefaultModelValue.value = value
  } catch {
    // non-critical; UI shows the code-default
  }
}

async function savePlatformDefaultModel() {
  savingPlatformDefaultModel.value = true
  platformDefaultModelSaveSuccess.value = false
  try {
    await settingsStore.updateSetting('platform_default_model', platformDefaultModelValue.value)
    platformDefaultModelSaveSuccess.value = true
    setTimeout(() => { platformDefaultModelSaveSuccess.value = false }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to save default model'
  } finally {
    savingPlatformDefaultModel.value = false
  }
}

// #1129: fleet-wide default access policy
async function loadDefaultAccessPolicy() {
  try {
    const policy = await settingsStore.getAgentDefaultAccessPolicy()
    defaultRequireEmail.value = !!policy.require_email
  } catch {
    // non-critical; UI shows the code-default (ON)
  }
}

async function saveDefaultAccessPolicy() {
  savingDefaultAccessPolicy.value = true
  defaultAccessPolicySaveSuccess.value = false
  try {
    const res = await settingsStore.setAgentDefaultRequireEmail(defaultRequireEmail.value)
    defaultRequireEmail.value = !!res.require_email
    defaultAccessPolicySaveSuccess.value = true
    setTimeout(() => { defaultAccessPolicySaveSuccess.value = false }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to save default access policy'
    // revert the toggle to the persisted value on failure
    await loadDefaultAccessPolicy()
  } finally {
    savingDefaultAccessPolicy.value = false
  }
}

// #506: fleet-wide max_parallel_tasks ceiling
async function loadMaxParallelTasksCeiling() {
  try {
    const data = await settingsStore.getMaxParallelTasksCeiling()
    maxParallelTasksCeiling.value = data.value
    ceilingMin.value = data.min
    ceilingMax.value = data.max
  } catch {
    // non-critical; UI shows the code default (10)
  }
}

async function saveMaxParallelTasksCeiling() {
  savingCeiling.value = true
  ceilingSaveSuccess.value = false
  ceilingError.value = ''
  try {
    const data = await settingsStore.setMaxParallelTasksCeiling(maxParallelTasksCeiling.value)
    maxParallelTasksCeiling.value = data.value
    ceilingSaveSuccess.value = true
    setTimeout(() => { ceilingSaveSuccess.value = false }, 3000)
  } catch (e) {
    ceilingError.value = e.response?.data?.detail || 'Failed to save fleet capacity ceiling'
    await loadMaxParallelTasksCeiling()
  } finally {
    savingCeiling.value = false
  }
}

// #1609: proactive channel-message caps
async function loadProactiveLimits() {
  try {
    const { data } = await axios.get('/api/settings/proactive-rate-limits', { headers: authStore.authHeader })
    const next = {}
    for (const key of Object.keys(data.limits || {})) next[key] = data.limits[key].value
    proactiveLimits.value = next
    if (data.max) proactiveMax.value = data.max
  } catch {
    // non-critical; card shows the shipped defaults
  }
}

async function saveProactiveLimits() {
  savingProactive.value = true
  proactiveSaveSuccess.value = false
  proactiveError.value = ''
  proactiveWarnings.value = []
  try {
    const { data } = await axios.put('/api/settings/proactive-rate-limits', { ...proactiveLimits.value }, { headers: authStore.authHeader })
    proactiveWarnings.value = data.warnings || []
    proactiveSaveSuccess.value = true
    setTimeout(() => { proactiveSaveSuccess.value = false }, 3000)
    await loadProactiveLimits()
  } catch (e) {
    proactiveError.value = e.response?.data?.detail || 'Failed to save proactive message limits'
  } finally {
    savingProactive.value = false
  }
}

// trinity-enterprise#85: Brain Orb platform flags
function applyBrainOrbState(data) {
  for (const key of Object.keys(brainOrb)) {
    if (data.flags?.[key]) brainOrb[key] = data.flags[key]
  }
  brainOrbGeminiKey.value = !!data.gemini_key_configured
}

async function loadBrainOrbSettings() {
  try {
    applyBrainOrbState(await settingsStore.getBrainOrbSettings())
  } catch {
    // non-critical; panel shows the code defaults (OFF)
  }
}

async function saveBrainOrbFlag(key, value) {
  await putBrainOrbSettings({ [key]: value })
}

async function clearBrainOrbFlag(key) {
  await putBrainOrbSettings({ clear: [key] })
}

async function putBrainOrbSettings(payload) {
  savingBrainOrb.value = true
  brainOrbSaveSuccess.value = false
  brainOrbError.value = ''
  try {
    applyBrainOrbState(await settingsStore.setBrainOrbSettings(payload))
    brainOrbSaveSuccess.value = true
    setTimeout(() => { brainOrbSaveSuccess.value = false }, 3000)
    // Refresh the feature flags the rest of THIS session gates on (Brain
    // tab / route guard); other open sessions update on next page load.
    sessionsStore.loadFeatureFlags(true).catch(() => {})
  } catch (e) {
    brainOrbError.value = e.response?.data?.detail || 'Failed to save Brain Orb settings'
    await loadBrainOrbSettings()
  } finally {
    savingBrainOrb.value = false
  }
}

// ElevenLabs / Voice platform settings (ent#117)
function applyElevenLabsState(state) {
  elevenLabs.keyConfigured = !!state.key_configured
  elevenLabs.keySource = state.key_source || 'none'
  elevenLabs.defaultVoiceId = state.default_voice_id || ''
}

async function loadElevenLabsSettings() {
  try {
    const { data } = await axios.get('/api/settings/elevenlabs', { headers: authStore.authHeader })
    applyElevenLabsState(data)
  } catch {
    // non-critical; panel shows unconfigured
  }
}

async function putElevenLabsSettings(payload) {
  savingElevenLabs.value = true
  elevenLabsSaveSuccess.value = false
  elevenLabsError.value = ''
  try {
    const { data } = await axios.put('/api/settings/elevenlabs', payload, { headers: authStore.authHeader })
    applyElevenLabsState(data)
    elevenLabs.apiKeyInput = ''
    elevenLabsSaveSuccess.value = true
    setTimeout(() => { elevenLabsSaveSuccess.value = false }, 3000)
    // Refresh the tts_available feature flag the rest of this session gates on.
    sessionsStore.loadFeatureFlags(true).catch(() => {})
  } catch (e) {
    elevenLabsError.value = e.response?.data?.detail || 'Failed to save voice settings'
    await loadElevenLabsSettings()
  } finally {
    savingElevenLabs.value = false
  }
}

async function saveElevenLabsKey() {
  if (!elevenLabs.apiKeyInput.trim()) return
  await putElevenLabsSettings({ api_key: elevenLabs.apiKeyInput.trim() })
}

async function clearElevenLabsKey() {
  await putElevenLabsSettings({ clear: ['api_key'] })
}

async function saveElevenLabsDefaultVoice() {
  await putElevenLabsSettings({ default_voice_id: elevenLabs.defaultVoiceId.trim() })
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

async function saveAdminEmail() {
  const email = (adminEmailInput.value || '').trim()
  if (!email) return
  savingAdminEmail.value = true
  adminEmailSaveSuccess.value = false
  adminEmailError.value = ''
  try {
    await axios.put('/api/users/me/email', { email }, { headers: authStore.authHeader })
    // Refresh so the displayed "current" email updates immediately.
    await authStore.fetchUserProfile()
    adminEmailInput.value = ''
    adminEmailSaveSuccess.value = true
    setTimeout(() => { adminEmailSaveSuccess.value = false }, 4000)
  } catch (e) {
    adminEmailError.value = e?.response?.data?.detail || 'Failed to save email'
  } finally {
    savingAdminEmail.value = false
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

function removeSlackSettings() {
  confirmDialog.title = 'Remove Slack Credentials'
  confirmDialog.message = 'Remove the stored Slack OAuth credentials (client ID, client secret, signing secret)? Slack integration will fall back to environment variables if configured; otherwise Slack channels will stop working until credentials are re-added.'
  confirmDialog.confirmText = 'Remove'
  confirmDialog.variant = 'danger'
  confirmDialog.onConfirm = async () => {
    removingSlackSettings.value = true
    error.value = null
    try {
      await axios.delete('/api/settings/slack')
      await loadSlackSettings()
      slackClientId.value = ''
      slackClientSecret.value = ''
      slackSigningSecret.value = ''
    } catch (e) {
      error.value = e.response?.data?.detail || 'Failed to remove Slack credentials'
    } finally {
      removingSlackSettings.value = false
    }
  }
  confirmDialog.visible = true
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

// Auto-Switch methods (SUB-003) moved to SubscriptionsPanel.vue (#471).

// Skills library automation (ent#236). ent#237 removed the URL/branch writes
// that used to live here — those settings no longer exist; sources are managed
// by SkillSourcesPanel, and syncing is the panel's Sync all / per-source Sync.
async function loadSkillsAutomation() {
  try {
    const automation = await axios.get('/api/settings/skills-library', {
      headers: authStore.authHeader
    })
    skillsAutomation.value = { ...skillsAutomation.value, ...automation.data }
  } catch (e) {
    console.error('Failed to load skills automation settings:', e)
  }
}

async function saveSkillsAutomation() {
  savingSkillsAutomation.value = true
  error.value = null

  try {
    // Its own validated endpoint (the generic settings PUT rejects these keys),
    // so a bad interval surfaces as a real 400 here.
    await axios.put('/api/settings/skills-library', {
      auto_sync_enabled: skillsAutomation.value.auto_sync_enabled,
      auto_sync_interval_seconds: skillsAutomation.value.auto_sync_interval_seconds,
      auto_reinject_enabled: skillsAutomation.value.auto_reinject_enabled
    }, { headers: authStore.authHeader })

    showSuccess.value = true
    setTimeout(() => {
      showSuccess.value = false
    }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to save skills automation settings'
  } finally {
    savingSkillsAutomation.value = false
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

// Subscription methods (SUB-001/002) moved to SubscriptionsPanel.vue (#471).

// (#302) Settings is now visible to non-admin users for the MCP Keys tab.
// Admin-only data fetches MUST be skipped when the user is not admin —
// otherwise the 403 from /api/settings/api-keys etc. would trigger
// `router.push('/')` in loadSettings() and bounce the user before they
// reach MCP Keys. McpKeysTab fetches its own (non-admin) data internally.
const adminDataLoaded = ref(false)
function loadAdminOnlySettings() {
  if (adminDataLoaded.value) return
  adminDataLoaded.value = true
  loadSettings()
  loadEmailWhitelist()
  loadUsers()
  loadGithubTemplates()
  loadOpsSettings()
  loadAgentQuotas()
  // ent#236 automation config. Admin-only endpoint, so it belongs here rather
  // than in the unconditional mount path — SkillSourcesPanel loads the source
  // list itself.
  loadSkillsAutomation()
}

// Watch isAdmin with `immediate: true` so the loaders fire as soon as the
// store reports admin — covering both:
//   (a) typical case: role already in localStorage at mount time
//   (b) refresh-after-upgrade case: fetchUserProfile lands later than mount
watch(isAdmin, (admin) => {
  if (admin) loadAdminOnlySettings()
}, { immediate: true })

onMounted(() => {
  // The non-admin-safe init runs unconditionally.
  loading.value = false  // McpKeysTab handles its own loading state

  // #926: build info — non-fatal load; the General-tab panel handles
  // loading/error states. Singleton, so a no-op when NavBar already loaded.
  buildInfo.load().catch(() => {})

  // #995: enterprise entitlements — cached/no-op if NavBar already loaded.
  // Gates the per-user activity column in User Management.
  enterpriseStore.loadFeatureFlags().catch(() => {})

  // #1039: data-retention read surface (available in every edition).
  if (isAdmin.value) loadRetention().catch(() => {})

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
