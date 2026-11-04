<!--
  Show me as a walk, across pages. Started by an address (`?walk=credentials&walk_agent=…`,
  from a Show me card in the Workspace chat), it lights up one thing to click at a time,
  starting on the Dashboard, and moves on when the page shows it was done: for an agent's
  keys, the agent's page opened, its Settings, Credentials, each key's box (Next), then
  Save, which waits until the page says it saved. Sharing, calling and schedules walk the
  same way to their own controls. Only the lit-up part can be clicked; the rest is dimmed
  and covered. It lives once beside the router view, so it survives the page changes.
  The steps and their words are in `showMeWalk.js`.
-->
<template>
  <Teleport to="body">
    <div
      v-if="walk && step"
      class="fixed inset-0 z-50 pointer-events-none"
      data-testid="show-me-walk"
      role="dialog"
      aria-modal="false"
      aria-labelledby="show-me-walk-title"
    >
      <template v-if="hole">
        <!-- Four panels around the lit-up part dim the page and take every click but its own. -->
        <div
          v-for="block in blocks"
          :key="block.side"
          class="absolute pointer-events-auto"
          :style="{ ...block.style, background: DIM }"
          data-testid="show-me-walk-block"
          aria-hidden="true"
        ></div>
        <div
          class="absolute rounded-xl ring-2 ring-action-primary-500 transition-all duration-200 motion-reduce:transition-none"
          :style="spot"
          data-testid="show-me-walk-spot"
          aria-hidden="true"
        ></div>
      </template>

      <div
        ref="card"
        class="absolute pointer-events-auto w-[340px] max-w-[calc(100vw-32px)] rounded-[10px] border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800 p-4 shadow-2xl transition-all duration-200 motion-reduce:transition-none"
        :style="cardPlace"
      >
        <p class="text-[11px] font-medium text-action-primary-600 dark:text-action-primary-400">Show me</p>
        <h2 id="show-me-walk-title" class="mt-1 text-base font-semibold text-gray-900 dark:text-gray-100">{{ shownTitle }}</h2>
        <p class="mt-1 text-sm text-gray-600 dark:text-gray-300" aria-live="polite" data-testid="show-me-walk-body">{{ shownBody }}</p>
        <p v-if="lost" class="mt-2 text-sm text-gray-600 dark:text-gray-300" data-testid="show-me-walk-lost">Can't see it on this page.</p>
        <div class="mt-4 flex items-center gap-2">
          <BaseButton v-if="!finished" variant="ghost" size="sm" data-testid="show-me-walk-stop" @click="stop">Stop</BaseButton>
          <span class="flex-1"></span>
          <BaseButton v-if="lost && step.fallback" variant="secondary" size="sm" data-testid="show-me-walk-fallback" @click="takeMeThere">{{ step.fallback.label }}</BaseButton>
          <BaseButton v-if="step.next && !lost" size="sm" data-testid="show-me-walk-next" @click="go(index + 1)">Next</BaseButton>
          <BaseButton v-if="finished" size="sm" data-testid="show-me-walk-done" @click="stop">Done</BaseButton>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import BaseButton from '@/components/base/BaseButton.vue'
import { tourCardPosition } from '@/components/portal/builder/portalBuildTour'
import { useNetworkStore } from '@/stores/network'
import { useAgentsStore } from '@/stores/agents'
import { agentDisplayName } from '@/utils/agentName'
import { keySteps, permissionSteps, pickRows, scheduleSteps, stepDone, walkFromQuery, walkSteps, withoutWalk } from './showMeWalk'

const props = defineProps({
  tickMs: { type: Number, default: 250 },
  // How long to look for the thing to click before offering a way on.
  giveUpMs: { type: Number, default: 5000 },
})

const PAD = 6
const DIM = 'rgb(3 7 18 / 0.62)'

const route = useRoute()
const router = useRouter()
const network = useNetworkStore()
const agentsStore = useAgentsStore()

const walk = ref(null)
const index = ref(0)
const picked = ref(null)
const rect = ref(null)
const lost = ref(false)
// The lit-up part is the step's `alt` (More, for a tab it has moved there), not its target.
const viaAlt = ref(false)
// The step's control has nothing to do yet (a greyed-out Save): say so, and offer Done.
const idle = ref(false)
const card = ref(null)
const cardPlace = ref({ right: '24px', bottom: '24px' })

let startedAt = 0
let seen = false
let scrolled = false
let current = null
let timer = null

