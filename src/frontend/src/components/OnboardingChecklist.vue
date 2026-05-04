<template>
  <!-- Notification Toast using Teleport -->
  <Teleport to="body">
    <div
      v-if="showHint"
      class="fixed bottom-24 right-4 z-[9998] w-80 bg-indigo-600 text-white rounded-lg shadow-2xl p-4 flex items-start gap-3 transition-all duration-300"
      style="animation: slideIn 0.3s ease-out;"
    >
      <div class="flex-shrink-0">
        <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
      </div>
      <div class="flex-1">
        <p class="text-sm font-medium">You're already here!</p>
        <p class="text-xs mt-1 opacity-90">{{ hintMessage }}</p>
      </div>
      <button @click="showHint = false" class="flex-shrink-0 opacity-70 hover:opacity-100">
        <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </div>
  </Teleport>

  <div
    v-if="showChecklist"
    class="fixed bottom-4 right-4 z-[9999] w-80 bg-white dark:bg-gray-800 rounded-xl shadow-2xl border border-gray-200 dark:border-gray-700 overflow-hidden transition-all duration-300"
    :class="[
      { 'h-auto': !isMinimized, 'h-14': isMinimized },
      isCelebrating ? 'ring-2 ring-status-success-400 ring-offset-2 animate-celebrate' : ''
    ]"
  >
    <!-- Header -->
    <div
      class="flex items-center justify-between px-4 py-3 bg-gradient-to-r from-indigo-500 to-purple-500 text-white cursor-pointer"
      @click="toggleMinimized"
    >
      <div class="flex items-center gap-2">
        <SparklesIcon class="h-5 w-5" />
        <span class="font-semibold text-sm">Getting Started</span>
      </div>
      <div class="flex items-center gap-2">
        <span class="text-xs bg-white/20 rounded-full px-2 py-0.5">
          {{ progress.completed }}/{{ progress.total }}
        </span>
        <ChevronDownIcon
          class="h-4 w-4 transition-transform duration-200"
          :class="{ 'rotate-180': isMinimized }"
        />
      </div>
    </div>

    <!-- Celebration Banner -->
    <div
      v-if="isCelebrating && !isMinimized"
      class="bg-gradient-to-r from-green-500 to-emerald-500 text-white px-4 py-2 flex items-center gap-2"
    >
      <span class="text-lg">🎉</span>
      <span class="text-sm font-medium">Great job! Here's your next step:</span>
    </div>

    <!-- Content -->
    <div v-if="!isMinimized" class="p-4">
      <!-- Progress bar -->
      <div class="mb-4">
        <div class="h-2 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
          <div
            class="h-full bg-gradient-to-r from-indigo-500 to-purple-500 transition-all duration-500"
            :style="{ width: `${progressPercent}%` }"
          />
        </div>
        <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">
          {{ progress.requiredCompleted >= progress.required ? 'All required steps complete!' : `${progress.required - progress.requiredCompleted} required steps remaining` }}
        </p>
      </div>

      <!-- Checklist items -->
      <ul class="space-y-2">
        <!-- Required items -->
        <li
          v-for="(item, index) in requiredItems"
          :key="item.id"
          class="flex items-start gap-3 p-2 rounded-lg transition-colors"
          :class="[
            item.completed
              ? 'bg-status-success-50 dark:bg-status-success-900/20'
              : isCurrentStep(index)
                ? 'bg-indigo-50 dark:bg-indigo-900/20 ring-1 ring-indigo-200 dark:ring-indigo-700'
                : 'opacity-50',
            !item.completed && item.link && isCurrentStep(index) ? 'cursor-pointer hover:bg-indigo-100 dark:hover:bg-indigo-900/30' : ''
          ]"
          @click="handleItemClick(item, index)"
        >
          <div
            class="flex-shrink-0 w-5 h-5 rounded-full flex items-center justify-center mt-0.5"
            :class="item.completed
              ? 'bg-status-success-500 text-white'
              : isCurrentStep(index)
                ? 'border-2 border-indigo-500 dark:border-indigo-400'
                : 'border-2 border-gray-300 dark:border-gray-600'"
          >
            <CheckIcon v-if="item.completed" class="h-3 w-3" />
          </div>
          <div class="flex-1 min-w-0">
            <p
              class="text-sm font-medium"
              :class="item.completed
                ? 'text-status-success-700 dark:text-status-success-400 line-through'
                : isCurrentStep(index)
                  ? 'text-gray-900 dark:text-white'
                  : 'text-gray-500 dark:text-gray-400'"
            >
              {{ item.label }}
            </p>
            <p
              class="text-xs flex items-center gap-1"
              :class="isCurrentStep(index) || item.completed
                ? 'text-gray-500 dark:text-gray-400'
                : 'text-gray-400 dark:text-gray-500'"
            >
              <component
                v-if="item.icon"
                :is="item.icon"
                class="h-3 w-3 flex-shrink-0"
              />
              {{ item.description }}
            </p>
          </div>
          <span
            v-if="!item.completed && item.link && isCurrentStep(index)"
            class="flex-shrink-0 text-xs font-medium"
            :class="isOnTargetPage(item)
              ? 'text-amber-600 dark:text-amber-400'
              : 'text-indigo-600 dark:text-indigo-400'"
          >
            {{ isOnTargetPage(item) ? 'See above ↑' : 'Start →' }}
          </span>
        </li>

        <!-- Divider -->
        <li class="border-t border-gray-200 dark:border-gray-700 my-2"></li>

        <!-- Optional items label -->
        <li
          class="text-xs px-2"
          :class="allRequiredComplete ? 'text-gray-500 dark:text-gray-400' : 'text-gray-400 dark:text-gray-500 opacity-50'"
        >
          Optional
        </li>

        <!-- Optional items -->
        <li
          v-for="item in optionalItems"
          :key="item.id"
          class="flex items-start gap-3 p-2 rounded-lg transition-colors"
          :class="[
            item.completed
              ? 'bg-status-success-50 dark:bg-status-success-900/20'
              : allRequiredComplete
                ? 'hover:bg-gray-50 dark:hover:bg-gray-700/50'
                : 'opacity-40'
          ]"
        >
          <div
            class="flex-shrink-0 w-5 h-5 rounded-full flex items-center justify-center mt-0.5"
            :class="item.completed
              ? 'bg-status-success-500 text-white'
              : 'border-2 border-gray-300 dark:border-gray-600'"
          >
            <CheckIcon v-if="item.completed" class="h-3 w-3" />
          </div>
          <div class="flex-1 min-w-0">
            <p
              class="text-sm font-medium"
              :class="item.completed
                ? 'text-status-success-700 dark:text-status-success-400 line-through'
                : allRequiredComplete
                  ? 'text-gray-900 dark:text-white'
                  : 'text-gray-500 dark:text-gray-400'"
            >
              {{ item.label }}
            </p>
            <p
              class="text-xs"
              :class="item.completed || allRequiredComplete
                ? 'text-gray-500 dark:text-gray-400'
                : 'text-gray-400 dark:text-gray-500'"
            >
              {{ item.description }}
            </p>
          </div>
        </li>
      </ul>

      <!-- Dismiss button -->
      <div class="mt-4 pt-3 border-t border-gray-200 dark:border-gray-700 flex justify-between items-center">
        <button
          @click="dismiss"
          class="text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300"
        >
          Dismiss checklist
        </button>
        <button
          @click.stop.prevent="navigateTo('/processes/docs')"
          type="button"
          class="text-xs text-indigo-600 dark:text-indigo-400 hover:underline cursor-pointer pointer-events-auto"
        >
          View all docs →
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useOnboarding } from '../composables/useOnboarding'
import {
  CheckIcon,
  ChevronDownIcon,
  SparklesIcon
} from '@heroicons/vue/24/solid'
import { PlayIcon } from '@heroicons/vue/24/outline'

