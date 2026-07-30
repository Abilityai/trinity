<template>
  <div
    ref="canvasEl"
    class="fleet-canvas"
    :class="{ panning: isPanning }"
    :style="canvasStyle"
    @pointerdown="onCanvasPointerDown"
    @pointermove="onCanvasPointerMove"
    @pointerup="endPan"
    @pointercancel="endPan"
  >
    <div class="gv-world" :style="worldStyle">
      <!-- PROTOTYPE org overlay — derived department zones (hull model):
           frames follow wherever the member tiles sit; never a constraint. -->
      <template v-if="showZones">
        <div
          v-for="z in zones"
          :key="'z' + z.dept"
          class="gv-zone"
          :class="{
            hot: dropZone === z.dept,
            blockdragging: zoneDrag && zoneDrag.dept === z.dept,
            invalid: zoneDrag && zoneDrag.dept === z.dept && !zoneDrag.valid,
          }"
          :style="zoneStyle(z)"
        >
          <div
            class="gv-zonehead"
            title="Drag to move the whole department"
            @pointerdown.stop.prevent="startZoneDrag(z, $event)"
          >
            <span class="zname">{{ z.dept }}</span>
            <span class="zstat">{{ z.count }} agent{{ z.count === 1 ? '' : 's' }} · {{ z.running }} running</span>
          </div>
        </div>
      </template>

      <!-- PROTOTYPE org overlay — reporting lines (manager → report) -->
      <svg
        v-if="edgeLayer"
        class="gv-wires"
        :style="{ left: edgeLayer.x + 'px', top: edgeLayer.y + 'px' }"
        :width="edgeLayer.w"
        :height="edgeLayer.h"
        :viewBox="`0 0 ${edgeLayer.w} ${edgeLayer.h}`"
      >
        <g :transform="`translate(${-edgeLayer.x},${-edgeLayer.y})`">
          <g v-for="e in edgeLayer.edges" :key="e.id" class="gv-wire" :class="edgeClass(e)">
            <path class="gv-wire-hit" :d="e.d" @click.stop="removeEdge(e)">
              <title>{{ e.report }} reports to {{ e.manager }} — click to remove</title>
            </path>
            <path class="gv-wire-line" :d="e.d"></path>
            <path class="gv-wire-ah" :d="e.ah"></path>
          </g>
          <template v-if="connectDraft">
            <path class="gv-wire-line adding" :d="connectDraft.d"></path>
            <path class="gv-wire-ah adding" :d="connectDraft.ah"></path>
          </template>
        </g>
      </svg>

      <div
        class="gv-grid"
        :class="{ showcells: isDragging }"
        role="list"
        aria-label="Agent tiles — drag to any cell on the canvas"
        @pointerdown="onTilePointerDown"
        @pointermove="onTilePointerMove"
        @pointerup="onTilePointerUp"
        @pointercancel="onTilePointerUp"
        @keydown="onTileKeydown"
      >
        <!-- Free-cell shading across the viewport while dragging -->
        <div
          v-for="mark in cellMarks"
          :key="'m' + mark.key"
          class="gv-cellmark"
          :style="{ width: CELL_W + 'px', height: CELL_H + 'px', transform: `translate(${mark.x}px,${mark.y}px)` }"
        ></div>

        <!-- Target-cell socket (dashed when the drop would swap) -->
        <div
          class="gv-socket"
          :class="{ show: isDragging, swap: socketSwap }"
          :style="{ width: CELL_W + 'px', height: CELL_H + 'px', transform: `translate(${socketX}px,${socketY}px)` }"
        ></div>

        <!-- Block-move target sockets: one per member tile of the dragged
             zone; red when any target cell is taken by a non-member -->
        <div
          v-for="s in zoneSockets"
          :key="'zs' + s.key"
          class="gv-socket show"
          :class="{ invalid: zoneDrag && !zoneDrag.valid }"
          :style="{ width: CELL_W + 'px', height: CELL_H + 'px', transform: `translate(${s.x}px,${s.y}px)` }"
        ></div>

        <!-- Tiles -->
        <div
          v-for="agent in placedAgents"
          :key="agent.name"
          class="gv-tile"
          :class="{
            system: agent.is_system,
            dragging: draggingName === agent.name,
            snapping: snappingName === agent.name,
            locked: lockedName === agent.name,
            linktarget: connectTarget === agent.name,
            blockdrag: zoneDrag && zoneDrag.memberSet.has(agent.name),
          }"
          :style="tileStyle(agent.name)"
          :data-agent="agent.name"
          role="listitem"
          tabindex="0"
          :aria-label="agent.name + ' — drag to any cell, or use arrow keys'"
          @transitionend="onTileTransitionEnd(agent.name, $event)"
          @pointerenter="onTileEnter(agent.name)"
          @pointerleave="onTileLeave(agent.name)"
        >
          <!-- Viewport-aware hydration: far-away tiles render a light
               placeholder and fetch nothing (#47 perf requirement). -->
          <AgentTile
            v-if="visibleNames.has(agent.name)"
            :agent="agent"
            :now="now"
            :dept="deptByAgent[agent.name] || null"
          />
          <div v-else class="gv-tile-far">{{ agent.name }}</div>
          <!-- PROTOTYPE org overlay: bottom connector — drag DOWN onto
               another agent to make it report to this one (org-chart
               direction: manager above, report below) -->
          <span
            v-if="showLines && visibleNames.has(agent.name)"
            class="gv-handle nodrag"
            title="Drag down onto an agent — it will report to this one"
            @pointerdown.stop.prevent="startConnect(agent.name, $event)"
          ></span>
        </div>
      </div>
    </div>

    <!-- Zoom controls (bottom-left, Vue Flow-style) -->
    <div class="gv-zoomctl">
      <button type="button" title="Zoom in" aria-label="Zoom in" @click="zoomStep(1.2)">
        <svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"></path></svg>
      </button>
      <button type="button" title="Zoom out" aria-label="Zoom out" @click="zoomStep(1 / 1.2)">
        <svg viewBox="0 0 24 24"><path d="M5 12h14"></path></svg>
      </button>
      <button type="button" title="Fit view" aria-label="Fit view" @click="fitView">
        <svg viewBox="0 0 24 24"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"></path></svg>
      </button>
    </div>
    <span class="gv-zoomlvl">{{ Math.round(vz * 100) }}%</span>

    <!-- PROTOTYPE org overlay controls -->
    <div class="gv-orgctl">
      <button type="button" :class="{ on: showZones }" title="Show department zones (dept-* tags; plain tags as fallback)" @click="toggleZones">Zones</button>
      <button type="button" :class="{ on: showLines }" title="Show reporting lines (reports-to-* tags)" @click="toggleLines">Lines</button>
      <button
        type="button"
        class="act"
        title="One-shot arrange into department blocks — tiles stay fully hand-editable after"
        @click="arrangeNow"
      >Group by dept</button>
    </div>

    <!-- Board-level legend: the activity chart's trigger colors, once -->
    <div class="gv-legend" title="Execution trigger types (14-day activity chart)">
      <span><i class="ls"></i>Scheduled</span>
      <span><i class="lm"></i>Manual · MCP</span>
      <span><i class="le"></i>External</span>
      <span v-if="showLines" class="lrep" title="Reporting line (manager → report)">
        <svg viewBox="0 0 22 8" width="22" height="8" aria-hidden="true"><path d="M0 4 H14" stroke="currentColor" stroke-width="1.5" fill="none"></path><path d="M13 0.8 L21 4 L13 7.2 Z" fill="currentColor"></path></svg>
        Reporting
      </span>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import AgentTile from './AgentTile.vue'
import { useFleetGridStore } from '@/stores/fleetGrid'
import {
  CELL_W,
  CELL_H,
  GAP_X,
  GAP_Y,
  Z_MIN,
  Z_MAX,
  COORD_LIMIT,
  cellXY,
  cellFromCenter,
  layoutBBox,
  occupantAt,
} from '@/utils/gridLayout'
import {
  deptOf,
  deptInfoByAgent,
  computeZones,
  computeEdges,
  zoneAt,
  arrangeByDept,
} from '@/utils/gridOrg'

/**
 * FleetGrid (trinity-enterprise#47) — the Grid dashboard mode's magnetic
 * tile canvas: an unbounded pan/zoom lattice with iPhone-style tile drag,
 * live snap preview, swap-with-preview, tidy/reset, and keyboard reorder.
 * Interaction constants and physics come verbatim from the approved design
 * of record. No Vue Flow dependency in this mode.
 */
const props = defineProps({
  agents: { type: Array, required: true },
})

const gridStore = useFleetGridStore()

const DOT_GAP = 22
const reducedMotion =
  typeof window !== 'undefined' &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches

// --- view state (world transform) ---
const canvasEl = ref(null)
const vz = ref(1)
const vtx = ref(0)
const vty = ref(0)
const canvasW = ref(0)
const canvasH = ref(0)

const worldStyle = computed(() => ({
  transform: `translate(${vtx.value}px,${vty.value}px) scale(${vz.value})`,
}))

// The dotted background lives in world space: dots scale + translate with
// the view (same canvas language as the graph view).
const canvasStyle = computed(() => ({
  backgroundSize: `${DOT_GAP * vz.value}px ${DOT_GAP * vz.value}px`,
  backgroundPosition: `${vtx.value}px ${vty.value}px`,
}))

function zoomAt(px, py, nz) {
  nz = Math.max(Z_MIN, Math.min(Z_MAX, nz))
  const wx = (px - vtx.value) / vz.value
  const wy = (py - vty.value) / vz.value
  vz.value = nz
  vtx.value = px - wx * nz
  vty.value = py - wy * nz
}

function zoomStep(factor) {
  zoomAt(canvasW.value / 2, canvasH.value / 2, vz.value * factor)
}

function onWheel(e) {
  e.preventDefault()
  const rect = canvasEl.value.getBoundingClientRect()
  const factor = Math.exp(-e.deltaY * 0.0018)
  zoomAt(e.clientX - rect.left, e.clientY - rect.top, vz.value * factor)
}

function fitView() {
  const b = layoutBBox(layout.value)
  // Include the half-out avatar overhang on the left edge.
  b.x -= 28
  b.w += 28
  const vw = canvasW.value
  const vh = canvasH.value
  if (!vw || !vh) return
  const pad = 56
  vz.value = Math.max(0.15, Math.min((vw - pad) / b.w, (vh - pad) / b.h, 1.1))
  vtx.value = (vw - b.w * vz.value) / 2 - b.x * vz.value
  vty.value = (vh - b.h * vz.value) / 2 - b.y * vz.value
}

// --- layout ---
const layout = computed(() => gridStore.layout)

const placedAgents = computed(() =>
  props.agents.filter((a) => layout.value[a.name])
)

function syncLayoutFromAgents() {
  const names = props.agents.map((a) => a.name)
  const systemNames = new Set(props.agents.filter((a) => a.is_system).map((a) => a.name))
  gridStore.syncLayout(names, systemNames)
}

// Sync immediately (setup, before first render) so the initial paint already
// has positioned tiles, then re-sync whenever the fleet roster changes.
syncLayoutFromAgents()
watch(
  () => props.agents.map((a) => a.name).join('\n'),
  () => {
    syncLayoutFromAgents()
    // The dragged agent can vanish mid-drag (deleted, or filtered out by a
    // roster refresh) — its tile unmounts and pointerup never reaches us, so
    // drop the drag state instead of leaving the socket/shading stuck on.
    if (draggingName.value && !props.agents.some((a) => a.name === draggingName.value)) {
      cancelDrag()
    }
  }
)

// --- viewport culling (#47: render/hydrate only tiles near the viewport) ---
const visibleNames = computed(() => {
  const set = new Set()
  const vw = canvasW.value
  const vh = canvasH.value
  if (!vw || !vh) return set
  const x0 = -vtx.value / vz.value
  const y0 = -vty.value / vz.value
  const w = vw / vz.value
  const h = vh / vz.value
  const mx = w * 0.6
  const my = h * 0.6
  for (const agent of placedAgents.value) {
    const p = layout.value[agent.name]
    const [x, y] = cellXY(p.c, p.r)
    if (x + CELL_W >= x0 - mx && x <= x0 + w + mx && y + CELL_H >= y0 - my && y <= y0 + h + my) {
      set.add(agent.name)
    }
  }
  if (draggingName.value) set.add(draggingName.value)
  return set
})

// --- PROTOTYPE org overlay: zones + reporting lines ---
const ORG_KEY = 'trinity-grid-org-v1'
let _orgSaved = {}
try {
  _orgSaved = JSON.parse(localStorage.getItem(ORG_KEY)) || {}
} catch {
  _orgSaved = {}
}
const showZones = ref(_orgSaved.zones !== false)
const showLines = ref(_orgSaved.lines !== false)

function _persistOrg() {
  try {
    localStorage.setItem(
      ORG_KEY,
      JSON.stringify({ zones: showZones.value, lines: showLines.value })
    )
  } catch {
    /* private mode */
  }
}
function toggleZones() {
  showZones.value = !showZones.value
  _persistOrg()
}
function toggleLines() {
  showLines.value = !showLines.value
  _persistOrg()
}

const hoverAgent = ref(null)
const connectTarget = ref(null)
const connecting = ref(null) // { source, sx, sy, x, y } in world coords
const dropZone = ref(null) // dept name of the zone the dragged tile hovers

const deptByAgent = computed(() => deptInfoByAgent(props.agents))
const zones = computed(() =>
  showZones.value ? computeZones(layout.value, props.agents) : []
)

// World-space SVG viewport for the wires: layout bbox + margin, stretched to
// include the in-progress connect cursor so the dashed draft never clips.
const edgeLayer = computed(() => {
  if (!showLines.value) return null
  const edges = computeEdges(layout.value, props.agents)
  if (edges.length === 0 && !connecting.value) return null
  const b = layoutBBox(layout.value)
  const M = 300
  let x = b.x - M
  let y = b.y - M
  let x2 = b.x + b.w + M
  let y2 = b.y + b.h + M
  const c = connecting.value
  if (c) {
    x = Math.min(x, c.x - 40)
    y = Math.min(y, c.y - 40)
    x2 = Math.max(x2, c.x + 40)
    y2 = Math.max(y2, c.y + 40)
  }
  return { x, y, w: x2 - x, h: y2 - y, edges }
})

const connectDraft = computed(() => {
  const c = connecting.value
  if (!c) return null
  // Vertical draft curve from the bottom connector; arrowhead points along
  // the drag direction (down in the normal manager-above case).
  const s = c.y >= c.sy ? 1 : -1
  const k = Math.min(120, Math.max(30, Math.abs(c.y - c.sy) * 0.4))
  return {
    d: `M ${c.sx} ${c.sy} C ${c.sx} ${c.sy + s * k}, ${c.x} ${c.y - s * k}, ${c.x} ${c.y}`,
    ah: `M ${c.x} ${c.y} L ${c.x - 4} ${c.y - s * 8} L ${c.x + 4} ${c.y - s * 8} Z`,
  }
})

function edgeClass(e) {
  if (!hoverAgent.value) return ''
  return e.manager === hoverAgent.value || e.report === hoverAgent.value ? 'hot' : 'dim'
}

function onTileEnter(name) {
  if (!draggingName.value && !connecting.value) hoverAgent.value = name
}
function onTileLeave(name) {
  if (hoverAgent.value === name) hoverAgent.value = null
}

function _worldPoint(e) {
  const rect = canvasEl.value.getBoundingClientRect()
  return [
    (e.clientX - rect.left - vtx.value) / vz.value,
    (e.clientY - rect.top - vty.value) / vz.value,
  ]
}

function startConnect(name, e) {
  const p = layout.value[name]
  if (!p || !canvasEl.value) return
  const [x, y] = cellXY(p.c, p.r)
  const [wx, wy] = _worldPoint(e)
  // Anchor at the tile's bottom-center: lines are drawn downward from the
  // manager to the report (org-chart direction).
  connecting.value = { source: name, sx: x + CELL_W / 2, sy: y + CELL_H, x: wx, y: wy }
  hoverAgent.value = null
  window.addEventListener('pointermove', onConnectMove)
  window.addEventListener('pointerup', onConnectUp, { once: true })
}

function onConnectMove(e) {
  if (!connecting.value || !canvasEl.value) return
  const [wx, wy] = _worldPoint(e)
  connecting.value = { ...connecting.value, x: wx, y: wy }
  const el = document.elementFromPoint(e.clientX, e.clientY)
  const tile = el && el.closest ? el.closest('.gv-tile') : null
  const name = tile ? tile.dataset.agent : null
  connectTarget.value = name && name !== connecting.value.source ? name : null
}

function onConnectUp() {
  window.removeEventListener('pointermove', onConnectMove)
  const c = connecting.value
  const target = connectTarget.value
  connecting.value = null
  connectTarget.value = null
  if (!c || !target) return
  // Drawn from the manager's bottom connector onto the report: the DROP
  // target gets the reports-to tag, so the stored relationship matches the
  // arrow exactly as drawn (source → target = manager → report).
  const agent = props.agents.find((a) => a.name === target)
  if (agent) gridStore.addReportsTo(agent, c.source)
}

function removeEdge(e) {
  if (!window.confirm(`Remove reporting line: ${e.report} → ${e.manager}?`)) return
  const agent = props.agents.find((a) => a.name === e.report)
  if (agent) gridStore.removeReportsTo(agent, e.manager)
}

// --- block move: drag a zone's header to move the whole department ---
const zoneDrag = ref(null) // { dept, members, memberSet, dx, dy, dc, dr, valid }
let zdStartX = 0
let zdStartY = 0

function zoneStyle(z) {
  const style = {
    '--zc': z.color,
    left: z.x + 'px',
    top: z.y + 'px',
    width: z.w + 'px',
    height: z.h + 'px',
  }
  const zd = zoneDrag.value
  if (zd && zd.dept === z.dept) {
    style.transform = `translate(${zd.dx}px,${zd.dy}px)`
  }
  return style
}

function startZoneDrag(z, e) {
  if (draggingName.value || connecting.value || zoneDrag.value) return
  const members = props.agents
    .filter((a) => deptOf(a) === z.dept && layout.value[a.name])
    .map((a) => a.name)
  if (members.length === 0) return
  zdStartX = e.clientX
  zdStartY = e.clientY
  zoneDrag.value = {
    dept: z.dept,
    members,
    memberSet: new Set(members),
    dx: 0,
    dy: 0,
    dc: 0,
    dr: 0,
    valid: true,
  }
  hoverAgent.value = null
  window.addEventListener('pointermove', onZoneDragMove)
  window.addEventListener('pointerup', onZoneDragUp, { once: true })
}

function _zoneTargetsValid(zd, dc, dr) {
  if (dc === 0 && dr === 0) return true
  const occ = new Set()
  for (const [n, p] of Object.entries(layout.value)) {
    if (!zd.memberSet.has(n)) occ.add(`${p.c},${p.r}`)
  }
  for (const n of zd.members) {
    const p = layout.value[n]
    const c = p.c + dc
    const r = p.r + dr
    if (Math.abs(c) > COORD_LIMIT || Math.abs(r) > COORD_LIMIT) return false
    if (occ.has(`${c},${r}`)) return false
  }
  return true
}

function onZoneDragMove(e) {
  const zd = zoneDrag.value
  if (!zd) return
  const dx = (e.clientX - zdStartX) / vz.value
  const dy = (e.clientY - zdStartY) / vz.value
  const dc = Math.round(dx / (CELL_W + GAP_X))
  const dr = Math.round(dy / (CELL_H + GAP_Y))
  zoneDrag.value = { ...zd, dx, dy, dc, dr, valid: _zoneTargetsValid(zd, dc, dr) }
}

function onZoneDragUp() {
  window.removeEventListener('pointermove', onZoneDragMove)
  const zd = zoneDrag.value
  zoneDrag.value = null
  if (!zd) return
  // Invalid target → no commit; tiles spring back via the transform
  // transition. A no-move drop is a plain click.
  if (zd.valid && (zd.dc !== 0 || zd.dr !== 0)) {
    gridStore.moveTiles(zd.members, zd.dc, zd.dr)
  }
}

const zoneSockets = computed(() => {
  const zd = zoneDrag.value
  if (!zd || (zd.dc === 0 && zd.dr === 0)) return []
  return zd.members.map((n) => {
    const p = layout.value[n]
    const [x, y] = cellXY(p.c + zd.dc, p.r + zd.dr)
    return { key: n, x, y }
  })
})

function arrangeNow() {
  gridStore.applyLayout(arrangeByDept(props.agents))
  nextTick(fitView)
}

// --- drag state ---
const draggingName = ref(null)
const snappingName = ref(null)
const lockedName = ref(null)
const isDragging = computed(() => draggingName.value !== null)
const dragTransform = ref('')
const socketX = ref(0)
const socketY = ref(0)
const socketSwap = ref(false)
// Displaced-neighbor live preview: while hovering an occupied cell the
// neighbor slides to the dragged tile's origin cell; dragging away slides
// it back. A swap never disturbs a third tile.
const previewName = ref(null)

let home = null
let target = null
let startPX = 0
let startPY = 0
let originX = 0
let originY = 0
let curX = 0
let curY = 0
let lastX = 0
let tilt = 0
let rafId = 0
let lockTimer = null
let snapFallbackTimer = null
let dragPointerId = null // discriminate multi-touch: only this pointer drags

function cancelDrag() {
  draggingName.value = null
  previewName.value = null
  socketSwap.value = false
  dragTransform.value = ''
  dragPointerId = null
  dropZone.value = null
  home = null
  target = null
}

function tileStyle(name) {
  const style = { width: CELL_W + 'px', height: CELL_H + 'px' }
  if (name === draggingName.value) {
    style.transform = dragTransform.value
    return style
  }
  // Block move: member tiles follow the zone-header drag 1:1.
  const zd = zoneDrag.value
  if (zd && zd.memberSet.has(name)) {
    const p = layout.value[name]
    const [x, y] = cellXY(p.c, p.r)
    style.transform = `translate(${x + zd.dx}px,${y + zd.dy}px)`
    return style
  }
  if (name === previewName.value && home) {
    const [x, y] = cellXY(home.c, home.r)
    style.transform = `translate(${x}px,${y}px)`
    return style
  }
  const p = layout.value[name]
  const [x, y] = cellXY(p.c, p.r)
  style.transform = `translate(${x}px,${y}px)`
  return style
}

function frame() {
  rafId = 0
  if (!draggingName.value) return
  const vx = curX - lastX
  lastX = curX
  const t = Math.max(-3, Math.min(3, vx * 0.3))
  tilt += (t - tilt) * 0.25
  if (reducedMotion) tilt = 0
  dragTransform.value =
    `translate(${curX}px,${curY}px) rotate(${tilt.toFixed(2)}deg)` +
    (reducedMotion ? '' : ' scale(1.03)')
}

function onTilePointerDown(e) {
  const el = e.target.closest('.gv-tile')
  if (!el || e.button > 0) return
  if (e.target.closest('.nodrag')) return
  if (draggingName.value) return // one drag at a time (second touch ignored)
  const name = el.dataset.agent
  if (!name || !layout.value[name]) return

  dragPointerId = e.pointerId
  draggingName.value = name
  snappingName.value = null
  lockedName.value = null
  el.setPointerCapture(e.pointerId)

  const p = layout.value[name]
  home = { c: p.c, r: p.r }
  target = { c: p.c, r: p.r }
  const [x, y] = cellXY(p.c, p.r)
  originX = x
  originY = y
  curX = x
  curY = y
  lastX = x
  tilt = 0
  startPX = e.clientX
  startPY = e.clientY
  dragTransform.value = `translate(${x}px,${y}px)`
  socketX.value = x
  socketY.value = y
  socketSwap.value = false
  e.stopPropagation()
  e.preventDefault()
}

function onTilePointerMove(e) {
  if (!draggingName.value || e.pointerId !== dragPointerId) return
  // Zoom-aware: screen deltas ÷ zoom so the tile follows the cursor 1:1.
  curX = originX + (e.clientX - startPX) / vz.value
  curY = originY + (e.clientY - startPY) / vz.value
  if (!rafId) rafId = requestAnimationFrame(frame)

  const cell = cellFromCenter(curX + CELL_W / 2, curY + CELL_H / 2)
  if (cell.c !== target.c || cell.r !== target.r) {
    target = cell
    const occ = occupantAt(layout.value, cell.c, cell.r, draggingName.value)
    previewName.value = occ
    socketSwap.value = !!occ
    const [sx, sy] = cellXY(cell.c, cell.r)
    socketX.value = sx
    socketY.value = sy
  }

  // Org overlay: highlight the department zone the tile would join on drop.
  if (showZones.value && zones.value.length) {
    const z = zoneAt(zones.value, curX + CELL_W / 2, curY + CELL_H / 2)
    const a = z ? props.agents.find((x) => x.name === draggingName.value) : null
    dropZone.value = z && a && deptOf(a) !== z.dept ? z.dept : null
  }
}

function onTilePointerUp(e) {
  if (!draggingName.value || e.pointerId !== dragPointerId) return
  const name = draggingName.value
  // A plain click (no movement) must not enter the snapping state — with an
  // unchanged transform no transition runs, so transitionend would never
  // clear it.
  const moved =
    target.c !== home.c ||
    target.r !== home.r ||
    Math.abs(curX - originX) > 0.5 ||
    Math.abs(curY - originY) > 0.5
  const dropCell = { c: target.c, r: target.r }
  const pendingZone = dropZone.value

  cancelDrag()
  gridStore.moveTile(name, dropCell.c, dropCell.r)

  // Org overlay: dropping inside another department's zone assigns it
  // (writes dept-<zone>, clears other dept-* tags; plain tags untouched).
  if (pendingZone) {
    const agent = props.agents.find((a) => a.name === name)
    if (agent) gridStore.assignDept(agent, pendingZone)
  }

  // Overshoot spring into the cell, then a one-shot lock-ring pulse.
  if (moved && !reducedMotion) {
    snappingName.value = name
    // Safety: if the transform transition is skipped (already at rest),
    // clear the snapping state anyway.
    clearTimeout(snapFallbackTimer)
    snapFallbackTimer = setTimeout(() => {
      if (snappingName.value === name) snappingName.value = null
    }, 700)
  }
}

function onTileTransitionEnd(name, e) {
  if (e.propertyName !== 'transform' || snappingName.value !== name) return
  snappingName.value = null
  if (!reducedMotion) {
    lockedName.value = name
    clearTimeout(lockTimer)
    lockTimer = setTimeout(() => {
      lockedName.value = null
    }, 500)
  }
}

// Keyboard reorder: arrows move a focused tile into free cells; moving
// through a neighbor swaps.
function onTileKeydown(e) {
  const el = e.target.closest('.gv-tile')
  if (!el || e.target.closest('.nodrag')) return
  const name = el.dataset.agent
  const p = layout.value[name]
  if (!p) return
  let { c, r } = p
  if (e.key === 'ArrowLeft') c -= 1
  else if (e.key === 'ArrowRight') c += 1
  else if (e.key === 'ArrowUp') r -= 1
  else if (e.key === 'ArrowDown') r += 1
  else return
  e.preventDefault()
  if (Math.abs(c) > COORD_LIMIT || Math.abs(r) > COORD_LIMIT) return
  gridStore.moveTile(name, c, r)
  if (!reducedMotion) {
    lockedName.value = name
    clearTimeout(lockTimer)
    lockTimer = setTimeout(() => {
      lockedName.value = null
    }, 500)
  }
}

// --- free-cell shading while dragging ---
const cellMarks = computed(() => {
  if (!isDragging.value) return []
  const vw = canvasW.value
  const vh = canvasH.value
  const x0 = -vtx.value / vz.value
  const y0 = -vty.value / vz.value
  const c0 = Math.floor(x0 / (CELL_W + GAP_X)) - 1
  const c1 = Math.ceil((x0 + vw / vz.value) / (CELL_W + GAP_X)) + 1
  const r0 = Math.floor(y0 / (CELL_H + GAP_Y)) - 1
  const r1 = Math.ceil((y0 + vh / vz.value) / (CELL_H + GAP_Y)) + 1
  if ((c1 - c0) * (r1 - r0) > 600) return [] // zoomed way out — skip shading
  const marks = []
  for (let r = r0; r <= r1; r++) {
    for (let c = c0; c <= c1; c++) {
      const [x, y] = cellXY(c, r)
      marks.push({ key: `${c},${r}`, x, y })
    }
  }
  return marks
})

// --- canvas pan ---
const isPanning = ref(false)
let panPointerId = null
let panSX = 0
let panSY = 0
let panTX = 0
let panTY = 0

function onCanvasPointerDown(e) {
  if (
    e.target.closest('.gv-tile') ||
    e.target.closest('.gv-zoomctl') ||
    e.target.closest('.gv-legend') ||
    e.target.closest('.gv-orgctl') ||
    e.target.closest('.gv-wire-hit')
  ) return
  if (e.button > 0) return
  // A second touch during a tile drag must not start a pan (multi-touch).
  if (isPanning.value || draggingName.value) return
  isPanning.value = true
  panPointerId = e.pointerId
  canvasEl.value.setPointerCapture(e.pointerId)
  panSX = e.clientX
  panSY = e.clientY
  panTX = vtx.value
  panTY = vty.value
}

function onCanvasPointerMove(e) {
  if (!isPanning.value || e.pointerId !== panPointerId) return
  vtx.value = panTX + (e.clientX - panSX)
  vty.value = panTY + (e.clientY - panSY)
}

function endPan(e) {
  if (e && e.pointerId !== panPointerId) return
  isPanning.value = false
  panPointerId = null
}

// --- shared 1s tick for the tiles' live timers ---
const now = ref(Date.now())
let tickTimer = null

// --- public surface for the Dashboard header controls ---
function tidyUp() {
  gridStore.tidy()
  nextTick(fitView)
}

function resetToDefault() {
  const names = props.agents.map((a) => a.name)
  const systemNames = new Set(props.agents.filter((a) => a.is_system).map((a) => a.name))
  gridStore.resetLayout(names, systemNames)
  nextTick(fitView)
}

function refresh() {
  gridStore.forceRefresh([...visibleNames.value])
}

defineExpose({ tidyUp, resetToDefault, refresh, fitView })

// --- lifecycle ---
let resizeObserver = null

function measureCanvas() {
  if (!canvasEl.value) return
  canvasW.value = canvasEl.value.clientWidth
  canvasH.value = canvasEl.value.clientHeight
}

onMounted(() => {
  gridStore.startPolling()
  measureCanvas()
  resizeObserver = new ResizeObserver(() => measureCanvas())
  resizeObserver.observe(canvasEl.value)
  // Wheel needs passive:false to preventDefault page scroll.
  canvasEl.value.addEventListener('wheel', onWheel, { passive: false })
  tickTimer = setInterval(() => {
    if (!document.hidden) now.value = Date.now()
  }, 1000)
  nextTick(fitView)
})

onBeforeUnmount(() => {
  gridStore.stopPolling()
  if (resizeObserver) resizeObserver.disconnect()
  if (canvasEl.value) canvasEl.value.removeEventListener('wheel', onWheel)
  clearInterval(tickTimer)
  clearTimeout(lockTimer)
  clearTimeout(snapFallbackTimer)
  if (rafId) cancelAnimationFrame(rafId)
  window.removeEventListener('pointermove', onConnectMove)
  window.removeEventListener('pointerup', onConnectUp)
  window.removeEventListener('pointermove', onZoneDragMove)
  window.removeEventListener('pointerup', onZoneDragUp)
})
</script>

<style scoped>
/*
 * Grid-view design tokens (--gv-*) from the trinity-enterprise#47 design of
 * record, mapped onto the app's Tailwind palette. Defined once here; the
 * AgentTile children inherit them through the CSS custom-property cascade.
 */
.fleet-canvas {
  --gv-text: #111827;
  --gv-muted: #6b7280;
  --gv-faint: #9ca3af;
  --gv-ghost: #d1d5db;
  --gv-border: #e5e7eb;
  --gv-border-soft: rgba(229, 231, 235, 0.6);
  --gv-panel: #ffffff;
  --gv-tile: rgba(255, 255, 255, 0.9);
  --gv-seg-bg: #f9fafb;
  --gv-bar-track: #e5e7eb;
  --gv-blue: #2563eb;
  --gv-green: #22c55e;
  --gv-green-text: #16a34a;
  --gv-yellow: #eab308;
  --gv-yellow-text: #a16207;
  --gv-red: #ef4444;
  --gv-red-text: #dc2626;
  --gv-dot-active: #10b981;
  --gv-btn-bg: #eff6ff;
  --gv-btn-bg-hover: #dbeafe;
  --gv-btn-text: #1d4ed8;
  --gv-btn-border: #bfdbfe;
  --gv-badge-warn-bg: #fef9c3;
  --gv-badge-warn-tx: #a16207;
  --gv-badge-sys-bg: #f3e8ff;
  --gv-badge-sys-tx: #7e22ce;
  /* ent#139 skill-runner class: teal, distinct from system purple */
  --gv-badge-runner-bg: #ccfbf1;
  --gv-badge-runner-tx: #0f766e;
  --gv-ring-runner: #2dd4bf;
  --gv-sys-tile: rgba(250, 245, 255, 0.9);
  --gv-sys-border: #e9d5ff;
  --gv-sys-btn-bg: #faf5ff;
  --gv-sys-btn-tx: #7e22ce;
  --gv-sys-btn-bd: #e9d5ff;
  --gv-bk-sched: #6366f1;
  --gv-bk-man: #14b8a6;
  --gv-bk-ext: #ec4899;
  --gv-dots: rgba(17, 24, 39, 0.12);
  /* Layered depth: top-edge highlight (glass lip) + tight contact shadow +
     mid key shadow + soft ambient falloff. */
  --gv-tile-sheen: linear-gradient(180deg, rgba(255, 255, 255, 0.6), rgba(255, 255, 255, 0) 55%);
  --gv-tile-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.8),
    0 1px 2px rgba(23, 24, 28, 0.08),
    0 5px 10px -3px rgba(23, 24, 28, 0.12),
    0 18px 36px -14px rgba(23, 24, 28, 0.22);
  --gv-tile-shadow-hover:
    inset 0 1px 0 rgba(255, 255, 255, 0.8),
    0 2px 4px rgba(23, 24, 28, 0.1),
    0 10px 18px -4px rgba(23, 24, 28, 0.16),
    0 26px 48px -16px rgba(23, 24, 28, 0.28);
  --gv-drag-shadow: 0 24px 48px -12px rgba(23, 24, 28, 0.35);

  position: relative;
  width: 100%;
  height: 100%;
  overflow: hidden;
  background-color: #f9fafb;
  background-image: radial-gradient(circle, var(--gv-dots) 1px, transparent 1px);
  cursor: grab;
  touch-action: none;
}

