<template>
  <div class="role-matrix">
    <!-- Header -->
    <div class="flex items-center justify-between mb-4">
      <div class="flex items-center gap-3">
        <h3 class="text-lg font-medium text-gray-900 dark:text-white">Role Matrix</h3>
        <span class="text-xs text-gray-500 dark:text-gray-400">
          EMI Pattern: Executor / Monitor / Informed
        </span>
      </div>
      <div class="flex items-center gap-2">
        <!-- Edit mode toggle -->
        <button
          @click="editMode = !editMode"
          :class="[
            'px-3 py-1.5 text-sm font-medium rounded-lg transition-colors',
            editMode
              ? 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400'
              : 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
          ]"
        >
          {{ editMode ? 'Editing' : 'Edit Mode' }}
        </button>
        <!-- Show all agents toggle -->
        <label class="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400">
          <input
            type="checkbox"
            v-model="showAllAgents"
            class="rounded border-gray-300 dark:border-gray-600 text-indigo-600 focus:ring-indigo-500"
          />
          Show all agents
        </label>
      </div>
    </div>

    <!-- Legend -->
    <div class="flex items-center gap-4 mb-4 text-sm text-gray-600 dark:text-gray-400">
      <div class="flex items-center gap-1.5">
        <span class="inline-flex items-center justify-center w-6 h-6 rounded bg-indigo-100 dark:bg-indigo-900/40 text-indigo-700 dark:text-indigo-400 font-semibold text-xs">E</span>
        <span>Executor</span>
      </div>
      <div class="flex items-center gap-1.5">
        <span class="inline-flex items-center justify-center w-6 h-6 rounded bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-400 font-semibold text-xs">M</span>
        <span>Monitor</span>
      </div>
      <div class="flex items-center gap-1.5">
        <span class="inline-flex items-center justify-center w-6 h-6 rounded bg-teal-100 dark:bg-teal-900/40 text-teal-700 dark:text-teal-400 font-semibold text-xs">I</span>
        <span>Informed</span>
      </div>
    </div>

    <!-- Empty state -->
    <div
      v-if="steps.length === 0"
      class="text-center py-12 text-gray-500 dark:text-gray-400 border border-dashed border-gray-200 dark:border-gray-700 rounded-lg"
    >
      <UsersIcon class="w-12 h-12 mx-auto mb-4 text-gray-300 dark:text-gray-600" />
      <p class="font-medium">No steps defined</p>
      <p class="text-sm mt-1">Add steps to your process to assign agent roles</p>
    </div>

    <!-- Matrix table -->
    <div v-else class="overflow-x-auto border border-gray-200 dark:border-gray-700 rounded-lg">
      <table class="w-full text-sm">
        <thead>
          <tr class="bg-gray-50 dark:bg-gray-800/50">
            <th class="px-4 py-3 text-left font-medium text-gray-700 dark:text-gray-300 border-b border-gray-200 dark:border-gray-700 min-w-[150px]">
              Step
            </th>
            <th
              v-for="agent in visibleAgents"
              :key="agent"
              class="px-3 py-3 text-center font-medium text-gray-700 dark:text-gray-300 border-b border-gray-200 dark:border-gray-700 min-w-[100px]"
            >
              <div class="truncate max-w-[120px]" :title="agent">{{ agent }}</div>
            </th>
            <th
              v-if="editMode"
              class="px-3 py-3 text-center font-medium text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700 min-w-[100px]"
            >
              <button
                @click="showAddAgentModal = true"
                class="text-indigo-600 dark:text-indigo-400 hover:text-indigo-700 dark:hover:text-indigo-300 text-xs"
              >
                + Add Agent
              </button>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="step in steps"
            :key="step.id"
            :class="[
              'border-b border-gray-100 dark:border-gray-700/50 last:border-b-0',
              !hasExecutor(step.id) && 'bg-status-danger-50 dark:bg-status-danger-900/10'
            ]"
          >
            <!-- Step name -->
            <td class="px-4 py-3 text-gray-900 dark:text-gray-100">
              <div class="flex items-center gap-2">
                <span class="truncate max-w-[180px]" :title="step.name">{{ step.name }}</span>
                <span
                  v-if="!hasExecutor(step.id)"
                  class="px-1.5 py-0.5 text-xs font-medium bg-status-danger-100 dark:bg-status-danger-900/30 text-status-danger-700 dark:text-status-danger-400 rounded"
                  title="Step must have an executor"
                >
                  No executor
                </span>
                <span class="text-xs text-gray-400 dark:text-gray-500">({{ step.type }})</span>
              </div>
            </td>
            <!-- Role cells for each agent -->
            <td
              v-for="agent in visibleAgents"
              :key="`${step.id}-${agent}`"
              class="px-3 py-3 text-center"
            >
              <button
                v-if="editMode"
                @click="toggleRole(step.id, agent)"
                :class="[
                  'inline-flex items-center justify-center w-8 h-8 rounded transition-colors',
                  getRoleCellClass(step.id, agent)
                ]"
                :title="getRoleTooltip(step.id, agent)"
              >
                {{ getRoleLabel(step.id, agent) }}
              </button>
              <span
                v-else
                :class="[
                  'inline-flex items-center justify-center w-8 h-8 rounded',
                  getRoleCellClass(step.id, agent)
                ]"
              >
                {{ getRoleLabel(step.id, agent) }}
              </span>
            </td>
            <!-- Empty cell for add agent column -->
            <td v-if="editMode" class="px-3 py-3"></td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Validation warnings -->
    <div v-if="validationWarnings.length > 0" class="mt-4 space-y-2">
      <div
        v-for="(warning, index) in validationWarnings"
        :key="index"
        class="flex items-center gap-2 px-3 py-2 text-sm bg-amber-50 dark:bg-amber-900/20 text-amber-700 dark:text-amber-400 rounded-lg"
      >
        <ExclamationTriangleIcon class="w-4 h-4 flex-shrink-0" />
        <span>{{ warning }}</span>
      </div>
    </div>

    <!-- Add Agent Modal -->
    <div
      v-if="showAddAgentModal"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      @click.self="showAddAgentModal = false"
    >
      <div class="bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full max-w-md mx-4 p-6">
        <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-4">Add Agent to Matrix</h3>
        <input
          v-model="newAgentName"
          type="text"
          placeholder="Enter agent name"
          class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
          @keyup.enter="addAgent"
        />
        <div class="flex justify-end gap-3 mt-4">
          <button
            @click="showAddAgentModal = false"
            class="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg"
          >
            Cancel
          </button>
          <button
            @click="addAgent"
            :disabled="!newAgentName.trim()"
            class="px-4 py-2 text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed rounded-lg"
          >
            Add
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch } from 'vue';
import { UsersIcon, ExclamationTriangleIcon } from '@heroicons/vue/24/outline';

