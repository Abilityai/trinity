<template>
  <!-- Claude Subscriptions (SUB-001/002; usage observability #471).
       Extracted from Settings.vue in #471 — the section gained the usage/
       headroom surfaces and the extraction is the established settings/ seam
       (partial paydown of #717/#1030). CRUD calls moved verbatim; NEW API
       surface goes through stores/subscriptions.js (Invariants #6/#7). -->
  <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
    <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
      <h2 class="text-lg font-medium text-gray-900 dark:text-white">Claude Subscriptions</h2>
      <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
        Manage Claude Max/Pro subscription credentials. Register once, assign to multiple agents.
      </p>
    </div>

    <div class="px-6 py-4">
      <div class="space-y-4">
        <!-- Panel-local error line (extraction keeps failures beside their cause) -->
        <div v-if="error" class="bg-status-danger-50 dark:bg-status-danger-900/30 border border-status-danger-200 dark:border-status-danger-800 rounded-lg px-4 py-2 text-sm text-status-danger-700 dark:text-status-danger-300 flex items-start justify-between">
          <span>{{ error }}</span>
          <button class="ml-3 text-xs underline" @click="error = null">dismiss</button>
        </div>

        <!-- Encryption Not Configured Warning -->
        <div v-if="!encryptionConfigured" class="bg-status-warning-50 dark:bg-status-warning-900/30 border border-status-warning-200 dark:border-status-warning-800 rounded-lg p-4">
          <div class="flex">
            <div class="flex-shrink-0">
              <svg class="h-5 w-5 text-status-warning-400" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd" />
              </svg>
            </div>
            <div class="ml-3">
              <h3 class="text-sm font-medium text-status-warning-800 dark:text-status-warning-300">Encryption not configured</h3>
              <p class="mt-1 text-sm text-status-warning-700 dark:text-status-warning-400">
                Subscription storage requires <code class="px-1 py-0.5 bg-status-warning-100 dark:bg-status-warning-900 rounded text-xs">CREDENTIAL_ENCRYPTION_KEY</code> in your <code class="px-1 py-0.5 bg-status-warning-100 dark:bg-status-warning-900 rounded text-xs">.env</code> file.
                Generate with: <code class="px-1 py-0.5 bg-status-warning-100 dark:bg-status-warning-900 rounded text-xs">openssl rand -hex 32</code> and restart the backend.
              </p>
            </div>
          </div>
        </div>

        <!-- Add Subscription Form -->
        <div class="bg-gray-50 dark:bg-gray-700/50 rounded-lg p-4">
          <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">Add Subscription</h3>

          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label for="subscription-name" class="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                Name
              </label>
              <input
                type="text"
                id="subscription-name"
                v-model="newSubscription.name"
                placeholder="e.g., eugene-max"
                class="block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white text-sm"
                :disabled="addingSubscription"
              />
            </div>
            <div>
              <label for="subscription-type" class="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                Type
              </label>
              <select
                id="subscription-type"
                v-model="newSubscription.type"
                class="block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white text-sm"
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
              class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white text-sm focus:ring-2 focus:ring-action-primary-500 focus:border-action-primary-500"
              :class="{ 'border-status-danger-400 dark:border-status-danger-500': newSubscription.token && !newSubscription.token.startsWith('sk-ant-oat01-') }"
            />
            <p v-if="newSubscription.token && !newSubscription.token.startsWith('sk-ant-oat01-')" class="mt-1 text-xs text-status-danger-500">
              Token must start with sk-ant-oat01-
            </p>
            <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">
              Run <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded">claude setup-token</code> locally to generate a long-lived token (~1 year)
            </p>
          </div>

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
              class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
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
                <!-- #471: live pressure column -->
                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                  Pressure
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
                <td colspan="6" class="px-6 py-4 text-center text-sm text-gray-500 dark:text-gray-400">
                  <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-action-primary-600 mx-auto"></div>
                </td>
              </tr>
              <tr v-else-if="subscriptions.length === 0">
                <td colspan="6" class="px-6 py-4 text-center text-sm text-gray-500 dark:text-gray-400">
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
                          :class="sub.subscription_type === 'max' ? 'bg-accent-purple-100 text-accent-purple-800 dark:bg-accent-purple-900 dark:text-accent-purple-200' : 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200'">
                      {{ sub.subscription_type === 'max' ? 'Max' : sub.subscription_type === 'pro' ? 'Pro' : sub.subscription_type }}
                    </span>
                    <span v-else class="text-sm text-gray-500 dark:text-gray-400">—</span>
                  </td>
                  <td class="px-6 py-4 whitespace-nowrap">
                    <span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200">
                      {{ sub.agent_count || 0 }} agent{{ (sub.agent_count || 0) === 1 ? '' : 's' }}
                    </span>
                  </td>
                  <!-- #471: compact pressure cell — gauge when provider-fresh, warn chip on events, honest '—' -->
                  <td class="px-6 py-4 whitespace-nowrap text-sm">
                    <div v-if="usageLoading[sub.id]" class="animate-pulse text-xs text-gray-400">…</div>
                    <template v-else-if="usageFor(sub.id) && !usageFor(sub.id).error">
                      <span v-if="usageFor(sub.id).rate_limited_now"
                            class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-status-danger-100 text-status-danger-800 dark:bg-status-danger-900/50 dark:text-status-danger-300"
                            :title="`Rate-limited now · ${usageFor(sub.id).failure_events_24h} failure event(s) in 24h`">
                        rate-limited
                      </span>
                      <span v-else-if="headroomIsFresh(usageFor(sub.id))"
                            class="text-xs text-gray-700 dark:text-gray-300"
                            :title="usageSourceLabel(usageFor(sub.id))">
                        5h: <b>{{ usageFor(sub.id).headroom.five_hour?.utilization_pct ?? '—' }}%</b>
                        <span v-if="usageFor(sub.id).headroom.seven_day?.utilization_pct != null" class="text-gray-500 dark:text-gray-400">
                          · 7d: {{ usageFor(sub.id).headroom.seven_day.utilization_pct }}%
                        </span>
                      </span>
                      <span v-else-if="(usageFor(sub.id).failure_events_24h || 0) > 0"
                            class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-status-warning-100 text-status-warning-800 dark:bg-status-warning-900/50 dark:text-status-warning-300"
                            :title="failureKindLabel(usageFor(sub.id).failure_events_by_kind) || ''">
                        {{ usageFor(sub.id).failure_events_24h }} event{{ usageFor(sub.id).failure_events_24h === 1 ? '' : 's' }} (24h)
                      </span>
                      <span v-else class="text-xs text-gray-400 dark:text-gray-500">ok</span>
                    </template>
                    <span v-else-if="usageFor(sub.id) && usageFor(sub.id).error" class="text-xs text-gray-400 dark:text-gray-500" title="Usage unavailable">—</span>
                    <span v-else class="text-xs text-gray-400 dark:text-gray-500">—</span>
                  </td>
                  <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                    {{ formatDate(sub.created_at) }}
                  </td>
                  <td class="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                    <button
                      @click.stop="deleteSubscription(sub)"
                      :disabled="deletingSubscription === sub.id"
                      class="text-status-danger-600 hover:text-status-danger-900 dark:text-status-danger-400 dark:hover:text-status-danger-300 disabled:opacity-50"
                    >
                      {{ deletingSubscription === sub.id ? 'Deleting...' : 'Delete' }}
                    </button>
                  </td>
                </tr>
                <!-- Expanded Details Row -->
                <tr v-if="expandedSubscriptions.has(sub.id)" class="bg-gray-50 dark:bg-gray-700/50">
                  <td colspan="6" class="px-6 py-4">
                    <div class="text-sm">
                      <!-- #471: usage & headroom block -->
                      <div class="mb-4 rounded-lg border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-800 p-3">
                        <div class="flex items-center justify-between mb-2">
                          <h4 class="text-xs font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">Usage</h4>
                          <div class="flex items-center gap-2">
                            <span v-if="usageFor(sub.id) && !usageFor(sub.id).error" class="text-xs text-gray-400 dark:text-gray-500">
                              {{ usageSourceLabel(usageFor(sub.id)) }}
                            </span>
                            <button
                              @click.stop="refreshHeadroom(sub.id)"
                              :disabled="subsStore.refreshingHeadroom[sub.id]"
                              class="inline-flex items-center px-2 py-1 border border-gray-300 dark:border-gray-600 rounded text-xs text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50"
                              title="Fetch live utilization from Anthropic (sends one ~1-token probe on this subscription's own quota)"
                            >
                              <svg v-if="subsStore.refreshingHeadroom[sub.id]" class="animate-spin mr-1 h-3 w-3" fill="none" viewBox="0 0 24 24">
                                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
                              </svg>
                              Refresh
                            </button>
                          </div>
                        </div>

                        <div v-if="usageLoading[sub.id]" class="text-xs text-gray-400 py-2">Loading usage…</div>
                        <div v-else-if="!usageFor(sub.id) || usageFor(sub.id).error" class="text-xs text-gray-500 dark:text-gray-400 py-2">
                          Usage unavailable right now — try Refresh.
                        </div>
                        <template v-else>
                          <!-- Live headroom (provider truth) -->
                          <div v-if="usageFor(sub.id).headroom && (usageFor(sub.id).headroom.five_hour || usageFor(sub.id).headroom.seven_day)" class="mb-3 space-y-0.5">
                            <div v-if="headroomWindowLabel(usageFor(sub.id).headroom.five_hour, '5h')" class="text-sm text-gray-800 dark:text-gray-200">
                              {{ headroomWindowLabel(usageFor(sub.id).headroom.five_hour, '5h') }}
                            </div>
                            <div v-if="headroomWindowLabel(usageFor(sub.id).headroom.seven_day, '7d')" class="text-sm text-gray-800 dark:text-gray-200">
                              {{ headroomWindowLabel(usageFor(sub.id).headroom.seven_day, '7d') }}
                            </div>
                            <div v-if="usageFor(sub.id).headroom.status !== 'ok'" class="text-xs text-status-warning-600 dark:text-status-warning-400">
                              Probe status: {{ usageFor(sub.id).headroom.status }}
                            </div>
                          </div>

                          <!-- Observed consumption (always present) -->
                          <table class="w-full text-xs">
                            <thead>
                              <tr class="text-gray-500 dark:text-gray-400">
                                <th class="text-left font-medium py-1">Window</th>
                                <th class="text-right font-medium py-1" title="Estimated from recorded context occupancy — an inference, not a billed counter">≈ In tokens (est.)</th>
                                <th class="text-right font-medium py-1">Out tokens</th>
                                <th class="text-right font-medium py-1">Cost</th>
                                <th class="text-right font-medium py-1">Messages</th>
                              </tr>
                            </thead>
                            <tbody class="text-gray-800 dark:text-gray-200">
                              <tr>
                                <td class="py-0.5">Last 5h</td>
                                <td class="text-right">{{ formatTokens(usageFor(sub.id).window_5h.input_tokens) }}</td>
                                <td class="text-right">{{ formatTokens(usageFor(sub.id).window_5h.output_tokens) }}</td>
                                <td class="text-right">≈{{ formatCost(usageFor(sub.id).window_5h.cost_usd) }}</td>
                                <td class="text-right">{{ usageFor(sub.id).window_5h.message_count }}</td>
                              </tr>
                              <tr>
                                <td class="py-0.5">Last 7d</td>
                                <td class="text-right">{{ formatTokens(usageFor(sub.id).window_7d.input_tokens) }}</td>
                                <td class="text-right">{{ formatTokens(usageFor(sub.id).window_7d.output_tokens) }}</td>
                                <td class="text-right">≈{{ formatCost(usageFor(sub.id).window_7d.cost_usd) }}</td>
                                <td class="text-right">{{ usageFor(sub.id).window_7d.message_count }}</td>
                              </tr>
                            </tbody>
                          </table>
                          <p class="mt-1 text-[11px] text-gray-400 dark:text-gray-500">
                            Cost is API-equivalent (what this consumption would cost at API prices) — not a bill.
                          </p>

                          <!-- Failure events -->
                          <div v-if="(usageFor(sub.id).failure_events_24h || 0) > 0" class="mt-2 text-xs text-status-warning-700 dark:text-status-warning-400">
                            ⚠ {{ usageFor(sub.id).failure_events_24h }} subscription failure event{{ usageFor(sub.id).failure_events_24h === 1 ? '' : 's' }} in the last 24h
                            <span v-if="failureKindLabel(usageFor(sub.id).failure_events_by_kind)">({{ failureKindLabel(usageFor(sub.id).failure_events_by_kind) }})</span>
                          </div>

                          <!-- Per-agent breakdown (lazy, #471 Tier 2) -->
                          <div class="mt-3">
                            <button
                              @click.stop="toggleBreakdown(sub.id)"
                              class="text-xs text-action-primary-600 dark:text-action-primary-400 hover:underline"
                            >
                              {{ showBreakdown.has(sub.id) ? 'Hide per-agent breakdown' : 'Show per-agent breakdown' }}
                            </button>
                            <div v-if="showBreakdown.has(sub.id)" class="mt-2">
                              <div v-if="subsStore.breakdownLoading[sub.id]" class="text-xs text-gray-400">Loading…</div>
                              <div v-else-if="!breakdownFor(sub.id) || breakdownFor(sub.id).error" class="text-xs text-gray-500 dark:text-gray-400">
                                Breakdown unavailable —
                                <button class="underline" @click.stop="subsStore.fetchBreakdown(sub.id)">retry</button>
                              </div>
                              <table v-else-if="(breakdownFor(sub.id).window_7d || []).length" class="w-full text-xs">
                                <thead>
                                  <tr class="text-gray-500 dark:text-gray-400">
                                    <th class="text-left font-medium py-1">Agent (7d, by cost)</th>
                                    <th class="text-right font-medium py-1">Cost</th>
                                    <th class="text-right font-medium py-1">Out tokens</th>
                                    <th class="text-right font-medium py-1">Messages</th>
                                    <th class="text-right font-medium py-1">5h cost</th>
                                  </tr>
                                </thead>
                                <tbody class="text-gray-800 dark:text-gray-200">
                                  <tr v-for="row in breakdownFor(sub.id).window_7d" :key="row.agent_name">
                                    <td class="py-0.5">{{ agentsStore.displayNameForSlug(row.agent_name) }}</td>
                                    <td class="text-right">≈{{ formatCost(row.cost_usd) }}</td>
                                    <td class="text-right">{{ formatTokens(row.output_tokens) }}</td>
                                    <td class="text-right">{{ row.message_count }}</td>
                                    <td class="text-right text-gray-500 dark:text-gray-400">≈{{ formatCost(breakdown5hCost(sub.id, row.agent_name)) }}</td>
                                  </tr>
                                </tbody>
                              </table>
                              <p v-else class="text-xs text-gray-500 dark:text-gray-400 italic">No recorded consumption in the last 7 days.</p>
                            </div>
                          </div>
                        </template>
                      </div>

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
                                class="inline-flex items-center px-2.5 py-1 rounded-md text-xs font-medium bg-action-primary-100 text-action-primary-800 dark:bg-action-primary-900 dark:text-action-primary-200">
                            <svg class="mr-1 h-3 w-3" fill="currentColor" viewBox="0 0 20 20">
                              <path d="M9 2a1 1 0 000 2h2a1 1 0 100-2H9z" />
                              <path fill-rule="evenodd" d="M4 5a2 2 0 012-2 3 3 0 003 3h2a3 3 0 003-3 2 2 0 012 2v11a2 2 0 01-2 2H6a2 2 0 01-2-2V5zm3 4a1 1 0 000 2h.01a1 1 0 100-2H7zm3 0a1 1 0 000 2h3a1 1 0 100-2h-3zm-3 4a1 1 0 100 2h.01a1 1 0 100-2H7zm3 0a1 1 0 100 2h3a1 1 0 100-2h-3z" clip-rule="evenodd" />
                            </svg>
                            {{ agent }}
                            <button
                              @click.stop="unassignAgentFromSubscription(agent)"
                              :disabled="unassigningAgent === agent"
                              class="ml-1.5 inline-flex items-center justify-center h-4 w-4 rounded-full hover:bg-action-primary-200 dark:hover:bg-action-primary-800 text-action-primary-600 dark:text-action-primary-300 disabled:opacity-50"
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
                            class="flex-1 max-w-xs px-3 py-1.5 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm text-sm focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white"
                            @click.stop
                          >
                            <option value="" disabled selected>{{ loadingAgents ? 'Loading agents...' : 'Select agent...' }}</option>
                            <option
                              v-for="agent in getAvailableAgents(sub.id)"
                              :key="agent.name"
                              :value="agent.name"
                            >
                              {{ agentOptionLabel(agent) }}{{ agentSubscriptionMap[agent.name] ? ` (on ${agentSubscriptionMap[agent.name]})` : '' }}
                            </option>
                          </select>
                          <button
                            @click.stop="assignAgentToSubscription(sub.name, selectedAgentToAssign[sub.id])"
                            :disabled="!selectedAgentToAssign[sub.id] || assigningAgent"
                            class="inline-flex items-center px-3 py-1.5 border border-transparent rounded-md shadow-sm text-xs font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
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
          Expand a subscription row to see usage, live headroom, and assign or remove agents. Running agents will restart automatically.
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
                autoSwitchEnabled ? 'bg-action-primary-600' : 'bg-gray-200 dark:bg-gray-600',
                'relative inline-flex h-6 w-11 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-action-primary-500 focus:ring-offset-2'
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

        <!-- Headroom Auto-Refresh Toggle (#471) -->
        <div class="pt-4 border-t border-gray-200 dark:border-gray-700">
          <div class="flex items-center justify-between">
            <div class="flex-1 mr-4">
              <label for="headroom-refresh-toggle" class="text-sm font-medium text-gray-700 dark:text-gray-300">
                Check subscription quota automatically
              </label>
              <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                Reads actual utilization from Anthropic by sending a minimal (~1-token) probe on each subscription's own quota,
                at most every {{ Math.round((subsStore.headroomAutoRefresh.refresh_seconds || 900) / 60) }} minutes per subscription.
                Trinity checks in the background whether or not anyone is watching &mdash; to keep this page current, to notice a
                rate-limited subscription recovering, and to watch weekly limits for the alert below.
                Probes appear as tiny messages in the Anthropic console. When off, headroom updates only via the Refresh button
                and weekly-limit alerts stop.
              </p>
            </div>
            <button
              id="headroom-refresh-toggle"
              type="button"
              :class="[
                subsStore.headroomAutoRefresh.enabled ? 'bg-action-primary-600' : 'bg-gray-200 dark:bg-gray-600',
                'relative inline-flex h-6 w-11 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-action-primary-500 focus:ring-offset-2'
              ]"
              :disabled="savingHeadroomToggle"
              @click="toggleHeadroomAutoRefresh"
            >
              <span
                :class="[
                  subsStore.headroomAutoRefresh.enabled ? 'translate-x-5' : 'translate-x-0',
                  'pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out'
                ]"
              />
            </button>
          </div>
        </div>

        <!-- Weekly-limit alert threshold (ent#434) -->
        <div class="pt-4 border-t border-gray-200 dark:border-gray-700">
          <div class="flex items-start justify-between gap-4">
            <div class="flex-1">
              <label
                for="headroom-alert-threshold"
                class="text-sm font-medium text-gray-700 dark:text-gray-300"
              >
                Warn me before the weekly limit
              </label>
              <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                Raises an operator alert when a subscription passes this share of its 7-day
                window, so there is still time to stagger schedules or move agents. Urgency
                follows your own burn rate: at this threshold but on track to finish the week
                under 100%, the alert is filed low priority. Set to 0 to turn the alerts off.
              </p>
              <p class="mt-2 text-xs" :class="alertStatusClass">
                {{ alertStatusText }}
              </p>
            </div>
            <div class="w-40 flex-shrink-0">
              <BaseInput
                id="headroom-alert-threshold"
                v-model="thresholdDraft"
                type="number"
                :min="0"
                :max="weeklyAlert.max_pct || 99"
                :error="thresholdError"
                :disabled="savingThreshold"
                @keyup.enter="saveThreshold"
              />
              <BaseButton
                class="mt-2 w-full"
                variant="secondary"
                size="sm"
                :disabled="!thresholdChanged || savingThreshold"
                :loading="savingThreshold"
                @click="saveThreshold"
              >
                Save
              </BaseButton>
            </div>
          </div>
          <InlineError
            v-if="thresholdSaveError"
            class="mt-3"
            :message="thresholdSaveError"
            @dismiss="thresholdSaveError = ''"
          />
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import axios from 'axios'
import { useAuthStore } from '../../stores/auth'
import { useAgentsStore } from '../../stores/agents'
import { useSubscriptionsStore } from '../../stores/subscriptions'
import BaseInput from '../base/BaseInput.vue'
import BaseButton from '../base/BaseButton.vue'
import InlineError from '../InlineError.vue'
import {
  thresholdValidationError,
  thresholdChanged as isThresholdChanged,
  alertStatusLine,
  describeSaveFailure,
} from '../../utils/headroomAlertSettings'
import { agentOptionLabel } from '../../utils/agentName'
import { formatCost } from '../../composables/useFormatters'
import {
  headroomWindowLabel,
  usageSourceLabel,
  headroomIsFresh,
  failureKindLabel,
} from '../../utils/subscriptionPressure'

const authStore = useAuthStore()
const agentsStore = useAgentsStore()
const subsStore = useSubscriptionsStore()

// ── ent#434: weekly-limit alert threshold ────────────────────────────────────
// Decidable rules live in utils/headroomAlertSettings.js — vitest runs
// `environment: 'node'` with no mount harness, so a rule expressed here would
// be untestable (the ent#392 precedent).
const thresholdDraft = ref('')
const savingThreshold = ref(false)
const thresholdSaveError = ref('')

const weeklyAlert = computed(() => subsStore.headroomAutoRefresh.weekly_alert || {})

const thresholdChanged = computed(() =>
  isThresholdChanged(thresholdDraft.value, weeklyAlert.value.threshold_pct)
)

const thresholdError = computed(() =>
  thresholdValidationError(thresholdDraft.value, {
    min: weeklyAlert.value.min_pct,
    max: weeklyAlert.value.max_pct,
  })
)

const alertStatus = computed(() =>
  alertStatusLine(weeklyAlert.value, subsStore.headroomAutoRefresh.loaded)
)
const alertStatusText = computed(() => alertStatus.value.text)
const alertStatusClass = computed(() =>
  alertStatus.value.tone === 'active'
    ? 'text-status-success-700 dark:text-status-success-300'
    : 'text-gray-500 dark:text-gray-400'
)

// Seeded from the server, never left blank (principle 3 — the operator edits a
// working proposal). Guarded on `thresholdChanged` so a background refetch
// cannot clobber a half-typed edit.
watch(
  () => weeklyAlert.value.threshold_pct,
  (v) => {
    if (v !== undefined && v !== null && !thresholdChanged.value) {
      thresholdDraft.value = String(v)
    }
  },
  { immediate: true }
)

async function saveThreshold() {
  if (thresholdError.value || !thresholdChanged.value || savingThreshold.value) return
  savingThreshold.value = true
  thresholdSaveError.value = ''
  try {
    await subsStore.setHeadroomAlertThreshold(Number(String(thresholdDraft.value).trim()))
  } catch (e) {
    // A failed verb gets a home next to its control (principle 18) — never a
    // bare console.error, never an alert().
    thresholdSaveError.value = describeSaveFailure(e)
  } finally {
    savingThreshold.value = false
  }
}

// --- State moved verbatim from Settings.vue (SUB-002; #471 extraction) ---
const subscriptions = ref([])
const loadingSubscriptions = ref(false)
const addingSubscription = ref(false)
const deletingSubscription = ref(null)
const expandedSubscriptions = ref(new Set())
const encryptionConfigured = ref(true)
const newSubscription = ref({ name: '', type: 'max', token: '' })

const allAgents = ref([])
const loadingAgents = ref(false)
const assigningAgent = ref(null)
const unassigningAgent = ref(null)
const selectedAgentToAssign = ref({})

const autoSwitchEnabled = ref(false)
const savingAutoSwitch = ref(false)

// #471 panel-local state
const error = ref(null)
const savingHeadroomToggle = ref(false)
const showBreakdown = ref(new Set())

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

const usageLoading = computed(() => subsStore.usageLoading)
function usageFor(subId) { return subsStore.usageBySub[subId] }
function breakdownFor(subId) { return subsStore.breakdownBySub[subId] }

function breakdown5hCost(subId, agentName) {
  const rows = breakdownFor(subId)?.window_5h || []
  return rows.find((r) => r.agent_name === agentName)?.cost_usd || 0
}

function formatTokens(n) {
  const v = Number(n) || 0
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}k`
  return String(v)
}

function formatDate(dateString) {
  if (!dateString) return 'N/A'
  const date = new Date(dateString)
  const now = new Date()
  const diffInDays = Math.floor((now - date) / (1000 * 60 * 60 * 24))
  if (diffInDays === 0) return 'Today'
  if (diffInDays === 1) return 'Yesterday'
  if (diffInDays < 7) return `${diffInDays} days ago`
  if (diffInDays < 30) return `${Math.floor(diffInDays / 7)} weeks ago`
  return date.toLocaleDateString()
}

// --- Methods moved verbatim from Settings.vue (error target localized) ---
async function loadSubscriptions() {
  loadingSubscriptions.value = true
  try {
    try {
      const statusResponse = await axios.get('/api/subscriptions/encryption-status', {
        headers: authStore.authHeader
      })
      encryptionConfigured.value = statusResponse.data?.configured ?? true
    } catch {
      encryptionConfigured.value = true
    }

    const response = await axios.get('/api/subscriptions', {
      headers: authStore.authHeader
    })
    subscriptions.value = response.data || []
    // #471: hydrate each card's usage (per-card isolation in the store)
    subsStore.fetchUsageForAll(subscriptions.value.map((s) => s.id))
  } catch (e) {
    console.error('Failed to load subscriptions:', e)
    if (e.response?.status !== 403) {
      error.value = e.response?.data?.detail || 'Failed to load subscriptions'
    }
  } finally {
    loadingSubscriptions.value = false
  }
}

function clearNewSubscription() {
  newSubscription.value = { name: '', type: 'max', token: '' }
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
    }, { headers: authStore.authHeader })
    clearNewSubscription()
    await loadSubscriptions()
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
    expandedSubscriptions.value.delete(subscription.id)
    await loadSubscriptions()
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
  expandedSubscriptions.value = new Set(expandedSubscriptions.value)
}

function toggleBreakdown(subId) {
  if (showBreakdown.value.has(subId)) {
    showBreakdown.value.delete(subId)
  } else {
    showBreakdown.value.add(subId)
    if (!breakdownFor(subId)) subsStore.fetchBreakdown(subId)
  }
  showBreakdown.value = new Set(showBreakdown.value)
}

async function refreshHeadroom(subId) {
  const result = await subsStore.refreshHeadroom(subId)
  if (result === null) {
    error.value = 'Headroom refresh failed — see backend logs'
  }
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
    selectedAgentToAssign.value = {}
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to assign agent'
  } finally {
    assigningAgent.value = null
  }
}

async function unassignAgentFromSubscription(agentName) {
  if (!confirm(`Remove "${agentsStore.displayNameForSlug(agentName)}" from this subscription?\n\nIf the agent is running, it will be restarted.`)) return
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

// SUB-003 auto-switch setting (moved verbatim)
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
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to update auto-switch setting'
  } finally {
    savingAutoSwitch.value = false
  }
}

// #471 headroom auto-refresh toggle
async function toggleHeadroomAutoRefresh() {
  savingHeadroomToggle.value = true
  error.value = null
  try {
    await subsStore.setHeadroomAutoRefresh(!subsStore.headroomAutoRefresh.enabled)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to update headroom auto-refresh setting'
  } finally {
    savingHeadroomToggle.value = false
  }
}

onMounted(() => {
  loadSubscriptions()
  loadAutoSwitchSetting()
  subsStore.fetchHeadroomAutoRefresh()
})
</script>
