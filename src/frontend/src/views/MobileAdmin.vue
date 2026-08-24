<template>
  <div class="mobile-admin" :class="{ 'keyboard-open': keyboardOpen }">
    <!-- Login Screen -->
    <div v-if="!authStore.isAuthenticated" class="login-screen">
      <div class="login-container">
        <div class="login-logo">
          <svg viewBox="0 0 48 48" class="w-12 h-12 mx-auto mb-2">
            <path d="M24 8 L36 16 L33 28 L15 28 L12 16 Z" fill="none" stroke="#818cf8" stroke-width="2" stroke-linejoin="round"/>
            <circle cx="24" cy="17" r="3" fill="#818cf8"/>
            <circle cx="17" cy="26" r="2" fill="#6366f1"/>
            <circle cx="31" cy="26" r="2" fill="#6366f1"/>
            <line x1="24" y1="17" x2="17" y2="26" stroke="#6366f1" stroke-width="1"/>
            <line x1="24" y1="17" x2="31" y2="26" stroke="#6366f1" stroke-width="1"/>
          </svg>
          <h1 class="text-xl font-semibold text-white">Trinity Mobile</h1>
        </div>
        <form @submit.prevent="handleLogin" class="login-form">
          <input
            v-model="loginPassword"
            type="password"
            placeholder="Admin password"
            class="login-input"
            autocomplete="current-password"
            autocapitalize="off"
            :disabled="loginLoading"
          />
          <button type="submit" class="login-button" :disabled="loginLoading || !loginPassword">
            {{ loginLoading ? 'Signing in...' : 'Sign In' }}
          </button>
          <p v-if="loginError" class="login-error">{{ loginError }}</p>
        </form>
      </div>
    </div>

    <!-- Main App -->
    <div v-else class="app-container">
      <!-- Header -->
      <header class="app-header">
        <h1 class="text-base font-semibold text-white truncate">Trinity</h1>
        <div class="header-actions">
          <button @click="refreshCurrentTab" class="header-btn" :class="{ 'animate-spin': refreshing }">
            <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
            </svg>
          </button>
          <button @click="handleLogout" class="header-btn text-status-danger-400">
            <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"/>
            </svg>
          </button>
        </div>
      </header>

      <!-- Tab Content -->
      <main class="tab-content" ref="scrollContainer">
        <!-- Pull to Refresh indicator -->
        <div v-if="pullDistance > 0" class="pull-indicator" :style="{ height: pullDistance + 'px' }">
          <svg class="w-5 h-5 text-action-primary-400" :class="{ 'animate-spin': pullRefreshing }" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
            <path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
          </svg>
        </div>

        <!-- Action failure banner (#1926) — start/stop, autonomy, queue respond
             and acknowledge used to fail to console only, so on a phone the
             verb simply appeared to do nothing. Persists until dismissed
             (principle 18: toasts are for completed verbs, not errors). -->
        <div v-if="actionError" class="action-error result-error" role="alert">
          <div class="action-error-body">
            <p>{{ actionError }}</p>
            <p v-if="actionErrorDetail" class="action-error-detail">{{ actionErrorDetail }}</p>
          </div>
          <button class="action-error-dismiss" aria-label="Dismiss error" @click="clearActionError">✕</button>
        </div>

        <!-- AGENTS TAB -->
        <div v-if="activeTab === 'agents'" class="tab-panel">
          <!-- Search -->
          <div class="search-bar">
            <input
              v-model="agentSearch"
              type="text"
              placeholder="Search agents..."
              class="search-input"
            />
          </div>

          <!-- Agent list. #1927: the 15s poll swaps values in place; the
               "Loading…" copy shows only before the first data, a failed first
               fetch is the failed state (never "No agents found"), and a failed
               refresh with data on screen is the SIBLING stale banner. -->
          <InlineError
            v-if="agentsView.stale"
            class="mb-3"
            :message="staleBannerMessage('agents', lastLoadedAt.agents)"
            :detail="fetchError.agents"
            retryable
            @retry="fetchAgents"
            @dismiss="fetchError.agents = ''"
          />
          <div v-if="agentsView.state === 'loading'" class="loading-state">Loading agents...</div>
          <LoadFailed
            v-else-if="agentsView.state === 'failed'"
            dense
            title="Couldn't load agents"
            message="The agent list didn't load. Try again or pull down to refresh."
            :detail="fetchError.agents"
            :retrying="loading.agents"
            @retry="fetchAgents"
          />
          <div v-else-if="agentsView.state === 'empty'" class="empty-state">No agents found</div>
          <div v-else class="agent-list">
            <div
              v-for="agent in filteredAgents"
              :key="agent.name"
              class="agent-card"
              @click="toggleAgentExpand(agent.name)"
            >
              <div class="agent-card-header">
                <div class="agent-info">
                  <div class="agent-name">{{ agent.name }}</div>
                  <div class="agent-meta">
                    <span class="status-dot" :class="agent.status === 'running' ? 'bg-status-success-400' : 'bg-gray-500'"></span>
                    <span class="text-xs text-gray-400">{{ agent.status }}</span>
                    <span v-if="agent.autonomy_enabled" class="autonomy-badge auto">AUTO</span>
                  </div>
                  <!-- Success rate bar -->
                  <div v-if="getAgentSuccessPercent(agent.name) > 0" class="success-bar-row">
                    <div class="success-bar-track">
                      <div
                        class="success-bar-fill"
                        :class="getSuccessBarClass(getAgentSuccessPercent(agent.name))"
                        :style="{ width: getAgentSuccessPercent(agent.name) + '%' }"
                      ></div>
                    </div>
                    <span class="success-bar-label" :class="getSuccessTextClass(getAgentSuccessPercent(agent.name))">{{ getAgentSuccessPercent(agent.name) }}%</span>
                  </div>
                </div>
                <div class="agent-actions" @click.stop>
                  <button
                    @click="toggleAgent(agent.name, agent.status)"
                    class="toggle-btn"
                    :class="agent.status === 'running' ? 'toggle-stop' : 'toggle-start'"
                    :disabled="togglingAgents[agent.name]"
                  >
                    <span v-if="togglingAgents[agent.name]" class="animate-spin inline-block w-4 h-4 border-2 border-current border-t-transparent rounded-full"></span>
                    <span v-else>{{ agent.status === 'running' ? 'Stop' : 'Start' }}</span>
                  </button>
                </div>
              </div>

              <!-- Expanded details -->
              <div v-if="expandedAgent === agent.name" class="agent-details" @click.stop>
                <!-- Autonomy toggle -->
                <div class="detail-row">
                  <span class="detail-label">Mode</span>
                  <button
                    @click="toggleAutonomy(agent)"
                    class="autonomy-toggle-btn"
                    :class="agent.autonomy_enabled ? 'autonomy-on' : 'autonomy-off'"
                    :disabled="togglingAutonomy[agent.name]"
                  >
                    <span v-if="togglingAutonomy[agent.name]" class="animate-spin inline-block w-3 h-3 border-2 border-current border-t-transparent rounded-full"></span>
                    <span v-else>{{ agent.autonomy_enabled ? 'AUTO' : 'Manual' }}</span>
                  </button>
                </div>

                <!-- Chat button -->
                <div class="detail-row">
                  <span class="detail-label">Chat</span>
                  <button
                    @click="openChat(agent)"
                    class="chat-open-btn"
                    :disabled="agent.status !== 'running'"
                  >
                    {{ agent.status !== 'running' ? 'Start agent first' : 'Open Chat' }}
                  </button>
                </div>

                <!-- Logs -->
                <div class="detail-row">
                  <span class="detail-label">Logs</span>
                  <button @click="fetchAgentLogs(agent.name)" class="text-xs text-action-primary-400 underline">
                    {{ agentLogs[agent.name] ? 'Refresh' : 'Load' }}
                  </button>
                </div>
                <pre v-if="agentLogs[agent.name]" class="logs-view">{{ agentLogs[agent.name] }}</pre>
              </div>
            </div>
          </div>
        </div>

        <!-- OPS TAB -->
        <div v-if="activeTab === 'ops'" class="tab-panel">
          <!-- Sub-tabs -->
          <div class="sub-tabs">
            <button
              v-for="sub in opsTabs"
              :key="sub.id"
              @click="activeOpsTab = sub.id"
              class="sub-tab"
              :class="{ active: activeOpsTab === sub.id }"
            >
              {{ sub.label }}
              <span v-if="sub.count > 0" class="sub-tab-badge">{{ sub.count }}</span>
            </button>
          </div>

          <!-- Queue items -->
          <div v-if="activeOpsTab === 'queue'" class="ops-section">
            <InlineError
              v-if="queueView.stale"
              class="mb-3"
              :message="staleBannerMessage('the queue', lastLoadedAt.queue)"
              :detail="fetchError.queue"
              retryable
              @retry="fetchQueue"
              @dismiss="fetchError.queue = ''"
            />
            <div v-if="queueView.state === 'loading'" class="loading-state">Loading queue...</div>
            <LoadFailed
              v-else-if="queueView.state === 'failed'"
              dense
              title="Couldn't load the queue"
              message="We can't tell whether your agents need you. Try again."
              :detail="fetchError.queue"
              :retrying="loading.queue"
              @retry="fetchQueue"
            />
            <div v-else-if="queueView.state === 'empty'" class="empty-state">No pending items</div>
            <div v-else class="ops-list">
              <div
                v-for="item in queueItems"
                :key="item.id"
                class="ops-card"
                data-testid="queue-card"
                :data-item-id="item.id"
              >
                <div class="ops-card-header">
                  <span class="ops-agent-name" :title="agentNameTooltip(agentsStore.agentRefForSlug(item.agent_name))">{{ item.agent_name }}</span>
                  <span class="ops-priority" :class="'priority-' + item.priority">{{ item.priority }}</span>
                </div>
                <!-- The API field is `type`; a read of a misnamed field here rendered a blank line for months (issue 2370). -->
                <div class="ops-card-type" data-testid="queue-type">{{ queueTypeLabel(item.type) }}</div>
                <p v-if="item.title && item.title !== item.question" class="ops-card-title" data-testid="queue-title">{{ item.title }}</p>
                <p class="ops-card-message">{{ item.message || item.question || item.description }}</p>
                <!-- Controls switch on the item TYPE (desktop parity), and an
                     approval is never answered on one tap: select → restated
                     consequence → optional note → explicit Send. The decision
                     rides `response`, the note rides `response_text`, and the
                     body comes from utils/operatorQueue.js — the same builder
                     the desktop store uses (the hand-built body here used to
                     send a hard-coded literal decision for every tap).
                     This inline step is deliberately p19-shaped — named verb,
                     restated consequence, the safe action first and focused —
                     and is NOT a confirm overlay (see the note on #1924). -->
                <template v-if="queueResponseKind(item) === 'approval'">
                  <div class="ops-options" role="group" aria-label="Options">
                    <button
                      v-for="(opt, idx) in optionsOf(item)"
                      :key="idx + ':' + opt"
                      type="button"
                      class="ops-option-btn"
                      data-testid="queue-option"
                      :aria-pressed="selectedOptions[item.id] === opt ? 'true' : 'false'"
                      :disabled="respondingItems[item.id]"
                      @click="selectOption(item.id, opt)"
                    >{{ opt }}</button>
                  </div>
                  <div v-if="selectedOptions[item.id]" class="ops-approval-form" data-testid="queue-approval-form">
                    <p class="ops-card-body" role="status" data-testid="queue-consequence">
                      Sending <strong>{{ selectedOptions[item.id] }}</strong> to {{ item.agent_name }} — it reads this as your decision on its next run.
                    </p>
                    <input
                      v-model="responseTexts[item.id]"
                      type="text"
                      enterkeyhint="done"
                      placeholder="Add a note (optional)..."
                      class="ops-response-input"
                      data-testid="queue-note"
                      :disabled="respondingItems[item.id]"
                    />
                    <div class="ops-response-row">
                      <button
                        type="button"
                        class="ops-ack-btn"
                        data-testid="queue-cancel"
                        :ref="(el) => registerCancelButton(item.id, el)"
                        :disabled="respondingItems[item.id]"
                        @click="clearSelection(item.id)"
                      >Cancel</button>
                      <button
                        type="button"
                        class="ops-respond-btn ops-send-btn"
                        data-testid="queue-send"
                        :disabled="respondingItems[item.id]"
                        @click="submitApproval(item)"
                      >Send: {{ selectedOptions[item.id] }}</button>
                    </div>
                  </div>
                </template>
                <div v-else-if="queueResponseKind(item) === 'acknowledge'" class="ops-card-footer ops-ack-only">
                  <button
                    type="button"
                    class="ops-ack-btn"
                    data-testid="queue-ack"
                    :disabled="respondingItems[item.id]"
                    @click="acknowledgeQueueItem(item)"
                  >Got it</button>
                </div>
                <div v-else class="ops-response-row">
                  <input
                    v-model="responseTexts[item.id]"
                    type="text"
                    placeholder="Type response..."
                    class="ops-response-input"
                    data-testid="queue-answer"
                    @keyup.enter="submitAnswer(item)"
                  />
                  <button
                    type="button"
                    class="ops-respond-btn"
                    data-testid="queue-answer-send"
                    :disabled="respondingItems[item.id] || !String(responseTexts[item.id] || '').trim()"
                    @click="submitAnswer(item)"
                  >Send</button>
                </div>
                <div v-if="respondErrors[item.id]" class="mt-2" data-testid="queue-respond-error">
                  <InlineError
                    :message="respondErrors[item.id].message"
                    :detail="respondErrors[item.id].detail"
                    @dismiss="dismissRespondError(item.id)"
                  />
                </div>
              </div>
            </div>
          </div>

          <!-- Notifications -->
          <div v-if="activeOpsTab === 'notifications'" class="ops-section">
            <InlineError
              v-if="notificationsView.stale"
              class="mb-3"
              :message="staleBannerMessage('notifications', lastLoadedAt.notifications)"
              :detail="fetchError.notifications"
              retryable
              @retry="fetchNotifications"
              @dismiss="fetchError.notifications = ''"
            />
            <div v-if="notificationsView.state === 'loading'" class="loading-state">Loading...</div>
            <LoadFailed
              v-else-if="notificationsView.state === 'failed'"
              dense
              title="Couldn't load notifications"
              message="The notification list didn't load. Try again."
              :detail="fetchError.notifications"
              :retrying="loading.notifications"
              @retry="fetchNotifications"
            />
            <div v-else-if="notificationsView.state === 'empty'" class="empty-state">No notifications</div>
            <div v-else class="ops-list">
              <div v-for="notif in notifications" :key="notif.id" class="ops-card">
                <div class="ops-card-header">
                  <span class="ops-agent-name" :title="agentNameTooltip(agentsStore.agentRefForSlug(notif.agent_name))">{{ notif.agent_name }}</span>
                  <span class="ops-priority" :class="'priority-' + notif.priority">{{ notif.priority }}</span>
                </div>
                <p class="ops-card-message">{{ notif.title || notif.message }}</p>
                <p v-if="notif.body" class="ops-card-body">{{ notif.body }}</p>
                <div class="ops-card-footer">
                  <span class="text-xs text-gray-500">{{ formatTime(notif.created_at) }}</span>
                  <button
                    v-if="notif.status === 'pending'"
                    @click="acknowledgeNotification(notif.id)"
                    class="ops-ack-btn"
                  >
                    Acknowledge
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- SYSTEM TAB -->
        <div v-if="activeTab === 'system'" class="tab-panel">
          <!-- Fleet Health Summary -->
          <div class="system-section">
            <h2 class="section-title">Fleet Health</h2>
            <InlineError
              v-if="fleetView.stale"
              class="mb-3"
              :message="staleBannerMessage('fleet health', lastLoadedAt.fleet)"
              :detail="fetchError.fleet"
              retryable
              @retry="fetchFleetHealth"
              @dismiss="fetchError.fleet = ''"
            />
            <div v-if="fleetView.state === 'loading'" class="loading-state">Loading...</div>
            <LoadFailed
              v-else-if="fleetView.state === 'failed'"
              dense
              title="Couldn't load fleet health"
              message="The fleet summary didn't load. Try again."
              :detail="fetchError.fleet"
              :retrying="loading.fleet"
              @retry="fetchFleetHealth"
            />
            <div v-else class="health-grid">
              <div class="health-card">
                <div class="health-value text-white">{{ fleetSummary.total }}</div>
                <div class="health-label">Total</div>
              </div>
              <div class="health-card">
                <div class="health-value text-status-success-400">{{ fleetSummary.running }}</div>
                <div class="health-label">Running</div>
              </div>
              <div class="health-card">
                <div class="health-value text-gray-400">{{ fleetSummary.stopped }}</div>
                <div class="health-label">Stopped</div>
              </div>
              <div class="health-card">
                <div class="health-value text-status-warning-400">{{ fleetSummary.high_context }}</div>
                <div class="health-label">High Ctx</div>
              </div>
            </div>
          </div>

          <!-- Quick Actions -->
          <div class="system-section">
            <h2 class="section-title">Actions</h2>
            <div class="actions-grid">
              <button @click="confirmAction('emergency-stop')" class="action-btn action-danger" :disabled="actionLoading">
                <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
                  <path stroke-linecap="round" stroke-linejoin="round" d="M9 10a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z"/>
                </svg>
                Emergency Stop
              </button>
              <button @click="confirmAction('fleet-restart')" class="action-btn action-warning" :disabled="actionLoading">
                <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
                </svg>
                Fleet Restart
              </button>
              <button @click="confirmAction('pause-schedules')" class="action-btn action-default" :disabled="actionLoading">
                <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M10 9v6m4-6v6m7-3a9 9 0 11-18 0 9 9 0 0118 0z"/>
                </svg>
                Pause Schedules
              </button>
              <button @click="confirmAction('resume-schedules')" class="action-btn action-default" :disabled="actionLoading">
                <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/>
                  <path stroke-linecap="round" stroke-linejoin="round" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
                </svg>
                Resume Schedules
              </button>
            </div>
          </div>

          <!-- Action Result -->
          <div v-if="actionResult" class="action-result" :class="actionResult.success ? 'result-success' : 'result-error'">
            {{ actionResult.message }}
          </div>
        </div>
      </main>

      <!-- Chat Overlay -->
      <div v-if="chatAgent" class="chat-overlay">
        <div class="chat-header">
          <button @click="closeChat" class="chat-back-btn">
            <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M15 19l-7-7 7-7"/>
            </svg>
          </button>
          <div class="chat-header-info">
            <span class="chat-header-name">{{ chatAgent }}</span>
            <span class="chat-header-status" :class="chatExecutionStatus === 'running' ? 'text-status-warning-400' : ''">
              {{ chatExecutionStatus === 'running' ? 'Thinking...' : 'Ready' }}
            </span>
          </div>
          <button @click="startNewChat" class="chat-new-btn">New</button>
        </div>

        <!-- Sessions dropdown -->
        <div v-if="showSessions" class="chat-sessions">
          <div
            v-for="s in chatSessions"
            :key="s.id"
            class="session-item"
            :class="{ active: s.id === chatSessionId }"
            @click="selectSession(s)"
          >
            <span class="session-preview">{{ s.last_message || 'Empty session' }}</span>
            <span class="session-meta">{{ s.message_count }} msgs &middot; {{ formatTime(s.started_at) }}</span>
          </div>
          <div v-if="chatSessions.length === 0" class="empty-state" style="padding: 16px;">No previous sessions</div>
        </div>
        <button @click="showSessions = !showSessions" class="sessions-toggle">
          {{ showSessions ? 'Hide' : 'Sessions' }} ({{ chatSessions.length }})
        </button>

        <!-- Messages -->
        <div class="chat-messages" ref="chatMessagesEl">
          <div v-if="chatMessages.length === 0" class="empty-state" style="padding: 40px 16px;">
            Send a message to start chatting
          </div>
          <div
            v-for="(msg, i) in chatMessages"
            :key="i"
            class="chat-bubble"
            :class="msg.role === 'user' ? 'bubble-user' : 'bubble-assistant'"
          >
            <div class="bubble-content">{{ msg.content }}</div>
            <div class="bubble-time">{{ formatTime(msg.timestamp) }}</div>
          </div>
          <div v-if="chatExecutionStatus === 'running'" class="chat-bubble bubble-assistant">
            <div class="bubble-content thinking-dots">
              <span></span><span></span><span></span>
            </div>
          </div>
        </div>

        <!-- Input -->
        <div class="chat-input-bar">
          <textarea
            v-model="chatInput"
            placeholder="Message..."
            class="chat-input"
            rows="1"
            @keydown.enter.exact.prevent="sendChatMessage"
            @input="autoResizeInput"
            ref="chatInputEl"
            :disabled="chatExecutionStatus === 'running'"
          ></textarea>
          <button
            @click="sendChatMessage"
            class="chat-send-btn"
            :disabled="!chatInput.trim() || chatExecutionStatus === 'running'"
          >
            <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M12 19V5m0 0l-7 7m7-7l7 7"/>
            </svg>
          </button>
        </div>
      </div>

      <!-- Confirmation Dialog -->
      <div v-if="confirmDialog" class="confirm-overlay" @click.self="confirmDialog = null">
        <div class="confirm-dialog">
          <h3 class="confirm-title">{{ confirmDialog.title }}</h3>
          <p class="confirm-message">{{ confirmDialog.message }}</p>
          <div class="confirm-actions">
            <button @click="confirmDialog = null" class="confirm-cancel">Cancel</button>
            <button @click="executeAction(confirmDialog.action)" class="confirm-execute" :class="confirmDialog.danger ? 'btn-danger' : 'btn-default'">
              {{ confirmDialog.confirmLabel }}
            </button>
          </div>
        </div>
      </div>

      <!-- Bottom Tab Bar -->
      <nav class="tab-bar">
        <button
          v-for="tab in tabs"
          :key="tab.id"
          @click="activeTab = tab.id"
          class="tab-item"
          :class="{ active: activeTab === tab.id }"
        >
          <div class="tab-icon-wrapper">
            <!-- Agents icon -->
            <svg v-if="tab.id === 'agents'" class="tab-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M15 19.128a9.38 9.38 0 002.625.372 9.337 9.337 0 004.121-.952 4.125 4.125 0 00-7.533-2.493M15 19.128v-.003c0-1.113-.285-2.16-.786-3.07M15 19.128v.106A12.318 12.318 0 018.624 21c-2.331 0-4.512-.645-6.374-1.766l-.001-.109a6.375 6.375 0 0111.964-3.07M12 6.375a3.375 3.375 0 11-6.75 0 3.375 3.375 0 016.75 0zm8.25 2.25a2.625 2.625 0 11-5.25 0 2.625 2.625 0 015.25 0z"/>
            </svg>
            <!-- Ops icon -->
            <svg v-if="tab.id === 'ops'" class="tab-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M2.25 13.5h3.86a2.25 2.25 0 012.012 1.244l.256.512a2.25 2.25 0 002.013 1.244h3.218a2.25 2.25 0 002.013-1.244l.256-.512a2.25 2.25 0 012.013-1.244h3.859m-19.5.338V18a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18v-4.162c0-.224-.034-.447-.1-.661L19.24 5.338a2.25 2.25 0 00-2.15-1.588H6.911a2.25 2.25 0 00-2.15 1.588L2.35 13.177a2.25 2.25 0 00-.1.661z"/>
            </svg>
            <!-- System icon -->
            <svg v-if="tab.id === 'system'" class="tab-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M5.25 14.25h13.5m-13.5 0a3 3 0 01-3-3m3 3a3 3 0 100 6h13.5a3 3 0 100-6m-16.5-3a3 3 0 013-3h13.5a3 3 0 013 3m-19.5 0a4.5 4.5 0 01.9-2.7L5.737 5.1a3.375 3.375 0 012.7-1.35h7.126c1.062 0 2.062.5 2.7 1.35l2.587 3.45a4.5 4.5 0 01.9 2.7m0 0a3 3 0 01-3 3m0 3h.008v.008h-.008v-.008zm0-6h.008v.008h-.008v-.008zm-3 6h.008v.008h-.008v-.008zm0-6h.008v.008h-.008v-.008z"/>
            </svg>
            <!-- Badge -->
            <span v-if="tab.badge > 0" class="tab-badge" :class="{ 'tab-badge-critical': tab.critical }">{{ tab.badge > 99 ? '99+' : tab.badge }}</span>
          </div>
          <span class="tab-label">{{ tab.label }}</span>
        </button>
      </nav>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch, reactive, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import { useAgentsStore } from '../stores/agents'
import { agentNameTooltip } from '../utils/agentName'
import { apiErrorMessage } from '../utils/apiError'
import { viewState, staleBannerMessage, listFrom } from '../utils/loadingState'
import {
  optionsOf, queueResponseKind, buildQueueResponse, queueTypeLabel,
  QUEUE_RESPONSE_NOT_RECORDED, respondRefusedAsNotPending,
} from '../utils/operatorQueue'
import LoadFailed from '../components/LoadFailed.vue'
import InlineError from '../components/InlineError.vue'

const route = useRoute()
const authStore = useAuthStore()
const agentsStore = useAgentsStore()

// ─── State ───────────────────────────────────────────────────────────────────

const activeTab = ref('agents')
const activeOpsTab = ref('queue')
const refreshing = ref(false)
const pullDistance = ref(0)
const pullRefreshing = ref(false)
const keyboardOpen = ref(false)
const scrollContainer = ref(null)

// Login
const loginPassword = ref('')
const loginLoading = ref(false)
const loginError = ref('')

// Agents
const agents = ref([])
const agentSearch = ref('')
const expandedAgent = ref(null)
const agentLogs = reactive({})
const togglingAgents = reactive({})
const togglingAutonomy = reactive({})

// Chat
const chatAgent = ref(null)
const chatInput = ref('')
const chatMessages = ref([])
const chatSessions = ref([])
const chatSessionId = ref(null)
const chatExecutionStatus = ref(null)
const showSessions = ref(false)
const chatMessagesEl = ref(null)
const chatInputEl = ref(null)

// Ops
const queueItems = ref([])
const notifications = ref([])
const responseTexts = reactive({})
const respondingItems = reactive({})
// #2370 — per-card answer state, keyed by item id (cards are keyed the same
// way, so a poll that swaps the list keeps a half-typed answer in place).
const selectedOptions = reactive({})   // approval cards: the tapped option
const respondErrors = reactive({})     // a failed send, shown NEXT TO the control (p18)
const cancelButtons = {}               // item id → Cancel element (focused on reveal, p19)

// System
const fleetSummary = ref({ total: 0, running: 0, stopped: 0, high_context: 0 })
const executionStats = ref({})
const confirmDialog = ref(null)
const actionLoading = ref(false)
const actionResult = ref(null)
// #1926: failed verbs (start/stop, autonomy, queue respond, acknowledge) land
// here instead of console.error, and stay until the operator dismisses them.
const actionError = ref('')
const actionErrorDetail = ref('')

// Loading states — `loading.*` stays "a fetch is in flight" (drives Retry
// labels). #1927: what the templates GATE on is "no data yet" — `hasLoaded.*`
// flips on the first SUCCEEDED fetch of each dataset, so the 15s poll swaps
// values in place instead of re-flashing "Loading…" every cycle (design-system
// p13/p14). `fetchError.*` with data on screen is the stale banner; with none it
// is the failed state — never the empty copy (p15).
const loading = reactive({
  agents: false,
  queue: false,
  notifications: false,
  fleet: false
})
const hasLoaded = reactive({ agents: false, queue: false, notifications: false, fleet: false })
const fetchError = reactive({ agents: '', queue: '', notifications: '', fleet: '' })
const lastLoadedAt = reactive({ agents: null, queue: null, notifications: null, fleet: null })

// Polling
let pollInterval = null

// ─── Computed ────────────────────────────────────────────────────────────────

// #1927: one rule (utils/loadingState.js) decides loading / failed / empty /
// ready + stale per dataset; the templates read these, never the raw flags.
const agentsView = computed(() => viewState({
  loading: loading.agents, hasLoaded: hasLoaded.agents, error: fetchError.agents, count: filteredAgents.value.length,
}))
const queueView = computed(() => viewState({
  loading: loading.queue, hasLoaded: hasLoaded.queue, error: fetchError.queue, count: queueItems.value.length,
}))
const notificationsView = computed(() => viewState({
  loading: loading.notifications, hasLoaded: hasLoaded.notifications, error: fetchError.notifications, count: notifications.value.length,
}))
const fleetView = computed(() => viewState({
  loading: loading.fleet, hasLoaded: hasLoaded.fleet, error: fetchError.fleet,
}))

const filteredAgents = computed(() => {
  let list = agents.value.filter(a => !a.is_system)
  if (agentSearch.value) {
    const q = agentSearch.value.toLowerCase()
    list = list.filter(a => a.name.toLowerCase().includes(q))
  }
  return list
})

const pendingQueueCount = computed(() => queueItems.value.filter(i => i.status === 'pending').length)
const pendingNotifCount = computed(() => notifications.value.filter(n => n.status === 'pending').length)

const opsTabs = computed(() => [
  { id: 'queue', label: 'Queue', count: pendingQueueCount.value },
  { id: 'notifications', label: 'Alerts', count: pendingNotifCount.value }
])

const tabs = computed(() => [
  { id: 'agents', label: 'Agents', badge: 0, critical: false },
  {
    id: 'ops',
    label: 'Ops',
    badge: pendingQueueCount.value + pendingNotifCount.value,
    critical: queueItems.value.some(i => i.priority === 'critical')
  },
  { id: 'system', label: 'System', badge: 0, critical: false }
])

// ─── Auth ────────────────────────────────────────────────────────────────────

async function handleLogin() {
  loginLoading.value = true
  loginError.value = ''
  const success = await authStore.loginWithCredentials('admin', loginPassword.value)
  if (!success) {
    loginError.value = authStore.authError || 'Invalid password'
  } else {
    loginPassword.value = ''
    loadAllData()
  }
  loginLoading.value = false
}

function handleLogout() {
  authStore.logout()
  stopPolling()
  resetQueueItemState()
}

// ─── Data Loading ────────────────────────────────────────────────────────────

async function fetchAgents() {
  loading.agents = true
  try {
    // #1927: fleet + autonomy are REQUIRED (autonomy feeds the rendered toggle);
    // execution stats are decorative, so a failing stats call must not fail the
    // tab. `allSettled` instead of `all` for exactly that split.
    const [fleetRes, autonomyRes, statsRes] = await Promise.allSettled([
      axios.get('/api/ops/fleet/status'),
      axios.get('/api/agents/autonomy-status'),
      axios.get('/api/agents/execution-stats', { params: { include_7d: true } })
    ])
    if (fleetRes.status !== 'fulfilled') throw fleetRes.reason
    if (autonomyRes.status !== 'fulfilled') throw autonomyRes.reason
    const autonomyMap = autonomyRes.value.data || {}
    const agentList = (fleetRes.value.data.agents || []).map(a => ({
      ...a,
      autonomy_enabled: autonomyMap[a.name]?.autonomy_enabled || false
    }))
    agents.value = agentList
    fleetSummary.value = fleetRes.value.data.summary || { total: 0, running: 0, stopped: 0, high_context: 0 }
    // Both datasets this response writes are now loaded (the System tab's fleet
    // summary has two writers — every writer marks it, or its first poll strobes).
    hasLoaded.agents = true
    hasLoaded.fleet = true
    lastLoadedAt.agents = Date.now()
    lastLoadedAt.fleet = lastLoadedAt.agents
    fetchError.agents = ''
    if (statsRes.status === 'fulfilled') {
      // The endpoint returns {agents:[…]} — this loop used to iterate the object
      // itself and throw on every poll (after the list was already written).
      const statsMap = {}
      for (const stat of listFrom(statsRes.value.data, 'agents')) {
        statsMap[stat.name] = stat
      }
      executionStats.value = statsMap
    } else {
      console.error('Failed to fetch execution stats:', statsRes.reason)
    }
  } catch (e) {
    console.error('Failed to fetch agents:', e)
    // Data already on screen stays; the template renders failed (no data) or the
    // stale banner (data) from this field.
    fetchError.agents = apiErrorMessage(e, 'Request failed')
  } finally {
    loading.agents = false
  }
}

// #2370: a poll issued BEFORE an answer's POST can complete AFTER the
// success-path refetch and rewrite the list with the answered card still
// pending. Only the newest fetch may write — and that holds on EVERY exit,
// not just the success one: a superseded poll that rejects on a transient
// blip would otherwise paint "couldn't refresh" over a list the newer fetch
// just proved fresh, and drop `loading.queue` while that newer request is
// still outstanding.
let queueFetchSeq = 0

async function fetchQueue() {
  const seq = ++queueFetchSeq
  loading.queue = true
  try {
    const res = await axios.get('/api/operator-queue', { params: { limit: 100 } })
    if (seq !== queueFetchSeq) return // a newer fetch owns the list now
    // The endpoint returns {items, count}; `(res.data || []).filter` on that
    // object threw on every poll, so this tab always read "No pending items".
    queueItems.value = listFrom(res.data, 'items').filter(i => i.status === 'pending')
    pruneQueueItemState(queueItems.value)
    hasLoaded.queue = true
    lastLoadedAt.queue = Date.now()
    fetchError.queue = ''
  } catch (e) {
    if (seq !== queueFetchSeq) return // superseded — its failure is not news
    console.error('Failed to fetch queue:', e)
    fetchError.queue = apiErrorMessage(e, 'Request failed')
  } finally {
    // The newest fetch owns the spinner; a superseded one leaves it to the
    // request that is still running (`return` in try/catch runs this block).
    if (seq === queueFetchSeq) loading.queue = false
  }
}

async function fetchNotifications() {
  loading.notifications = true
  try {
    const res = await axios.get('/api/notifications', { params: { status: 'pending', limit: 100 } })
    notifications.value = listFrom(res.data, 'notifications')
    hasLoaded.notifications = true
    lastLoadedAt.notifications = Date.now()
    fetchError.notifications = ''
  } catch (e) {
    console.error('Failed to fetch notifications:', e)
    fetchError.notifications = apiErrorMessage(e, 'Request failed')
  } finally {
    loading.notifications = false
  }
}

async function fetchFleetHealth() {
  loading.fleet = true
  try {
    const res = await axios.get('/api/ops/fleet/status')
    fleetSummary.value = res.data.summary || { total: 0, running: 0, stopped: 0, high_context: 0 }
    hasLoaded.fleet = true
    lastLoadedAt.fleet = Date.now()
    fetchError.fleet = ''
  } catch (e) {
    console.error('Failed to fetch fleet health:', e)
    fetchError.fleet = apiErrorMessage(e, 'Request failed')
  } finally {
    loading.fleet = false
  }
}

async function fetchAgentLogs(name) {
  try {
    const res = await axios.get(`/api/agents/${name}/logs`, { params: { tail: 30 } })
    agentLogs[name] = res.data.logs || 'No logs available'
  } catch (e) {
    agentLogs[name] = 'Failed to load logs'
  }
}

function loadAllData() {
  fetchAgents()
  fetchQueue()
  fetchNotifications()
}

async function refreshCurrentTab() {
  refreshing.value = true
  if (activeTab.value === 'agents') await fetchAgents()
  else if (activeTab.value === 'ops') { await fetchQueue(); await fetchNotifications() }
  else if (activeTab.value === 'system') { await fetchFleetHealth() }
  refreshing.value = false
}

// ─── Agent Actions ───────────────────────────────────────────────────────────

function toggleAgentExpand(name) {
  expandedAgent.value = expandedAgent.value === name ? null : name
}

// #1926 — one persistent, dismissible surface for every failed verb on this
// screen. console.error alone is invisible on a phone, where there is no
// devtools pane to open.
function clearActionError() {
  actionError.value = ''
  actionErrorDetail.value = ''
}

function reportActionFailure(e, what) {
  console.error(`Failed to ${what}:`, e)
  actionError.value = `Couldn't ${what}. Nothing was changed — try again.`
  actionErrorDetail.value = apiErrorMessage(e, 'Request failed')
}

async function toggleAgent(name, currentStatus) {
  togglingAgents[name] = true
  try {
    if (currentStatus === 'running') {
      await axios.post(`/api/agents/${name}/stop`)
    } else {
      await axios.post(`/api/agents/${name}/start`)
    }
    await fetchAgents()
  } catch (e) {
    reportActionFailure(e, `${currentStatus === 'running' ? 'stop' : 'start'} ${name}`)
  } finally {
    togglingAgents[name] = false
  }
}

async function toggleAutonomy(agent) {
  togglingAutonomy[agent.name] = true
  try {
    const newState = !agent.autonomy_enabled
    await axios.put(`/api/agents/${agent.name}/autonomy`, { enabled: newState })
    agent.autonomy_enabled = newState
  } catch (e) {
    // The toggle reverts to the server value, so without this the switch just
    // springs back with no reason given (#1926).
    reportActionFailure(e, `change autonomy for ${agent.name}`)
  } finally {
    togglingAutonomy[agent.name] = false
  }
}

// ─── Chat ────────────────────────────────────────────────────────────────────

async function openChat(agent) {
  chatAgent.value = agent.name
  chatMessages.value = []
  chatSessionId.value = null
  chatInput.value = ''
  chatExecutionStatus.value = null
  showSessions.value = false
  stopPolling()
  await loadChatSessions()
  // Auto-select most recent active session
  if (chatSessions.value.length > 0) {
    const active = chatSessions.value.find(s => s.status === 'active')
    if (active) await selectSession(active)
  }
  await nextTick()
  if (chatInputEl.value) chatInputEl.value.focus()
}

function closeChat() {
  chatAgent.value = null
  chatMessages.value = []
  chatSessionId.value = null
  chatExecutionStatus.value = null
  startPolling()
}

function startNewChat() {
  chatMessages.value = []
  chatSessionId.value = null
  chatExecutionStatus.value = null
  showSessions.value = false
}

async function loadChatSessions() {
  try {
    const res = await axios.get(`/api/agents/${chatAgent.value}/chat/sessions`)
    chatSessions.value = res.data.sessions || []
  } catch (e) {
    chatSessions.value = []
  }
}

async function selectSession(session) {
  chatSessionId.value = session.id
  showSessions.value = false
  try {
    const res = await axios.get(`/api/agents/${chatAgent.value}/chat/sessions/${session.id}`)
    chatMessages.value = res.data.messages || []
    scrollChatToBottom()
  } catch (e) {
    console.error('Failed to load session:', e)
  }
}

function buildContextPrompt(userMessage) {
  // Include last 10 exchanges for context
  const recent = chatMessages.value.slice(-20)
  if (recent.length === 0) return userMessage
  let context = 'Previous conversation:\n'
  recent.forEach(m => {
    context += `${m.role === 'user' ? 'User' : 'Assistant'}: ${m.content}\n`
  })
  context += `\nUser: ${userMessage}`
  return context
}

async function sendChatMessage() {
  const message = chatInput.value.trim()
  if (!message || chatExecutionStatus.value === 'running') return

  chatInput.value = ''
  if (chatInputEl.value) {
    chatInputEl.value.style.height = 'auto'
  }

  // Add user message immediately
  chatMessages.value.push({
    role: 'user',
    content: message,
    timestamp: new Date().toISOString()
  })
  scrollChatToBottom()

  chatExecutionStatus.value = 'running'

  try {
    const contextPrompt = buildContextPrompt(message)
    const payload = {
      message: contextPrompt,
      save_to_session: true,
      user_message: message,
      create_new_session: !chatSessionId.value,
      chat_session_id: chatSessionId.value || undefined,
      async_mode: true
    }

    const submitRes = await axios.post(`/api/agents/${chatAgent.value}/task`, payload)
    const executionId = submitRes.data.execution_id

    // Poll for completion
    const result = await pollExecution(chatAgent.value, executionId)

    if (result?.status === 'success' && result.response) {
      chatMessages.value.push({
        role: 'assistant',
        content: result.response,
        timestamp: new Date().toISOString()
      })
    } else if (result?.status === 'failed') {
      chatMessages.value.push({
        role: 'assistant',
        content: `Error: ${result.error || 'Task failed'}`,
        timestamp: new Date().toISOString()
      })
    }

    // Refresh sessions to pick up new session ID
    await loadChatSessions()
    if (!chatSessionId.value && chatSessions.value.length > 0) {
      chatSessionId.value = chatSessions.value[0].id
    }
  } catch (e) {
    chatMessages.value.push({
      role: 'assistant',
      content: `Error: ${e.response?.data?.detail || e.message || 'Failed to send message'}`,
      timestamp: new Date().toISOString()
    })
  } finally {
    chatExecutionStatus.value = null
    scrollChatToBottom()
  }
}

async function pollExecution(agentName, executionId) {
  const maxAttempts = 120  // 10 minutes at 5s intervals
  for (let i = 0; i < maxAttempts; i++) {
    await new Promise(r => setTimeout(r, 5000))
    try {
      const res = await axios.get(`/api/agents/${agentName}/executions/${executionId}`)
      const exec = res.data
      if (exec.status && exec.status !== 'running' && exec.status !== 'pending') {
        return exec
      }
    } catch (e) {
      console.error('Poll error:', e)
    }
  }
  return { status: 'failed', error: 'Timed out waiting for response' }
}

function scrollChatToBottom() {
  nextTick(() => {
    if (chatMessagesEl.value) {
      chatMessagesEl.value.scrollTop = chatMessagesEl.value.scrollHeight
    }
  })
}

function autoResizeInput(e) {
  const el = e.target
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 120) + 'px'
}

// ─── Ops Actions ─────────────────────────────────────────────────────────────

// #2370 — queue answers. `response` carries the DECISION (the tapped option,
// the typed answer, or `acknowledged`); a note rides `response_text`. The body
// comes from utils/operatorQueue.js — the builder the desktop store uses —
// because this view used to hand-build it with a hard-coded literal decision
// ('approved', whatever was tapped) and so recorded a Deny as an approval.

function registerCancelButton(id, el) {
  if (el) cancelButtons[id] = el
  else delete cancelButtons[id]
}

function selectOption(id, opt) {
  if (respondingItems[id]) return
  selectedOptions[id] = opt
  delete respondErrors[id]
  // p19: the safe action is focused first. Focusing a button pops no keyboard
  // and draws no ring after a touch (:focus-visible), so this is invisible on
  // a phone and load-bearing for keyboard / screen-reader users.
  nextTick(() => cancelButtons[id]?.focus({ preventScroll: true }))
}

function clearSelection(id) {
  if (respondingItems[id]) return
  delete selectedOptions[id]
}

function dismissRespondError(id) {
  delete respondErrors[id]
}

function clearQueueItemState(id) {
  delete selectedOptions[id]
  delete responseTexts[id]
  delete respondErrors[id]
  delete respondingItems[id]
}

// Drop per-card state for cards that left the list (answered elsewhere,
// expired, cancelled) so a long-lived PWA tab does not accumulate it.
//
// A card whose POST is still outstanding is NEVER pruned. `respondingItems` is
// the in-flight guard `sendQueueResponse` checks, and the GET is a `limit: 100`
// window ordered status → priority → created_at: on a busy queue an item can be
// pushed out of that window by newer high-priority arrivals and return once
// they are answered. Clearing its guard mid-flight re-enables Send under an
// outstanding POST — the server 400s the loser, so nothing is mis-recorded, but
// the operator is told an answer that WAS recorded was not. The selection and
// note are held for the same reason: the retryable-failure path keeps them for
// a second attempt.
function pruneQueueItemState(items) {
  const live = new Set(items.map(i => i.id))
  for (const map of [selectedOptions, responseTexts, respondErrors, respondingItems]) {
    for (const id of Object.keys(map)) {
      if (live.has(id) || respondingItems[id] === true) continue
      delete map[id]
    }
  }
}

// Logout forgets everything, in-flight included — the session is over, and a
// guard surviving into the next sign-in would leave that card's Send disabled
// with no way to clear it. It therefore clears the maps directly instead of
// delegating to the prune above, which would inherit that exemption: this is a
// wipe, not a reconcile against a served list.
function resetQueueItemState() {
  for (const map of [selectedOptions, responseTexts, respondErrors, respondingItems]) {
    for (const id of Object.keys(map)) delete map[id]
  }
}

async function sendQueueResponse(item, body) {
  const id = item.id
  if (!body || respondingItems[id]) return false
  respondingItems[id] = true
  delete respondErrors[id]
  try {
    await axios.post(`/api/operator-queue/${id}/respond`, body)
  } catch (e) {
    console.error('Failed to send queue response:', e?.response?.status ?? e?.message ?? e)
    respondingItems[id] = false
    if (respondRefusedAsNotPending(e)) {
      // 409 (somebody else resolved it first, #1017), 400 (already terminal)
      // or 404 (row gone): the answer was NOT recorded and the item is not
      // waiting for it, so drop the card now (the server said so — a failed
      // refetch must not leave it tappable under the notice) and put the
      // notice on the persistent page-level banner, brought into view — a
      // per-card message would vanish with the card.
      queueItems.value = queueItems.value.filter(i => i.id !== id)
      clearQueueItemState(id)
      actionError.value = QUEUE_RESPONSE_NOT_RECORDED
      actionErrorDetail.value = apiErrorMessage(e, 'Request failed')
      scrollContainer.value?.scrollTo?.({ top: 0, behavior: 'smooth' })
      await fetchQueue()
    } else {
      // The operator believes they answered the agent. They did not — say so
      // NEXT TO the control (p18) and keep the selection + note for a retry.
      // No "nothing was changed" claim: a timed-out POST may have landed.
      respondErrors[id] = {
        message: "Couldn't send your response — the agent is still waiting. Try again.",
        detail: apiErrorMessage(e, 'Request failed'),
      }
    }
    return false
  }
  // Success: drop the card NOW. `fetchQueue` swallows its own errors, so a
  // failed refetch must never leave an answered card looking pending.
  queueItems.value = queueItems.value.filter(i => i.id !== id)
  clearQueueItemState(id)
  await fetchQueue()
  return true
}

function submitApproval(item) {
  return sendQueueResponse(item, buildQueueResponse({
    kind: 'approval', option: selectedOptions[item.id], note: responseTexts[item.id],
  }))
}

function submitAnswer(item) {
  return sendQueueResponse(item, buildQueueResponse({ kind: 'question', answer: responseTexts[item.id] }))
}

function acknowledgeQueueItem(item) {
  return sendQueueResponse(item, buildQueueResponse({ kind: 'acknowledge' }))
}

async function acknowledgeNotification(id) {
  try {
    await axios.post(`/api/notifications/${id}/acknowledge`)
    await fetchNotifications()
  } catch (e) {
    reportActionFailure(e, 'acknowledge that notification')
  }
}

// ─── System Actions ──────────────────────────────────────────────────────────

function confirmAction(action) {
  const configs = {
    'emergency-stop': {
      title: 'Emergency Stop',
      message: 'This will pause ALL schedules and stop ALL running agents. Are you sure?',
      confirmLabel: 'Emergency Stop',
      danger: true
    },
    'fleet-restart': {
      title: 'Fleet Restart',
      message: 'This will restart all running agents. They will be briefly unavailable.',
      confirmLabel: 'Restart All',
      danger: false
    },
    'pause-schedules': {
      title: 'Pause Schedules',
      message: 'This will pause all enabled schedules across all agents.',
      confirmLabel: 'Pause All',
      danger: false
    },
    'resume-schedules': {
      title: 'Resume Schedules',
      message: 'This will resume all paused schedules across all agents.',
      confirmLabel: 'Resume All',
      danger: false
    }
  }
  confirmDialog.value = { ...configs[action], action }
}

async function executeAction(action) {
  confirmDialog.value = null
  actionLoading.value = true
  actionResult.value = null

  try {
    let res
    if (action === 'emergency-stop') {
      res = await axios.post('/api/ops/emergency-stop')
    } else if (action === 'fleet-restart') {
      res = await axios.post('/api/ops/fleet/restart')
    } else if (action === 'pause-schedules') {
      res = await axios.post('/api/ops/schedules/pause')
    } else if (action === 'resume-schedules') {
      res = await axios.post('/api/ops/schedules/resume')
    }
    actionResult.value = { success: true, message: res.data.message || 'Action completed' }
    await fetchFleetHealth()
    await fetchAgents()
  } catch (e) {
    actionResult.value = { success: false, message: e.response?.data?.detail || 'Action failed' }
  } finally {
    actionLoading.value = false
    setTimeout(() => { actionResult.value = null }, 5000)
  }
}

// ─── Pull to Refresh ─────────────────────────────────────────────────────────

let touchStartY = 0
let isPulling = false

function onTouchStart(e) {
  const el = scrollContainer.value
  if (el && el.scrollTop === 0) {
    touchStartY = e.touches[0].clientY
    isPulling = true
  }
}

function onTouchMove(e) {
  if (!isPulling) return
  const diff = e.touches[0].clientY - touchStartY
  if (diff > 0 && diff < 120) {
    pullDistance.value = diff * 0.5
  }
}

async function onTouchEnd() {
  if (pullDistance.value > 40) {
    pullRefreshing.value = true
    await refreshCurrentTab()
    pullRefreshing.value = false
  }
  pullDistance.value = 0
  isPulling = false
}

// ─── Keyboard Handling ───────────────────────────────────────────────────────

function onViewportResize() {
  if (window.visualViewport) {
    keyboardOpen.value = window.visualViewport.height < window.innerHeight * 0.75
  }
}

// ─── PWA Setup ───────────────────────────────────────────────────────────────

function setupPWA() {
  // Manifest
  const manifestLink = document.createElement('link')
  manifestLink.rel = 'manifest'
  manifestLink.href = '/mobile-manifest.json'
  document.head.appendChild(manifestLink)

  // iOS meta tags
  const metaTags = [
    { name: 'apple-mobile-web-app-capable', content: 'yes' },
    { name: 'apple-mobile-web-app-status-bar-style', content: 'black-translucent' },
    { name: 'apple-mobile-web-app-title', content: 'Trinity' },
    { name: 'theme-color', content: '#111827' }
  ]
  metaTags.forEach(({ name, content }) => {
    const meta = document.createElement('meta')
    meta.name = name
    meta.content = content
    meta.dataset.mobilePwa = 'true'
    document.head.appendChild(meta)
  })

  // Apple touch icon
  const touchIcon = document.createElement('link')
  touchIcon.rel = 'apple-touch-icon'
  touchIcon.href = '/icons/apple-touch-icon-mobile.png'
  touchIcon.dataset.mobilePwa = 'true'
  document.head.appendChild(touchIcon)

  // Update viewport for safe areas
  const viewport = document.querySelector('meta[name="viewport"]')
  if (viewport) {
    viewport.content = 'width=device-width, initial-scale=1.0, viewport-fit=cover, maximum-scale=1'
  }

  // Register service worker
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.getRegistrations().then(registrations => {
      registrations.forEach(reg => {
        if (reg.active && reg.active.scriptURL.includes('mobile-sw.js')) {
          reg.update()
        }
      })
    })
    navigator.serviceWorker.register('/mobile-sw.js?v=1').catch(err => {
      console.warn('SW registration failed:', err)
    })
  }
}

