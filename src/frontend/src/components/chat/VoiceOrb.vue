<!--
  The small orb (trinity-enterprise#1046): the Live call's smoke and core, in a disc
  the size of a button, for the Direct call's strip. One renderer, two faces — the
  overlay fills a column with `voiceOrbField.js`, this fills a circle with it.

  Lightweight on purpose: 80 particles, a bitmap no larger than the disc at a capped
  device-pixel ratio, and nothing drawn beyond the clip. Under
  `prefers-reduced-motion: reduce` it draws one still frame and redraws only when the
  state changes.
-->
<template>
  <canvas
    ref="el"
    class="block shrink-0 rounded-full"
    :style="{ width: `${size}px`, height: `${size}px` }"
    aria-hidden="true"
    data-testid="voice-orb"
  />
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { createOrbField, ORB_GROUND, STATE_HUE } from './voiceOrbField'

const props = defineProps({
  // One of the orb vocabulary: idle · connecting · listening · speaking · tool_calling · error.
  state: { type: String, default: 'idle' },
  // 0–1, the sound right now: the agent's voice while it speaks, the person's while it listens.
  amplitude: { type: Number, default: 0 },
  // Dimmed and still: a muted mic.
  muted: { type: Boolean, default: false },
  size: { type: Number, default: 48 },
})

// The field's units: the core rests at radius ~70 and the smoke fades out by ~285.
// The disc shows 150 of them each way, so the core fills most of it and the smoke is
// a halo at the rim; `spread` pulls the particles in to match.
const VIEW = 300
const DPR_CAP = 2

const el = ref(null)
let field = null
let raf = null
let ratio = 1

const stillOnly = () => (
  typeof window !== 'undefined' && typeof window.matchMedia === 'function'
  && !!window.matchMedia('(prefers-reduced-motion: reduce)')?.matches
)

function sizeBitmap() {
  const canvas = el.value
  if (!canvas) return
  ratio = Math.min(DPR_CAP, Math.max(1, (typeof window !== 'undefined' && window.devicePixelRatio) || 1))
  const px = Math.round(props.size * ratio)
  if (canvas.width !== px || canvas.height !== px) { canvas.width = px; canvas.height = px }
}

function drawFrame() {
  const canvas = el.value
  const ctx = canvas?.getContext?.('2d')
  if (!ctx) return false
  const s = props.size
  const r = s / 2
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0)
  ctx.clearRect(0, 0, s, s)
  ctx.save()
  ctx.beginPath(); ctx.arc(r, r, r, 0, Math.PI * 2); ctx.clip()
  ctx.fillStyle = ORB_GROUND
  ctx.fillRect(0, 0, s, s)
  ctx.translate(r, r)
  ctx.scale(s / VIEW, s / VIEW)
  const energy = props.muted ? 0 : Math.min(1, props.amplitude * 2.5)
  field.draw(ctx, {
    // The smoke is a rim here, so it is drawn brighter and larger than in the column
    // or it reads as a plain glowing ball.
    energy, spread: 0.8, size: 1.3, core: 1.6,
    brightness: props.muted ? 0.6 : 3.0,
  })
  ctx.restore()
  return true
}

function loop() {
  raf = null
  if (!drawFrame()) return
  raf = requestAnimationFrame(loop)
}

function start() {
  if (!field) {
    field = createOrbField({ count: 80, tintCore: true })
    field.setHueTarget(STATE_HUE[props.state] ?? 0)
  }
  sizeBitmap()
  // The first frame now, not a frame later: the strip must not open on a blank disc.
  if (!drawFrame() || stillOnly()) return
  if (raf === null && typeof requestAnimationFrame === 'function') raf = requestAnimationFrame(loop)
}

function stop() {
  if (raf !== null) { cancelAnimationFrame(raf); raf = null }
}

watch(() => props.state, (s) => {
  field?.setHueTarget(STATE_HUE[s] ?? 0)
  if (stillOnly()) drawFrame()
})
watch(() => props.muted, () => { if (stillOnly()) drawFrame() })
watch(() => props.size, () => { sizeBitmap(); if (stillOnly()) drawFrame() })

onMounted(start)
onBeforeUnmount(stop)
</script>
