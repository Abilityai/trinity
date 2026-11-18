<template>
  <!--
    #4281 slice F (decisions D4, D8): the review an owner reads before taking a
    share holder's schedule over. A take-over is a grant: a prompt someone else
    wrote gets the owner's access on every later run. So the dialog shows what
    the runs will do (full prompt, tools, model, validation), where they deliver
    and whether a webhook URL is live, and the request echoes exactly this
    version (`utils/scheduleRunTier.js::takeOverBody`). The panel owns the
    request and passes a refusal back as `error`.
  -->
  <BaseModal
    :model-value="modelValue"
    panel-class="relative flex max-h-[85vh] w-full max-w-xl flex-col overflow-hidden rounded-lg bg-white shadow-xl dark:bg-gray-800"
    labelledby="take-over-title"
    :close-on-backdrop="!busy"
    @close="cancel"
  >
    <div v-if="schedule" class="flex min-h-0 flex-col" data-testid="take-over-dialog">
      <div class="border-b border-gray-200 px-6 pb-4 pt-5 dark:border-gray-750">
        <h3 id="take-over-title" class="text-[18px] font-[650] text-gray-900 dark:text-gray-100">
          Take over “{{ schedule.name }}”
        </h3>
        <p class="mt-2 text-[14px] text-gray-600 dark:text-gray-300">
          {{ introText }} After you take it over, every run has your access. Review what it runs first.
        </p>
      </div>

      <div class="min-h-0 flex-1 space-y-4 overflow-y-auto px-6 py-4">
        <section>
          <h4 class="mb-1 text-[11px] font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Prompt</h4>
          <pre
            class="max-h-48 overflow-y-auto whitespace-pre-wrap break-words rounded-md border border-gray-200 bg-gray-50 p-3 font-mono text-[12.5px] text-gray-900 dark:border-gray-750 dark:bg-gray-900 dark:text-gray-100"
            data-testid="take-over-prompt"
          >{{ schedule.message }}</pre>
        </section>

        <dl class="grid grid-cols-1 gap-3 text-[14px] sm:grid-cols-2">
          <div data-testid="take-over-tools">
            <dt class="text-[12.5px] text-gray-500 dark:text-gray-400">Allowed tools</dt>
            <dd class="text-gray-900 dark:text-gray-100">{{ toolsText }}</dd>
          </div>
          <div data-testid="take-over-model">
            <dt class="text-[12.5px] text-gray-500 dark:text-gray-400">Model</dt>
            <dd class="font-mono text-gray-900 dark:text-gray-100">{{ schedule.model || 'platform default' }}</dd>
          </div>
        </dl>

        <section data-testid="take-over-validation">
          <h4 class="mb-1 text-[11px] font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Validation</h4>
          <p v-if="!schedule.validation_enabled" class="text-[14px] text-gray-900 dark:text-gray-100">Off</p>
          <template v-else>
            <p class="text-[14px] text-gray-900 dark:text-gray-100">On, with this prompt:</p>
            <pre class="mt-1 max-h-32 overflow-y-auto whitespace-pre-wrap break-words rounded-md border border-gray-200 bg-gray-50 p-3 font-mono text-[12.5px] text-gray-900 dark:border-gray-750 dark:bg-gray-900 dark:text-gray-100">{{ schedule.validation_prompt || '(the default validation prompt)' }}</pre>
          </template>
        </section>

        <section data-testid="take-over-delivery">
          <h4 class="mb-1 text-[11px] font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Workspace delivery</h4>
          <p v-if="deliveryKind === 'none'" class="text-[14px] text-gray-900 dark:text-gray-100">None: results stay on this agent.</p>
          <p v-else-if="deliveryKind === 'own'" class="text-[14px] text-gray-900 dark:text-gray-100">
            Delivers to your Workspace ({{ schedule.deliver_to_workspace_email }}). That stays as it is.
          </p>
          <fieldset v-else class="space-y-2">
            <legend class="text-[14px] text-gray-900 dark:text-gray-100">
              Delivers to {{ schedule.deliver_to_workspace_email }}'s Workspace. After you take it over, those runs have your access there.
            </legend>
            <label class="flex items-start gap-2 text-[14px] text-gray-900 dark:text-gray-100">
              <input v-model="delivery" type="radio" value="clear" class="mt-1" data-testid="take-over-delivery-clear" />
              <span>Stop delivering to {{ schedule.deliver_to_workspace_email }} (recommended)</span>
            </label>
            <label class="flex items-start gap-2 text-[14px] text-gray-900 dark:text-gray-100">
              <input v-model="delivery" type="radio" value="keep" class="mt-1" data-testid="take-over-delivery-keep" />
              <span>Keep delivering to {{ schedule.deliver_to_workspace_email }}</span>
            </label>
          </fieldset>
        </section>

        <section data-testid="take-over-webhook">
          <h4 class="mb-1 text-[11px] font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Webhook</h4>
          <div v-if="webhook.state === 'loading'" class="py-1" aria-busy="true">
            <div class="h-3 w-2/3 rounded bg-gray-100 animate-pulse motion-reduce:animate-none dark:bg-gray-750"></div>
            <span class="sr-only">Reading the webhook state…</span>
          </div>
          <p v-else class="text-[14px] text-gray-900 dark:text-gray-100">{{ webhookText }}</p>
        </section>

        <p v-if="!creatorGone" class="text-[12.5px] text-gray-600 dark:text-gray-300">
          {{ creatorPhrase }} can no longer edit or delete it, or see its scheduled runs.
        </p>
      </div>

      <div class="space-y-3 border-t border-gray-200 bg-gray-50 px-6 py-3 dark:border-gray-750 dark:bg-gray-900">
        <div v-if="error" data-testid="take-over-error">
          <InlineError :message="error" @dismiss="$emit('dismiss-error')" />
        </div>
        <div class="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <BaseButton variant="secondary" :disabled="busy" data-testid="take-over-cancel" @click="cancel">Cancel</BaseButton>
          <BaseButton
            variant="primary"
            :loading="busy"
            loading-label="Taking over…"
            :disabled="schedule.can_take_over !== true"
            data-destructive
            data-testid="take-over-confirm"
            @click="confirm"
          >Take over</BaseButton>
        </div>
      </div>
    </div>
  </BaseModal>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import BaseModal from './base/BaseModal.vue'
