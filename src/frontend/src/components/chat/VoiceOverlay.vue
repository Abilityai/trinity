<template>
  <Transition
    enter-active-class="transition ease-out duration-300"
    enter-from-class="opacity-0"
    enter-to-class="opacity-100"
    leave-active-class="transition ease-in duration-200"
    leave-from-class="opacity-100"
    leave-to-class="opacity-0"
  >
    <div
      v-if="voice.isActive.value"
      class="absolute inset-0 z-30 flex flex-col items-center justify-center overflow-hidden rounded-lg"
      style="background: #000"
    >
      <!-- Canvas orb -->
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
          class="absolute top-6 left-1/2 -translate-x-1/2 z-10 px-3 py-1 rounded-full text-xs font-medium tracking-widest uppercase"
          style="background: rgba(245,158,11,0.18); border: 1px solid rgba(245,158,11,0.35); color: rgba(253,211,77,0.9);"
        >
          {{ voice.toolName.value ? voice.toolName.value.replace(/_/g, ' ') : 'working…' }}
        </div>
      </Transition>

      <!-- ent#551: tasks running in the background. Persists across turns,
           unlike the amber badge above, which is one tool call's moment; a
           different hue so "the agent is thinking" and "the agent has work in
           flight" never read as the same thing. -->
      <Transition
        enter-active-class="transition ease-out duration-200"
        enter-from-class="opacity-0 translate-y-2"
        enter-to-class="opacity-100 translate-y-0"
        leave-active-class="transition ease-in duration-150"
        leave-from-class="opacity-100 translate-y-0"
        leave-to-class="opacity-0 translate-y-2"
      >
        <div
          v-if="voice.hasBackgroundTasks?.value"
          class="absolute top-14 left-1/2 -translate-x-1/2 z-10 max-w-[85%] flex flex-col items-center gap-1.5"
          data-testid="voice-background-tasks"
        >
          <!-- One pill per task, never bunched: each names what it is doing and
               whether it is queued behind another (one turn per thread). -->
          <div
            v-for="t in voice.backgroundTasks.value"
            :key="t.taskId"
            class="max-w-full truncate px-3 py-1 rounded-full text-xs font-medium bg-status-info-500/15 border border-status-info-400/40"
            :class="t.status === 'queued' ? 'text-status-info-300/70' : 'text-status-info-200'"
            :title="t.label"
            data-testid="voice-background-task"
          >
            {{ taskItemLabel(t) }}
          </div>
        </div>
      </Transition>

      <!-- Status text + controls: ONE bottom-anchored column, so the two can
           never overlap however short the stage is. They used to be two
           absolutely positioned rows (status 4 rem up, controls 1.25 rem up
           with 48px buttons), and on a short stage the word LISTENING sat on
           top of the buttons. -->
      <div class="absolute bottom-5 left-1/2 -translate-x-1/2 z-10 flex flex-col items-center gap-3" data-testid="voice-bottom-stack">
        <div class="flex items-center gap-2">
          <div
            class="w-1.5 h-1.5 rounded-full transition-colors duration-300"
            :style="{ background: statusDotColor }"
          />
          <span class="text-xs tracking-widest uppercase" :style="{ color: statusTextColor }">
            {{ statusLabel }}
          </span>
        </div>

      <!-- Controls -->
      <div class="flex items-center gap-5">
        <!-- Mute (also the M key while the call is on) -->
        <button
          @click="voice.toggleMute()"
          class="w-10 h-10 rounded-full flex items-center justify-center transition-colors"
          :style="voice.muted.value
            ? 'background: rgba(217,119,6,0.35); border: 1px solid rgba(217,119,6,0.5);'
            : 'background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.15);'"
          :title="voice.muted.value ? 'Unmute (M)' : 'Mute (M)'"
          :aria-pressed="voice.muted.value ? 'true' : 'false'"
        >
          <svg v-if="!voice.muted.value" class="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4M12 15a3 3 0 003-3V5a3 3 0 00-6 0v7a3 3 0 003 3z" />
          </svg>
          <svg v-else class="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z" />
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2" />
          </svg>
        </button>

        <!-- End call -->
        <button
          @click="$emit('end')"
          class="w-12 h-12 rounded-full flex items-center justify-center transition-colors shadow-lg"
          style="background: rgba(185,28,28,0.7); border: 1px solid rgba(220,38,38,0.5);"
          title="End voice session"
        >
          <svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 8l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2M5 3a2 2 0 00-2 2v1c0 8.284 6.716 15 15 15h1a2 2 0 002-2v-3.28a1 1 0 00-.684-.948l-4.493-1.498a1 1 0 00-1.21.502l-1.13 2.257a11.042 11.042 0 01-5.516-5.517l2.257-1.128a1 1 0 00.502-1.21L9.228 3.683A1 1 0 008.279 3H5z" />
          </svg>
        </button>
      </div>
      </div>

      <!-- Error -->
      <div
        v-if="voice.error.value"
        class="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 z-10 px-4 py-2 rounded-lg text-sm"
        style="background: rgba(127,29,29,0.6); border: 1px solid rgba(239,68,68,0.4); color: rgba(252,165,165,0.9);"
      >
        {{ voice.error.value }}
      </div>
    </div>
  </Transition>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { taskItemLabel } from '../portal/portalVoiceMode'
