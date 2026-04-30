<template>
  <div class="yaml-editor-container" :class="{ 'has-errors': errors.length > 0 }">
    <!-- Toolbar -->
    <div class="flex items-center justify-between mb-2 px-1">
      <div class="flex items-center gap-2">
        <span class="text-sm font-medium text-gray-700 dark:text-gray-200">YAML Editor</span>
        <div v-if="errors.length > 0" class="flex items-center gap-1 text-xs text-status-danger-600 dark:text-status-danger-400">
          <ExclamationCircleIcon class="h-3.5 w-3.5" />
          <span>{{ errors.length }} error{{ errors.length !== 1 ? 's' : '' }}</span>
        </div>
        <div v-else-if="warnings.length > 0" class="flex items-center gap-1 text-xs text-status-warning-600 dark:text-status-warning-400">
          <ExclamationTriangleIcon class="h-3.5 w-3.5" />
          <span>{{ warnings.length }} warning{{ warnings.length !== 1 ? 's' : '' }}</span>
        </div>
      </div>
      <div class="flex items-center gap-2">
        <button
          @click="formatYaml"
          class="p-1.5 text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700 rounded transition-colors"
          title="Format YAML"
        >
          <CodeBracketIcon class="h-4 w-4" />
        </button>
        <button
          @click="copyContent"
          class="p-1.5 text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700 rounded transition-colors"
          title="Copy to clipboard"
        >
          <ClipboardDocumentIcon v-if="!copied" class="h-4 w-4" />
          <CheckIcon v-else class="h-4 w-4 text-status-success-500" />
        </button>
      </div>
    </div>

    <!-- Editor container -->
    <div
      ref="editorContainer"
      class="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden"
      :style="{ height: height }"
    />

    <!-- Error list -->
    <div v-if="showErrors && (errors.length > 0 || warnings.length > 0)" class="mt-2 space-y-1 max-h-32 overflow-y-auto">
      <div
        v-for="(error, index) in errors"
        :key="'error-' + index"
        class="flex items-start gap-2 rounded-md border border-status-danger-300 dark:border-status-danger-700/50 bg-status-danger-50 dark:bg-status-danger-900/20 p-2 text-xs"
        @click="goToLine(error.line)"
      >
        <ExclamationCircleIcon class="h-3.5 w-3.5 mt-0.5 flex-shrink-0 text-status-danger-500" />
        <div class="text-status-danger-700 dark:text-status-danger-300">
          <span v-if="error.line" class="font-medium">Line {{ error.line }}:</span>
          {{ error.message }}
        </div>
      </div>
      <div
        v-for="(warning, index) in warnings"
        :key="'warning-' + index"
        class="flex items-start gap-2 rounded-md border border-status-warning-300 dark:border-status-warning-700/50 bg-status-warning-50 dark:bg-status-warning-900/20 p-2 text-xs"
        @click="goToLine(warning.line)"
      >
        <ExclamationTriangleIcon class="h-3.5 w-3.5 mt-0.5 flex-shrink-0 text-status-warning-500" />
        <div class="text-status-warning-700 dark:text-status-warning-300">
          <span v-if="warning.line" class="font-medium">Line {{ warning.line }}:</span>
          {{ warning.message }}
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch, onMounted, onUnmounted, computed } from 'vue'
import {
  ExclamationCircleIcon,
  ExclamationTriangleIcon,
  ClipboardDocumentIcon,
  CheckIcon,
  CodeBracketIcon,
} from '@heroicons/vue/24/outline'

const props = defineProps({
  modelValue: {
    type: String,
    default: ''
  },
  height: {
    type: String,
    default: '400px'
  },
  readOnly: {
    type: Boolean,
    default: false
  },
  validationErrors: {
    type: Array,
    default: () => []
  },
  showErrors: {
    type: Boolean,
    default: true
  }
})

const emit = defineEmits(['update:modelValue', 'change', 'save', 'cursor-context', 'selection-change'])

const editorContainer = ref(null)
const copied = ref(false)
let editor = null
let monaco = null

// Separate errors and warnings
const errors = computed(() =>
  props.validationErrors.filter(e => e.level === 'error' || !e.level)
)

const warnings = computed(() =>
  props.validationErrors.filter(e => e.level === 'warning')
)

// Dark mode detection
const isDark = ref(false)

const checkDarkMode = () => {
  isDark.value = document.documentElement.classList.contains('dark')
}