const router = useRouter()
const showHint = ref(false)
const hintMessage = ref('')
let hintTimeout = null

const {
  state,
  celebrateStep,
  checklistProgress,
  isChecklistComplete,
  dismissOnboarding,
  toggleChecklistMinimized
} = useOnboarding()

// Track if we're currently celebrating
const isCelebrating = computed(() => !!celebrateStep.value)

onUnmounted(() => {
  if (hintTimeout) {
    clearTimeout(hintTimeout)
  }
})

// Show checklist only if not dismissed
const showChecklist = computed(() => {
  return state.value && !state.value.dismissed
})

const isMinimized = computed(() => {
  return state.value?.checklistMinimized || false
})

const progress = computed(() => checklistProgress.value)

const progressPercent = computed(() => {
  return Math.round((progress.value.completed / progress.value.total) * 100)
})

// Check if all required steps are complete
const allRequiredComplete = computed(() => {
  return progress.value.requiredCompleted >= progress.value.required
})

// Checklist item definitions
const requiredItems = computed(() => [
  {
    id: 'createProcess',
    label: 'Create your first process',
    description: 'Use the wizard to define your workflow',
    completed: state.value?.checklist.createProcess || false,
    link: '/processes/wizard',
    icon: null
  },
  {
    id: 'runExecution',
    label: 'Run a process execution',
    description: 'Execute your process and see it in action',
    completed: state.value?.checklist.runExecution || false,
    link: '/processes',
    icon: PlayIcon
  },
  {
    id: 'monitorExecution',
    label: 'Monitor an execution',
    description: 'View progress and step outputs',
    completed: state.value?.checklist.monitorExecution || false,
    link: '/executions',
    icon: null
  }
])

