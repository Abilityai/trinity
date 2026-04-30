<template>
  <div class="space-y-6">
    <!-- Loading State -->
    <div v-if="loading" class="flex items-center justify-center py-8">
      <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-indigo-500"></div>
    </div>

    <!-- Agent Not Running State -->
    <div v-else-if="agentStatus !== 'running'" class="text-center py-8">
      <div class="mx-auto w-16 h-16 bg-gray-100 dark:bg-gray-700 rounded-full flex items-center justify-center mb-4">
        <svg class="w-8 h-8 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 5a1 1 0 011-1h14a1 1 0 011 1v2a1 1 0 01-1 1H5a1 1 0 01-1-1V5zM4 13a1 1 0 011-1h6a1 1 0 011 1v6a1 1 0 01-1 1H5a1 1 0 01-1-1v-6zM16 13a1 1 0 011-1h2a1 1 0 011 1v6a1 1 0 01-1 1h-2a1 1 0 01-1-1v-6z" />
        </svg>
      </div>
      <h3 class="text-lg font-medium text-gray-900 dark:text-white">Agent Not Running</h3>
      <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">
        Start the agent to view its dashboard.
      </p>
    </div>

    <!-- Error State (only when no cached dashboard available) -->
    <div v-else-if="dashboardData?.error && !dashboardData?.has_dashboard" class="text-center py-8">
      <div class="mx-auto w-16 h-16 bg-status-danger-100 dark:bg-status-danger-900/30 rounded-full flex items-center justify-center mb-4">
        <svg class="w-8 h-8 text-status-danger-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
        </svg>
      </div>
      <h3 class="text-lg font-medium text-gray-900 dark:text-white">Dashboard Error</h3>
      <p class="mt-2 text-sm text-gray-500 dark:text-gray-400 max-w-md mx-auto">
        {{ dashboardData?.error }}
      </p>
    </div>

    <!-- No Dashboard Defined State -->
    <div v-else-if="!dashboardData?.has_dashboard" class="text-center py-8">
      <div class="mx-auto w-16 h-16 bg-gray-100 dark:bg-gray-700 rounded-full flex items-center justify-center mb-4">
        <svg class="w-8 h-8 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 5a1 1 0 011-1h14a1 1 0 011 1v2a1 1 0 01-1 1H5a1 1 0 01-1-1V5zM4 13a1 1 0 011-1h6a1 1 0 011 1v6a1 1 0 01-1 1H5a1 1 0 01-1-1v-6zM16 13a1 1 0 011-1h2a1 1 0 011 1v6a1 1 0 01-1 1h-2a1 1 0 01-1-1v-6z" />
        </svg>
      </div>
      <h3 class="text-lg font-medium text-gray-900 dark:text-white">No Dashboard Defined</h3>
      <p class="mt-2 text-sm text-gray-500 dark:text-gray-400 max-w-sm mx-auto">
        This agent does not have a dashboard.yaml file.
      </p>
      <div class="mt-4 text-xs text-gray-400 dark:text-gray-500">
        Create <code class="bg-gray-100 dark:bg-gray-700 px-1 py-0.5 rounded">~/dashboard.yaml</code> to define a custom dashboard.
      </div>
    </div>

    <!-- Dashboard Display -->
    <div v-else class="space-y-6">
      <!-- Stale Dashboard Warning Banner -->
      <div v-if="dashboardData.stale" class="rounded-md bg-status-warning-50 dark:bg-status-warning-900/20 border border-status-warning-200 dark:border-status-warning-800 p-3">
        <div class="flex items-start">
          <svg class="w-5 h-5 text-status-warning-400 mt-0.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
          </svg>
          <div class="ml-3 flex-1">
            <p class="text-sm font-medium text-status-warning-800 dark:text-status-warning-200">
              Showing cached dashboard
            </p>
            <p class="mt-1 text-xs text-status-warning-700 dark:text-status-warning-300">
              {{ dashboardData.stale_reason }}
            </p>
          </div>
        </div>
      </div>

      <!-- Widget Validation Warnings Banner -->
      <div v-if="dashboardData.warnings?.length" class="rounded-md bg-state-autonomous-50 dark:bg-state-autonomous-900/20 border border-state-autonomous-200 dark:border-state-autonomous-800 p-3">
        <div class="flex items-start">
          <svg class="w-5 h-5 text-state-autonomous-400 mt-0.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <div class="ml-3 flex-1">
            <p class="text-sm font-medium text-state-autonomous-800 dark:text-state-autonomous-200">
              {{ dashboardData.warnings.length }} widget{{ dashboardData.warnings.length > 1 ? 's' : '' }} skipped due to validation errors
            </p>
            <ul class="mt-1 text-xs text-state-autonomous-700 dark:text-state-autonomous-300 list-disc list-inside">
              <li v-for="(warning, idx) in dashboardData.warnings" :key="idx">{{ warning }}</li>
            </ul>
          </div>
        </div>
      </div>

      <!-- Header -->
      <div class="flex items-center justify-between">
        <div>
          <h2 class="text-xl font-semibold text-gray-900 dark:text-white">
            {{ dashboardData.config.title }}
          </h2>
          <p v-if="dashboardData.config.description" class="mt-1 text-sm text-gray-500 dark:text-gray-400">
            {{ dashboardData.config.description }}
          </p>
        </div>
        <div class="flex items-center space-x-3 text-xs text-gray-500 dark:text-gray-400">
          <span v-if="dashboardData.last_modified">
            Updated {{ formatRelativeTime(dashboardData.last_modified) }}
          </span>
          <button
            v-if="hasUpdateDashboardPlaybook"
            @click="triggerUpdateDashboard"
            :disabled="updatingDashboard"
            class="inline-flex items-center px-2.5 py-1.5 text-xs font-medium rounded bg-indigo-50 text-indigo-700 hover:bg-indigo-100 dark:bg-indigo-900/50 dark:text-indigo-300 dark:hover:bg-indigo-900/70 disabled:opacity-50"
            title="Run /update-dashboard playbook"
          >
            <svg v-if="updatingDashboard" class="w-3.5 h-3.5 mr-1 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
            <svg v-else class="w-3.5 h-3.5 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
            Update Dashboard
          </button>
          <button
            @click="loadDashboard"
            :disabled="loading"
            class="p-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-700"
            title="Refresh dashboard"
          >
            <svg :class="['w-4 h-4', loading ? 'animate-spin' : '']" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </button>
        </div>
      </div>

      <!-- Sections -->
      <div
        v-for="(section, sectionIndex) in dashboardData.config.sections"
        :key="sectionIndex"
        class="space-y-4"
        :class="section.platform_managed ? 'mt-6 pt-6 border-t border-indigo-200 dark:border-indigo-800' : ''"
      >
        <!-- Section Header -->
        <div v-if="section.title" class="border-b border-gray-200 dark:border-gray-700 pb-2" :class="section.platform_managed ? 'border-indigo-200 dark:border-indigo-800' : ''">
          <div class="flex items-center gap-2">
            <h3 class="text-sm font-semibold uppercase tracking-wider" :class="section.platform_managed ? 'text-indigo-600 dark:text-indigo-400' : 'text-gray-900 dark:text-white'">
              {{ section.title }}
            </h3>
            <span v-if="section.platform_managed" class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-indigo-100 text-indigo-700 dark:bg-indigo-900/50 dark:text-indigo-300">
              Auto
            </span>
          </div>
          <p v-if="section.description" class="mt-1 text-xs text-gray-500 dark:text-gray-400">
            {{ section.description }}
          </p>
        </div>

        <!-- Widgets Grid -->
        <div
          :class="[
            section.layout === 'list' ? 'space-y-4' : 'grid gap-4',
            section.layout !== 'list' && `grid-cols-1 sm:grid-cols-2 lg:grid-cols-${section.columns || 3}`
          ]"
        >
          <template v-for="(widget, widgetIndex) in section.widgets" :key="widgetIndex">
            <!-- Metric Widget -->
            <div
              v-if="widget.type === 'metric'"
              class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4"
              :class="{ 'col-span-2': widget.colspan === 2 }"
            >
              <div class="flex items-baseline justify-between">
                <div class="text-3xl font-bold text-gray-900 dark:text-white">
                  {{ formatValue(widget.value) }}
                  <span v-if="widget.unit" class="text-lg text-gray-400 dark:text-gray-500">{{ widget.unit }}</span>
                </div>
                <div v-if="widget.trend || widget.history?.trend" class="flex items-center text-sm" :class="getTrendColor(widget.trend || widget.history?.trend)">
                  <svg v-if="(widget.trend || widget.history?.trend) === 'up'" class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                    <path fill-rule="evenodd" d="M5.293 9.707a1 1 0 010-1.414l4-4a1 1 0 011.414 0l4 4a1 1 0 01-1.414 1.414L11 7.414V15a1 1 0 11-2 0V7.414L6.707 9.707a1 1 0 01-1.414 0z" clip-rule="evenodd" />
                  </svg>
                  <svg v-else-if="(widget.trend || widget.history?.trend) === 'down'" class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                    <path fill-rule="evenodd" d="M14.707 10.293a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 111.414-1.414L9 12.586V5a1 1 0 012 0v7.586l2.293-2.293a1 1 0 011.414 0z" clip-rule="evenodd" />
                  </svg>
                  <span v-if="widget.trend_value || widget.history?.trend_percent" class="ml-1">
                    {{ widget.trend_value || (widget.history?.trend_percent ? `${widget.history.trend_percent > 0 ? '+' : ''}${widget.history.trend_percent}%` : '') }}
                  </span>
                </div>
              </div>
              <div class="mt-1 text-sm text-gray-500 dark:text-gray-400">{{ widget.label }}</div>
              <div v-if="widget.description" class="mt-1 text-xs text-gray-400 dark:text-gray-500">{{ widget.description }}</div>
              <!-- Sparkline -->
              <SparklineChart
                v-if="widget.history?.values?.length > 1"
                :data="widget.history.values.map(v => v.v)"
                :color="getSparklineColor(widget)"
                :y-max="widget.history.max || 100"
                :width="120"
                :height="24"
                class="mt-2"
              />
            </div>

            <!-- Status Widget -->
            <div
              v-else-if="widget.type === 'status'"
              class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4"
              :class="{ 'col-span-2': widget.colspan === 2 }"
            >
              <span
                class="inline-flex items-center px-2.5 py-1 text-sm font-medium rounded-full"
                :class="getStatusColors(widget.color)"
              >
                {{ widget.value }}
              </span>
              <div class="mt-2 text-sm text-gray-500 dark:text-gray-400">{{ widget.label }}</div>
              <div v-if="widget.description" class="mt-1 text-xs text-gray-400 dark:text-gray-500">{{ widget.description }}</div>
            </div>

            <!-- Progress Widget -->
            <div
              v-else-if="widget.type === 'progress'"
              class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4"
              :class="{ 'col-span-2': widget.colspan === 2 }"
            >
              <div class="flex items-baseline justify-between">
                <span class="text-2xl font-bold text-gray-900 dark:text-white">
                  {{ widget.value }}%
                </span>
                <div v-if="widget.history?.trend" class="flex items-center text-sm" :class="getTrendColor(widget.history.trend)">
                  <svg v-if="widget.history.trend === 'up'" class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                    <path fill-rule="evenodd" d="M5.293 9.707a1 1 0 010-1.414l4-4a1 1 0 011.414 0l4 4a1 1 0 01-1.414 1.414L11 7.414V15a1 1 0 11-2 0V7.414L6.707 9.707a1 1 0 01-1.414 0z" clip-rule="evenodd" />
                  </svg>
                  <svg v-else-if="widget.history.trend === 'down'" class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                    <path fill-rule="evenodd" d="M14.707 10.293a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 111.414-1.414L9 12.586V5a1 1 0 012 0v7.586l2.293-2.293a1 1 0 011.414 0z" clip-rule="evenodd" />
                  </svg>
                  <span v-if="widget.history.trend_percent" class="ml-1">{{ widget.history.trend_percent > 0 ? '+' : '' }}{{ widget.history.trend_percent }}%</span>
                </div>
              </div>
              <div class="mt-2 w-full h-2 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
                <div
                  class="h-full rounded-full transition-all duration-500"
                  :class="getProgressBarColor(widget.color)"
                  :style="{ width: `${Math.min(100, widget.value || 0)}%` }"
                ></div>
              </div>
              <div class="mt-2 text-sm text-gray-500 dark:text-gray-400">{{ widget.label }}</div>
              <!-- Sparkline -->
              <SparklineChart
                v-if="widget.history?.values?.length > 1"
                :data="widget.history.values.map(v => v.v)"
                :color="getSparklineColor(widget)"
                :y-max="100"
                :width="120"
                :height="24"
                class="mt-2"
              />
            </div>

            <!-- Text Widget -->
            <div
              v-else-if="widget.type === 'text'"
              class="p-2"
              :class="[
                widget.colspan === 2 ? 'col-span-2' : '',
                getTextSize(widget.size),
                getTextColor(widget.color),
                getTextAlign(widget.align)
              ]"
            >
              {{ widget.content }}
            </div>

            <!-- Markdown Widget -->
            <div
              v-else-if="widget.type === 'markdown'"
              class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4"
              :class="{ 'col-span-2': widget.colspan === 2 }"
            >
              <div class="prose prose-sm dark:prose-invert max-w-none" v-html="renderMarkdown(widget.content)"></div>
            </div>

            <!-- Table Widget -->
            <div
              v-else-if="widget.type === 'table'"
              class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden"
              :class="{ 'col-span-2': widget.colspan === 2 }"
            >
              <div v-if="widget.title" class="px-4 py-3 border-b border-gray-200 dark:border-gray-700">
                <h4 class="text-sm font-medium text-gray-900 dark:text-white">{{ widget.title }}</h4>
              </div>
              <div class="overflow-x-auto">
                <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                  <thead class="bg-gray-50 dark:bg-gray-900">
                    <tr>
                      <th
                        v-for="col in widget.columns"
                        :key="col.key"
                        class="px-4 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider"
                      >
                        {{ col.label }}
                      </th>
                    </tr>
                  </thead>
                  <tbody class="divide-y divide-gray-200 dark:divide-gray-700">
                    <tr v-for="(row, rowIndex) in getTableRows(widget)" :key="rowIndex">
                      <td
                        v-for="col in widget.columns"
                        :key="col.key"
                        class="px-4 py-2 text-sm text-gray-700 dark:text-gray-300"
                      >
                        {{ row[col.key] }}
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>

            <!-- List Widget -->
            <div
              v-else-if="widget.type === 'list'"
              class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4"
              :class="{ 'col-span-2': widget.colspan === 2 }"
            >
              <div v-if="widget.title" class="mb-2 text-sm font-medium text-gray-900 dark:text-white">
                {{ widget.title }}
              </div>
              <ul :class="getListStyle(widget.style)">
                <li
                  v-for="(item, itemIndex) in getListItems(widget)"
                  :key="itemIndex"
                  class="text-sm text-gray-700 dark:text-gray-300"
                >
                  {{ item }}
                </li>
              </ul>
            </div>

            <!-- Link Widget -->
            <div
              v-else-if="widget.type === 'link'"
              class="p-2"
              :class="{ 'col-span-2': widget.colspan === 2 }"
            >
              <a
                :href="widget.url"
                :target="widget.external ? '_blank' : '_self'"
                :class="getLinkStyle(widget)"
              >
                {{ widget.label }}
                <svg v-if="widget.external" class="inline w-3 h-3 ml-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                </svg>
              </a>
            </div>

            <!-- Divider Widget -->
            <div
              v-else-if="widget.type === 'divider'"
              class="col-span-full"
            >
              <hr class="border-gray-200 dark:border-gray-700" />
            </div>

            <!-- Spacer Widget -->
            <div
              v-else-if="widget.type === 'spacer'"
              :class="[
                'col-span-full',
                widget.size === 'sm' ? 'h-2' : widget.size === 'lg' ? 'h-8' : 'h-4'
              ]"
            ></div>

            <!-- Image Widget -->
            <div
              v-else-if="widget.type === 'image'"
              class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden"
              :class="{ 'col-span-2': widget.colspan === 2 }"
            >
              <img
                :src="getImageUrl(widget.src)"
                :alt="widget.alt"
                class="w-full h-auto"
              />
              <div v-if="widget.caption" class="px-4 py-2 text-xs text-gray-500 dark:text-gray-400 text-center">
                {{ widget.caption }}
              </div>
            </div>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { renderMarkdown } from '../utils/markdown'
