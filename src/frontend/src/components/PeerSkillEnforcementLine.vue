<template>
  <!--
    abilityai/trinity-enterprise#925 — is a peer's skill grant enforced INSIDE
    this agent? Peer skill grants are checked at skill load by the in-agent
    hook (ent#752); without an intact hook, a peer that asks in prose is
    checked only by the skill names it sends. Shown only when a peer could
    reach one of this agent's library skills without a grant, and only to a
    person who may manage its skills (the server answers null to everyone
    else). Same treatment and vocabulary as the Skills tab's gate line
    (ent#754). It sits at the bottom of the panel, so a late answer moves
    nothing above it.
  -->
  <p
    v-if="line"
    class="mt-3 text-[12.5px] leading-5 text-status-warning-700 dark:text-status-warning-400"
    :title="`${line} (${state.hook})`"
    data-testid="peer-skill-enforcement"
  >{{ line }}</p>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useAgentsStore } from '../stores/agents'

const props = defineProps({
  agentName: { type: String, required: true },
  agentStatus: { type: String, default: '' },
})

const agentsStore = useAgentsStore()

const NO_ANSWER = Object.freeze({ reachable: null, hook: null })
const state = ref(NO_ANSWER)

// Every hook state the agent reports, in words (the raw state on hover only).
const PREFIX = 'Agents calling this agent are checked only for library skills they name: '
const REASONS = {
  missing: 'its image has no in-agent skill check. Rebuild the base image and recreate the agent.',
  not_root_owned: "its in-agent skill check isn't protected. Recreate the agent to restore it.",
  writable: 'its in-agent skill check can be changed from inside it. Recreate the agent to restore it.',
  unsupported_runtime: "its runtime can't run an in-agent skill check.",
  predates: 'its image predates the in-agent skill check. Recreate the agent to enforce grants inside it.',
}
const FALLBACK = "its in-agent skill check isn't intact. Recreate the agent to restore it."

const line = computed(() => {
  const { reachable, hook } = state.value
  // `ok` is enforced; `unknown` is an agent that did not answer (stopped).
  if (reachable !== true || !hook || hook === 'ok' || hook === 'unknown') return ''
  return PREFIX + (REASONS[hook] || FALLBACK)
})

// Only the latest read may answer: a status flip or an agent switch starts a
// new one, and an older read that resolves after it is dropped.
let latest = 0

async function load() {
  const mine = ++latest
  try {
    const data = await agentsStore.getAgentPeerSkillEnforcement(props.agentName)
    if (mine !== latest) return
    state.value = { reachable: data?.reachable ?? null, hook: data?.hook ?? null }
  } catch {
    // A failed read is "no answer", never an error: the line is a warning
    // about the agent, and the grants above stand either way.
    if (mine === latest) state.value = NO_ANSWER
  }
}

onMounted(load)
watch(() => props.agentName, () => {
  state.value = NO_ANSWER
  load()
})
// Started or stopped: the in-agent check can now (or no longer) answer.
watch(() => props.agentStatus, load)
</script>
