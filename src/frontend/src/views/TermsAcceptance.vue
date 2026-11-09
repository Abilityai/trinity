<!--
  trinity-enterprise#1054, #1107 — the terms screen a person accepts once.

  Every person whose current terms version is not accepted — an admin on their
  first visit included — gets 403 `terms_required` from every route until
  they accept here; the two terms routes this page calls are exempt from that
  refusal. The text is the instance's own (Settings › Access) or the shipped
  default. So the page:
    - shows the text and its version, and one accept action that sends the
      version shown (a newer one is a 409 `version_stale`: reload, re-tick);
    - always offers a way out — declining signs out, on every branch;
    - never navigates by itself unless an accept succeeded. An "already
      accepted" or "nothing published" answer is a Continue the person clicks,
      so a gate that keeps refusing cannot turn into a redirect loop.
-->
<template>
  <div class="min-h-screen bg-gray-50 dark:bg-gray-900 flex items-start justify-center px-4 py-12">
    <BaseCard flush class="w-full max-w-2xl">
      <div class="px-6 py-5">
        <h1 class="text-2xl font-bold text-gray-900 dark:text-gray-100" data-testid="terms-title">{{ terms.title || 'Terms of use' }}</h1>

        <!-- The invitation that opened this account is no longer live. -->
        <div v-if="closedMessage" class="mt-4" data-testid="terms-closed">
          <p class="text-sm text-gray-900 dark:text-gray-100">{{ closedMessage }}</p>
          <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">
            Ask the person who invited you to send a new invitation, then sign in again.
          </p>
        </div>

        <LoadFailed
          v-else-if="view.state === 'failed'"
          dense
          title="Couldn't load the terms"
          message="The terms could not be read, so they cannot be accepted yet. Try again, or sign out."
          :detail="errorDetail(store.error)"
          :retrying="store.loading"
          @retry="store.load()"
        />

        <div v-else-if="view.state === 'loading'" class="mt-4" aria-busy="true" data-testid="terms-loading">
          <span class="sr-only">Loading the terms…</span>
          <div class="h-4 w-32 rounded bg-gray-200 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" aria-hidden="true"></div>
          <div class="mt-4 h-64 rounded-md bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" aria-hidden="true"></div>
        </div>

        <div v-else-if="alreadyClear" class="mt-4" data-testid="terms-clear">
          <p class="text-sm text-gray-600 dark:text-gray-300">
            {{ terms.accepted && terms.version
              ? `You have accepted version ${terms.version}.`
              : 'There are no terms to accept right now.' }}
          </p>
          <div class="mt-4">
            <BaseButton data-testid="terms-continue" @click="leave">Continue</BaseButton>
          </div>
        </div>

        <template v-else>
          <!-- ent#1172 AC 4: version label is a platform detail; share-tier
               members (role=user) see the terms text without it. The version
               still records in the acceptance row on submit. -->
          <p v-if="!isUserRole" class="mt-1 text-sm text-gray-600 dark:text-gray-300 tabular-nums" data-testid="terms-version">
            Version {{ terms.version }}
          </p>
          <p
            v-if="staleNotice"
            class="mt-3 text-sm text-status-warning-700 dark:text-status-warning-400"
            role="status"
            data-testid="terms-stale"
          >
            The terms changed while you were reading. This is version {{ terms.version }} — please read it before accepting.
          </p>
          <div
            class="mt-4 max-h-[50vh] overflow-y-auto rounded-md border border-gray-200 dark:border-gray-750 p-4 prose prose-sm dark:prose-invert max-w-none"
            data-testid="terms-text"
            v-html="renderedText"
          ></div>

          <label class="mt-4 flex items-start gap-2 text-sm text-gray-900 dark:text-gray-100">
            <input
              v-model="agreed"
              type="checkbox"
              :disabled="accepting"
              class="mt-0.5 h-4 w-4 text-action-primary-600 focus:ring-action-primary-500 border-gray-300 dark:border-gray-600 rounded"
              data-testid="terms-agree"
            />
            <span>I have read these terms and accept them.</span>
          </label>

          <div class="mt-4">
            <InlineError :message="acceptError" :detail="acceptErrorDetail" @dismiss="acceptError = ''" />
          </div>

          <div class="mt-4">
            <BaseButton
              data-testid="terms-accept"
              :disabled="!agreed"
              :loading="accepting"
              loading-label="Accepting…"
              @click="onAccept"
            >Accept and continue</BaseButton>
          </div>
        </template>

        <!-- The plain refusal path, on every branch: leave without accepting. -->
        <div class="mt-6 border-t border-gray-200 dark:border-gray-750 pt-4 flex items-center justify-between gap-4">
          <p class="text-xs text-gray-600 dark:text-gray-300">
            {{ closedMessage ? '' : 'If you do not accept, you are signed out and nothing else changes. You can come back any time: sign in again and accept the terms.' }}
          </p>
          <BaseButton variant="ghost" size="sm" data-testid="terms-decline" @click="signOut">
            {{ closedMessage || view.state !== 'ready' || alreadyClear ? 'Sign out' : 'Decline and sign out' }}
          </BaseButton>
        </div>
      </div>
    </BaseCard>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import BaseCard from '../components/base/BaseCard.vue'
