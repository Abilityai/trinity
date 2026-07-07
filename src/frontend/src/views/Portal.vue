<template>
  <div class="min-h-screen bg-gray-50 dark:bg-gray-950 text-gray-900 dark:text-gray-100">
    <!-- Top bar -->
    <header class="border-b border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900">
      <div class="max-w-4xl mx-auto px-4 py-3 flex items-center justify-between">
        <div class="flex items-center gap-2">
          <span class="text-xl">🪟</span>
          <span class="font-semibold">Client Portal</span>
        </div>
        <button
          v-if="store.isClientSignedIn"
          class="text-sm text-gray-500 hover:text-gray-800 dark:hover:text-gray-200"
          @click="store.signOut()"
        >Sign out</button>
      </div>
    </header>

    <main class="max-w-4xl mx-auto px-4 py-10">
      <!-- ============ SIGN-IN ============ -->
      <div v-if="!store.isClientSignedIn" class="max-w-sm mx-auto">
        <h1 class="text-xl font-semibold mb-1">Sign in to your agents</h1>
        <p class="text-sm text-gray-500 dark:text-gray-400 mb-6">
          Enter the email an operator shared agents with. We'll send you a 6-digit code.
        </p>

        <!-- Step 1: email -->
        <form v-if="step === 'email'" @submit.prevent="onRequest" class="space-y-3">
          <input
            v-model="email"
            type="email"
            required
            placeholder="you@example.com"
            class="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm focus:ring-action-primary-500 focus:border-action-primary-500"
          />
          <button
            type="submit"
            :disabled="busy || !email"
            class="w-full rounded-lg bg-action-primary-600 hover:bg-action-primary-700 text-white text-sm px-4 py-2 disabled:opacity-50"
          >{{ busy ? 'Sending…' : 'Send code' }}</button>
        </form>

        <!-- Step 2: code -->
        <form v-else @submit.prevent="onVerify" class="space-y-3">
          <p class="text-sm text-gray-500 dark:text-gray-400">
            If <span class="font-medium text-gray-700 dark:text-gray-300">{{ email }}</span> has access,
            a code is on its way. Enter it below.
          </p>
          <input
            v-model="code"
            inputmode="numeric"
            maxlength="6"
            placeholder="6-digit code"
            class="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm tracking-widest text-center focus:ring-action-primary-500 focus:border-action-primary-500"
          />
          <button
            type="submit"
            :disabled="busy || code.length < 6"
            class="w-full rounded-lg bg-action-primary-600 hover:bg-action-primary-700 text-white text-sm px-4 py-2 disabled:opacity-50"
          >{{ busy ? 'Verifying…' : 'Verify & continue' }}</button>
          <button type="button" class="w-full text-xs text-gray-400 hover:text-gray-600" @click="step = 'email'; code = ''">
            ← use a different email
          </button>
        </form>

        <p v-if="error" class="mt-3 text-sm text-status-danger-600 dark:text-status-danger-400">{{ error }}</p>
      </div>

      <!-- ============ ROSTER ============ -->
      <div v-else>
        <div class="mb-4">
          <h1 class="text-xl font-semibold">Your Agents</h1>
          <p class="text-sm text-gray-500 dark:text-gray-400">
            Shared with <span class="font-medium text-gray-700 dark:text-gray-300">{{ store.clientEmail }}</span>.
          </p>
        </div>

        <div v-if="store.loading" class="text-center py-16">
          <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-action-primary-500 mx-auto"></div>
        </div>

        <div v-else-if="store.agents.length === 0" class="text-center py-16 border border-dashed border-gray-300 dark:border-gray-700 rounded-lg">
          <div class="text-4xl mb-3">🗂️</div>
          <p class="text-sm text-gray-600 dark:text-gray-300 font-medium">No agents shared with you yet</p>
        </div>

        <div v-else class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          <div
            v-for="a in store.agents"
            :key="a.name"
            class="rounded-xl border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 p-4"
          >
            <div class="flex items-center gap-3">
              <div
                class="h-12 w-12 rounded-full flex items-center justify-center text-white font-semibold"
                :style="{ background: tint(a.name) }"
              >{{ initials(a.name) }}</div>
              <div class="min-w-0">
                <div class="font-medium truncate">{{ a.name }}</div>
                <div class="text-xs text-gray-500 dark:text-gray-400 truncate"><span v-if="a.owner">by {{ a.owner }}</span></div>
              </div>
            </div>
            <div class="mt-3 flex items-center justify-between">
              <span class="text-[11px] text-gray-400"><span v-if="a.shared_at">shared {{ formatDate(a.shared_at) }}</span></span>
              <button
                class="text-xs px-2.5 py-1 rounded-md bg-gray-100 dark:bg-gray-800 text-gray-400 dark:text-gray-500 cursor-not-allowed"
                disabled
                title="Chat over a portal session arrives in the next slice"
              >Chat — soon</button>
            </div>
          </div>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'

const store = useClientPortalStore()
const step = ref('email')
const email = ref('')
const code = ref('')
const busy = ref(false)
const error = ref(null)

async function onRequest() {
  busy.value = true
  error.value = null
  try {
    await store.requestCode(email.value.trim().toLowerCase())
    step.value = 'code'
  } catch (err) {
    error.value = err.response?.data?.detail || 'Could not send a code. Try again.'
  } finally {
    busy.value = false
  }
}

async function onVerify() {
  busy.value = true
  error.value = null
  try {
    await store.verifyCode(email.value.trim().toLowerCase(), code.value.trim())
    await store.fetchRoster()
  } catch (err) {
    error.value = err.response?.status === 401
      ? 'Invalid or expired code.'
      : (err.response?.data?.detail || 'Verification failed.')
  } finally {
    busy.value = false
  }
}

function initials(name) {
  const p = (name || '?').replace(/[^A-Za-z0-9]+/g, ' ').trim().split(' ')
  return ((p[0]?.[0] || '') + (p[1]?.[0] || p[0]?.[1] || '')).toUpperCase() || '?'
}
function tint(name) {
  let h = 0
  for (let i = 0; i < (name || '').length; i++) h = (h * 31 + name.charCodeAt(i)) % 360
  return `hsl(${h}, 45%, 45%)`
}
function formatDate(iso) {
  try { return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) }
  catch { return iso }
}

onMounted(() => { if (store.isClientSignedIn) store.fetchRoster() })
</script>