function cleanupPWA() {
  // Remove dynamically added PWA elements
  document.querySelectorAll('[data-mobile-pwa]').forEach(el => el.remove())
  const manifestLink = document.querySelector('link[href="/mobile-manifest.json"]')
  if (manifestLink) manifestLink.remove()

  // Restore viewport
  const viewport = document.querySelector('meta[name="viewport"]')
  if (viewport) {
    viewport.content = 'width=device-width, initial-scale=1.0'
  }
}

// ─── Polling ─────────────────────────────────────────────────────────────────

function startPolling() {
  stopPolling()
  pollInterval = setInterval(() => {
    if (activeTab.value === 'agents') fetchAgents()
    else if (activeTab.value === 'ops') { fetchQueue(); fetchNotifications() }
    else if (activeTab.value === 'system') fetchFleetHealth()
  }, 15000)
}

function stopPolling() {
  if (pollInterval) {
    clearInterval(pollInterval)
    pollInterval = null
  }
}

// ─── Success Rate Helpers ────────────────────────────────────────────────────

function getAgentSuccessPercent(agentName) {
  const stats = executionStats.value[agentName]
  if (stats && stats.task_count_24h > 0) {
    return Math.round(stats.success_rate || 0)
  }
  if (stats && (stats.task_count_7d || 0) > 0) {
    return Math.round(stats.success_rate_7d || 0)
  }
  return 0
}

