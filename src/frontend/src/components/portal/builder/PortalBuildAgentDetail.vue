<!--
  One agent, zoomed in: what it does, its skills, what it connects to, the
  variables it needs and when it runs. Everything here is what the guide reports,
  except whether the agent is really running, which comes from the roster.
-->
<template>
  <div class="space-y-6" data-testid="build-agent-detail">
    <section>
      <div class="flex items-start justify-between gap-3">
        <h3 class="text-lg font-semibold text-gray-900 dark:text-gray-100">{{ agent.name }}</h3>
        <BaseBadge :variant="status.variant" :dot="agent.status === 'building'">{{ status.label }}</BaseBadge>
      </div>
      <p v-if="agent.purpose" class="mt-1 text-sm text-gray-600 dark:text-gray-300">{{ agent.purpose }}</p>
      <p v-if="agent.manages.length" class="mt-2 text-xs text-gray-600 dark:text-gray-300" data-testid="build-agent-manages">
        Runs: {{ agent.manages.join(', ') }}
      </p>
      <div v-if="live || agent.status === 'live' || agent.live_name" class="mt-3">
        <div v-if="live" class="flex flex-wrap items-center gap-3">
          <router-link
            :to="`/workspace/a/${encodeURIComponent(live.name)}`"
            class="text-sm font-medium text-action-primary-600 dark:text-action-primary-400 hover:underline"
            data-testid="build-agent-open"
          >Open {{ agent.name }}</router-link>
          <a
            v-if="agent.variables.length"
            :href="credentialsLinkFor(live.name)"
            target="_blank"
            rel="noopener"
            class="text-sm font-medium text-action-primary-600 dark:text-action-primary-400 hover:underline"
            data-testid="build-agent-add-keys"
          >Add its keys</a>
          <BaseBadge v-if="live.availability === 'stopped' || live.availability === 'unavailable'" size="sm" variant="warning">{{ live.availability === 'stopped' ? 'Stopped' : 'Unreachable' }}</BaseBadge>
        </div>
        <p v-else class="text-xs text-status-warning-700 dark:text-status-warning-300" data-testid="build-agent-not-found">
          The guide reports this agent as live, but it isn't among your agents yet.
        </p>
      </div>
    </section>

    <p
      v-if="!agent.skills.length && !agent.connections.length && !agent.variables.length && !agent.schedules.length"
      class="text-xs text-gray-500 dark:text-gray-400"
      data-testid="build-agent-forming"
    >{{ BUILD_GUIDE_NAME }} works out how to build it with you, including its skills and connections.</p>

    <section v-if="agent.skills.length" data-testid="build-agent-skills">
      <h4 :class="OVERLINE">Skills</h4>
      <ul class="mt-2 divide-y divide-gray-200 dark:divide-gray-750">
        <li v-for="skill in agent.skills" :key="skill.name">
          <button
            type="button"
            class="w-full flex items-center gap-3 py-2 text-left"
            :aria-expanded="openSkill === skill.name"
            @click="openSkill = openSkill === skill.name ? '' : skill.name"
          >
            <span class="shrink-0 w-4 h-4 flex items-center justify-center" :class="SKILL_MARK[skill.status].class" :aria-label="SKILL_MARK[skill.status].label" role="img">
              <svg v-if="skill.status === 'done'" class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7" /></svg>
              <span v-else-if="skill.status === 'building'" class="w-2.5 h-2.5 rounded-full bg-current motion-safe:animate-pulse"></span>
              <span v-else class="w-2.5 h-2.5 rounded-full border border-current"></span>
            </span>
            <span class="flex-1 min-w-0 font-mono text-xs text-gray-900 dark:text-gray-100 truncate">/{{ skill.name }}</span>
            <span class="shrink-0 text-xs text-gray-500 dark:text-gray-400">{{ SKILL_MARK[skill.status].label }}</span>
          </button>
          <p v-if="openSkill === skill.name && skill.purpose" class="pb-3 pl-7 text-xs text-gray-600 dark:text-gray-300">{{ skill.purpose }}</p>
        </li>
      </ul>
    </section>

    <section v-if="agent.connections.length" data-testid="build-agent-connections">
      <h4 :class="OVERLINE">Connects to</h4>
      <ul class="mt-2 space-y-2">
        <li v-for="c in agent.connections" :key="c.platform" class="flex items-start justify-between gap-3">
          <div class="min-w-0">
            <p class="text-sm text-gray-900 dark:text-gray-100">{{ c.platform }}</p>
            <p v-if="c.why" class="text-xs text-gray-500 dark:text-gray-400">{{ c.why }}</p>
          </div>
          <BaseBadge size="sm" :variant="c.status === 'connected' ? 'success' : 'warning'">
            {{ c.status === 'connected' ? 'Connected' : 'Needed' }}
          </BaseBadge>
        </li>
      </ul>
    </section>

    <section v-if="agent.variables.length" data-testid="build-agent-variables">
      <h4 :class="OVERLINE">Keys it needs</h4>
      <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">Names only. You add the values yourself; they never go through the chat.</p>
      <ul class="mt-2 flex flex-wrap gap-1.5">
        <li
          v-for="name in agent.variables"
          :key="name"
          class="rounded-md bg-gray-100 dark:bg-gray-750 px-2 py-0.5 font-mono text-xs text-gray-700 dark:text-gray-300"
        >{{ name }}</li>
      </ul>
    </section>

    <section v-if="agent.schedules.length" data-testid="build-agent-schedules">
      <h4 :class="OVERLINE">Runs on its own</h4>
      <ul class="mt-2 space-y-2">
        <li v-for="s in agent.schedules" :key="s.name" class="flex items-center justify-between gap-3">
          <div class="min-w-0">
            <p class="text-sm text-gray-900 dark:text-gray-100">{{ s.name }}</p>
            <p v-if="s.when" class="text-xs text-gray-500 dark:text-gray-400">{{ s.when }}</p>
          </div>
          <BaseBadge size="sm" :variant="s.enabled ? 'success' : 'neutral'">{{ s.enabled ? 'On' : 'Off until you turn it on' }}</BaseBadge>
        </li>
      </ul>
    </section>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import { BUILD_GUIDE_NAME, STATUS_DISPLAY, credentialsLinkFor } from './portalBuild'

const OVERLINE = 'text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400'

const SKILL_MARK = Object.freeze({
  planned: { label: 'Planned', class: 'text-gray-500 dark:text-gray-400' },
  building: { label: 'Building', class: 'text-action-primary-600 dark:text-action-primary-400' },
  done: { label: 'Done', class: 'text-status-success-700 dark:text-status-success-400' },
})

const props = defineProps({
  agent: { type: Object, required: true },
  live: { type: Object, default: null },
  focusSkill: { type: String, default: '' },
})

const status = computed(() => STATUS_DISPLAY[props.agent.status])
const openSkill = ref(props.focusSkill)
watch(() => props.focusSkill, (skill) => { if (skill) openSkill.value = skill })
</script>
