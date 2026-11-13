import { defineStore } from 'pinia'
import { ref } from 'vue'
import { portalHttp, useClientPortalStore } from './clientPortal'
import { projectErrorMessage } from '@/components/portal/projects/projectsUtils'

/**
 * Workspace Projects (trinity-enterprise#661) — the domain store.
 *
 * Every request goes through the Workspace client (`portalHttp` + the portal
 * store's auth header), like `portalRailFeeds.js`, so the credential decision
 * stays in one place. The server is the authority for every permission and
 * answers a missing project and a hidden one with the same 404.
 *
 * Loading means "no verdict yet" (`listLoaded` / `pageState().loaded`); a failed
 * REFRESH keeps what is on screen and records the error for the stale banner
 * (ent#253) — it never replaces data with a synthetic empty list.
 */
const BASE = '/api/enterprise/projects'

export const useProjectsStore = defineStore('projects', () => {
  const list = ref([])
  const listLoaded = ref(false)
  const listLoading = ref(false)
  const listError = ref(null)
  const pages = ref({})     // id → { data, loaded, loading, error, notFound }
  const links = ref({})     // `${kind}:${id}` → project | null
  // ent#1169: the last create/edit answer's `steward_skills` outcome, by project
  // id — the Steward card says WHY the set wasn't assigned (awaiting_owner,
  // not_permitted, failed, unavailable), which the page read alone can't.
  const stewardOutcomes = ref({})
  const _keepStewardOutcome = (id, data) => {
    if (id && data?.steward_skills) stewardOutcomes.value = { ...stewardOutcomes.value, [id]: data.steward_skills }
  }

  const headers = () => useClientPortalStore().authHeader

  async function fetchList() {
    listLoading.value = true
    try {
      const { data } = await portalHttp.get(BASE, {
        headers: headers(), params: { scope: 'all', include_archived: true },
      })
      list.value = data.projects || []
      listLoaded.value = true
      listError.value = null
    } catch (err) {
      listError.value = projectErrorMessage(err)
    } finally {
      listLoading.value = false
    }
  }

  function pageState(id) {
    return pages.value[id] || { data: null, loaded: false, loading: false, error: null, notFound: false }
  }

  async function fetchProject(id) {
    const prev = pageState(id)
    pages.value = { ...pages.value, [id]: { ...prev, loading: true } }
    try {
      const { data } = await portalHttp.get(`${BASE}/${encodeURIComponent(id)}`, { headers: headers() })
      pages.value = { ...pages.value, [id]: { data, loaded: true, loading: false, error: null, notFound: false } }
    } catch (err) {
      const notFound = err?.response?.status === 404
      pages.value = {
        ...pages.value,
        [id]: { ...prev, loading: false, error: projectErrorMessage(err), notFound },
      }
    }
  }

  function forLink(kind, target) {
    return links.value[`${kind}:${target}`]
  }

  async function fetchForLink(kind, target) {
    if (!target) return
    try {
      const { data } = await portalHttp.get(
        `${BASE}/by-link/${kind}/${encodeURIComponent(target)}`, { headers: headers() })
      links.value = { ...links.value, [`${kind}:${target}`]: data.project || null }
    } catch {
      // Unknown stays unknown: the header offers nothing until a read succeeds.
    }
  }

  function _dropLink(kind, target) {
    const next = { ...links.value }
    delete next[`${kind}:${target}`]
    links.value = next
  }

  // Writes rethrow the server's error untouched: the calling control renders
  // `projectErrorMessage(err)` next to itself (principle 18).
  async function create(body) {
    const { data } = await portalHttp.post(BASE, body, { headers: headers() })
    _keepStewardOutcome(data?.id, data)
    if (body.link_thread_id) _dropLink('thread', body.link_thread_id)
    void fetchList()
    return data
  }

  async function _write(id, fn) {
    const out = await fn()
    await fetchProject(id)
    void fetchList()
    return out
  }

  const url = (id, tail = '') => `${BASE}/${encodeURIComponent(id)}${tail}`

  const update = (id, body) =>
    _write(id, async () => {
      const out = await portalHttp.patch(url(id), body, { headers: headers() })
      _keepStewardOutcome(id, out?.data)
      return out
    })
  // ent#1235: a person starts and stops the project (P11); the mode is a PATCH.
  const startProject = (id) => _write(id, () => portalHttp.post(url(id, '/start'), {}, { headers: headers() }))
  const stopProject = (id) => _write(id, () => portalHttp.post(url(id, '/stop'), {}, { headers: headers() }))
  const setMode = (id, mode) => update(id, { mode })
  const setArchived = (id, archived) =>
    _write(id, () => portalHttp.post(url(id, archived ? '/archive' : '/restore'), {}, { headers: headers() }))
  const addMember = (id, email) =>
    _write(id, () => portalHttp.post(url(id, '/members'), { email }, { headers: headers() }))
  const removeMember = (id, email) =>
    _write(id, () => portalHttp.delete(url(id, `/members/${encodeURIComponent(email)}`), { headers: headers() }))
  const addAgent = (id, agentName) =>
    _write(id, () => portalHttp.post(url(id, '/agents'), { agent_name: agentName }, { headers: headers() }))
  const removeAgent = (id, agentName) =>
    _write(id, () => portalHttp.delete(url(id, `/agents/${encodeURIComponent(agentName)}`), { headers: headers() }))

  async function link(id, kind, target) {
    await portalHttp.post(url(id, '/links'), { kind, target_id: target }, { headers: headers() })
    _dropLink(kind, target)
    void fetchList()
  }

  async function unlink(id, kind, target) {
    await portalHttp.delete(url(id, `/links/${kind}/${encodeURIComponent(target)}`), { headers: headers() })
    links.value = { ...links.value, [`${kind}:${target}`]: null }
    void fetchList()
  }

  // --- v2: tasks, the log, items, guests, import -----------------------------------
  // Reads return data for the calling panel to hold (they are per-project and
  // short-lived); writes rethrow untouched, like the rest of this store.
  const tasks = ref({})    // id → { rows, loaded, error }
  const logs = ref({})     // id → { rows, loaded, error }

  // trinity-enterprise#957: the tasks are now polled, so two reads of one
  // project can be in flight at once. Only the NEWEST one writes — on success
  // and on failure alike, or a slow old read (or its error) lands on top of
  // rows a later read already proved fresh. Counted per project.
  const _tasksSeq = {}

  async function fetchTasks(id) {
    const seq = (_tasksSeq[id] = (_tasksSeq[id] || 0) + 1)
    try {
      const { data } = await portalHttp.get(url(id, '/tasks'), { headers: headers(), params: { status: 'all' } })
      if (seq !== _tasksSeq[id]) return
      tasks.value = { ...tasks.value, [id]: { rows: data.tasks || [], loaded: true, error: null, errorStatus: null } }
    } catch (err) {
      if (seq !== _tasksSeq[id]) return
      const prev = tasks.value[id] || { rows: [], loaded: false, error: null }
      tasks.value = { ...tasks.value, [id]: { ...prev, error: projectErrorMessage(err), errorStatus: err?.response?.status || null } }
    }
  }

  // The server's answer to a write IS the task: put it in the rows at once, so
  // a saved change never reads as unsaved while (or if) the re-read fails.
  function _mergeTask(id, task) {
    const cur = tasks.value[id]
    if (!cur || !task || !task.id) return
    const rows = cur.rows.some((r) => r.id === task.id)
      ? cur.rows.map((r) => (r.id === task.id ? { ...r, ...task } : r))
      : [...cur.rows, task]
    tasks.value = { ...tasks.value, [id]: { ...cur, rows } }
  }

  async function fetchTask(id, taskId) {
    const { data } = await portalHttp.get(url(id, `/tasks/${encodeURIComponent(taskId)}`), { headers: headers() })
    return data
  }

  async function createTask(id, body) {
    const { data } = await portalHttp.post(url(id, '/tasks'), body, { headers: headers() })
    await Promise.all([fetchTasks(id), fetchLog(id)])
    return data
  }

  async function updateTask(id, taskId, body) {
    const { data } = await portalHttp.patch(url(id, `/tasks/${encodeURIComponent(taskId)}`), body, { headers: headers() })
    _mergeTask(id, data)
    await Promise.all([fetchTasks(id), fetchLog(id)])
    return data
  }

  // A board move (trinity-enterprise#957): the status, answered as soon as
  // the server has it. The re-reads reconcile in the background — a card does
  // not stay "Saving…" for the length of a log read it does not need.
  // `note` rides along when the move needs its reason on record (Done without a
  // wait for verification): it lands in the task's log beside the status change.
  async function moveTask(id, taskId, status, note = '') {
    const body = note ? { status, note } : { status }
    const { data } = await portalHttp.patch(url(id, `/tasks/${encodeURIComponent(taskId)}`), body, { headers: headers() })
    _mergeTask(id, data)
    void Promise.all([fetchTasks(id), fetchLog(id)])
    return data
  }

  async function addTaskNote(id, taskId, body) {
    await portalHttp.post(url(id, `/tasks/${encodeURIComponent(taskId)}/log`), { body }, { headers: headers() })
  }

  // trinity-enterprise#1240: the GitHub issues and PRs a task's work happens in.
  // The answer is the task's full ref list; the row in the list follows it.
  function _setTaskRefs(id, taskId, refs) {
    const st = tasks.value[id]
    if (!st) return
    const rows = st.rows.map((t) => (t.id === taskId ? { ...t, refs } : t))
    tasks.value = { ...tasks.value, [id]: { ...st, rows } }
  }

  async function addTaskRefs(id, taskId, refs) {
    const { data } = await portalHttp.post(url(id, `/tasks/${encodeURIComponent(taskId)}/refs`), { refs }, { headers: headers() })
    _setTaskRefs(id, taskId, data.refs || [])
    return data
  }

  async function removeTaskRef(id, taskId, refId) {
    await portalHttp.delete(url(id, `/tasks/${encodeURIComponent(taskId)}/refs/${encodeURIComponent(refId)}`), { headers: headers() })
    const row = (tasks.value[id]?.rows || []).find((t) => t.id === taskId)
    if (row) _setTaskRefs(id, taskId, (row.refs || []).filter((r) => r.id !== refId))
  }

  async function fetchLog(id) {
    const prev = logs.value[id] || { rows: [], loaded: false, error: null }
    try {
      const { data } = await portalHttp.get(url(id, '/log'), { headers: headers(), params: { limit: 200 } })
      logs.value = { ...logs.value, [id]: { rows: data.entries || [], loaded: true, error: null } }
    } catch (err) {
      logs.value = { ...logs.value, [id]: { ...prev, error: projectErrorMessage(err) } }
    }
  }

  async function addLogEntry(id, body) {
    const { data } = await portalHttp.post(url(id, '/log'), body, { headers: headers() })
    await fetchLog(id)
    return data
  }

  const addGuest = (id, email) =>
    _write(id, () => portalHttp.post(url(id, '/guests'), { email }, { headers: headers() }))
  const addItem = (id, kind, targetId, audience = 'members') =>
    _write(id, () => portalHttp.post(url(id, '/items'), { kind, target_id: targetId, audience }, { headers: headers() }))
  const setItemAudience = (id, kind, targetId, audience) =>
    _write(id, () => portalHttp.patch(url(id, `/items/${kind}/${encodeURIComponent(targetId)}`), { audience }, { headers: headers() }))
  const removeItem = (id, kind, targetId) =>
    _write(id, () => portalHttp.delete(url(id, `/items/${kind}/${encodeURIComponent(targetId)}`), { headers: headers() }))

  async function fetchReport(id, reportId) {
    const { data } = await portalHttp.get(url(id, `/reports/${encodeURIComponent(reportId)}`), { headers: headers() })
    return data
  }

  /**
   * What this person could add to a project from one agent: their files,
   * reports and decisions there. Read through the same Workspace routes the
   * Files tab, the Info tab and the decisions panel use — directly, so the
   * picker never writes the Workspace store's single-agent panel state.
   */
  async function myItems(agentName) {
    const base = `/api/enterprise/client-portal/agents/${encodeURIComponent(agentName)}`
    const get = (path) => portalHttp.get(`${base}${path}`, { headers: headers() }).then((r) => r.data)
    const [files, reports, decisions] = await Promise.allSettled([
      get('/documents'), get('/reports'), get('/decisions'),
    ])
    return {
      files: files.status === 'fulfilled' ? (files.value.documents || []) : null,
      reports: reports.status === 'fulfilled' ? (reports.value.reports || []) : null,
      decisions: decisions.status === 'fulfilled' ? (decisions.value.decisions || []) : null,
    }
  }

  async function importCandidates(agentName) {
    const { data } = await portalHttp.get(`${BASE}/import/candidates`, { headers: headers(), params: { agent: agentName } })
    return data.candidates || []
  }

  async function importProject(agentName, path) {
    const { data } = await portalHttp.post(`${BASE}/import`, { agent_name: agentName, path }, { headers: headers() })
    void fetchList()
    return data
  }

  // --- v3: health, linked metrics, the steward digest --------------------------------
  const setHealth = (id, state, note) =>
    _write(id, () => portalHttp.post(url(id, '/health'), { state, note }, { headers: headers() }))
  const linkMetric = (id, agentName, metric) =>
    _write(id, () => portalHttp.post(url(id, '/metrics'), { agent_name: agentName, metric }, { headers: headers() }))
  const unlinkMetric = (id, agentName, metric) =>
    _write(id, () => portalHttp.delete(
      url(id, `/metrics/${encodeURIComponent(agentName)}/${encodeURIComponent(metric)}`), { headers: headers() }))

  // --- ent#1132 part 2 / ent#1131 part 2: a person asks the project's creator --------
  const requestToJoin = (id, note) =>
    _write(id, () => portalHttp.post(url(id, '/join-request'), note ? { note } : {}, { headers: headers() }))
  const requestAgent = (id, agentName) =>
    _write(id, () => portalHttp.post(url(id, '/agent-requests'), { agent_name: agentName }, { headers: headers() }))

  // --- ent#1161: the steward agent's seat, asked for and given on the project -------
  const requestStewardAccess = (id) =>
    _write(id, () => portalHttp.post(url(id, '/steward-access/request'), {}, { headers: headers() }))
  const grantStewardAccess = (id, email) =>
    _write(id, () => portalHttp.post(url(id, '/steward-access/grant'), { email }, { headers: headers() }))
  // trinity-enterprise#1236: a person starts or pauses Trinity waking the agent
  // steward. Through `_write`, so the card re-reads its `wake` block.
  const startStewardWake = (id) =>
    _write(id, () => portalHttp.post(url(id, '/steward/resume'), {}, { headers: headers() }))
  const pauseStewardWake = (id) =>
    _write(id, () => portalHttp.post(url(id, '/steward/pause'), {}, { headers: headers() }))

  // --- ent#1193: context — briefs prepared for the project's agents ------------------
  // Writes go through `_write`, so the page read (its `context` list) refreshes.
  const writeContext = (id, body) =>
    _write(id, () => portalHttp.post(url(id, '/context'), body, { headers: headers() }))
  async function readContext(id, itemId) {
    const { data } = await portalHttp.get(url(id, `/context/${encodeURIComponent(itemId)}`), { headers: headers() })
    return data
  }
  const updateContext = (id, itemId, body) =>
    _write(id, () => portalHttp.patch(url(id, `/context/${encodeURIComponent(itemId)}`), body, { headers: headers() }))
  const deleteContext = (id, itemId) =>
    _write(id, () => portalHttp.delete(url(id, `/context/${encodeURIComponent(itemId)}`), { headers: headers() }))
  const markItemForAgents = (id, kind, targetId, forAgents) =>
    _write(id, () => portalHttp.put(url(id, `/items/${kind}/${encodeURIComponent(targetId)}/for-agents`),
      { for_agents: forAgents }, { headers: headers() }))

  // --- ent#1195: start a task's run from the project ------------------------------
  async function startPreview(id, taskId, agent = null) {
    const { data } = await portalHttp.get(url(id, `/tasks/${encodeURIComponent(taskId)}/start-preview`),
      { headers: headers(), params: agent ? { agent } : {} })
    return data
  }
  async function startTask(id, taskId, agentName, previewSha = null) {
    const { data } = await portalHttp.post(url(id, `/tasks/${encodeURIComponent(taskId)}/start`),
      { agent_name: agentName, ...(previewSha ? { preview_sha: previewSha } : {}) }, { headers: headers() })
    return data
  }

  // ent#1235: the proposed runs waiting for a person's go, and the batch decision.
  // Each id is decided on its own; the answer is one outcome per id.
  async function fetchRunsWaiting(id) {
    const { data } = await portalHttp.get(url(id, '/runs/waiting'), { headers: headers() })
    return data.runs || []
  }
  async function decideRuns(id, verb, ids, note = null) {
    const { data } = await portalHttp.post(url(id, `/runs/${verb === 'decline' ? 'decline' : 'approve'}`),
      { ids, ...(note ? { note } : {}) }, { headers: headers() })
    void fetchTasks(id)
    return data.results || []
  }

  async function metricOptions(id) {
    const { data } = await portalHttp.get(url(id, '/metric-options'), { headers: headers() })
    return data.agents || []
  }

  // trinity-enterprise#1155 — the person fields' scoped reads (one people search).
  async function fetchProjectPeople(id, q, purpose) {
    const { data } = await portalHttp.get(url(id, '/people'), { headers: headers(), params: { q, purpose } })
    return data.people || []
  }
  async function fetchCompanyPeople(q) {
    const { data } = await portalHttp.get(`${BASE}/people`, { headers: headers(), params: { q } })
    return data.people || []
  }

  // ent#1169: whether the library offers the project-management set, and per
  // agent I can use whether it holds it and whether I may give it. A read for
  // the dialog's hint; a failure leaves the hint out, never blocks creating.
  async function fetchStewardOptions() {
    const { data } = await portalHttp.get(`${BASE}/steward-options`, { headers: headers() })
    return data
  }

  const stewarding = ref({ rows: [], loaded: false, loading: false, error: null })

  async function fetchStewarding() {
    stewarding.value = { ...stewarding.value, loading: true }
    try {
      const { data } = await portalHttp.get(`${BASE}/stewarding`, { headers: headers() })
      stewarding.value = { rows: data.projects || [], loaded: true, loading: false, error: null }
    } catch (err) {
      stewarding.value = { ...stewarding.value, loading: false, error: projectErrorMessage(err) }
    }
  }

  // ent#1105: a project's runs. Keyed by project; `filters` is what the rows
  // were fetched with, so a filter change replaces them and "Load more"
  // (`{ more: true }`) appends the next page via `next_cursor`. A failed
  // refetch keeps what is on screen and sets `error` (stale, never empty).
  const runs = ref({})     // id → { rows, totals, window, retention_days, next_cursor, filters, loaded, loading, error }

  async function fetchRuns(id, filters = {}, { more = false } = {}) {
    const prev = runs.value[id] || { rows: [], totals: null, next_cursor: null, filters: {}, loaded: false, loading: false, error: null }
    const params = {}
    for (const [k, v] of Object.entries(filters)) {
      if (v !== undefined && v !== null && v !== '') params[k] = v
    }
    if (more && prev.next_cursor) params.cursor = prev.next_cursor
    runs.value = { ...runs.value, [id]: { ...prev, loading: true } }
    try {
      const { data } = await portalHttp.get(url(id, '/executions'), { headers: headers(), params })
      const rows = more ? [...prev.rows, ...(data.rows || [])] : (data.rows || [])
      runs.value = { ...runs.value, [id]: {
        rows, totals: data.totals || null, window: data.window || null,
        retention_days: data.retention_days ?? null, next_cursor: data.next_cursor || null,
        filters: { ...filters }, loaded: true, loading: false, error: null,
      } }
    } catch (err) {
      runs.value = { ...runs.value, [id]: { ...prev, loading: false, error: projectErrorMessage(err) } }
    }
  }

  return {
    list, listLoaded, listLoading, listError, stewardOutcomes,
    fetchList, pageState, fetchProject, forLink, fetchForLink,
    create, update, setArchived, startProject, stopProject, setMode, fetchRunsWaiting, decideRuns, addMember, removeMember, addAgent, removeAgent, link, unlink,
    tasks, logs, fetchTasks, fetchTask, createTask, updateTask, moveTask, addTaskNote, fetchLog, addLogEntry,
    addTaskRefs, removeTaskRef,
    addGuest, addItem, setItemAudience, removeItem, fetchReport, myItems, importCandidates, importProject,
    setHealth, linkMetric, unlinkMetric, metricOptions, stewarding, fetchStewarding,
    requestStewardAccess, grantStewardAccess, startStewardWake, pauseStewardWake, requestToJoin, requestAgent,
    startPreview, startTask,
    writeContext, readContext, updateContext, deleteContext, markItemForAgents,
    fetchProjectPeople, fetchCompanyPeople, fetchStewardOptions,
    runs, fetchRuns,
  }
})
