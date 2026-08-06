<!--
  ImportIntentPicker.vue (trinity-enterprise#15)

  3-way import-intent selector for the "GitHub Repository" create path.
  Rendered by CreateAgentModal below the repo URL input; the selected value is
  sent as `import_intent` on POST /api/agents ('clone' | 'copy' | 'fork').

  Lives in its own file so the ratcheted CreateAgentModal gains no new color
  markup (design-system contract: new markup goes in new child components —
  zero raw non-gray classes, zero hardcoded colors; gray chrome per the
  contract's surface/ink ladder).

  Keyboard: a native radio group — arrow keys move the selection, the whole
  card is the <label> hit target, focus ring on the radio itself.
-->
<template>
  <fieldset class="mt-3">
    <legend class="block text-sm font-medium text-gray-700 dark:text-gray-300">
      How should this repository be imported?
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
          name="import-intent"
          :value="opt.value"
          :checked="modelValue === opt.value"
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
  // 'clone' (default) | 'copy' | 'fork'
  modelValue: { type: String, default: 'clone' },
})
defineEmits(['update:modelValue'])

const OPTIONS = [
  {
    value: 'clone',
    title: 'Clone — my repo',
    desc: 'Clones with two-way git sync. Best when the repo is yours.',
  },
  {
    value: 'copy',
    title: 'Copy — snapshot',
    desc: 'One-time copy, no link to the source repo. You can connect it to your own repo later from the Git tab.',
  },
  {
    value: 'fork',
    title: 'Fork — make it mine',
    desc: 'Copies the repo into your GitHub account first; the agent syncs to YOUR copy.',
  },
]
</script>
