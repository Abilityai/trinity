<template>
  <!-- ent#454. Gated on the entitlement: on an OSS build (or an unentitled
       enterprise build) `enterprise_features` omits the id and this renders
       nothing at all — no dead control promising a verb the backend 404s
       (the CrossModelValidationPanel.vue precedent). -->
  <BaseCard v-if="entitled">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h3 class="text-sm font-semibold text-gray-900 dark:text-gray-100">
          Remove a deployed system
        </h3>
        <p class="mt-1 max-w-xl text-xs text-gray-500 dark:text-gray-400">
          The inverse of installing one: preview everything a removal would touch,
          confirm it agent by agent, then remove. Each agent goes through the
          standard delete, so its data and name are retained for the recovery
          window — an admin can restore the record, though not the container.
        </p>
      </div>
    </div>

    <!-- Result view ------------------------------------------------------- -->
    <div v-if="store.teardownResult" class="mt-4">
      <TeardownResult
        :result="store.teardownResult"
        @view-fleet="goToFleet"
        @remove-another="startOver"
      />
    </div>

    <!-- Outcome-unknown view ----------------------------------------------
         A timeout does NOT cancel the server: teardown is synchronous and
         serial, so agents may still be being removed. Offering "retry" here
         would be offering to delete more, so it deliberately is not offered. -->
    <div
      v-else-if="store.teardownOutcomeUnknown"
      class="mt-4 rounded-lg border-2 border-status-warning-400 dark:border-status-warning-600 bg-status-warning-50 dark:bg-status-warning-900/20 p-4"
    >
      <!-- `text-sm`, like its twin in TeardownPreview and the panel h3 above. -->
      <h4 class="text-sm font-semibold text-status-warning-900 dark:text-status-warning-100 flex items-center gap-2">
        <span aria-hidden="true">❓</span> Outcome unknown — removal may still be running
      </h4>
      <p class="mt-2 text-sm text-status-warning-800 dark:text-status-warning-200">
        {{ store.teardownOutcomeUnknown }}
      </p>
      <p class="mt-2 text-sm text-status-warning-800 dark:text-status-warning-200">
        <strong>Do not simply try again.</strong> Check your agent list first to see
        which members are gone — the server keeps removing them even though this
        request gave up waiting.
      </p>
      <div class="mt-3 flex flex-wrap gap-2">
        <BaseButton @click="goToFleet">Check the agent list</BaseButton>
        <BaseButton variant="secondary" @click="startOver">Start over</BaseButton>
      </div>
    </div>

    <!-- Remove view ------------------------------------------------------- -->
    <div v-else class="mt-4 space-y-4">
      <!-- The panel owns the label and the help, not BaseInput — which is the
           affordance that primitive documents ("`label`/`help` are optional so
           the control can slot into composed layouts that own their own
           label") and the shape TemplateRegistryPanel already uses for the same
           control-plus-action row.

           It has to be composed this way because BaseInput is a THREE-part
           stack: label, control, help. Left whole inside the row, any flex
           alignment resolves against that whole box, so `items-end` put the
           button's bottom on the HELP TEXT's bottom — 22px below the control's
           centre, reading as a stranded caption rather than the field's action.
           With label and help lifted out, the row holds two control boxes and
           alignment means what it says.

           `items-stretch` rather than `items-start`: the two primitives are
           deliberately different heights (field py-2 on a 20px line box = 38px,
           button py-[7px] on a 13.5px/1.35 line box = 34px), and stretching the
           shorter one to the row costs no magic number and no override of
           either recipe's padding. -->
      <div>
        <label :for="nameFieldId" :class="[LABEL_CLASS, 'mb-1']">System name</label>
        <div class="flex flex-wrap items-stretch gap-3">
          <BaseInput
            :id="nameFieldId"
            :model-value="store.teardownName"
            class="min-w-0 flex-1"
            placeholder="acme"
            data-testid="teardown-name"
            :aria-describedby="`${nameFieldId}-help`"
            @update:model-value="store.setTeardownName($event)"
            @keyup.enter="preview"
          />
          <BaseButton
            :disabled="!store.teardownName.trim() || store.isPreviewingTeardown"
            :loading="store.isPreviewingTeardown"
            loading-label="Checking…"
            variant="secondary"
            data-testid="teardown-preview"
            @click="preview"
          >
            Preview removal
          </BaseButton>
        </div>
        <p :id="`${nameFieldId}-help`" class="mt-1 text-xs text-gray-500 dark:text-gray-400">
          The name the manifest deployed under — the prefix its agents share.
        </p>
      </div>

      <InlineError
        v-if="store.teardownError"
        :message="store.teardownError"
        @dismiss="store.teardownError = null"
      />

      <!-- Not previewed yet vs previewed something else --------------------
           Both arms matter: "Preview first" is a different instruction from
           "your preview is for another system", and conflating them is how a
           user reads a stale removal set as current. -->
      <p
        v-if="!store.teardownPreview && store.teardownPreviewedName"
        class="text-sm text-gray-600 dark:text-gray-400"
      >
        The name changed since the last preview. Preview
        <span class="font-mono">{{ store.teardownName.trim() || '…' }}</span>
        before removing anything.
      </p>

      <template v-if="store.teardownPreview && store.teardownPreviewIsCurrent">
        <TeardownPreview
          :preview="store.teardownPreview"
          :acknowledged="acknowledged"
          :checked="checked"
          @update:acknowledged="acknowledged = $event"
          @update:checked="checked = $event"
        />

        <div class="flex flex-wrap items-center gap-3">
          <BaseButton
            variant="danger"
            :disabled="!canRemove"
            :loading="store.isTearingDown"
            loading-label="Removing…"
            data-testid="teardown-execute"
            @click="execute"
          >
            Remove {{ checked.length }} agent{{ checked.length === 1 ? '' : 's' }}
          </BaseButton>
          <BaseButton variant="ghost" @click="startOver">Cancel</BaseButton>
          <span
            v-if="blockedReason"
            class="text-xs text-gray-500 dark:text-gray-400"
            data-testid="teardown-blocked"
          >
            {{ blockedReason }}
          </span>
        </div>
      </template>
    </div>
  </BaseCard>
</template>

<script setup>
/**
 * Remove a deployed system (trinity-enterprise#454).
 *
 * The entitled half of the Library → Systems section: install is OSS, remove is
 * a gated module, so this component renders nothing when the id is absent from
 * `enterprise_features`.
 *
 * Preview → per-agent checklist → acknowledgement → remove. Three gates on the
 * destructive button, and each exists for its own reason:
 *
 *   * the preview must be CURRENT for the typed name — a removal set for `acme`
 *     says nothing about `acme-2`;
 *   * the consequence must be acknowledged (the ent#126 ack contract, reused);
 *   * at least one agent must be checked — the server rejects an empty
 *     confirmed set with a 400, and a button whose only outcome is an error
 *     should not be enabled (principle 15).
 *
 * A fourth condition disables it entirely: a preview whose membership the
 * server could not verify. The backend refuses that with a 503 regardless, so
 * this is not enforcement — it is not offering a verb that cannot succeed.
 *
 * The system name is TYPED (or arrives from a deploy result), never taken from
 * `GET /api/systems`: that endpoint groups agents by their last hyphen rather
 * than by the membership predicate, so it reports names that are not systems.
 */
import { computed, ref, useId, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSystemsStore, TEARDOWN_FEATURE_ID } from '../../stores/systems'
import { useEnterpriseStore } from '../../stores/enterprise'
import BaseButton from '../base/BaseButton.vue'
import BaseCard from '../base/BaseCard.vue'
import BaseInput from '../base/BaseInput.vue'
import { LABEL_CLASS } from '../base/fieldClasses'
import InlineError from '../InlineError.vue'
import TeardownPreview from './TeardownPreview.vue'
import TeardownResult from './TeardownResult.vue'

const props = defineProps({
  // Prefilled from a deploy result, so "I just installed the wrong thing" has a
  // one-click path into the inverse (AC #7).
  initialName: { type: String, default: '' }
})

const router = useRouter()
const store = useSystemsStore()
const enterpriseStore = useEnterpriseStore()

const entitled = computed(() => enterpriseStore.enterpriseFeatures.includes(TEARDOWN_FEATURE_ID))

// The label and help live in the template, so their `for`/`aria-describedby`
// wiring needs an id the panel controls rather than the one BaseInput would
// have minted for itself.
const nameFieldId = useId()

const acknowledged = ref(false)
const checked = ref([])

// A fresh preview ticks what the server CONFIRMED and leaves the rest to the
// operator — the rule and its reasoning live in the store (`teardownDefault
// Selection`) so a node-environment vitest can execute it. Ticking everything
// made the escape hatch an opt-OUT on a delete, in the one state the 503
// refusal does not cover: a `prefix` member with the tags reading fine.
watch(
  () => store.teardownPreview,
  () => {
    checked.value = [...store.teardownDefaultSelection]
    acknowledged.value = false
  }
)

watch(
  () => props.initialName,
  (name) => {
    if (name) store.setTeardownName(name)
  },
  { immediate: true }
)

const canRemove = computed(
  () => store.teardownPreviewIsCurrent
    && !store.teardownMembershipUnverified
    && acknowledged.value
    && checked.value.length > 0
    && !store.isTearingDown
)

/** Why Remove is disabled — stated, so the button is never mysteriously dead. */
const blockedReason = computed(() => {
  if (canRemove.value || store.isTearingDown) return ''
  if (store.teardownMembershipUnverified) {
    return 'Removal is unavailable until membership can be verified.'
  }
  if (!checked.value.length) return 'Select at least one agent.'
  if (!acknowledged.value) return 'Confirm you understand the consequence.'
  return ''
})

async function preview () {
  await store.previewTeardown()
}

async function execute () {
  await store.teardown(checked.value)
}

function startOver () {
  store.resetTeardown()
  acknowledged.value = false
  checked.value = []
}

function goToFleet () {
  router.push('/')
}
</script>
