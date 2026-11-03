// The Workspace voice orb's particle field (ent#534), as one renderer with two faces:
// the full-column orb `VoiceOverlay.vue` draws for a Live call, and the small orb
// `VoiceOrb.vue` draws in a Direct call's strip (trinity-enterprise#1046). Both draw the
// same smoke and the same core; they differ only in how many particles they keep and
// what they draw it on. Everything here is canvas arithmetic — the palette is the orb's
// own art, not the UI token layer, which is why it lives in a module and not a template.
//
// A field is an instance, never module state: two orbs on one page (the strip's and the
// overlay's can never both be on, but a test can mount both) must not share particles.

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
    if (!ctx) return c

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

// Maps an orb state → hue rotation applied to the amber sprites. The vocabulary is
// the Live call's; a Direct call's statuses are mapped onto it by
// `portalDirectCall.directOrbState`.
export const STATE_HUE = Object.freeze({
  idle:         0,
  connecting:   0,
  listening:    90,    // amber → green
  speaking:     210,   // amber → indigo/blue
  tool_calling: 0,     // stay amber/orange
  error:        -30,   // red-ish
})

// The field is drawn on black: the sprites are additive-looking soft lights and read
// as smoke only against a dark ground. The overlay fills its whole box with this; the
// small orb fills its disc.
export const ORB_GROUND = '#000'

// ── Draw core ─────────────────────────────────────────────────────────────────

// The sphere's stops, as hue / saturation / brightness / alpha so the core can take
// the state colour (`tintCore`): the amber the overlay has always drawn is hue shift 0.
const CORE_STOPS = [
  [0,    48, 16, 100, 0.94],
  [0.10, 46, 35, 100, 0.88],
  [0.26, 41, 62,  97, 0.76],
  [0.42, 37, 76,  84, 0.54],
  [0.58, 31, 83,  69, 0.28],
  [0.72, 30, 85,  59, 0.10],
  [0.88, 28, 86,  51, 0.03],
  [1,    27, 87,  43, 0],
]
const GLOW_STOPS = [
  [0,    37, 79, 84, 0.13],
  [0.28, 34, 82, 75, 0.06],
  [0.6,  32, 84, 63, 0.02],
  [1,    29, 86, 51, 0],
]

function stopsInto(grad, stops, hueShift) {
  for (const [at, h, s, b, a] of stops) {
    const [r, g, bl] = hsbToRgb(((h + hueShift) % 360 + 360) % 360, s, b)
    grad.addColorStop(at, `rgba(${r},${g},${bl},${a})`)
  }
}

function drawCore(ctx, size, fc, hueShift = 0) {
  const R = size
  ctx.save()
  // Outer glow
  const glow = ctx.createRadialGradient(0,0,R*0.15, 0,0,R*5.8)
  stopsInto(glow, GLOW_STOPS, hueShift)
  ctx.fillStyle = glow
  ctx.beginPath(); ctx.arc(0,0,R*5.8,0,Math.PI*2); ctx.fill()

  // Main sphere
  const t = fc * 0.012
  const wA = noise(t)*0.08+0.96, wB = noise(t+50)*0.06+0.97
  const tilt = noise(t*0.4) * Math.PI * 0.35
  const sph = ctx.createRadialGradient(-R*0.26,-R*0.30, 0, 0,0, R*1.55)
  stopsInto(sph, CORE_STOPS, hueShift)
  ctx.fillStyle = sph
  ctx.beginPath(); ctx.ellipse(0,0, R*wA*1.55, R*wB*1.55, tilt, 0, Math.PI*2); ctx.fill()
  ctx.restore()
}

// ── The field ─────────────────────────────────────────────────────────────────

/**
 * One orb's smoke and core. `draw` renders a frame centred on the context's current
 * origin, in the field's own units (the core rests at radius ~70, the smoke fades out
 * by ~285): the caller sets the transform — a translate for the overlay, a translate
 * plus a scale for the small orb.
 *
 * @param {object} [opts]
 * @param {number} [opts.count] particles; 220 fills a column, 80 is plenty for a disc
 * @param {boolean} [opts.tintCore] the core takes the state hue too. The overlay keeps
 *   its amber core and lets the smoke carry the state; in a disc the smoke is a rim,
 *   so the core has to say it.
 */
export function createOrbField({ count = 220, tintCore = false } = {}) {
  let particles = []
  let sprites = null
  let hue = 0
  let target = 0
  let frame = 0

  function ensure() {
    if (sprites) return
    sprites = buildSprites(0)
    particles = []
    for (let i = 0; i < count; i++) particles.push(new Smoke(i, sprites))
  }

  function rebuild(nextHue) {
    sprites = buildSprites(nextHue)
    hue = nextHue
    for (const p of particles) p.sprites = sprites
  }

  return {
    // Eases toward the hue over the following frames (sprites rebuilt as it moves).
    setHueTarget(h) { target = Number.isFinite(h) ? h : 0 },
    /**
     * @param {CanvasRenderingContext2D} ctx
     * @param {object} [opts]
     * @param {number} [opts.energy] 0–1, the sound right now
     * @param {number} [opts.spread] multiplier on particle distance from the core
     * @param {number} [opts.size] multiplier on particle size
     * @param {number} [opts.brightness] multiplier on particle alpha
     * @param {number} [opts.core] multiplier on the core's radius
     */
    draw(ctx, { energy = 0, spread = 1.1, size = 1.1, brightness = 1.0, core = 1.0 } = {}) {
      ensure()
      frame++
      if (Math.abs(hue - target) > 1) {
        const next = Math.round(lerp(hue, target, 0.05))
        if (next !== hue) rebuild(next)
      }
      const e = Math.max(0, Math.min(1, energy))
      const bass = e * 0.8, mid = e * 0.5, high = e * 0.3
      for (const p of particles) {
        p.update(e, bass, mid, high, frame)
        p.draw(ctx, spread, size, brightness)
      }
      drawCore(ctx, (45 + e * 20) * core, frame, tintCore ? hue : 0)
    },
  }
}
