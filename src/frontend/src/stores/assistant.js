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
import { useOnboardingStore } from './onboarding'
import { buildContext, buildFleet, describePage } from '../utils/assistantContext'
import { acceptCards, buildOutcome, cardLabel, cardNote, credentialLine, outcomeLine, runCard, validBuild, webLink } from '../utils/assistantCards'
import { useAgentsStore } from './agents'
import { useAuthStore } from './auth'
import { useFirstRunStore } from './firstRun'
import { firstResultSeen, inFirstSession, markFirstResultSeen, resultPath } from '../utils/firstSession'

export const FEATURE_ID = 'assistant'

const BASE = '/api/enterprise/assistant'
const STEP_TIMEOUT_MS = 260000
const BUILD_TIMEOUT_MS = 600000
const MAX_STEPS = 40
const JOB_POLL_MS = 4000
const RUN_FIRST_POLL_MS = 1500   // a short run is over before the first slow poll
// A plain yes to a design that is waiting. Nothing longer: "yes but change the name" is a message.
const SAID_YES = /^(yes|yep|yeah|ok|okay|sure|go|go ahead|do it|build it|build|create it|create them|make it|please do|yes please|yes go ahead|ok build it|yes build it)[.! ]*$/i
const FOLLOWED = ['job', 'run']    // cards the page follows instead of asking about
// Until the person has opened the assistant once, the launcher says there is something to read.
const SEEN_KEY = 'trinity-assistant-seen'
const neverOpened = () => {
  try {
    return !localStorage.getItem(SEEN_KEY)
  } catch {
    return false
  }
}
const JOB_POLL_LIMIT = 450 // 30 minutes
// trinity-enterprise#1330: right after setup the status can be read before the
// instance's account report has landed, and says so with `bind.state: 'pending'`.
// It is read again after each of these waits; after the last one the panel
// carries on with what it has, exactly as a status without the marker does.
const SETTLE_DELAYS_MS = [500, 1000, 2000, 4000]
const reportPending = (status) => status?.bind?.state === 'pending'
let settleRun = 0   // bumped by stopSettling(): a loop from an earlier run reads nothing more

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

let nextId = 1
const newId = () => `m${nextId++}`
let statusFlight = null

const detail = (error) => error?.response?.data?.detail
const words = (error, fallback) => {
  const d = detail(error)
  return (typeof d === 'string' ? d : d?.message) || fallback
}

// The conversation is kept in this browser so a reload does not lose it, or the
// builds and runs being followed. A day old, it is let go.
const KEEP_KEY = 'trinity-assistant-conversation'
const KEEP_MS = 24 * 3600 * 1000
const KEEP_MESSAGES = 80

// `stale`: the account is known but the terms it accepted are no longer the current ones.
const closedAccount = () => ({ step: null, email: '', devCode: '', terms: null, busy: false, error: '', stale: false })

