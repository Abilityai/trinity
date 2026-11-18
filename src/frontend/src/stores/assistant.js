/**
 * The assistant panel's store.
 *
 * One conversation with the instance's assistant agent, behind the
 * entitlement-gated `/api/enterprise/assistant/*`. On an OSS or unentitled
 * build those routes answer 404/403 and `absent` stays true, so the panel
 * never renders and the help widget stays as it is.
 *
 * Before the first conversation the admin chooses who serves the assistant:
 *
 *   - Trinity (the gateway): they register this instance with their email and
 *     a one-time code. It works at once, with an allowance on the free plan.
 *   - their own Claude (the basic helper): no registration.
 *
 * The plan and the allowance are whatever the gateway reports; nothing here
 * counts or caches them.
 *
 * The chat goes through the ordinary task endpoint with the person's session.
 * That endpoint is stateless, so each turn carries the page, the fleet and the
 * conversation (utils/assistantContext.js). The conversation lives in this
 * store only. A change the assistant proposes is a card the person confirms;
 * the page makes it and reads it back (utils/assistantCards.js).
 */
import { defineStore } from 'pinia'
import api from '../api'
import { buildFleet, buildPrompt, describePage } from '../utils/assistantContext'
import { STATUS_URL, cardLabel, cardNote, outcomeLine, parseReply, runCard } from '../utils/assistantCards'
import { useAgentsStore } from './agents'

export const FEATURE_ID = 'assistant'

const BASE = '/api/enterprise/assistant'
const POLL_MS = 2000
const POLL_LIMIT = 450 // 15 minutes
const PROVISION_TIMEOUT_MS = 180000
const TERMINAL = ['success', 'failed', 'cancelled']

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

let nextId = 1
const newId = () => `m${nextId++}`

const words = (error, fallback) => {
  const detail = error?.response?.data?.detail
  return (typeof detail === 'string' ? detail : detail?.message) || fallback
}

