/**
 * The fixed first message a new member sees from their agent (trinity-enterprise#1170).
 *
 * `GET /api/enterprise/member-agents/settings` carries `welcome:
 * { text, is_default, replaces_hello, max_length }`; `PUT .../settings/welcome`
 * writes it — `text: null` restores the shipped default, `""` turns it off.
 * Admin only, behind the member-agents entitlement.
 *
 * trinity-enterprise#1202: the same response also carries the member-turn
 * `model` block and the `wake_model` block (each member agent's own model).
 * `settings` keeps the whole view so the read-only model panel reads it here
 * rather than issuing a second GET; `save()` replaces only `welcome`.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'

const BASE = '/api/enterprise/member-agents/settings'

export const useMemberWelcomeStore = defineStore('memberWelcome', () => {
  const welcome = ref(null)
  const settings = ref(null)
  const hasLoaded = ref(false)
  const loading = ref(false)
  const error = ref(null)

  async function load() {
    loading.value = true
    try {
      const { data } = await api.get(BASE)
      welcome.value = data?.welcome || null
      settings.value = data || null
      hasLoaded.value = true
      error.value = null
    } catch (e) {
      error.value = e
    } finally {
      loading.value = false
    }
  }

  /** Save `{ text, replaces_hello }`. Rejects with the server's error. */
  async function save(text, replacesHello) {
    const { data } = await api.put(`${BASE}/welcome`, { text, replaces_hello: !!replacesHello })
    welcome.value = data
    return data
  }

  return { welcome, settings, hasLoaded, loading, error, load, save }
})