import { useAgentsStore } from '../stores/agents'
import { useAuthStore } from '../stores/auth'
import axios from 'axios'
import SparklineChart from './SparklineChart.vue'

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
const authStore = useAuthStore()
const dashboardData = ref(null)
const loading = ref(true)
const hasUpdateDashboardPlaybook = ref(false)
const updatingDashboard = ref(false)
let refreshInterval = null

const loadDashboard = async () => {
  loading.value = true
  try {
    const response = await agentsStore.getAgentDashboard(props.agentName)
    dashboardData.value = response
  } catch (error) {
    console.error('Failed to load dashboard:', error)
    dashboardData.value = {
      has_dashboard: false,
      error: 'Failed to load dashboard'
    }
  } finally {
    loading.value = false
  }
}

// Check if agent has an update-dashboard playbook
const checkUpdateDashboardPlaybook = async () => {
  try {
    const response = await axios.get(`/api/agents/${props.agentName}/playbooks`, {
      headers: authStore.authHeader
    })
    const skills = response.data?.skills || response.data || []
    hasUpdateDashboardPlaybook.value = skills.some(
      s => s.name === 'update-dashboard'
    )
  } catch {
    hasUpdateDashboardPlaybook.value = false
  }
}

// Trigger the /update-dashboard playbook via task endpoint
const triggerUpdateDashboard = async () => {
  updatingDashboard.value = true
  try {
    await axios.post(`/api/agents/${props.agentName}/task`, {
      message: '/update-dashboard'
    }, {
      headers: authStore.authHeader
    })
    // Wait a bit then refresh the dashboard to show updated data
    setTimeout(() => {
      loadDashboard()
      updatingDashboard.value = false
    }, 5000)
  } catch (error) {
    console.error('Failed to trigger update-dashboard:', error)
    updatingDashboard.value = false
  }
}

