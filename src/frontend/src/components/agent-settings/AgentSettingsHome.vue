<template>
  <!--
    The agent's one Settings home (trinity-enterprise#887, trinity#966): a left
    rail of panes, each answering one question, and the pane itself on the
    right under the page's one restart banner.

    The pane is in the URL — `?tab=settings&section=<pane>` — on top of the
    tab-in-URL work of #2900, so Back steps between panes and a reload stays
    put. Old `?tab=` values (info, credentials, folders, access, guardrails)
    land on their pane through `resolveSettingsSection`.

    The panes themselves are rendered by the caller through the scoped
    default slot, because the state they edit (the agent, its tags, its
    funding, the restart flow) lives in AgentDetail.

    Below `lg` the rail becomes a select above the pane: a fixed-height
    control rather than a column that would squeeze the pane to nothing.
  -->
  <div class="lg:grid lg:grid-cols-[15rem_minmax(0,1fr)] lg:gap-8" data-testid="agent-settings-home">
    <!-- Rail (lg and up) -->
    <nav class="hidden lg:block" aria-label="Settings sections">
      <BaseInput
        v-model="query"
        type="search"
        placeholder="Find a setting"
        aria-label="Find a setting"
        class="mb-3"
        data-testid="settings-find"
      />
      <ul class="space-y-1" data-testid="settings-rail">
        <li v-for="s in railSections" :key="s.id">
          <button
            type="button"
            class="w-full text-left rounded-lg px-3 py-2 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
            :class="s.id === section
              ? 'bg-action-primary-50 dark:bg-action-primary-500/16'
              : 'hover:bg-gray-100 dark:hover:bg-gray-750'"
            :aria-current="s.id === section ? 'page' : undefined"
            :data-section="s.id"
            @click="select(s.id)"
          >
            <span
              class="block text-sm font-medium"
              :class="s.id === section ? 'text-action-primary-700 dark:text-action-primary-300' : 'text-gray-900 dark:text-gray-100'"
            >{{ s.label }}</span>
            <span
              class="block text-[12.5px]"
              :class="s.id === section ? 'text-action-primary-700 dark:text-action-primary-300' : 'text-gray-600 dark:text-gray-400'"
            >{{ s.question }}</span>
          </button>
        </li>
      </ul>
      <p v-if="railSections.length === 0" class="px-3 py-2 text-[12.5px] text-gray-600 dark:text-gray-400" data-testid="settings-find-empty">
        No setting matches “{{ query.trim() }}”. Try a word like timeout, token or folders.
      </p>
      <p class="mt-6 px-3 text-[12.5px] text-gray-600 dark:text-gray-400">
        Only the sections you can use appear here. A change that needs a restart is listed once, at the top.
      </p>
    </nav>

    <!-- Pane picker (below lg) -->
    <div class="lg:hidden mb-4">
      <BaseSelect :model-value="section" label="Section" data-testid="settings-section-select" @update:model-value="select">
        <option v-for="s in sections" :key="s.id" :value="s.id">{{ s.label }} — {{ s.question }}</option>
      </BaseSelect>
    </div>

    <div class="min-w-0 space-y-6" data-settings-pane>
      <SettingsRestartBanner
        :agent-name="agentName"
        :agent-status="agentStatus"
        :restarting="restarting"
        @restart="$emit('restart')"
      />
      <header>
        <h2 class="text-2xl font-bold text-gray-900 dark:text-gray-100">
          {{ current?.label }}
          <span class="ml-2 text-sm font-normal text-gray-600 dark:text-gray-400">{{ current?.question }}.</span>
        </h2>
      </header>
      <slot :section="section" />
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { resolveSettingsSection, filterSections } from '../../utils/agentSettings'
import BaseInput from '../base/BaseInput.vue'
import BaseSelect from '../base/BaseSelect.vue'
import SettingsRestartBanner from './SettingsRestartBanner.vue'

const props = defineProps({
  agentName: { type: String, required: true },
  agentStatus: { type: String, default: '' },
  // The panes this viewer may open, from `settingsSections()`.
  sections: { type: Array, required: true },
  restarting: { type: Boolean, default: false },
})
defineEmits(['restart'])

const route = useRoute()
const router = useRouter()

const section = computed(() => resolveSettingsSection(route.query, props.sections))
const current = computed(() => props.sections.find((s) => s.id === section.value))

const query = ref('')
const railSections = computed(() => filterSections(props.sections, query.value))

function select(id) {
  if (id === section.value && route.query.section === id) return
  // A push, so Back returns to the previous pane (#2900's history contract).
  router.push({ query: { ...route.query, tab: 'settings', section: id } })
}
</script>
