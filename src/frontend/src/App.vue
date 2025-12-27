<template>
  <div id="app" class="min-h-screen bg-gray-100 dark:bg-gray-900">
    <!-- KeepAlive preserves terminal WebSocket connections when navigating away -->
    <router-view v-slot="{ Component }">
      <KeepAlive :include="['SystemAgent', 'AgentDetail']">
        <component :is="Component" />
      </KeepAlive>
    </router-view>
  </div>
</template>

<script setup>
import { onMounted } from 'vue'
import axios from 'axios'
import { useAuthStore } from './stores/auth'
import { useThemeStore } from './stores/theme'
import { useWebSocket } from './utils/websocket'

const authStore = useAuthStore()
const themeStore = useThemeStore()
const { connect } = useWebSocket()

onMounted(async () => {
  // Initialize theme immediately to prevent flash
  themeStore.initTheme()

  // Check if user is authenticated
  const token = localStorage.getItem('token')
  if (token) {
    authStore.token = token
    authStore.isAuthenticated = true
    // Set axios default authorization header
    axios.defaults.headers.common['Authorization'] = `Bearer ${token}`
    // Connect to WebSocket for real-time updates
    connect()
  }
})
</script>