// trinity-enterprise#1331: subscribing by code. The code goes to the instance's
// registered admin, whose address the panel only ever shows masked. Each named
// refusal is a sentence that says what to do next; the gateway's own words are
// the fallback for a code this list does not know.
const TOO_MANY_CODES = 'Too many codes have been sent for now. Wait a few minutes, then send a new code.'
const BY_CODE_REFUSALS = Object.freeze({
  wrong_code: "That code isn't right. Check the email and type it again, or send a new code.",
  code_expired: 'That code has expired. Send a new code and use that one.',
  too_many_attempts: 'Too many tries with that code. Send a new code to try again.',
  code_required: 'Enter the code from the email.',
  terms_required: 'Tick the subscription terms to continue.',
  terms_changed: 'The subscription terms changed while you were reading. Read the current terms below and tick them again.',
  register_first: "This instance is not registered yet, so no code can be sent. Its admin needs to register it before anyone can subscribe.",
  unknown_instance: "Trinity doesn't recognise this instance, so no code can be sent. Its admin needs to register it again before anyone can subscribe.",
  workspace_only: "This account can't subscribe the instance. Ask this instance's admin to sign in and subscribe.",
  subscription_key_refused: "Subscribing needs a person signed in, not a key. Sign in to Trinity in this browser as the instance's admin, then subscribe.",
  invalid_ip: "Trinity couldn't record where this request came from, so the subscription didn't start. Press Confirm to try again; if it happens again, contact Trinity support.",
  invalid_who: "Trinity couldn't record who accepted the terms, so the subscription didn't start. Press Confirm to try again; if it happens again, contact Trinity support.",
  code_limit: TOO_MANY_CODES,
  address_limit: TOO_MANY_CODES,
  email_limit: TOO_MANY_CODES,
  email_off: "Trinity can't send email right now, so no code was sent. Try again later.",
  email_failed: "The code couldn't be sent. Try again in a moment.",
})
// Refusals a new code or another press cannot fix: the panel offers neither.
const BY_CODE_DEAD_ENDS = new Set(['register_first', 'unknown_instance', 'workspace_only', 'subscription_key_refused', 'subscription_admin_only'])
const deadEnd = (error) => BY_CODE_DEAD_ENDS.has(detail(error)?.code)
const TERMS_UNREAD = "The subscription terms couldn't be loaded. Try again in a moment."
const byCodeWords = (error, fallback) => {
  const d = detail(error)
  if (d?.code === 'subscription_admin_only') {
    return d.admin ? `Only this instance's admin (${d.admin}) can subscribe. Ask them to sign in and subscribe.` : words(error, fallback)
  }
  return BY_CODE_REFUSALS[d?.code] || words(error, fallback)
}
/** The address as the panel may show it: first letter and domain. Empty when there is none. */
export function maskAddress(value) {
  const [local, domain] = String(value || '').trim().split('@')
  return local && domain ? `${local[0]}•••@${domain}` : ''
}
const byCodeAccount = (fields = {}) => ({
  ...closedAccount(), byCode: true, subTerms: null, ticked: false, sentTo: '', expiresIn: null, resent: false, stuck: false, blocked: false, ...fields,
})


const LOW_MESSAGES = 5

/** The getting-started steps the person sees in the panel, for the assistant to steer by. Titles and ticks only. */
function setupSteps() {
  const setup = useOnboardingStore()
  if (!setup.visible) return {}
  return { setup: setup.items.slice(0, 12).map((i) => ({ title: String(i.title || '').slice(0, 80), done: Boolean(i.done) })) }
}

// Whether this browser session has already had the hello, opened or waved away.
const HELLO_KEY = 'trinity-assistant-hello'
function helloGiven() {
  try {
    return Boolean(sessionStorage.getItem(HELLO_KEY))
  } catch {
    return true      // storage refused: say nothing rather than say it on every page
  }
}
function markHelloGiven() {
  try {
    sessionStorage.setItem(HELLO_KEY, '1')
  } catch { /* it is simply offered again */ }
}

/** When the oldest use frees up, in words: "in about 3 hours", "on Friday". Empty when unknown or past. */
function backIn(resetsAt) {
  const at = resetsAt ? new Date(resetsAt) : null
  if (!at || Number.isNaN(at.getTime())) return ''
  const hours = Math.round((at.getTime() - Date.now()) / 3600000)
  if (hours < 0) return ''
  if (hours <= 1) return 'within the hour'
  return hours < 24 ? `in about ${hours} hours` : `on ${at.toLocaleDateString(undefined, { weekday: 'long' })}`
}