// Format value with locale
const formatValue = (value) => {
  if (value === null || value === undefined) return '-'
  if (typeof value === 'number') {
    return value.toLocaleString('en-US', { maximumFractionDigits: 2 })
  }
  return value
}

// Format relative time
const formatRelativeTime = (isoString) => {
  if (!isoString) return ''
  const date = new Date(isoString)
  const now = new Date()
  const diffSeconds = Math.floor((now - date) / 1000)
  if (diffSeconds < 60) return 'just now'
  if (diffSeconds < 3600) return `${Math.floor(diffSeconds / 60)}m ago`
  if (diffSeconds < 86400) return `${Math.floor(diffSeconds / 3600)}h ago`
  return `${Math.floor(diffSeconds / 86400)}d ago`
}

// renderMarkdown imported from utils/markdown

// Get trend color
const getTrendColor = (trend) => {
  if (trend === 'up') return 'text-status-success-600'
  if (trend === 'down') return 'text-status-danger-600'
  return 'text-gray-500'
}

// Get sparkline color based on trend
const getSparklineColor = (widget) => {
  const trend = widget.history?.trend
  if (trend === 'up') return '#10b981'  // green-500
  if (trend === 'down') return '#ef4444'  // red-500
  return '#3b82f6'  // blue-500 (stable)
}