:root.dark .fleet-canvas {
  --gv-text: #f9fafb;
  --gv-muted: #9ca3af;
  --gv-faint: #6b7280;
  --gv-ghost: #4b5563;
  --gv-border: #374151;
  --gv-border-soft: rgba(55, 65, 81, 0.5);
  --gv-panel: #1f2937;
  --gv-tile: rgba(31, 41, 55, 0.9);
  --gv-seg-bg: #374151;
  --gv-bar-track: #374151;
  --gv-blue: #3b82f6;
  --gv-green: #22c55e;
  --gv-green-text: #4ade80;
  --gv-yellow: #eab308;
  --gv-yellow-text: #facc15;
  --gv-red: #ef4444;
  --gv-red-text: #f87171;
  --gv-dot-active: #10b981;
  --gv-btn-bg: rgba(30, 58, 138, 0.3);
  --gv-btn-bg-hover: rgba(30, 58, 138, 0.5);
  --gv-btn-text: #93c5fd;
  --gv-btn-border: #1d4ed8;
  --gv-badge-warn-bg: rgba(113, 63, 18, 0.5);
  --gv-badge-warn-tx: #fde047;
  --gv-badge-sys-bg: rgba(88, 28, 135, 0.5);
  --gv-badge-sys-tx: #d8b4fe;
  --gv-badge-runner-bg: rgba(19, 78, 74, 0.55);
  --gv-badge-runner-tx: #5eead4;
  --gv-ring-runner: #0d9488;
  --gv-sys-tile: rgba(88, 28, 135, 0.3);
  --gv-sys-border: rgba(126, 34, 206, 0.5);
  --gv-sys-btn-bg: rgba(88, 28, 135, 0.3);
  --gv-sys-btn-tx: #d8b4fe;
  --gv-sys-btn-bd: #7e22ce;
  --gv-bk-sched: #818cf8;
  --gv-bk-man: #2dd4bf;
  --gv-bk-ext: #f472b6;
  --gv-dots: rgba(249, 250, 251, 0.08);
  --gv-drag-shadow: 0 28px 56px -12px rgba(0, 0, 0, 0.75);
  --gv-tile-sheen: linear-gradient(180deg, rgba(255, 255, 255, 0.05), rgba(255, 255, 255, 0) 55%);
  --gv-tile-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.08),
    0 1px 2px rgba(0, 0, 0, 0.45),
    0 6px 12px -4px rgba(0, 0, 0, 0.5),
    0 22px 44px -18px rgba(0, 0, 0, 0.75);
  --gv-tile-shadow-hover:
    inset 0 1px 0 rgba(255, 255, 255, 0.1),
    0 2px 4px rgba(0, 0, 0, 0.5),
    0 12px 20px -6px rgba(0, 0, 0, 0.55),
    0 30px 56px -18px rgba(0, 0, 0, 0.8);

  background-color: #111827;
}

