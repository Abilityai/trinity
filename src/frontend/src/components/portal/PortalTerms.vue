<!--
  trinity-enterprise#1107 ruling A(1) — the terms screen a Workspace PORTAL
  session accepts, inside the Workspace, on its own token.

  The platform session is sent to /terms (`views/TermsAcceptance.vue`) by the
  HTTP interceptor; a portal session cannot open that page (it holds no
  platform credential), so the shell renders this screen in place of the
  roster while `store.termsRefusal` is set. Same contract as that page:
    - the text and its version, one accept action that sends the version shown
      (a newer one is a 409 `version_stale`: reload, re-tick);
    - always a way out — declining signs out, on every branch;
    - never moves by itself: a successful accept emits `accepted` and the shell
      re-bootstraps; a closed invitation offers only the way out.
-->
<template>
  <div class="flex-1 flex items-start justify-center px-4 py-12 overflow-y-auto" data-testid="portal-terms">
    <BaseCard flush class="w-full max-w-2xl">
      <div class="px-6 py-5">
        <div class="mb-6">
          <PortalBrand mark-size="h-7 w-7" text-size="text-lg" />
        </div>
        <h1 class="text-2xl font-bold text-gray-900 dark:text-gray-100" data-testid="portal-terms-title">{{ terms.title || 'Terms of use' }}</h1>

        <!-- The invitation that opened this access is no longer live. -->
        <div v-if="closedMessage" class="mt-4" data-testid="portal-terms-closed">
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
          :detail="errorDetail(loadError)"
          :retrying="loading"
          @retry="load()"
        />

        <div v-else-if="view.state === 'loading'" class="mt-4" aria-busy="true" data-testid="portal-terms-loading">
          <span class="sr-only">Loading the terms…</span>
          <div class="h-4 w-32 rounded bg-gray-200 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" aria-hidden="true"></div>
          <div class="mt-4 h-64 rounded-md bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" aria-hidden="true"></div>
        </div>

        <template v-else>
          <p class="mt-1 text-sm text-gray-600 dark:text-gray-300 tabular-nums" data-testid="portal-terms-version">
            Version {{ terms.version }}
          </p>
          <p
            v-if="staleNotice"
            class="mt-3 text-sm text-status-warning-700 dark:text-status-warning-400"
            role="status"
            data-testid="portal-terms-stale"
          >
            The terms changed while you were reading. This is version {{ terms.version }} — please read it before accepting.
          </p>
          <div
            class="mt-4 max-h-[50vh] overflow-y-auto rounded-md border border-gray-200 dark:border-gray-750 p-4 prose prose-sm dark:prose-invert max-w-none"
            data-testid="portal-terms-text"
            v-html="renderedText"
          ></div>

          <label class="mt-4 flex items-start gap-2 text-sm text-gray-900 dark:text-gray-100">
            <input
              v-model="agreed"
              type="checkbox"
              :disabled="accepting"
              class="mt-0.5 h-4 w-4 text-action-primary-600 focus:ring-action-primary-500 border-gray-300 dark:border-gray-600 rounded"
              data-testid="portal-terms-agree"
            />
            <span>I have read these terms and accept them.</span>
          </label>

          <div class="mt-4">
            <InlineError :message="acceptError" :detail="acceptErrorDetail" @dismiss="acceptError = ''" />
          </div>

          <div class="mt-4">
            <BaseButton
              data-testid="portal-terms-accept"
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
          <BaseButton variant="ghost" size="sm" data-testid="portal-terms-decline" @click="emit('decline')">
            {{ closedMessage || view.state !== 'ready' ? 'Sign out' : 'Decline and sign out' }}
          </BaseButton>
        </div>
      </div>
    </BaseCard>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import BaseCard from '../base/BaseCard.vue'
import BaseButton from '../base/BaseButton.vue'
import InlineError from '../InlineError.vue'
import LoadFailed from '../LoadFailed.vue'
import PortalBrand from './PortalBrand.vue'
import { useClientPortalStore } from '../../stores/clientPortal'
import { viewState } from '../../utils/loadingState'
import { renderMarkdown } from '../../utils/markdown'
import { closedInviteMessage } from '../../utils/termsGate'

const emit = defineEmits(['accepted', 'decline'])
const store = useClientPortalStore()

const hasLoaded = ref(false)
const loading = ref(false)
const loadError = ref(null)
const agreed = ref(false)
const accepting = ref(false)
const acceptError = ref('')
const acceptErrorDetail = ref('')
const staleNotice = ref(false)
// The refusal that put this screen up, or one the accept met.
const closedCode = ref(closedInviteMessage(store.termsRefusal) ? store.termsRefusal : '')

const closedMessage = computed(() => closedInviteMessage(closedCode.value))
const view = computed(() => viewState({ hasLoaded: hasLoaded.value, error: loadError.value }))
const terms = computed(() => store.terms || {})
const renderedText = computed(() => renderMarkdown(terms.value.text || ''))

function errorDetail(e) {
  if (!e) return ''
  const d = e.response?.data?.detail
  return String((d && (d.message || d.code)) || d || e.message || '')
}

async function load() {
  loading.value = true
  try {
    await store.loadTerms()
    hasLoaded.value = true
    loadError.value = null
  } catch (e) {
    loadError.value = e
  } finally {
    loading.value = false
  }
}

async function onAccept() {
  if (!agreed.value || accepting.value) return
  accepting.value = true
  acceptError.value = ''
  acceptErrorDetail.value = ''
  try {
    await store.acceptTerms(terms.value.version)
    emit('accepted')
  } catch (e) {
    const code = e.response?.data?.detail?.code
    if (code === 'version_stale') {
      agreed.value = false
      staleNotice.value = true
      await load()
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

onMounted(() => {
  if (!closedMessage.value) load()
})
</script>
