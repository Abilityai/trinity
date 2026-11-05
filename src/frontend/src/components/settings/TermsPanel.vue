<!--
  Terms of use per instance (trinity-enterprise#1107).

  The text every first-time person on this Trinity accepts once per version,
  on whichever door they arrive through. Shipped with a default; an admin
  replaces it here (title + Markdown body), and saving stamps a new version
  that asks everyone else again — the admin who saved it has accepted it by
  writing it. One action restores the shipped default. The switch turns the
  gate off instance-wide. Below the editor, the record: who accepted which
  version, when (bounded, total stated).

  Shown in every edition: the default is in force whether or not anyone
  edited it, and an admin who cannot see the text cannot know what their
  people are agreeing to.
-->
<template>
  <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
    <div class="px-6 py-5">
      <div class="flex items-start justify-between gap-3">
        <div>
          <h3 class="text-lg font-medium text-gray-900 dark:text-gray-100">Terms of use</h3>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Shown once to every person on their first visit, on any door. Saving a new text
            asks everyone again at their next visit.
          </p>
        </div>
        <BaseBadge v-if="terms" :variant="terms.is_default ? 'neutral' : 'info'" dot data-testid="terms-panel-state">
          {{ terms.is_default ? 'shipped default' : 'custom text' }}
        </BaseBadge>
      </div>

      <!-- #1927: gated on "no data yet", never on the in-flight flag. -->
      <div v-if="view.state === 'loading'" class="mt-4" aria-busy="true" data-testid="terms-panel-loading">
        <span class="sr-only">Loading the terms…</span>
        <div class="h-9 w-64 rounded-md bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" aria-hidden="true"></div>
        <div class="mt-3 h-40 rounded-md bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" aria-hidden="true"></div>
      </div>

      <LoadFailed
        v-else-if="view.state === 'failed'"
        dense
        class="mt-4"
        title="Couldn't load the terms"
        message="The current terms could not be read. Try again."
        :detail="errorDetail(store.error)"
        :retrying="store.loading"
        @retry="store.load()"
      />

      <div v-else-if="terms" class="mt-5 space-y-4">
        <BaseToggle
          v-model="required"
          label="Ask every person to accept the terms"
          aria-label="Ask every person to accept the terms"
          :disabled="saving"
          data-testid="terms-panel-required"
          @update:modelValue="saveSwitch"
        />
        <p class="text-xs text-gray-500 dark:text-gray-400 -mt-2">
          Off: nobody is asked, on any door. Already recorded acceptances are kept.
        </p>

        <BaseInput
          v-model="title"
          label="Title"
          :disabled="saving"
          :error="titleError"
          data-testid="terms-panel-title"
        />

        <BaseTextarea
          v-model="text"
          label="Text (Markdown)"
          :rows="12"
          mono
          :disabled="saving"
          :error="textError"
          help="What a person reads before accepting. Markdown headings and lists render on the terms screen."
          data-testid="terms-panel-text"
        />

        <InlineError :message="saveError" :detail="saveErrorDetail" @dismiss="saveError = ''" />

        <div class="flex flex-wrap items-center gap-3">
          <BaseButton
            :disabled="!dirty || saving || !!clientError"
            :loading="saving"
            loading-label="Saving…"
            data-testid="terms-panel-save"
            @click="save"
          >Save as a new version</BaseButton>
          <BaseButton
            variant="secondary"
            :disabled="saving || terms.is_default"
            data-testid="terms-panel-restore"
            @click="confirmRestore = true"
          >Restore the default</BaseButton>
          <button
            v-if="dirty"
            type="button"
            class="text-sm text-gray-600 dark:text-gray-400 underline"
            data-testid="terms-panel-discard"
            @click="discard"
          >Discard</button>
          <span v-if="savedNotice" class="text-sm text-status-success-700 dark:text-status-success-400" role="status" data-testid="terms-panel-saved">
            {{ savedNotice }}
          </span>
        </div>
        <p class="text-xs text-gray-500 dark:text-gray-400 tabular-nums" data-testid="terms-panel-version">
          Current version: {{ terms.version }}
        </p>

        <!-- The record -->
        <div class="pt-3 border-t border-gray-200 dark:border-gray-750">
          <div class="flex items-center justify-between gap-3">
            <h4 class="text-sm font-medium text-gray-900 dark:text-gray-100">Acceptances</h4>
            <span class="text-xs text-gray-600 dark:text-gray-300 tabular-nums" data-testid="terms-panel-acceptances-total">
              {{ acceptancesTotal }}
            </span>
          </div>
          <InlineError
            v-if="store.acceptancesError"
            class="mt-2"
            message="The acceptance list could not be loaded."
            :detail="errorDetail(store.acceptancesError)"
            @dismiss="store.acceptancesError = null"
          />
          <p v-else-if="store.acceptancesLoaded && !store.acceptances.length" class="mt-2 text-sm text-gray-600 dark:text-gray-300" data-testid="terms-panel-acceptances-empty">
            Nobody has accepted yet. The next person to sign in is asked.
          </p>
          <div v-else-if="store.acceptances.length" class="mt-2 max-h-64 overflow-y-auto rounded-md border border-gray-200 dark:border-gray-750">
            <table class="min-w-full text-sm">
              <thead class="sticky top-0 bg-gray-100 dark:bg-gray-750 text-[11px] font-mono uppercase tracking-wide text-gray-600 dark:text-gray-300">
                <tr>
                  <th class="px-3 py-2 text-left font-medium">Account</th>
                  <th class="px-3 py-2 text-left font-medium">Version</th>
                  <th class="px-3 py-2 text-left font-medium">Accepted</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in store.acceptances" :key="row.id" class="border-t border-gray-200 dark:border-gray-750" data-testid="terms-panel-acceptance">
                  <td class="px-3 py-2 text-gray-900 dark:text-gray-100">{{ row.email || `user #${row.user_id}` }}</td>
                  <td class="px-3 py-2 font-mono text-[12px] text-gray-600 dark:text-gray-300">{{ row.terms_version }}</td>
                  <td class="px-3 py-2 text-gray-600 dark:text-gray-300 tabular-nums" :title="row.accepted_at">{{ formatWhen(row.accepted_at) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <ConfirmDialog
      v-model:visible="confirmRestore"
      title="Restore the default terms?"
      message="The shipped text replaces your custom text. The default is its own version: anyone who accepted it before is not asked again; anyone who only accepted your custom text is asked at their next visit."
      confirm-text="Restore the default"
      @confirm="restore"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import BaseTextarea from '../base/BaseTextarea.vue'
import BaseToggle from '../base/BaseToggle.vue'
import ConfirmDialog from '../ConfirmDialog.vue'
import InlineError from '../InlineError.vue'
import LoadFailed from '../LoadFailed.vue'
import { useTermsStore } from '../../stores/terms'
import { viewState } from '../../utils/loadingState'
import { termsClientError } from '../../utils/termsEditor'

const store = useTermsStore()

const title = ref('')
const text = ref('')
const required = ref(true)
const saving = ref(false)
const saveError = ref('')
const saveErrorDetail = ref('')
const savedNotice = ref('')
const confirmRestore = ref(false)

const view = computed(() => viewState({ hasLoaded: store.hasLoaded, error: store.error }))
const terms = computed(() => store.current)
const dirty = computed(() => !!terms.value && (title.value !== (terms.value.title || '') || text.value !== (terms.value.text || '')))

const clientError = computed(() => (dirty.value ? termsClientError({ title: title.value, text: text.value }) : null))
const textError = computed(() => (clientError.value?.field === 'text' ? clientError.value.message : ''))
const titleError = computed(() => (clientError.value?.field === 'title' ? clientError.value.message : ''))

const acceptancesTotal = computed(() => {
  if (!store.acceptancesLoaded) return ''
  const n = store.acceptances.length
  return n === 1 ? '1 acceptance' : `${n} acceptances${n >= 200 ? ' · latest 200 shown' : ''}`
})

function adopt() {
  if (!terms.value) return
  title.value = terms.value.title || ''
  text.value = terms.value.text || ''
  required.value = terms.value.required !== false
}

watch(terms, adopt)

function errorDetail(e) {
  if (!e) return ''
  const d = e.response?.data?.detail
  return String((d && (d.message || d.code)) || d || e.message || '')
}

function discard() {
  adopt()
  saveError.value = ''
}

function notice(message) {
  savedNotice.value = message
  setTimeout(() => { savedNotice.value = '' }, 4000)
}

async function run(action, done) {
  saving.value = true
  saveError.value = ''
  saveErrorDetail.value = ''
  try {
    await action()
    adopt()
    notice(done)
    await store.loadAcceptances()
  } catch (e) {
    const d = e.response?.data?.detail
    saveError.value = (d && d.message) || 'The terms were not saved. Try again.'
    saveErrorDetail.value = errorDetail(e)
  } finally {
    saving.value = false
  }
}

function save() {
  if (clientError.value || !dirty.value) return
  return run(() => store.save({ title: title.value, text: text.value }), 'Saved as a new version — everyone else is asked at their next visit.')
}

function restore() {
  return run(() => store.restoreDefault(), 'The default terms are back.')
}

function saveSwitch(value) {
  return run(() => store.save({ required: value }), value ? 'Everyone is asked at their next visit.' : 'Nobody is asked.')
}

onMounted(async () => {
  await store.load()
  adopt()
  await store.loadAcceptances()
})

function formatWhen(iso) {
  if (!iso) return '—'
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  const diff = Date.now() - t
  const day = 86_400_000
  if (diff < 60_000) return 'just now'
  if (diff < 3_600_000) return `${Math.round(diff / 60_000)} min ago`
  if (diff < day) return `${Math.round(diff / 3_600_000)} h ago`
  if (diff < 30 * day) return `${Math.round(diff / day)} d ago`
  return new Date(t).toISOString().slice(0, 10)
}
</script>
