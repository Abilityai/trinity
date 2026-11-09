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
    _write(id, () => portalHttp.patch(url(id), body, { headers: headers() }))
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

  async function fetchTasks(id) {
    const prev = tasks.value[id] || { rows: [], loaded: false, error: null }
    try {
      const { data } = await portalHttp.get(url(id, '/tasks'), { headers: headers(), params: { status: 'all' } })
      tasks.value = { ...tasks.value, [id]: { rows: data.tasks || [], loaded: true, error: null } }
    } catch (err) {
      tasks.value = { ...tasks.value, [id]: { ...prev, error: projectErrorMessage(err) } }
    }
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
    await Promise.all([fetchTasks(id), fetchLog(id)])
    return data
  }

  async function addTaskNote(id, taskId, body) {
    await portalHttp.post(url(id, `/tasks/${encodeURIComponent(taskId)}/log`), { body }, { headers: headers() })
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

  return {
    list, listLoaded, listLoading, listError,
    fetchList, pageState, fetchProject, forLink, fetchForLink,
    create, update, setArchived, addMember, removeMember, addAgent, removeAgent, link, unlink,
    tasks, logs, fetchTasks, fetchTask, createTask, updateTask, addTaskNote, fetchLog, addLogEntry,
    addGuest, addItem, setItemAudience, removeItem, fetchReport, myItems, importCandidates, importProject,
    setHealth, linkMetric, unlinkMetric, metricOptions, stewarding, fetchStewarding,
    requestStewardAccess, grantStewardAccess, requestToJoin, requestAgent,
    startPreview, startTask,
    writeContext, readContext, updateContext, deleteContext, markItemForAgents,
    fetchProjectPeople, fetchCompanyPeople, fetchStewardOptions,
  }
})