.fleet-canvas.panning {
  cursor: grabbing;
}

.gv-world {
  position: absolute;
  top: 0;
  left: 0;
  transform-origin: 0 0;
}

.gv-grid {
  position: relative;
  width: 0;
  height: 0;
}

/* --- tiles (chrome + drag physics; content lives in AgentTile) --- */
.gv-tile {
  position: absolute;
  top: 0;
  left: 0;
  background-color: var(--gv-tile);
  background-image: var(--gv-tile-sheen);
  border: 1px solid var(--gv-border-soft);
  border-radius: 12px;
  box-shadow: var(--gv-tile-shadow);
  backdrop-filter: blur(4px);
  cursor: grab;
  touch-action: none;
  will-change: transform;
  transition: transform 0.28s cubic-bezier(0.3, 0.7, 0.25, 1), box-shadow 0.2s ease, border-color 0.2s ease;
  z-index: 2;
}
.gv-tile:hover {
  box-shadow: var(--gv-tile-shadow-hover);
}
.gv-tile:focus-visible {
  outline: 2px solid var(--gv-blue);
  outline-offset: 2px;
}
.gv-tile.dragging {
  transition: box-shadow 0.2s ease, border-color 0.2s ease;
  box-shadow: var(--gv-drag-shadow);
  border-color: color-mix(in srgb, var(--gv-blue) 50%, var(--gv-border));
  cursor: grabbing;
  z-index: 20;
}
.gv-tile.snapping {
  transition: transform 0.42s cubic-bezier(0.22, 1.35, 0.32, 1), box-shadow 0.35s ease, border-color 0.35s ease;
  z-index: 10;
}
.gv-tile.locked {
  animation: gv-lockring 0.45s ease-out;
}
@keyframes gv-lockring {
  0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--gv-blue) 45%, transparent), var(--gv-tile-shadow); }
  100% { box-shadow: 0 0 0 12px transparent, var(--gv-tile-shadow); }
}
.gv-tile.system {
  background-color: var(--gv-sys-tile);
  border-color: var(--gv-sys-border);
}

