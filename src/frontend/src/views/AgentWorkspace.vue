<template>
  <div class="h-screen flex flex-col bg-gray-950 text-white overflow-hidden">

    <!-- ── Slim header ─────────────────────────────────────────────────────── -->
    <header class="flex-shrink-0 flex items-center gap-3 px-4 py-2.5 bg-gray-900 border-b border-gray-800">
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

    <!-- ── Split body ──────────────────────────────────────────────────────── -->
    <div class="flex-1 flex overflow-hidden">

      <!-- Left: voice panel ──────────────────────────────────────────────── -->
      <div class="w-2/5 flex-shrink-0 flex flex-col bg-black relative">

        <!-- Orb fills most of the panel -->
        <div class="flex-1 relative">
          <canvas ref="canvasEl" class="absolute inset-0 w-full h-full" />

          <!-- Tool calling label -->
          <Transition
            enter-active-class="transition ease-out duration-200"
            enter-from-class="opacity-0 translate-y-2"
            enter-to-class="opacity-100 translate-y-0"
            leave-active-class="transition ease-in duration-150"
            leave-from-class="opacity-100 translate-y-0"
            leave-to-class="opacity-0 translate-y-2"
          >
            <div
              v-if="voice.isToolCalling.value"
              class="absolute top-5 left-1/2 -translate-x-1/2 z-10 px-3 py-1 rounded-full text-xs font-medium tracking-widest uppercase"
              style="background: rgba(245,158,11,0.18); border: 1px solid rgba(245,158,11,0.35); color: rgba(253,211,77,0.9);"
            >
              {{ voice.toolName.value ? voice.toolName.value.replace(/_/g, ' ') : 'working…' }}
            </div>
          </Transition>

          <!-- Status indicator -->
          <div class="absolute bottom-20 left-1/2 -translate-x-1/2 z-10 flex items-center gap-2">
            <div class="w-1.5 h-1.5 rounded-full transition-colors duration-300" :style="{ background: statusDotColor }" />
            <span class="text-xs tracking-widest uppercase" :style="{ color: statusTextColor }">{{ statusLabel }}</span>
          </div>

          <!-- Error -->
          <div
            v-if="voice.error.value"
            class="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 z-10 px-4 py-2 rounded-lg text-sm text-center max-w-xs"
            style="background: rgba(127,29,29,0.7); border: 1px solid rgba(239,68,68,0.4); color: rgba(252,165,165,0.9);"
          >
            {{ voice.error.value }}
          </div>
        </div>

        <!-- Controls bar -->
        <div class="flex-shrink-0 px-5 py-4 flex flex-col gap-3 border-t border-gray-900">
          <!-- Voice selector -->
          <div class="flex items-center gap-2">
            <span class="text-xs text-gray-500 w-10 flex-shrink-0">Voice</span>
            <select
              v-model="selectedVoice"
              :disabled="voice.isActive.value"
              class="flex-1 text-xs bg-gray-900 border border-gray-700 rounded px-2 py-1 text-gray-300 focus:outline-none focus:border-action-primary-500 disabled:opacity-40"
            >
              <option v-for="v in VOICES" :key="v.id" :value="v.id">{{ v.label }}</option>
            </select>
          </div>

          <!-- Start / Mute / Stop -->
          <div class="flex items-center justify-center gap-4">
            <!-- Mute (only when active) -->
            <button
              v-if="voice.isActive.value"
              @click="voice.toggleMute()"
              class="w-10 h-10 rounded-full flex items-center justify-center transition-colors"
              :style="voice.muted.value
                ? 'background: rgba(217,119,6,0.35); border: 1px solid rgba(217,119,6,0.5);'
                : 'background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.15);'"
              :title="voice.muted.value ? 'Unmute' : 'Mute mic'"
            >
              <svg v-if="!voice.muted.value" class="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4M12 15a3 3 0 003-3V5a3 3 0 00-6 0v7a3 3 0 003 3z" />
              </svg>
              <svg v-else class="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z" />
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2" />
              </svg>
            </button>

            <!-- Start / Stop -->
            <button
              @click="toggleSession"
              :disabled="!agent || agent.status !== 'running'"
              class="flex items-center gap-2 px-5 py-2.5 rounded-full font-medium text-sm transition-all disabled:opacity-40 disabled:cursor-not-allowed"
              :style="voice.isActive.value
                ? 'background: rgba(185,28,28,0.7); border: 1px solid rgba(220,38,38,0.5);'
                : 'background: rgba(79,70,229,0.8); border: 1px solid rgba(99,102,241,0.5);'"
            >
              <svg v-if="!voice.isActive.value" class="w-4 h-4 text-white" fill="currentColor" viewBox="0 0 24 24">
                <circle cx="12" cy="12" r="8" />
              </svg>
              <svg v-else class="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 8l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2M5 3a2 2 0 00-2 2v1c0 8.284 6.716 15 15 15h1a2 2 0 002-2v-3.28a1 1 0 00-.684-.948l-4.493-1.498a1 1 0 00-1.21.502l-1.13 2.257a11.042 11.042 0 01-5.516-5.517l2.257-1.128a1 1 0 00.502-1.21L9.228 3.683A1 1 0 008.279 3H5z" />
              </svg>
              <span class="text-white">{{ voice.isActive.value ? 'End Session' : 'Start' }}</span>
            </button>
          </div>
        </div>
      </div>

      <!-- Right: canvas panel ─────────────────────────────────────────────── -->
      <div class="flex-1 flex flex-col bg-gray-900 border-l border-gray-800 overflow-hidden">

        <!-- Canvas header -->
        <div class="flex-shrink-0 flex items-center justify-between gap-3 px-5 py-3 border-b border-gray-800">
          <h2 class="text-sm font-medium text-gray-200 truncate">
            {{ displayedPanel.title || 'Canvas' }}
          </h2>
          <div class="flex items-center gap-2 flex-shrink-0">
            <!-- History navigation (shown once ≥2 snapshots exist) -->
            <div v-if="hasHistory" class="flex items-center gap-1">
              <button
                @click="goPrev"
                :disabled="effectiveIndex <= 0"
                class="p-1 rounded hover:bg-gray-800 text-gray-400 hover:text-white transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                title="Previous snapshot"
              >
                <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M15 19l-7-7 7-7" />
                </svg>
              </button>
              <select
                :value="effectiveIndex"
                @change="selectSnapshot"
                class="max-w-[12rem] text-xs bg-gray-900 border border-gray-700 rounded px-1.5 py-0.5 text-gray-300 focus:outline-none focus:border-action-primary-500"
                title="Jump to snapshot"
              >
                <option v-for="(snap, i) in history" :key="i" :value="i">{{ snapshotLabel(snap, i) }}</option>
              </select>
              <button
                @click="goNext"
                :disabled="isLive"
                class="p-1 rounded hover:bg-gray-800 text-gray-400 hover:text-white transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                title="Next snapshot"
              >
                <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M9 5l7 7-7 7" />
                </svg>
              </button>
              <span
                v-if="isLive"
                class="px-1.5 py-0.5 text-[10px] font-semibold rounded bg-status-success-900/40 text-status-success-400 tracking-wide"
              >LIVE</span>
              <span
                v-else
                class="px-1.5 py-0.5 text-[10px] font-semibold rounded bg-gray-700 text-gray-300 tracking-wide"
              >PINNED</span>
            </div>
            <span
              v-if="panelUpdatedAgo"
              :class="['text-xs transition-colors duration-700', justUpdated ? 'text-state-autonomous-300' : 'text-gray-600']"
            >{{ panelUpdatedAgo }}</span>
          </div>
        </div>

        <!-- Canvas content — cross-fades on each update / history navigation;
             the keyed inner node is the single transition child. -->
        <div class="flex-1 overflow-auto p-6 relative">
          <Transition :name="canvasTransition" mode="out-in">
            <div :key="transitionKey" class="h-full">

              <!-- Empty state -->
              <div v-if="!displayedPanel.content" class="flex flex-col items-center justify-center h-full gap-4 text-center">
                <div class="w-16 h-16 rounded-full border-2 border-gray-800 flex items-center justify-center">
                  <svg class="w-7 h-7 text-gray-700" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
                  </svg>
                </div>
                <p class="text-gray-600 text-sm max-w-xs">
                  {{ voice.isActive.value
                    ? 'The agent can display notes, summaries, and structured content here during your conversation.'
                    : 'Start a conversation — the agent will display structured content here in real time.' }}
                </p>
              </div>

              <!-- Markdown content -->
              <div
                v-else-if="displayedPanel.type === 'markdown'"
                v-html="renderedContent"
                class="prose prose-sm prose-invert max-w-none
                  prose-headings:text-gray-100 prose-headings:font-semibold
                  prose-p:text-gray-300 prose-p:leading-relaxed
                  prose-strong:text-gray-100
                  prose-code:text-state-autonomous-300 prose-code:bg-gray-800 prose-code:px-1 prose-code:rounded prose-code:text-xs
                  prose-pre:bg-gray-800 prose-pre:border prose-pre:border-gray-700
                  prose-ul:text-gray-300 prose-ol:text-gray-300
                  prose-li:marker:text-gray-500
                  prose-blockquote:border-action-primary-500 prose-blockquote:text-gray-400
                  prose-a:text-action-primary-400 hover:prose-a:text-action-primary-300
                  prose-hr:border-gray-700
                  prose-table:text-sm
                  prose-th:text-gray-200 prose-th:bg-gray-800
                  prose-td:text-gray-300 prose-td:border-gray-700"
              />

              <!-- Mermaid diagram — rendered in-parent via the bundled mermaid lib
                   (securityLevel:'strict') then DOMPurify-sanitized before v-html
                   (H-005). No iframe: the production CSP (script-src 'self') blocks
                   inline scripts in a srcdoc iframe and CORP blocks the bundle from
                   the iframe's opaque origin (#979 prod-CSP regression). -->
              <div v-else-if="displayedPanel.type === 'mermaid'" class="flex items-center justify-center h-full">
                <pre
                  v-if="mermaidError"
                  class="text-xs text-status-danger-400 whitespace-pre-wrap font-mono max-w-full overflow-auto"
                >{{ mermaidError }}</pre>
                <div v-else v-html="mermaidSvg" class="mermaid-host max-w-full max-h-full" />
              </div>

              <!-- Image — rendered in the parent DOM via a Vue :src binding (safe;
                   no markup injection). Workspace-path images come from an
                   authenticated blob fetch; web URLs render directly. -->
              <div v-else-if="displayedPanel.type === 'image'" class="flex flex-col items-center justify-center h-full gap-3">
                <img
                  v-if="displayedImageSrc && !imageError"
                  :src="displayedImageSrc"
                  :alt="displayedPanel.title || 'Agent image'"
                  class="max-w-full max-h-full object-contain rounded-lg"
                  @error="imageError = true"
                />
                <div v-else class="text-sm" :class="imageError ? 'text-status-danger-400' : 'text-gray-500'">
                  {{ imageError ? 'Image could not be loaded.' : 'Loading image…' }}
                </div>
                <p v-if="displayedPanel.caption" class="text-sm text-gray-400 text-center max-w-prose">
                  {{ displayedPanel.caption }}
                </p>
              </div>

              <!-- HTML content — DOMPurify-sanitized and rendered in-parent (H-005),
                   same trust model as markdown. Scripts are stripped, so agent JS
                   (e.g. chart.js) does NOT execute — static layout only. Replaces
                   the prior srcdoc iframe, which the production CSP blocked (#979). -->
              <div
                v-else-if="displayedPanel.type === 'html'"
                v-html="sanitizedHtml"
                class="agent-html-panel text-sm text-gray-300 max-w-none"
              />
            </div>
          </Transition>
        </div>
      </div>

    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import { useAgentsStore } from '../stores/agents'
import { useVoiceSession } from '../composables/useVoiceSession'
import { renderMarkdown } from '../utils/markdown'
import AgentAvatar from '../components/AgentAvatar.vue'
import { VOICES, DEFAULT_VOICE_NAME } from '../constants/voices'
// Mermaid renders in-parent (not in a sandboxed iframe): the production CSP
// (script-src 'self') blocks inline scripts in a srcdoc iframe, and CORP blocks
// the bundle from the iframe's opaque origin (#979). securityLevel:'strict'
// disables interactivity/htmlLabels; the output SVG is DOMPurify-sanitized before
// it touches the DOM (H-005). Imported as a normal ESM dep so it lands in this
// route's chunk.
import mermaid from 'mermaid'
import DOMPurify from 'dompurify'

mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'dark' })

