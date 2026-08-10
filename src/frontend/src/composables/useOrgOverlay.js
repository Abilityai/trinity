import { ref, computed } from 'vue'
import { useFleetGridStore } from '@/stores/fleetGrid'
import { useNetworkStore } from '@/stores/network'
import {
  CELL_W,
  CELL_H,
  GAP_X,
  GAP_Y,
  COORD_LIMIT,
  cellXY,
  layoutBBox,
} from '@/utils/gridLayout'
import {
  DEPT_PREFIX,
  REPORTS_PREFIX,
  orgMeta,
  deptOf,
  deptInfoByAgent,
  computeZones,
  computeEdges,
  zoneAt,
  arrangeByDept,
  tidyByDept,
  newcomerOrigin,
  orgTagFits,
  arrowheadPath,
} from '@/utils/gridOrg'

/**
 * Org-overlay state machine for the Dashboard Grid view
 * (trinity-enterprise#305): department zones, reporting lines, connect-port
 * drag, drop-to-assign, zone block move, arrange/tidy, direction affordances.
 *
 * FleetGrid.vue owns the canvas (pan/zoom, tile drag, culling) and passes its
 * view refs in; this composable owns everything org. Tag writes go through
 * the network store (atomic PUT set-list; agent_tags_changed WS keeps other
 * browsers converged).
 */