/* Far-from-viewport placeholder: keeps the constellation shape visible
   without mounting the full tile or triggering hydration. */
.gv-tile-far {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 600;
  color: var(--gv-faint);
}

/* --- drag aids --- */
.gv-cellmark {
  position: absolute;
  border-radius: 12px;
  background: color-mix(in srgb, var(--gv-text) 4%, transparent);
  pointer-events: none;
  opacity: 0;
  transition: opacity 0.25s ease;
}
.gv-grid.showcells .gv-cellmark {
  opacity: 1;
}

.gv-socket {
  position: absolute;
  border-radius: 12px;
  border: 1.5px solid color-mix(in srgb, var(--gv-blue) 60%, transparent);
  background: color-mix(in srgb, var(--gv-blue) 8%, transparent);
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.15s ease;
  z-index: 1;
}
.gv-socket.show {
  opacity: 1;
}
.gv-socket.swap {
  border-style: dashed;
}
.gv-socket::after {
  content: '';
  position: absolute;
  inset: -5px;
  border-radius: 16px;
  border: 1px solid color-mix(in srgb, var(--gv-blue) 25%, transparent);
}

/* --- zoom controls + readout --- */
.gv-zoomctl {
  position: absolute;
  left: 14px;
  bottom: 14px;
  z-index: 30;
  display: flex;
  flex-direction: column;
  background: var(--gv-panel);
  border: 1px solid var(--gv-border);
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.12);
}
.gv-zoomctl button {
  width: 28px;
  height: 28px;
  border: 0;
  background: transparent;
  color: var(--gv-muted);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}
