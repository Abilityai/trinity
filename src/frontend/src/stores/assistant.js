/**
 * The assistant panel's store.
 *
 * The assistant runs on Trinity's side. This store carries one conversation
 * to it through the instance's backend (`/api/enterprise/assistant/*`), which
 * also runs whatever the assistant needs to look up, as the signed-in admin.
 * On a build without those routes they answer 404/403, `absent` stays true and
 * the panel is never offered.
 *
 * Nothing is sent anywhere until the person sends a message. Reading the
 * status does not contact Trinity for an instance that never has.
 *
 * A turn is a few short requests, not one long one: each answer is either the
 * reply or "working", with a line saying what is being looked at.
 *
 * A change the assistant proposes is a card the person confirms; the page
 * makes it and reads it back (utils/assistantCards.js). The allowance and the
 * plan are whatever the gateway reports; nothing here counts or caches them.
 */
import { defineStore } from 'pinia'
import api from '../api'
import { buildContext, buildFleet } from '../utils/assistantContext'
import { acceptCards, buildOutcome, cardLabel, cardNote, outcomeLine, runCard } from '../utils/assistantCards'
import { useAgentsStore } from './agents'

export const FEATURE_ID = 'assistant'

const BASE = '/api/enterprise/assistant'
const STEP_TIMEOUT_MS = 260000
const BUILD_TIMEOUT_MS = 600000
const MAX_STEPS = 40

let nextId = 1
const newId = () => `m${nextId++}`

const detail = (error) => error?.response?.data?.detail
const words = (error, fallback) => {
  const d = detail(error)
  return (typeof d === 'string' ? d : d?.message) || fallback
}

const closedAccount = () => ({ step: null, email: '', devCode: '', terms: null, busy: false, error: '' })

