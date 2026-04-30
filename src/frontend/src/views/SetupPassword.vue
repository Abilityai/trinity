<template>
  <div class="min-h-screen bg-gray-100 dark:bg-gray-900 flex items-center justify-center px-4">
    <div class="max-w-md w-full">
      <!-- Logo/Header -->
      <div class="text-center mb-8">
        <div class="inline-flex items-center justify-center w-16 h-16 rounded-full bg-indigo-100 dark:bg-indigo-900/50 mb-4">
          <svg class="w-8 h-8 text-indigo-600 dark:text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
          </svg>
        </div>
        <h1 class="text-2xl font-bold text-gray-900 dark:text-white">Set Admin Password</h1>
        <p class="mt-2 text-sm text-gray-600 dark:text-gray-400">
          Create a password for the admin account to get started.
        </p>
      </div>

      <!-- Setup Form -->
      <div class="bg-white dark:bg-gray-800 shadow-lg rounded-lg p-6">
        <form @submit.prevent="handleSubmit" class="space-y-4">
          <!-- Setup Token Field -->
          <div>
            <label for="setupToken" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
              Setup Token
            </label>
            <div class="mt-1">
              <input
                type="text"
                id="setupToken"
                v-model="setupToken"
                :disabled="loading"
                class="block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 dark:bg-gray-700 dark:text-white font-mono text-sm"
                placeholder="Paste token from server logs"
                required
                autocomplete="off"
              />
            </div>
            <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Check server logs (<code class="bg-gray-100 dark:bg-gray-700 px-1 rounded">docker compose logs backend</code>) for the setup token printed at startup.
            </p>
          </div>

          <!-- Password Field -->
          <div>
            <label for="password" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
              Password
            </label>
            <div class="mt-1 relative">
              <input
                :type="showPassword ? 'text' : 'password'"
                id="password"
                v-model="password"
                :disabled="loading"
                class="block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 dark:bg-gray-700 dark:text-white"
                placeholder="Enter password (12+ characters)"
                required
                minlength="12"
              />
              <button
                type="button"
                @click="showPassword = !showPassword"
                class="absolute inset-y-0 right-0 pr-3 flex items-center text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
              >
                <svg v-if="showPassword" class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
                </svg>
                <svg v-else class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                </svg>
              </button>
            </div>
          </div>

          <!-- Confirm Password Field -->
          <div>
            <label for="confirmPassword" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
              Confirm Password
            </label>
            <div class="mt-1 relative">
              <input
                :type="showConfirmPassword ? 'text' : 'password'"
                id="confirmPassword"
                v-model="confirmPassword"
                :disabled="loading"
                class="block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 dark:bg-gray-700 dark:text-white"
                placeholder="Confirm your password"
                required
                minlength="12"
              />
              <button
                type="button"
                @click="showConfirmPassword = !showConfirmPassword"
                class="absolute inset-y-0 right-0 pr-3 flex items-center text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
              >
                <svg v-if="showConfirmPassword" class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
                </svg>
                <svg v-else class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                </svg>
              </button>
            </div>
          </div>

          <!-- Password Match Indicator -->
          <div v-if="password && confirmPassword" class="flex items-center text-sm">
            <svg v-if="passwordsMatch" class="h-4 w-4 text-status-success-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
            </svg>
            <svg v-else class="h-4 w-4 text-status-danger-500 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
            </svg>
            <span :class="passwordsMatch ? 'text-status-success-600 dark:text-status-success-400' : 'text-status-danger-600 dark:text-status-danger-400'">
              {{ passwordsMatch ? 'Passwords match' : 'Passwords do not match' }}
            </span>
          </div>

          <!-- Password Strength Indicator -->
          <div v-if="password" class="text-sm">
            <div class="flex items-center">
              <span class="text-gray-500 dark:text-gray-400 mr-2">Strength:</span>
              <span :class="passwordStrengthClass">{{ passwordStrengthText }}</span>
            </div>
            <div class="mt-1 h-1 w-full bg-gray-200 dark:bg-gray-700 rounded">
              <div
                class="h-full rounded transition-all duration-300"
                :class="passwordStrengthBarClass"
                :style="{ width: `${passwordStrength * 20}%` }"
              ></div>
            </div>
          </div>

          <!-- Password Requirements Checklist -->
          <div v-if="password" class="space-y-1 text-sm">
            <div v-for="req in passwordRequirements" :key="req.label" class="flex items-center">
              <svg v-if="req.met" class="h-4 w-4 text-status-success-500 mr-2 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
              </svg>
              <svg v-else class="h-4 w-4 text-gray-400 mr-2 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <circle cx="12" cy="12" r="10" stroke-width="2" />
              </svg>
              <span :class="req.met ? 'text-status-success-600 dark:text-status-success-400' : 'text-gray-500 dark:text-gray-400'">
                {{ req.label }}
              </span>
            </div>
          </div>

          <!-- Error Message -->
          <div v-if="error" class="rounded-md bg-status-danger-50 dark:bg-status-danger-900/30 p-4">
            <div class="flex">
              <svg class="h-5 w-5 text-status-danger-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              <div class="ml-3">
                <p class="text-sm text-status-danger-700 dark:text-status-danger-300">{{ error }}</p>
              </div>
            </div>
          </div>

          <!-- Submit Button -->
          <button
            type="submit"
            :disabled="!isValid || loading"
            class="w-full flex justify-center py-2 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <svg v-if="loading" class="animate-spin -ml-1 mr-3 h-5 w-5 text-white" fill="none" viewBox="0 0 24 24">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
            {{ loading ? 'Setting Password...' : 'Set Password & Continue' }}
          </button>
        </form>
      </div>

      <!-- Info Text -->
      <p class="mt-4 text-center text-xs text-gray-500 dark:text-gray-400">
        You'll use this password to log in as the admin user.
      </p>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import axios from 'axios'