const route = useRoute()
const authStore = useAuthStore()
const agentsStore = useAgentsStore()
// route.params.name is a plain string in Vue Router 4 setup()
const agentName = route.params.name
const voice = useVoiceSession(agentName)

const agent = ref(null)
// Defaults to the persisted per-agent voice once fetchAgent resolves (#28); the
// user can still override for this session via the picker.
const selectedVoice = ref(DEFAULT_VOICE_NAME)
const panelState = ref({ type: 'empty', content: '', title: null, updated_at: null })
let panelPollTimer = null
let panelFetching = false

// ── Panel history (client-side, ephemeral) ───────────────────────────────────
// Ring buffer of panel snapshots. "Live" (viewIndex === -1) follows the latest
// update; navigating back pins a snapshot until the user returns to live or a
// brand-new update arrives. Frontend-only — no backend/history persistence.
const HISTORY_MAX = 40
const history = ref([])      // oldest → newest
const viewIndex = ref(-1)    // -1 = live; otherwise index into history
const justUpdated = ref(false)  // drives the header "updated" flash

const prefersReducedMotion =
  typeof window !== 'undefined' && window.matchMedia
    ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
    : false

// Image blob cache: workspace-path images are fetched through the authenticated
// /files/preview endpoint (a bare <img src> would 401), keyed by path so history
// navigation reuses them. objectURLs are revoked on eviction + unmount (F2).
const imageBlobCache = new Map()  // path → objectURL
const imageObjectUrl = ref(null)
const imageError = ref(false)

