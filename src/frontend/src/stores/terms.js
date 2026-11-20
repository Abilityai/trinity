/**
 * Terms of use (trinity-enterprise#1054, #1107): the text every person accepts
 * once per version before any route answers them, and — for an admin — the
 * instance's own text, its version and who accepted what.
 *
 * The server is the boundary — every route a gated person calls answers 403
 * `terms_required` until this store's `accept` succeeds. The two routes
 * `/current` and `/accept` are exempt from that refusal, so reading and
 * accepting can never be refused by the gate they end. The routes are OSS
 * (`/api/terms/*`, trinity-enterprise#1107): a Trinity with no optional
 * module serves its shipped default.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'
import { useAuthStore } from './auth'
import { useEnterpriseStore } from './enterprise'
import { useSessionsStore } from './sessions'
import { FEATURE_ID as ASSISTANT_FEATURE, useAssistantStore } from './assistant'
import { useOnboardingStore } from './onboarding'
import { useFirstRunStore } from './firstRun'
import { useBuildInfo } from '../composables/useBuildInfo'

const BASE = '/api/terms'

/** The longest acceptance waits on the re-reads; a later answer lands when it lands. */
export const REREAD_LIMIT_MS = 4000

export const useTermsStore = defineStore('terms', () => {
  /** `{ required, version, title, text, is_default, accepted }` from `GET /current`. */
  const current = ref(null)
  const hasLoaded = ref(false)
  const loading = ref(false)
  const error = ref(null)

  /** The admin record: `[{ id, user_id, email, terms_version, accepted_at, door }]`. */
  const acceptances = ref([])
  const acceptancesLoaded = ref(false)
  const acceptancesError = ref(null)

  async function load() {
    loading.value = true
    try {
      const { data } = await api.get(`${BASE}/current`)
      current.value = data
      hasLoaded.value = true
      error.value = null
    } catch (e) {
      error.value = e
    } finally {
      loading.value = false
    }
  }

  /** Accept `version` — the one the person read. Rejects with the server's error. */
  async function accept(version) {
    const { data } = await api.post(`${BASE}/accept`, { version })
    if (current.value) current.value = { ...current.value, version: data.version, accepted: true }
    await rereadStartup()
    return data
  }

  /**
   * trinity-enterprise#1327 — the app's start-up reads were answered 403
   * `terms_required`, and each one keeps that answer for the page load: the
   * profile (`profileVerified` stays false), the entitlements and session
   * flags (cached as "none"), the build info, the first-run state (cached as
   * "not a first run"), the assistant's status and the getting-started
   * checklist (both cached as "absent"). Accepting ends the refusal, so ask
   * them all again — before the page moves on, so the page the person lands on
   * is drawn from the real answers, not the refused ones. The wait is bounded
   * by `REREAD_LIMIT_MS`: a read that never answers cannot keep the person on
   * the terms page. Never rejects: a read that still fails leaves its own
   * safe fallback.
   */
  async function rereadStartup() {
    let timer
    const limit = new Promise((resolve) => { timer = setTimeout(resolve, REREAD_LIMIT_MS) })
    try {
      await Promise.race([rereadAll(), limit])
    } finally {
      clearTimeout(timer)
    }
  }

  async function rereadAll() {
    const auth = useAuthStore()
    const enterprise = useEnterpriseStore()
    const assistant = useAssistantStore()
    const onboarding = useOnboardingStore()
    assistant.absent = false
    assistant.statusError = ''
    onboarding.absent = false
    await Promise.allSettled([
      auth.fetchUserProfile(),
      enterprise.loadFeatureFlags(true),
      useSessionsStore().loadFeatureFlags(true),
      useBuildInfo().load(true),
      onboarding.fetchChecklist(true),
      useFirstRunStore().fetchState(true),
    ])
    // The same gate App.vue mounts the assistant on; joins its read if it started one.
    if (auth.profileVerified && auth.user?.role === 'admin' && enterprise.isEntitled(ASSISTANT_FEATURE)) {
      await assistant.loadStatus()
    }
  }

  /**
   * Admin: save the instance's text. Any subset of `{ title, text, required }`;
   * a text or title save stamps a new version. Rejects with the server's
   * named 422 (`terms_text_empty`, `terms_text_too_large`, `terms_title_too_long`).
   */
  async function save(patch) {
    const { data } = await api.put(BASE, patch)
    current.value = data
    hasLoaded.value = true
    return data
  }

  /** Admin: the one action that brings the shipped default text back. */
  async function restoreDefault() {
    const { data } = await api.post(`${BASE}/restore-default`)
    current.value = data
    hasLoaded.value = true
    return data
  }

  async function loadAcceptances() {
    try {
      const { data } = await api.get(`${BASE}/acceptances`)
      acceptances.value = Array.isArray(data) ? data : []
      acceptancesLoaded.value = true
      acceptancesError.value = null
    } catch (e) {
      acceptancesError.value = e
    }
  }

  return {
    current, hasLoaded, loading, error, load, accept,
    save, restoreDefault,
    acceptances, acceptancesLoaded, acceptancesError, loadAcceptances,
  }
})
