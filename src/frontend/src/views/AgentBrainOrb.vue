<template>
  <div class="h-screen flex flex-col bg-black text-white overflow-hidden">

    <!-- ── Slim header (mirrors AgentWorkspace) ─────────────────────────────── -->
    <header class="flex-shrink-0 flex items-center gap-3 px-4 py-2.5 bg-gray-900 border-b border-gray-800 z-10">
      <button
        @click="$router.push(`/agents/${agentName}`)"
        class="p-1.5 rounded hover:bg-gray-800 text-gray-400 hover:text-white transition-colors flex-shrink-0"
        title="Back to agent"
      >
        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7" />
        </svg>
      </button>

      <AgentAvatar :name="agentName" :avatar-url="agent?.avatar_url" size="sm" class="flex-shrink-0" />

      <span class="font-semibold text-sm text-white truncate">{{ agentName }}</span>
      <span class="text-xs text-gray-500 truncate hidden sm:inline">· The Self-Rendering Mind</span>

      <span
        v-if="agent"
        :class="[
          'px-2 py-0.5 text-[11px] font-medium rounded-full flex-shrink-0',
          agent.status === 'running'
            ? 'bg-status-success-900/50 text-status-success-400'
            : 'bg-gray-700 text-gray-400'
        ]"
      >{{ agent.status }}</span>

      <span class="flex-1" />

      <span class="px-1.5 py-0.5 text-[10px] font-bold rounded bg-state-autonomous-900/40 text-state-autonomous-400 border border-state-autonomous-700/50 tracking-wide flex-shrink-0">
        BETA
      </span>
    </header>

    <!-- ── Orb (first-party static page, same-origin iframe) ────────────────── -->
    <div class="flex-1 relative bg-black">
      <iframe
        ref="orbFrame"
        src="/brain-orb/index.html"
        title="Brain Orb"
        class="absolute inset-0 w-full h-full border-0"
        allow="microphone"
        @load="sendInit"
      />

      <!-- Friendly overlay when the agent hasn't produced its visualization yet -->
      <div
        v-if="loadError"
        class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-black/85 text-center px-6 pointer-events-none"
      >
        <div class="text-3xl text-amber-400/80">◇</div>
        <div class="text-sm text-gray-300">This agent hasn't rendered its mind yet.</div>
        <div class="text-xs text-gray-500 max-w-md">
          The Brain Orb reads a visualization the agent produces in its own container.
          It will appear here once the agent has exported it.
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
/*
 * Brain Orb host (#58, trinity-enterprise). The orb itself is a verbatim,
 * first-party static page under /brain-orb/ (served from the frontend origin,
 * CSP script-src 'self'-clean). This view is a thin chrome + same-origin iframe.
 *
 * Auth: the orb's data fetch needs the user's JWT, but a fetch() from the iframe
 * doesn't auto-carry the Bearer token. We hand it over via origin-pinned
 * postMessage — never in a URL. The orb posts 'brain-orb:ready'; we reply with
 * 'brain-orb:init' { agentName, apiBase, authToken }. We also send on iframe
 * load (the orb attaches its listener before posting ready, so either path
 * delivers). The orb fetches GET /api/agents/{name}/brain-orb/data with that
 * token; a failure posts 'brain-orb:error' and we show the empty-state overlay.
 */
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { useRoute } from 'vue-router'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import { useSessionsStore } from '../stores/sessions'
import AgentAvatar from '../components/AgentAvatar.vue'

const route = useRoute()
const authStore = useAuthStore()
const sessionsStore = useSessionsStore()

const agentName = route.params.name
const agent = ref(null)
const orbFrame = ref(null)
const loadError = ref(false)

function sendInit() {
  const frame = orbFrame.value
  if (!frame || !frame.contentWindow) return
  frame.contentWindow.postMessage(
    {
      type: 'brain-orb:init',
      agentName,
      apiBase: '',                 // same-origin — relative /api paths
      authToken: authStore.token || '',
      // #60 Phase 3: gate the client-held voice tile on the platform voice flag.
      // The per-agent `brain-orb` capability is already enforced by the route
      // guard (router/index.js) — this page never loads for a non-capable agent —
      // so the flag alone is the correct additional gate here. The mint route is
      // independently flag-gated (404), so this is UI-only.
      voiceAvailable: !!sessionsStore.brainOrbVoiceAvailable,
      // #61 Phase 4a: gate the KB-write panel (capture/link) on the platform write
      // flag. UI-only — the broker /action + /actions routes independently enforce
      // the flag AND owner access, and orb.js only reveals the panel after GET
      // /actions confirms owner + the agent's write hook. run_skill + transcript
      // capture are Phase 4b (trinity-enterprise#66).
      writeAvailable: !!sessionsStore.brainOrbWriteAvailable,
    },
    window.location.origin,        // pin target origin (same-origin iframe)
  )
}

function onMessage(e) {
  // same-origin + same iframe only
  if (e.origin !== window.location.origin) return
  if (orbFrame.value && e.source !== orbFrame.value.contentWindow) return
  const d = e.data
  if (!d || typeof d !== 'object') return
  if (d.type === 'brain-orb:ready') {
    loadError.value = false
    sendInit()
  } else if (d.type === 'brain-orb:error') {
    loadError.value = true
  }
}

async function fetchAgent() {
  try {
    const r = await axios.get(`/api/agents/${agentName}`, {
      headers: authStore.authHeader,
    })
    agent.value = r.data
  } catch {
    // header degrades gracefully — the orb iframe still loads
  }
}

onMounted(() => {
  window.addEventListener('message', onMessage)
  fetchAgent()
})

onBeforeUnmount(() => {
  window.removeEventListener('message', onMessage)
})
</script>
