<template>
  <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
    <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
      <h2 class="text-lg font-medium text-gray-900 dark:text-white">Security &amp; product updates</h2>
      <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
        Optionally receive occasional emails about
        <span class="font-medium">security fixes affecting your version</span> and
        significant product updates. Off by default; identified contact (email
        + a few optional details), never anonymous telemetry. (ent#463)
      </p>
    </div>

    <div class="p-6 space-y-5">
      <div v-if="store.error" class="text-sm text-status-danger-600 dark:text-status-danger-400">
        {{ store.error }}
      </div>

      <!-- Hard-disabled by config -->
      <div
        v-if="store.status.hard_disabled"
        class="rounded-md border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/20 px-4 py-3 text-sm text-amber-800 dark:text-amber-300"
      >
        Intake is disabled by configuration
        (<code class="text-xs">OPERATOR_INTAKE_ENABLED=false</code> or
        <code class="text-xs">DO_NOT_TRACK</code>). Nothing leaves the box.
      </div>

      <!-- Already submitted: terminal state -->
      <div
        v-else-if="store.status.already_submitted"
        class="space-y-4"
      >
        <div class="rounded-md border border-status-success-200 dark:border-status-success-800 bg-status-success-50 dark:bg-status-success-900/20 px-4 py-3 text-sm text-status-success-800 dark:text-status-success-300">
          <div class="font-medium">Already opted in</div>
          <p class="mt-1">
            Contact details were submitted on
            <span class="font-medium">{{ fmt(store.status.submitted_at) || 'an earlier date (not recorded)' }}</span>.
            The intake is at-most-once per install, so this cannot be re-sent from here.
          </p>
        </div>

        <div class="flex items-start justify-between gap-4">
          <div>
            <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Future updates</p>
            <p class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
              <template v-if="store.status.enabled">Consent is on since {{ fmt(store.status.consent_at) || 'the initial submission' }}.</template>
              <template v-else>Consent is off — future updates would not be sent.</template>
            </p>
          </div>
          <button
            type="button"
            :disabled="store.saving"
            @click="toggleConsent"
            :class="[
              'relative inline-flex h-6 w-11 flex-shrink-0 rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-action-primary-500 focus:ring-offset-2 dark:focus:ring-offset-gray-800 disabled:opacity-40',
              store.status.enabled ? 'bg-action-primary-600' : 'bg-gray-300 dark:bg-gray-600',
            ]"
            role="switch"
            :aria-checked="store.status.enabled"
          >
            <span
              :class="['inline-block h-5 w-5 transform rounded-full bg-white transition-transform mt-0.5',
                       store.status.enabled ? 'translate-x-5' : 'translate-x-0.5']"
            ></span>
          </button>
        </div>

        <p class="text-xs text-gray-400 dark:text-gray-500">
          Toggling off records a durable decline for any future contact from this install.
          It cannot recall the existing record — the hosted endpoint has no local delete authority.
          To request deletion of the record itself, email
          <a href="mailto:support@ability.ai" class="underline">support@ability.ai</a>
          with your installation id.
        </p>
      </div>

      <!-- Fresh install: form -->
      <form
        v-else
        @submit.prevent="submit"
        class="space-y-4"
      >
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <label class="block sm:col-span-2">
            <span class="text-sm text-gray-700 dark:text-gray-200">Email <span class="text-status-danger-600">*</span></span>
            <input
              v-model.trim="form.email"
              type="email"
              required
              maxlength="254"
              autocomplete="email"
              class="mt-1 block w-full rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100 text-sm"
            />
          </label>
          <label class="block">
            <span class="text-sm text-gray-700 dark:text-gray-200">Company (optional)</span>
            <input v-model.trim="form.company" type="text" maxlength="200" class="mt-1 block w-full rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100 text-sm" />
          </label>
          <label class="block">
            <span class="text-sm text-gray-700 dark:text-gray-200">Name (optional)</span>
            <input v-model.trim="form.name" type="text" maxlength="200" class="mt-1 block w-full rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100 text-sm" />
          </label>
          <label class="block">
            <span class="text-sm text-gray-700 dark:text-gray-200">Role (optional)</span>
            <input v-model.trim="form.role" type="text" maxlength="200" class="mt-1 block w-full rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100 text-sm" />
          </label>
          <label class="block">
            <span class="text-sm text-gray-700 dark:text-gray-200">Primary use case (optional)</span>
            <input v-model.trim="form.use_case" type="text" maxlength="500" class="mt-1 block w-full rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100 text-sm" />
          </label>
        </div>

        <details class="rounded-md border border-gray-200 dark:border-gray-700">
          <summary class="cursor-pointer px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300">
            Exactly what would be sent
          </summary>
          <div class="px-4 pb-4 pt-1">
            <p class="text-xs text-gray-500 dark:text-gray-400 mb-2">
              The email above and any optional fields you filled in, plus this install's
              random installation id and the running Trinity version. No agent content,
              no user data, no credentials — this is a contact form, not telemetry.
            </p>
            <pre class="overflow-x-auto rounded bg-gray-50 dark:bg-gray-900 p-3 text-xs text-gray-700 dark:text-gray-300"><code>{{ prettyPreview }}</code></pre>
          </div>
        </details>

        <div class="flex items-center justify-between gap-4">
          <p class="text-xs text-gray-500 dark:text-gray-400">
            At-most-once per install. Reversible at any time as a durable decline.
          </p>
          <button
            type="submit"
            :disabled="store.saving || !form.email"
            class="inline-flex justify-center rounded-md border border-transparent bg-action-primary-600 py-2 px-4 text-sm font-medium text-white shadow-sm hover:bg-action-primary-700 focus:outline-none focus:ring-2 focus:ring-action-primary-500 focus:ring-offset-2 disabled:opacity-40"
          >
            {{ store.saving ? 'Submitting…' : 'Opt in &amp; submit' }}
          </button>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, reactive } from 'vue'
import { useOperatorIntakeStore } from '../../stores/operatorIntake'

const store = useOperatorIntakeStore()

const form = reactive({
  email: '',
  company: '',
  name: '',
  role: '',
  use_case: '',
})

const prettyPreview = computed(() =>
  JSON.stringify(
    {
      email: form.email || '(required)',
      company: form.company || null,
      name: form.name || null,
      role: form.role || null,
      use_case: form.use_case || null,
      consent: 'security_and_product_updates',
    },
    null,
    2,
  ),
)

function fmt(iso) {
  if (!iso) return ''
  try { return new Date(iso).toLocaleString() } catch { return iso }
}

async function submit() {
  if (!form.email) return
  const ok = await store.setConsent({
    enabled: true,
    email: form.email,
    company: form.company || null,
    name: form.name || null,
    role: form.role || null,
    use_case: form.use_case || null,
  })
  if (ok) store.load(true)
}

async function toggleConsent() {
  const enabling = !store.status.enabled
  const ok = await store.setConsent({ enabled: enabling })
  if (ok) store.load(true)
}

onMounted(() => {
  store.load()
})
</script>
