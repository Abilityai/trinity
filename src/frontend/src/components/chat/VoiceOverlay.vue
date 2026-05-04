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

      <!-- Status text -->
      <div class="absolute bottom-16 left-1/2 -translate-x-1/2 z-10 flex items-center gap-2">
        <div
          class="w-1.5 h-1.5 rounded-full transition-colors duration-300"
          :style="{ background: statusDotColor }"
        />
        <span class="text-xs tracking-widest uppercase" :style="{ color: statusTextColor }">
          {{ statusLabel }}
        </span>
      </div>

      <!-- Controls -->
      <div class="absolute bottom-5 left-1/2 -translate-x-1/2 z-10 flex items-center gap-5">
        <!-- Mute -->
        <button
          @click="voice.toggleMute()"
          class="w-10 h-10 rounded-full flex items-center justify-center transition-colors"
          :style="voice.muted.value
            ? 'background: rgba(217,119,6,0.35); border: 1px solid rgba(217,119,6,0.5);'
            : 'background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.15);'"
          :title="voice.muted.value ? 'Unmute' : 'Mute'"
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

const props = defineProps({
  voice: { type: Object, required: true }
})
defineEmits(['end'])

const canvasEl = ref(null)
let rafHandle = null
let frameCount = 0

// ── Noise (simple value noise, smooth enough for smoke) ──────────────────────

function _hash(n) {
  return Math.abs(Math.sin(n * 127.1 + 311.7) * 43758.5453) % 1
}

function noise(x, y = 0, z = 0) {
  const ix = Math.floor(x), iy = Math.floor(y), iz = Math.floor(z)
  const fx = x - ix, fy = y - iy, fz = z - iz
  const ux = fx * fx * (3 - 2 * fx)
  const uy = fy * fy * (3 - 2 * fy)
  const uz = fz * fz * (3 - 2 * fz)
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

// ── Sprite builder (offscreen radial-gradient circles) ───────────────────────

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

    // Shift hue for state colour
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

// ── Particle system ───────────────────────────────────────────────────────────

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

// ── Orb state ─────────────────────────────────────────────────────────────────

// Maps voice status → hue rotation applied to the amber sprites
const STATE_HUE = {
  idle:         0,
  connecting:   0,
  listening:    90,    // amber → green
  speaking:     210,   // amber → indigo/blue
  tool_calling: 0,     // stay amber/orange
  error:        -30,   // red-ish
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

function initParticles(sprites, count = 220) {
  particles = []
  for (let i = 0; i < count; i++) particles.push(new Smoke(i, sprites))
}

// ── Draw core ─────────────────────────────────────────────────────────────────

function drawCore(ctx, size, fc) {
  const R = size
  ctx.save()
  // Outer glow
  const glow = ctx.createRadialGradient(0,0,R*0.15, 0,0,R*5.8)
  glow.addColorStop(0,    `rgba(215,148,45,0.13)`)
  glow.addColorStop(0.28, `rgba(190,122,35,0.06)`)
  glow.addColorStop(0.6,  `rgba(160,98,25,0.02)`)
  glow.addColorStop(1,    `rgba(130,72,18,0)`)
  ctx.fillStyle = glow
  ctx.beginPath(); ctx.arc(0,0,R*5.8,0,Math.PI*2); ctx.fill()

  // Main sphere
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

// ── Main render loop ───────────────────────────────────────────────────────────

function renderFrame() {
  const canvas = canvasEl.value
  if (!canvas) return

  const W = canvas.width, H = canvas.height
  const ctx = canvas.getContext('2d')
  const cx = W/2, cy = H/2
  frameCount++

  // Lerp hue shift
  if (Math.abs(currentHueShift - targetHueShift) > 1) {
    const next = Math.round(lerp(currentHueShift, targetHueShift, 0.05))
    if (next !== currentHueShift) rebuildSprites(next)
  }

  // Amplitude → energy
  const amp = props.voice.amplitude?.value ?? 0
  const energy = Math.min(1, amp * 2.5)
  const bass = energy * 0.8
  const mid  = energy * 0.5
  const high = energy * 0.3

  ctx.fillStyle = '#000'
  ctx.fillRect(0, 0, W, H)

  ctx.save()
  ctx.translate(cx, cy)

  for (const p of particles) {
    p.update(energy, bass, mid, high, frameCount)
    p.draw(ctx, 1.1, 1.1, 1.0)
  }

  // Pulse core size with amplitude
  const coreSize = 45 + energy * 20
  drawCore(ctx, coreSize, frameCount)

  ctx.restore()

  rafHandle = requestAnimationFrame(renderFrame)
}

// ── Lifecycle ─────────────────────────────────────────────────────────────────

function resizeCanvas() {
  const canvas = canvasEl.value
  if (!canvas) return
  const { width, height } = canvas.getBoundingClientRect()
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width
    canvas.height = height
  }
}

function startLoop() {
  if (!currentSprites) {
    currentSprites = buildSprites(0)
    initParticles(currentSprites)
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
    startLoop()
  } else {
    stopLoop()
  }
})

onUnmounted(() => {
  stopLoop()
})

// Update hue target when status changes
watch(() => props.voice.status.value, (s) => {
  targetHueShift = STATE_HUE[s] ?? 0
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