function getSuccessBarClass(percent) {
  if (percent >= 90) return 'bar-green'
  if (percent >= 50) return 'bar-yellow'
  return 'bar-red'
}

function getSuccessTextClass(percent) {
  if (percent >= 90) return 'text-green'
  if (percent >= 50) return 'text-yellow'
  return 'text-red'
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function formatTime(dateStr) {
  if (!dateStr) return ''
  const d = new Date(dateStr)
  const now = new Date()
  const diff = now - d
  if (diff < 60000) return 'just now'
  if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago'
  if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago'
  return d.toLocaleDateString()
}

// ─── Tab query param ─────────────────────────────────────────────────────────

watch(() => route.query.tab, (tab) => {
  if (tab && ['agents', 'ops', 'system'].includes(tab)) {
    activeTab.value = tab
  }
}, { immediate: true })

// ─── Lifecycle ───────────────────────────────────────────────────────────────

onMounted(() => {
  // Force dark mode
  document.documentElement.classList.add('dark')

  setupPWA()

  if (authStore.isAuthenticated) {
    loadAllData()
    startPolling()
  }

  // Pull to refresh
  document.addEventListener('touchstart', onTouchStart, { passive: true })
  document.addEventListener('touchmove', onTouchMove, { passive: true })
  document.addEventListener('touchend', onTouchEnd, { passive: true })

  // Keyboard detection
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', onViewportResize)
  }
})

