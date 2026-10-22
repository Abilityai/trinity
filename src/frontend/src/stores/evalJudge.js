/**
 * Eval judge policy — the Settings › Judge tab's store (trinity-enterprise#936).
 *
 * Reads and replaces the platform policy through the admin-only, entitlement-
 * gated `/api/enterprise/eval-judge/settings` route. The server validates the
 * whole document and refuses by name; `REFUSALS` turns each code into a sentence
 * that says what to change.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'
import { apiErrorMessage } from '../utils/apiError'

export const SETTINGS_URL = '/api/enterprise/eval-judge/settings'

export const REFUSALS = {
  agent_default_unknown: 'Choose whether agents are judged by default.',
  judge_models_empty: 'Pick at least one judge model.',
  judge_models_too_many: 'Use at most two judge models: a primary and one fallback.',
  judge_model_below_floor: 'That model is below the Sonnet 5 floor. Pick Sonnet 5 or above.',
  judge_model_not_allowlisted: 'That model is not on the judge allowlist.',
  judge_models_same_family: 'The fallback must be a different model family from the primary, for example Sonnet then Opus.',
  cap_out_of_range: 'The daily cap must be between $0 and $100 per agent.',
  quota_out_of_range: 'Passing runs per day must be 0–100, and the cost multiple 1–1000.',
  cost_floor_lane_unknown: 'Cost floor lanes can only be “pass” and “failure”.',
  setting_type_invalid: 'One of the values has the wrong type.',
  unknown_setting: 'The policy contains a setting the server does not know.',
}

export function refusalMessage(err) {
  const code = err?.response?.data?.detail?.code
  return REFUSALS[code] || apiErrorMessage(err, "Couldn't save the judge policy")
}

const splitList = (text) => String(text || '').split(',').map((s) => s.trim()).filter(Boolean)

/** Form state → the exact document the server validates. */
export function toPolicy(form) {
  return {
    enabled: !!form.enabled,
    agent_default: form.agentDefault,
    judge_models: [form.primary, form.fallback].filter(Boolean),
    sampling: {
      every_tier0_failure: !!form.everyFailure,
      passing_per_agent_per_day: Number(form.passingPerDay),
      min_run_cost_multiple: Number(form.costMultiple),
      cost_floor_lanes: [form.floorPass && 'pass', form.floorFailure && 'failure'].filter(Boolean),
    },
    daily_cap_usd_per_agent: Number(form.dailyCap),
    excluded_triggers: splitList(form.excludedTriggers),
    excluded_archetypes: splitList(form.excludedArchetypes),
  }
}

/** The server's policy → form state. */
export function toForm(p) {
  const lanes = p?.sampling?.cost_floor_lanes || []
  return {
    enabled: !!p?.enabled,
    agentDefault: p?.agent_default || 'off',
    primary: p?.judge_models?.[0] || '',
    fallback: p?.judge_models?.[1] || '',
    everyFailure: !!p?.sampling?.every_tier0_failure,
    passingPerDay: p?.sampling?.passing_per_agent_per_day ?? 0,
    costMultiple: p?.sampling?.min_run_cost_multiple ?? 10,
    floorPass: lanes.includes('pass'),
    floorFailure: lanes.includes('failure'),
    dailyCap: p?.daily_cap_usd_per_agent ?? 0,
    excludedTriggers: (p?.excluded_triggers || []).join(', '),
    excludedArchetypes: (p?.excluded_archetypes || []).join(', '),
  }
}

export const useEvalJudgeStore = defineStore('evalJudge', () => {
  const policy = ref(null)
  const allowlist = ref([])
  const loading = ref(false)
  const hasLoaded = ref(false)
  const error = ref(null)
  const saving = ref(false)
  const saveError = ref(null)
  const savedAt = ref(null)

  async function load() {
    loading.value = true
    try {
      const r = await api.get(SETTINGS_URL)
      policy.value = r.data?.platform || null
      allowlist.value = r.data?.judge_model_allowlist || []
      hasLoaded.value = true
      error.value = null
    } catch (e) {
      error.value = apiErrorMessage(e, "Couldn't load the judge policy")
    } finally {
      loading.value = false
    }
  }

  async function save(form) {
    saving.value = true
    saveError.value = null
    try {
      const r = await api.put(SETTINGS_URL, toPolicy(form))
      policy.value = r.data?.platform || policy.value
      savedAt.value = new Date()
      return true
    } catch (e) {
      saveError.value = refusalMessage(e)
      return false
    } finally {
      saving.value = false
    }
  }

  return { policy, allowlist, loading, hasLoaded, error, saving, saveError, savedAt, load, save,
    clearSaveError: () => { saveError.value = null } }
})
