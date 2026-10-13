<template>
  <!-- #1924: the keyboard contract comes from BaseModal (#1923) — Esc, focus
       trap, focus return, and initial focus on the SAFE action. The dialog's
       own markup below is unchanged, so every data-testid and the visual
       button order are preserved. -->
  <BaseModal
    :model-value="visible"
    panel-class="bg-white dark:bg-gray-800 rounded-lg text-left overflow-hidden shadow-xl transform transition-all w-full sm:max-w-lg"
    labelledby="confirm-dialog-title-h"
    @close="onCancel"
  >
    <div data-testid="confirm-dialog">
      <div data-testid="confirm-dialog-content">
          <div class="bg-white dark:bg-gray-800 px-4 pt-5 pb-4 sm:p-6 sm:pb-4">
            <div class="sm:flex sm:items-start">
              <!-- Icon -->
              <div :class="[
                'mx-auto flex-shrink-0 flex items-center justify-center h-12 w-12 rounded-full sm:mx-0 sm:h-10 sm:w-10',
                ICON_BG[variant]
              ]">
                <svg
                  :class="['h-6 w-6', ICON_INK[variant]]"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  aria-hidden="true"
                  data-testid="confirm-dialog-icon"
                  :data-variant="variant"
                >
                  <path v-if="variant === 'info'" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                  <path v-else stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                </svg>
              </div>

              <!-- Content -->
              <div class="mt-3 text-center sm:mt-0 sm:ml-4 sm:text-left flex-1">
                <h3 id="confirm-dialog-title-h" class="text-lg leading-6 font-medium text-gray-900 dark:text-white" data-testid="confirm-dialog-title">
                  {{ title }}
                </h3>
                <div class="mt-2">
                  <p class="text-sm text-gray-500 dark:text-gray-400" data-testid="confirm-dialog-message">
                    {{ message }}
                  </p>
                </div>
              </div>
            </div>
          </div>

          <!-- Actions (BaseButton, #2122). The confirm is the unsafe action in
               both dialog variants, so it renders as the danger button; the
               header icon carries the danger/warning distinction. -->
          <div class="bg-gray-50 dark:bg-gray-900 px-4 py-3 sm:px-6 sm:flex sm:flex-row-reverse">
            <!-- #1924: `data-destructive` is what makes focus land on Cancel.
                 The DOM order stays confirm-first and `sm:flex-row-reverse`
                 still renders Confirm on the right — marking it moves the
                 FOCUS without moving the button, which reordering would. -->
            <BaseButton
              :variant="confirmVariant"
              class="w-full sm:ml-3 sm:w-auto"
              data-testid="confirm-dialog-confirm"
              data-destructive
              @click="onConfirm"
            >
              {{ confirmText }}
            </BaseButton>
            <BaseButton
              variant="secondary"
              class="mt-3 w-full sm:mt-0 sm:ml-3 sm:w-auto"
              data-testid="confirm-dialog-cancel"
              @click="onCancel"
            >
              {{ cancelText }}
            </BaseButton>
          </div>
      </div>
    </div>
  </BaseModal>
</template>

<script setup>
import { ref, watch } from 'vue'
import BaseButton from './base/BaseButton.vue'
import BaseModal from './base/BaseModal.vue'

// The icon's disc and ink per variant (danger / warning unchanged).
const ICON_BG = {
  danger: 'bg-status-danger-100 dark:bg-status-danger-900/50',
  warning: 'bg-status-warning-100 dark:bg-status-warning-900/50',
  info: 'bg-action-primary-100 dark:bg-action-primary-900/50',
}
const ICON_INK = {
  danger: 'text-status-danger-600 dark:text-status-danger-400',
  warning: 'text-status-warning-700 dark:text-status-warning-400',
  info: 'text-action-primary-700 dark:text-action-primary-300',
}

const props = defineProps({
  visible: {
    type: Boolean,
    default: false
  },
  title: {
    type: String,
    default: 'Confirm Action'
  },
  message: {
    type: String,
    required: true
  },
  confirmText: {
    type: String,
    default: 'Confirm'
  },
  cancelText: {
    type: String,
    default: 'Cancel'
  },
  variant: {
    type: String,
    // 'danger' | 'warning' | 'info'. ent#610 round 3: `info` (an i in a
    // circle, primary ink) is for a confirm that is consequential but NOT
    // destructive — a warning triangle over "Mark 28 chats read" read as a
    // danger the primary button then contradicted.
    default: 'danger',
    validator: (value) => ['danger', 'warning', 'info'].includes(value)
  },
  // ent#610 §3g A9: the confirm button's own variant. Default `danger`, so
  // every existing dialog is unchanged; `primary` is for a confirm that is
  // consequential but not destructive (Mark N chats read). Focus still lands
  // on Cancel either way — that is `data-destructive`'s job, not the colour's.
  confirmVariant: {
    type: String,
    default: 'danger',
    validator: (value) => ['danger', 'primary'].includes(value)
  }
})

const emit = defineEmits(['confirm', 'cancel', 'update:visible'])

const onConfirm = () => {
  emit('confirm')
  emit('update:visible', false)
}

const onCancel = () => {
  emit('cancel')
  emit('update:visible', false)
}
</script>