// Get status badge colors
const getStatusColors = (color) => {
  const colorMap = {
    green: 'bg-status-success-100 text-status-success-800 dark:bg-status-success-900/50 dark:text-status-success-300',
    red: 'bg-status-danger-100 text-status-danger-800 dark:bg-status-danger-900/50 dark:text-status-danger-300',
    yellow: 'bg-status-warning-100 text-status-warning-800 dark:bg-status-warning-900/50 dark:text-status-warning-300',
    gray: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
    blue: 'bg-status-info-100 text-status-info-800 dark:bg-status-info-900/50 dark:text-status-info-300',
    orange: 'bg-status-urgent-100 text-status-urgent-800 dark:bg-status-urgent-900/50 dark:text-status-urgent-300',
    purple: 'bg-accent-accent-purple-100 text-accent-accent-purple-800 dark:bg-accent-accent-purple-900/50 dark:text-accent-accent-purple-300'
  }
  return colorMap[color] || colorMap.gray
}

// Get progress bar color
const getProgressBarColor = (color) => {
  const colorMap = {
    green: 'bg-status-success-500',
    red: 'bg-status-danger-500',
    yellow: 'bg-status-warning-500',
    blue: 'bg-blue-500',
    orange: 'bg-status-urgent-500',
    purple: 'bg-accent-purple-500'
  }
  return colorMap[color] || 'bg-blue-500'
}