// Initialize Monaco Editor
const initMonaco = async () => {
  try {
    // Dynamic import Monaco
    monaco = await import('monaco-editor')

    if (!editorContainer.value) return

    // Define custom YAML theme for dark mode
    monaco.editor.defineTheme('trinity-dark', {
      base: 'vs-dark',
      inherit: true,
      rules: [],
      colors: {
        'editor.background': '#1f2937', // gray-800
      }
    })

    monaco.editor.defineTheme('trinity-light', {
      base: 'vs',
      inherit: true,
      rules: [],
      colors: {
        'editor.background': '#ffffff',
      }
    })

    // Create editor
    editor = monaco.editor.create(editorContainer.value, {
      value: props.modelValue,
      language: 'yaml',
      theme: isDark.value ? 'trinity-dark' : 'trinity-light',
      readOnly: props.readOnly,
      minimap: { enabled: false },
      fontSize: 14,
      lineNumbers: 'on',
      scrollBeyondLastLine: false,
      automaticLayout: true,
      tabSize: 2,
      wordWrap: 'on',
      folding: true,
      renderLineHighlight: 'all',
      scrollbar: {
        vertical: 'visible',
        horizontal: 'visible',
        useShadows: false,
      },
    })

    // Listen for content changes
    editor.onDidChangeModelContent(() => {
      const value = editor.getValue()
      emit('update:modelValue', value)
      emit('change', value)
    })

    // Listen for Cmd/Ctrl+S to save
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
      emit('save')
    })

    // Listen for cursor position changes to provide contextual help
    editor.onDidChangeCursorPosition((e) => {
      const context = getCursorContext(e.position.lineNumber)
      emit('cursor-context', context)
    })

    // Listen for selection changes
    editor.onDidChangeCursorSelection((e) => {
      const selection = e.selection
      if (!selection.isEmpty()) {
        const selectedText = editor.getModel().getValueInRange(selection)
        emit('selection-change', selectedText)
      } else {
        emit('selection-change', '')
      }
    })

    // Apply validation errors
    updateMarkers()

  } catch (error) {
    console.error('Failed to load Monaco Editor:', error)
  }
}

// Update editor markers from validation errors
const updateMarkers = () => {
  if (!editor || !monaco) return

  const model = editor.getModel()
  if (!model) return

  // Clear existing markers
  monaco.editor.setModelMarkers(model, 'yaml-validation', [])

  // Add new markers
  const markers = props.validationErrors.map(error => ({
    startLineNumber: error.line || 1,
    startColumn: 1,
    endLineNumber: error.line || 1,
    endColumn: model.getLineMaxColumn(error.line || 1),
    message: error.message,
    severity: error.level === 'warning'
      ? monaco.MarkerSeverity.Warning
      : monaco.MarkerSeverity.Error,
  }))

  monaco.editor.setModelMarkers(model, 'yaml-validation', markers)
}

// Watch for validation errors changes
watch(() => props.validationErrors, () => {
  updateMarkers()
}, { deep: true })

// Watch for value changes from parent
watch(() => props.modelValue, (newValue) => {
  if (editor && editor.getValue() !== newValue) {
    editor.setValue(newValue)
  }
})

// Watch for dark mode changes
watch(isDark, (dark) => {
  if (editor && monaco) {
    monaco.editor.setTheme(dark ? 'trinity-dark' : 'trinity-light')
  }
})

// Copy content to clipboard
const copyContent = async () => {
  if (!editor) return
  await navigator.clipboard.writeText(editor.getValue())
  copied.value = true
  setTimeout(() => {
    copied.value = false
  }, 2000)
}

// Format YAML (basic indentation)
const formatYaml = () => {
  if (!editor) return
  editor.getAction('editor.action.formatDocument')?.run()
}

// Go to specific line
const goToLine = (line) => {
  if (!editor || !line) return
  editor.revealLineInCenter(line)
  editor.setPosition({ lineNumber: line, column: 1 })
  editor.focus()
}

// Determine cursor context by analyzing the YAML structure
const getCursorContext = (lineNumber) => {
  if (!editor) return { path: 'default', line: lineNumber }

  const content = editor.getValue()
  const lines = content.split('\n')

  if (lineNumber > lines.length) return { path: 'default', line: lineNumber }

  // Build context path by looking at indentation levels
  const currentLine = lines[lineNumber - 1] || ''
  const currentIndent = currentLine.search(/\S/)

  // Extract key from current line
  const keyMatch = currentLine.match(/^\s*-?\s*(\w+)\s*:/)
  const currentKey = keyMatch ? keyMatch[1] : null

  // Walk backwards to build full path
  const pathParts = []
  if (currentKey) pathParts.unshift(currentKey)

  let prevIndent = currentIndent
  for (let i = lineNumber - 2; i >= 0; i--) {
    const line = lines[i]
    const indent = line.search(/\S/)

    // Skip empty lines or comments
    if (indent < 0 || line.trim().startsWith('#')) continue

    // If we find a less indented line, it's a parent
    if (indent < prevIndent) {
      const parentMatch = line.match(/^\s*-?\s*(\w+)\s*:/)
      if (parentMatch) {
        pathParts.unshift(parentMatch[1])
        prevIndent = indent
      }
    }

    // Stop at root level
    if (indent === 0 && pathParts.length > 0) break
  }

  return {
    path: pathParts.join('.') || 'default',
    line: lineNumber,
    key: currentKey
  }
}

// Lifecycle
onMounted(() => {
  checkDarkMode()

  // Watch for dark mode changes
  const observer = new MutationObserver((mutations) => {
    mutations.forEach((mutation) => {
      if (mutation.type === 'attributes' && mutation.attributeName === 'class') {
        checkDarkMode()
      }
    })
  })

  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['class'],
  })

  initMonaco()
})

onUnmounted(() => {
  if (editor) {
    editor.dispose()
  }
})

// Expose methods
defineExpose({
  goToLine,
  formatYaml,
  focus: () => editor?.focus(),
})
</script>

<style scoped>
.yaml-editor-container.has-errors :deep(.monaco-editor) {
  border-color: theme('colors.red.300');
}
.dark .yaml-editor-container.has-errors :deep(.monaco-editor) {
  border-color: theme('colors.red.700');
}
</style>
