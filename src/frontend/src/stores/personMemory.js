/**
 * Person memory — the owner's view of what an agent remembers about each
 * person (trinity-enterprise#962), for Agent Settings → Guardrails → Memory.
 *
 * Two levels: the people list (everyone with memory on the agent, paged,
 * `GET /api/agents/{name}/person-memory`) and the open person (notes, summary,
 * the write history paged by cursor, and their retrieval-memory facts through
 * the MEM-002 owner routes: list, journal per fact, forget).
 *
 * - The server decides what is undoable; after an Undo the store re-reads as
 *   many history rows as were on screen, so the list keeps its length (no
 *   scroll reset) and every Undo flag comes from one read.
 * - AgentDetail is KeepAlive'd: an agent switch (or closing the person) bumps
 *   a generation counter, and a response for the old one is dropped.
 * - "Loading" is "no data yet" (`hasLoaded` flags), never "a fetch is in
 *   flight"; a failed verb keeps its message beside the control.
 *
 * All HTTP goes through the shared `api` client (Invariant #7).
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'

const PEOPLE_PAGE = 50
const PEOPLE_PAGE_MAX = 200     // the route's cap (`person_memory_service.PEOPLE_PAGE_MAX`)
const HISTORY_PAGE = 20
const FACTS_PAGE = 20
const LINEAGE_LIMIT = 50

function message(e, fallback) {
  const d = e?.response?.data?.detail
  if (d && typeof d === 'object' && d.message) return d.message
  if (typeof d === 'string' && d) return d
  return fallback
}

function base(agentName) {
  return `/api/agents/${encodeURIComponent(agentName)}`
}

export const usePersonMemoryStore = defineStore('personMemory', () => {
  const agentName = ref(null)
  let agentGen = 0

  // ---- the people list ------------------------------------------------------
  const people = ref([])
  const peopleTotal = ref(0)
  const peopleLoaded = ref(false)
  const peopleError = ref('')
  const peopleLoadingMore = ref(false)
  const peopleMoreError = ref('')

  // ---- the open person ------------------------------------------------------
  const personEmail = ref(null)
  let personGen = 0
  const person = ref(null)          // notes, summary, identity, agent_scope
  const personLoaded = ref(false)
  const personError = ref('')
  const writes = ref([])
  const nextBefore = ref(null)
  const writesLoadingMore = ref(false)
  const writesMoreError = ref('')
  const undoing = ref(null)         // write id in flight
  const undoError = ref('')

  // ---- the open person's retrieval-memory facts -----------------------------
  const memoryStatus = ref(null)    // GET …/retrieval-memory (on/off, for the empty reason)
  const facts = ref([])
  const factsLoaded = ref(false)
  const factsError = ref('')
  const factsHasMore = ref(false)
  const factsLoadingMore = ref(false)
  const forgetting = ref(null)
  const forgetError = ref('')
  const lineage = ref({})           // fact id → { state: 'loading'|'ready'|'failed', events }

  function resetPerson(email = null) {
    personGen += 1
    personEmail.value = email
    person.value = null
    personLoaded.value = false
    personError.value = ''
    writes.value = []
    nextBefore.value = null
    writesLoadingMore.value = false
    writesMoreError.value = ''
    undoing.value = null
    undoError.value = ''
    facts.value = []
    factsLoaded.value = false
    factsError.value = ''
    factsHasMore.value = false
    factsLoadingMore.value = false
    forgetting.value = null
    forgetError.value = ''
    lineage.value = {}
  }

  function resetAgent(name = null) {
    agentGen += 1
    agentName.value = name
    people.value = []
    peopleTotal.value = 0
    peopleLoaded.value = false
    peopleError.value = ''
    peopleLoadingMore.value = false
    peopleMoreError.value = ''
    memoryStatus.value = null
    resetPerson(null)
  }

  // ---- people ---------------------------------------------------------------

  async function loadPeople(name) {
    if (agentName.value !== name) resetAgent(name)
    const gen = agentGen
    peopleError.value = ''
    // A quiet re-read (after an Undo or a Forget) keeps as many rows as are on
    // screen, so the list behind the dialog does not shrink back to one page —
    // paged at the route's 200-row cap, as refreshPerson pages the history.
    const want = Math.max(people.value.length, PEOPLE_PAGE)
    try {
      let rows = []
      let total = 0
      let offset = 0
      while (rows.length < want) {
        const limit = Math.min(want - rows.length, PEOPLE_PAGE_MAX)
        const { data } = await api.get(`${base(name)}/person-memory`, { params: { offset, limit } })
        if (gen !== agentGen) return
        const page = data?.people || []
        total = data?.total || 0
        offset += page.length
        const seen = new Set(rows.map((p) => p.user_email))
        const fresh = page.filter((p) => !seen.has(p.user_email))
        rows = rows.concat(fresh)
        // The last page, or a page of only rows already held (the order moved): stop.
        if (page.length < limit || !fresh.length) break
      }
      people.value = rows
      peopleTotal.value = total
      peopleLoaded.value = true
    } catch (e) {
      if (gen !== agentGen) return
      peopleError.value = message(e, 'The request failed. Check your connection and try again.')
    }
  }

  async function loadMorePeople() {
    const name = agentName.value
    if (!name || peopleLoadingMore.value) return
    const gen = agentGen
    peopleLoadingMore.value = true
    peopleMoreError.value = ''
    try {
      const { data } = await api.get(`${base(name)}/person-memory`, {
        params: { offset: people.value.length, limit: PEOPLE_PAGE },
      })
      if (gen !== agentGen) return
      // The order moves as people change; never show one person twice.
      const seen = new Set(people.value.map((p) => p.user_email))
      people.value = people.value.concat((data?.people || []).filter((p) => !seen.has(p.user_email)))
      peopleTotal.value = data?.total ?? peopleTotal.value
    } catch (e) {
      if (gen !== agentGen) return
      peopleMoreError.value = message(e, 'Could not load more people. Try again.')
    } finally {
      if (gen === agentGen) peopleLoadingMore.value = false
    }
  }

  // ---- one person -----------------------------------------------------------

  async function openPerson(email) {
    const name = agentName.value
    if (!name) return
    resetPerson(email)
    const gen = personGen
    try {
      const { data } = await api.get(`${base(name)}/person-memory/person`, { params: { user_email: email } })
      if (gen !== personGen) return
      person.value = data
      writes.value = data?.history?.writes || []
      nextBefore.value = data?.history?.next_before || null
      personLoaded.value = true
    } catch (e) {
      if (gen !== personGen) return
      personError.value = message(e, 'The request failed. Check your connection and try again.')
      return
    }
    if (!person.value?.agent_scope) loadFacts()
  }

  function closePerson() {
    resetPerson(null)
  }

  async function loadMoreWrites() {
    const name = agentName.value
    const email = personEmail.value
    if (!name || !email || !nextBefore.value || writesLoadingMore.value) return
    const gen = personGen
    writesLoadingMore.value = true
    writesMoreError.value = ''
    try {
      const { data } = await api.get(`${base(name)}/person-memory/writes`, {
        params: { user_email: email, before: nextBefore.value, limit: HISTORY_PAGE },
      })
      if (gen !== personGen) return
      const seen = new Set(writes.value.map((w) => w.id))
      writes.value = writes.value.concat((data?.writes || []).filter((w) => !seen.has(w.id)))
      nextBefore.value = data?.next_before || null
    } catch (e) {
      if (gen !== personGen) return
      writesMoreError.value = message(e, 'Could not load more changes. Try again.')
    } finally {
      if (gen === personGen) writesLoadingMore.value = false
    }
  }

  /** Re-read the person and as many history rows as are on screen, in one
   *  pass of pages, so an Undo keeps the list's length and every flag fresh. */
  async function refreshPerson() {
    const name = agentName.value
    const email = personEmail.value
    if (!name || !email) return
    const gen = personGen
    const shown = Math.max(writes.value.length, 1)
    const { data } = await api.get(`${base(name)}/person-memory/person`, { params: { user_email: email } })
    if (gen !== personGen) return
    let rows = data?.history?.writes || []
    let cursor = data?.history?.next_before || null
    while (cursor && rows.length < shown) {
      const page = await api.get(`${base(name)}/person-memory/writes`, {
        params: { user_email: email, before: cursor, limit: HISTORY_PAGE },
      })
      if (gen !== personGen) return
      rows = rows.concat(page.data?.writes || [])
      cursor = page.data?.next_before || null
    }
    person.value = data
    writes.value = rows
    nextBefore.value = cursor
  }

  async function undo(writeId) {
    const name = agentName.value
    const email = personEmail.value
    if (!name || !email || undoing.value) return false
    const gen = personGen
    undoing.value = writeId
    undoError.value = ''
    try {
      await api.post(`${base(name)}/person-memory/writes/${encodeURIComponent(writeId)}/undo`, { user_email: email })
    } catch (e) {
      if (gen === personGen) {
        undoError.value = message(e, 'Could not undo that change. Try again.')
        undoing.value = null
      }
      return false
    }
    try {
      await refreshPerson()
    } catch (e) {
      if (gen === personGen) undoError.value = 'The change was undone, but the list could not be refreshed. Reopen the person to see it.'
    } finally {
      if (gen === personGen) undoing.value = null
    }
    // The people list's counts and "last change" moved too; refresh it quietly.
    if (gen === personGen) loadPeople(name)
    return true
  }

  // ---- facts ----------------------------------------------------------------

  async function loadMemoryStatus(name) {
    try {
      const { data } = await api.get(`${base(name)}/retrieval-memory`)
      if (name === agentName.value) memoryStatus.value = data || null
    } catch {
      // The status only chooses an empty-state reason; without it the plain empty copy shows.
    }
  }

  async function loadFacts() {
    const name = agentName.value
    const email = personEmail.value
    if (!name || !email) return
    const gen = personGen
    factsError.value = ''
    if (!memoryStatus.value) loadMemoryStatus(name)
    try {
      const { data } = await api.get(`${base(name)}/retrieval-memory/facts`, {
        params: { user_email: email, limit: FACTS_PAGE, offset: 0 },
      })
      if (gen !== personGen) return
      facts.value = Array.isArray(data) ? data : []
      factsHasMore.value = facts.value.length === FACTS_PAGE
      factsLoaded.value = true
    } catch (e) {
      if (gen !== personGen) return
      factsError.value = message(e, 'The request failed. Check your connection and try again.')
    }
  }

  async function loadMoreFacts() {
    const name = agentName.value
    const email = personEmail.value
    if (!name || !email || factsLoadingMore.value) return
    const gen = personGen
    factsLoadingMore.value = true
    forgetError.value = ''
    try {
      // The offset is the count still on screen: a forgotten fact left the list
      // AND the store, so the next page starts at the first fact not yet shown.
      const { data } = await api.get(`${base(name)}/retrieval-memory/facts`, {
        params: { user_email: email, limit: FACTS_PAGE, offset: facts.value.length },
      })
      if (gen !== personGen) return
      const page = Array.isArray(data) ? data : []
      const seen = new Set(facts.value.map((f) => f.id))
      facts.value = facts.value.concat(page.filter((f) => !seen.has(f.id)))
      factsHasMore.value = page.length === FACTS_PAGE
    } catch (e) {
      if (gen !== personGen) return
      forgetError.value = message(e, 'Could not load more facts. Try again.')
    } finally {
      if (gen === personGen) factsLoadingMore.value = false
    }
  }

  async function forget(factId) {
    const name = agentName.value
    if (!name || forgetting.value) return false
    const gen = personGen
    forgetting.value = factId
    forgetError.value = ''
    try {
      await api.delete(`${base(name)}/retrieval-memory/facts/${encodeURIComponent(factId)}`)
      if (gen !== personGen) return true
      facts.value = facts.value.filter((f) => f.id !== factId)
      const { [factId]: _gone, ...rest } = lineage.value
      lineage.value = rest
      loadPeople(name)
      return true
    } catch (e) {
      if (gen === personGen) forgetError.value = message(e, 'Could not forget that fact. Try again.')
      return false
    } finally {
      if (gen === personGen) forgetting.value = null
    }
  }

  async function loadLineage(factId) {
    const name = agentName.value
    if (!name) return
    const gen = personGen
    lineage.value = { ...lineage.value, [factId]: { state: 'loading', events: [] } }
    try {
      const { data } = await api.get(`${base(name)}/retrieval-memory/events`, {
        params: { fact_id: factId, limit: LINEAGE_LIMIT },
      })
      if (gen !== personGen) return
      lineage.value = { ...lineage.value, [factId]: { state: 'ready', events: Array.isArray(data) ? data : [] } }
    } catch {
      if (gen !== personGen) return
      lineage.value = { ...lineage.value, [factId]: { state: 'failed', events: [] } }
    }
  }

  return {
    agentName, people, peopleTotal, peopleLoaded, peopleError, peopleLoadingMore, peopleMoreError,
    personEmail, person, personLoaded, personError, writes, nextBefore, writesLoadingMore, writesMoreError,
    undoing, undoError, memoryStatus, facts, factsLoaded, factsError, factsHasMore, factsLoadingMore,
    forgetting, forgetError, lineage,
    resetAgent, loadPeople, loadMorePeople, openPerson, closePerson, loadMoreWrites, refreshPerson, undo,
    loadFacts, loadMoreFacts, forget, loadLineage,
  }
})
