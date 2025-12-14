<template>
  <nav class="bg-white shadow">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
      <div class="flex justify-between h-16">
        <div class="flex">
          <div class="flex-shrink-0 flex items-center">
            <img src="../assets/trinity-logo.svg" alt="Trinity Logo" class="h-8 w-8 mr-2" />
            <h1 class="text-xl font-bold text-gray-900">Trinity</h1>
          </div>
          <div class="hidden sm:ml-6 sm:flex sm:space-x-8">
            <router-link
              to="/"
              class="border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium"
              :class="{ 'border-blue-500 text-gray-900': $route.path === '/' }"
            >
              Dashboard
            </router-link>
            <router-link
              to="/agents"
              class="border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium"
              :class="{ 'border-blue-500 text-gray-900': $route.path === '/agents' || $route.path.startsWith('/agents/') }"
            >
              Agents
            </router-link>
            <router-link
              to="/credentials"
              class="border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium"
              :class="{ 'border-blue-500 text-gray-900': $route.path === '/credentials' }"
            >
              Credentials
            </router-link>
            <router-link
              to="/templates"
              class="border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium"
              :class="{ 'border-blue-500 text-gray-900': $route.path === '/templates' }"
            >
              Templates
            </router-link>
            <router-link
              to="/api-keys"
              class="border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium"
              :class="{ 'border-blue-500 text-gray-900': $route.path === '/api-keys' }"
            >
              MCP Keys
            </router-link>
            <router-link
              v-if="isAdmin"
              to="/settings"
              class="border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium"
              :class="{ 'border-blue-500 text-gray-900': $route.path === '/settings' }"
            >
              Settings
            </router-link>
          </div>
        </div>
        <div class="flex items-center space-x-4">
          <!-- WebSocket Status -->
          <span class="text-sm text-gray-500">
            <span class="inline-block h-2 w-2 rounded-full mr-1" :class="isConnected ? 'bg-green-400' : 'bg-gray-400'"></span>
            {{ isConnected ? 'Connected' : 'Disconnected' }}
          </span>

          <!-- User Menu -->
          <div class="relative" ref="userMenuRef">
            <button
              @click="toggleUserMenu"
              class="flex items-center space-x-2 focus:outline-none"
            >
              <!-- User Avatar -->
              <div
                v-if="authStore.userPicture"
                class="w-8 h-8 rounded-full overflow-hidden border-2 border-gray-200 hover:border-blue-400 transition-colors"
              >
                <img
                  :src="authStore.userPicture"
                  :alt="authStore.userName"
                  class="w-full h-full object-cover"
                />
              </div>
              <div
                v-else
                class="w-8 h-8 rounded-full bg-blue-500 text-white flex items-center justify-center text-sm font-medium border-2 border-gray-200 hover:border-blue-400 transition-colors"
              >
                {{ authStore.userInitials }}
              </div>
            </button>

            <!-- Dropdown Menu -->
            <div
              v-if="showUserMenu"
              class="absolute right-0 mt-2 w-56 rounded-lg bg-white shadow-lg ring-1 ring-black ring-opacity-5 py-1 z-50"
            >
              <div class="px-4 py-3 border-b border-gray-100">
                <p class="text-sm font-medium text-gray-900 truncate">{{ authStore.userName }}</p>
                <p class="text-xs text-gray-500 truncate">{{ authStore.userEmail }}</p>
              </div>
              <button
                @click="handleLogout"
                class="w-full text-left px-4 py-2 text-sm text-gray-700 hover:bg-gray-100 flex items-center"
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
import { useWebSocket } from '../utils/websocket'
import axios from 'axios'

const router = useRouter()
const authStore = useAuthStore()
const { isConnected } = useWebSocket()

// Check if user is admin (fetch from backend)
const userRole = ref(null)
const isAdmin = computed(() => userRole.value === 'admin')

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
    const response = await axios.get('/api/users/me')
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