export const useAssistantStore = defineStore('assistant', {
  state: () => ({
    agentName: null,
    absent: false,       // 404/403 seen: no assistant on this build
    status: null,        // GET /status; null until first read
    statusError: '',
    door: { step: 'email', email: '', devCode: '', busy: false, error: '', open: false },
    catalog: { templates: [], playbooks: [] },
    ready: false,        // the agent exists, is running and has its instructions
    preparing: false,
    prepareError: '',
    messages: [],        // { id, role: 'user' | 'assistant' | 'note', content, cards? }
    sending: false,
    sendError: '',
    upgrading: false,
    fleet: [],
  }),

  getters: {
    mode: (state) => state.status?.mode || null,
    /** The door shows until a mode is chosen, or when asked for from the basic helper. */
    atDoor: (state) => Boolean(state.status) && (!state.status.mode || state.door.open),
    onGateway: (state) => state.status?.mode === 'gateway',
    limitReached: (state) => state.status?.mode === 'gateway' && Boolean(state.status?.limit_reached),
    /** One line for the header: who is serving, and how much of the allowance is used. */
    planLine: (state) => {
      const s = state.status
      if (!s?.mode) return 'The agent that helps you run this instance'
      if (s.mode === 'local') return 'Basic helper · your own Claude'
      if (s.gateway_reachable === false) return "Trinity · can't reach the gateway"
      if (s.plan === 'pro') return 'Trinity Pro'
      if (s.agent_limit == null) return 'Free plan'
      return `Free plan · ${s.agents_built ?? 0} of ${s.agent_limit} agents built`
    },
  },

  actions: {
    async loadStatus() {
      try {
        const { data } = await api.get(STATUS_URL)
        this.status = data
        this.agentName = data.agent_name
        this.statusError = ''
      } catch (error) {
        const code = error?.response?.status
        if (code === 404 || code === 403) {
          this.absent = true
          return
        }
        this.statusError = "Couldn't load the assistant. Check your connection and try again."
      }
    },

    /** Step 1 of registering: the gateway sends a code to this email. */
    async register(email) {
      const address = String(email || '').trim().toLowerCase()
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(address)) {
        this.door.error = 'Enter a full email address, like you@example.com.'
        return
      }
      this.door.busy = true
      this.door.error = ''
      try {
        const { data } = await api.post(`${BASE}/register`, { email: address })
        this.door.email = data.email || address
        this.door.devCode = data.dev_code || ''
        this.door.step = 'code'
      } catch (error) {
        this.door.error = words(error, "Couldn't send the code. Try again.")
      } finally {
        this.door.busy = false
      }
    },

    /** Step 2: the code proves the email; the instance is now registered to it. */
    async verify(code) {
      const digits = String(code || '').trim()
      if (!digits) {
        this.door.error = 'Enter the code from the email.'
        return
      }
      this.door.busy = true
      this.door.error = ''
      try {
        const { data } = await api.post(`${BASE}/verify`, { email: this.door.email, code: digits })
        this.status = data
        this.enter()
      } catch (error) {
        this.door.error = words(error, "That code didn't work. Check it and try again.")
      } finally {
        this.door.busy = false
      }
    },

    /** The other door: the basic helper on the instance's own Claude. */
    async useOwnClaude() {
      this.door.busy = true
      this.door.error = ''
      try {
        const { data } = await api.post(`${BASE}/mode`, { mode: 'local' })
        this.status = data
        this.enter()
      } catch (error) {
        this.door.error = words(error, "Couldn't switch. Try again.")
      } finally {
        this.door.busy = false
      }
    },

    /** Leave the door for the conversation, on whichever route was chosen. */
    enter() {
      this.door = { step: 'email', email: '', devCode: '', busy: false, error: '', open: false }
      this.ready = false
      this.messages = []
      this.prepare()
    },

    backToEmail() {
      this.door.step = 'email'
      this.door.devCode = ''
      this.door.error = ''
    },

    openDoor() {
      this.door.open = true
    },

    closeDoor() {
      this.door.open = false
    },

    /** Make sure the agent is there. Safe to call on every open. */
    async prepare() {
      if (this.ready || this.preparing || this.absent || !this.mode) return
      this.preparing = true
      this.prepareError = ''
      try {
        const { data } = await api.post(`${BASE}/provision`, {}, { timeout: PROVISION_TIMEOUT_MS })
        this.agentName = data.agent_name
        if (!data.persona_installed) {
          this.prepareError = 'The assistant was created but is not running yet. Try again in a moment.'
          return
        }
        this.ready = true
        this.refreshFleet()
        this.loadCatalog()
      } catch (error) {
        const status = error?.response?.status
        if (status === 404 || status === 403) {
          this.absent = true
          return
        }
        this.prepareError = words(error, "Couldn't set up the assistant. Check that Trinity can create agents, then try again.")
      } finally {
        this.preparing = false
      }
    },

    /** Recipe and playbook names from the gateway. Optional: a failed read shows none. */
    async loadCatalog() {
      if (!this.onGateway) {
        this.catalog = { templates: [], playbooks: [] }
        return
      }
      try {
        const { data } = await api.get(`${BASE}/catalog`)
        this.catalog = { templates: data.templates || [], playbooks: data.playbooks || [] }
      } catch {
        this.catalog = { templates: [], playbooks: [] }
      }
    },

    /** The fleet as this person's session sees it. A failed read keeps the last one. */
    async refreshFleet() {
      const read = async (url, fallback) => {
        try {
          return (await api.get(url)).data ?? fallback
        } catch {
          return fallback
        }
      }
      const [agents, autonomy, schedules] = await Promise.all([
        read('/api/agents', null),
        read('/api/agents/autonomy-status', {}),
        read('/api/ops/schedules', {}),
      ])
      if (!Array.isArray(agents)) return this.fleet
      this.fleet = buildFleet(agents, autonomy, schedules?.schedules || [], this.agentName)
      return this.fleet
    },

    /**
     * One turn. `shown` false sends a line the page wrote (a card's outcome):
     * it shows as a short note, not as something the person said.
     */
    async send(text, route, { shown = true } = {}) {
      const message = String(text || '').trim()
      if (!message || this.sending || !this.ready) return
      this.sendError = ''
      const history = this.messages
        .filter((m) => m.role !== 'note' || m.forAgent)
        .map((m) => ({ role: m.role === 'assistant' ? 'assistant' : 'user', content: m.content }))
      this.messages.push({ id: newId(), role: shown ? 'user' : 'note', content: message, forAgent: !shown })
      this.sending = true
      try {
        const fleet = await this.refreshFleet()
        const prompt = buildPrompt({ page: describePage(route), fleet, history, message })
        const { data } = await api.post(`/api/agents/${encodeURIComponent(this.agentName)}/task`, {
          message: prompt,
          async_mode: true,
        })
        if (!data.execution_id) throw new Error('no execution id')

        const execution = await this.awaitExecution(data.execution_id)
        if (!execution) {
          this.sendError = 'The assistant is taking longer than expected. Its reply will be in its Tasks list when it finishes.'
          return
        }
        if (execution.status !== 'success') {
          this.sendError = execution.error
            ? `The assistant couldn't finish: ${execution.error}`
            : "The assistant couldn't finish that. Try again."
          return
        }
        // It may have created or changed agents: read again before validating
        // cards against the fleet, and let the page behind the panel catch up.
        const after = await this.refreshFleet()
        useAgentsStore().fetchAgents().catch(() => {})
        const { text: reply, cards } = parseReply(execution.response, after)
        this.messages.push({
          id: newId(),
          role: 'assistant',
          content: reply || 'Done.',
          cards: cards.map((card) => ({
            id: newId(), card, label: cardLabel(card, after), note: cardNote(card), state: 'open', reason: '',
          })),
        })
      } catch (error) {
        const status = error?.response?.status
        this.sendError = status === 503
          ? 'The assistant is not running. Start it from the Agents page, then try again.'
          : "Couldn't reach the assistant. Check your connection and try again."
      } finally {
        this.sending = false
        // The gateway counts what was built during the turn; show its number.
        if (this.onGateway) this.loadStatus()
      }
    },

    async awaitExecution(executionId) {
      const url = `/api/agents/${encodeURIComponent(this.agentName)}/executions/${encodeURIComponent(executionId)}`
      for (let i = 0; i < POLL_LIMIT; i++) {
        await wait(POLL_MS)
        try {
          const { data } = await api.get(url)
          if (TERMINAL.includes(data?.status)) return data
        } catch {
          // a missed poll is not an outcome; keep waiting
        }
      }
      return null
    },

    /** The person answered a card. Confirm makes the change as them. */
    async answerCard(messageId, cardId, confirm, route) {
      const entry = this.messages.find((m) => m.id === messageId)?.cards?.find((c) => c.id === cardId)
      if (!entry || entry.state !== 'open') return
      // On an instance that has not registered, "upgrade" means "start": it
      // opens the door rather than a checkout, and nothing is told to the agent.
      if (confirm && entry.card.action === 'upgrade' && !this.status?.registered) {
        entry.state = 'declined'
        entry.reason = 'Enter your email to start.'
        this.openDoor()
        return
      }
      let kind = 'declined'
      let reason = ''
      if (confirm) {
        entry.state = 'running'
        const result = await runCard(api, entry.card, this.fleet)
        kind = result.kind
        reason = result.reason || ''
        useAgentsStore().fetchAgents().catch(() => {})
        if (entry.card.action === 'upgrade') await this.loadStatus()
      }
      entry.state = kind
      entry.reason = reason
      await this.send(outcomeLine(kind, entry.label, reason), route, { shown: false })
    },

    /** The upgrade button outside a card: same route, same read-back. */
    async upgrade(route) {
      if (this.upgrading) return
      this.upgrading = true
      this.sendError = ''
      try {
        const { data } = await api.post(`${BASE}/upgrade`, {})
        this.status = data
        if (data.plan === 'pro' && this.ready) {
          await this.send(outcomeLine('done', 'Upgrade this instance to Trinity Pro'), route, { shown: false })
        }
      } catch (error) {
        this.sendError = words(error, "Couldn't upgrade. Try again.")
      } finally {
        this.upgrading = false
      }
    },

    newConversation() {
      if (this.sending) return
      this.messages = []
      this.sendError = ''
    },
  },
})
