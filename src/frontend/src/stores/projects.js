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

  return {
    list, listLoaded, listLoading, listError,
    fetchList, pageState, fetchProject, forLink, fetchForLink,
    create, update, setArchived, addMember, removeMember, addAgent, removeAgent, link, unlink,
  }
})
