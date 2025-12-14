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
