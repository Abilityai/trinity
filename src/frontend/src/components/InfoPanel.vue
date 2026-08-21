<template>
  <div class="space-y-6">
    <!-- Refresh failed with content on screen (#1927 × #1926): the agentStatus
         watcher re-fetches on Start/Restart and a Retry re-fetches too — both
         used to blow the rendered content away behind a spinner (design-system
         p13). The content stays; this SIBLING banner names the staleness and
         carries the retry. It is a sibling of the chain below, never an
         else-if arm, so it can never replace the data it promises to keep. -->
    <InlineError
      v-if="view.stale"
      :message="staleMessage"
      :detail="loadError"
      retryable
      :retry-label="loading ? 'Retrying…' : 'Try again'"
      @retry="loadTemplateInfo"
      @dismiss="loadError = ''"
    />

    <!-- Loading State — only while there is NO data yet (#1927, p13/p14):
         first mount and an agent switch (the agentName watcher nulls the data),
         never a refetch with content on screen. -->
    <div v-if="firstLoad" class="flex items-center justify-center py-8" data-testid="info-loading">
      <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-action-primary-500"></div>
    </div>

    <!-- Failed State (#1926) — a failed /info fetch used to be dressed up as
         has_template:false, i.e. "This agent was created without a template",
         which is a claim about the agent rather than about the request. Gated
         on "no data" too: with content on screen the failure is the banner above. -->
    <LoadFailed
      v-else-if="loadFailed"
      title="Couldn't load template info"
      :message="agentStatus === 'running'
        ? 'The agent did not return its template information. Try again.'
        : 'The agent is not running, so its template information is unavailable. Start the agent and try again.'"
      :detail="loadError"
      :retrying="loading"
      @retry="loadTemplateInfo"
    />

    <!-- No Template State — the agent answered, and has no template -->
    <div v-else-if="!templateInfo?.has_template" class="text-center py-8">
      <div class="mx-auto w-16 h-16 bg-gray-100 dark:bg-gray-700 rounded-full flex items-center justify-center mb-4">
        <svg class="w-8 h-8 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
        </svg>
      </div>
      <h3 class="text-lg font-medium text-gray-900 dark:text-white">No Template Information</h3>
      <p class="mt-2 text-sm text-gray-500 dark:text-gray-400 max-w-sm mx-auto">
        {{ templateInfo?.message || 'This agent was created without a template.' }}
      </p>
      <div v-if="templateInfo?.template_name" class="mt-4 text-sm text-gray-500 dark:text-gray-400">
        Template: <span class="font-mono text-gray-700 dark:text-gray-300">{{ templateInfo.template_name }}</span>
      </div>
    </div>

    <!-- Template Info Display -->
    <div v-else class="space-y-6" data-testid="info-content">
      <!-- Header Section -->
      <div class="bg-gradient-to-r from-action-primary-50 to-accent-purple-50 dark:from-action-primary-900/30 dark:to-accent-purple-900/30 rounded-lg p-6 border border-action-primary-100 dark:border-action-primary-800">
        <div class="flex items-start justify-between">
          <div>
            <h2 class="text-2xl font-bold text-gray-900 dark:text-white">
              {{ templateInfo.display_name || templateInfo.name }}
            </h2>
            <!-- Tagline -->
            <p v-if="templateInfo.tagline" class="mt-1 text-sm text-action-primary-600 dark:text-action-primary-400 font-medium">
              {{ templateInfo.tagline }}
            </p>
            <div class="mt-2 flex items-center space-x-3 text-sm text-gray-500 dark:text-gray-400">
              <span v-if="templateInfo.version" class="flex items-center">
                <svg class="w-4 h-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z" />
                </svg>
                v{{ templateInfo.version }}
              </span>
              <span v-if="templateInfo.author" class="flex items-center">
                <svg class="w-4 h-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
                </svg>
                {{ templateInfo.author }}
              </span>
              <span v-if="templateInfo.updated" class="flex items-center">
                <svg class="w-4 h-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
                </svg>
                {{ templateInfo.updated }}
              </span>
            </div>
          </div>
        </div>

        <!-- Description -->
        <p v-if="templateInfo.description" class="mt-4 text-gray-600 dark:text-gray-300 whitespace-pre-line">
          {{ templateInfo.description }}
        </p>
      </div>

      <!-- Use Cases Section -->
      <div v-if="templateInfo.use_cases && templateInfo.use_cases.length > 0" class="bg-white dark:bg-gray-800 rounded-lg p-5 border border-gray-200 dark:border-gray-700">
        <h3 class="text-sm font-semibold text-gray-900 dark:text-white uppercase tracking-wider mb-3 flex items-center">
          <svg class="w-4 h-4 mr-2 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
          </svg>
          What You Can Ask
        </h3>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-2">
          <div
            v-for="(useCase, index) in templateInfo.use_cases"
            :key="index"
            class="flex items-start space-x-2 px-3 py-2 bg-gray-50 dark:bg-gray-700 rounded-lg hover:bg-action-primary-50 dark:hover:bg-action-primary-900/30 hover:border-action-primary-200 dark:hover:border-action-primary-700 border border-transparent transition-colors cursor-pointer"
            @click="handleUseCaseClick(useCase)"
            title="Click to run this task"
          >
            <svg class="w-4 h-4 text-action-primary-400 mt-0.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
            </svg>
            <span class="text-sm text-gray-700 dark:text-gray-200">"{{ useCase }}"</span>
          </div>
        </div>
      </div>

      <!-- Technical details (#1107): exhaustive template.yaml metadata tucked
           behind a collapsible disclosure so the About narrative leads. -->
      <details v-if="hasTechnicalDetails" class="group">
        <summary class="flex items-center justify-between cursor-pointer select-none px-5 py-3 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-700/50">
          <span class="text-sm font-semibold text-gray-900 dark:text-white uppercase tracking-wider flex items-center">
            <svg class="w-4 h-4 mr-2 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
            Technical details
          </span>
          <svg class="w-4 h-4 text-gray-400 transition-transform group-open:rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
          </svg>
        </summary>
        <div class="space-y-6 mt-4">

      <!-- Resources Section -->
      <div v-if="templateInfo.resources && Object.keys(templateInfo.resources).length > 0" class="bg-white dark:bg-gray-800 rounded-lg p-5 border border-gray-200 dark:border-gray-700">
        <h3 class="text-sm font-semibold text-gray-900 dark:text-white uppercase tracking-wider mb-3 flex items-center">
          <svg class="w-4 h-4 mr-2 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z" />
          </svg>
          Resources
        </h3>
        <div class="flex space-x-6">
          <div v-if="templateInfo.resources.cpu" class="flex items-center space-x-2">
            <span class="text-sm text-gray-500 dark:text-gray-400">CPU:</span>
            <span class="text-sm font-mono font-medium text-gray-900 dark:text-gray-100">{{ templateInfo.resources.cpu }} cores</span>
          </div>
          <div v-if="templateInfo.resources.memory" class="flex items-center space-x-2">
            <span class="text-sm text-gray-500 dark:text-gray-400">Memory:</span>
            <span class="text-sm font-mono font-medium text-gray-900 dark:text-gray-100">{{ templateInfo.resources.memory }}</span>
          </div>
        </div>
      </div>

      <!-- Sub-Agents Section -->
      <div v-if="templateInfo.sub_agents && templateInfo.sub_agents.length > 0" class="bg-white dark:bg-gray-800 rounded-lg p-5 border border-gray-200 dark:border-gray-700">
        <h3 class="text-sm font-semibold text-gray-900 dark:text-white uppercase tracking-wider mb-3 flex items-center">
          <svg class="w-4 h-4 mr-2 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
          </svg>
          Sub-Agents ({{ templateInfo.sub_agents.length }})
        </h3>
        <div class="space-y-2">
          <div
            v-for="subAgent in templateInfo.sub_agents"
            :key="getItemName(subAgent)"
            class="flex items-start space-x-3 px-3 py-2 bg-blue-50 dark:bg-blue-900/30 rounded-lg hover:bg-blue-100 dark:hover:bg-blue-900/50 cursor-pointer transition-colors"
            @click="handleSubAgentClick(subAgent)"
            title="Click to delegate task to this sub-agent"
          >
            <div class="w-2 h-2 bg-blue-400 rounded-full mt-1.5 flex-shrink-0"></div>
            <div class="flex-1 min-w-0">
              <span class="text-sm font-medium text-blue-800 dark:text-blue-300">{{ getItemName(subAgent) }}</span>
              <p v-if="getItemDescription(subAgent)" class="text-xs text-blue-600 dark:text-blue-400 mt-0.5">
                {{ getItemDescription(subAgent) }}
              </p>
            </div>
            <svg class="w-4 h-4 text-blue-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
            </svg>
          </div>
        </div>
      </div>

      <!-- Commands Section -->
      <div v-if="templateInfo.commands && templateInfo.commands.length > 0" class="bg-white dark:bg-gray-800 rounded-lg p-5 border border-gray-200 dark:border-gray-700">
        <h3 class="text-sm font-semibold text-gray-900 dark:text-white uppercase tracking-wider mb-3 flex items-center">
          <svg class="w-4 h-4 mr-2 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
          </svg>
          Slash Commands ({{ templateInfo.commands.length }})
        </h3>
        <div class="space-y-2">
          <div
            v-for="command in templateInfo.commands"
            :key="getItemName(command)"
            class="flex items-start space-x-3 px-3 py-2 bg-accent-purple-50 dark:bg-accent-purple-900/30 rounded-lg hover:bg-accent-purple-100 dark:hover:bg-accent-purple-900/50 cursor-pointer transition-colors"
            @click="handleCommandClick(command)"
            title="Click to run this command"
          >
            <span class="text-sm font-mono font-medium text-accent-purple-800 dark:text-accent-purple-300 flex-shrink-0">/{{ getItemName(command) }}</span>
            <p v-if="getItemDescription(command)" class="text-xs text-accent-purple-600 dark:text-accent-purple-400 mt-0.5 flex-1">
              {{ getItemDescription(command) }}
            </p>
            <svg class="w-4 h-4 text-accent-purple-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
            </svg>
          </div>
        </div>
      </div>

      <!-- MCP Servers Section -->
      <div v-if="templateInfo.mcp_servers && templateInfo.mcp_servers.length > 0" class="bg-white dark:bg-gray-800 rounded-lg p-5 border border-gray-200 dark:border-gray-700">
        <h3 class="text-sm font-semibold text-gray-900 dark:text-white uppercase tracking-wider mb-3 flex items-center">
          <svg class="w-4 h-4 mr-2 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2m-2-4h.01M17 16h.01" />
          </svg>
          MCP Servers ({{ templateInfo.mcp_servers.length }})
        </h3>
        <div class="space-y-2">
          <div
            v-for="server in templateInfo.mcp_servers"
            :key="getItemName(server)"
            class="flex items-start space-x-3 px-3 py-2 bg-status-warning-50 dark:bg-status-warning-900/30 rounded-lg"
          >
            <span class="text-sm font-mono font-medium text-status-warning-800 dark:text-status-warning-300 flex-shrink-0">{{ getItemName(server) }}</span>
            <p v-if="getItemDescription(server)" class="text-xs text-status-warning-700 dark:text-status-warning-400 mt-0.5">
              {{ getItemDescription(server) }}
            </p>
          </div>
        </div>
      </div>

      <!-- Skills Section -->
      <div v-if="templateInfo.skills && templateInfo.skills.length > 0" class="bg-white dark:bg-gray-800 rounded-lg p-5 border border-gray-200 dark:border-gray-700">
        <h3 class="text-sm font-semibold text-gray-900 dark:text-white uppercase tracking-wider mb-3 flex items-center">
          <svg class="w-4 h-4 mr-2 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
          </svg>
          Skills ({{ templateInfo.skills.length }})
        </h3>
        <div class="space-y-2">
          <div
            v-for="skill in templateInfo.skills"
            :key="getItemName(skill)"
            class="flex items-start space-x-3 px-3 py-2 bg-teal-50 dark:bg-teal-900/30 rounded-lg"
          >
            <span class="text-sm font-medium text-teal-800 dark:text-teal-300 flex-shrink-0">{{ getItemName(skill) }}</span>
            <p v-if="getItemDescription(skill)" class="text-xs text-teal-600 dark:text-teal-400 mt-0.5">
              {{ getItemDescription(skill) }}
            </p>
          </div>
        </div>
      </div>

      <!-- Capabilities Section -->
      <div v-if="templateInfo.capabilities && templateInfo.capabilities.length > 0" class="bg-white dark:bg-gray-800 rounded-lg p-5 border border-gray-200 dark:border-gray-700">
        <h3 class="text-sm font-semibold text-gray-900 dark:text-white uppercase tracking-wider mb-3 flex items-center">
          <svg class="w-4 h-4 mr-2 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
          </svg>
          Capabilities
        </h3>
        <div class="flex flex-wrap gap-2">
          <span
            v-for="capability in templateInfo.capabilities"
            :key="capability"
            class="px-3 py-1 bg-status-success-100 dark:bg-status-success-900/50 text-status-success-800 dark:text-status-success-300 text-sm font-medium rounded-full"
          >
            {{ formatCapability(capability) }}
          </span>
        </div>
      </div>

      <!-- Platforms Section -->
      <div v-if="templateInfo.platforms && templateInfo.platforms.length > 0" class="bg-white dark:bg-gray-800 rounded-lg p-5 border border-gray-200 dark:border-gray-700">
        <h3 class="text-sm font-semibold text-gray-900 dark:text-white uppercase tracking-wider mb-3 flex items-center">
          <svg class="w-4 h-4 mr-2 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9" />
          </svg>
          Supported Platforms
        </h3>
        <div class="flex flex-wrap gap-2">
          <span
            v-for="platform in templateInfo.platforms"
            :key="platform"
            class="px-3 py-1 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 text-sm font-medium rounded-full capitalize"
          >
            {{ platform }}
          </span>
        </div>
      </div>

      <!-- Tools Section -->
      <div v-if="templateInfo.tools && templateInfo.tools.length > 0" class="bg-white dark:bg-gray-800 rounded-lg p-5 border border-gray-200 dark:border-gray-700">
        <h3 class="text-sm font-semibold text-gray-900 dark:text-white uppercase tracking-wider mb-3 flex items-center">
          <svg class="w-4 h-4 mr-2 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
          </svg>
          Enabled Tools
        </h3>
        <div class="flex flex-wrap gap-2">
          <span
            v-for="tool in templateInfo.tools"
            :key="tool"
            class="px-3 py-1 bg-status-urgent-100 dark:bg-status-urgent-900/50 text-status-urgent-800 dark:text-status-urgent-300 text-sm font-medium rounded-full"
          >
            {{ tool }}
          </span>
        </div>
      </div>
        </div>
      </details>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useAgentsStore } from '../stores/agents'
import LoadFailed from './LoadFailed.vue'
import InlineError from './InlineError.vue'
import { apiErrorMessage } from '../utils/apiError'
import { viewState, staleBannerMessage } from '../utils/loadingState'

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

const emit = defineEmits(['item-click'])

const agentsStore = useAgentsStore()
const templateInfo = ref(null)
const loading = ref(true)
// #1926: "the request failed" is a different state from "the agent has no
// template" — the old code collapsed the first into the second.
const loadError = ref('')
const lastLoadedAt = ref(null)

// #1927: loading/failed/empty/stale from ONE rule (utils/loadingState.js).
// `loading` keeps meaning "fetch in flight" (it drives the Retry labels); what
// the template gates on is "no data yet" — `templateInfo === null`.
const view = computed(() => viewState({
  loading: loading.value,
  hasLoaded: templateInfo.value !== null,
  error: loadError.value,
}))
// The genuine first load of this mount (or of a newly selected agent). Exposed
// as one boolean so the #1921 ScanlineReveal swap is mechanical (`:loading="firstLoad"`).
const firstLoad = computed(() => view.value.state === 'loading')
const loadFailed = computed(() => view.value.state === 'failed')
const staleMessage = computed(() => staleBannerMessage('template info', lastLoadedAt.value))

// #1107: whether any technical-metadata section has content (gates the
// collapsible "Technical details" disclosure).
const hasTechnicalDetails = computed(() => {
  const t = templateInfo.value
  if (!t) return false
  return Boolean(
    (t.resources && Object.keys(t.resources).length) ||
    (t.sub_agents && t.sub_agents.length) ||
    (t.commands && t.commands.length) ||
    (t.mcp_servers && t.mcp_servers.length) ||
    (t.skills && t.skills.length) ||
    (t.capabilities && t.capabilities.length) ||
    (t.platforms && t.platforms.length) ||
    (t.tools && t.tools.length)
  )
})

const loadTemplateInfo = async () => {
  loading.value = true
  loadError.value = ''
  try {
    const response = await agentsStore.getAgentInfo(props.agentName)
    templateInfo.value = response
    lastLoadedAt.value = Date.now()
    loadError.value = ''
  } catch (error) {
    console.error('Failed to load template info:', error)
    // Do NOT synthesize a has_template:false payload here — that renders the
    // failure as the empty state (#1926). Keep the last good value (if any)
    // and let the failed state own the surface.
    loadError.value = apiErrorMessage(error, 'Request failed')
  } finally {
    loading.value = false
  }
}

// Get name from item - handles both string and {name, description} object
const getItemName = (item) => {
  if (typeof item === 'string') return item
  return item?.name || ''
}

// Get description from item - returns null for strings
const getItemDescription = (item) => {
  if (typeof item === 'string') return null
  return item?.description || null
}

const formatCapability = (capability) => {
  // Convert snake_case to Title Case
  return capability
    .split('_')
    .map(word => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}

const handleItemClick = (type, text) => {
  // Emit event to parent to open Tasks tab with this text
  emit('item-click', { type, text })
}

const handleUseCaseClick = (text) => {
  handleItemClick('use-case', text)
}

const handleCommandClick = (command) => {
  const commandName = getItemName(command)
  handleItemClick('command', `/${commandName}`)
}

const handleSubAgentClick = (subAgent) => {
  const agentName = getItemName(subAgent)
  const description = getItemDescription(subAgent)
  // Create a prompt to delegate to the sub-agent
  const prompt = description
    ? `Ask ${agentName} to help with: ${description}`
    : `Ask ${agentName} to help with a task`
  handleItemClick('sub-agent', prompt)
}

// Reload when agent name changes (navigating between agents)
watch(() => props.agentName, (newName, oldName) => {
  if (newName && newName !== oldName) {
    // Reset state and reload for new agent
    templateInfo.value = null
    loadTemplateInfo()
  }
})

// Reload when agent status changes to running (to get full info). #1927: with
// content already on screen this refetch is silent — the spinner is gated on
// "no data yet", so the values swap in place and a failure shows the stale banner.
watch(() => props.agentStatus, (newStatus) => {
  if (newStatus === 'running') {
    loadTemplateInfo()
  }
})

onMounted(() => {
  loadTemplateInfo()
})
</script>