// An agent's own name as the Dashboard shows it, once a list holding it has loaded.
function nameOf(name) {
  if (!name) return ''
  const mine = (list) => (Array.isArray(list) ? list.find((a) => a?.name === name) : null)
  const selected = agentsStore.selectedAgent?.name === name ? agentsStore.selectedAgent : null
  const found = mine(network.agents) || mine(agentsStore.agents) || selected
  return found ? agentDisplayName(found) : name
}
const label = computed(() => nameOf(walk.value?.agent))
const toLabel = computed(() => nameOf(walk.value?.to))

// The steps an `expand` step stands for, from the rows the page showed (`picked`).
function expanded(kind, rows, who) {
  if (kind === 'permissions') return permissionSteps(rows, walk.value.to, who)
  if (kind === 'schedules') return scheduleSteps(rows, walk.value.schedule, who)
  return keySteps(rows, who)
}
const steps = computed(() => {
  if (!walk.value) return []
  const who = { agent: walk.value.agent, label: label.value, toLabel: toLabel.value }
  const base = walkSteps(walk.value, who)
  return picked.value ? base.flatMap((s) => (s.expand ? expanded(s.expand, picked.value, who) : [s])) : base
})
const step = computed(() => steps.value[index.value] || null)
const finished = computed(() => Boolean(step.value?.end) || step.value?.id === 'finished' || idle.value)
const shownTitle = computed(() => (idle.value && step.value?.idle ? step.value.idle.title : step.value?.title))
const shownBody = computed(() => {
  if (idle.value && step.value?.idle) return step.value.idle.body
  return viaAlt.value && step.value?.alt ? step.value.alt.body : step.value?.body
})

const hole = computed(() => (rect.value ? {
  left: rect.value.left - PAD,
  top: rect.value.top - PAD,
  right: rect.value.left + rect.value.width + PAD,
  bottom: rect.value.top + rect.value.height + PAD,
} : null))
const px = (n) => `${Math.max(0, n)}px`
const blocks = computed(() => {
  const h = hole.value
  if (!h) return []
  return [
    { side: 'top', style: { left: 0, top: 0, width: '100%', height: px(h.top) } },
    { side: 'bottom', style: { left: 0, top: px(h.bottom), width: '100%', bottom: 0 } },
    { side: 'left', style: { left: 0, top: px(h.top), width: px(h.left), height: px(h.bottom - h.top) } },
    { side: 'right', style: { left: px(h.right), top: px(h.top), right: 0, height: px(h.bottom - h.top) } },
  ]
})
const spot = computed(() => (hole.value ? {
  left: `${hole.value.left}px`,
  top: `${hole.value.top}px`,
  width: `${hole.value.right - hole.value.left}px`,
  height: `${hole.value.bottom - hole.value.top}px`,
} : {}))

const onScreen = (el) => {
  const r = el?.getBoundingClientRect?.()
  if (!r || !(r.width > 0 && r.height > 0)) return false
  const right = r.right ?? r.left + r.width
  const bottom = r.bottom ?? r.top + r.height
  return right > 0 && bottom > 0 && r.left < window.innerWidth && r.top < window.innerHeight
}
// Above or below the window but within its width: scrolling the page reaches it. Something
// pushed off to the side (a closed panel) doesn't count.
const scrolledAway = (el) => {
  const r = el?.getBoundingClientRect?.()
  if (!r || !(r.width > 0 && r.height > 0)) return false
  const right = r.right ?? r.left + r.width
  return right > 0 && r.left < window.innerWidth
}
// The first match that is really there: the Dashboard's list has a layout per screen size,
// and a hidden layout has no size. One on screen first; else one scrolled out of view (a
// long list, a banner above it), which the walk then scrolls to.
function find(selector) {
  if (!selector) return null
  let away = null
  for (const el of document.querySelectorAll(selector)) {
    if (el.closest('[data-testid="show-me-walk"]')) continue
    if (onScreen(el)) return el
    if (!away && scrolledAway(el)) away = el
  }
  return away
}
// The rows a walk picks from, as the page marked them.
function readRows(kind) {
  if (kind === 'permissions') {
    return [...document.querySelectorAll('[data-walk-permission]')].map((el) => ({
      name: el.dataset.walkPermission,
      checked: el.dataset.walkChecked,
    }))
  }
  if (kind === 'schedules') {
    return [...document.querySelectorAll('[data-walk-schedule]')].map((el) => ({
      id: el.dataset.walkSchedule,
      enabled: el.dataset.walkEnabled,
      name: el.dataset.walkScheduleName,
    }))
  }
  return [...document.querySelectorAll('[data-walk-credential]')].map((el) => ({
    name: el.dataset.walkCredential,
    status: el.dataset.walkStatus,
    required: el.dataset.walkRequired,
  }))
}