// Voice list is shared with the VoIP settings picker (src/constants/voices.js)
// so the two can't drift (#28).

// ── Computed ────────────────────────────────────────────────────────────────

// The snapshot currently shown: a pinned history entry, or the live panel.
const effectiveIndex = computed(() => {
  if (viewIndex.value >= 0 && viewIndex.value < history.value.length) return viewIndex.value
  return history.value.length - 1
})
const displayedPanel = computed(() => {
  const i = effectiveIndex.value
  if (i >= 0 && i < history.value.length) return history.value[i]
  return panelState.value
})
const isLive = computed(() => effectiveIndex.value === history.value.length - 1)
const hasHistory = computed(() => history.value.length >= 2)

// Re-key the canvas content on every snapshot change so the Transition fires on
// both live updates and history navigation. updated_at is unique per snapshot;
// effectiveIndex disambiguates the empty state.
const transitionKey = computed(() => `${effectiveIndex.value}:${displayedPanel.value.updated_at || 'empty'}`)
// prefers-reduced-motion → an undefined transition name (no CSS classes) = instant swap.
const canvasTransition = computed(() => (prefersReducedMotion ? 'canvas-none' : 'canvas-fade'))

const displayedImageSrc = computed(() => {
  const p = displayedPanel.value
  if (p.type !== 'image') return null
  return p.image_kind === 'url' ? p.content : imageObjectUrl.value
})

