<template>
  <div class="h-screen flex flex-col bg-gray-100 dark:bg-gray-900 overflow-hidden">
    <NavBar />

    <main class="flex-1 flex overflow-hidden">
      <!-- System Views Sidebar -->
      <SystemViewsSidebar
        @create="openCreateModal"
        @edit="openEditModal"
      />

      <!-- relative: anchor for the type-to-filter pill + query-empty overlay
           (ent#261) — this column does NOT scroll (panes scroll internally),
           so absolutely-positioned chrome here never scrolls away. -->
      <div class="relative flex flex-col flex-1 overflow-hidden">
        <!-- Compact Header -->
        <div class="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 px-4 py-2">
          <div class="flex items-center justify-between">
            <!-- Left: Stats — elastic (#1830). `flex-1 min-w-0` makes this the
                 only cluster that gives ground, and `container-type: inline-size`
                 (see .stats-cluster below) turns its leftover width into the
                 query axis the progressive-hide ladder degrades against. -->
            <div class="stats-cluster flex items-center min-w-0 flex-1 overflow-hidden">
              <div class="flex items-center space-x-3 text-xs text-gray-500 dark:text-gray-400 whitespace-nowrap">
                <span class="flex items-center space-x-1">
                  <span class="w-1.5 h-1.5 rounded-full bg-status-success-500"></span>
                  <span class="font-medium text-status-success-600 dark:text-status-success-400">{{ runningCount }}/{{ agents.length }}</span>
                  <span>agents</span>
                </span>
                <!-- Working-now count (trinity-enterprise#47) -->
                <span class="text-gray-300 dark:text-gray-500" data-sep="working">·</span>
                <span class="flex items-center space-x-1" data-stat="working">
                  <span class="font-medium text-status-info-600 dark:text-status-info-400">{{ workingNowCount }}</span>
                  <span>working now</span>
                </span>
                <span class="text-gray-300 dark:text-gray-500" data-sep="messages">·</span>
                <span class="flex items-center space-x-1" data-stat="messages">
                  <span class="font-medium text-status-info-600 dark:text-status-info-400">{{ totalCollaborationCount }}</span>
                  <span>messages ({{ timeRangeHours }}h)</span>
                </span>
                <!-- Host Telemetry (inline) — owns its own leading separator and
                     its own container-query hide ladder (HostTelemetry.vue). -->
                <HostTelemetry />
              </div>
            </div>

            <!-- Right: Controls -->
            <div class="flex items-center space-x-2 flex-shrink-0">
              <!-- Create Agent (trinity-enterprise#260) — chassis-level so agent
                   creation is reachable from every mode, not just the List tab.
                   The label degrades to icon-only below `md` (pre-decided in the
                   plan): the controls cluster is flex-shrink-0, and at 640px in
                   grid mode the full label pushes the stats cluster below the
                   71px `agents-only` floor of the #1830 degrade ladder — the
                   stats-overflow spec's clip assertion would fire. -->
              <button
                @click="showCreateModal = true"
                class="flex items-center space-x-1 px-2 py-1 rounded text-xs font-medium bg-action-primary-600 hover:bg-action-primary-700 text-white whitespace-nowrap transition-colors"
                title="Create Agent"
                aria-label="Create Agent"
              >
                <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
                </svg>
                <span class="hidden md:inline">Create Agent</span>
              </button>

              <!-- Quick Tag Filter Dropdown -->
              <div v-if="availableTags.length > 0" ref="tagDropdownRef" class="relative">
                <button
                  @click="showTagDropdown = !showTagDropdown"
                  :class="[
                    'flex items-center space-x-1 px-2 py-0.5 rounded text-xs font-medium transition-all whitespace-nowrap',
                    selectedQuickTags.length > 0
                      ? 'bg-blue-600 text-white'
                      : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
                  ]"
                >
                  <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A2 2 0 013 12V7a4 4 0 014-4z"/>
                  </svg>
                  <span class="whitespace-nowrap">{{ selectedQuickTags.length > 0 ? selectedQuickTags.length + ' tag' + (selectedQuickTags.length > 1 ? 's' : '') : 'Tags' }}</span>
                  <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                  </svg>
                </button>
                <div
                  v-if="showTagDropdown"
                  class="absolute right-0 top-full mt-1 bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-gray-200 dark:border-gray-700 py-1 z-50 max-h-48 overflow-y-auto min-w-36"
                >
                  <button
                    v-if="selectedQuickTags.length > 0"
                    @click="clearQuickTags(); showTagDropdown = false"
                    class="w-full px-3 py-1.5 text-left text-xs text-status-danger-600 dark:text-status-danger-400 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors border-b border-gray-200 dark:border-gray-700"
                  >
                    Clear all
                  </button>
                  <button
                    v-for="tagInfo in availableTags"
                    :key="tagInfo.tag"
                    @click="toggleQuickTag(tagInfo.tag)"
                    :class="[
                      'w-full px-3 py-1.5 text-left text-xs hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors flex items-center justify-between',
                      selectedQuickTags.includes(tagInfo.tag) ? 'text-blue-600 dark:text-blue-400 font-medium' : 'text-gray-700 dark:text-gray-300'
                    ]"
                  >
                    <span>#{{ tagInfo.tag }}</span>
                    <span class="text-gray-400 dark:text-gray-400 text-[10px]">{{ tagInfo.count }}</span>
                  </button>
                </div>
              </div>

              <!-- Owner filter dropdown -->
              <select
                v-if="availableOwners.length > 1"
                v-model="selectedOwner"
                @change="onOwnerFilterChange"
                class="text-xs border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-700 dark:text-gray-200 focus:outline-none focus:ring-1 focus:ring-blue-500"
              >
                <option value="">All Owners</option>
                <option v-for="owner in availableOwners" :key="owner.name || '__unassigned__'" :value="owner.name || '__unassigned__'">
                  {{ owner.name || 'Unassigned' }} ({{ owner.count }})
                </option>
              </select>

              <span v-if="availableTags.length > 0 || availableOwners.length > 1" class="text-gray-300 dark:text-gray-500">|</span>

              <!-- Type-to-filter hint (ent#261) — mouse/touch parity for the
                   `/` hotkey. TOGGLES: opens the pill when closed,
                   clears+closes when the filter is open/active. -->
              <button
                @click="toggleFilterPill"
                :class="[
                  'px-2 py-1 rounded text-xs font-mono font-medium transition-all',
                  (filterOpen || filterActive)
                    ? 'bg-blue-600 text-white'
                    : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
                ]"
                title="Filter agents (press /)"
                aria-label="Filter agents"
                data-testid="filter-kbd-hint"
              >/</button>

              <!-- Mode Toggle (Timeline / Grid / List — trinity-enterprise#47 grid,
                   trinity-enterprise#260 list; Graph decommissioned #1689). This
                   v-for is the second home of the mode list — keep in sync with
                   VIEW_MODES in stores/network.js. -->
              <div class="flex rounded-md border border-gray-300 dark:border-gray-600 p-0.5 bg-gray-50 dark:bg-gray-700">
                <button
                  v-for="mode in ['timeline', 'grid', 'list']"
                  :key="mode"
                  @click="toggleMode(mode)"
                  :class="[
                    'px-2 py-1 rounded text-xs font-medium transition-all capitalize',
                    viewMode === mode ? 'bg-blue-600 text-white' : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'
                  ]"
                >
                  {{ mode }}
                </button>
              </div>

              <!-- Grid-mode controls (trinity-enterprise#47) -->
              <template v-if="viewMode === 'grid'">
                <button
                  @click="fleetGridRef?.tidyUp()"
                  class="text-xs border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-gray-100 transition-colors"
                  title="Compact tiles row-by-row, preserving reading order"
                >
                  Tidy up
                </button>
                <button
                  @click="fleetGridRef?.resetToDefault()"
                  class="text-xs border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-gray-100 transition-colors"
                  title="Restore the default tile layout"
                >
                  Reset
                </button>
              </template>

              <!-- Time Range -->
              <select
                v-model="selectedTimeRange"
                @change="onTimeRangeChange"
                class="text-xs border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-700 dark:text-gray-200 focus:outline-none focus:ring-1 focus:ring-blue-500"
              >
                <option :value="1">1h</option>
                <option :value="6">6h</option>
                <option :value="24">24h</option>
                <option :value="72">3d</option>
                <option :value="168">7d</option>
              </select>

              <!-- Connection indicator -->
              <div
                :class="[
                  'w-2 h-2 rounded-full',
                  isConnected ? 'bg-status-success-500' : 'bg-status-danger-500'
                ]"
                :title="isConnected ? 'Connected' : 'Disconnected'"
              ></div>

              <!-- Loading -->
              <svg v-if="isLoadingHistory" class="animate-spin h-4 w-4 text-blue-600 dark:text-blue-400" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
              </svg>

              <!-- Refresh -->
              <button
                @click="refreshAll"
                :disabled="isLoadingHistory"
                class="p-1.5 text-gray-500 dark:text-gray-400 hover:text-blue-600 dark:hover:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/30 rounded transition-colors disabled:opacity-50"
                title="Refresh"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
              </button>
            </div>
          </div>
        </div>

        <!--
          Onboarding stack (#2380). At most ONE card renders, ever — see the
          `.onboarding-stack` rule below. Four independent surfaces landed here
          from four issues, none aware of the others, and on a first login all
          four can be true at once: ~520px of chrome above the product the
          operator installed Trinity for, with four dismiss buttons. DOM order
          IS priority order, highest first.
        -->
        <div class="onboarding-stack">
        <!-- Instance hardening guide (#2380). FIRST in the stack on purpose: a
             security-posture prompt outranks a getting-started nudge — a
             marketplace droplet is answering the public internet right now,
             whereas the cards below can wait a page load. Renders only on a
             marketplace install that has not yet been given a domain. -->
        <HardeningGuide />
        <!-- First-run front desk (ent#319). Shows only on a seed-only install:
             the fleet is running and none of it is the user's, which is the
             exact case the wizard's auto-open cannot see since ent#124. -->
        <FrontDeskPanel @make-one="openOnboarding" />
        <!-- Getting-started checklist (ent#238). Renders nothing unless the
             enterprise onboarding module is entitled AND the user still has an
             undone step — never a gate, always dismissible. -->
        <ActivationChecklist />
        <!-- Finish setup (ent#437): ONE card for the post-login admin asks the
             first-run wizard can no longer carry — the sign-in email prompt
             (#2381, section 1) and the usage-sharing consent (ent#437, section 2).
             One chassis rather than a fifth stacked nudge. Each section decides
             its own visibility; the card renders nothing when none applies. -->
        <FinishSetupCard />
        </div>

    <!-- Timeline View (only visible in timeline mode) -->
    <template v-if="isTimelineMode">
      <!-- Loading skeleton (#1266): immediate feedback while fleet/timeline data loads -->
      <div
        v-if="isFleetLoading && agents.length === 0"
        class="flex-1 min-h-0 overflow-hidden bg-white dark:bg-gray-800 px-4 py-4"
      >
        <SkeletonLoader variant="rows" :count="8" height="2.5rem" gap="0.5rem" />
      </div>
      <!-- Error state (#1266): distinct from an empty timeline / infinite skeleton -->
      <div
        v-else-if="fleetLoadError && agents.length === 0"
        class="flex-1 min-h-0 flex flex-col items-center justify-center text-center px-4"
      >
        <p class="text-sm text-gray-600 dark:text-gray-300">Couldn't load timeline data.</p>
        <button
          @click="refreshAll"
          class="mt-2 text-sm font-medium text-blue-600 dark:text-blue-400 hover:underline"
        >Retry</button>
      </div>
      <!-- :agents is the visibleAgents seam (ent#261): rows, communication
           arrows, and schedule markers all derive from this prop, so the
           type-to-filter query AND the owner filter now apply to the timeline
           (owner previously applied to grid/list only — deliberate change). -->
      <ReplayTimeline
        v-else
        :agents="visibleAgents"
        :nodes="nodes"
        :events="historicalCollaborations"
        :timeline-start="timelineStart"
      :timeline-end="timelineEnd"
      :current-event-index="currentEventIndex"
      :total-events="totalEvents"
      :total-duration="totalDuration"
      :replay-elapsed-ms="replayElapsedMs"
      :replay-speed="replaySpeed"
      :is-playing="isPlaying"
      :context-stats="contextStats"
      :execution-stats="executionStats"
      :is-live-mode="true"
      :time-range-hours="selectedTimeRange"
      :schedules="schedules"
      :slot-stats="slotStats"
      @play="handlePlay"
      @pause="handlePause"
      @stop="handleStop"
      @speed-change="handleSpeedChange"
      @toggle-autonomy="handleToggleAutonomy"
      />
    </template>

    <!-- Grid View (trinity-enterprise#47) — magnetic tile canvas. v-if so its
         polling and timers tear down whenever the mode is not active. -->
    <div v-if="viewMode === 'grid'" class="relative bg-white dark:bg-gray-800 shadow-sm dark:shadow-gray-900 flex-1 min-h-0">
      <!-- Loading skeleton: immediate feedback while the fleet list loads -->
      <div
        v-if="isFleetLoading && agents.length === 0"
        class="absolute inset-0 flex items-center justify-center"
      >
        <SkeletonLoader variant="nodes" :count="5" />
      </div>
      <!-- Error state -->
      <div
        v-else-if="fleetLoadError && agents.length === 0"
        class="absolute inset-0 flex items-center justify-center"
      >
        <div class="text-center">
          <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">Couldn't load agents</h3>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Something went wrong fetching the fleet.</p>
          <button
            @click="refreshAll"
            class="mt-4 inline-flex items-center px-4 py-2 border border-transparent shadow-sm text-sm font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700"
          >Retry</button>
        </div>
      </div>
      <!-- True-empty state. `!filterActive` (ent#261): under an active query a
           zero-match must show the chassis query-empty overlay, never the
           onboarding CTA — and the grid must stay MOUNTED (v-else below). -->
      <div
        v-else-if="visibleAgents.length === 0 && !filterActive"
        class="absolute inset-0 flex items-center justify-center"
      >
        <div class="text-center">
          <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">No agents yet</h3>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Launch your first agent in a couple of clicks.</p>
          <button
            @click="openOnboarding"
            class="mt-4 inline-flex items-center px-4 py-2 border border-transparent shadow-sm text-sm font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700"
          >
            Get started
          </button>
        </div>
      </div>
      <FleetGrid v-else ref="fleetGridRef" :agents="visibleAgents" :org-agents="ownerFilteredAgents" />
    </div>

    <!-- List View (trinity-enterprise#260) — the Agents page consolidated into
         a dashboard mode. v-if so the panel's sync-health interval tears down
         whenever the mode is not active. The wrapper is the flex slot
         (min-h-0 so it can shrink inside the overflow-hidden column); the
         panel root owns the scroll + horizontal padding. -->
    <div v-if="viewMode === 'list'" class="flex-1 min-h-0 overflow-hidden bg-gray-100 dark:bg-gray-900">
      <!-- Loading skeleton (#1266): immediate feedback while the fleet list loads -->
      <div
        v-if="isFleetLoading && agents.length === 0"
        class="h-full px-4 sm:px-6 lg:px-8 py-4"
      >
        <SkeletonLoader variant="rows" :count="8" height="4rem" gap="0.75rem" />
      </div>
      <!-- Error state -->
      <div
        v-else-if="fleetLoadError && agents.length === 0"
        class="h-full flex items-center justify-center"
      >
        <div class="text-center">
          <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">Couldn't load agents</h3>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Something went wrong fetching the fleet.</p>
          <button
            @click="refreshAll"
            class="mt-4 inline-flex items-center px-4 py-2 border border-transparent shadow-sm text-sm font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700"
          >Retry</button>
        </div>
      </div>
      <!-- True-empty state (grid-identical teach — chassis-owned, D7).
           `!filterActive` (ent#261): same guard as the grid pane — a query
           zero-match falls through to the mounted panel + chassis overlay. -->
      <div
        v-else-if="visibleAgents.length === 0 && !filterActive"
        class="h-full flex items-center justify-center"
      >
        <div class="text-center">
          <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">No agents yet</h3>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Launch your first agent in a couple of clicks.</p>
          <button
            @click="openOnboarding"
            class="mt-4 inline-flex items-center px-4 py-2 border border-transparent shadow-sm text-sm font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700"
          >
            Get started
          </button>
        </div>
      </div>
      <AgentListPanel
        v-else
        ref="listPanelRef"
        :agents="visibleAgents"
        :available-tags="availableTags"
        @tags-changed="fetchAvailableTags"
        @clear-chassis-filters="clearChassisFilters"
      />
    </div>

    <!-- Query-empty overlay (ent#261 D8) — ONE chassis-level element covering
         whichever pane is active; the pane stays MOUNTED underneath (a
         transient zero-match while typing must never unmount ReplayTimeline /
         FleetGrid — zoom/scroll/layout state would reset). pointer-events pass
         through everywhere except the card, so header controls stay usable. -->
    <div
      v-if="queryEmpty"
      class="absolute inset-0 z-20 flex items-center justify-center pointer-events-none"
      data-testid="filter-query-empty"
    >
      <div class="pointer-events-auto text-center px-6 py-5 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg">
        <p class="text-sm text-gray-700 dark:text-gray-200">
          No agents match "{{ filterQueryTrimmed }}" — Esc to clear
        </p>
        <button
          @click="clearFilter"
          class="mt-3 inline-flex items-center px-3 py-1.5 border border-gray-300 dark:border-gray-600 rounded-md text-sm font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600"
        >
          Clear filter
        </button>
      </div>
    </div>

    <!-- Type-to-filter pill (ent#261 D6) — floating overlay anchored to this
         non-scrolling column. Renders whenever open OR a query is applied (an
         applied-but-hidden filter is the dishonest state AC-5 prevents).
         top-28 clears the chassis header (~41px) + every pane-internal control
         strip (timeline zoom bar ends ~82px + 24px time scale; list toolbar
         ends ~97px). z-30: above panes + the query-empty overlay, below modals. -->
    <div
      v-if="filterOpen || filterActive"
      role="search"
      class="absolute top-28 left-1/2 -translate-x-1/2 z-30 flex items-center gap-2 px-3 py-1.5 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-full shadow-lg"
      data-testid="filter-pill"
    >
      <svg class="w-4 h-4 text-gray-400 dark:text-gray-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
      </svg>
      <input
        ref="filterInputRef"
        v-model="filterQueryModel"
        type="text"
        placeholder="Filter agents…"
        aria-label="Filter agents"
        autofocus
        class="w-44 bg-transparent text-sm text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none"
        @keydown.esc.stop.prevent="clearFilter"
        @keydown.enter.prevent="filterInputRef?.blur()"
      />
      <span
        v-if="filterActive"
        class="text-xs text-gray-500 dark:text-gray-400 whitespace-nowrap"
        aria-live="polite"
        data-testid="filter-match-count"
      >{{ visibleAgents.length }} of {{ ownerFilteredAgents.length }} match</span>
      <kbd class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-[10px] font-mono text-gray-500 dark:text-gray-400">Esc</kbd>
      <button
        @click="clearFilter"
        class="p-0.5 rounded text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
        aria-label="Clear filter"
        data-testid="filter-clear"
      >
        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </div>

      </div>
    </main>

    <!-- Create Agent Modal (trinity-enterprise#260 — chassis-level, all modes).
         On close, refresh the fleet: the WS agent_created event can lag while
         the container spins up. -->
    <CreateAgentModal v-if="showCreateModal" @close="onCreateModalClose" />

    <!-- System View Editor Modal -->
    <SystemViewEditor
      :is-open="isEditorOpen"
      :editing-view="editingView"
      @close="closeEditor"
      @saved="onViewSaved"
    />

    <!-- First-run onboarding wizard (trinity-enterprise#52) -->
    <OnboardingWizard
      v-if="showOnboarding"
      :claude-auth-configured="sessionsStore.claudeAuthConfigured"
      @close="closeOnboarding"
      @deployed="onAgentDeployed"
    />
  </div>
</template>

<script setup>
import NavBar from '@/components/NavBar.vue'
import HostTelemetry from '@/components/HostTelemetry.vue'
import ReplayTimeline from '@/components/ReplayTimeline.vue'
import SkeletonLoader from '@/components/SkeletonLoader.vue'
import SystemViewsSidebar from '@/components/SystemViewsSidebar.vue'
import SystemViewEditor from '@/components/SystemViewEditor.vue'
import OnboardingWizard from '@/components/OnboardingWizard.vue'
import HardeningGuide from '@/components/onboarding/HardeningGuide.vue'
import FrontDeskPanel from '@/components/onboarding/FrontDeskPanel.vue'
import ActivationChecklist from '@/components/onboarding/ActivationChecklist.vue'
import FinishSetupCard from '@/components/onboarding/FinishSetupCard.vue'
import { useSessionsStore } from '@/stores/sessions'
import axios from 'axios'
import { ref, onMounted, onUnmounted, computed, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useNetworkStore } from '@/stores/network'
import { useSystemViewsStore } from '@/stores/systemViews'
import { storeToRefs } from 'pinia'
import FleetGrid from '@/components/FleetGrid.vue'
import { isOrgTag } from '@/utils/gridOrg'
import AgentListPanel from '@/components/AgentListPanel.vue'
import CreateAgentModal from '@/components/CreateAgentModal.vue'
import { useNotification } from '@/composables/useNotification'

const networkStore = useNetworkStore()
const systemViewsStore = useSystemViewsStore()
const sessionsStore = useSessionsStore()
const route = useRoute()
const router = useRouter()

// ?view= deep-link intent (trinity-enterprise#260 D2) — a route WATCH, not an
// onMounted read, so navigating to `/?view=list` while the Dashboard is already
// mounted still applies. Non-persisting (a redirect/bookmark is not a
// preference statement — AC-4 protects the *selected* view), and the param is
// stripped after applying so a reload doesn't re-apply it. setViewMode itself
// whitelists against VIEW_MODES (invalid values degrade to timeline).
watch(() => route.query.view, (view) => {
  if (!view) return
  networkStore.setViewMode(view, { persist: false })
  const { view: _stripped, ...rest } = route.query
  router.replace({ query: rest }).catch(() => {})
}, { immediate: true })

// First-run onboarding (trinity-enterprise#52). Auto-opens once for a fresh
// install with zero agents; dismissal is remembered so it never nags.
const ONBOARDING_DISMISSED_KEY = 'trinity_onboarding_dismissed_v1'
const showOnboarding = ref(false)
function openOnboarding() {
  showOnboarding.value = true
}
function closeOnboarding() {
  showOnboarding.value = false
  try { localStorage.setItem(ONBOARDING_DISMISSED_KEY, '1') } catch { /* ignore */ }
}
function onAgentDeployed() {
  // Agent was created via the wizard's real create modal. Keep the wizard open
  // (it advances to the credential step on its own) and refresh the fleet so
  // the new agent appears on the graph without a manual page reload — the
  // WebSocket agent_created event can lag while the container spins up.
  networkStore.fetchAgents()
}
function maybeAutoOpenOnboarding() {
  if (showOnboarding.value) return
  // Explicit ?onboarding=1 re-opens the wizard any time (re-run / QA preview),
  // regardless of fleet size or prior dismissal.
  if (route.query.onboarding === '1') {
    showOnboarding.value = true
    return
  }
  if (isFleetLoading.value) return
  // Count only user-created agents — `trinity-system` exists on every install,
  // so counting it would mean a fresh fleet is never "empty" and auto-open
  // would never fire.
  //
  // ent#319: since ent#124 seeds a fleet on first run, this predicate is false
  // on an out-of-the-box install and the wizard no longer auto-opens there.
  // That case is now served by the front-desk panel (which shows only when
  // something WAS seeded), so this stays exactly as it is: it still fires on a
  // genuinely empty install, and the two surfaces never appear together.
  if (agents.value.filter(a => !a.is_system).length > 0) return
  if (localStorage.getItem(ONBOARDING_DISMISSED_KEY) === '1') return
  showOnboarding.value = true
}

// System View Editor Modal State
const isEditorOpen = ref(false)
const editingView = ref(null)

function openCreateModal() {
  editingView.value = null
  isEditorOpen.value = true
}

function openEditModal(view) {
  editingView.value = view
  isEditorOpen.value = true
}

function closeEditor() {
  isEditorOpen.value = false
  editingView.value = null
}

async function onViewSaved() {
  // Refresh views after save
  await systemViewsStore.fetchViews()
}

const {
  agents,
  visibleAgents,
  ownerFilteredAgents,
  filterQuery,
  nodes,
  edges,
  collaborationHistory,
  isConnected,
  activeCollaborationCount,
  lastEventTimeFormatted,
  historicalCollaborations,
  totalCollaborationCount,
  timeRangeHours,
  isLoadingHistory,
  loading: isFleetLoading,
  loadError: fleetLoadError,
  contextStats,
  executionStats,
  slotStats,
  workingState,
  schedules,
  // Timeline/Replay state
  viewMode,
  isTimelineMode,
  isPlaying,
  replaySpeed,
  currentEventIndex,
  replayElapsedMs,
  totalEvents,
  totalDuration,
  playbackPosition,
  timelineStart,
  timelineEnd,
  currentTime
} = storeToRefs(networkStore)

// Persisted state: Time range (default 24h, persisted to localStorage)
const savedTimeRange = localStorage.getItem('trinity-dashboard-time-range')
const selectedTimeRange = ref(savedTimeRange ? parseInt(savedTimeRange) : 24)


// Quick Tag Filter state (persisted to localStorage when not using System View)
const availableTags = ref([])
const savedQuickTags = localStorage.getItem('trinity-dashboard-quick-tags')
const selectedQuickTags = ref(savedQuickTags ? JSON.parse(savedQuickTags) : [])
const showTagDropdown = ref(false)

// Owner filter state (persisted to localStorage, synced with network store)
const selectedOwner = ref(networkStore.filterOwner || '')

// Derive distinct owners from the full (unfiltered) agent list
const availableOwners = computed(() => {
  const allAgents = agents.value
  const counts = {}
  for (const agent of allAgents) {
    const owner = agent.owner || null
    counts[owner] = (counts[owner] || 0) + 1
  }
  return Object.entries(counts)
    .map(([name, count]) => ({ name: name === 'null' ? null : name, count }))
    .sort((a, b) => {
      if (a.name === null) return 1
      if (b.name === null) return -1
      return a.name.localeCompare(b.name)
    })
})

function onOwnerFilterChange() {
  networkStore.setFilterOwner(selectedOwner.value)
}

// Computed: First 5 tags for inline display
const displayedTags = computed(() => availableTags.value.slice(0, 5))

// Watch for active view changes and update network filter
const { activeFilterTags, activeViewId } = storeToRefs(systemViewsStore)
watch(activeFilterTags, (tags) => {
  networkStore.setFilterTags(tags)
  // Sync quick tags with system view selection
  if (activeViewId.value) {
    selectedQuickTags.value = [...tags]
    // Clear persisted quick tags when using a system view
    localStorage.removeItem('trinity-dashboard-quick-tags')
  }
}, { immediate: true })


// Computed stats
const runningCount = computed(() => {
  return agents.value.filter(a => a.status?.toLowerCase() === 'running').length
})

// Grid view (trinity-enterprise#47)
const fleetGridRef = ref(null)

// List view (trinity-enterprise#260). Both grid and list render the store's
// `visibleAgents` computed (the ent#261 seam — server-side tag filter ∘ owner
// filter) instead of a local copy of the owner-filter expression.
const listPanelRef = ref(null)

// Create Agent modal (chassis-level — reachable from every mode, ent#260)
const showCreateModal = ref(false)
function onCreateModalClose() {
  showCreateModal.value = false
  networkStore.fetchAgents()
}

// clear-chassis-filters (ent#260 strategy F6): the list panel's "Clear all
// filters" clears its local name/status filters AND asks the chassis to clear
// the quick-tag + owner layers — the retired page's button cleared all four.
function clearChassisFilters() {
  clearQuickTags()
  if (selectedOwner.value) {
    selectedOwner.value = ''
    networkStore.setFilterOwner('')
  }
}

// --- Type-to-filter (ent#261) ---
// `filterOpen` is Dashboard-LOCAL (eng F8): pill visibility survives mode
// switches (panes are inner v-ifs) and dies with the page — a store-level
// open flag would resurrect an open empty pill on remount. The store carries
// only `filterQuery` (never persisted; cleared on unmount below).
const filterOpen = ref(false)
const filterInputRef = ref(null)

// One mutation path: the input writes through the store setter.
const filterQueryModel = computed({
  get: () => filterQuery.value,
  set: (v) => networkStore.setFilterQuery(v)
})
const filterActive = computed(() => filterQuery.value.trim() !== '')
const filterQueryTrimmed = computed(() => filterQuery.value.trim())
// Query-empty (D8): only while a query is active — loading/error keep their
// own ladder states.
const queryEmpty = computed(() =>
  filterActive.value && visibleAgents.value.length === 0 &&
  !isFleetLoading.value && !fleetLoadError.value
)

function openFilterPill() {
  filterOpen.value = true
  // autofocus on the input is belt-and-braces for first render; nextTick
  // covers reopening an already-rendered pill (autofocus fires only on mount).
  nextTick(() => filterInputRef.value?.focus())
}

function clearFilter() {
  networkStore.setFilterQuery('')
  filterOpen.value = false
  filterInputRef.value?.blur()
}

// Header kbd hint (D7): TOGGLES — opens when closed, clears+closes when
// open/active (mouse/touch parity with `/` + Esc).
function toggleFilterPill() {
  if (filterOpen.value || filterActive.value) clearFilter()
  else openFilterPill()
}

// Document keydown: `/` opens (guards 0-5), Esc is the clear backstop so
// "Esc to clear" stays true after focus wanders out of the pill input.
function handleDashboardKeydown(e) {
  // Guard 0: respect consumers + ignore key-hold repeat.
  if (e.defaultPrevented || e.repeat) return

  if (e.key === 'Escape') {
    // Backstop only while the filter exists; the pill input's own Esc handler
    // .stop's before reaching here.
    if (!(filterOpen.value || filterActive.value)) return
    // Never race a modal's own Esc handling.
    if (showOnboarding.value || isEditorOpen.value || showCreateModal.value) return
    // Layered dismissal (strategy F5): an open tag dropdown consumes this
    // Esc; the filter survives — the second Esc clears.
    if (showTagDropdown.value) {
      showTagDropdown.value = false
      return
    }
    // Don't nuke the filter from inside ANOTHER editable field (gemini G4
    // generalized): Esc in the list panel's search box, a chat widget
    // textarea, or a native <select> being closed belongs to that control —
    // clearing the chassis filter from there is surprising cross-layer
    // destruction. The pill input never reaches here (its own Esc handler
    // .stop.prevent's), so this can't block the pill's Esc.
    const et = e.target
    if (et && (et.tagName === 'INPUT' || et.tagName === 'TEXTAREA' || et.tagName === 'SELECT' || et.isContentEditable)) return
    clearFilter()
    return
  }

  // Guard 1: layout-produced `/` only (fires for Shift+7 on de-DE — do NOT
  // exclude shiftKey).
  if (e.key !== '/') return
  // Guard 2: don't shadow browser/OS chords.
  if (e.ctrlKey || e.metaKey || e.altKey) return
  // Guard 3: IME composition.
  if (e.isComposing) return
  // Guard 4: editable targets (isContentEditable inherits — no .closest()).
  const t = e.target
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return
  // Guard 5: open modals.
  if (showOnboarding.value || isEditorOpen.value || showCreateModal.value) return

  e.preventDefault() // blocks Firefox quick-find
  openFilterPill()
}

// Agents executing right now: WS-observed in-flight work unioned with the
// polled context-stats activity state.
const workingNowCount = computed(() => {
  const working = new Set(Object.keys(workingState.value))
  for (const [name, s] of Object.entries(contextStats.value)) {
    if (s.activityState === 'active') working.add(name)
  }
  const names = new Set(agents.value.map(a => a.name))
  return [...working].filter(n => names.has(n)).length
})

const stoppedCount = computed(() => {
  return agents.value.filter(a => a.status?.toLowerCase() !== 'running').length
})

// ent#126: land here filtered to a freshly installed system.
//
// A manifest deploy always tags every agent it creates with the system name, so
// `?tags=<system>` is the fallback that ALWAYS works; `?view=<id>` is preferred
// when the manifest also declared a `system_view:` and it was created.
// Additive and deliberately narrow: it seeds the same state the tag chips and the
// view sidebar already drive, and does nothing when the query is absent.
function applyDeepLinkFilters() {
  const viewId = route.query.view
  if (typeof viewId === 'string' && viewId) {
    // A view carries its own filter tags; selecting it wins over ?tags=.
    systemViewsStore.selectView(viewId)
    return
  }

  const tagsParam = route.query.tags
  if (typeof tagsParam !== 'string' || !tagsParam.trim()) return
  const tags = tagsParam.split(',').map(t => t.trim().toLowerCase()).filter(Boolean)
  if (!tags.length) return

  // An explicit ?tags= wins over a PERSISTED view selection, exactly as picking a
  // tag chip does (`toggleQuickTag` clears the selection for the same reason).
  // Bailing out instead would silently no-op the post-deploy "View this fleet"
  // link for anyone who happens to have a view selected from a previous session —
  // `initialize()` above restores it from localStorage before this runs, and the
  // `activeFilterTags` watcher would then overwrite these tags once the views
  // load. That is a dead end for AC #5, not deference.
  systemViewsStore.clearSelection()

  selectedQuickTags.value = tags
  networkStore.setFilterTags([...tags])
  localStorage.setItem('trinity-dashboard-quick-tags', JSON.stringify(tags))
}

onMounted(async () => {
  // Document-level interaction is armed FIRST, above every await (#2200).
  //
  // The fleet paints as soon as fetchAgents() ALONE resolves (the
  // `agents.length > 0` template gate), but the await below waits on the
  // SLOWEST of five fetches. Registering down there left a window — ~50ms
  // measured, but `max(five) - fetchAgents()` and so unbounded on a large
  // fleet or a cold DB — in which the dashboard rendered as interactive and
  // every `/` was silently dropped. The slot was inherited, not chosen: the
  // ent#261 hotkey was appended beside handleClickOutside, whose position had
  // silently become post-await when PERF-269 introduced the parallel fetch.
  //
  // Safe by construction: neither handler reads fetched data — every guard in
  // handleDashboardKeydown reads the event or a setup()-created ref, and
  // handleClickOutside is a no-op while showTagDropdown is false. Registering
  // early only means `/` and click-outside work sooner.
  //
  // Do NOT move these below an await. Guarded by
  // tests/unit/mountListenerOrdering.spec.js.
  document.addEventListener('click', handleClickOutside) // tag dropdown dismiss
  document.addEventListener('keydown', handleDashboardKeydown) // ent#261 type-to-filter + Esc backstop

  // Initialize system views store (restores persisted view selection)
  systemViewsStore.initialize()

  // Apply persisted time range to network store
  networkStore.timeRangeHours = selectedTimeRange.value

  // Apply persisted quick tags filter (only if no system view is active)
  if (!systemViewsStore.activeViewId && selectedQuickTags.value.length > 0) {
    networkStore.setFilterTags([...selectedQuickTags.value])
  }

  // ent#126: ?view= / ?tags= override the persisted selection above.
  applyDeepLinkFilters()

  // PERF-269: Parallelize independent mount calls
  await Promise.allSettled([
    systemViewsStore.fetchViews(),
    networkStore.fetchAgents(),
    networkStore.fetchHistoricalCommunications(),
    networkStore.fetchSchedules(),
    fetchAvailableTags()
  ])

  // First-run onboarding: load the Claude-auth flag (for the wizard's setup
  // hint) and auto-open the wizard if this is a fresh, empty install.
  sessionsStore.loadFeatureFlags().catch(() => {})
  maybeAutoOpenOnboarding()

  // Connect WebSocket for real-time updates
  networkStore.connectWebSocket()

  // Start polling (PERF-269: reduced frequencies, visibility-aware)
  networkStore.startContextPolling()
  networkStore.startAgentRefresh()

  // Start activity refresh polling if in timeline mode (fallback for WebSocket gaps)
  if (networkStore.isTimelineMode) {
    networkStore.startActivityRefresh()
  }
})

onUnmounted(() => {
  networkStore.disconnectWebSocket()
  networkStore.stopContextPolling()
  networkStore.stopAgentRefresh()
  networkStore.stopActivityRefresh()
  document.removeEventListener('click', handleClickOutside)
  document.removeEventListener('keydown', handleDashboardKeydown)
  // Store state outlives the page — a lingering invisible filter after a
  // remount would lie (ent#261 honest-state AC).
  networkStore.setFilterQuery('')
})

async function refreshAll() {
  await networkStore.fetchAgents()
  await networkStore.fetchHistoricalCommunications()
  if (networkStore.viewMode === 'grid') {
    // Grid mode: re-pull chip batch data + re-hydrate visible tiles.
    fleetGridRef.value?.refresh()
  } else if (networkStore.viewMode === 'list') {
    // List mode: re-fetch sync health (the panel's only own data source —
    // fleet rows + tags are already refreshed by the fetches above).
    listPanelRef.value?.refresh()
  }
  // Timeline mode needs nothing extra — the two fetches above feed it.
}

async function onTimeRangeChange() {
  networkStore.timeRangeHours = selectedTimeRange.value
  // Persist time range to localStorage
  localStorage.setItem('trinity-dashboard-time-range', selectedTimeRange.value)
  await networkStore.fetchHistoricalCommunications()
}


function getNodeColor(node) {
  // System agent gets purple color
  if (node.data?.is_system) {
    return '#a855f7' // purple-500
  }

  const status = node.data?.status?.toLowerCase() || 'stopped'

  const colors = {
    running: '#06b6d4', // cyan-500
    stopped: '#94a3b8', // slate-400
    starting: '#f59e0b', // amber-500
    error: '#ef4444', // red-500
    exited: '#6b7280' // gray-500
  }

  return colors[status] || colors.stopped
}

function formatTime(timestamp) {
  const date = new Date(timestamp)
  const now = new Date()
  const diff = now - date

  if (diff < 60000) return `${Math.floor(diff / 1000)}s`
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m`
  return date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
}

// View Mode Functions
function toggleMode(mode) {
  networkStore.setViewMode(mode)
}

function handlePlay() {
  networkStore.startReplay()
}

function handlePause() {
  networkStore.pauseReplay()
}

function handleStop() {
  networkStore.stopReplay()
}

function handleSpeedChange(event) {
  const speed = parseInt(event.target.value)
  networkStore.setReplaySpeed(speed)
}

async function handleToggleAutonomy(agentName) {
  await networkStore.toggleAutonomy(agentName)
}

function handleTimelineClick(event) {
  const rect = event.currentTarget.getBoundingClientRect()
  const clickX = event.clientX - rect.left
  const timelineWidth = rect.width
  networkStore.handleTimelineClick(clickX, timelineWidth)
}

function formatDuration(ms) {
  if (!ms) return '00:00'
  const minutes = Math.floor(ms / 60000)
  const seconds = Math.floor((ms % 60000) / 1000)
  return `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`
}

function formatTimestamp(timestamp) {
  if (!timestamp) return '--:--'
  const date = new Date(timestamp)
  return date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
}

// Quick Tag Filter functions
async function fetchAvailableTags() {
  try {
    const response = await axios.get('/api/tags')
    // Org-overlay namespaces (dept-*/reports-to-*) are structural facts, not
    // browse filters — hidden here; the Grid renders them as zones/lines and
    // the AgentDetail tag editor still shows them (trinity-enterprise#305).
    availableTags.value = (response.data.tags || []).filter((t) => !isOrgTag(t.tag))
  } catch (err) {
    console.error('Failed to fetch tags:', err)
    availableTags.value = []
  }
}

function toggleQuickTag(tag) {
  const index = selectedQuickTags.value.indexOf(tag)
  if (index === -1) {
    selectedQuickTags.value.push(tag)
  } else {
    selectedQuickTags.value.splice(index, 1)
  }
  // Clear system view selection when using quick tags
  systemViewsStore.clearSelection()
  // Apply filter to network store
  networkStore.setFilterTags([...selectedQuickTags.value])
  // Persist quick tags to localStorage
  localStorage.setItem('trinity-dashboard-quick-tags', JSON.stringify(selectedQuickTags.value))
}

function clearQuickTags() {
  selectedQuickTags.value = []
  networkStore.setFilterTags([])
  // Clear persisted quick tags
  localStorage.removeItem('trinity-dashboard-quick-tags')
}

// Close dropdown when clicking outside — scoped to the dropdown's own element
// ref (ent#260 eng F10): the old `.closest('.relative')` heuristic kept the
// dropdown open on ANY click inside ANY `relative`-positioned element, which
// breaks once the list rows (position: relative) mount inside the chassis.
const tagDropdownRef = ref(null)
function handleClickOutside(event) {
  if (showTagDropdown.value && !tagDropdownRef.value?.contains(event.target)) {
    showTagDropdown.value = false
  }
}
</script>

<style scoped>
/*
  One onboarding card at a time (#2380).

  Every card in the stack is `v-if`'d, so a card that has nothing to say leaves
  no element behind — which makes "the first ELEMENT child" exactly "the
  highest-priority card that currently wants to speak". Hiding the rest in CSS
  keeps each card's visibility predicate where it already lives (its own store,
  its own localStorage dismissal) instead of lifting four of them into this
  view, and dismissing the top card reveals the next one for free.

  The wrapper carries no margin of its own on purpose: with every card hidden
  it collapses to a zero-height empty div rather than a phantom gap. Each card
  owns `mt-3 mb-3`, so whichever one shows is spaced on both sides — the pane
  below is a full-bleed surface with no top padding of its own.
*/
.onboarding-stack > * ~ * {
  display: none;
}


/*
 * Stats bar progressive degrade (#1830).
 *
 * The header row is [elastic stats cluster | fixed controls cluster]. Before
 * this, the stats cluster only ever got CLIPPED by `overflow-hidden` — the
 * telemetry meters were cut mid-element (and their boxes still ran under the
 * controls) with no visual affordance. Making the cluster a size container
 * lets the content drop out in a defined order as space runs out, keyed to the
 * width actually left over — so it adapts to a wider controls cluster (grid
 * mode, tags chip, owner filter) instead of a guessed viewport breakpoint.
 *
 * Ladder (widest → narrowest), split by ownership:
 *   ≤ 820px  sparklines drop        (HostTelemetry.vue)
 *   ≤ 700px  Disk meter drops       (HostTelemetry.vue)
 *   ≤ 560px  Mem meter drops        (HostTelemetry.vue)
 *   ≤ 420px  telemetry drops whole  (HostTelemetry.vue)
 *   ≤ 330px  "messages (Nh)" drops  (here)
 *   ≤ 200px  "working now" drops    (here)
 * The agent count always survives.
 *
 * Each threshold sits ~25-30px ABOVE the measured intrinsic width of the level
 * it gates (full 791 → no-sparks 663 → no-disk 522 → no-mem 394 → no-telemetry
 * 308 → no-messages 182 → agents-only 71), so every level still fits its own
 * band with headroom for wider live values (three-digit fleets, 100.0/128G).
 * Thresholds set below those widths reintroduce the clip this fixes — re-measure
 * before moving one.
 *
 * The container NAME is the contract HostTelemetry.vue queries against — keep
 * `statsbar` in sync with the @container rules there if it ever changes.
 */
.stats-cluster {
  container-type: inline-size;
  container-name: statsbar;
}

@container statsbar (max-width: 330px) {
  [data-stat='messages'],
  [data-sep='messages'] {
    display: none;
  }
}

@container statsbar (max-width: 200px) {
  [data-stat='working'],
  [data-sep='working'] {
    display: none;
  }
}

/* Replay Mode Styles */
.mode-toggle {
  transition: all 0.2s ease;
}

.mode-toggle button {
  transition: all 0.2s ease;
}

/* Timeline Scrubber Styles */
.timeline-scrubber {
  user-select: none;
}

.timeline-track {
  position: relative;
  transition: background-color 0.2s ease;
}

.timeline-track:hover {
  background-color: #e5e7eb;
}

.event-marker {
  transition: all 0.15s ease;
  z-index: 10;
}

.event-marker:hover {
  z-index: 20;
  transform: translate(-50%, -50%) scale(1.3);
}

.playback-marker {
  z-index: 30;
  pointer-events: none;
  transition: left 0.3s ease-out;
}

.playback-marker > div {
  pointer-events: all;
  transition: all 0.2s ease;
}

.playback-marker > div:hover {
  transform: translate(-50%, -50%) scale(1.2);
}

/* Replay controls hover effects */
button:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

button:not(:disabled):hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
}

button:not(:disabled):active {
  transform: translateY(0);
}

/* Loading animation for timeline */
@keyframes timeline-pulse {
  0%, 100% {
    opacity: 1;
  }
  50% {
    opacity: 0.5;
  }
}

.timeline-loading {
  animation: timeline-pulse 2s ease-in-out infinite;
}
</style>
