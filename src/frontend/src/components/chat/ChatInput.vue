<template>
  <div
    class="relative bg-gray-50 dark:bg-gray-700 border border-gray-200 dark:border-gray-600 rounded-xl shadow-lg p-3"
    @dragover.prevent="dragOver = true"
    @dragleave="dragOver = false"
    @drop.prevent="onDrop"
  >

    <!-- ── Autocomplete Dropdown (appears above input) ──────────────────── -->
    <Transition
      enter-active-class="transition ease-out duration-100"
      enter-from-class="opacity-0 translate-y-1"
      enter-to-class="opacity-100 translate-y-0"
      leave-active-class="transition ease-in duration-75"
      leave-from-class="opacity-100 translate-y-0"
      leave-to-class="opacity-0 translate-y-1"
    >
      <div
        v-if="ac.showDropdown.value && ac.filteredPlaybooks.value.length > 0"
        class="absolute bottom-full mb-2 left-0 right-0 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-xl z-50 overflow-hidden"
        @mousedown.prevent
      >
        <!-- Header hint -->
        <div class="px-3 py-1.5 border-b border-gray-100 dark:border-gray-700 flex items-center justify-between">
          <span class="text-xs text-gray-400 dark:text-gray-500 font-medium">Playbooks</span>
          <span class="text-xs text-gray-400 dark:text-gray-500">
            <kbd class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-[10px] font-mono">↑↓</kbd>
            navigate &nbsp;
            <kbd class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-[10px] font-mono">Tab</kbd>
            accept
          </span>
        </div>

        <!-- Suggestion list (max 8 items) -->
        <ul class="max-h-56 overflow-y-auto py-1">
          <li
            v-for="(playbook, idx) in ac.filteredPlaybooks.value.slice(0, 8)"
            :key="playbook.name"
            :class="[
              'flex items-start gap-2 px-3 py-2 cursor-pointer transition-colors',
              idx === ac.selectedIndex.value
                ? 'bg-indigo-50 dark:bg-indigo-900/40'
                : 'hover:bg-gray-50 dark:hover:bg-gray-700/60'
            ]"
            @click="onClickSuggestion(playbook)"
          >
            <!-- Command name -->
            <code class="shrink-0 text-sm font-semibold text-indigo-600 dark:text-indigo-400">
              /{{ playbook.name }}
            </code>

            <!-- Description -->
            <span class="flex-1 text-xs text-gray-500 dark:text-gray-400 truncate leading-5">
              {{ playbook.description || '' }}
            </span>

            <!-- Argument hint preview -->
            <span
              v-if="playbook.argument_hint"
              class="shrink-0 text-xs text-gray-400 dark:text-gray-500 font-mono hidden sm:block"
            >
              {{ playbook.argument_hint }}
            </span>

            <!-- Tab badge on the highlighted item -->
            <span
              v-if="idx === ac.selectedIndex.value"
              class="shrink-0 ml-auto text-[10px] text-gray-400 dark:text-gray-500 font-mono"
            >
              Tab ⇥
            </span>
          </li>
        </ul>

        <!-- Overflow hint -->
        <div
          v-if="ac.filteredPlaybooks.value.length > 8"
          class="px-3 py-1 border-t border-gray-100 dark:border-gray-700 text-xs text-gray-400 dark:text-gray-500"
        >
          {{ ac.filteredPlaybooks.value.length - 8 }} more — keep typing to filter
        </div>
      </div>
    </Transition>

    <!-- ── File preview chips ────────────────────────────────────────────── -->
    <div v-if="pendingFiles.length > 0" class="flex flex-wrap gap-1 mb-2">
      <div
        v-for="(f, idx) in pendingFiles"
        :key="idx"
        class="flex items-center gap-1 px-2 py-0.5 bg-indigo-100 dark:bg-indigo-900/40 text-indigo-700 dark:text-indigo-300 text-xs rounded-full"
      >
        <svg class="w-3 h-3 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" />
        </svg>
        <span class="max-w-[120px] truncate">{{ f.name }}</span>
        <button type="button" @click="removeFile(idx)" class="ml-0.5 hover:text-status-danger-500 transition-colors" title="Remove">
          <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
    </div>

    <!-- ── Drag-over overlay ──────────────────────────────────────────────── -->
    <div
      v-if="dragOver"
      class="absolute inset-0 rounded-xl bg-indigo-50/90 dark:bg-indigo-900/60 border-2 border-dashed border-indigo-400 flex items-center justify-center z-20 pointer-events-none"
    >
      <span class="text-indigo-600 dark:text-indigo-300 text-sm font-medium">Drop files here</span>
    </div>

    <!-- ── Input row ─────────────────────────────────────────────────────── -->
    <form @submit.prevent="handleSubmit" class="flex items-end space-x-2">
      <!-- Hidden file input -->
      <input
        ref="fileInputRef"
        type="file"
        multiple
        accept="image/*,text/*,application/json,application/csv,.csv,.txt,.json,.py,.js,.ts,.md,.yaml,.yml"
        class="hidden"
        @change="onFileInputChange"
      />

      <!-- Ghost text + textarea wrapper -->
      <div ref="inputWrapperRef" class="flex-1 relative">

        <!--
          Ghost-text overlay: shows the typed text (transparent) followed by the
          predicted completion (gray). Sits behind the textarea.
        -->
        <div
          v-if="ac.ghostCompletion.value"
          aria-hidden="true"
          class="ghost-overlay absolute inset-0 pointer-events-none select-none overflow-hidden break-words whitespace-pre-wrap text-sm leading-6"
        >
          <span class="text-transparent">{{ localMessage }}</span><span class="text-gray-400 dark:text-gray-500">{{ ac.ghostCompletion.value }}</span>
        </div>

        <textarea
          ref="textareaRef"
          v-model="localMessage"
          rows="1"
          :placeholder="showPlaceholder ? placeholder : ''"
          class="relative z-10 w-full resize-none border-0 p-0 bg-transparent text-gray-900 dark:text-white placeholder-gray-500 dark:placeholder-gray-400 focus:ring-0 focus:outline-none text-sm leading-6"
          :disabled="disabled"
          @keydown="handleKeydown"
          @input="handleInput"
          @blur="handleBlur"
          @click="handleClick"
        ></textarea>
      </div>

      <!-- Paperclip / attach button -->
      <button
        type="button"
        @click="fileInputRef?.click()"
        :disabled="disabled || pendingFiles.length >= 3"
        class="p-2 rounded-lg transition-colors shrink-0 bg-gray-200 dark:bg-gray-600 text-gray-600 dark:text-gray-300 hover:bg-indigo-100 dark:hover:bg-indigo-900/30 hover:text-indigo-600 dark:hover:text-indigo-400 disabled:opacity-40"
        title="Attach files (max 3, 5 MB each)"
      >
        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" />
        </svg>
      </button>

      <!-- Voice button (VOICE-004) -->
      <button
        v-if="voiceAvailable"
        type="button"
        @click="$emit('voice')"
        :disabled="disabled || voiceActive"
        class="p-2 rounded-lg transition-colors shrink-0"
        :class="voiceActive
          ? 'bg-status-danger-500 hover:bg-status-danger-600 text-white'
          : 'bg-gray-200 dark:bg-gray-600 text-gray-600 dark:text-gray-300 hover:bg-indigo-100 dark:hover:bg-indigo-900/30 hover:text-indigo-600 dark:hover:text-indigo-400 disabled:opacity-50'"
        :title="voiceActive ? 'Voice session active' : 'Start voice conversation'"
      >
        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4M12 15a3 3 0 003-3V5a3 3 0 00-6 0v7a3 3 0 003 3z" />
        </svg>
      </button>

      <!-- Send button -->
      <button
        type="submit"
        :disabled="disabled || (!localMessage.trim() && pendingFiles.length === 0)"
        class="p-2 bg-indigo-600 hover:bg-indigo-700 disabled:bg-indigo-400 text-white rounded-lg transition-colors shrink-0"
      >
        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
        </svg>
      </button>
    </form>

    <!-- ── Argument hint bar (shown after "/command " is completed) ──────── -->
    <Transition
      enter-active-class="transition ease-out duration-100"
      enter-from-class="opacity-0"
      enter-to-class="opacity-100"
      leave-active-class="transition ease-in duration-75"
      leave-from-class="opacity-100"
      leave-to-class="opacity-0"
    >
      <div
        v-if="ac.activeArgHint.value"
        class="mt-1.5 flex items-center gap-1.5 text-xs font-mono text-gray-400 dark:text-gray-500"
      >
        <span class="text-indigo-500 dark:text-indigo-400">/{{ ac.activeArgHint.value.name }}</span>
        <span>{{ ac.activeArgHint.value.argument_hint }}</span>
        <span
          v-if="ac.activeArgHint.value.description"
          class="ml-1 not-italic text-gray-400 dark:text-gray-500 font-sans truncate"
        >
          — {{ ac.activeArgHint.value.description }}
        </span>
      </div>
    </Transition>
  </div>
