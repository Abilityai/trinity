import { defineStore } from 'pinia'
import axios from 'axios'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: null,
    user: null,
    isAuthenticated: false,
    isLoading: true,
    authError: null,
    // Runtime mode detection (from backend)
    emailAuthEnabled: null,  // Email-based authentication
    modeDetected: false,
    // Promise that resolves when initializeAuth() completes (PERF-269)
    _initResolve: null,
    _initPromise: null
  }),

  getters: {
    authHeader() {
      return this.token ? { Authorization: `Bearer ${this.token}` } : {}
    },

    userEmail() {
      return this.user?.email || null
    },

    userName() {
      return this.user?.name || this.user?.email || 'User'
    },

    userInitials() {
      const name = this.userName
      if (!name) return '?'
      return name.split(' ').map(n => n[0]).join('').toUpperCase().slice(0, 2)
    },

    userPicture() {
      return this.user?.picture || null
    }
  },

  actions: {
    // Detect authentication mode from backend (called before login)
    async detectAuthMode() {
      try {
        const response = await axios.get('/api/auth/mode')
        this.emailAuthEnabled = response.data.email_auth_enabled !== false
        this.modeDetected = true

        console.log(`🔐 Auth mode: EMAIL=${this.emailAuthEnabled}`)
        return true
      } catch (error) {
        console.error('Failed to detect auth mode:', error)
        // Default to email auth if detection fails
        this.emailAuthEnabled = true
        this.modeDetected = true
        return true
      }
    },

    // Returns a promise that resolves when auth initialization is complete (PERF-269)
    waitForInit() {
      if (!this.isLoading) return Promise.resolve()
      if (!this._initPromise) {
        this._initPromise = new Promise(resolve => {
          this._initResolve = resolve
        })
      }
      return this._initPromise
    },

    // Initialize auth - called on app startup
    async initializeAuth() {
      this.isLoading = true
      this.authError = null

      // First detect auth mode from backend
      await this.detectAuthMode()

      const storedToken = localStorage.getItem('token')
      const storedUser = localStorage.getItem('auth0_user')

      if (storedToken && storedUser) {
        try {
          const user = JSON.parse(storedUser)

          // Check token validity
          // Parse JWT to get mode claim (without verification - just for client-side check)
          const tokenPayload = this.parseJwtPayload(storedToken)
          const tokenMode = tokenPayload?.mode

          // Valid token modes: admin, email, prod (Auth0)
          // All modes are accepted - no cross-mode restrictions needed
          if (tokenMode) {
            // Restore the session from localStorage
            this.token = storedToken
            this.user = user
            this.isAuthenticated = true
            this.setupAxiosAuth()
            console.log('✅ Session restored for:', user.email || user.name)
          }
        } catch (e) {
          console.warn('Failed to parse stored user, clearing credentials')
          localStorage.removeItem('token')
          localStorage.removeItem('auth0_user')
        }
      }

      this.isLoading = false
      // Resolve the init promise so router guards can proceed (PERF-269)
      if (this._initResolve) {
        this._initResolve()
        this._initResolve = null
        this._initPromise = null
      }
    },

    // Parse JWT payload without verification (client-side mode check only)
    parseJwtPayload(token) {
      try {
        const base64Url = token.split('.')[1]
        const base64 = base64Url.replace(/-/g, '+').replace(/_/g, '/')
        const jsonPayload = decodeURIComponent(
          atob(base64).split('').map(c =>
            '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2)
          ).join('')
        )
        return JSON.parse(jsonPayload)
      } catch (e) {
        return null
      }
    },

    // Setup axios Authorization header for API calls.
    //
    // Issue #188 (UnderDefense pentest 3.3.5): the token used to be
    // mirrored into a `token` cookie here so an nginx `auth_request`
    // could validate it. That nginx directive was never actually
    // configured in any deployment (`grep -r auth_request *.conf` is
    // empty), so the cookie was a pure attack-surface gift — readable
    // via document.cookie (no HttpOnly flag — JS-set cookies cannot
    // be HttpOnly), sent over HTTP without the Secure flag, and
    // automatically attached to every request as a CSRF vector.
    //
    // Removed entirely. API auth uses the Authorization: Bearer header
    // exclusively. The clear-on-logout below stays so users carrying a
    // cookie from a pre-fix version get cleaned up on next logout (the
    // cookie's max-age=1800 also expires it within 30 minutes).
    setupAxiosAuth() {
      if (this.token) {
        axios.defaults.headers.common['Authorization'] = `Bearer ${this.token}`
      }
    },

    // Login with username/password (for admin login)
    async loginWithCredentials(username, password) {
      try {
        const formData = new FormData()
        formData.append('username', username)
        formData.append('password', password)

        const response = await axios.post('/api/token', formData)
        this.token = response.data.access_token

        // Create a dev user profile
        const devUser = {
          sub: `local|${username}`,
          email: `${username}@localhost`,
          name: username,
          email_verified: true
        }

        this.user = devUser
        this.isAuthenticated = true

        localStorage.setItem('token', this.token)
        localStorage.setItem('auth0_user', JSON.stringify(devUser))
        this.setupAxiosAuth()

        console.log('🔐 Admin login: authenticated as', username)
        return true
      } catch (error) {
        console.error('Admin login failed:', error)
        const detail = error.response?.data?.detail || 'Invalid username or password'
        this.authError = detail
        return false
      }
    },

    // =========================================================================
    // Email-Based Authentication (Phase 12.4)
    // =========================================================================

    // Request a verification code via email
    async requestEmailCode(email) {
      if (!this.emailAuthEnabled) {
        this.authError = 'Email authentication is disabled'
        return { success: false, error: 'Email authentication is disabled' }
      }

      try {
        const response = await axios.post('/api/auth/email/request', { email })
        return {
          success: true,
          message: response.data.message,
          expiresInSeconds: response.data.expires_in_seconds
        }
      } catch (error) {
        console.error('Request email code failed:', error)
        const detail = error.response?.data?.detail || 'Failed to send verification code'
        this.authError = detail
        return { success: false, error: detail }
      }
    },

    // Verify email code and login
    async verifyEmailCode(email, code) {
      if (!this.emailAuthEnabled) {
        this.authError = 'Email authentication is disabled'
        return false
      }

      try {
        const response = await axios.post('/api/auth/email/verify', { email, code })

        this.token = response.data.access_token
        this.user = response.data.user
        this.isAuthenticated = true

        localStorage.setItem('token', this.token)
        localStorage.setItem('auth0_user', JSON.stringify(this.user))
        this.setupAxiosAuth()

        console.log('📧 Email auth: authenticated as', this.user.email)
        return true
      } catch (error) {
        console.error('Verify email code failed:', error)
        const detail = error.response?.data?.detail || 'Invalid or expired verification code'
        this.authError = detail
        return false
      }
    },

    // Logout
    logout() {
      this.token = null
      this.user = null
      this.isAuthenticated = false
      this.authError = null

      localStorage.removeItem('token')
      localStorage.removeItem('auth0_user')
      delete axios.defaults.headers.common['Authorization']

      // Clear the token cookie
      document.cookie = 'token=; path=/; max-age=0'
    },

    // Clear auth error
    clearError() {
      this.authError = null
    }
  }
})