const renderedContent = computed(() =>
  displayedPanel.value.type === 'markdown' ? renderMarkdown(displayedPanel.value.content || '') : ''
)

const panelUpdatedAgo = computed(() => {
  if (!displayedPanel.value.updated_at) return null
  const secs = Math.round((Date.now() - new Date(displayedPanel.value.updated_at).getTime()) / 1000)
  if (secs < 5)  return 'just now'
  if (secs < 60) return `${secs}s ago`
  return `${Math.round(secs / 60)}m ago`
})

function snapshotLabel(snap, idx) {
  const t = snap.updated_at
    ? new Date(snap.updated_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    : '—'
  const kind = snap.type === 'mermaid' ? 'diagram' : snap.type
  return `${idx + 1}. ${snap.title || kind} · ${t}`
}

// ── History navigation ────────────────────────────────────────────────────────

function goPrev() {
  const i = effectiveIndex.value
  if (i > 0) viewIndex.value = i - 1
}
function goNext() {
  const i = effectiveIndex.value
  if (i < history.value.length - 1) {
    const next = i + 1
    viewIndex.value = next >= history.value.length - 1 ? -1 : next  // snap to live at the end
  }
}
function selectSnapshot(e) {
  const idx = Number(e.target.value)
  viewIndex.value = idx >= history.value.length - 1 ? -1 : idx
}

function pushHistory(snap) {
  history.value.push(snap)
  while (history.value.length > HISTORY_MAX) {
    const evicted = history.value.shift()
    // Revoke a cached image blob only if no remaining snapshot references its path.
    if (evicted.type === 'image' && evicted.image_kind === 'path') {
      const stillUsed = history.value.some(
        (s) => s.type === 'image' && s.image_kind === 'path' && s.content === evicted.content
      )
      if (!stillUsed && imageBlobCache.has(evicted.content)) {
        URL.revokeObjectURL(imageBlobCache.get(evicted.content))
        imageBlobCache.delete(evicted.content)
      }
    }
  }
}

// ── In-parent panel rendering (HTML + Mermaid) ───────────────────────────────

// HTML panels render via DOMPurify (same trust model as markdown, H-005). Scripts
// are stripped, so agent JS (chart.js) does not run — static layout only. This
// replaces the prior srcdoc iframe, which the production CSP (script-src 'self')
// + CORP blocked entirely (#979).
const sanitizedHtml = computed(() =>
  displayedPanel.value.type === 'html'
    ? DOMPurify.sanitize(displayedPanel.value.content || '')
    : ''
)

// Mermaid renders to an SVG string off-DOM (securityLevel:'strict' disables
// interactivity + htmlLabels), then DOMPurify-sanitizes the SVG before v-html.
// mermaid.render is async and the displayed snapshot can change while a render is
// in flight (live update or history navigation), so a monotonic seq token drops
// stale results. Invalid syntax surfaces a contained error + the source.
const mermaidSvg = ref('')
const mermaidError = ref('')
let mermaidRenderSeq = 0

async function renderMermaid(src) {
  const seq = ++mermaidRenderSeq
  mermaidError.value = ''
  mermaidSvg.value = ''
  if (!src) return
  try {
    const { svg } = await mermaid.render(`voice-mmd-${seq}`, String(src))
    if (seq !== mermaidRenderSeq) return  // superseded by a newer snapshot
    mermaidSvg.value = DOMPurify.sanitize(svg)
  } catch (e) {
    if (seq !== mermaidRenderSeq) return
    mermaidSvg.value = ''
    mermaidError.value = `Diagram error:\n${(e && e.message) ? e.message : String(e)}\n\n${src}`
  }
}

// Fetch a workspace-path image through the authenticated /files/preview endpoint
// (reuses the store's blob helper) and bind the objectURL. Web-URL images bypass
// this entirely (rendered directly). Cached by path for history navigation.
async function loadImageBlob(path) {
  imageError.value = false
  if (imageBlobCache.has(path)) {
    imageObjectUrl.value = imageBlobCache.get(path)
    return
  }
  // Still showing the path we set out to fetch? Guards against out-of-order
  // resolution when the user navigates history faster than blobs load.
  const stillCurrent = () =>
    displayedPanel.value.type === 'image' && displayedPanel.value.content === path
  try {
    const res = await agentsStore.getFilePreviewBlob(agentName, path)
    imageBlobCache.set(path, res.url)  // cache regardless, for later navigation
    if (stillCurrent()) imageObjectUrl.value = res.url
  } catch (_) {
    if (stillCurrent()) {
      imageObjectUrl.value = null
      imageError.value = true
    }
  }
}

// Image and mermaid need work on display: a workspace-path image is fetched as an
// authenticated blob; a mermaid diagram is rendered to SVG asynchronously. HTML
// renders synchronously via the sanitizedHtml computed.
watch(displayedPanel, (panel) => {
  if (panel.type === 'image') {
    imageError.value = false
    if (panel.image_kind === 'path') loadImageBlob(panel.content)
    else imageObjectUrl.value = null  // url-kind renders content directly
  } else if (panel.type === 'mermaid') {
    renderMermaid(panel.content)
  }
}, { deep: true })

// ── Session lifecycle ────────────────────────────────────────────────────────

function revokeAllImageBlobs() {
  for (const url of imageBlobCache.values()) URL.revokeObjectURL(url)
  imageBlobCache.clear()
  imageObjectUrl.value = null
}

function resetPanelState() {
  panelState.value = { type: 'empty', content: '', title: null, updated_at: null }
  history.value = []
  viewIndex.value = -1
  imageError.value = false
  revokeAllImageBlobs()
}

async function toggleSession() {
  if (voice.isActive.value) {
    stopPanelPoll()
    await voice.stop()
    // Content preserved in panelState — user can still read what the agent wrote
  } else {
    resetPanelState()  // Clear previous session's content before starting new one
    await voice.start(null, selectedVoice.value, true) // workspace_mode = true
    startPanelPoll()
  }
}

// ── Panel polling ────────────────────────────────────────────────────────────

let flashTimer = null
function flashUpdated() {
  if (prefersReducedMotion) return
  justUpdated.value = true
  if (flashTimer !== null) clearTimeout(flashTimer)
  flashTimer = setTimeout(() => { justUpdated.value = false }, 1100)
}

function startPanelPoll() {
  stopPanelPoll()
  panelPollTimer = setInterval(fetchPanel, 300)
}

function stopPanelPoll() {
  if (panelPollTimer !== null) {
    clearInterval(panelPollTimer)
    panelPollTimer = null
  }
}

async function fetchPanel() {
  const sid = voice.voiceSessionId.value
  if (!sid || panelFetching) return
  panelFetching = true
  try {
    const r = await axios.get(
      `/api/agents/${agentName}/voice/${sid}/panel`,
      { headers: authStore.authHeader }
    )
    // Only update when the agent has actually called a panel tool (updated_at
    // is non-null) AND the timestamp changed. A null updated_at means "session
    // not found or no tool ever called" — never overwrite real content with it.
    if (r.data.updated_at !== null && r.data.updated_at !== panelState.value.updated_at) {
      panelState.value = r.data
      pushHistory(r.data)
      viewIndex.value = -1  // a brand-new update snaps the view back to live
      flashUpdated()
    }
  } catch (_) {
    // Ignore poll errors (session may have just ended)
  } finally {
    panelFetching = false
  }
}

// Stop poll when session ends naturally (content is preserved in panelState)
watch(() => voice.isActive.value, (active) => {
  if (!active) stopPanelPoll()
})

// ── Agent fetch ──────────────────────────────────────────────────────────────

async function fetchAgent() {
  try {
    const r = await axios.get(`/api/agents/${agentName}`, {
      headers: authStore.authHeader,
    })
    agent.value = r.data
  } catch (_) {}
}

// Default the session picker to the agent's persisted voice (#28). Best-effort:
// a failure leaves the picker on DEFAULT_VOICE_NAME.
async function fetchPersistedVoice() {
  try {
    const r = await axios.get(`/api/agents/${agentName}/voice/name`, {
      headers: authStore.authHeader,
    })
    if (r.data?.voice_name) selectedVoice.value = r.data.voice_name
  } catch (_) {}
}

// ── Orb animation ────────────────────────────────────────────────────────────
// Verbatim from VoiceOverlay.vue — self-contained in this page.

const canvasEl = ref(null)
let rafHandle = null
let frameCount = 0

function _hash(n) {
  return Math.abs(Math.sin(n * 127.1 + 311.7) * 43758.5453) % 1
}
function noise(x, y = 0, z = 0) {
  const ix = Math.floor(x), iy = Math.floor(y), iz = Math.floor(z)
  const fx = x - ix, fy = y - iy, fz = z - iz
  const ux = fx * fx * (3 - 2 * fx), uy = fy * fy * (3 - 2 * fy), uz = fz * fz * (3 - 2 * fz)
  const h = (a, b, c) => _hash(a + b * 57 + c * 113)
  return (
    h(ix,   iy,   iz)   * (1-ux)*(1-uy)*(1-uz) +
    h(ix+1, iy,   iz)   * ux    *(1-uy)*(1-uz) +
    h(ix,   iy+1, iz)   * (1-ux)*uy    *(1-uz) +
    h(ix+1, iy+1, iz)   * ux    *uy    *(1-uz) +
    h(ix,   iy,   iz+1) * (1-ux)*(1-uy)*uz     +
    h(ix+1, iy,   iz+1) * ux    *(1-uy)*uz     +
    h(ix,   iy+1, iz+1) * (1-ux)*uy    *uz     +
    h(ix+1, iy+1, iz+1) * ux    *uy    *uz
  )
}
function curl(x, y, t, ox, oy) {
  const eps = 1.5, sc = 0.0035
  const dy = noise(x*sc+ox, (y+eps)*sc+oy, t) - noise(x*sc+ox, (y-eps)*sc+oy, t)
  const dx = noise((x+eps)*sc+ox, y*sc+oy, t) - noise((x-eps)*sc+ox, y*sc+oy, t)
  return { x: dy/(eps*sc*2), y: -dx/(eps*sc*2) }
}
function hsbToRgb(h, s, b) {
  h /= 360; s /= 100; b /= 100
  let r, g, bv
  const i = Math.floor(h*6), f = h*6-i
  const pp=b*(1-s), q=b*(1-f*s), tv=b*(1-(1-f)*s)
  switch(i%6) {
    case 0: r=b;  g=tv; bv=pp; break
    case 1: r=q;  g=b;  bv=pp; break
    case 2: r=pp; g=b;  bv=tv; break
    case 3: r=pp; g=q;  bv=b;  break
    case 4: r=tv; g=pp; bv=b;  break
    case 5: r=b;  g=pp; bv=q;  break
  }
  return [Math.round(r*255), Math.round(g*255), Math.round(bv*255)]
}
function buildSprites(hueShift) {
  const cfgs = [
    {h:29,s:44},{h:33,s:40},{h:38,s:35},
    {h:39,s:23},{h:43,s:19},{h:47,s:15},
    {h:45,s:13},{h:50,s:10},{h:55,s:7},
  ]
  return cfgs.map(({ h, s }) => {
    const sz = 128, cx = 64, r = 61
    const c = document.createElement('canvas')
    c.width = c.height = sz
    const ctx = c.getContext('2d')
    const hFinal = ((h + hueShift) % 360 + 360) % 360
    const [rv, gv, bv] = hsbToRgb(hFinal, s, 90)
    const grad = ctx.createRadialGradient(cx, cx, 0, cx, cx, r)
    grad.addColorStop(0,    `rgba(${rv},${gv},${bv},0.94)`)
    grad.addColorStop(0.32, `rgba(${rv},${gv},${bv},0.52)`)
    grad.addColorStop(0.62, `rgba(${rv},${gv},${bv},0.16)`)
    grad.addColorStop(0.86, `rgba(${rv},${gv},${bv},0.03)`)
    grad.addColorStop(1,    `rgba(${rv},${gv},${bv},0)`)
    ctx.fillStyle = grad
    ctx.beginPath(); ctx.arc(cx, cx, r, 0, Math.PI*2); ctx.fill()
    return c
  })
}
function lerp(a, b, t) { return a + (b-a)*t }
function rnd(lo, hi) { return Math.random()*(hi-lo)+lo }

class Smoke {
  constructor(idx, sprites) {
    this.sprites = sprites
    this.type = idx % 3
    this.spriteIdx = this.type*3 + Math.floor(Math.random()*3)
    this.nox = rnd(0,100); this.noy = rnd(0,100); this.nosh = rnd(0,2000)
    this.rot = rnd(0, Math.PI*2); this.rotSpd = rnd(-0.012, 0.012)
    this.vx = rnd(-0.4,0.4); this.vy = rnd(-0.4,0.4)
    this.reset(true)
  }
  reset(init = false) {
    const a = rnd(0, Math.PI*2)
    let r
    if      (this.type===0) r = init ? rnd(18,145) : rnd(15,46)
    else if (this.type===1) r = init ? rnd(44,235) : rnd(38,76)
    else                    r = init ? rnd(78,315) : rnd(68,112)
    this.x = Math.cos(a)*r; this.y = Math.sin(a)*r
    this.life = init ? rnd(0.3,1.0) : 1.0
    if      (this.type===0) { this.baseSize=rnd(32,70); this.aspect=rnd(0.72,1.30); this.decay=rnd(0.0015,0.004) }
    else if (this.type===1) { this.baseSize=rnd(13,43); this.aspect=rnd(0.26,0.62); this.decay=rnd(0.0013,0.0034) }
    else                    { this.baseSize=rnd(4,15);  this.aspect=rnd(0.16,0.50); this.decay=rnd(0.001,0.0026) }
    this.sz = this.baseSize; this.ia = init ? 1.0 : 0.0
  }
  update(energy, bv, mv, hv, fc) {
    const te = this.type===0 ? bv : (this.type===1 ? mv : hv)
    const t = fc * 0.0022
    const c = curl(this.x, this.y, t, this.nox, this.noy)
    const cs = 5 + energy*12 + te*8
    const dist = Math.max(Math.sqrt(this.x*this.x + this.y*this.y), 1)
    const push = 1.2 + energy*3.5 + te*2.5
    this.vx = lerp(this.vx, c.x*cs + (this.x/dist)*push, 0.06)
    this.vy = lerp(this.vy, c.y*cs + (this.y/dist)*push, 0.06)
    this.x += this.vx*0.5; this.y += this.vy*0.5
    const ss = this.type===2 ? 3.5 : (this.type===1 ? 1.8 : 0.55)
    this.rotSpd += (noise(this.nosh + fc*0.004) - 0.5) * 0.0007
    this.rotSpd = Math.max(-0.042, Math.min(0.042, this.rotSpd))
    this.rot += this.rotSpd * ss * (1 + te*2.2)
    const spd = Math.sqrt(this.vx*this.vx + this.vy*this.vy)
    this.sz = this.baseSize * Math.max(0.18, 1.2/(1+spd*0.38)) * (0.5 + te*0.42 + energy*0.2)
    this.ia = Math.min(1.0, this.ia + 0.045)
    this.life -= this.decay
    if (this.life <= 0 || Math.sqrt(this.x*this.x+this.y*this.y) > 445) this.reset()
  }
  draw(ctx, spread, size, brightness) {
    const dist = Math.sqrt(this.x*this.x + this.y*this.y)
    const fs = this.type===0 ? 95 : (this.type===1 ? 135 : 160)
    const fe = this.type===0 ? 195 : (this.type===1 ? 250 : 285)
    const tF = Math.max(0, Math.min(1, (dist-fs)/(fe-fs)))
    const alpha = this.life * this.ia * (1 - tF*tF*(3-2*tF)) * (this.type===2 ? 0.22 : 0.15) * brightness
    if (alpha <= 0.001) return
    const w = this.sz * size * (0.42 + this.life*0.58)
    ctx.save()
    ctx.translate(this.x*spread, this.y*spread)
    ctx.rotate(this.rot)
    ctx.scale(1, this.aspect)
    ctx.globalAlpha = alpha
    ctx.drawImage(this.sprites[this.spriteIdx], -w, -w, w*2, w*2)
    ctx.restore()
  }
}

const STATE_HUE = {
  idle: 0, connecting: 0, listening: 90, speaking: 210, tool_calling: 0, error: -30,
}

let particles = []
let currentSprites = null
let currentHueShift = 0
let targetHueShift = 0
// Persistent orb smoothing state (lerped across frames, not recomputed raw).
let smoothedEnergy = 0
let coreSize = 58

function rebuildSprites(hueShift) {
  currentSprites = buildSprites(hueShift)
  currentHueShift = hueShift
  for (const p of particles) p.sprites = currentSprites
}
function initParticles(count = 220) {
  particles = []
  for (let i = 0; i < count; i++) particles.push(new Smoke(i, currentSprites))
}
function drawCore(ctx, size, fc) {
  const R = size
  ctx.save()
  const glow = ctx.createRadialGradient(0,0,R*0.15, 0,0,R*5.8)
  glow.addColorStop(0,    `rgba(215,148,45,0.13)`)
  glow.addColorStop(0.28, `rgba(190,122,35,0.06)`)
  glow.addColorStop(0.6,  `rgba(160,98,25,0.02)`)
  glow.addColorStop(1,    `rgba(130,72,18,0)`)
  ctx.fillStyle = glow
  ctx.beginPath(); ctx.arc(0,0,R*5.8,0,Math.PI*2); ctx.fill()
  const t = fc * 0.012
  const wA = noise(t)*0.08+0.96, wB = noise(t+50)*0.06+0.97
  const tilt = noise(t*0.4) * Math.PI * 0.35
  const sph = ctx.createRadialGradient(-R*0.26,-R*0.30, 0, 0,0, R*1.55)
  sph.addColorStop(0,    `rgba(255,252,215,0.94)`)
  sph.addColorStop(0.10, `rgba(255,238,165,0.88)`)
  sph.addColorStop(0.26, `rgba(248,200,95,0.76)`)
  sph.addColorStop(0.42, `rgba(215,152,52,0.54)`)
  sph.addColorStop(0.58, `rgba(175,105,30,0.28)`)
  sph.addColorStop(0.72, `rgba(150,85,22,0.10)`)
  sph.addColorStop(0.88, `rgba(130,70,18,0.03)`)
  sph.addColorStop(1,    `rgba(110,58,14,0)`)
  ctx.fillStyle = sph
  ctx.beginPath(); ctx.ellipse(0,0, R*wA*1.55, R*wB*1.55, tilt, 0, Math.PI*2); ctx.fill()
  ctx.restore()
}

function renderFrame() {
  const canvas = canvasEl.value
  if (!canvas) return
  const W = canvas.width, H = canvas.height
  const ctx = canvas.getContext('2d')
  frameCount++
  if (Math.abs(currentHueShift - targetHueShift) > 1) {
    const next = Math.round(lerp(currentHueShift, targetHueShift, 0.05))
    if (next !== currentHueShift) rebuildSprites(next)
  }
  const amp = voice.amplitude?.value ?? 0
  const targetEnergy = Math.min(1, amp * 2.5)
  // Asymmetric attack/release: rise quickly toward louder audio (0.18), fall back
  // gently (0.10) so the orb glides instead of stepping with each amplitude frame.
  const k = targetEnergy > smoothedEnergy ? 0.18 : 0.10
  smoothedEnergy = lerp(smoothedEnergy, targetEnergy, k)
  // Idle "breathe" floor — a slow low-amplitude sine so the orb never goes flat.
  const breathe = (Math.sin(frameCount * 0.035) * 0.5 + 0.5) * 0.06
  const energy = Math.max(smoothedEnergy, breathe)
  const bass = energy * 0.8, mid = energy * 0.5, high = energy * 0.3
  ctx.fillStyle = '#000'
  ctx.fillRect(0, 0, W, H)
  ctx.save()
  ctx.translate(W/2, H/2)
  for (const p of particles) {
    p.update(energy, bass, mid, high, frameCount)
    p.draw(ctx, 1.25, 1.25, 1.0)
  }
  // Smooth the core size toward its target (larger base 58, stronger 32× swing)
  // so the soft glow grows/shrinks smoothly rather than snapping.
  const targetCoreSize = 58 + energy * 32
  coreSize = lerp(coreSize, targetCoreSize, 0.12)
  drawCore(ctx, coreSize, frameCount)
  ctx.restore()
  rafHandle = requestAnimationFrame(renderFrame)
}

function resizeCanvas() {
  const canvas = canvasEl.value
  if (!canvas) return
  const { width, height } = canvas.getBoundingClientRect()
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width
    canvas.height = height
  }
}