onUnmounted(() => {
  cleanupPWA()
  stopPolling()
  chatAgent.value = null

  document.removeEventListener('touchstart', onTouchStart)
  document.removeEventListener('touchmove', onTouchMove)
  document.removeEventListener('touchend', onTouchEnd)

  if (window.visualViewport) {
    window.visualViewport.removeEventListener('resize', onViewportResize)
  }
})

watch(() => authStore.isAuthenticated, (isAuth) => {
  if (isAuth) {
    loadAllData()
    startPolling()
  } else {
    stopPolling()
  }
})
</script>

<style scoped>
/* ─── Base ──────────────────────────────────────────────────────────────── */

.mobile-admin {
  position: fixed;
  top: env(safe-area-inset-top);
  bottom: 0;
  left: env(safe-area-inset-left);
  right: env(safe-area-inset-right);
  background: #111827;
  color: #e5e7eb;
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro', system-ui, sans-serif;
  font-size: 16px;
  -webkit-font-smoothing: antialiased;
  -webkit-text-size-adjust: 100%;
  touch-action: manipulation;
  -webkit-tap-highlight-color: transparent;
  overscroll-behavior: none;
}

/* ─── Login ─────────────────────────────────────────────────────────────── */

.login-screen {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  padding: 24px;
}

.login-container {
  width: 100%;
  max-width: 320px;
}