import { createOrbField, ORB_GROUND, STATE_HUE } from './voiceOrbField'

const props = defineProps({
  voice: { type: Object, required: true }
})
defineEmits(['end'])

const canvasEl = ref(null)
let rafHandle = null
// #2640: the canvas's CSS box, in CSS pixels, and the device-pixel ratio the
// bitmap was last sized for. The render loop works in CSS pixels and lets the
// context scale, so the particle field — which lives in a fixed coordinate
// space around (0,0) and is seeded ONCE — is untouched by a resize. Re-seeding
// on resize would restart the orb every time the column moves.
let cssWidth = 0
let cssHeight = 0
let pixelRatio = 1
let resizeObserver = null

// ── The field ─────────────────────────────────────────────────────────────────

// The smoke and the core live in `voiceOrbField.js`, shared with the Direct
// call's small orb (trinity-enterprise#1046). This component owns the column:
// the black ground, the bitmap's size, and the loop.
let field = null

// ── Main render loop ───────────────────────────────────────────────────────────

function renderFrame() {
  const canvas = canvasEl.value
  if (!canvas) return

  // #2640: CSS pixels, not bitmap pixels. `resizeCanvas` sizes the bitmap at
  // `css * devicePixelRatio` so the orb is not blurry on a HiDPI screen; the
  // transform below puts the drawing back into CSS-pixel units, so every
  // literal in the field (the 45px core, the particles' fixed radii) keeps
  // meaning what it meant before DPR scaling existed.
  const W = cssWidth, H = cssHeight
  if (W <= 0 || H <= 0) { rafHandle = requestAnimationFrame(renderFrame); return }
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  ctx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0)

  // Amplitude → energy
  const amp = props.voice.amplitude?.value ?? 0
  const energy = Math.min(1, amp * 2.5)

  ctx.fillStyle = ORB_GROUND
  ctx.fillRect(0, 0, W, H)

  ctx.save()
  ctx.translate(W/2, H/2)
  field.draw(ctx, { energy })
  ctx.restore()

  rafHandle = requestAnimationFrame(renderFrame)
}

// ── Lifecycle ─────────────────────────────────────────────────────────────────

// #2640: the bitmap must FOLLOW the box. This used to run once, from the
// `watch(canvasEl)` below, and nothing ever called it again — no
// ResizeObserver, no window listener, no per-frame check. The canvas is
// `absolute inset-0 w-full h-full`, so any later change to the column's width
// (starting or ending a call, dragging the rail, resizing the window, crossing
// the `sm` breakpoint) left CSS stretching a stale bitmap. That is the
// reported squashed orb: an ellipse because the bitmap's aspect ratio no
// longer matched its box's.
//
// `DPR_CAP`: a 3x display would otherwise quadruple the fill cost of a
// full-column particle field for detail nobody can see at this blur radius.
const DPR_CAP = 2

