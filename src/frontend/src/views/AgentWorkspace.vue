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
            ? 'bg-green-900/50 text-green-400'
            : 'bg-gray-700 text-gray-400'
        ]"
      >{{ agent.status }}</span>

      <span class="flex-1" />

      <span class="px-1.5 py-0.5 text-[10px] font-bold rounded bg-amber-900/40 text-amber-400 border border-amber-700/50 tracking-wide flex-shrink-0">
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
              class="flex-1 text-xs bg-gray-900 border border-gray-700 rounded px-2 py-1 text-gray-300 focus:outline-none focus:border-indigo-500 disabled:opacity-40"
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
        <div class="flex-shrink-0 flex items-center justify-between px-5 py-3 border-b border-gray-800">
          <h2 class="text-sm font-medium text-gray-200">
            {{ panelState.title || 'Canvas' }}
          </h2>
          <span v-if="panelUpdatedAgo" class="text-xs text-gray-600">{{ panelUpdatedAgo }}</span>
        </div>

        <!-- Canvas content -->
        <div class="flex-1 overflow-auto p-6">
          <!-- Empty state -->
          <div v-if="!panelState.content" class="flex flex-col items-center justify-center h-full gap-4 text-center">
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
            v-else-if="panelState.type === 'markdown'"
            v-html="renderedContent"
            class="prose prose-sm prose-invert max-w-none
              prose-headings:text-gray-100 prose-headings:font-semibold
              prose-p:text-gray-300 prose-p:leading-relaxed
              prose-strong:text-gray-100
              prose-code:text-amber-300 prose-code:bg-gray-800 prose-code:px-1 prose-code:rounded prose-code:text-xs
              prose-pre:bg-gray-800 prose-pre:border prose-pre:border-gray-700
              prose-ul:text-gray-300 prose-ol:text-gray-300
              prose-li:marker:text-gray-500
              prose-blockquote:border-indigo-500 prose-blockquote:text-gray-400
              prose-a:text-indigo-400 hover:prose-a:text-indigo-300
              prose-hr:border-gray-700
              prose-table:text-sm
              prose-th:text-gray-200 prose-th:bg-gray-800
              prose-td:text-gray-300 prose-td:border-gray-700"
          />

          <!-- HTML content -->
          <div
            v-else-if="panelState.type === 'html'"
            ref="htmlPanelEl"
            class="text-gray-300 text-sm leading-relaxed"
          />
        </div>
      </div>

    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import { useVoiceSession } from '../composables/useVoiceSession'
import { renderMarkdown } from '../utils/markdown'
import DOMPurify from 'dompurify'
import AgentAvatar from '../components/AgentAvatar.vue'

const route = useRoute()
const authStore = useAuthStore()
// route.params.name is a plain string in Vue Router 4 setup()
const agentName = route.params.name
const voice = useVoiceSession(agentName)

const agent = ref(null)
const selectedVoice = ref('Kore')
const panelState = ref({ type: 'empty', content: '', title: null, updated_at: null })
const htmlPanelEl = ref(null)
let panelPollTimer = null
let panelFetching = false

const VOICES = [
  { id: 'Kore',    label: 'Kore — Firm' },
  { id: 'Zephyr',  label: 'Zephyr — Bright' },
  { id: 'Puck',    label: 'Puck — Upbeat' },
  { id: 'Aoede',   label: 'Aoede — Breezy' },
  { id: 'Charon',  label: 'Charon — Informational' },
  { id: 'Fenrir',  label: 'Fenrir — Excitable' },
]

// ── Computed ────────────────────────────────────────────────────────────────

const renderedContent = computed(() =>
  panelState.value.type === 'markdown' ? renderMarkdown(panelState.value.content || '') : ''
)

const panelUpdatedAgo = computed(() => {
  if (!panelState.value.updated_at) return null
  const secs = Math.round((Date.now() - new Date(panelState.value.updated_at).getTime()) / 1000)
  if (secs < 5)  return 'just now'
  if (secs < 60) return `${secs}s ago`
  return `${Math.round(secs / 60)}m ago`
})

// ── HTML panel rendering ─────────────────────────────────────────────────────

// Re-execute <script> tags after innerHTML assignment — v-html and innerHTML
// both skip script execution. We clone each script node as a live element so
// Chart.js initialisation code in update_panel HTML runs correctly.
// Chart.js 4 is pre-loaded globally (see injectChartJs), so agents must NOT
// add their own CDN <script src> tags.
function _execScripts(container) {
  Array.from(container.querySelectorAll('script')).forEach(old => {
    const s = document.createElement('script')
    Array.from(old.attributes).forEach(a => s.setAttribute(a.name, a.value))
    s.textContent = old.textContent
    old.replaceWith(s)
  })
}

function renderHtmlPanel(html) {
  const el = htmlPanelEl.value
  if (!el) return
  el.innerHTML = DOMPurify.sanitize(html, { ADD_TAGS: ['script'], ADD_ATTR: ['type'] })
  _execScripts(el)
}

// Re-render HTML panel when panelState content changes
watch(() => panelState.value, (state) => {
  if (state.type === 'html') nextTick(() => renderHtmlPanel(state.content || ''))
}, { deep: true })

// ── Session lifecycle ────────────────────────────────────────────────────────

function resetPanelState() {
  panelState.value = { type: 'empty', content: '', title: null, updated_at: null }
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
    // Only update state when content has actually changed — prevents 3x/sec
    // Vue re-renders and avoids overwriting preserved content with empty state
    // when the session ends and the backend returns the default empty response.
    if (r.data.updated_at !== panelState.value.updated_at) {
      panelState.value = r.data
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
  const energy = Math.min(1, amp * 2.5)
  const bass = energy * 0.8, mid = energy * 0.5, high = energy * 0.3
  ctx.fillStyle = '#000'
  ctx.fillRect(0, 0, W, H)
  ctx.save()
  ctx.translate(W/2, H/2)
  for (const p of particles) {
    p.update(energy, bass, mid, high, frameCount)
    p.draw(ctx, 1.1, 1.1, 1.0)
  }
  drawCore(ctx, 45 + energy * 20, frameCount)
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

function injectChartJs() {
  if (document.getElementById('chartjs-cdn')) return
  const s = document.createElement('script')
  s.id = 'chartjs-cdn'
  s.src = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js'
  document.head.appendChild(s)
}

onMounted(async () => {
  injectChartJs()
  await fetchAgent()
  currentSprites = buildSprites(0)
  initParticles()
  resizeCanvas()
  rafHandle = requestAnimationFrame(renderFrame)
  window.addEventListener('resize', resizeCanvas)
})

onUnmounted(() => {
  if (rafHandle !== null) cancelAnimationFrame(rafHandle)
  stopPanelPoll()
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
