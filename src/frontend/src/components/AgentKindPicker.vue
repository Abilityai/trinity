<!--
  AgentKindPicker.vue (trinity-enterprise#704)

  The one question the create flow asks about a GitHub-backed agent: is the
  repository THIS AGENT (its memory, skills, state), or a codebase it deploys?
  Sent as `kind` on POST /api/agents ('agent' | 'deployment'); the platform
  decides the git binding from it (ent#705).

  Its own file so the ratcheted CreateAgentModal gains no colour markup (the
  ImportIntentPicker precedent). Keyboard: a native radio group — arrow keys
  move the selection, the whole card is the <label> hit target.
-->
<template>
  <fieldset class="mt-3" data-testid="agent-kind-picker">
    <legend class="block text-sm font-medium text-gray-700 dark:text-gray-300">
      What is this repository?
    </legend>
    <div class="mt-1 space-y-2">
      <label
        v-for="opt in OPTIONS"
        :key="opt.value"
        :class="[
          'flex items-start p-3 border rounded-lg cursor-pointer transition-all',
          modelValue === opt.value
            ? 'border-action-primary-500 bg-action-primary-50 dark:bg-action-primary-900/30 ring-1 ring-action-primary-500'
            : 'border-gray-300 dark:border-gray-600 hover:border-gray-400 dark:hover:border-gray-500'
        ]"
      >
        <input
          type="radio"
          name="agent-kind"
          :value="opt.value"
          :checked="modelValue === opt.value"
          :data-testid="`agent-kind-${opt.value}`"
          @change="$emit('update:modelValue', opt.value)"
          class="mt-0.5 shrink-0 text-action-primary-600 focus:ring-action-primary-500"
        />
        <span class="ml-2.5 min-w-0">
          <span class="block text-sm font-medium text-gray-900 dark:text-white">{{ opt.title }}</span>
          <span class="mt-0.5 block text-xs text-gray-500 dark:text-gray-400">{{ opt.desc }}</span>
        </span>
      </label>
    </div>
  </fieldset>
</template>

<script setup>
defineProps({
  // 'agent' (default) | 'deployment'
  modelValue: { type: String, default: 'agent' },
})
defineEmits(['update:modelValue'])

const OPTIONS = [
  {
    value: 'agent',
    title: 'An agent',
    desc: 'The repository is this agent — its memory, skills and state. It gets its own branch and saves its work there every 15 minutes. If your GitHub token cannot push to this repository it is created pull-only; to keep an agent built from someone else\'s template, choose Fork instead.',
  },
  {
    value: 'deployment',
    title: 'A deployment of a codebase',
    desc: 'The repository is a product the agent runs. It only pulls updates; nothing is pushed back.',
  },
]
</script>