</template>

<script setup>
import { ref, watch, computed } from 'vue'
import { usePlaybookAutocomplete } from '../../composables/usePlaybookAutocomplete'
import { useAuthStore } from '../../stores/auth'

const MAX_FILES = 3
const MAX_FILE_BYTES = 5 * 1024 * 1024  // 5 MB

const props = defineProps({
  modelValue: {
    type: String,
    default: ''
  },
  placeholder: {
    type: String,
    default: 'Type your message or / for playbooks…'
  },
  disabled: {
    type: Boolean,
    default: false
  },
  agentName: {
    type: String,
    default: null
  },
  agentStatus: {
    type: String,
    default: 'stopped'
  },
  publicToken: {
    type: String,
    default: null
  },
  voiceAvailable: {
    type: Boolean,
    default: false
  },
  voiceActive: {
    type: Boolean,
    default: false
  }
})

const emit = defineEmits(['update:modelValue', 'submit', 'voice'])

const authStore = useAuthStore()
const ac = usePlaybookAutocomplete()

const localMessage = ref(props.modelValue)
const textareaRef = ref(null)
const inputWrapperRef = ref(null)
const fileInputRef = ref(null)
const pendingFiles = ref([])   // [{name, mimetype, size, data_base64}]
const dragOver = ref(false)