export const useAssistantStore = defineStore('assistant', {
  state: () => ({
    absent: false,       // 404/403 seen: no assistant on this build
    status: null,        // GET /status; null until first read
    statusError: '',
    settling: false,     // the status said the account report is still pending: it is being read again
    conversationId: null,
    messages: [],        // { id, role: 'user' | 'assistant' | 'note', content, cards? }
    sending: false,
    activity: '',        // what is being looked at right now
    steps: [],           // every step of the turn in hand, in order
    saying: '',          // what it said it is about to do, while it does it
    startedAt: 0,        // when the turn in hand began
    panelOpen: false,    // set by the panel
    resizing: false,     // the panel's edge is being dragged: the page follows it without easing
    unread: neverOpened() ? 1 : 0,   // answers that arrived while the panel was closed; one to start with, on a first visit
    opening: '',         // a line the assistant opened with, told to it with the person's reply
    keptSince: undefined, // set for a conversation restored from this browser: when Trinity first knew the instance it was had with
    openingKey: '',      // which line that was, until Trinity has been told the person saw it
    corner: 'undecided', // which button the corner of the app holds: 'assistant', 'help', or not yet known (set by App.vue)
    peek: neverOpened(), // a first visit: the launcher says hello in a few words, until opened or waved away
    checkedAt: 0,        // when the status was last read
    held: '',            // the message the limit stopped, to ask again after subscribing
    waiting: '',         // what the person wrote before agreeing to the terms; sent once they agree
    agreeing: false,
    agreeError: '',
    termsText: '',       // the trial's terms, read when the person asks to see them
    fullTermsText: '',   // the terms accepted at sign-in, read the same way
    termsError: '',
    sendError: '',
    limit: null,         // the allowance, when the last answer was "limit"
    account: closedAccount(),
    byCode: null,        // the module offers subscribing by code: null until Subscribe is first pressed (trinity-enterprise#1331)
    fleet: [],
    openResult: '',      // ent#1355: a first result the page should open in front of the person (the panel does it)
  }),

  getters: {
    available: (state) => !state.absent && Boolean(state.status?.available),
    subscribed: (state) => state.status?.plan === 'pro',
    // False only when the account is known and the terms it accepted are not the
    // current version. No current version to compare with: let Trinity decide.
    termsCurrent: (state) => {
      const s = state.status
      if (!s?.email || !s.terms?.version) return true
      return s.terms_accepted === s.terms.version
    },
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
      const left = `Free · ${a.messages_left} ${a.messages_left === 1 ? 'message' : 'messages'} left`
      // Running low: say when more come back, before the limit says it for us.
      const back = a.messages_left <= LOW_MESSAGES ? backIn(a.resets_at) : ''
      if (back) return `${left} · more ${back}`
      const period = a.period_hours === 24 ? 'today' : a.period_hours === 168 ? 'this week' : 'for now'
      return `${left} ${period}`
    },
  },

  actions: {
    // One read at a time: App.vue's watch and the terms page's re-read after
    // acceptance (trinity-enterprise#1327) both ask, and must join one request.
    loadStatus() {
      if (!statusFlight) statusFlight = this._readStatus().finally(() => { statusFlight = null })
      return statusFlight
    },

    async _readStatus() {
      try {
        const { data } = await api.get(`${BASE}/status`)
        this.status = data
        this.statusError = ''
        this.checkedAt = Date.now()
        this.greet(data?.nudge)
        this.dropStale()
        if (reportPending(data) && !this.settling) this._settle()     // not awaited: the panel says it is finishing meanwhile
        // Nobody here has started with the assistant yet: it says hello once per
        // browser session until someone does, whatever this browser saw before.
        if (data?.available && !this.agreed && !this.panelOpen && !helloGiven()) {
          this.peek = true
          this.unread = Math.max(this.unread, 1)
        }
      } catch (error) {
        const code = error?.response?.status
        if (code === 404 || code === 403) {
          this.absent = true
          return
        }
        this.statusError = "Couldn't load the assistant. Check your connection and try again."
      }
    },

    /**
     * The account report was still pending when the status was read, so what it
     * says about the terms may be out of date. Read it again with backoff until
     * the marker is gone, or give up after the last wait and keep what was read.
     * A status without the marker never starts this (older backends included).
     */
    async _settle() {
      const run = ++settleRun
      this.settling = true
      try {
        for (const ms of SETTLE_DELAYS_MS) {
          await wait(ms)
          // Signed out, or the panel was torn down: no read without a credential.
          if (run !== settleRun || !useAuthStore().isAuthenticated) return
          await this.loadStatus()
          if (!reportPending(this.status)) return
        }
      } finally {
        if (run === settleRun) this.settling = false
      }
    },

    /** The panel is going away: the re-read loop, if one runs, stops before its next read. */
    stopSettling() {
      settleRun++
      this.settling = false
    },

    /**
     * The assistant speaks first: one line about the step worth taking next,
     * which Trinity gives once per stage. It shows as a message of its own and
     * lights the launcher; nothing is sent, and it costs the person nothing.
     */
    greet(nudge) {
      const text = typeof nudge?.text === 'string' ? nudge.text.trim().slice(0, 400) : ''
      if (!text || this.sending) return
      // ent#1355: the first session follows its own path; a stage line (connect a model in Settings) waits until it is over.
      if (inFirstSession(this.status, this.fleet, useFirstRunStore().seededAgents || [])) return
      const key = typeof nudge?.key === 'string' ? nudge.key : ''
      // Already on screen (a reload, a second sign-in, a re-read): said once, not again (trinity-enterprise#1356).
      if (this.messages.some((m) => m.opener && !m.local && ((key && m.key === key) || m.content === text))) return
      this.messages = this.messages.filter((m) => !m.local)      // its own line takes the place of the page's hello
      this.messages.push({ id: newId(), role: 'assistant', content: text, opener: true, ...(key ? { key } : {}) })
      this.opening = text
      this.openingKey = key
      if (!this.panelOpen) this.unread += 1
      else this.sawOpening()
    },

    /**
     * A conversation this browser kept belongs to the Trinity record it was
     * had with. If Trinity has never heard from this instance, or first heard
     * from it at another time than the conversation was kept under, that
     * record is gone (a reset, another instance at the same address) and so is
     * everything the assistant knew: showing the old talk would be showing a
     * memory it does not have. Only ever judged on a real answer from Trinity.
     */
    dropStale() {
      const s = this.status
      if (!s?.available || this.keptSince === undefined) return
      // Kept without a time (it began before Trinity had said one): only "never heard of you" settles it.
      const gone = s.contacted === false || (s.reachable === true && s.since && this.keptSince && s.since !== this.keptSince)
      if (!gone) return
      this.keptSince = undefined
      if (this.sending) return
      this.newConversation()
      if (s.contacted === false) {
        try {
          sessionStorage.removeItem(HELLO_KEY)      // it is new here again: it may say hello again
        } catch { /* it simply stays quiet */ }
        if (!this.panelOpen) {
          this.peek = true
          this.unread = Math.max(this.unread, 1)
        }
      }
    },

    /**
     * The assistant starts the conversation with someone it does not know yet.
     * The page writes this line itself, so it needs nothing sent anywhere and
     * works before anyone has agreed to anything. The assistant is told it was
     * said, with the person's reply.
     */
    welcome(text) {
      if (this.messages.length || this.sending || !text) return
      this.messages.push({ id: newId(), role: 'assistant', content: text, opener: true, local: true })
      this.opening = text
    },

    /** The person waved the launcher's hello away without opening the panel. */
    dismissPeek() {
      this.peek = false
      markHelloGiven()
    },

    /**
     * Tell Trinity the person has seen the line it opened with, once. It stops
     * speaking first to someone who never looks, so it has to know who does.
     */
    sawOpening() {
      const key = this.openingKey
      if (!key) return
      this.openingKey = ''
      api.post(`${BASE}/opened`, { key }).catch(() => { /* worth telling, never worth an error */ })
    },

    /** Read the status again when it has gone stale, so a next step can be said without a reload. */
    recheck() {
      if (this.absent || this.sending || this.panelOpen || !this.status?.contacted) return
      if (Date.now() - this.checkedAt < 10 * 60 * 1000) return
      this.checkedAt = Date.now()
      this.loadStatus()
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
    async send(text, route, { shown = true, counted = shown } = {}) {
      const message = String(text || '').trim()
      if (!message || this.sending) return
      this.sendError = ''
      this.limit = null
      // "yes, build it" said in words, with one design waiting: that is them pressing its button.
      const waitingBuild = shown && SAID_YES.test(message.trim()) ? this.openBuilds() : []
      if (waitingBuild.length === 1) {
        this.messages.push({ id: newId(), role: 'user', content: message })
        return this.answerCard(waitingBuild[0].messageId, waitingBuild[0].cardId, true, route)
      }
      // Answers already clicked on questions they then chose to write about go along.
      const clicked = shown ? this.messages.flatMap((m) => (m.cards || [])
        .filter((c) => c.card.action === 'choice' && c.state === 'picked').map((c) => `${c.card.question} ${c.picked}`)) : []
      if (shown) this.closeChoices()
      this.messages.push({ id: newId(), role: shown ? 'user' : 'note', content: message })
      const said = this.opening
      this.opening = ''
      this.sending = true
      this.activity = ''
      this.steps = []
      this.saying = ''
      this.startedAt = Date.now()
      try {
        const fleet = await this.refreshFleet()
        let { data } = await api.post(`${BASE}/turn`, {
          conversation_id: this.conversationId,
          message: clicked.length ? `${clicked.join('\n')}\n${message}` : message,
          context: { ...buildContext(route, fleet), ...(said ? { said } : {}), ...setupSteps(),
            // trinity-enterprise#1355: the assistant leads this session and follows its script for it.
            ...(inFirstSession(this.status, fleet, useFirstRunStore().seededAgents || []) ? { first_session: true } : {}) },
          counted,
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
            state: FOLLOWED.includes(card.action) ? 'making' : 'open', reason: '', progress: '', picked: '',
            since: Date.now(), fraction: null,
            auto: false,       // a proposed build waits for the owner's click on Build; they can tick create-when-ready (ent#1343)
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
        if (entry.auto) {
          // They ticked "create when ready" themselves: no second click.
          for (let i = 0; this.sending && i < 300; i++) await wait(1000)
          return this.answerCard(messageId, cardId, true, route)
        }
        line = `The design "${design.design.title}" is made and passed the standard's check. They now see it with a Build button; nothing is created until they click it, so it waits for their click.`
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
        entry.result = resultPath(agent, runId)
        // ent#1355: where the assistant leads, the first result opens in front of the person, once.
        if (this.status?.leads_setup && !firstResultSeen()) {
          markFirstResultSeen()
          this.openResult = entry.result
        }
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

    /** The terms accepted at sign-in, when the person asks to read them. */
    async readFullTerms() {
      if (this.fullTermsText) return
      this.termsError = ''
      try {
        const { data } = await api.get(`${BASE}/terms/subscription`)
        this.fullTermsText = data?.text || ''
        if (!this.fullTermsText) this.termsError = "The terms couldn't be loaded. Try again in a moment."
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
      else if (this.agreed) await this.loadStatus()      // nothing asked yet: let it say where to start
    },

    /** Write the conversation down. Cheap, and called on every change. */
    keep() {
      try {
        if (!this.messages.some((m) => !m.local)) return localStorage.removeItem(KEEP_KEY)   // the page's own hello is not a conversation
        localStorage.setItem(KEEP_KEY, JSON.stringify({
          at: Date.now(), conversationId: this.conversationId, messages: this.messages.slice(-KEEP_MESSAGES),
          since: this.status?.since || this.keptSince || null,
        }))
      } catch { /* storage full or refused: the conversation simply is not kept */ }
    },

    /**
     * Pick up where the person left off after a reload: the conversation as it
     * stood, and every build or run that was being followed is followed again.
     */
    restore(route) {
      if (this.messages.length) return
      let kept = null
      try {
        kept = JSON.parse(localStorage.getItem(KEEP_KEY) || 'null')
      } catch { /* unreadable: start fresh */ }
      if (!kept || !Array.isArray(kept.messages) || Date.now() - kept.at > KEEP_MS) return
      const messages = kept.messages.filter((m) => m && typeof m.id === 'string' && typeof m.content === 'string')
      for (const m of messages) {
        nextId = Math.max(nextId, (parseInt(m.id.slice(1), 10) || 0) + 1)
        for (const c of m.cards || []) {
          nextId = Math.max(nextId, (parseInt(String(c.id).slice(1), 10) || 0) + 1)
          if (c.state === 'running') c.state = 'open'          // a click whose answer the reload lost: ask again
        }
      }
      this.messages = messages
      this.conversationId = kept.conversationId || null
      this.keptSince = kept.since || null      // judged against Trinity's answer, now or when it comes
      this.dropStale()
      for (const m of this.messages) {
        for (const c of m.cards || []) {
          if (c.state !== 'making') continue
          if (c.card.action === 'job') this.followJob(m.id, c.id, route)
          if (c.card.action === 'run') this.followRun(m.id, c.id, route)
        }
      }
    },

    /** The panel was opened or closed. Opening it is reading it. */
    setOpen(open) {
      this.panelOpen = open
      if (!open) return
      this.unread = 0
      this.peek = false
      markHelloGiven()
      this.sawOpening()
      try {
        localStorage.setItem(SEEN_KEY, '1')
      } catch { /* the launcher simply says so again next time */ }
    },

    /**
     * The person clicked an answer to a question. With one question that is
     * their reply; with several, the reply goes once each has an answer. A
     * click is not counted against their messages: it answers what was asked.
     */
    async answerChoice(messageId, cardId, option, route) {
      const cards = this.messages.find((m) => m.id === messageId)?.cards || []
      const entry = cards.find((c) => c.id === cardId)
      if (!entry || entry.card.action !== 'choice' || !['open', 'picked'].includes(entry.state) || this.sending) return
      if (!entry.card.options.includes(option)) return
      entry.picked = option
      entry.state = 'picked'
      const asked = cards.filter((c) => c.card.action === 'choice')
      if (asked.some((c) => c.state === 'open')) return
      const reply = asked.length === 1 ? option : asked.map((c) => `${c.card.question} ${c.picked}`).join('\n')
      for (const c of asked) c.state = 'done'
      await this.send(reply, route, { counted: false })
    },

    /**
     * The person wrote instead of clicking: questions still open are closed,
     * and any answer already clicked goes along with what they wrote.
     */
    closeChoices() {
      for (const m of this.messages) {
        for (const c of m.cards || []) {
          if (c.card.action !== 'choice') continue
          if (c.state === 'open') c.state = 'declined'
          if (c.state === 'picked') c.state = 'done'
        }
      }
    },

    /** Designs waiting for their Build button. */
    openBuilds() {
      return this.messages.flatMap((m) => (m.cards || [])
        .filter((c) => c.card.action === 'build' && c.state === 'open')
        .map((c) => ({ messageId: m.id, cardId: c.id })))
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
      } else if (entry.card.action === 'feedback') {
        outcome = await this.sendFeedback(entry.card, entry.label, route)
      } else {
        const result = await runCard(api, entry.card, this.fleet)
        outcome = { kind: result.kind, reason: result.reason || '', line: outcomeLine(result.kind, entry.label, result.reason) }
      }
      useAgentsStore().fetchAgents().catch(() => {})
      entry.state = outcome.kind
      entry.reason = outcome.reason
      await this.send(outcome.line, route, { shown: false })
    },

    /**
     * Whether the credential a field asks for is set already, so the field can
     * say that sending replaces it. `{ set: true | false | null }`,
     * never the value; null when it cannot be read. A 422 is the instance's
     * own policy refusing the name (or agent) outright: `{ refused: words }`,
     * so the field closes before anyone types a secret Save would refuse.
     */
    async credentialState(card) {
      try {
        const { data } = await api.get(`${BASE}/credential`, { params: { name: card.name, ...(card.agent ? { agent: card.agent } : {}) } })
        return data || null
      } catch (error) {
        if (error?.response?.status === 422) return { refused: words(error, "It can't be set from here.") }
        return null
      }
    },

    /**
     * A fresh Claude sign-in link for this person (trinity-enterprise#1354),
     * made by the instance, which keeps the sign-in's secret half. The URL, or
     * null when the instance cannot make one (the field then takes an API key).
     */
    async claudeSignInLink() {
      try {
        const { data } = await api.post('/api/subscriptions/claude-sign-in')
        return typeof data?.url === 'string' && data.url.startsWith('https://') ? data.url : null
      } catch {
        return null
      }
    },

    /**
     * Close a credential field the instance refused by name. No value was
     * typed and no turn is sent: the card says why, and nothing else changes.
     */
    refuseCredential(messageId, cardId, reason) {
      const entry = this.messages.find((m) => m.id === messageId)?.cards?.find((c) => c.id === cardId)
      if (!entry || entry.card.action !== 'credential' || entry.state !== 'open') return
      entry.state = 'refused'
      entry.reason = reason
    },

    /**
     * The person answered a credential field. `value` is what they
     * typed, or null for Cancel. The value is posted once, to this instance,
     * and is held nowhere here: not on the card, not in the conversation kept
     * in this browser, not in the line the assistant is sent, which says only
     * "Provided" or "Not provided". A refusal leaves the card open and sends
     * nothing: the person can fix the value or cancel.
     * Returns `{ ok }` or `{ ok: false, error, code }`.
     */
    async answerCredential(messageId, cardId, value, route) {
      const entry = this.messages.find((m) => m.id === messageId)?.cards?.find((c) => c.id === cardId)
      if (!entry || entry.card.action !== 'credential' || entry.state !== 'open' || this.sending) return { ok: false, error: '' }
      if (value === null) {
        entry.state = 'declined'
        await this.send(credentialLine(entry.card, false), route, { shown: false })
        return { ok: true }
      }
      entry.state = 'running'
      try {
        const { data } = await api.post(`${BASE}/credential`,
          { agent: entry.card.agent || null, name: entry.card.name, value }, { timeout: STEP_TIMEOUT_MS })
        entry.state = 'done'
        entry.reason = data?.replaced ? 'replaced' : ''
        // ent#1355: this instance's Claude key is checked after the save, on the
        // instance, and the save counts the agents it connected. Never the value.
        entry.connected = Number.isInteger(data?.connected_agents) ? data.connected_agents : null
        entry.working = typeof data?.working === 'boolean' ? data.working : null
        entry.problem = entry.working === false && typeof data?.problem === 'string' ? data.problem.slice(0, 300) : ''
      } catch (error) {
        entry.state = 'open'
        const code = error?.response?.status
        return {
          ok: false,
          code: detail(error)?.code || null,
          error: words(error, code ? `Trinity refused it (${code}). Nothing was stored.` : "Couldn't reach Trinity, so it may or may not have been stored."),
        }
      }
      await this.send(credentialLine(entry.card, true, { working: entry.working }), route, { shown: false })
      return { ok: true }
    },

    /**
     * Whether a client outside this server may reach its MCP endpoint
     * (trinity-enterprise#1353: the first of the steps that bring local agents
     * in). true / false, or null when it cannot be read.
     */
    async externalMcpAccess() {
      try {
        const { data } = await api.get('/api/settings/mcp-external-access')
        return typeof data?.enabled === 'boolean' ? data.enabled : null
      } catch {
        return null
      }
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

    /** The person read the note and pressed Send: it goes to the makers of Trinity as shown. */
    async sendFeedback(card, label, route) {
      try {
        await api.post(`${BASE}/feedback`, { kind: card.kind, summary: card.summary, details: card.details, page: describePage(route).label })
        return { kind: 'done', reason: '', line: outcomeLine('done', label) }
      } catch (error) {
        const code = error?.response?.status
        const reason = words(error, code ? `Trinity refused it (${code}).` : "Couldn't reach Trinity, so it may not have been sent.")
        const kind = code && code < 500 ? 'refused' : 'unsure'
        return { kind, reason, line: outcomeLine(kind, label, reason) }
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
    /**
     * Subscribe was pressed. A module with the by-code routes (trinity-enterprise#1331)
     * shows the subscription terms first; one without them answers 404 and the
     * panel signs in by email exactly as before.
     */
    async openAccount() {
      if (this.byCode !== false) {
        this.account = byCodeAccount({ step: 'sub-terms', busy: true })
        try {
          const { data } = await api.get(`${BASE}/subscription/terms`)
          this.byCode = true
          this.account = byCodeAccount({ step: 'sub-terms', subTerms: data || null })
          return
        } catch (error) {
          if (error?.response?.status !== 404) {
            this.byCode = true
            // Refused outright (only the admin may subscribe): nothing to send from here.
            const stuck = detail(error)?.code === 'subscription_admin_only'
            this.account = byCodeAccount({ step: 'sub-terms', stuck, error: byCodeWords(error, TERMS_UNREAD) })
            return
          }
          this.byCode = false
        }
      }
      this.openSignIn()
    },

    /** Read the subscription terms again: the tick is the person's, for the version they now see. */
    async reloadSubscriptionTerms(notice = '') {
      this.account = { ...this.account, step: 'sub-terms', ticked: false, sentTo: '', busy: true, error: '' }
      try {
        const { data } = await api.get(`${BASE}/subscription/terms`)
        this.account = { ...this.account, subTerms: data || null, stuck: false, blocked: false, error: notice }
      } catch (error) {
        this.account = { ...this.account, stuck: detail(error)?.code === 'subscription_admin_only', error: byCodeWords(error, TERMS_UNREAD) }
      } finally {
        this.account.busy = false
      }
    },

    /** Send the code to the registered admin, or a new one. No address goes with it. */
    async sendSubscriptionCode({ resend = false } = {}) {
      if (!this.account.ticked) {
        this.account.error = BY_CODE_REFUSALS.terms_required
        return
      }
      this.account.busy = true
      this.account.error = ''
      try {
        const { data } = await api.post(`${BASE}/subscription/${resend ? 'code/resend' : 'code'}`, {})
        // The code is bound to the terms version it names: one the person did not
        // tick would only be refused at confirm, so show the current terms now.
        const sentVersion = data?.terms ? (data.terms.version || '') : null
        if (sentVersion !== null && sentVersion !== (this.account.subTerms?.version || '')) {
          this.account.busy = false
          return await this.reloadSubscriptionTerms(BY_CODE_REFUSALS.terms_changed)
        }
        this.account = { ...this.account, step: 'sub-code', sentTo: data?.email || '', expiresIn: data?.expires_in ?? null, resent: resend }
      } catch (error) {
        this.account.blocked = deadEnd(error)
        this.account.error = byCodeWords(error, "The code couldn't be sent. Try again in a moment.")
      } finally {
        this.account.busy = false
      }
    },

    /** The person typed the code, having ticked the terms themselves: start the subscription. */
    async confirmSubscription(code, route) {
      const digits = String(code || '').trim()
      if (!digits) {
        this.account.error = BY_CODE_REFUSALS.code_required
        return
      }
      this.account.busy = true
      this.account.error = ''
      let data
      try {
        ({ data } = await api.post(`${BASE}/subscription/confirm`, {
          code: digits, accept: this.account.ticked === true, version: this.account.subTerms?.version || '',
        }))
      } catch (error) {
        if (detail(error)?.code === 'terms_changed') await this.reloadSubscriptionTerms(BY_CODE_REFUSALS.terms_changed)
        else {
          this.account.blocked = deadEnd(error)
          this.account.error = byCodeWords(error, "Couldn't start the subscription. Try again.")
        }
        return
      } finally {
        this.account.busy = false
      }
      await this.finishSubscribing(data, route)
    },

    /** The old sign-in-by-email path, for a module without the by-code routes. */
    openSignIn() {
      const email = this.status?.email
      if (email && !this.termsCurrent) {
        // The current terms are accepted with a code, as at any sign-in.
        this.account = { ...closedAccount(), step: 'email', email, stale: true }
        return this.startAccount(email)
      }
      this.account = { ...closedAccount(), step: email ? 'subscribe' : 'email' }
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
        await this.finishSubscribing(data, route)
      } catch (error) {
        if (detail(error)?.code === 'sign_in_first') {   // the terms changed since the status was read
          await this.loadStatus()
          if (this.status?.email && !this.termsCurrent) return await this.openSignIn()
        }
        this.account.error = words(error, "Couldn't start the subscription. Try again.")
      } finally {
        this.account.busy = false
      }
    },

    /** The subscription started, by either path: open the checkout, or say so and answer what was held. */
    async finishSubscribing(data, route) {
      // The by-code confirm names the address masked or not at all: either would
      // replace the full one this instance holds, so only a plain address is kept.
      const { email, ...rest } = data || {}
      this.status = { ...this.status, ...rest }
      if (typeof email === 'string' && email.includes('@') && !email.includes('•')) this.status.email = email
      const checkout = webLink(data?.checkout_url)
      if (checkout) {
        window.open(checkout, '_blank', 'noopener')
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
      this.opening = ''
      this.keep()
    },
  },
})