// Text helpers
const getTextSize = (size) => {
  const sizes = { xs: 'text-xs', sm: 'text-sm', md: 'text-base', lg: 'text-lg' }
  return sizes[size] || 'text-sm'
}

const getTextColor = (color) => {
  if (color === 'gray') return 'text-gray-500 dark:text-gray-400'
  return 'text-gray-700 dark:text-gray-300'
}

const getTextAlign = (align) => {
  const aligns = { left: 'text-left', center: 'text-center', right: 'text-right' }
  return aligns[align] || 'text-left'
}

// Table helpers
const getTableRows = (widget) => {
  if (!widget.rows) return []
  const maxRows = widget.max_rows || widget.rows.length
  return widget.rows.slice(0, maxRows)
}

// List helpers
const getListStyle = (style) => {
  if (style === 'number') return 'list-decimal list-inside space-y-1'
  if (style === 'none') return 'space-y-1'
  return 'list-disc list-inside space-y-1'
}

const getListItems = (widget) => {
  if (!widget.items) return []
  const maxItems = widget.max_items || widget.items.length
  return widget.items.slice(0, maxItems)
}

// Link helpers
const getLinkStyle = (widget) => {
  if (widget.style === 'button') {
    const colorClasses = {
      blue: 'bg-blue-600 hover:bg-blue-700 text-white',
      green: 'bg-status-success-600 hover:bg-status-success-700 text-white',
      red: 'bg-status-danger-600 hover:bg-status-danger-700 text-white',
      gray: 'bg-gray-600 hover:bg-gray-700 text-white'
    }
    return `inline-flex items-center px-4 py-2 rounded-md text-sm font-medium ${colorClasses[widget.color] || colorClasses.blue}`
  }
  return 'text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300 text-sm underline'
}