// Hide the default placeholder text while the dropdown/ghost hint is active
const showPlaceholder = computed(() => !ac.showDropdown.value && !ac.activeArgHint.value)

// ── Sync v-model ────────────────────────────────────────────────────────────
watch(() => props.modelValue, (val) => {
  localMessage.value = val
})
watch(localMessage, (val) => {
  emit('update:modelValue', val)
})

// ── Load playbooks when the agent is available ───────────────────────────────
watch(
  () => [props.agentName, props.agentStatus],
  ([name, status]) => {
    if (name && status === 'running') {
      ac.load(name, authStore.authHeader)
    }
  },
  { immediate: true }
)

// ── Load playbooks via public token (no auth) ────────────────────────────────
watch(
  () => props.publicToken,
  (token) => {
    if (token) {
      ac.loadPublic(token)
    }
  },
  { immediate: true }
)

// ── Input event: parse for slash commands ────────────────────────────────────
function handleInput(event) {
  const textarea = event.target
  ac.parse(localMessage.value, textarea.selectionStart)
  autoResize(textarea)
}

function handleClick() {
  // Re-parse on click so caret position is fresh
  if (textareaRef.value) {
    ac.parse(localMessage.value, textareaRef.value.selectionStart)
  }
}

function handleBlur() {
  // Delay hiding so click on dropdown item fires first
  setTimeout(() => ac.hide(), 150)
}

