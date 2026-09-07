<template>
  <!-- ent#473: one component for the three places a chat's title can be
       renamed — the sidebar row, the 1:1 header and the room header. Three
       inline copies of an editor that carries a pencil, an Enter/Esc/blur
       contract, client-side validation and a failed-verb surface is three
       places for those to drift (the PortalStarButton lesson, ent#359).

       READ mode is the title plus a pencil; EDIT mode is a field that commits
       on Enter or blur and abandons on Esc. The pencil and the whole edit mode
       stop their keys and clicks: the sidebar row is a `role="button"` div
       that opens the chat on Enter/Space/click, and without the stops a
       rename would open the chat it was renaming (the StarButton's
       `@keydown.stop` reason). READ-mode text deliberately does NOT stop —
       the title is the row's largest target, and a click on it must still
       open the chat (found live: a row whose title swallowed the click). -->
  <div class="min-w-0 flex-1" :class="dense ? '' : 'flex flex-col'">
    <template v-if="!editing">
      <span class="min-w-0 flex items-center gap-1">
        <span
          class="min-w-0 truncate"
          :class="[textClass, value ? '' : 'text-gray-500 dark:text-gray-400']"
          :title="value || placeholder"
          @dblclick="rename && !dense ? begin() : null"
        >{{ value || placeholder }}</span>
        <!-- Reveal-on-hover from `sm:` up in the dense row, for the same
             reason the star does: a pencil on every row is noise, and a touch
             screen has no hover, so below `sm` it is simply visible. -->
        <button
          v-if="rename"
          type="button"
          class="shrink-0 rounded p-0.5 transition text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 focus:opacity-100"
          :class="dense ? 'opacity-100 sm:opacity-0 sm:group-hover:opacity-100' : ''"
          :title="label"
          :aria-label="label"
          data-testid="rename-chat"
          @click.stop="begin"
          @keydown.stop
        >
          <svg :class="dense ? 'w-3.5 h-3.5' : 'w-4 h-4'" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
          </svg>
        </button>
      </span>
    </template>

    <div v-else class="min-w-0 flex-1" @click.stop @keydown.stop>
      <!-- A plain field rather than BaseInput: BaseInput owns a label row, a
           help line and 8×11 padding, which is the right primitive for a form
           and the wrong one for a title being edited in place inside a 40px
           row or a 56px header. Same field tokens, same focus ring. -->
      <input
        ref="field"
        v-model="draft"
        type="text"
        :maxlength="CHAT_TITLE_MAX_CHARS + 20"
        :placeholder="placeholder"
        :disabled="saving"
        :aria-label="label"
        :aria-invalid="!!error"
        data-testid="rename-chat-field"
        class="w-full min-w-0 rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-2 py-1 focus:ring-2 focus:ring-action-primary-500/40 focus:border-action-primary-500 focus:outline-none disabled:opacity-50"
        :class="dense ? 'text-sm' : 'text-sm font-semibold'"
        @keydown.enter.prevent="commit"
        @keydown.esc.prevent="cancel"
        @blur="onBlur"
      />
      <!-- The failed verb persists next to the control until it is fixed or
           abandoned (principle 18); a bounds problem names the rule and an
           example (17). -->
      <InlineError v-if="error" class="mt-1" :message="error" @dismiss="error = ''" />
    </div>
  </div>
</template>

<script setup>
import { ref, nextTick } from 'vue'
import InlineError from '@/components/InlineError.vue'
import { CHAT_TITLE_MAX_CHARS, normalizeChatTitle, renameFailureMessage } from './portalUtils'

const props = defineProps({
  // The stored title ('' when the thread has none yet).
  value: { type: String, default: '' },
  placeholder: { type: String, default: 'New chat' },
  // async (title) => void — rejects with the request error. Null = read-only.
  rename: { type: Function, default: null },
  label: { type: String, default: 'Rename this chat' },
  // The sidebar-row form: smaller pencil, revealed on hover — and no
  // double-click-to-edit, since a click there is the row's open.
  dense: { type: Boolean, default: false },
  // Ink for the read-mode text (the row bolds an unread title; the header is
  // semibold). Kept a prop so this component owns no caller's typography.
  textClass: { type: String, default: '' },
})
const emit = defineEmits(['editing'])

const editing = ref(false)
const draft = ref('')
const saving = ref(false)
const error = ref('')
const field = ref(null)
// A blur fired by the component's own teardown (Enter → commit → exit) must
// not commit a second time; this flag says a commit is already in hand.
let settling = false

function begin() {
  if (!props.rename) return
  draft.value = props.value || ''
  error.value = ''
  editing.value = true
  emit('editing', true)
  nextTick(() => { field.value?.focus(); field.value?.select() })
}

function cancel() {
  if (saving.value) return
  editing.value = false
  error.value = ''
  emit('editing', false)
}

function onBlur() {
  if (settling || saving.value) return
  // Leaving the field with an unchanged draft is an abandon, not a save.
  if ((draft.value || '').trim() === (props.value || '').trim()) { cancel(); return }
  commit()
}

async function commit() {
  if (saving.value || settling) return
  const check = normalizeChatTitle(draft.value)
  if (!check.ok) { error.value = check.message; return }
  if (check.title === (props.value || '').trim()) { cancel(); return }
  saving.value = true
  settling = true
  error.value = ''
  try {
    await props.rename(check.title)
    editing.value = false
    emit('editing', false)
  } catch (err) {
    error.value = renameFailureMessage(err)
    nextTick(() => field.value?.focus())
  } finally {
    saving.value = false
    settling = false
  }
}
</script>
