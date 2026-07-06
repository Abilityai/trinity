import { defineStore } from 'pinia'
import axios from 'axios'

export const useSettingsStore = defineStore('settings', {
  state: () => ({
    settings: {},
    loading: false,
    saving: false,
    error: null
  }),

  getters: {
    trinityPrompt() {
      return this.settings.trinity_prompt || ''
    }
  },

  actions: {
    /**
     * Fetch all system settings from the backend.
     * Admin-only endpoint.
     */
    async fetchSettings() {
      this.loading = true
      this.error = null

      try {
        const response = await axios.get('/api/settings')
        // Convert array to object for easier access
        const settingsObj = {}
        for (const setting of response.data) {
          settingsObj[setting.key] = setting.value
        }
        this.settings = settingsObj
        return this.settings
      } catch (error) {
        console.error('Failed to fetch settings:', error)
        this.error = error.response?.data?.detail || 'Failed to fetch settings'
        throw error
      } finally {
        this.loading = false
      }
    },

    /**
     * #1129: Fleet-wide default access policy for new agents.
     * Returns { require_email, require_email_default, note }. Admin-only.
     */
    async getAgentDefaultAccessPolicy() {
      const response = await axios.get('/api/settings/agent-defaults/access-policy')
      return response.data
    },

    /**
     * #1129: Set the fleet-wide default `require_email` for new agents.
     * Applies to newly created agents only. Admin-only.
     */
    async setAgentDefaultRequireEmail(requireEmail) {
      const response = await axios.put('/api/settings/agent-defaults/access-policy', {
        require_email: requireEmail,
      })
      return response.data
    },

    /**
     * #506: Fleet-wide ceiling on per-agent max_parallel_tasks.
     * Returns { value, default, min, max }. Admin-only.
     */
    async getMaxParallelTasksCeiling() {
      const response = await axios.get('/api/settings/max-parallel-tasks-ceiling')
      return response.data
    },

    /**
     * #506: Set the fleet-wide max_parallel_tasks ceiling (1–32).
     * Uses the dedicated range-validated endpoint (the generic updateSetting
     * bypasses range validation and is blocked for this key). Admin-only.
     */
    async setMaxParallelTasksCeiling(value) {
      const response = await axios.put('/api/settings/max-parallel-tasks-ceiling', {
        value,
      })
      return response.data
    },

    /**
     * trinity-enterprise#85: Brain Orb platform flags. Returns
     * { flags: { enabled|voice_enabled|write_enabled: { value, source } },
     *   gemini_key_configured }. `source` is override|env|default. Admin-only.
     */
    async getBrainOrbSettings() {
      const response = await axios.get('/api/settings/brain-orb')
      return response.data
    },

    /**
     * trinity-enterprise#85: Update Brain Orb platform flags. `payload` may
     * carry enabled / voice_enabled / write_enabled booleans and/or a
     * `clear: [flag,...]` list reverting flags to their env/default value.
     * Returns the same shape as getBrainOrbSettings plus updated/cleared.
     * Admin-only; takes effect on the next request — no restart.
     */
    async setBrainOrbSettings(payload) {
      const response = await axios.put('/api/settings/brain-orb', payload)
      return response.data
    },

    /**
     * Get a specific setting by key.
     */
    async getSetting(key) {
      try {
        const response = await axios.get(`/api/settings/${key}`)
        this.settings[key] = response.data.value
        return response.data.value
      } catch (error) {
        if (error.response?.status === 404) {
          return null
        }
        console.error(`Failed to get setting ${key}:`, error)
        throw error
      }
    },

    /**
     * Update a system setting.
     * Admin-only endpoint.
     */
    async updateSetting(key, value) {
      this.saving = true
      this.error = null

      try {
        const response = await axios.put(`/api/settings/${key}`, { value })
        this.settings[key] = response.data.value
        return response.data
      } catch (error) {
        console.error(`Failed to update setting ${key}:`, error)
        this.error = error.response?.data?.detail || 'Failed to update setting'
        throw error
      } finally {
        this.saving = false
      }
    },

    /**
     * Delete a system setting.
     * Admin-only endpoint.
     */
    async deleteSetting(key) {
      this.saving = true
      this.error = null

      try {
        await axios.delete(`/api/settings/${key}`)
        delete this.settings[key]
        return true
      } catch (error) {
        console.error(`Failed to delete setting ${key}:`, error)
        this.error = error.response?.data?.detail || 'Failed to delete setting'
        throw error
      } finally {
        this.saving = false
      }
    },

    /**
     * Clear any error state.
     */
    clearError() {
      this.error = null
    }
  }
})