function resizeCanvas() {
  const canvas = canvasEl.value
  if (!canvas) return
  const { width, height } = canvas.getBoundingClientRect()
  // A hidden or not-yet-laid-out canvas measures 0. Sizing a bitmap to 0 throws
  // away the last good size and draws nothing, so leave it alone and let the
  // observer fire again when it has a box.
  if (width <= 0 || height <= 0) return
  const ratio = Math.min(
    DPR_CAP,
    Math.max(1, (typeof window !== 'undefined' && window.devicePixelRatio) || 1),
  )
  const nextW = Math.round(width * ratio)
  const nextH = Math.round(height * ratio)
  cssWidth = width
  cssHeight = height
  pixelRatio = ratio
  // Assigning to `canvas.width`/`height` CLEARS the canvas and resets its
  // context state, so it is guarded on an actual change — an observer that
  // fires on every scroll-induced sub-pixel reflow would otherwise blank the
  // orb continuously.
  if (canvas.width !== nextW || canvas.height !== nextH) {
    canvas.width = nextW
    canvas.height = nextH
  }
}

// Both, deliberately. `ResizeObserver` catches the box moving under a stable
// window — the call's column swap, a rail drag, a flex reflow — which a window
// listener never sees. The window listener catches a `devicePixelRatio` change
// (dragging the window to a different-density monitor), which resizes no box
// at all and so fires no observer.
function observeCanvas(canvas) {
  unobserveCanvas()
  if (typeof ResizeObserver === 'function') {
    resizeObserver = new ResizeObserver(() => resizeCanvas())
    resizeObserver.observe(canvas)
  }
  if (typeof window !== 'undefined') window.addEventListener('resize', resizeCanvas)
}

function unobserveCanvas() {
  if (resizeObserver) {
    resizeObserver.disconnect()
    resizeObserver = null
  }
  if (typeof window !== 'undefined') window.removeEventListener('resize', resizeCanvas)
}

function startLoop() {
  if (!field) {
    field = createOrbField({ count: 220 })
    field.setHueTarget(STATE_HUE[props.voice.status.value] ?? 0)
  }
  if (rafHandle === null) {
    rafHandle = requestAnimationFrame(renderFrame)
  }
}

function stopLoop() {
  if (rafHandle !== null) {
    cancelAnimationFrame(rafHandle)
    rafHandle = null
  }
}

// The canvas is inside v-if="voice.isActive.value", so canvasEl.value is null
// until the voice session starts. Watch it to start/stop the RAF loop.
watch(canvasEl, (canvas) => {
  if (canvas) {
    resizeCanvas()
    // #2640: the overlay mounts in the same tick the call re-lays out the
    // columns, so this first measurement can capture the PRE-call width even
    // with nothing resizing afterwards. The observer fires on the layout that
    // follows and corrects it, which is why the one-shot measurement above is
    // no longer the only one.
    observeCanvas(canvas)
    startLoop()
  } else {
    unobserveCanvas()
    stopLoop()
  }
})

onUnmounted(() => {
  unobserveCanvas()
  stopLoop()
})

// Update hue target when status changes
watch(() => props.voice.status.value, (s) => {
  field?.setHueTarget(STATE_HUE[s] ?? 0)
})

// ── Status display helpers ─────────────────────────────────────────────────────

const statusDotColor = computed(() => {
  switch (props.voice.status.value) {
    case 'connecting':   return 'rgba(200,150,65,0.7)'
    case 'listening':    return 'rgba(74,222,128,0.8)'
    case 'speaking':     return 'rgba(129,140,248,0.9)'
    case 'tool_calling': return 'rgba(245,158,11,0.9)'
    case 'error':        return 'rgba(239,68,68,0.8)'
    default:             return 'rgba(200,150,65,0.3)'
  }
})

const statusTextColor = computed(() => {
  switch (props.voice.status.value) {
    case 'connecting':   return 'rgba(255,210,130,0.5)'
    case 'listening':    return 'rgba(134,239,172,0.6)'
    case 'speaking':     return 'rgba(199,210,254,0.7)'
    case 'tool_calling': return 'rgba(253,211,77,0.7)'
    default:             return 'rgba(255,210,130,0.3)'
  }
})

const statusLabel = computed(() => {
  if (props.voice.muted.value && props.voice.isListening.value) return 'muted'
  switch (props.voice.status.value) {
    case 'connecting':   return 'connecting'
    case 'listening':    return 'listening'
    case 'speaking':     return 'speaking'
    case 'tool_calling': return 'working'
    case 'error':        return 'error'
    default:             return ''
  }
})
</script>