async function measure(el) {
  const r = el.getBoundingClientRect()
  rect.value = { left: r.left, top: r.top, width: r.width, height: r.height }
  await nextTick()
  const box = card.value?.getBoundingClientRect?.()
  cardPlace.value = tourCardPosition(
    { left: hole.value.left, top: hole.value.top, width: hole.value.right - hole.value.left, height: hole.value.bottom - hole.value.top },
    { width: box?.width || 340, height: box?.height || 200 },
    { width: window.innerWidth, height: window.innerHeight },
  )
}
function unlit() {
  current = null
  rect.value = null
  cardPlace.value = { right: '24px', bottom: '24px' }
}

function go(i) {
  index.value = i
  startedAt = Date.now()
  seen = false
  scrolled = false
  lost.value = false
  viaAlt.value = false
  idle.value = false
  unlit()
}

// One look at the page for the current step. Returns true when it moved on, so a page
// that already shows several steps done (a direct link) passes through them at once.
function look() {
  const s = step.value
  if (!walk.value || !s) return false
  const waited = Date.now() - startedAt
  const ready = !s.waitFor || Boolean(document.querySelector(s.waitFor)) || waited >= props.giveUpMs

  if (s.expand) {
    // Agents to call and schedules come from a list that must really be there: one that is
    // slow or failed to load is not an empty one. (Keys fall back to the paste box.)
    const listed = !s.waitFor || Boolean(document.querySelector(s.waitFor))
    if (s.expand !== true && !listed) {
      unlit()
      if (waited >= props.giveUpMs) lost.value = true
      return false
    }
    if (!ready) { unlit(); return false }
    const rows = readRows(s.expand)
    picked.value = s.expand === 'permissions' || s.expand === 'schedules' ? rows : pickRows(rows, walk.value.keys)
    go(index.value)
    return true
  }
  if (stepDone(s, route, walk.value.agent)) { go(index.value + 1); return true }
  if (s.until && s.until !== 'gone' && document.querySelector(s.until)) { go(index.value + 1); return true }

  idle.value = Boolean(s.idle && document.querySelector(s.idle.when))
  let el = find(s.target)
  viaAlt.value = false
  if (!el && s.alt) {
    el = find(s.alt.target)
    viaAlt.value = Boolean(el)
  }
  if (s.optional && !el && !seen) {
    if (ready) { go(index.value + 1); return true }
    unlit()
    return false
  }
  if (s.until === 'gone') {
    if (el) seen = true
    else if (seen) { go(index.value + 1); return true }
  }
  if (!s.target) { unlit(); return false }
  if (el) {
    if (!scrolled) { el.scrollIntoView?.({ block: 'center', inline: 'nearest' }); scrolled = true }
    current = el
    lost.value = false
    measure(el)
    return false
  }
  unlit()
  if (waited >= props.giveUpMs) lost.value = true
  return false
}
function tick() {
  for (let moves = 0; moves < 20 && look(); moves++) { /* moved on: look at the next step */ }
}

function takeMeThere() {
  const to = step.value?.fallback?.to
  go(index.value)
  if (to) router.push(to)
}

const follow = () => { if (current && walk.value) measure(current) }
// Escape stops the walk, except while it is closing the agent page's More menu. Heard in
// the capture phase, before the menu's own handler closes it and Vue removes it.
const onKey = (event) => {
  if (!walk.value || event.key !== 'Escape') return
  if (document.querySelector('[data-overflow-menu]')) return
  stop()
}

function begin(found) {
  walk.value = found
  picked.value = null
  go(0)
  clearInterval(timer)
  timer = setInterval(tick, props.tickMs)
  window.addEventListener('resize', follow)
  window.addEventListener('scroll', follow, true)
  document.addEventListener('keydown', onKey, true)
  tick()
}
function stop() {
  walk.value = null
  picked.value = null
  clearInterval(timer)
  timer = null
  unlit()
  window.removeEventListener('resize', follow)
  window.removeEventListener('scroll', follow, true)
  document.removeEventListener('keydown', onKey, true)
}

// Started by the address; taken out of it at once, so a reload doesn't start it again.
watch(() => route.query, (query) => {
  const found = walkFromQuery(query)
  if (!found) return
  router.replace({ path: route.path, query: withoutWalk(query), hash: route.hash })
  begin(found)
}, { immediate: true })

// A page change moves the lit-up part: look again straight away rather than on the next tick.
watch(() => route.fullPath, () => { if (walk.value) nextTick(tick) })

onBeforeUnmount(stop)
</script>