.gv-zoomctl button + button {
  border-top: 1px solid var(--gv-border);
}
.gv-zoomctl button:hover {
  background: var(--gv-btn-bg);
  color: var(--gv-btn-text);
}
.gv-zoomctl button:focus-visible {
  outline: 2px solid var(--gv-blue);
  outline-offset: -2px;
}
.gv-zoomctl svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.gv-zoomlvl {
  position: absolute;
  left: 50px;
  bottom: 18px;
  z-index: 30;
  font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
  font-size: 10.5px;
  color: var(--gv-faint);
  background: color-mix(in srgb, var(--gv-panel) 80%, transparent);
  border-radius: 5px;
  padding: 2px 7px;
}

/* --- legend --- */
.gv-legend {
  position: absolute;
  right: 84px; /* clear of the floating help-chat button */
  bottom: 14px;
  z-index: 30;
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--gv-panel);
  border: 1px solid var(--gv-border);
  border-radius: 8px;
  padding: 6px 12px;
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.12);
  font-size: 10.5px;
  color: var(--gv-muted);
}
.gv-legend span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  white-space: nowrap;
}
.gv-legend i {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  display: inline-block;
}
.gv-legend i.ls { background: var(--gv-bk-sched); }
.gv-legend i.lm { background: var(--gv-bk-man); }
.gv-legend i.le { background: var(--gv-bk-ext); }