.login-logo {
  text-align: center;
  margin-bottom: 32px;
}

.login-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.login-input {
  width: 100%;
  padding: 14px 16px;
  background: #1f2937;
  border: 1px solid #374151;
  border-radius: 12px;
  color: white;
  font-size: 16px;
  outline: none;
  box-sizing: border-box;
}

.login-input:focus {
  border-color: #6366f1;
}

.login-button {
  padding: 14px;
  background: #6366f1;
  border: none;
  border-radius: 12px;
  color: white;
  font-size: 16px;
  font-weight: 600;
  cursor: pointer;
}

.login-button:disabled {
  opacity: 0.5;
}

.login-error {
  color: #f87171;
  font-size: 14px;
  text-align: center;
}

/* ─── App Layout ────────────────────────────────────────────────────────── */

.app-container {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  background: #1f2937;
  border-bottom: 1px solid #374151;
  flex-shrink: 0;
}

.header-actions {
  display: flex;
  gap: 8px;
}

.header-btn {
  padding: 8px;
  background: none;
  border: none;
  color: #9ca3af;
  cursor: pointer;
  border-radius: 8px;
}

.header-btn:active {
  background: #374151;
}

/* ─── Tab Content ───────────────────────────────────────────────────────── */

.tab-content {
  flex: 1;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  overscroll-behavior-y: contain;
}