export const useAssistantStore = defineStore('assistant', {
  state: () => ({
    absent: false,       // 404/403 seen: no assistant on this build
    status: null,        // GET /status; null until first read
    statusError: '',
    conversationId: null,
    messages: [],        // { id, role: 'user' | 'assistant' | 'note', content, cards? }
    sending: false,
    activity: '',        // what is being looked at right now
    sendError: '',
    limit: null,         // the allowance, when the last answer was "limit"
    account: closedAccount(),
    fleet: [],
  }),

  getters: {
    available: (state) => !state.absent && Boolean(state.status?.available),
    subscribed: (state) => state.status?.plan === 'pro',
    /** One line for the header: the plan and what is left of the allowance. */
    planLine: (state) => {
      const s = state.status
      if (!s?.contacted) return 'Ask it anything about this instance'
      if (s.reachable === false) return "Can't reach Trinity right now"
      const a = s.allowance
      if (s.plan === 'pro') return 'Trinity Pro'
      if (!a) return 'Free'
      const period = a.period_hours === 24 ? 'today' : a.period_hours === 168 ? 'this week' : 'for now'
      return `Free · ${a.messages_left} ${a.messages_left === 1 ? 'message' : 'messages'} left ${period}`
    },
  },

  actions: {
    async loadStatus() {
      try {
        const { data } = await api.get(`${BASE}/status`)
        this.status = data
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
      this.fleet = buildFleet(agents, autonomy, schedules?.schedules || [])
      return this.fleet
    },

    /**
     * One turn. `shown` false sends a line the page wrote (a card's outcome):
     * it shows as a short note, not as something the person said, and it is
     * not counted against their messages.
     */
    async send(text, route, { shown = true } = {}) {
      const message = String(text || '').trim()
      if (!message || this.sending) return
      this.sendError = ''
      this.limit = null
      this.messages.push({ id: newId(), role: shown ? 'user' : 'note', content: message })
      this.sending = true
      this.activity = ''
      try {
        const fleet = await this.refreshFleet()
        let { data } = await api.post(`${BASE}/turn`, {
          conversation_id: this.conversationId,
          message,
          context: buildContext(route, fleet),
          counted: shown,
        }, { timeout: STEP_TIMEOUT_MS })

        for (let step = 0; data.status === 'working' && step < MAX_STEPS; step++) {
          this.conversationId = data.conversation_id
          this.activity = (data.activity || []).join(' · ')
          ;({ data } = await api.post(`${BASE}/turn/continue`,
            { conversation_id: data.conversation_id }, { timeout: STEP_TIMEOUT_MS }))
        }
        this.conversationId = data.conversation_id || this.conversationId
        if (data.allowance && this.status) this.status = { ...this.status, contacted: true, reachable: true, allowance: data.allowance, plan: data.allowance.plan }

        if (data.status === 'limit') {
          this.limit = data.allowance || {}
          return
        }
        if (data.status === 'working') {
          this.sendError = 'That took more steps than expected. Ask me to continue.'
          return
        }
        // It may have added a schedule: read again before checking cards
        // against the fleet, and let the page behind the panel catch up.
        const after = await this.refreshFleet()
        useAgentsStore().fetchAgents().catch(() => {})
        const cards = acceptCards(data.cards, after)
        this.messages.push({
          id: newId(),
          role: 'assistant',
          content: data.text || (cards.length ? '' : 'Done.'),
          cards: cards.map((card) => ({
            id: newId(), card, label: cardLabel(card, after), note: cardNote(card), state: 'open', reason: '',
          })),
        })
      } catch (error) {
        const code = error?.response?.status
        if (code === 404 || code === 403) this.absent = true
        this.sendError = words(error, "Couldn't reach the assistant. Check your connection and try again.")
      } finally {
        this.sending = false
        this.activity = ''
        if (!this.status?.contacted) this.loadStatus()
      }
    },

    /** The person answered a card. Confirm makes the change as them. */
    async answerCard(messageId, cardId, confirm, route) {
      const entry = this.messages.find((m) => m.id === messageId)?.cards?.find((c) => c.id === cardId)
      if (!entry || entry.state !== 'open') return
      if (!confirm) {
        entry.state = 'declined'
        return this.send(outcomeLine('declined', entry.label), route, { shown: false })
      }
      if (entry.card.action === 'upgrade') {
        // Not a change the page can make by itself: the person signs in first.
        entry.state = 'declined'
        entry.reason = 'Continue below.'
        this.openAccount()
        return
      }
      entry.state = 'running'
      let outcome
      if (entry.card.action === 'build') {
        outcome = await this.build(entry.card)
      } else if (entry.card.action === 'tool') {
        outcome = await this.confirmTool(entry.card, entry.label)
      } else {
        const result = await runCard(api, entry.card, this.fleet)
        outcome = { kind: result.kind, reason: result.reason || '', line: outcomeLine(result.kind, entry.label, result.reason) }
      }
      useAgentsStore().fetchAgents().catch(() => {})
      entry.state = outcome.kind
      entry.reason = outcome.reason
      await this.send(outcome.line, route, { shown: false })
    },

    /** Build an approved design. The instance creates the agents as this person. */
    async build(card) {
      try {
        const { data } = await api.post(`${BASE}/designs/${encodeURIComponent(card.design_id)}/build`, {},
          { timeout: BUILD_TIMEOUT_MS })
        return buildOutcome(card, data)
      } catch (error) {
        const code = error?.response?.status
        const reason = words(error, code ? `Trinity refused it (${code}).` : "Couldn't reach Trinity, so it may not have been built.")
        const kind = code && code < 500 ? 'refused' : 'unsure'
        return { kind, reason, line: outcomeLine(kind, cardLabel(card), reason) }
      }
    },

    /** Run a change that cannot be undone, now that the person pressed the button. */
    async confirmTool(card, label) {
      try {
        const { data } = await api.post(`${BASE}/tools/confirm`, { tool: card.tool, args: card.args }, { timeout: STEP_TIMEOUT_MS })
        const said = String(data?.content || '').slice(0, 400)
        if (data?.ok) return { kind: 'done', reason: '', line: `${outcomeLine('done', label)} ${said}`.trim() }
        return { kind: 'refused', reason: said, line: outcomeLine('refused', label, said) }
      } catch (error) {
        const code = error?.response?.status
        const reason = words(error, code ? `Trinity refused it (${code}).` : "Couldn't reach Trinity, so it may not have run.")
        const kind = code && code < 500 ? 'refused' : 'unsure'
        return { kind, reason, line: outcomeLine(kind, label, reason) }
      }
    },

    // -- signing in and subscribing ---------------------------------------
    openAccount() {
      this.account = { ...closedAccount(), step: this.status?.email ? 'subscribe' : 'email' }
    },

    closeAccount() {
      this.account = closedAccount()
    },

    /** Step 1: Trinity sends a code to this email. */
    async startAccount(email) {
      const address = String(email || '').trim().toLowerCase()
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(address)) {
        this.account.error = 'Enter a full email address, like you@example.com.'
        return
      }
      this.account.busy = true
      this.account.error = ''
      try {
        const { data } = await api.post(`${BASE}/account/start`, { email: address })
        this.account = { ...this.account, step: 'code', email: data.email || address, devCode: data.dev_code || '', terms: data.terms || null }
      } catch (error) {
        this.account.error = words(error, "Couldn't send the code. Try again.")
      } finally {
        this.account.busy = false
      }
    },

    /**
     * Step 2: the code proves the email. `accepted` is the person's own tick
     * on the terms; it is passed on as given and never set here.
     */
    async verifyAccount(code, accepted) {
      const digits = String(code || '').trim()
      if (!digits) {
        this.account.error = 'Enter the code from the email.'
        return
      }
      if (!accepted) {
        this.account.error = 'Accept the terms to continue.'
        return
      }
      this.account.busy = true
      this.account.error = ''
      try {
        const { data } = await api.post(`${BASE}/account/verify`, {
          email: this.account.email, code: digits, accept_terms: true, terms_version: this.account.terms?.version || '',
        })
        this.status = { ...this.status, ...data }
        this.account.step = 'subscribe'
      } catch (error) {
        this.account.error = words(error, "That code didn't work. Check it and try again.")
      } finally {
        this.account.busy = false
      }
    },

    /** Step 3: start the subscription. A checkout address, when there is one, opens in a new tab. */
    async subscribe(route) {
      this.account.busy = true
      this.account.error = ''
      try {
        const { data } = await api.post(`${BASE}/subscription/start`, {})
        this.status = { ...this.status, ...data }
        if (data.checkout_url) {
          window.open(data.checkout_url, '_blank', 'noopener')
          this.account.step = 'checkout'
          return
        }
        this.account = closedAccount()
        this.limit = null
        if (this.conversationId) await this.send(outcomeLine('done', 'Subscribe to Trinity Pro'), route, { shown: false })
      } catch (error) {
        this.account.error = words(error, "Couldn't start the subscription. Try again.")
      } finally {
        this.account.busy = false
      }
    },

    backToEmail() {
      this.account = { ...closedAccount(), step: 'email' }
    },

    newConversation() {
      if (this.sending) return
      this.messages = []
      this.conversationId = null
      this.sendError = ''
      this.limit = null
    },
  },
})