/* --- PROTOTYPE org overlay: zones, reporting wires, connect handle --- */
.fleet-canvas {
  --gv-wire: #64748b;
}
:root.dark .fleet-canvas {
  --gv-wire: #8b96a8;
}

.gv-zone {
  position: absolute;
  z-index: 0;
  border: 1px solid color-mix(in srgb, var(--zc) 32%, transparent);
  background: color-mix(in srgb, var(--zc) 5%, transparent);
  border-radius: 16px;
  pointer-events: none;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.gv-zone.hot {
  border-color: color-mix(in srgb, var(--zc) 75%, transparent);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--zc) 15%, transparent);
}
/* Header band must fit the 34px top overhang (chrome budget in gridOrg.js).
   It is also the block-move grab handle — the only pointer-active part of
   the zone (the frame itself stays pointer-transparent for canvas pan). */
.gv-zonehead {
  display: flex;
  align-items: baseline;
  gap: 9px;
  padding: 7px 14px 0;
  font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
  font-size: 10.5px;
  line-height: 1.4;
  letter-spacing: 0.13em;
  font-weight: 700;
  color: var(--zc);
  white-space: nowrap;
  pointer-events: auto;
  cursor: grab;
  user-select: none;
  -webkit-user-select: none;
  touch-action: none;
}
.gv-zone.blockdragging {
  transition: none;
  border-color: color-mix(in srgb, var(--zc) 60%, transparent);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--zc) 12%, transparent);
}
.gv-zone.blockdragging .gv-zonehead {
  cursor: grabbing;
}
.gv-zone.invalid {
  border-color: color-mix(in srgb, var(--gv-red) 70%, transparent);
  background: color-mix(in srgb, var(--gv-red) 6%, transparent);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--gv-red) 12%, transparent);
}
/* Member tiles follow the block drag 1:1 — no transform transition while
   moving; the transition returns on drop for the snap/spring-back. */
