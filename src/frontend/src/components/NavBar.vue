<template>
  <nav class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
      <div class="flex justify-between h-16">
        <div class="flex">
          <div class="flex-shrink-0 flex items-center">
            <img src="../assets/trinity-logo.svg" alt="Trinity Logo" class="h-8 w-8 mr-2" />
            <h1 class="text-xl font-bold text-gray-900 dark:text-white">Trinity</h1>
          </div>
          <div class="hidden sm:ml-6 sm:flex sm:space-x-8">
            <router-link
              to="/"
              class="border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-200 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium"
              :class="{ 'border-blue-500 dark:border-blue-400 text-gray-900 dark:text-white': $route.path === '/' }"
            >
              Dashboard
            </router-link>
            <router-link
              to="/agents"
              class="border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-200 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium"
              :class="{ 'border-blue-500 dark:border-blue-400 text-gray-900 dark:text-white': $route.path === '/agents' || $route.path.startsWith('/agents/') }"
            >
              Agents
            </router-link>
            <router-link
              to="/credentials"
              class="border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-200 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium"
              :class="{ 'border-blue-500 dark:border-blue-400 text-gray-900 dark:text-white': $route.path === '/credentials' }"
            >
              Credentials
            </router-link>
            <router-link
              to="/templates"
              class="border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-200 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium"
              :class="{ 'border-blue-500 dark:border-blue-400 text-gray-900 dark:text-white': $route.path === '/templates' }"
            >
              Templates
            </router-link>
            <router-link
              to="/api-keys"
              class="border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-200 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium"
              :class="{ 'border-blue-500 dark:border-blue-400 text-gray-900 dark:text-white': $route.path === '/api-keys' }"
            >
              MCP Keys
            </router-link>
            <router-link
              v-if="isAdmin"
              to="/system-agent"
              class="border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-200 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium"
              :class="{ 'border-blue-500 dark:border-blue-400 text-gray-900 dark:text-white': $route.path === '/system-agent' }"
            >
              <svg class="w-4 h-4 mr-1 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z" />
              </svg>
              System
            </router-link>
            <router-link
              v-if="isAdmin"
              to="/settings"
              class="border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-200 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium"
              :class="{ 'border-blue-500 dark:border-blue-400 text-gray-900 dark:text-white': $route.path === '/settings' }"
            >
              Settings
            </router-link>
          </div>
        </div>
        <div class="flex items-center space-x-4">
          <!-- WebSocket Status -->
          <span class="text-sm text-gray-500 dark:text-gray-400">
            <span class="inline-block h-2 w-2 rounded-full mr-1" :class="isConnected ? 'bg-green-400' : 'bg-gray-400 dark:bg-gray-600'"></span>
            {{ isConnected ? 'Connected' : 'Disconnected' }}
          </span>

          <!-- Theme Toggle Button -->
          <button
            @click="cycleTheme"
            class="p-2 rounded-lg text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-500"
            :title="themeTitle"
          >
            <!-- Sun icon for light mode -->
            <svg v-if="themeStore.theme === 'light'" class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
            </svg>
            <!-- Moon icon for dark mode -->
            <svg v-else-if="themeStore.theme === 'dark'" class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
            </svg>
            <!-- Computer/System icon for system mode -->
            <svg v-else class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
            </svg>
          </button>

          <!-- User Menu -->
          <div class="relative" ref="userMenuRef">
            <button
              @click="toggleUserMenu"
              class="flex items-center space-x-2 focus:outline-none"
            >
              <!-- User Avatar -->
              <div
                v-if="authStore.userPicture"
                class="w-8 h-8 rounded-full overflow-hidden border-2 border-gray-200 dark:border-gray-600 hover:border-blue-400 dark:hover:border-blue-500 transition-colors"
              >
                <img
                  :src="authStore.userPicture"
                  :alt="authStore.userName"
                  class="w-full h-full object-cover"
                />
              </div>
              <div
                v-else
                class="w-8 h-8 rounded-full bg-blue-500 text-white flex items-center justify-center text-sm font-medium border-2 border-gray-200 dark:border-gray-600 hover:border-blue-400 dark:hover:border-blue-500 transition-colors"
              >
                {{ authStore.userInitials }}
              </div>
            </button>

            <!-- Dropdown Menu -->
            <div
              v-if="showUserMenu"
              class="absolute right-0 mt-2 w-56 rounded-lg bg-white dark:bg-gray-800 shadow-lg ring-1 ring-black ring-opacity-5 dark:ring-gray-700 py-1 z-50"
            >
              <div class="px-4 py-3 border-b border-gray-100 dark:border-gray-700">
                <p class="text-sm font-medium text-gray-900 dark:text-white truncate">{{ authStore.userName }}</p>
                <p class="text-xs text-gray-500 dark:text-gray-400 truncate">{{ authStore.userEmail }}</p>
              </div>
              <!-- Theme Selector in Menu -->
              <div class="px-4 py-2 border-b border-gray-100 dark:border-gray-700">
                <p class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">Theme</p>
                <div class="flex space-x-1">
                  <button
                    @click="setTheme('light')"
                    class="flex-1 px-2 py-1.5 text-xs rounded-md flex items-center justify-center space-x-1"
                    :class="themeStore.theme === 'light' ? 'bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300' : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'"
                  >
                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
                    </svg>
                    <span>Light</span>
                  </button>
                  <button
                    @click="setTheme('dark')"
                    class="flex-1 px-2 py-1.5 text-xs rounded-md flex items-center justify-center space-x-1"
                    :class="themeStore.theme === 'dark' ? 'bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300' : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'"
                  >
                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
                    </svg>
                    <span>Dark</span>
                  </button>
                  <button
                    @click="setTheme('system')"
                    class="flex-1 px-2 py-1.5 text-xs rounded-md flex items-center justify-center space-x-1"
                    :class="themeStore.theme === 'system' ? 'bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300' : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'"
                  >
                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
                    </svg>
                    <span>Auto</span>
                  </button>
                </div>
              </div>
              <button
                @click="handleLogout"
                class="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center"
              >
                <svg class="w-4 h-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
                </svg>
                Sign out
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </nav>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuth0 } from '@auth0/auth0-vue'
import { useAuthStore } from '../stores/auth'
import { useThemeStore } from '../stores/theme'
import { useWebSocket } from '../utils/websocket'
import axios from 'axios'

