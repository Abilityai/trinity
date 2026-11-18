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
import { acceptCards, buildOutcome, cardLabel, cardNote, outcomeLine, runCard, validBuild } from '../utils/assistantCards'
import { useAgentsStore } from './agents'

export const FEATURE_ID = 'assistant'

const BASE = '/api/enterprise/assistant'
const STEP_TIMEOUT_MS = 260000
const BUILD_TIMEOUT_MS = 600000
const MAX_STEPS = 40
const JOB_POLL_MS = 4000
const RUN_FIRST_POLL_MS = 1500   // a short run is over before the first slow poll
const FOLLOWED = ['job', 'run']    // cards the page follows instead of asking about
const JOB_POLL_LIMIT = 450 // 30 minutes

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

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
    steps: [],           // every step of the turn in hand, in order
    saying: '',          // what it said it is about to do, while it does it
    startedAt: 0,        // when the turn in hand began
    panelOpen: false,    // set by the panel
    unread: 0,           // answers that arrived while the panel was closed
    held: '',            // the message the limit stopped, to ask again after subscribing
    waiting: '',         // what the person wrote before agreeing to the terms; sent once they agree
    agreeing: false,
    agreeError: '',
    termsText: '',       // the trial's terms, read when the person asks to see them
    termsError: '',
    sendError: '',
    limit: null,         // the allowance, when the last answer was "limit"
    account: closedAccount(),
    fleet: [],
  }),

  getters: {
    available: (state) => !state.absent && Boolean(state.status?.available),
    subscribed: (state) => state.status?.plan === 'pro',
    /** Someone at this instance has agreed to the trial's terms. Nothing is sent before that. */
    agreed: (state) => Boolean(state.status?.trial_terms?.accepted),
    /** How many builds and runs are being followed right now. */
    following: (state) => state.messages.reduce((n, m) => n + (m.cards || []).filter((c) => c.state === 'making').length, 0),
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
      this.steps = []
      this.saying = ''
      this.startedAt = Date.now()
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
          this.steps.push(...(data.activity || []))
          if (data.text) this.saying = data.text
          ;({ data } = await api.post(`${BASE}/turn/continue`,
            { conversation_id: data.conversation_id }, { timeout: STEP_TIMEOUT_MS }))
        }
        this.conversationId = data.conversation_id || this.conversationId
        if (data.allowance && this.status) this.status = { ...this.status, contacted: true, reachable: true, allowance: data.allowance, plan: data.allowance.plan }

        if (data.status === 'limit') {
          this.limit = data.allowance || {}
          if (shown) this.held = message          // never reached the assistant: asked again once there is room
          return
        }
        this.held = ''
        if (data.status === 'working') {
          this.sendError = 'That took more steps than expected. Ask me to continue.'
          return
        }
        // It may have added a schedule: read again before checking cards
        // against the fleet, and let the page behind the panel catch up.
        const after = await this.refreshFleet()
        useAgentsStore().fetchAgents().catch(() => {})
        const cards = acceptCards(data.cards, after)
        const reply = {
          id: newId(),
          role: 'assistant',
          content: data.text || (cards.length ? '' : 'Done.'),
          cards: cards.map((card) => ({
            id: newId(), card, label: cardLabel(card, after), note: cardNote(card),
            state: FOLLOWED.includes(card.action) ? 'making' : 'open', reason: '', progress: '',
            since: Date.now(), fraction: null,
          })),
        }
        this.messages.push(reply)
        if (!this.panelOpen) this.unread += 1
        for (const entry of reply.cards) {
          if (entry.card.action === 'job') this.followJob(reply.id, entry.id, route)
          if (entry.card.action === 'run') this.followRun(reply.id, entry.id, route)
        }
      } catch (error) {
        const code = error?.response?.status
        if (detail(error)?.code === 'terms_required') {     // the terms changed, or were never agreed to here
          this.waiting = shown ? message : this.waiting
          this.messages = this.messages.filter((m) => !(m.role === 'user' && m.content === message))
          await this.loadStatus()
          return
        }
        if (code === 404 || code === 403) this.absent = true
        this.sendError = words(error, "Couldn't reach the assistant. Check your connection and try again.")
      } finally {
        this.sending = false
        this.activity = ''
        this.steps = []
        this.saying = ''
        if (!this.status?.contacted) this.loadStatus()
      }
    },

    /**
     * Follow a build in the making until it is a design or it has failed. When
     * it is done the card becomes the design's own Build card; either way the
     * assistant is told, once it is free to hear it.
     */
    async followJob(messageId, cardId, route) {
      const find = () => this.messages.find((m) => m.id === messageId)?.cards?.find((c) => c.id === cardId)
      const jobId = find()?.card.job_id
      const title = find()?.card.title
      let seen = null
      for (let i = 0; i < JOB_POLL_LIMIT; i++) {
        await wait(JOB_POLL_MS)
        const entry = find()
        if (!entry || entry.state !== 'making') return        // the conversation was cleared
        try {
          seen = (await api.get(`${BASE}/jobs/${encodeURIComponent(jobId)}`)).data
        } catch {
          continue                                            // a missed poll is not an outcome
        }
        entry.progress = seen.status === 'queued' ? 'Waiting for a builder'
          : `${seen.doing || 'Working'} (step ${Math.min(seen.steps_done + 1, seen.steps)} of ${seen.steps})`
        entry.fraction = seen.steps ? Math.min(1, (seen.steps_done || 0) / seen.steps) : null
        if (seen.status === 'done' || seen.status === 'failed') break
      }
      const entry = find()
      if (!entry || entry.state !== 'making') return
      const design = seen?.status === 'done' ? validBuild(seen.card) : null
      let line
      if (design) {
        entry.card = design
        entry.label = cardLabel(design)
        entry.note = cardNote(design)
        entry.state = 'open'
        line = `The design "${design.design.title}" is made and passed the standard's check. The person now sees it with a Build button.`
      } else {
        entry.state = 'refused'
        entry.reason = seen?.status === 'failed' ? 'It did not pass the check.' : 'It took too long.'
        line = seen?.status === 'failed'
          ? `That didn't work: making "${title}". ${seen.reason}. ${seen.detail || ''}`.trim()
          : `Not sure it worked: making "${title}". It did not finish in time.`
      }
      await this.comeBack(line, route)
    },

    /**
     * Follow a run on an agent until it ends, then tell the assistant, which
     * reads the result and comes back to the person with it.
     */
    async followRun(messageId, cardId, route) {
      const find = () => this.messages.find((m) => m.id === messageId)?.cards?.find((c) => c.id === cardId)
      const { agent, execution_id: runId, title } = find()?.card || {}
      let seen = null
      for (let i = 0; i < JOB_POLL_LIMIT; i++) {
        await wait(i < 5 ? RUN_FIRST_POLL_MS : JOB_POLL_MS)
        const entry = find()
        if (!entry || entry.state !== 'making') return
        try {
          seen = (await api.get(`${BASE}/runs/${encodeURIComponent(agent)}/${encodeURIComponent(runId)}`)).data
        } catch {
          continue
        }
        entry.progress = seen.status === 'queued' ? 'Waiting its turn' : 'Running'
        if (seen.ended) break
      }
      const entry = find()
      if (!entry || entry.state !== 'making') return
      let line
      if (seen?.ended && seen.ok) {
        entry.state = 'done'
        line = `The run "${title}" on ${agent} finished (execution ${runId}). Read its result and tell the person what it produced.`
      } else if (seen?.ended) {
        entry.state = 'refused'
        entry.reason = seen.error || 'It failed.'
        line = `The run "${title}" on ${agent} failed (execution ${runId}). ${seen.error || ''} Say what went wrong and what would fix it.`.replace(/\s+/g, ' ')
      } else {
        entry.state = 'unsure'
        entry.reason = 'It is still going. Ask me about it later.'
        line = `The run "${title}" on ${agent} is still going after half an hour (execution ${runId}). I have stopped watching it.`
      }
      await this.comeBack(line, route)
    },

    /** Something being followed ended: tell the assistant once it is free to hear it. */
    async comeBack(line, route) {
      for (let i = 0; this.sending && i < 300; i++) await wait(1000)   // not in the middle of a turn
      await this.send(line, route, { shown: false })
    },

    /** The person asked to read the terms. Read once; a public page opens instead when there is one. */
    async readTerms() {
      if (this.termsText) return
      this.termsError = ''
      try {
        const { data } = await api.get(`${BASE}/terms/trial`)
        if (data?.url) this.status = { ...(this.status || {}), trial_terms: { ...(this.status?.trial_terms || {}), url: data.url } }
        this.termsText = data?.text || ''
        if (!this.termsText && !data?.url) this.termsError = "The terms couldn't be loaded. Try again in a moment."
      } catch (error) {
        this.termsError = words(error, "The terms couldn't be loaded. Check your connection and try again.")
      }
    },

    /** What the person wrote before agreeing: kept, and sent the moment they agree. */
    hold(message) {
      this.waiting = message
    },

    /**
     * The person pressed Agree. Called from that button and nowhere else: the
     * page never agrees for them.
     */
    async agree(route) {
      this.agreeing = true
      this.agreeError = ''
      try {
        const { data } = await api.post(`${BASE}/terms/accept`, { accept: true })
        this.status = { ...(this.status || {}), available: true, contacted: true, reachable: true, ...data }
      } catch (error) {
        this.agreeError = words(error, "Couldn't reach Trinity. Check your connection and try again.")
        return
      } finally {
        this.agreeing = false
      }
      const waiting = this.waiting
      this.waiting = ''
      if (waiting && this.agreed) await this.send(waiting, route)
    },

    /** The panel was opened or closed. Opening it is reading it. */
    setOpen(open) {
      this.panelOpen = open
      if (open) this.unread = 0
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
        // What they asked when the allowance ran out was never answered: answer
        // it now, with the subscription said in the same breath.
        const held = this.held
        this.held = ''
        const done = outcomeLine('done', 'Subscribe to Trinity Pro')
        if (held) await this.send(`${done} Now answer what they asked just before, which the limit stopped: "${held}"`, route, { shown: false })
        else if (this.conversationId) await this.send(done, route, { shown: false })
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
      this.held = ''
    },
  },
})