.gv-tile.blockdrag {
  transition: box-shadow 0.2s ease, border-color 0.2s ease;
  z-index: 15;
}
.gv-socket.invalid {
  border-color: color-mix(in srgb, var(--gv-red) 60%, transparent);
  background: color-mix(in srgb, var(--gv-red) 8%, transparent);
}
.gv-zonehead .zname {
  text-transform: uppercase;
}
.gv-zonehead .zstat {
  font-weight: 400;
  letter-spacing: 0.02em;
  color: var(--gv-muted);
}

.gv-wires {
  position: absolute;
  z-index: 1;
  pointer-events: none;
  overflow: visible;
}
.gv-wire-line {
  fill: none;
  stroke: var(--gv-wire);
  stroke-width: 1.5;
  opacity: 0.55;
  transition: opacity 0.15s ease;
}
.gv-wire-ah {
  fill: var(--gv-wire);
  stroke: none;
  opacity: 0.55;
  transition: opacity 0.15s ease;
}
.gv-wire-hit {
  fill: none;
  stroke: transparent;
  stroke-width: 14;
  pointer-events: stroke;
  cursor: pointer;
}
.gv-wire.hot .gv-wire-line,
.gv-wire.hot .gv-wire-ah {
  opacity: 0.95;
}
.gv-wire.hot .gv-wire-line {
  stroke-width: 2;
}
.gv-wire.dim .gv-wire-line,
.gv-wire.dim .gv-wire-ah {
  opacity: 0.12;
}
.gv-wire-line.adding {
  stroke: var(--gv-blue);
  stroke-dasharray: 5 4;
  opacity: 0.95;
}
.gv-wire-ah.adding {
  fill: var(--gv-blue);
  opacity: 0.95;
}