watch(() => voice.status.value, (s) => { targetHueShift = STATE_HUE[s] ?? 0 })

onMounted(async () => {
  await fetchAgent()
  fetchPersistedVoice()
  currentSprites = buildSprites(0)
  initParticles()
  resizeCanvas()
  rafHandle = requestAnimationFrame(renderFrame)
  window.addEventListener('resize', resizeCanvas)
})

onUnmounted(() => {
  if (rafHandle !== null) cancelAnimationFrame(rafHandle)
  if (flashTimer !== null) clearTimeout(flashTimer)
  stopPanelPoll()
  revokeAllImageBlobs()
  window.removeEventListener('resize', resizeCanvas)
  if (voice.isActive.value) voice.stop()
})

// ── Status display ────────────────────────────────────────────────────────────

const statusDotColor = computed(() => {
  switch (voice.status.value) {
    case 'connecting':   return 'rgba(200,150,65,0.7)'
    case 'listening':    return 'rgba(74,222,128,0.8)'
    case 'speaking':     return 'rgba(129,140,248,0.9)'
    case 'tool_calling': return 'rgba(245,158,11,0.9)'
    case 'error':        return 'rgba(239,68,68,0.8)'
    default:             return 'rgba(200,150,65,0.2)'
  }
})
const statusTextColor = computed(() => {
  switch (voice.status.value) {
    case 'connecting':   return 'rgba(255,210,130,0.5)'
    case 'listening':    return 'rgba(134,239,172,0.6)'
    case 'speaking':     return 'rgba(199,210,254,0.7)'
    case 'tool_calling': return 'rgba(253,211,77,0.7)'
    default:             return 'rgba(255,210,130,0.2)'
  }
})
const statusLabel = computed(() => {
  if (voice.muted.value && voice.isListening.value) return 'muted'
  switch (voice.status.value) {
    case 'connecting':   return 'connecting'
    case 'listening':    return 'listening'
    case 'speaking':     return 'speaking'
    case 'tool_calling': return 'working'
    case 'error':        return 'error'
    default:             return 'ready'
  }
})
</script>

<style scoped>
/* Canvas content cross-fade + subtle slide-in on update / history navigation.
   The `canvas-none` transition name (used under prefers-reduced-motion) has no
   matching classes, so Vue swaps content instantly. */
.canvas-fade-enter-active {
  transition: opacity 0.28s ease, transform 0.28s ease;
}
.canvas-fade-leave-active {
  transition: opacity 0.16s ease;
}
.canvas-fade-enter-from {
  opacity: 0;
  transform: translateY(8px);
}
.canvas-fade-leave-to {
  opacity: 0;
}

/* v-html'd panel content (mermaid SVG + sanitized agent HTML) needs :deep() to be
   reached by scoped styles. */
.mermaid-host :deep(svg) {
  max-width: 100%;
  height: auto;
}
.agent-html-panel :deep(table) {
  border-collapse: collapse;
}
.agent-html-panel :deep(th),
.agent-html-panel :deep(td) {
  padding: 4px 8px;
}
.agent-html-panel :deep(a) {
  color: #818cf8;
}
.agent-html-panel :deep(img),
.agent-html-panel :deep(canvas) {
  max-width: 100%;
}
</style>