export function useOrgOverlay({ agents, layout, canvasEl, vz, vtx, vty, draggingName, fitView }) {
  const gridStore = useFleetGridStore()
  const networkStore = useNetworkStore()

  // --- visibility toggles (persisted per user) ---
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

  // --- derived org state ---
  const meta = computed(() => orgMeta(agents.value))
  const deptByAgent = computed(() => deptInfoByAgent(agents.value, meta.value))
  const zones = computed(() =>
    showZones.value ? computeZones(layout.value, agents.value, meta.value) : []
  )

  // Committed edges + a stable world-space viewport. Deliberately does NOT
  // track the connect cursor (the SVG has overflow:visible, so the draft may
  // draw outside the box) — pointermove never re-runs computeEdges.
  const wireLayer = computed(() => {
    if (!showLines.value) return null
    const edges = computeEdges(layout.value, agents.value)
    if (edges.length === 0 && !connecting.value) return null
    const b = layoutBBox(layout.value)
    const M = 200
    return { x: b.x - M, y: b.y - M, w: b.w + 2 * M, h: b.h + 2 * M, edges }
  })

  // --- canvas-scoped toast (completed verbs get a toast + Undo; failures
  // render as a persistent alert with a named problem and fix) ---
  const orgToast = ref(null) // { message, type: 'success'|'error', undo?: fn }
  let _toastTimer = null

  function _toast(message, { undo = null, type = 'success' } = {}) {
    clearTimeout(_toastTimer)
    orgToast.value = { message, type, undo }
    if (type !== 'error') {
      _toastTimer = setTimeout(() => {
        orgToast.value = null
      }, 6000)
    }
  }
  function dismissToast() {
    clearTimeout(_toastTimer)
    orgToast.value = null
  }
  async function undoToast() {
    const t = orgToast.value
    dismissToast()
    if (t && t.undo) {
      try {
        await t.undo()
      } catch {
        _toast('Undo failed — the fleet may have changed. Refresh and check the agent’s tags.', { type: 'error' })
      }
    }
  }

  function _worldPoint(e) {
    const rect = canvasEl.value.getBoundingClientRect()
    return [
      (e.clientX - rect.left - vtx.value) / vz.value,
      (e.clientY - rect.top - vty.value) / vz.value,
    ]
  }

  // --- hover states ---
  const hoverAgent = ref(null)
  const hoverEdge = ref(null) // {id, mid, manager, report} for the line chip

  function onTileEnter(name) {
    if (!draggingName.value && !connecting.value && !zoneDrag.value) hoverAgent.value = name
  }
  function onTileLeave(name) {
    if (hoverAgent.value === name) hoverAgent.value = null
  }
  function onEdgeEnter(e) {
    hoverEdge.value = e
  }
  function onEdgeLeave(e) {
    if (hoverEdge.value && hoverEdge.value.id === e.id) hoverEdge.value = null
  }
  function edgeClass(e) {
    if (hoverEdge.value && hoverEdge.value.id === e.id) return 'hot'
    if (!hoverAgent.value) return ''
    return e.manager === hoverAgent.value || e.report === hoverAgent.value ? 'hot' : 'dim'
  }

  // --- connect drag (bottom port: manager → drop target becomes the report) ---
  const connecting = ref(null) // { source, sx, sy, x, y }
  const connectTarget = ref(null)
  let _connectRaf = 0
  let _connectEv = null

  function startConnect(name, e) {
    const p = layout.value[name]
    if (!p || !canvasEl.value) return
    const [x, y] = cellXY(p.c, p.r)
    const [wx, wy] = _worldPoint(e)
    connecting.value = { source: name, sx: x + CELL_W / 2, sy: y + CELL_H, x: wx, y: wy }
    hoverAgent.value = null
    window.addEventListener('pointermove', onConnectMove)
    window.addEventListener('pointerup', onConnectUp, { once: true })
  }

  function onConnectMove(e) {
    _connectEv = e
    if (!_connectRaf) _connectRaf = requestAnimationFrame(_processConnectMove)
  }

  function _processConnectMove() {
    _connectRaf = 0
    const e = _connectEv
    if (!connecting.value || !canvasEl.value || !e) return
    const [wx, wy] = _worldPoint(e)
    connecting.value = { ...connecting.value, x: wx, y: wy }
    const el = document.elementFromPoint(e.clientX, e.clientY)
    const tile = el && el.closest ? el.closest('.gv-tile') : null
    const name = tile ? tile.dataset.agent : null
    connectTarget.value = name && name !== connecting.value.source ? name : null
  }

  async function onConnectUp() {
    window.removeEventListener('pointermove', onConnectMove)
    if (_connectRaf) {
      cancelAnimationFrame(_connectRaf)
      _connectRaf = 0
    }
    const c = connecting.value
    const target = connectTarget.value
    connecting.value = null
    connectTarget.value = null
    if (!c || !target) return
    if (!orgTagFits(REPORTS_PREFIX, c.source)) {
      _toast(
        `Can’t link: "${REPORTS_PREFIX}${c.source}" exceeds the 50-character tag limit. Rename the manager agent shorter, or manage this line from the agent’s tags.`,
        { type: 'error' }
      )
      return
    }
    try {
      const { previous } = await networkStore.addReportsTo(target, c.source)
      _toast(`${target} now reports to ${c.source}`, {
        undo: () => networkStore.setAgentTags(target, previous),
      })
    } catch (e) {
      _toast(
        `Couldn’t save the reporting line (${e?.response?.status || 'network error'}). You need owner access to ${target} — retry after checking.`,
        { type: 'error' }
      )
    }
  }

  const connectDraft = computed(() => {
    const c = connecting.value
    if (!c) return null
    const s = c.y >= c.sy ? 1 : -1
    const k = Math.min(120, Math.max(30, Math.abs(c.y - c.sy) * 0.4))
    return {
      d: `M ${c.sx} ${c.sy} C ${c.sx} ${c.sy + s * k}, ${c.x} ${c.y - s * k}, ${c.x} ${c.y}`,
      ah: arrowheadPath(c.x, c.y, true, s),
    }
  })

  async function removeEdge(e) {
    try {
      const { previous } = await networkStore.removeReportsTo(e.report, e.manager)
      hoverEdge.value = null
      _toast(`Removed reporting line ${e.manager} → ${e.report}`, {
        undo: () => networkStore.setAgentTags(e.report, previous),
      })
    } catch (err) {
      _toast(
        `Couldn’t remove the line (${err?.response?.status || 'network error'}). You need owner access to ${e.report}.`,
        { type: 'error' }
      )
    }
  }

  // --- drop-to-assign (tile drag over a writable zone) ---
  const dropZone = ref(null)

  /** Called from the tile-drag pointermove with the dragged tile's center. */
  function trackDropZone(cx, cy, dragged) {
    if (!showZones.value || zones.value.length === 0) {
      dropZone.value = null
      return
    }
    const z = zoneAt(zones.value, cx, cy)
    if (!z || z.readOnly) {
      dropZone.value = null
      return
    }
    const a = agents.value.find((x) => x.name === dragged)
    dropZone.value = a && deptOf(a, meta.value) !== z.dept ? z.dept : null
  }

  /** Called from the tile-drag pointerup AFTER the tile move commits. */
  async function commitDrop(name, cx, cy) {
    const pending = dropZone.value
    dropZone.value = null
    if (!pending) return
    // Re-validate at drop against fresh zones (roster/tags may have changed
    // mid-drag) — never commit a stale highlight.
    const z = zoneAt(zones.value, cx, cy)
    if (!z || z.readOnly || z.dept !== pending) return
    const a = agents.value.find((x) => x.name === name)
    if (!a || deptOf(a, meta.value) === z.dept) return
    if (!orgTagFits(DEPT_PREFIX, z.dept)) return
    try {
      const { previous } = await networkStore.assignDept(name, z.dept)
      _toast(`${name} moved to ${z.dept}`, {
        undo: () => networkStore.setAgentTags(name, previous),
      })
    } catch (e) {
      _toast(
        `Couldn’t change department (${e?.response?.status || 'network error'}). You need owner access to ${name}.`,
        { type: 'error' }
      )
    }
  }

  // --- block move (drag a zone header to move the whole department) ---
  const zoneDrag = ref(null) // { dept, members, memberSet, dx, dy, dc, dr, valid }
  let zdStartX = 0
  let zdStartY = 0
  let _zoneRaf = 0
  let _zoneEv = null

  function zoneStyle(z) {
    const style = {
      '--zc': `var(--gv-dept-${z.slot})`,
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
    const members = agents.value
      .filter((a) => deptOf(a, meta.value) === z.dept && layout.value[a.name])
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
      if (!p) return false
      const c = p.c + dc
      const r = p.r + dr
      if (Math.abs(c) > COORD_LIMIT || Math.abs(r) > COORD_LIMIT) return false
      if (occ.has(`${c},${r}`)) return false
    }
    return true
  }

  function onZoneDragMove(e) {
    _zoneEv = e
    if (!_zoneRaf) _zoneRaf = requestAnimationFrame(_processZoneMove)
  }

  function _processZoneMove() {
    _zoneRaf = 0
    const e = _zoneEv
    const zd = zoneDrag.value
    if (!zd || !e) return
    const dx = (e.clientX - zdStartX) / vz.value
    const dy = (e.clientY - zdStartY) / vz.value
    const dc = Math.round(dx / (CELL_W + GAP_X))
    const dr = Math.round(dy / (CELL_H + GAP_Y))
    zoneDrag.value = { ...zd, dx, dy, dc, dr, valid: _zoneTargetsValid(zd, dc, dr) }
  }

  function onZoneDragUp() {
    window.removeEventListener('pointermove', onZoneDragMove)
    if (_zoneRaf) {
      cancelAnimationFrame(_zoneRaf)
      _zoneRaf = 0
    }
    const zd = zoneDrag.value
    zoneDrag.value = null
    if (!zd || (zd.dc === 0 && zd.dr === 0)) return
    // Re-validate at drop — the layout may have changed mid-drag (roster
    // sync, newcomer placement). A stale ok is never committed.
    if (_zoneTargetsValid(zd, zd.dc, zd.dr)) {
      gridStore.moveTiles(zd.members, zd.dc, zd.dr)
    }
  }

  const zoneSockets = computed(() => {
    const zd = zoneDrag.value
    if (!zd || (zd.dc === 0 && zd.dr === 0)) return []
    return zd.members
      .filter((n) => layout.value[n])
      .map((n) => {
        const p = layout.value[n]
        const [x, y] = cellXY(p.c + zd.dc, p.r + zd.dr)
        return { key: n, x, y }
      })
  })

  // --- arrange / tidy ---
  function arrangeNow() {
    gridStore.applyLayout(arrangeByDept(agents.value, meta.value))
    if (fitView) requestAnimationFrame(() => fitView())
  }

  /** Zone-aware Tidy; returns false when zones are off/empty (caller falls
   *  back to the classic global tidy). */
  function tidyZones() {
    if (!showZones.value || zones.value.length === 0) return false
    gridStore.applyLayout(tidyByDept(layout.value, agents.value, meta.value))
    return true
  }

  /** originFor hook for syncLayout: newcomers join their department's hull. */
  function newcomerOriginFor(name, layoutSoFar) {
    return newcomerOrigin(name, layoutSoFar, agents.value, meta.value)
  }

  // --- New department (bootstrap affordance) ---
  const newDeptOpen = ref(false)
  const newDeptName = ref('')
  const newDeptError = ref('')
  const assignMode = ref(null) // { dept, count }

  function openNewDept() {
    newDeptOpen.value = true
    newDeptName.value = ''
    newDeptError.value = ''
  }
  function closeNewDept() {
    newDeptOpen.value = false
  }
  function confirmNewDept() {
    const name = newDeptName.value.trim().toLowerCase()
    if (!name || !/^[a-z0-9-]+$/.test(name)) {
      newDeptError.value =
        'Department names use lowercase letters, numbers, and hyphens — e.g. "marketing" or "gtm-emea".'
      return
    }
    if (!orgTagFits(DEPT_PREFIX, name)) {
      newDeptError.value = `Too long: "${DEPT_PREFIX}${name}" must be ≤ 50 characters.`
      return
    }
    newDeptOpen.value = false
    assignMode.value = { dept: name, count: 0 }
  }

  /** Tile click while assign mode is armed adds the agent to the new dept. */
  async function assignModeClick(name) {
    const m = assignMode.value
    if (!m) return false
    try {
      await networkStore.assignDept(name, m.dept)
      assignMode.value = { ...m, count: m.count + 1 }
    } catch (e) {
      _toast(
        `Couldn’t add ${name} to ${m.dept} (${e?.response?.status || 'network error'}).`,
        { type: 'error' }
      )
    }
    return true
  }

  function endAssignMode() {
    const m = assignMode.value
    assignMode.value = null
    if (m && m.count > 0) {
      _toast(`${m.dept}: ${m.count} agent${m.count === 1 ? '' : 's'} assigned`)
    }
  }

  /** Roster changed mid-gesture: drop every in-flight org drag. */
  function cancelOrgDrags() {
    if (connecting.value) {
      window.removeEventListener('pointermove', onConnectMove)
      connecting.value = null
      connectTarget.value = null
    }
    if (zoneDrag.value) {
      window.removeEventListener('pointermove', onZoneDragMove)
      zoneDrag.value = null
    }
    dropZone.value = null
  }

  function destroy() {
    cancelOrgDrags()
    window.removeEventListener('pointerup', onConnectUp)
    window.removeEventListener('pointerup', onZoneDragUp)
    clearTimeout(_toastTimer)
    if (_connectRaf) cancelAnimationFrame(_connectRaf)
    if (_zoneRaf) cancelAnimationFrame(_zoneRaf)
  }

  return {
    // toggles
    showZones,
    showLines,
    toggleZones,
    toggleLines,
    // derived
    meta,
    deptByAgent,
    zones,
    wireLayer,
    connectDraft,
    zoneSockets,
    // hover / drag state
    hoverAgent,
    hoverEdge,
    connecting,
    connectTarget,
    dropZone,
    zoneDrag,
    // handlers
    onTileEnter,
    onTileLeave,
    onEdgeEnter,
    onEdgeLeave,
    edgeClass,
    startConnect,
    removeEdge,
    trackDropZone,
    commitDrop,
    startZoneDrag,
    zoneStyle,
    arrangeNow,
    tidyZones,
    newcomerOriginFor,
    // new department / assign mode
    newDeptOpen,
    newDeptName,
    newDeptError,
    assignMode,
    openNewDept,
    closeNewDept,
    confirmNewDept,
    assignModeClick,
    endAssignMode,
    // toast
    orgToast,
    dismissToast,
    undoToast,
    // lifecycle
    cancelOrgDrags,
    destroy,
  }
}