import BaseButton from '../components/base/BaseButton.vue'
import InlineError from '../components/InlineError.vue'
import LoadFailed from '../components/LoadFailed.vue'
import { useAuthStore } from '../stores/auth'
import { useTermsStore } from '../stores/terms'
import { viewState } from '../utils/loadingState'
import { renderMarkdown } from '../utils/markdown'
import { afterTermsLocation, closedInviteMessage } from '../utils/termsGate'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const store = useTermsStore()

// ent#1172 AC 4: suppress the version label for share-tier (role=user) viewers.
const isUserRole = computed(() => authStore.user?.role === 'user')

const agreed = ref(false)
const accepting = ref(false)
const acceptError = ref('')
const acceptErrorDetail = ref('')
const staleNotice = ref(false)
// The refusal that sent the person here (`?reason=`), or one the accept met.
const closedCode = ref(typeof route.query.reason === 'string' ? route.query.reason : '')

const closedMessage = computed(() => closedInviteMessage(closedCode.value))
const view = computed(() => viewState({ hasLoaded: store.hasLoaded, error: store.error }))
const terms = computed(() => store.current || {})
// Nothing to accept: already accepted, or no version published.
const alreadyClear = computed(() => view.value.state === 'ready' && (terms.value.accepted || !terms.value.version))
const renderedText = computed(() => renderMarkdown(terms.value.text || ''))

function errorDetail(e) {
  if (!e) return ''
  const d = e.response?.data?.detail
  return String((d && (d.message || d.code)) || d || e.message || '')
}

function leave() {
  return router.replace(afterTermsLocation(route.query.redirect, authStore.user?.role))
}

async function onAccept() {
  if (!agreed.value || accepting.value) return
  accepting.value = true
  acceptError.value = ''
  acceptErrorDetail.value = ''
  const version = terms.value.version
  try {
    await store.accept(version)
    await leave()
  } catch (e) {
    const detail = e.response?.data?.detail
    const code = detail?.code
    if (code === 'version_stale') {
      agreed.value = false
      staleNotice.value = true
      await store.load()
    } else if (closedInviteMessage(code)) {
      closedCode.value = code
    } else {
      acceptError.value = 'Your acceptance was not recorded. Try again; if it keeps failing, sign out and back in.'
      acceptErrorDetail.value = errorDetail(e)
    }
  } finally {
    accepting.value = false
  }
}

function signOut() {
  authStore.logout()
  return router.replace('/login')
}

onMounted(() => {
  if (!closedMessage.value) store.load()
})
</script>