const router = useRouter()
const authStore = useAuthStore()
const themeStore = useThemeStore()
const { isConnected } = useWebSocket()

// Check if user is admin (fetch from backend)
const userRole = ref(null)
const isAdmin = computed(() => userRole.value === 'admin')

// Theme management
const themeTitle = computed(() => {
  const titles = {
    light: 'Light mode (click to switch)',
    dark: 'Dark mode (click to switch)',
    system: 'System theme (click to switch)'
  }
  return titles[themeStore.theme]
})

const cycleTheme = () => {
  themeStore.toggleTheme()
}

const setTheme = (theme) => {
  themeStore.setTheme(theme)
}

// Auth0 is always available (plugin always loaded)
const auth0 = useAuth0()

// User menu state
const showUserMenu = ref(false)
const userMenuRef = ref(null)

onMounted(async () => {
  // Add click outside listener
  document.addEventListener('click', handleClickOutside)

  // Fetch user role from backend
  try {
    const response = await axios.get('/api/users/me', {
      headers: authStore.authHeader
    })
    userRole.value = response.data.role
  } catch (e) {
    console.warn('Failed to fetch user role:', e)
  }
})

onUnmounted(() => {
  document.removeEventListener('click', handleClickOutside)
})

const toggleUserMenu = () => {
  showUserMenu.value = !showUserMenu.value
}

const handleClickOutside = (event) => {
  if (userMenuRef.value && !userMenuRef.value.contains(event.target)) {
    showUserMenu.value = false
  }
}

const handleLogout = () => {
  showUserMenu.value = false

  // Clear local auth state and handle Auth0 logout if in prod mode
  // The authStore.logout() will call auth0.logout() if not in dev mode
  authStore.logout(auth0.logout)

  // Redirect to login
  router.push('/login')
}
</script>