const props = defineProps({
  // Steps array: [{ id, name, type, roles: { executor, monitors, informed } }, ...]
  steps: {
    type: Array,
    required: true,
  },
  // Available agents in the system
  availableAgents: {
    type: Array,
    default: () => [],
  },
  // Whether the matrix is read-only
  readOnly: {
    type: Boolean,
    default: false,
  },
});

const emit = defineEmits(['update:roles']);

// Local state
const editMode = ref(!props.readOnly);
const showAllAgents = ref(false);
const showAddAgentModal = ref(false);
const newAgentName = ref('');

// Track roles locally for editing
const localRoles = ref({});

// Initialize local roles from props
// Uses step.agent as default executor if roles.executor not explicitly set
watch(() => props.steps, (newSteps) => {
  const roles = {};
  newSteps.forEach(step => {
    // For agent_task steps, use step.agent as default executor if no explicit roles.executor
    const defaultExecutor = step.type === 'agent_task' ? (step.agent || '') : '';
    roles[step.id] = {
      executor: step.roles?.executor || defaultExecutor,
      monitors: [...(step.roles?.monitors || [])],
      informed: [...(step.roles?.informed || [])],
    };
  });
  localRoles.value = roles;
}, { immediate: true, deep: true });

// Get all unique agents referenced in roles
const referencedAgents = computed(() => {
  const agents = new Set();
  Object.values(localRoles.value).forEach(stepRoles => {
    if (stepRoles.executor) agents.add(stepRoles.executor);
    stepRoles.monitors.forEach(m => agents.add(m));
    stepRoles.informed.forEach(i => agents.add(i));
  });
  return Array.from(agents).sort();
});

// Combine referenced agents with available agents
const allAgents = computed(() => {
  const agents = new Set([...referencedAgents.value, ...props.availableAgents]);
  return Array.from(agents).sort();
});

// Visible agents based on filter
const visibleAgents = computed(() => {
  return showAllAgents.value ? allAgents.value : referencedAgents.value;
});