// ── Keyboard navigation ───────────────────────────────────────────────────────
function handleKeydown(event) {
  if (ac.showDropdown.value && ac.filteredPlaybooks.value.length > 0) {
    if (event.key === 'Tab' || event.key === 'ArrowRight') {
      if (ac.ghostCompletion.value || ac.showDropdown.value) {
        event.preventDefault()
        _commitAccept()
        return
      }
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      ac.moveDown()
      return
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault()
      ac.moveUp()
      return
    }
    if (event.key === 'Enter') {
      event.preventDefault()
      _commitAccept()
      return
    }
    if (event.key === 'Escape') {
      event.preventDefault()
      ac.hide()
      return
    }
  }

  // Default submit on Enter (no autocomplete active)
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    handleSubmit()
  }
}

function _commitAccept() {
  if (!textareaRef.value) return
  const result = ac.accept(localMessage.value, textareaRef.value.selectionStart)
  if (result) {
    localMessage.value = result.value
    // Set caret after the inserted command
    nextTick_(() => {
      if (textareaRef.value) {
        textareaRef.value.setSelectionRange(result.caretPos, result.caretPos)
        autoResize(textareaRef.value)
      }
    })
  }
}

function onClickSuggestion(playbook) {
  if (!textareaRef.value) return
  const result = ac.acceptPlaybook(playbook, localMessage.value, textareaRef.value.selectionStart)
  if (result) {
    localMessage.value = result.value
    nextTick_(() => {
      if (textareaRef.value) {
        textareaRef.value.focus()
        textareaRef.value.setSelectionRange(result.caretPos, result.caretPos)
        autoResize(textareaRef.value)
      }
    })
  }
}

// ── Resize ────────────────────────────────────────────────────────────────────
function autoResize(textarea) {
  textarea.style.height = 'auto'
  textarea.style.height = Math.min(textarea.scrollHeight, 150) + 'px'
}

// ── File handling ─────────────────────────────────────────────────────────────
function encodeFile(file) {
  return new Promise((resolve) => {
    const reader = new FileReader()
    reader.onload = (e) => resolve(e.target.result)  // data: URI
    reader.readAsDataURL(file)
  })
}

async function addFiles(fileList) {
  const remaining = MAX_FILES - pendingFiles.value.length
  const toAdd = Array.from(fileList).slice(0, remaining)
  for (const file of toAdd) {
    if (file.size > MAX_FILE_BYTES) {
      alert(`"${file.name}" exceeds the 5 MB limit and was skipped.`)
      continue
    }
    const data_base64 = await encodeFile(file)
    // Parse MIME from data: URI prefix (more reliable than file.type across browsers)
    const mimeMatch = data_base64.match(/^data:([^;]+);/)
    const mimetype = mimeMatch ? mimeMatch[1] : (file.type || 'application/octet-stream')
    pendingFiles.value.push({ name: file.name, mimetype, size: file.size, data_base64 })
  }
}

function onFileInputChange(event) {
  addFiles(event.target.files)
  event.target.value = ''  // reset so same file can be re-selected
}

function onDrop(event) {
  dragOver.value = false
  if (event.dataTransfer?.files?.length) {
    addFiles(event.dataTransfer.files)
  }
}

function removeFile(idx) {
  pendingFiles.value.splice(idx, 1)
}

// ── Submit ────────────────────────────────────────────────────────────────────
function handleSubmit() {
  const hasMessage = localMessage.value.trim()
  const hasFiles = pendingFiles.value.length > 0
  if ((hasMessage || hasFiles) && !props.disabled) {
    ac.hide()
    emit('submit', localMessage.value.trim(), [...pendingFiles.value])
    localMessage.value = ''
    pendingFiles.value = []
    if (textareaRef.value) {
      textareaRef.value.style.height = 'auto'
    }
  }
}

// Micro-task helper (avoid importing nextTick from vue at module level)
function nextTick_(fn) {
  Promise.resolve().then(fn)
}

// ── Expose focus ──────────────────────────────────────────────────────────────
defineExpose({
  focus: () => textareaRef.value?.focus()
})
</script>

<style scoped>
/* Ensure ghost overlay font metrics match the textarea exactly */
.ghost-overlay {
  font-size: 0.875rem; /* text-sm */
  line-height: 1.5rem; /* leading-6 */
  padding: 0;
  /* Same font family as textarea inherits from body */
  font-family: inherit;
  word-break: break-word;
}
</style>