// Image URL helper
const getImageUrl = (src) => {
  if (!src) return ''
  // If it starts with /files/, prepend the agent files API path
  if (src.startsWith('/files/')) {
    return `/api/agents/${props.agentName}/files/preview?path=${encodeURIComponent(src.replace('/files/', ''))}`
  }
  return src
}

// Auto-refresh
const startRefresh = () => {
  if (refreshInterval) clearInterval(refreshInterval)
  const refreshSeconds = dashboardData.value?.config?.refresh || 30
  refreshInterval = setInterval(() => {
    if (props.agentStatus === 'running') {
      loadDashboard()
    }
  }, refreshSeconds * 1000)
}

const stopRefresh = () => {
  if (refreshInterval) {
    clearInterval(refreshInterval)
    refreshInterval = null
  }
}

// Watch for agent changes
watch(() => props.agentName, (newName, oldName) => {
  if (newName && newName !== oldName) {
    dashboardData.value = null
    hasUpdateDashboardPlaybook.value = false
    stopRefresh()
    if (props.agentStatus === 'running') {
      loadDashboard()
      checkUpdateDashboardPlaybook()
      startRefresh()
    } else {
      loading.value = false
    }
  }
})

watch(() => props.agentStatus, (newStatus) => {
  if (newStatus === 'running') {
    loadDashboard()
    checkUpdateDashboardPlaybook()
    startRefresh()
  } else {
    stopRefresh()
  }
})

onMounted(() => {
  if (props.agentStatus === 'running') {
    loadDashboard()
    checkUpdateDashboardPlaybook()
    startRefresh()
  } else {
    loading.value = false
  }
})

onUnmounted(() => {
  stopRefresh()
})
</script>
