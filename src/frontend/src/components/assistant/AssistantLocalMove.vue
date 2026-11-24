<template>
  <!--
    The steps that bring in the agents a person runs in Claude Code on their own
    machine (trinity-enterprise#1353). The assistant asked for this card; every
    word and command in it is written by the page (utils/localMove.js), with
    this instance's address. It changes nothing here, so it has no Confirm.
  -->
  <div class="space-y-2" data-testid="assistant-local-move">
    <ol v-if="steps" class="space-y-2 list-decimal pl-5 text-[12.5px] text-gray-900 dark:text-gray-100">
      <li v-for="s in steps" :key="s.key" :data-testid="`local-move-${s.key}`" class="space-y-1">
        <p class="font-medium">{{ s.title }}</p>
        <div v-for="cmd in s.commands || []" :key="cmd" class="flex items-center gap-2">
          <code class="font-mono min-w-0 flex-1 break-all rounded bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 px-2 py-1">{{ cmd }}</code>
          <BaseButton variant="secondary" size="sm" :aria-label="`Copy ${cmd}`" @click="copy(cmd)">{{ copied === cmd ? 'Copied' : 'Copy' }}</BaseButton>
        </div>
        <p v-if="s.text" class="text-gray-600 dark:text-gray-300">{{ s.text }}</p>
        <a
          v-if="s.link"
          :href="s.link.href"
          class="inline-block font-medium underline text-action-primary-600 dark:text-action-primary-400"
          @click.prevent="open(s.link.href)"
        >{{ s.link.label }}</a>
      </li>
    </ol>
    <!-- Space kept while outside access is read, so the card does not jump. -->
    <div v-else class="h-24" aria-hidden="true"></div>
    <p class="text-[12.5px] text-gray-600 dark:text-gray-300">{{ LOCAL_MOVE_SECRETS }}</p>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import BaseButton from '../base/BaseButton.vue'
import { useAssistantStore } from '../../stores/assistant'
import { LOCAL_MOVE_SECRETS, localMoveSteps } from '../../utils/localMove'

const store = useAssistantStore()
const router = useRouter()
const access = ref(undefined)        // undefined: not read yet; true / false / null (unreadable)
const copied = ref('')

const steps = computed(() => (access.value === undefined ? null : localMoveSteps(window.location.origin, access.value)))

onMounted(async () => {
  access.value = await store.externalMcpAccess()
})

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text)
    copied.value = text
  } catch { /* the command is on screen to select by hand */ }
}

function open(href) {
  router.push(href).catch?.(() => {})
}
</script>
