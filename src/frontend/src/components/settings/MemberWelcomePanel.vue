<!--
  A new member's first message (trinity-enterprise#1170).

  The words a member sees from their agent in their main conversation the
  moment it is handed over — before the agent has said anything itself.
  Shipped with a default; an admin replaces it here. `{first_name}` and
  `{agent_name}` are filled in for each member. Empty turns the message off;
  "Restore the default" brings the shipped text back. The switch decides
  whether this message replaces the agent's own first greeting.

  trinity-enterprise#1198: the agent template may declare the message. The
  badge names where it comes from (`source`); the template's own message is
  shown as a member sees it, a refused or unreadable one says why, and a text
  set here says it overrides the template, with a way back. When the template
  supplies the message, it also sets the switch.

  Rendered only where the member-agents entitlement is on.
-->
<template>
  <div v-if="entitled" class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg" data-testid="member-welcome-panel">
    <div class="px-6 py-5">
      <div class="flex items-start justify-between gap-3">
        <div>
          <h3 class="text-lg font-medium text-gray-900 dark:text-gray-100">Welcome message for new members</h3>
          <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">
            Shown in a new member's conversation with their agent the moment it is handed to them.
          </p>
        </div>
        <BaseBadge v-if="welcome" :variant="sourceBadge.variant" dot data-testid="member-welcome-state">
          {{ sourceBadge.label }}
        </BaseBadge>
      </div>

      <!-- #1927: gated on "no data yet", never on the in-flight flag. -->
      <div v-if="view.state === 'loading'" class="mt-4" aria-busy="true" data-testid="member-welcome-loading">
        <span class="sr-only">Loading the welcome message…</span>
        <div class="h-28 rounded-md bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" aria-hidden="true"></div>
      </div>

      <LoadFailed
        v-else-if="view.state === 'failed'"
        dense
        class="mt-4"
        title="Couldn't load the welcome message"
        message="The member settings could not be read. Try again."
        :detail="errorDetail(store.error)"
        :retrying="store.loading"
        @retry="store.load()"
      />

      <div v-else-if="welcome" class="mt-5 space-y-4">
        <div v-if="templateState === 'ok'" class="rounded-md border border-gray-200 dark:border-gray-750 bg-gray-50 dark:bg-gray-900 px-4 py-3" data-testid="member-welcome-template">
          <p class="text-[11px] font-mono uppercase tracking-wide text-gray-600 dark:text-gray-300">The agent template's welcome</p>
          <p class="mt-1 text-sm text-gray-900 dark:text-gray-100 whitespace-pre-wrap">{{ templatePreview }}</p>
          <p class="mt-1 text-xs text-gray-600 dark:text-gray-300">
            {{ welcome.template.replaces_hello ? "Replaces the agent's own first greeting." : 'The agent still introduces itself after it.' }}
          </p>
          <ul v-if="templateWarnings.length" class="mt-2 space-y-0.5 text-xs text-status-warning-700 dark:text-status-warning-400" data-testid="member-welcome-template-warnings">
            <li v-for="(w, i) in templateWarnings" :key="i">{{ w }}</li>
          </ul>
        </div>
        <div v-if="overridesTemplate" class="flex flex-wrap items-center gap-3" data-testid="member-welcome-override">
          <p class="text-sm text-gray-600 dark:text-gray-300">This setting overrides the agent template's welcome.</p>
          <BaseButton
            variant="secondary"
            size="sm"
            :disabled="saving"
            data-testid="member-welcome-use-template"
            @click="save(null)"
          >Use the template's welcome</BaseButton>
        </div>
        <div
          v-if="templateState === 'refused' || templateState === 'unreadable'"
          class="rounded-md border border-status-warning-200 dark:border-status-warning-500/30 bg-status-warning-50 dark:bg-status-warning-500/10 px-4 py-3 text-sm"
          role="status"
          data-testid="member-welcome-template-problem"
        >
          <p class="text-status-warning-700 dark:text-status-warning-400">
            {{ templateState === 'refused' ? "The agent template's welcome was not used." : "The agent template's welcome could not be read." }}
            {{ fallbackSentence }}
          </p>
          <p v-if="welcome.template.message" class="mt-1 text-gray-900 dark:text-gray-100">{{ welcome.template.message }}</p>
          <p v-if="welcome.template.code" class="mt-1 text-xs font-mono text-gray-600 dark:text-gray-300">{{ welcome.template.code }}</p>
        </div>
        <p v-if="templateState === 'absent'" class="text-xs text-gray-600 dark:text-gray-300" data-testid="member-welcome-template-absent">
          The agent template declares no welcome message, so the message below is used.
        </p>
        <BaseTextarea
          v-model="text"
          label="Message"
          :rows="5"
          :disabled="saving"
          :error="textError"
          help="Use {first_name} and {agent_name}; they are filled in for each member. Leave empty to show no message."
          data-testid="member-welcome-text"
        />
        <BaseToggle
          v-model="replacesHello"
          label="Replace the agent's own first greeting"
          aria-label="Replace the agent's own first greeting"
          :disabled="saving || toggleLocked"
          data-testid="member-welcome-replaces"
        />
        <p class="text-xs text-gray-600 dark:text-gray-300 -mt-2">
          {{ toggleLocked
            ? 'Set by the agent template. Edit the message to set it here instead.'
            : 'Off: the agent still introduces itself after this message, without repeating it.' }}
        </p>
        <div v-if="preview" class="rounded-md border border-gray-200 dark:border-gray-750 bg-gray-50 dark:bg-gray-900 px-4 py-3" data-testid="member-welcome-preview">
          <p class="text-[11px] font-mono uppercase tracking-wide text-gray-600 dark:text-gray-300">Preview</p>
          <p class="mt-1 text-sm text-gray-900 dark:text-gray-100 whitespace-pre-wrap">{{ preview }}</p>
        </div>
        <InlineError :message="saveError" :detail="saveErrorDetail" @dismiss="saveError = ''" />
        <div class="flex flex-wrap items-center gap-3">
          <BaseButton
            :disabled="!dirty || saving || !!textError"
            :loading="saving"
            loading-label="Saving…"
            data-testid="member-welcome-save"
            @click="save(text)"
          >Save</BaseButton>
          <BaseButton
            v-if="templateState !== 'ok'"
            variant="secondary"
            :disabled="saving || welcome.is_default"
            data-testid="member-welcome-restore"
            @click="save(null)"
          >Restore the default</BaseButton>
          <span v-if="savedNotice" class="text-sm text-status-success-700 dark:text-status-success-400" role="status" data-testid="member-welcome-saved">
            {{ savedNotice }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseTextarea from '../base/BaseTextarea.vue'
import BaseToggle from '../base/BaseToggle.vue'
import InlineError from '../InlineError.vue'
import LoadFailed from '../LoadFailed.vue'
import { useEnterpriseStore } from '../../stores/enterprise'
import { useMemberWelcomeStore } from '../../stores/memberWelcome'
import { viewState } from '../../utils/loadingState'

const FEATURE_ID = 'member_agents'
const SAMPLE = { first_name: 'Alex', agent_name: 'your assistant' }

const enterprise = useEnterpriseStore()
const store = useMemberWelcomeStore()

const entitled = computed(() => enterprise.isEntitled(FEATURE_ID))
const view = computed(() => viewState({ hasLoaded: store.hasLoaded, error: store.error }))
const welcome = computed(() => store.welcome)

const text = ref('')
const replacesHello = ref(false)
const saving = ref(false)
const saveError = ref('')
const saveErrorDetail = ref('')
const savedNotice = ref('')

const maxLength = computed(() => welcome.value?.max_length || 4000)
const textError = computed(() => (text.value.trim().length > maxLength.value
  ? `The message is ${text.value.trim().length} characters; the most is ${maxLength.value}. Shorten it.`
  : ''))
const dirty = computed(() => !!welcome.value
  && (text.value !== welcome.value.text || replacesHello.value !== welcome.value.replaces_hello))

const SOURCE_BADGES = {
  template: { label: 'From the agent template', variant: 'info' },
  setting: { label: 'Set here', variant: 'info' },
  default: { label: 'Shipped default', variant: 'neutral' },
  off: { label: 'Off', variant: 'neutral' },
}
/** Older servers send no `source`; derive it the way they resolved it. */
const source = computed(() => welcome.value?.source
  || (welcome.value?.is_default ? 'default' : (welcome.value?.text ? 'setting' : 'off')))
const sourceBadge = computed(() => SOURCE_BADGES[source.value] || SOURCE_BADGES.default)
const templateState = computed(() => welcome.value?.template?.status || null)
const templateWarnings = computed(() => welcome.value?.template?.warnings || [])
/** The server's own render when the template is the source; else the panel's sample fill. */
const templatePreview = computed(() => (source.value === 'template' && welcome.value?.preview)
  || fill(welcome.value?.template?.text || ''))
const overridesTemplate = computed(() => templateState.value === 'ok'
  && (source.value === 'setting' || source.value === 'off'))
/** The template's switch is shown, not edited, until the message itself is edited here. */
const toggleLocked = computed(() => source.value === 'template' && text.value === welcome.value?.text)
const fallbackSentence = computed(() => (source.value === 'default'
  ? 'The shipped default is used instead.'
  : (source.value === 'off' ? 'No message is shown.' : 'The message set here is used.')))
/** The same two literal replacements the server makes — nothing else is filled in. */
function fill(value) {
  return value.trim()
    .split('{first_name}').join(SAMPLE.first_name)
    .split('{agent_name}').join(SAMPLE.agent_name)
}
const preview = computed(() => fill(text.value))

function reset() {
  text.value = welcome.value?.text ?? ''
  replacesHello.value = !!welcome.value?.replaces_hello
}

watch(welcome, reset, { immediate: true })

function errorDetail(e) {
  if (!e) return ''
  const d = e.response?.data?.detail
  return String((d && (d.message || d.code)) || d || e.message || '')
}

async function save(value) {
  if (saving.value) return
  saving.value = true
  saveError.value = ''
  saveErrorDetail.value = ''
  savedNotice.value = ''
  try {
    await store.save(value, replacesHello.value)
    savedNotice.value = value === null
      ? (source.value === 'template' ? "The agent template's welcome is back." : 'Default restored.')
      : 'Saved. New members see this message.'
  } catch (e) {
    saveError.value = 'The welcome message was not saved. Try again.'
    saveErrorDetail.value = errorDetail(e)
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  if (entitled.value) store.load()
})
watch(entitled, (on) => { if (on && !store.hasLoaded) store.load() })
</script>
