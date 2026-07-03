<template>
  <div class="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900">
    <div class="max-w-md w-full space-y-8">
      <div>
        <h2 class="mt-6 text-center text-3xl font-extrabold text-gray-900 dark:text-white">
          Trinity
        </h2>
        <p class="mt-2 text-center text-sm text-gray-600 dark:text-gray-400">
          Sign in to manage your agents
        </p>
      </div>

      <!-- Loading State (detecting mode or authenticating) -->
      <div v-if="isLoading" class="text-center">
        <div class="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600 mx-auto"></div>
        <p class="mt-4 text-gray-600 dark:text-gray-400">{{ loadingMessage }}</p>
      </div>

      <!-- Two-Factor step (#5) — shown after the first factor when 2FA is required -->
      <div v-else-if="authStore.mfaChallenge" class="mt-8 space-y-6 bg-white dark:bg-gray-800 rounded-lg shadow-lg dark:shadow-gray-900 p-8">
        <!-- Recovery codes shown once after forced enrollment -->
        <div v-if="mfaRecoveryCodes.length" class="space-y-4">
          <h3 class="text-lg font-medium text-gray-900 dark:text-white">Save your recovery codes</h3>
          <p class="text-sm text-gray-600 dark:text-gray-400">
            Store these somewhere safe — each works once if you lose your authenticator. They won't be shown again.
          </p>
          <div class="grid grid-cols-2 gap-1 font-mono text-sm text-gray-800 dark:text-gray-200 p-3 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800">
            <code v-for="c in mfaRecoveryCodes" :key="c" class="select-all">{{ c }}</code>
          </div>
          <button @click="finishMfa"
            class="w-full py-3 px-4 rounded-lg text-white bg-blue-600 hover:bg-blue-700">Continue</button>
        </div>

        <!-- Forced enrollment: scan QR + confirm -->
        <div v-else-if="mfaMode === 'enroll'" class="space-y-4">
          <h3 class="text-lg font-medium text-gray-900 dark:text-white">Set up two-factor authentication</h3>
          <p class="text-sm text-gray-600 dark:text-gray-400">
            Your account requires 2FA. Scan this QR with an authenticator app, then enter the 6-digit code.
          </p>
          <template v-if="mfaEnroll">
            <QrCode :value="mfaEnroll.otpauth_uri" />
            <div class="text-xs text-gray-500 dark:text-gray-400">
              Can't scan? Enter this key manually:
              <code class="block mt-1 select-all font-mono text-sm text-gray-800 dark:text-gray-200 break-all">{{ mfaEnroll.secret }}</code>
            </div>
          </template>
          <p v-else class="text-sm text-gray-500 dark:text-gray-400">Preparing enrollment…</p>

          <form @submit.prevent="handleMfaEnrollConfirm" class="space-y-3">
            <input v-model="mfaCode" type="text" inputmode="numeric" maxlength="6" placeholder="000000"
              autocomplete="one-time-code"
              class="block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 text-center text-2xl tracking-widest" />
            <button type="submit" :disabled="loginLoading || mfaCode.length < 6 || !mfaEnroll"
              class="w-full py-3 px-4 rounded-lg text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-50">
              {{ loginLoading ? 'Verifying…' : 'Confirm & Sign In' }}
            </button>
          </form>
        </div>

        <!-- Verify (already enrolled) -->
        <div v-else class="space-y-4">
          <h3 class="text-lg font-medium text-gray-900 dark:text-white">Two-factor authentication</h3>
          <p class="text-sm text-gray-600 dark:text-gray-400">
            Enter the 6-digit code from your authenticator app, or a recovery code.
          </p>
          <form @submit.prevent="handleMfaVerify" class="space-y-3">
            <input v-model="mfaCode" type="text" inputmode="text" autocomplete="one-time-code"
              placeholder="000000"
              class="block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 text-center text-2xl tracking-widest" />
            <button type="submit" :disabled="loginLoading || !mfaCode"
              class="w-full py-3 px-4 rounded-lg text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-50">
              {{ loginLoading ? 'Verifying…' : 'Verify & Sign In' }}
            </button>
          </form>
        </div>

        <p v-if="authError" class="text-sm text-red-600 dark:text-red-400 text-center">{{ authError }}</p>
        <button v-if="!mfaRecoveryCodes.length" @click="handleMfaCancel"
          class="w-full text-sm text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200">
          ← Cancel
        </button>
      </div>

      <!-- Error State -->
      <div v-else-if="authError" class="bg-white dark:bg-gray-800 rounded-lg shadow-lg dark:shadow-gray-900 p-8">
        <div class="text-center">
          <div class="text-status-danger-500 text-5xl mb-4">⚠️</div>
          <h3 class="text-xl font-bold text-gray-900 dark:text-white mb-4">Access Denied</h3>
          <p class="text-gray-600 dark:text-gray-400 mb-6">{{ authError }}</p>
          <button
            @click="handleRetry"
            class="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
          >
            Try Again
          </button>
        </div>
      </div>

      <!-- Login Forms -->
      <div v-else class="mt-8 space-y-6 bg-white dark:bg-gray-800 rounded-lg shadow-lg dark:shadow-gray-900 p-8">

        <!-- Email Authentication (Default) -->
        <div v-if="authStore.emailAuthEnabled && !showAdminLogin">
          <!-- Step 1: Enter Email -->
          <div v-if="!codeSent">
            <form @submit.prevent="handleRequestCode" class="space-y-4">
              <div>
                <label for="email" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                  Email Address
                </label>
                <input
                  id="email"
                  v-model="emailInput"
                  type="email"
                  required
                  autocomplete="email"
                  class="mt-1 block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500"
                  placeholder="you@example.com"
                />
              </div>

              <button
                type="submit"
                :disabled="loginLoading || !emailInput"
                class="w-full flex justify-center py-3 px-4 border border-transparent text-sm font-medium rounded-lg text-white bg-blue-600 hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 dark:focus:ring-offset-gray-800 disabled:opacity-50 transition-colors"
              >
                {{ loginLoading ? 'Sending code...' : 'Send Verification Code' }}
              </button>
            </form>
          </div>

          <!-- Step 2: Enter Code -->
          <div v-else>
            <div class="mb-4">
              <p class="text-sm text-gray-600 dark:text-gray-400">
                📧 We sent a 6-digit code to <strong class="text-gray-900 dark:text-white">{{ emailInput }}</strong>
              </p>
              <p v-if="countdown > 0" class="text-xs text-gray-500 dark:text-gray-500 mt-1">
                Code expires in {{ formatTime(countdown) }}
              </p>
            </div>

            <form @submit.prevent="handleVerifyCode" class="space-y-4">
              <div>
                <label for="code" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                  Verification Code
                </label>
                <input
                  id="code"
                  v-model="codeInput"
                  type="text"
                  required
                  maxlength="6"
                  pattern="[0-9]{6}"
                  autocomplete="one-time-code"
                  class="mt-1 block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 text-center text-2xl tracking-widest placeholder-gray-400 dark:placeholder-gray-500"
                  placeholder="000000"
                />
              </div>

              <button
                type="submit"
                :disabled="loginLoading || codeInput.length !== 6"
                class="w-full flex justify-center py-3 px-4 border border-transparent text-sm font-medium rounded-lg text-white bg-blue-600 hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 dark:focus:ring-offset-gray-800 disabled:opacity-50 transition-colors"
              >
                {{ loginLoading ? 'Verifying...' : 'Verify & Sign In' }}
              </button>

              <button
                type="button"
                @click="handleBackToEmail"
                class="w-full text-sm text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200 transition-colors"
              >
                ← Back to email
              </button>
            </form>
          </div>

          <!-- Admin Login Option -->
          <div v-if="!codeSent" class="mt-6 pt-6 border-t border-gray-200 dark:border-gray-700">
            <button
              @click="showAdminLogin = true"
              class="w-full text-sm py-2 px-4 border border-gray-300 dark:border-gray-600 rounded-lg text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
            >
              🔐 Admin Login
            </button>
          </div>

          <!-- #32 — Enterprise SSO (OIDC). Buttons appear only when the `sso`
               feature is entitled and at least one provider is enabled.
               Full-page nav to the backend login endpoint → IdP → callback. -->
          <div v-if="!codeSent && ssoProviders.length" class="mt-6 pt-6 border-t border-gray-200 dark:border-gray-700 space-y-2">
            <p class="text-xs text-center text-gray-500 dark:text-gray-400">Or sign in with</p>
            <a
              v-for="p in ssoProviders"
              :key="p.id"
              :href="`/api/enterprise/sso/login/${p.id}`"
              class="w-full flex justify-center py-2 px-4 border border-gray-300 dark:border-gray-600 rounded-lg text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
            >
              🔑 {{ p.name }}
            </a>
          </div>
        </div>

        <!-- Admin Login: Password Only (username is fixed as 'admin') -->
        <div v-else-if="showAdminLogin || !authStore.emailAuthEnabled">
          <div class="mb-4 p-3 bg-gray-50 dark:bg-gray-900/50 border border-gray-200 dark:border-gray-700 rounded-lg">
            <p class="text-sm text-gray-600 dark:text-gray-400 flex items-center">
              <span class="mr-2">🔐</span>
              Admin Login
            </p>
          </div>

          <form @submit.prevent="handleAdminLogin" class="space-y-4">
            <div>
              <label for="adminIdentifier" class="block text-sm font-medium text-gray-700 dark:text-gray-300">Username or email</label>
              <input
                id="adminIdentifier"
                v-model="adminIdentifier"
                type="text"
                required
                autocomplete="username"
                class="mt-1 block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500"
                placeholder="admin or you@company.com"
              />
            </div>

            <div>
              <label for="password" class="block text-sm font-medium text-gray-700 dark:text-gray-300">Password</label>
              <input
                id="password"
                v-model="password"
                type="password"
                required
                autocomplete="current-password"
                class="mt-1 block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500"
                placeholder="Enter admin password"
              />
            </div>

            <button
              type="submit"
              :disabled="loginLoading || !password"
              class="w-full flex justify-center py-3 px-4 border border-transparent text-sm font-medium rounded-lg text-white bg-blue-600 hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 dark:focus:ring-offset-gray-800 disabled:opacity-50 transition-colors"
            >
              {{ loginLoading ? 'Signing in...' : 'Sign In as Admin' }}
            </button>
          </form>

          <button
            v-if="authStore.emailAuthEnabled"
            @click="showAdminLogin = false"
            class="w-full mt-4 text-sm text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200 transition-colors"
          >
            ← Back to email login
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import QrCode from '../components/QrCode.vue'

const router = useRouter()
const authStore = useAuthStore()

// Local state for admin login form. #82 Phase 1: the admin may sign in with the
// email they registered at setup (or in Settings) instead of the fixed 'admin'.
const adminIdentifier = ref('admin')
const password = ref('')
const loginLoading = ref(false)
const loadingMessage = ref('Checking authentication...')

// Email authentication state
const emailInput = ref('')
const codeInput = ref('')
const codeSent = ref(false)
const countdown = ref(0)
const countdownInterval = ref(null)

// UI state for switching between login methods
const showAdminLogin = ref(false)

// Two-factor step state (#5)
const mfaCode = ref('')
const mfaEnroll = ref(null)            // provisioning payload during forced enrollment
const mfaRecoveryCodes = ref([])
const ssoProviders = ref([])           // #32 — enabled SSO IdPs (login buttons)
const mfaMode = computed(() =>
  authStore.mfaChallenge?.enrollmentRequired ? 'enroll' : 'verify'
)

// When a forced-enrollment challenge appears, fetch the provisioning QR.
watch(() => authStore.mfaChallenge, async (ch) => {
  mfaCode.value = ''
  mfaEnroll.value = null
  if (ch && ch.enrollmentRequired) {
    mfaEnroll.value = await authStore.startMfaEnrollment()
  }
}, { immediate: true })

// Computed
const isLoading = computed(() => {
  // Still detecting mode
  if (!authStore.modeDetected) return true
  // Auth store is loading
  if (authStore.isLoading) return true
  return false
})

const authError = computed(() => {
  return authStore.authError
})

// Format countdown time (MM:SS)
const formatTime = (seconds) => {
  const mins = Math.floor(seconds / 60)
  const secs = seconds % 60
  return `${mins}:${secs.toString().padStart(2, '0')}`
}

// Start countdown timer
const startCountdown = (seconds) => {
  countdown.value = seconds
  if (countdownInterval.value) {
    clearInterval(countdownInterval.value)
  }
  countdownInterval.value = setInterval(() => {
    countdown.value--
    if (countdown.value <= 0) {
      clearInterval(countdownInterval.value)
      countdownInterval.value = null
    }
  }, 1000)
}

// Cleanup countdown on unmount
onUnmounted(() => {
  if (countdownInterval.value) {
    clearInterval(countdownInterval.value)
  }
})

// Email authentication handlers
const handleRequestCode = async () => {
  loginLoading.value = true
  authStore.clearError()

  const result = await authStore.requestEmailCode(emailInput.value)
  if (result.success) {
    codeSent.value = true
    codeInput.value = ''
    startCountdown(result.expiresInSeconds || 600)
  }

  loginLoading.value = false
}

const handleVerifyCode = async () => {
  loginLoading.value = true
  authStore.clearError()

  const success = await authStore.verifyEmailCode(emailInput.value, codeInput.value)
  if (success) {
    router.push('/')
  }

  loginLoading.value = false
}

const handleBackToEmail = () => {
  codeSent.value = false
  codeInput.value = ''
  if (countdownInterval.value) {
    clearInterval(countdownInterval.value)
    countdownInterval.value = null
  }
  countdown.value = 0
}

// Initialize on mount
onMounted(async () => {
  // Wait for mode detection (happens in initializeAuth)
  if (!authStore.modeDetected) {
    await authStore.detectAuthMode()
  }

  // If already authenticated, redirect to dashboard
  if (authStore.isAuthenticated) {
    router.push('/')
    return
  }

  // #32 — handle an SSO (OIDC) callback redirect: the backend lands us back at
  // /login with the result in the URL fragment. Consume it, then strip it from
  // the address bar so a refresh/back can't replay it.
  if (window.location.hash.includes('sso=')) {
    const params = new URLSearchParams(window.location.hash.slice(1))
    history.replaceState(null, '', window.location.pathname + window.location.search)
    const res = await authStore.completeSsoLogin(params)
    if (res.ok && !res.mfa) {
      router.push('/')
      return
    }
    // mfa → the existing 2FA challenge UI takes over; error → authError shows.
  }

  // Populate SSO login buttons (no-op / empty in OSS-only builds).
  ssoProviders.value = await authStore.fetchSsoProviders()
})

// Handle admin login (username 'admin' OR the admin's registered email — #82 Phase 1)
const handleAdminLogin = async () => {
  loginLoading.value = true
  authStore.clearError()

  const identifier = (adminIdentifier.value || 'admin').trim()
  const success = await authStore.loginWithCredentials(identifier, password.value)
  if (success) {
    router.push('/')
  }

  loginLoading.value = false
}

// --- Two-factor handlers (#5) ---
const handleMfaVerify = async () => {
  loginLoading.value = true
  authStore.clearError()
  const ok = await authStore.verifyMfaCode(mfaCode.value.trim())
  loginLoading.value = false
  if (ok) router.push('/')
}

const handleMfaEnrollConfirm = async () => {
  loginLoading.value = true
  authStore.clearError()
  const { ok, recoveryCodes } = await authStore.confirmMfaEnrollment(mfaCode.value.trim())
  loginLoading.value = false
  if (ok) {
    // Already authenticated (token minted); show the codes, then continue.
    mfaRecoveryCodes.value = recoveryCodes || []
    if (!mfaRecoveryCodes.value.length) router.push('/')
  }
}

const finishMfa = () => {
  mfaRecoveryCodes.value = []
  router.push('/')
}

const handleMfaCancel = () => {
  authStore.cancelMfa()
  authStore.clearError()
  mfaCode.value = ''
  mfaEnroll.value = null
  mfaRecoveryCodes.value = []
  password.value = ''
  handleBackToEmail()
}

// Handle retry after error
const handleRetry = () => {
  authStore.clearError()
  codeSent.value = false
  codeInput.value = ''
  emailInput.value = ''
  password.value = ''
  showAdminLogin.value = false
  handleBackToEmail()
}
</script>