const optionalItems = computed(() => [
  {
    id: 'setupSchedule',
    label: 'Set up a schedule',
    description: 'Automate recurring process runs',
    completed: state.value?.checklist.setupSchedule || false
  },
  {
    id: 'configureApproval',
    label: 'Configure human approval',
    description: 'Add approval gates to your workflow',
    completed: state.value?.checklist.configureApproval || false
  }
])

const toggleMinimized = () => {
  toggleChecklistMinimized()
}

const dismiss = () => {
  dismissOnboarding()
}

// Find the index of the first incomplete required item (the "current" step)
const currentStepIndex = computed(() => {
  const items = requiredItems.value
  for (let i = 0; i < items.length; i++) {
    if (!items[i].completed) return i
  }
  return -1 // All completed
})

const isCurrentStep = (index) => {
  return index === currentStepIndex.value
}

const isOnTargetPage = (item) => {
  if (!item.link) return false

  const currentPath = router.currentRoute.value.path
  const onPage = currentPath === item.link || currentPath.startsWith(item.link + '/')

  if (!onPage) return false

  // Only show "See above" if prerequisites are met
  // Otherwise the hint doesn't make sense
  if (item.id === 'runExecution') {
    // Can only run if a process exists (createProcess completed)
    return state.value?.checklist.createProcess === true
  }
  if (item.id === 'monitorExecution') {
    // Can only monitor if there's an execution (runExecution completed)
    return state.value?.checklist.runExecution === true
  }

  return true
}

const navigateTo = (path) => {
  router.push(path)
}

const showHintToast = (message) => {
  hintMessage.value = message
  showHint.value = true

  // Clear any existing timeout
  if (hintTimeout) {
    clearTimeout(hintTimeout)
  }

  // Auto-hide after 5 seconds
  hintTimeout = setTimeout(() => {
    showHint.value = false
  }, 5000)
}

const handleItemClick = (item, index) => {
  if (item.completed) {
    return // Already completed, no action needed
  }

  if (!item.link) {
    return // No link defined
  }

  // Only allow clicking on the current step
  if (!isCurrentStep(index)) {
    return // Not the current step, ignore click
  }

  // If already on the target page, provide feedback
  if (isOnTargetPage(item)) {
    // Show helpful hints based on the item
    if (item.id === 'runExecution') {
      showHintToast('Click the Play button (▶️) on a published process to execute it!')
    } else if (item.id === 'createProcess') {
      showHintToast('Follow the wizard steps above to create your first process!')
    } else if (item.id === 'monitorExecution') {
      showHintToast('View your running executions and monitor their progress here!')
    }
    return
  }

  navigateTo(item.link)
}
</script>

<style scoped>
@keyframes slideIn {
  from {
    transform: translateY(20px);
    opacity: 0;
  }
  to {
    transform: translateY(0);
    opacity: 1;
  }
}

@keyframes celebrate {
  0%, 100% {
    transform: scale(1);
    box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
  }
  50% {
    transform: scale(1.02);
    box-shadow: 0 25px 50px -12px rgba(74, 222, 128, 0.4);
  }
}

.animate-celebrate {
  animation: celebrate 0.6s ease-in-out 2;
}
</style>
