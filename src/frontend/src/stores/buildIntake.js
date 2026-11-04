import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import { useAuthStore } from './auth'
import { useUserPreferencesStore } from './userPreferences'
import { PREF_KEYS } from '../utils/gridStorageKeys'
import { emptyIntake, forgetHandover, loadIntake, saveIntake } from '../components/portal/builder/portalBuild'

/**
 * Workspace Build: the setup answers, kept on the person's account.
 *
 * They follow the account rather than the browser, so whoever signs in as admin or
 * with that account's email gets the same answers, on any browser. The browser keeps a
 * copy so the page has them before the network answers, and for anyone with no
 * platform account, whose saves the preferences store ignores.
 */
const PREFERENCE_KEY = PREF_KEYS.workspaceIntake
const storage = () => (typeof window === 'undefined' ? null : window.localStorage)
const hasContent = (i) => Boolean(i?.done || i?.audit || Object.keys(i?.answers || {}).length)

/** The newer of two copies. A tie goes to the account. */
export function newerIntake(local, server) {
  if (!server?.answers) return local
  return (local?.savedAt || 0) > (server.savedAt || 0) ? local : forgetHandover({ ...emptyIntake(), ...server })
}

export const useBuildIntakeStore = defineStore('buildIntake', () => {
  const auth = useAuthStore()
  const preferences = useUserPreferencesStore()
  const intake = ref(emptyIntake())
  let browserKey = ''

  /** Read this person's answers. It finishes even if the account can't be reached. */
  async function load(email = '') {
    browserKey = email
    const local = loadIntake(storage(), email)
    intake.value = local
    await preferences.load()
    const chosen = newerIntake(local, preferences.records[PREFERENCE_KEY]?.value)
    intake.value = chosen
    saveIntake(storage(), email, chosen)
    if (chosen === local && hasContent(local)) preferences.save(PREFERENCE_KEY, local, { origin: 'reconcile' })
  }

  function set(next) {
    const stamped = { ...next, savedAt: Date.now() }
    intake.value = stamped
    saveIntake(storage(), browserKey, stamped)
    preferences.save(PREFERENCE_KEY, stamped)
  }

  /** The answers as this browser last saved them, another tab included. */
  function storedIntake() {
    return loadIntake(storage(), browserKey)
  }

  /** A new sign-in email is the same person, so the answers move with them. */
  function rekey(email = '') {
    if (email === browserKey) return
    browserKey = email
    saveIntake(storage(), email, intake.value)
  }

  // Someone else signing in, or the token arriving after this store was created.
  watch(() => auth.principalId, (id, before) => {
    if (id !== before) load(auth.userEmail || '')
  })

  return { intake, load, set, rekey, storedIntake }
})
