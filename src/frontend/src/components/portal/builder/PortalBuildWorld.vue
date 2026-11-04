<!--
  The right-hand column of the Build page: the person's world, filling in as they
  talk. Company at the top, then each department with the agents in it. Clicking
  an agent zooms into it, and the view follows along when the guide starts working
  on one (`focus.agent`). Findings holds what the audit learned, and Context holds
  the company's context.md.
-->
<template>
  <aside class="flex flex-col min-h-0 h-full border-l border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-950" aria-label="Your world" data-testid="build-world">
    <header class="shrink-0 flex items-center gap-2 px-4 h-14 border-b border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900">
      <nav class="flex-1 min-w-0 flex items-center gap-1.5 text-sm" aria-label="Where you are">
        <button
          v-if="selected"
          type="button"
          class="truncate text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100"
          data-testid="build-world-back"
          @click="selectedId = ''"
        >{{ worldName }}</button>
        <span v-else class="truncate font-semibold text-gray-900 dark:text-gray-100" data-testid="build-world-name">{{ worldName }}</span>
        <template v-if="selected">
          <svg class="shrink-0 w-3 h-3 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" /></svg>
          <span class="truncate font-semibold text-gray-900 dark:text-gray-100">{{ selected.name }}</span>
        </template>
      </nav>
      <BaseBadge v-if="world.profileIncomplete" variant="danger" size="sm" data-testid="build-profile-incomplete">Incomplete profile</BaseBadge>
      <slot name="header-end" />
    </header>

    <ol class="shrink-0 flex items-center gap-1 px-4 h-12 border-b border-gray-200 dark:border-gray-800" aria-label="Progress" data-testid="build-stepper">
      <li
        v-for="(step, i) in BUILD_STAGES"
        :key="step.id"
        class="flex items-center gap-1.5 min-w-0"
        :class="i < BUILD_STAGES.length - 1 ? 'flex-1' : ''"
        :aria-current="stepState(step.id, world) === 'current' ? 'step' : undefined"
        :data-step-state="stepState(step.id, world)"
      >
        <component
          :is="stepState(step.id, world) === 'incomplete' ? 'button' : 'span'"
          class="flex items-center gap-1.5 min-w-0"
          :type="stepState(step.id, world) === 'incomplete' ? 'button' : undefined"
          :title="stepState(step.id, world) === 'incomplete' ? 'Finish your profile' : undefined"
          :data-testid="stepState(step.id, world) === 'incomplete' ? 'build-step-finish-profile' : undefined"
          @click="stepState(step.id, world) === 'incomplete' && $emit('finish-profile')"
        >
          <span
            class="shrink-0 w-5 h-5 rounded-full flex items-center justify-center text-[11px] font-semibold tabular-nums"
            :class="stepState(step.id, world) === 'done'
              ? 'bg-status-success-600 text-white'
              : stepState(step.id, world) === 'incomplete'
                ? 'bg-status-danger-600 text-white'
                : stepState(step.id, world) === 'current'
                  ? 'bg-action-primary-600 dark:bg-action-primary-500 text-white'
                  : 'border border-gray-300 dark:border-gray-700 text-gray-500 dark:text-gray-400'"
          >
            <svg v-if="stepState(step.id, world) === 'done'" class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" /></svg>
            <template v-else-if="stepState(step.id, world) === 'incomplete'">!</template>
            <template v-else>{{ i + 1 }}</template>
          </span>
          <span
            class="hidden md:inline text-xs truncate"
            :class="stepState(step.id, world) === 'incomplete'
              ? 'font-medium text-status-danger-700 dark:text-status-danger-400 underline underline-offset-2'
              : stepState(step.id, world) === 'current'
                ? 'font-medium text-gray-900 dark:text-gray-100'
                : 'text-gray-500 dark:text-gray-400'"
          >{{ step.label }}</span>
        </component>
        <span v-if="i < BUILD_STAGES.length - 1" class="flex-1 h-px min-w-2 bg-gray-200 dark:bg-gray-750"></span>
      </li>
    </ol>

    <div class="shrink-0 px-4 border-b border-gray-200 dark:border-gray-800">
      <OverflowTabs v-model="tab" :tabs="tabs" />
    </div>

    <div class="flex-1 min-h-0 overflow-y-auto p-4">
      <InlineError v-if="readError" class="mb-4" :message="readError" retryable @retry="$emit('retry')" @dismiss="$emit('dismiss-error')" />

      <template v-if="tab === 'world'">
        <Transition
          mode="out-in"
          enter-active-class="transition duration-200 ease-out motion-reduce:transition-none"
          enter-from-class="opacity-0 scale-[.98]"
          leave-active-class="transition duration-100 ease-in motion-reduce:transition-none"
          leave-to-class="opacity-0"
        >
          <PortalBuildAgentDetail
            v-if="selected"
            :key="selected.id"
            :agent="selected"
            :live="liveAgentFor(selected, roster)"
            :focus-skill="world.focus.agent === selected.id ? world.focus.skill : ''"
          />

          <div v-else class="space-y-4" data-testid="build-world-map">
            <BaseCard>
              <p class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Company</p>
              <p class="mt-1 text-sm font-medium text-gray-900 dark:text-gray-100" data-testid="build-company">
                {{ world.company.name || 'Not told yet' }}
              </p>
              <p v-if="companyMeta" class="text-xs text-gray-500 dark:text-gray-400">{{ companyMeta }}</p>
              <p v-if="world.person.role" class="mt-2 text-xs text-gray-600 dark:text-gray-300" data-testid="build-role">You: {{ world.person.role }}</p>
              <p v-if="world.level" class="mt-1 text-xs text-gray-600 dark:text-gray-300" data-testid="build-level">{{ LEVEL_LABELS[world.level] }}</p>
              <button
                v-if="research.status === 'done'"
                type="button"
                class="mt-2 block text-left text-xs font-medium text-action-primary-600 dark:text-action-primary-500 underline-offset-2 hover:underline"
                data-testid="build-research-link"
                @click="tab = 'findings'"
              >Company research is ready. Read it &rarr;</button>
              <p v-else-if="research.status === 'running'" class="mt-2 text-xs text-gray-500 dark:text-gray-400" data-testid="build-research-running">
                Researching your company. It will appear under Findings.
              </p>
            </BaseCard>

            <PortalBuildLevels
              v-if="levels.length"
              :levels="levels"
              :celebrate="levelCelebrate"
              :guide-label="guideLabel"
              @ask="(prompt) => $emit('ask', prompt)"
              @dismiss="$emit('dismiss-level')"
            />

            <div
              v-if="world.auditNudge"
              class="rounded-lg border border-status-warning-200 dark:border-status-warning-500/30 bg-status-warning-50 dark:bg-status-warning-500/10 p-4"
              data-testid="build-audit-nudge"
            >
              <p class="text-sm font-medium text-status-warning-800 dark:text-status-warning-300">You skipped the AI audit</p>
              <p class="mt-1 text-xs text-status-warning-800 dark:text-status-warning-300">
                Without it the guide can build what you ask for, but it can't tell you where agents would help most or where your data lives.
              </p>
              <BaseButton size="sm" variant="secondary" class="mt-3" @click="$emit('ask', AUDIT_PROMPT)">Do the audit</BaseButton>
            </div>

            <section
              v-for="zone in zones"
              :key="zone.name"
              class="rounded-lg border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-900 p-3"
              data-testid="build-zone"
            >
              <div class="flex items-center justify-between gap-2 px-1">
                <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{{ zone.name }}</h3>
                <span v-if="zone.opportunityCount" class="shrink-0 text-xs tabular-nums text-gray-500 dark:text-gray-400">
                  {{ zone.opportunityCount }} {{ zone.opportunityCount === 1 ? 'idea' : 'ideas' }}
                </span>
              </div>
              <TransitionGroup
                v-if="zone.agents.length"
                tag="div"
                class="mt-2 grid gap-2 grid-cols-[repeat(auto-fill,minmax(13rem,1fr))]"
                enter-active-class="transition duration-300 ease-out motion-reduce:transition-none"
                enter-from-class="opacity-0 translate-y-1"
              >
                <button
                  v-for="agent in zone.agents"
                  :key="agent.id"
                  type="button"
                  class="text-left rounded-lg border border-gray-200 dark:border-gray-750 bg-gray-50 dark:bg-gray-800 p-3 hover:border-gray-300 dark:hover:border-gray-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
                  :class="agent.status === 'building' ? 'ring-1 ring-action-primary-500/40' : ''"
                  :data-testid="`build-agent-${agent.id}`"
                  @click="selectedId = agent.id"
                >
                  <span class="flex items-start justify-between gap-2">
                    <span class="min-w-0 flex items-center gap-1.5 text-sm font-medium text-gray-900 dark:text-gray-100">
                      <span v-if="agent.status === 'building'" class="shrink-0 w-2 h-2 rounded-full bg-action-primary-500 motion-safe:animate-pulse" aria-hidden="true"></span>
                      <span class="truncate">{{ agent.name }}</span>
                    </span>
                    <BaseBadge size="sm" :variant="STATUS_DISPLAY[agent.status].variant">{{ STATUS_DISPLAY[agent.status].label }}</BaseBadge>
                  </span>
                  <span v-if="agent.purpose" class="mt-1 block text-xs text-gray-600 dark:text-gray-300 line-clamp-2">{{ agent.purpose }}</span>
                  <span class="mt-2 block text-xs tabular-nums text-gray-500 dark:text-gray-400">{{ partsLine(agent) }}</span>
                </button>
              </TransitionGroup>
              <p v-else class="mt-1 px-1 text-xs text-gray-500 dark:text-gray-400">No agent here yet.</p>
            </section>

            <section
              v-if="instanceAgents.length"
              class="rounded-lg border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-900 p-3"
              data-testid="build-instance-agents"
            >
              <h3 class="px-1 text-sm font-medium text-gray-900 dark:text-gray-100">Already on your instance</h3>
              <ul class="mt-2 grid gap-2 grid-cols-[repeat(auto-fill,minmax(13rem,1fr))]">
                <li v-for="agent in instanceAgents" :key="agent.name">
                  <router-link
                    :to="`/workspace/a/${encodeURIComponent(agent.name)}`"
                    class="block rounded-lg border border-gray-200 dark:border-gray-750 bg-gray-50 dark:bg-gray-800 p-3 text-sm font-medium text-gray-900 dark:text-gray-100 truncate hover:border-gray-300 dark:hover:border-gray-700"
                  >{{ agent.display_label || agent.name }}</router-link>
                </li>
              </ul>
            </section>

            <div
              v-if="!zones.length && !instanceAgents.length"
              class="rounded-lg border border-dashed border-gray-300 dark:border-gray-700 p-6 text-center"
              data-testid="build-world-empty"
            >
              <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Your agents will appear here</p>
              <p class="mx-auto mt-1 max-w-[42ch] text-xs text-gray-500 dark:text-gray-400">
                As you talk, departments and agents show up here, and you can open any of them to see how it's put together.
              </p>
            </div>

            <div v-if="world.agents.length" class="rounded-lg border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-900 p-4" data-testid="build-orchestrator">
              <div class="flex items-center justify-between gap-2">
                <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Orchestrator</p>
                <BaseBadge v-if="!orchestrationAvailable" size="sm" variant="locked">Not on this plan</BaseBadge>
              </div>
              <p class="mt-1 text-xs text-gray-600 dark:text-gray-300">An agent that runs your other agents: it hands out work, checks on it, and tells you only what needs you.</p>
              <BaseButton v-if="orchestrationAvailable" size="sm" variant="secondary" class="mt-3" @click="$emit('ask', ORCHESTRATOR_PROMPT)">Design one</BaseButton>
            </div>
          </div>
        </Transition>
      </template>

      <div v-else-if="tab === 'findings'" class="space-y-6" data-testid="build-findings">
        <section v-if="research.status !== 'none'" data-testid="build-research">
          <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">Company research</h3>
          <p v-if="research.status === 'running'" class="mt-1 text-sm text-gray-600 dark:text-gray-300" role="status">Your guide is researching your company from public sources. It keeps going while you talk.</p>
          <div v-else-if="research.status === 'failed'" class="mt-1">
            <p class="text-sm text-status-danger-700 dark:text-status-danger-400" data-testid="build-research-failed">{{ research.error }}</p>
            <BaseButton class="mt-2" size="sm" variant="secondary" data-testid="build-research-retry" @click="$emit('retry-research')">Try again</BaseButton>
          </div>
          <div v-else class="mt-2" data-testid="build-research-report"><PortalMarkdown :content="research.report" /></div>
        </section>

        <p v-if="world.audit.summary" class="text-sm text-gray-700 dark:text-gray-300">{{ world.audit.summary }}</p>

        <section v-for="dept in departmentsWithIdeas" :key="dept.name">
          <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ dept.name }}</h3>
          <ul class="mt-2 space-y-2">
            <li v-for="idea in dept.opportunities" :key="idea.title" class="rounded-lg border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-900 p-3">
              <div class="flex items-start justify-between gap-2">
                <p class="text-sm text-gray-900 dark:text-gray-100">{{ idea.title }}</p>
                <BaseBadge v-if="idea.recommended" size="sm" variant="info">Start here</BaseBadge>
              </div>
              <p v-if="idea.why" class="mt-1 text-xs text-gray-600 dark:text-gray-300">{{ idea.why }}</p>
              <div class="mt-2 flex items-center justify-between gap-2">
                <p class="text-xs text-gray-500 dark:text-gray-400">{{ capitalize(idea.effort) }} effort, {{ idea.value }} value</p>
                <BaseButton size="sm" :variant="idea.recommended ? 'primary' : 'secondary'" data-testid="build-this" @click="$emit('ask', `Let's build this: ${idea.title}.`)">Build this</BaseButton>
              </div>
            </li>
          </ul>
        </section>

        <section v-if="world.dataSources.length" data-testid="build-data-sources">
          <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">Where your data lives</h3>
          <div class="mt-2 overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-750">
            <table class="w-full text-xs">
              <thead class="bg-gray-100 dark:bg-gray-750 text-left text-gray-600 dark:text-gray-300">
                <tr>
                  <th scope="col" class="px-3 py-2 font-medium">Source</th>
                  <th scope="col" class="px-3 py-2 font-medium">Holds</th>
                  <th scope="col" class="px-3 py-2 font-medium">Format</th>
                  <th scope="col" class="px-3 py-2 font-medium">Access</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-gray-200 dark:divide-gray-750 bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-300">
                <tr v-for="d in world.dataSources" :key="d.name">
                  <td class="px-3 py-2 font-medium text-gray-900 dark:text-gray-100">{{ d.name }}</td>
                  <td class="px-3 py-2">{{ d.holds }}</td>
                  <td class="px-3 py-2">{{ d.format }}</td>
                  <td class="px-3 py-2">{{ capitalize(d.access) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <div v-if="!departmentsWithIdeas.length && !world.dataSources.length && !world.audit.summary" class="py-8 text-center">
          <p class="text-sm font-medium text-gray-900 dark:text-gray-100">No findings yet</p>
          <p class="mx-auto mt-1 max-w-[42ch] text-xs text-gray-500 dark:text-gray-400">The AI audit finds where agents would help most, and where your data lives and in what format.</p>
          <BaseButton size="sm" variant="secondary" class="mt-3" @click="$emit('ask', AUDIT_PROMPT)">Start the audit</BaseButton>
        </div>
      </div>

      <div v-else data-testid="build-context">
        <button
          v-if="research.status === 'done'"
          type="button"
          class="mb-3 block text-left text-xs font-medium text-action-primary-600 dark:text-action-primary-500 underline-offset-2 hover:underline"
          data-testid="build-context-research-link"
          @click="tab = 'findings'"
        >This draws on your company research. Read it &rarr;</button>
        <PortalMarkdown v-if="world.contextMd" :content="world.contextMd" />
        <div v-else class="py-8 text-center">
          <p class="text-sm font-medium text-gray-900 dark:text-gray-100">No context.md yet</p>
          <p class="mx-auto mt-1 max-w-[42ch] text-xs text-gray-500 dark:text-gray-400">This is where your company's terms, teams and systems get written down, so every agent you build speaks your language. The audit writes it.</p>
        </div>
      </div>
    </div>

    <footer class="shrink-0 px-4 py-3 space-y-1 border-t border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 text-xs text-gray-600 dark:text-gray-300">
      <p v-if="world.nextStep" data-testid="build-next-step"><span class="font-medium text-gray-900 dark:text-gray-100">Next: </span>{{ world.nextStep }}</p>
      <p class="text-gray-500 dark:text-gray-400" data-testid="build-instance-note">A live view of your Trinity instance{{ instanceHost ? ` (${instanceHost})` : '' }}. Agents built here are real and stay there.</p>
      <button type="button" class="mt-1 text-xs text-action-primary-600 dark:text-action-primary-400 hover:underline" data-testid="build-world-get-help" @click="$emit('get-help')">Need help? Reach the team</button>
    </footer>
  </aside>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseCard from '@/components/base/BaseCard.vue'
import InlineError from '@/components/InlineError.vue'
import OverflowTabs from '@/components/OverflowTabs.vue'
import PortalMarkdown from '../PortalMarkdown.vue'
import PortalBuildAgentDetail from './PortalBuildAgentDetail.vue'
import PortalBuildLevels from './PortalBuildLevels.vue'
import { BUILD_STAGES, LEVEL_LABELS, STATUS_DISPLAY, instanceAgentsFor, liveAgentFor, stepState } from './portalBuild'

const AUDIT_PROMPT = "Let's do the AI audit now."
const ORCHESTRATOR_PROMPT = 'Orchestrators are turned on for this instance. Help me design one for the agents we have.'

const props = defineProps({
  world: { type: Object, required: true },
  intake: { type: Object, required: true },
  roster: { type: Array, default: () => [] },
  orchestrationAvailable: { type: Boolean, default: false },
  readError: { type: String, default: '' },
  research: { type: Object, default: () => ({ status: 'none', report: '', error: '' }) },
  instanceHost: { type: String, default: '' },
  // The five levels (`levels.js`), empty until the page has read them once.
  levels: { type: Array, default: () => [] },
  levelCelebrate: { type: Object, default: null },
  guideLabel: { type: String, default: 'Atlas' },
})
defineEmits(['ask', 'retry', 'dismiss-error', 'finish-profile', 'retry-research', 'get-help', 'dismiss-level'])

const tab = ref('world')
const selectedId = ref('')

const worldName = computed(() => props.world.company.name || 'Your world')
const selected = computed(() => props.world.agents.find((a) => a.id === selectedId.value) || null)

const companyMeta = computed(() => [props.world.company.industry, props.world.company.size].filter(Boolean).join(', '))

// Departments the guide named, plus any an agent names that the guide has not
// listed, so an agent is never left off the map.
const zones = computed(() => {
  const byName = new Map()
  for (const dept of props.world.departments) {
    byName.set(dept.name, { name: dept.name, opportunityCount: dept.opportunities.length, agents: [] })
  }
  for (const agent of props.world.agents) {
    const name = agent.department || 'General'
    if (!byName.has(name)) byName.set(name, { name, opportunityCount: 0, agents: [] })
    byName.get(name).agents.push(agent)
  }
  return [...byName.values()]
})

const instanceAgents = computed(() => instanceAgentsFor(props.roster, props.world.agents))

const departmentsWithIdeas = computed(() => props.world.departments.filter((d) => d.opportunities.length))

const tabs = computed(() => {
  const ideas = departmentsWithIdeas.value.reduce((n, d) => n + d.opportunities.length, 0)
  return [
    { id: 'world', label: 'Your company' },
    { id: 'findings', label: 'Findings', badge: ideas || undefined, badgeVariant: 'neutral' },
    { id: 'context', label: 'Context' },
  ]
})

function partsLine(agent) {
  const done = agent.skills.filter((s) => s.status === 'done').length
  const parts = []
  if (agent.skills.length) parts.push(`${done} of ${agent.skills.length} skills`)
  if (agent.manages.length) parts.push(`runs ${agent.manages.length} ${agent.manages.length === 1 ? 'agent' : 'agents'}`)
  if (agent.connections.length) {
    parts.push(`${agent.connections.length} ${agent.connections.length === 1 ? 'connection' : 'connections'}`)
  }
  return parts.join(', ') || 'Being designed'
}

const capitalize = (s) => (s ? s[0].toUpperCase() + s.slice(1) : '')

// A level moment lights the Levels card, which lives on Your company: the page brings the
// right side there and puts it back afterwards with the function this returns.
// While it is held there, a move of the guide's is kept for the restore rather than made.
let held = null
function showCompany() {
  const hold = { tab: tab.value, selectedId: selectedId.value, followed: '' }
  held = hold
  tab.value = 'world'
  selectedId.value = ''
  return () => {
    if (held === hold) held = null
    const id = hold.followed || hold.selectedId
    tab.value = hold.followed ? 'world' : hold.tab
    selectedId.value = props.world.agents.some((a) => a.id === id) ? id : ''
  }
}
defineExpose({ showCompany })

// The view follows the guide only when it moves to something new, so someone who
// stepped back to the map is not pulled away again by the next reread.
watch(() => props.world.focus.agent, (id) => {
  if (id && props.world.agents.some((a) => a.id === id)) {
    if (held) { held.followed = id; return }
    selectedId.value = id
    tab.value = 'world'
  }
}, { immediate: true })
</script>