import BaseButton from './base/BaseButton.vue'
import InlineError from './InlineError.vue'
import { deliveryDecision, takeOverBody } from '../utils/scheduleRunTier'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  // The schedule read the person is reviewing (a copy the panel holds still).
  schedule: { type: Object, default: null },
  // { state: 'loading' | 'live' | 'none' | 'failed' }
  webhook: { type: Object, default: () => ({ state: 'loading' }) },
  viewerEmail: { type: String, default: null },
  busy: { type: Boolean, default: false },
  error: { type: String, default: '' },
})

const emit = defineEmits(['update:modelValue', 'confirm', 'dismiss-error'])

// D8: another person's delivery defaults to "stop delivering". Reset to that
// for every version shown, so a choice made on one version never carries
// over to the next.
const delivery = ref('clear')
watch(() => [props.schedule?.id, props.schedule?.updated_at, props.modelValue], () => { delivery.value = 'clear' })

const creatorPhrase = computed(() => props.schedule?.created_by || 'Its creator')
// `creator_missing`: no account is left to describe, or to lose anything.
const creatorGone = computed(() => props.schedule?.runs_as_reason?.code === 'creator_missing')
const introText = computed(() => (creatorGone.value
  ? "The account that created this schedule no longer exists, so its runs have a shared person's access: no shell commands and no tools the owner added."
  : `${creatorPhrase.value} created this schedule. Its runs have their access as a person the agent is shared with: no shell commands and no tools the owner added.`))
const deliveryKind = computed(() => deliveryDecision(props.schedule, props.viewerEmail))
const toolsText = computed(() => {
  const tools = props.schedule?.allowed_tools
  if (!Array.isArray(tools)) return 'All tools'
  return tools.length ? tools.join(', ') : 'None'
})
const webhookText = computed(() => {
  switch (props.webhook?.state) {
    case 'live': return 'A webhook URL is live. Taking over revokes it, so whoever holds the URL can no longer start this schedule.'
    case 'none': return 'No webhook URL.'
    default: return "Couldn't read the webhook state. Taking over revokes any webhook URL either way."
  }
})

function cancel () {
  if (props.busy) return
  emit('update:modelValue', false)
}

function confirm () {
  emit('confirm', takeOverBody(props.schedule, { viewerEmail: props.viewerEmail, delivery: delivery.value }))
}
</script>
