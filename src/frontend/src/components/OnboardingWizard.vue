<!--
  OnboardingWizard.vue (trinity-enterprise#52)

  Minimalistic guided first-run flow. It does NOT reimplement agent creation —
  it *drives the real UI*: pick what you want → the actual CreateAgentModal opens
  (template prefilled) → once the agent exists, the wizard leads you to the one
  thing it still needs to think: a Claude subscription credential.

  Teach by doing, not by reading. No mandatory tour.

  Flow:
    intro      — one question, "what do you want it to do?" (prefills a template)
    create     — the REAL CreateAgentModal (the right buttons, not a clone)
    credential — "connect a Claude subscription" → Settings → Integrations
-->
<template>
  <!-- Teleport to <body> so the overlay escapes the Dashboard's nested layout
       stacking contexts — otherwise z-index alone can't keep header chrome
       (e.g. the time-range control) from poking through the dim backdrop. -->
  <Teleport to="body">
  <!-- create step: hand off to the real creation modal -->
  <CreateAgentModal
    v-if="step === 'create'"
    :initial-template="selectedTemplate"
    @created="onCreated"
    @close="onModalClose"
  />

  <!-- intro + credential: the wizard's own guidance card -->
  <div
    v-else
    class="fixed inset-0 z-50 overflow-y-auto"
    role="dialog"
    aria-modal="true"
    aria-labelledby="onboarding-title"
  >
    <div class="flex min-h-screen items-center justify-center p-4">
      <div class="fixed inset-0 bg-gray-900/85 backdrop-blur-sm" @click="dismiss"></div>

      <div ref="dialogCard" class="relative w-full max-w-2xl rounded-2xl bg-white dark:bg-gray-800 shadow-2xl ring-1 ring-black/5 dark:ring-white/10 overflow-hidden">
        <!-- Header -->
        <div class="px-6 pt-6 pb-4 sm:px-8">
          <div class="flex items-start justify-between">
            <div>
              <p class="text-xs font-semibold uppercase tracking-wide text-action-primary-600 dark:text-action-primary-400">
                {{ step === 'credential' ? 'Almost there' : 'Welcome to Trinity' }}
              </p>
              <h2 id="onboarding-title" class="mt-1 text-xl font-semibold text-gray-900 dark:text-white">
                {{ step === 'credential' ? 'Give your agent a brain' : 'Launch your first agent' }}
              </h2>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                {{ step === 'credential'
                  ? `${createdName} is created. Connect a Claude subscription so it can think and act.`
                  : "Pick what you want it to do — we'll open the create form with the right template ready." }}
              </p>
            </div>
            <button
              @click="dismiss"
              class="ml-4 rounded-lg p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 focus:outline-none focus:ring-2 focus:ring-action-primary-500"
              aria-label="Close onboarding"
            >
              <svg class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>

        <div class="px-6 pb-6 sm:px-8 sm:pb-8">
          <!-- ===== Credential step ===== -->
          <div v-if="step === 'credential'">
            <!-- Already connected -->
            <div
              v-if="claudeAuthConfigured"
              class="flex items-center gap-3 rounded-xl border border-status-success-200 dark:border-status-success-800 bg-status-success-50 dark:bg-status-success-900/30 px-4 py-3"
            >
              <svg class="h-6 w-6 flex-shrink-0 text-status-success-500" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
              </svg>
              <div>
                <p class="text-sm font-medium text-gray-900 dark:text-white">Claude is already connected</p>
                <p class="text-xs text-gray-500 dark:text-gray-400">{{ createdName }} can think right away. You can manage subscriptions in Settings.</p>
              </div>
            </div>

            <!-- Needs a subscription -->
            <div v-else class="rounded-xl border border-gray-200 dark:border-gray-700 p-4">
              <ol class="space-y-2 text-sm text-gray-600 dark:text-gray-300">
                <li class="flex gap-2"><span class="font-semibold text-action-primary-600 dark:text-action-primary-400">1.</span> Run <code class="rounded bg-gray-100 dark:bg-gray-700 px-1 py-0.5 text-xs">claude setup-token</code> on your machine to get a subscription token.</li>
                <li class="flex gap-2"><span class="font-semibold text-action-primary-600 dark:text-action-primary-400">2.</span> Open <span class="font-medium">Settings → Integrations → Claude Subscriptions</span> and register it.</li>
                <li class="flex gap-2"><span class="font-semibold text-action-primary-600 dark:text-action-primary-400">3.</span> Assign it to <span class="font-medium">{{ createdName }}</span> — then chat.</li>
              </ol>
              <p class="mt-3 text-xs text-gray-400 dark:text-gray-500">No subscription? You can instead set a platform Anthropic API key in the same place.</p>
            </div>

            <div class="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
              <button
                @click="openChat"
                class="rounded-lg px-4 py-2 text-sm font-medium text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700"
              >
                Open chat with {{ createdName }}
              </button>
              <button
                v-if="!claudeAuthConfigured"
                @click="goToCredentials"
                class="rounded-lg bg-action-primary-600 px-4 py-2 text-sm font-medium text-white hover:bg-action-primary-700 focus:outline-none focus:ring-2 focus:ring-offset-2 dark:focus:ring-offset-gray-800 focus:ring-action-primary-500"
              >
                Connect Claude subscription →
              </button>
              <button
                v-else
                @click="goToCredentials"
                class="rounded-lg px-4 py-2 text-sm font-medium text-action-primary-600 dark:text-action-primary-400 hover:bg-action-primary-50 dark:hover:bg-action-primary-900/30"
              >
                Manage subscriptions
              </button>
            </div>
          </div>

          <!-- ===== Intro / purpose picker ===== -->
          <div v-else>
            <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <button
                v-for="p in purposes"
                :key="p.key"
                type="button"
                @click="select(p)"
                class="flex items-start gap-3 rounded-xl border border-gray-200 dark:border-gray-700 p-4 text-left transition-all hover:border-action-primary-400 dark:hover:border-action-primary-500 hover:bg-action-primary-50/50 dark:hover:bg-action-primary-900/20"
              >
                <span class="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg bg-action-primary-100 dark:bg-action-primary-900/50">
                  <span class="text-lg leading-none">{{ p.icon }}</span>
                </span>
                <span class="min-w-0">
                  <span class="block text-sm font-medium text-gray-900 dark:text-white">{{ p.title }}</span>
                  <span class="mt-0.5 block text-xs text-gray-500 dark:text-gray-400">{{ p.desc }}</span>
                </span>
              </button>
            </div>

            <div class="mt-5 text-center">
              <button @click="dismiss" class="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
                Skip for now — I'll explore on my own
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
  </Teleport>
</template>

<script setup>
import { ref, onMounted, onUnmounted, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import CreateAgentModal from './CreateAgentModal.vue'

defineProps({
  // Whether platform Claude auth is configured (from feature-flags). Decides
  // whether the credential step nudges to connect, or just confirms + offers chat.
  claudeAuthConfigured: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'deployed'])

const router = useRouter()

// Intent → starter template. Each maps to a real local template shipped in
// config/agent-templates. CreateAgentModal falls back to a blank agent if a
// mapped template is missing in this deploy, so no existence pre-check here.
const purposes = ref([
  { key: 'research', title: 'Research a market or topic', desc: 'Scans trends and competitors, summarizes findings.', icon: '🔎', template: 'local:scout' },
  { key: 'strategy', title: 'Advise on strategy', desc: 'Turns inputs into clear, actionable recommendations.', icon: '🧭', template: 'local:sage' },
  { key: 'writing', title: 'Write content & reports', desc: 'Drafts reports, proposals, and client deliverables.', icon: '✍️', template: 'local:scribe' },
  { key: 'blank', title: 'Start from scratch', desc: 'A blank Claude Code agent you shape yourself.', icon: '✨', template: '' },
])

const step = ref('intro')          // intro | create | credential
const selectedTemplate = ref('')
const createdName = ref('')

// --- Modal a11y: Escape to close, focus trap, focus-on-open, scroll lock ---
const dialogCard = ref(null)

function focusable() {
  if (!dialogCard.value) return []
  return Array.from(
    dialogCard.value.querySelectorAll(
      'a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex="-1"])'
    )
  )
}

function onKeydown(e) {
  // While the real CreateAgentModal is open it owns the keyboard.
  if (step.value === 'create') return
  if (e.key === 'Escape') {
    e.preventDefault()
    dismiss()
    return
  }
  if (e.key === 'Tab') {
    const els = focusable()
    if (!els.length) return
    const first = els[0]
    const last = els[els.length - 1]
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault()
      first.focus()
    }
  }
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown)
  // Scroll-lock the page behind the modal; restored on unmount.
  document.body.style.overflow = 'hidden'
  nextTick(() => focusable()[0]?.focus())
})

onUnmounted(() => {
  document.removeEventListener('keydown', onKeydown)
  document.body.style.overflow = ''
})

function select(p) {
  selectedTemplate.value = p.template
  step.value = 'create'           // hand off to the real create form
}

function onModalClose() {
  // CreateAgentModal emits 'created' THEN 'close'. Only treat a close as a
  // cancel when we're still on the create step — once 'created' has advanced
  // us to 'credential', this close must not bounce us back to the picker.
  if (step.value === 'create') step.value = 'intro'
}

function onCreated(agent) {
  createdName.value = agent?.name || ''
  emit('deployed', createdName.value)
  step.value = 'credential'        // lead to the credential they must provide
}

function dismiss() {
  emit('close')
}

function goToCredentials() {
  emit('close')
  router.push({ path: '/settings', query: { tab: 'integrations' } })
}

function openChat() {
  emit('close')
  if (createdName.value) {
    router.push({ path: `/agents/${createdName.value}`, query: { tab: 'chat' } })
  }
}
</script>