// Validation warnings
const validationWarnings = computed(() => {
  const warnings = [];
  props.steps.forEach(step => {
    const roles = localRoles.value[step.id];
    if (!roles?.executor) {
      warnings.push(`Step "${step.name}" has no executor assigned`);
    }
    if (roles?.executor && roles?.monitors?.includes(roles.executor)) {
      warnings.push(`Step "${step.name}": executor is also a monitor (redundant)`);
    }
    if (roles?.executor && roles?.informed?.includes(roles.executor)) {
      warnings.push(`Step "${step.name}": executor is also informed (redundant)`);
    }
  });
  return warnings;
});

// Check if step has an executor
function hasExecutor(stepId) {
  return !!localRoles.value[stepId]?.executor;
}

// Get role for a step/agent combination
function getRole(stepId, agent) {
  const roles = localRoles.value[stepId];
  if (!roles) return null;
  if (roles.executor === agent) return 'executor';
  if (roles.monitors?.includes(agent)) return 'monitor';
  if (roles.informed?.includes(agent)) return 'informed';
  return null;
}

// Get display label for role cell
function getRoleLabel(stepId, agent) {
  const role = getRole(stepId, agent);
  switch (role) {
    case 'executor': return 'E';
    case 'monitor': return 'M';
    case 'informed': return 'I';
    default: return '';
  }
}

// Get CSS class for role cell
function getRoleCellClass(stepId, agent) {
  const role = getRole(stepId, agent);
  const base = editMode.value ? 'cursor-pointer hover:ring-2 hover:ring-gray-300 dark:hover:ring-gray-600' : '';
  switch (role) {
    case 'executor':
      return `${base} bg-indigo-100 dark:bg-indigo-900/40 text-indigo-700 dark:text-indigo-400 font-semibold`;
    case 'monitor':
      return `${base} bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-400 font-semibold`;
    case 'informed':
      return `${base} bg-teal-100 dark:bg-teal-900/40 text-teal-700 dark:text-teal-400 font-semibold`;
    default:
      return `${base} bg-gray-50 dark:bg-gray-800 text-gray-400 dark:text-gray-600`;
  }
}

// Get tooltip for role cell
function getRoleTooltip(stepId, agent) {
  const role = getRole(stepId, agent);
  if (editMode.value) {
    switch (role) {
      case 'executor': return 'Click to change to Monitor';
      case 'monitor': return 'Click to change to Informed';
      case 'informed': return 'Click to remove';
      default: return 'Click to set as Executor';
    }
  }
  switch (role) {
    case 'executor': return `${agent} executes this step`;
    case 'monitor': return `${agent} monitors this step`;
    case 'informed': return `${agent} is informed about this step`;
    default: return '';
  }
}

// Toggle role: none -> E -> M -> I -> none
function toggleRole(stepId, agent) {
  if (!editMode.value) return;

  const roles = localRoles.value[stepId];
  if (!roles) return;

  const currentRole = getRole(stepId, agent);

  // Remove from current role
  if (currentRole === 'executor') {
    roles.executor = '';
  } else if (currentRole === 'monitor') {
    roles.monitors = roles.monitors.filter(m => m !== agent);
  } else if (currentRole === 'informed') {
    roles.informed = roles.informed.filter(i => i !== agent);
  }

  // Add to next role in cycle
  switch (currentRole) {
    case null:
      roles.executor = agent;
      break;
    case 'executor':
      roles.monitors.push(agent);
      break;
    case 'monitor':
      roles.informed.push(agent);
      break;
    // 'informed' -> null (already removed above)
  }

  emitUpdate();
}

// Add a new agent to the matrix
function addAgent() {
  const name = newAgentName.value.trim();
  if (!name) return;

  // Agent will show up in the matrix when showAllAgents is true
  // or when assigned a role
  showAddAgentModal.value = false;
  newAgentName.value = '';
  showAllAgents.value = true;
}

// Emit updated roles to parent
function emitUpdate() {
  const rolesMap = {};
  Object.entries(localRoles.value).forEach(([stepId, roles]) => {
    rolesMap[stepId] = {
      executor: roles.executor || undefined,
      monitors: roles.monitors.length > 0 ? roles.monitors : undefined,
      informed: roles.informed.length > 0 ? roles.informed : undefined,
    };
  });
  emit('update:roles', rolesMap);
}

// Watch readOnly prop
watch(() => props.readOnly, (newVal) => {
  if (newVal) editMode.value = false;
});
</script>

<style scoped>
.role-matrix {
  /* Component-specific styles if needed */
}
</style>