.tab-panel {
  padding: 12px;
  padding-bottom: 80px;
}

.pull-indicator {
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}

/* ─── Search ────────────────────────────────────────────────────────────── */

.search-bar {
  margin-bottom: 12px;
}

.search-input {
  width: 100%;
  padding: 10px 14px;
  background: #1f2937;
  border: 1px solid #374151;
  border-radius: 10px;
  color: white;
  font-size: 16px;
  outline: none;
  box-sizing: border-box;
}

.search-input:focus {
  border-color: #6366f1;
}

/* ─── Agent Cards ───────────────────────────────────────────────────────── */

.agent-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.agent-card {
  background: #1f2937;
  border-radius: 12px;
  padding: 14px;
  cursor: pointer;
}

.agent-card:active {
  background: #263040;
}

.agent-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.agent-info {
  min-width: 0;
  flex: 1;
}

.agent-name {
  font-weight: 600;
  font-size: 15px;
  color: white;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.agent-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.agent-actions {
  margin-left: 12px;
  flex-shrink: 0;
}

.toggle-btn {
  padding: 6px 14px;
  border: none;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  min-width: 60px;
}

.toggle-start {
  background: #065f46;
  color: #6ee7b7;
}

.toggle-stop {
  background: #7f1d1d;
  color: #fca5a5;
}

.toggle-btn:disabled {
  opacity: 0.6;
}

/* Agent details */
.agent-details {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid #374151;
}

.detail-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 13px;
}