import { clearSetupCache } from '../router'

const router = useRouter()

const setupToken = ref('')
const password = ref('')
const confirmPassword = ref('')
const showPassword = ref(false)
const showConfirmPassword = ref(false)
const loading = ref(false)
const error = ref(null)

const passwordsMatch = computed(() => {
  return password.value === confirmPassword.value
})

const passwordRequirements = computed(() => {
  const p = password.value
  return [
    { label: 'At least 12 characters', met: p.length >= 12 },
    { label: 'Uppercase letter (A-Z)', met: /[A-Z]/.test(p) },
    { label: 'Lowercase letter (a-z)', met: /[a-z]/.test(p) },
    { label: 'Number (0-9)', met: /[0-9]/.test(p) },
    { label: 'Special character (!@#$...)', met: /[^A-Za-z0-9]/.test(p) },
  ]
})

const passwordStrength = computed(() => {
  return passwordRequirements.value.filter(r => r.met).length
})

const passwordStrengthText = computed(() => {
  const texts = ['Very Weak', 'Weak', 'Fair', 'Good', 'Strong', 'Excellent']
  return texts[passwordStrength.value] || 'Very Weak'
})

const passwordStrengthClass = computed(() => {
  const classes = [
    'text-status-danger-600 dark:text-status-danger-400',
    'text-status-danger-600 dark:text-status-danger-400',
    'text-status-urgent-600 dark:text-status-urgent-400',
    'text-status-warning-600 dark:text-status-warning-400',
    'text-status-success-600 dark:text-status-success-400',
    'text-status-success-600 dark:text-status-success-400'
  ]
  return classes[passwordStrength.value] || classes[0]
})

const passwordStrengthBarClass = computed(() => {
  const classes = [
    'bg-status-danger-500',
    'bg-status-danger-500',
    'bg-status-urgent-500',
    'bg-status-warning-500',
    'bg-status-success-500',
    'bg-status-success-500'
  ]
  return classes[passwordStrength.value] || classes[0]
})

const isValid = computed(() => {
  return setupToken.value.length > 0 && passwordRequirements.value.every(r => r.met) && passwordsMatch.value
})

async function handleSubmit() {
  if (!isValid.value) return

  loading.value = true
  error.value = null

  try {
    await axios.post('/api/setup/admin-password', {
      setup_token: setupToken.value,
      password: password.value,
      confirm_password: confirmPassword.value
    })

    // Clear the cache so router knows setup is done
    clearSetupCache()

    // Redirect to login page
    router.push('/login')
  } catch (e) {
    if (e.response?.status === 403) {
      const detail = e.response?.data?.detail || ''
      if (detail.toLowerCase().includes('token')) {
        error.value = 'Invalid setup token. Check server logs for the correct token.'
      } else {
        error.value = 'Setup has already been completed.'
        setTimeout(() => router.push('/login'), 2000)
      }
    } else {
      error.value = e.response?.data?.detail || 'Failed to set password. Please try again.'
    }
  } finally {
    loading.value = false
  }
}
</script>