/* Bottom connector — reporting lines are drawn vertically (org-chart:
   manager above, report below), so the port sits at bottom-center. */
.gv-handle {
  position: absolute;
  bottom: -7px;
  left: 50%;
  margin-left: -7px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--gv-blue);
  border: 2px solid var(--gv-panel);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--gv-blue) 25%, transparent);
  opacity: 0;
  cursor: crosshair;
  transition: opacity 0.15s ease;
  z-index: 5;
  touch-action: none;
}
.gv-tile:hover .gv-handle,
.gv-handle:hover {
  opacity: 1;
}

.gv-tile.linktarget {
  border-color: color-mix(in srgb, var(--gv-blue) 70%, var(--gv-border));
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--gv-blue) 20%, transparent), var(--gv-tile-shadow);
}

.gv-orgctl {
  position: absolute;
  top: 14px;
  right: 16px;
  z-index: 30;
  display: flex;
  gap: 4px;
  background: var(--gv-panel);
  border: 1px solid var(--gv-border);
  border-radius: 8px;
  padding: 4px;
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.12);
}
.gv-orgctl button {
  border: 0;
  background: transparent;
  color: var(--gv-muted);
  font-size: 11px;
  font-weight: 600;
  padding: 4px 10px;
  border-radius: 6px;
  cursor: pointer;
  white-space: nowrap;
}
.gv-orgctl button:hover {
  background: var(--gv-btn-bg-hover);
  color: var(--gv-btn-text);
}
.gv-orgctl button.on {
  background: var(--gv-btn-bg);
  color: var(--gv-btn-text);
}
.gv-orgctl button:focus-visible {
  outline: 2px solid var(--gv-blue);
  outline-offset: -2px;
}

.gv-legend .lrep {
  color: var(--gv-muted);
}
.gv-legend .lrep svg {
  color: var(--gv-wire);
  display: block;
}

@media (prefers-reduced-motion: reduce) {
  .gv-tile,
  .gv-tile.snapping {
    transition: box-shadow 0.2s ease, border-color 0.2s ease;
  }
  .gv-tile.locked {
    animation: none;
  }
}
</style>