.detail-label {
  color: #9ca3af;
  min-width: 56px;
}

.context-bar-container {
  flex: 1;
  height: 6px;
  background: #374151;
  border-radius: 3px;
  overflow: hidden;
}

.context-bar {
  height: 100%;
  border-radius: 3px;
  transition: width 0.3s;
}

.logs-view {
  margin-top: 8px;
  padding: 10px;
  background: #0f172a;
  border-radius: 8px;
  font-family: 'SF Mono', 'Menlo', monospace;
  font-size: 11px;
  line-height: 1.5;
  color: #94a3b8;
  overflow-x: auto;
  max-height: 200px;
  white-space: pre-wrap;
  word-break: break-all;
}

/* ─── Ops ───────────────────────────────────────────────────────────────── */

.sub-tabs {
  display: flex;
  gap: 4px;
  margin-bottom: 12px;
  background: #1f2937;
  border-radius: 10px;
  padding: 4px;
}

.sub-tab {
  flex: 1;
  padding: 8px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: #9ca3af;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  position: relative;
}

.sub-tab.active {
  background: #374151;
  color: white;
}

.sub-tab-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  background: #ef4444;
  border-radius: 9px;
  font-size: 11px;
  font-weight: 700;
  color: white;
  margin-left: 4px;
}

.ops-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.ops-card {
  background: #1f2937;
  border-radius: 12px;
  padding: 14px;
}

.ops-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}

.ops-agent-name {
  font-weight: 600;
  font-size: 14px;
  color: white;
}

.ops-priority {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 6px;
  font-weight: 600;
  text-transform: uppercase;
}

.priority-critical { background: #7f1d1d; color: #fca5a5; }
.priority-high { background: #78350f; color: #fcd34d; }
.priority-normal { background: #1e3a5f; color: #93c5fd; }
.priority-low { background: #374151; color: #9ca3af; }

.ops-card-type {
  font-size: 12px;
  color: #9ca3af; /* issue 2370: gray-500 is the dark-ink floor, never meta text */
  margin-bottom: 6px;
}

.ops-card-message {
  font-size: 14px;
  color: #d1d5db;
  line-height: 1.4;
  margin-bottom: 10px;
}

.ops-card-body {
  font-size: 13px;
  color: #9ca3af;
  margin-bottom: 8px;
}

.ops-options {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.ops-option-btn {
  padding: 8px 16px;
  background: #374151;
  border: none;
  border-radius: 8px;
  color: white;
  font-size: 14px;
  cursor: pointer;
  /* issue 2370: options are agent-authored and may be sentence-length — wrap, never clip */
  max-width: 100%;
  white-space: normal;
  overflow-wrap: anywhere;
  text-align: left;
}

.ops-option-btn:active {
  background: #4b5563;
}

/* issue 2370: an option button SELECTS, it never sends. The selected state is
   colour-free (an inset ring in currentColor — the raw-colour ratchet allows no
   new literal here) and uses box-shadow so `outline` stays the focus indicator. */
.ops-option-btn[aria-pressed="true"] {
  box-shadow: inset 0 0 0 2px currentColor;
  font-weight: 600;
}

.ops-card-title {
  font-size: 14px;
  font-weight: 600;
  line-height: 1.35;
  margin-bottom: 4px;
}

.ops-approval-form {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 10px;
}

/* actions right-aligned, safe choice first (left), destructive last */
.ops-approval-form .ops-response-row {
  justify-content: flex-end;
}

.ops-send-btn {
  max-width: 100%;
  white-space: normal;
  overflow-wrap: anywhere;
}

.ops-card-footer.ops-ack-only {
  justify-content: flex-end;
}

.ops-response-row {
  display: flex;
  gap: 8px;
}

.ops-response-input {
  flex: 1;
  padding: 10px 12px;
  background: #0f172a;
  border: 1px solid #374151;
  border-radius: 8px;
  color: white;
  font-size: 16px;
  outline: none;
}

.ops-response-input:focus {
  border-color: #6366f1;
}

.ops-respond-btn {
  padding: 10px 16px;
  background: #6366f1;
  border: none;
  border-radius: 8px;
  color: white;
  font-weight: 600;
  font-size: 14px;
  cursor: pointer;
}

.ops-respond-btn:disabled {
  opacity: 0.5;
}

.ops-card-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 8px;
}

.ops-ack-btn {
  padding: 6px 12px;
  background: #374151;
  border: none;
  border-radius: 6px;
  color: #818cf8;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
}

/* ─── System ────────────────────────────────────────────────────────────── */

.system-section {
  margin-bottom: 20px;
}

.section-title {
  font-size: 14px;
  font-weight: 600;
  color: #9ca3af;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 10px;
}

.health-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
}

.health-card {
  background: #1f2937;
  border-radius: 12px;
  padding: 12px 8px;
  text-align: center;
}

.health-value {
  font-size: 24px;
  font-weight: 700;
  line-height: 1;
}

.health-label {
  font-size: 11px;
  color: #6b7280;
  margin-top: 4px;
}

.actions-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}

.action-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 14px 12px;
  border: none;
  border-radius: 12px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  color: white;
}

.action-btn:disabled {
  opacity: 0.5;
}

.action-danger {
  background: #7f1d1d;
}

.action-danger:active {
  background: #991b1b;
}

.action-warning {
  background: #78350f;
}

.action-warning:active {
  background: #92400e;
}

.action-default {
  background: #1f2937;
  border: 1px solid #374151;
}

.action-default:active {
  background: #374151;
}

.action-result {
  margin-top: 12px;
  padding: 12px;
  border-radius: 10px;
  font-size: 14px;
  text-align: center;
}

.result-success {
  background: #065f46;
  color: #6ee7b7;
}

.result-error {
  background: #7f1d1d;
  color: #fca5a5;
}

/* #1926: persistent failed-verb banner (see the template note). Layout only —
   it composes `.result-error` for the palette, so failure reads the same
   everywhere on this screen and no new hardcoded color enters the ratchet. */
.action-error {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin: 0 0 12px;
  padding: 12px;
  border-radius: 10px;
  font-size: 14px;
  text-align: left;
}

.action-error-body {
  flex: 1;
  min-width: 0;
}

.action-error-detail {
  margin-top: 4px;
  font-size: 12px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  opacity: 0.85;
  overflow-wrap: anywhere;
}

.action-error-dismiss {
  flex-shrink: 0;
  background: none;
  border: none;
  color: inherit;
  font-size: 16px;
  line-height: 1;
  padding: 2px 4px;
  min-width: 32px;
  min-height: 32px;
}

/* ─── Confirm Dialog ────────────────────────────────────────────────────── */

.confirm-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.7);
  display: flex;
  align-items: flex-end;
  justify-content: center;
  z-index: 100;
  padding: 16px;
  padding-bottom: calc(16px + env(safe-area-inset-bottom));
}

.confirm-dialog {
  width: 100%;
  max-width: 400px;
  background: #1f2937;
  border-radius: 16px;
  padding: 20px;
}

.confirm-title {
  font-size: 18px;
  font-weight: 700;
  color: white;
  margin-bottom: 8px;
}

.confirm-message {
  font-size: 14px;
  color: #9ca3af;
  line-height: 1.5;
  margin-bottom: 20px;
}

.confirm-actions {
  display: flex;
  gap: 10px;
}

.confirm-cancel {
  flex: 1;
  padding: 12px;
  background: #374151;
  border: none;
  border-radius: 10px;
  color: white;
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
}

.confirm-execute {
  flex: 1;
  padding: 12px;
  border: none;
  border-radius: 10px;
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
  color: white;
}

.btn-danger {
  background: #dc2626;
}

.btn-default {
  background: #6366f1;
}

/* ─── Bottom Tab Bar ────────────────────────────────────────────────────── */

.tab-bar {
  display: flex;
  background: #1f2937;
  border-top: 1px solid #374151;
  flex-shrink: 0;
  padding-bottom: env(safe-area-inset-bottom);
}

.tab-item {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 8px 4px 6px;
  background: none;
  border: none;
  color: #6b7280;
  cursor: pointer;
  position: relative;
  gap: 2px;
}

.tab-item.active {
  color: #818cf8;
}

.tab-icon-wrapper {
  position: relative;
}

.tab-icon {
  width: 24px;
  height: 24px;
}

.tab-badge {
  position: absolute;
  top: -4px;
  right: -8px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  background: #ef4444;
  border-radius: 8px;
  font-size: 10px;
  font-weight: 700;
  color: white;
  display: flex;
  align-items: center;
  justify-content: center;
}

.tab-badge-critical {
  animation: pulse-badge 1.5s infinite;
}

.tab-label {
  font-size: 11px;
  font-weight: 500;
}

/* ─── States ────────────────────────────────────────────────────────────── */

.loading-state, .empty-state {
  text-align: center;
  padding: 32px 16px;
  color: #6b7280;
  font-size: 14px;
}

/* ─── Keyboard ──────────────────────────────────────────────────────────── */

.keyboard-open .tab-bar {
  display: none;
}

/* ─── Animations ────────────────────────────────────────────────────────── */

@keyframes pulse-badge {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

.animate-spin {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

/* ─── Autonomy ─────────────────────────────────────────────────────────── */

.autonomy-badge {
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 4px;
  font-weight: 700;
  letter-spacing: 0.5px;
}

.autonomy-badge.auto {
  background: #78350f;
  color: #fbbf24;
}

.autonomy-toggle-btn {
  padding: 6px 14px;
  border: none;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  min-width: 70px;
}

.autonomy-on {
  background: #78350f;
  color: #fbbf24;
}

.autonomy-off {
  background: #374151;
  color: #9ca3af;
}

.autonomy-toggle-btn:disabled {
  opacity: 0.6;
}

/* ─── Success Rate Bar ─────────────────────────────────────────────────── */

.success-bar-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
}

.success-bar-track {
  flex: 1;
  height: 4px;
  background: #374151;
  border-radius: 2px;
  overflow: hidden;
}

.success-bar-fill {
  height: 100%;
  border-radius: 2px;
  transition: width 0.3s;
}

.bar-green { background: #22c55e; }
.bar-yellow { background: #eab308; }
.bar-red { background: #ef4444; }

.success-bar-label {
  font-size: 10px;
  font-weight: 600;
  min-width: 28px;
  text-align: right;
}

.text-green { color: #4ade80; }
.text-yellow { color: #facc15; }
.text-red { color: #f87171; }

.chat-open-btn {
  padding: 6px 14px;
  background: #312e81;
  border: none;
  border-radius: 8px;
  color: #a5b4fc;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
}

.chat-open-btn:disabled {
  background: #374151;
  color: #6b7280;
  cursor: default;
}

/* ─── Chat Overlay ─────────────────────────────────────────────────────── */

.chat-overlay {
  position: fixed;
  top: env(safe-area-inset-top);
  bottom: 0;
  left: env(safe-area-inset-left);
  right: env(safe-area-inset-right);
  background: #111827;
  z-index: 200;
  display: flex;
  flex-direction: column;
  padding-bottom: env(safe-area-inset-bottom);
}

.chat-header {
  display: flex;
  align-items: center;
  padding: 8px 12px;
  background: #1f2937;
  border-bottom: 1px solid #374151;
  flex-shrink: 0;
  gap: 10px;
}

.chat-back-btn {
  padding: 8px;
  background: none;
  border: none;
  color: #818cf8;
  cursor: pointer;
}

.chat-header-info {
  flex: 1;
  min-width: 0;
}

.chat-header-name {
  display: block;
  font-weight: 600;
  font-size: 15px;
  color: white;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.chat-header-status {
  font-size: 12px;
  color: #6b7280;
}

.chat-new-btn {
  padding: 6px 12px;
  background: #374151;
  border: none;
  border-radius: 8px;
  color: #818cf8;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}

.sessions-toggle {
  padding: 6px;
  background: #1a2332;
  border: none;
  border-bottom: 1px solid #374151;
  color: #6b7280;
  font-size: 12px;
  cursor: pointer;
  text-align: center;
  flex-shrink: 0;
}

.chat-sessions {
  max-height: 200px;
  overflow-y: auto;
  background: #1a2332;
  border-bottom: 1px solid #374151;
  flex-shrink: 0;
}

.session-item {
  padding: 10px 14px;
  border-bottom: 1px solid #1f2937;
  cursor: pointer;
}

.session-item:active, .session-item.active {
  background: #263040;
}

.session-preview {
  display: block;
  font-size: 13px;
  color: #d1d5db;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.session-meta {
  font-size: 11px;
  color: #6b7280;
  margin-top: 2px;
  display: block;
}

.chat-messages {
  flex: 1;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.chat-bubble {
  max-width: 85%;
  padding: 10px 14px;
  border-radius: 16px;
  font-size: 14px;
  line-height: 1.5;
  word-break: break-word;
}

.bubble-user {
  align-self: flex-end;
  background: #4338ca;
  color: white;
  border-bottom-right-radius: 4px;
}

.bubble-assistant {
  align-self: flex-start;
  background: #1f2937;
  color: #e5e7eb;
  border-bottom-left-radius: 4px;
}

.bubble-content {
  white-space: pre-wrap;
}

.bubble-time {
  font-size: 10px;
  color: rgba(255, 255, 255, 0.4);
  margin-top: 4px;
}

.bubble-assistant .bubble-time {
  color: #6b7280;
}

.thinking-dots {
  display: flex;
  gap: 4px;
  padding: 4px 0;
}

.thinking-dots span {
  width: 6px;
  height: 6px;
  background: #6b7280;
  border-radius: 50%;
  animation: thinking 1.4s infinite;
}

.thinking-dots span:nth-child(2) { animation-delay: 0.2s; }
.thinking-dots span:nth-child(3) { animation-delay: 0.4s; }

@keyframes thinking {
  0%, 80%, 100% { opacity: 0.3; transform: scale(0.8); }
  40% { opacity: 1; transform: scale(1); }
}

.chat-input-bar {
  display: flex;
  gap: 8px;
  padding: 8px 12px;
  background: #1f2937;
  border-top: 1px solid #374151;
  flex-shrink: 0;
  align-items: flex-end;
}

.chat-input {
  flex: 1;
  padding: 10px 14px;
  background: #0f172a;
  border: 1px solid #374151;
  border-radius: 20px;
  color: white;
  font-size: 16px;
  outline: none;
  resize: none;
  max-height: 120px;
  line-height: 1.4;
  font-family: inherit;
}

.chat-input:focus {
  border-color: #6366f1;
}

.chat-send-btn {
  width: 40px;
  height: 40px;
  background: #6366f1;
  border: none;
  border-radius: 50%;
  color: white;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.chat-send-btn:disabled {
  background: #374151;
  color: #6b7280;
}
</style>
