<template>
  <div class="terminal-wrapper h-full flex flex-col">
    <!-- Header -->
    <div class="flex items-center justify-between px-4 py-2 bg-gray-800 border-b border-gray-700">
      <div class="flex items-center space-x-3">
        <div :class="[
          'w-2 h-2 rounded-full',
          connectionStatus === 'connected' ? 'bg-green-500' :
          connectionStatus === 'connecting' ? 'bg-yellow-500 animate-pulse' :
          'bg-red-500'
        ]"></div>
        <span class="text-sm text-gray-300">{{ connectionStatusText }}</span>
      </div>
      <div class="flex items-center space-x-2">
        <!-- Mode Toggle -->
        <div class="flex bg-gray-700 rounded-lg p-0.5">
          <button
            @click="selectedMode = 'claude'"
            :class="[
              'px-3 py-1 text-xs font-medium rounded-md transition-colors',
              selectedMode === 'claude'
                ? 'bg-blue-600 text-white'
                : 'text-gray-400 hover:text-gray-200'
            ]"
            :disabled="connectionStatus === 'connected'"
          >
            Claude Code
          </button>
          <button
            @click="selectedMode = 'bash'"
            :class="[
              'px-3 py-1 text-xs font-medium rounded-md transition-colors',
              selectedMode === 'bash'
                ? 'bg-blue-600 text-white'
                : 'text-gray-400 hover:text-gray-200'
            ]"
            :disabled="connectionStatus === 'connected'"
          >
            Bash
          </button>
        </div>
        <!-- Fullscreen Toggle -->
        <button
          v-if="showFullscreenToggle"
          @click="$emit('toggle-fullscreen')"
          class="p-1.5 text-gray-400 hover:text-gray-200 rounded transition-colors"
          :title="isFullscreen ? 'Exit Fullscreen (Esc)' : 'Fullscreen'"
        >
          <!-- Expand icon -->
          <svg v-if="!isFullscreen" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5l-5-5m5 5v-4m0 4h-4" />
          </svg>
          <!-- Collapse icon -->
          <svg v-else class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 9V4.5M9 9H4.5M9 9L3.75 3.75M9 15v4.5M9 15H4.5M9 15l-5.25 5.25M15 9h4.5M15 9V4.5M15 9l5.25-5.25M15 15h4.5M15 15v4.5m0-4.5l5.25 5.25" />
          </svg>
        </button>
        <!-- Reconnect Button -->
        <button
          v-if="connectionStatus === 'disconnected'"
          @click="connect"
          class="px-3 py-1 text-xs font-medium bg-green-600 hover:bg-green-700 text-white rounded-lg transition-colors"
        >
          Connect
        </button>
        <!-- Disconnect Button -->
        <button
          v-else-if="connectionStatus === 'connected'"
          @click="disconnect"
          class="px-3 py-1 text-xs font-medium bg-red-600 hover:bg-red-700 text-white rounded-lg transition-colors"
        >
          Disconnect
        </button>
      </div>
    </div>

    <!-- Terminal Container -->
    <div
      ref="terminalContainer"
      class="flex-1 bg-[#1e1e1e] overflow-hidden"
    ></div>

    <!-- Error Message -->
    <div
      v-if="errorMessage"
      class="px-4 py-2 bg-red-900/50 border-t border-red-800 text-red-300 text-sm"
    >
      {{ errorMessage }}
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import { WebglAddon } from '@xterm/addon-webgl'
import { CanvasAddon } from '@xterm/addon-canvas'
import '@xterm/xterm/css/xterm.css'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  },
  autoConnect: {
    type: Boolean,
    default: true
  },
  showFullscreenToggle: {
    type: Boolean,
    default: false
  },
  isFullscreen: {
    type: Boolean,
    default: false
  }
})

const emit = defineEmits(['connected', 'disconnected', 'error', 'toggle-fullscreen'])

// Refs
const terminalContainer = ref(null)
const selectedMode = ref('claude')
const connectionStatus = ref('disconnected')
const errorMessage = ref('')

// Terminal and WebSocket instances
let terminal = null
let fitAddon = null
let webglAddon = null
let canvasAddon = null
let ws = null
let resizeObserver = null
let resizeTimeout = null

// Computed
const connectionStatusText = computed(() => ({
  'connected': 'Connected',
  'connecting': 'Connecting...',
  'disconnected': 'Disconnected'
})[connectionStatus.value] || 'Disconnected')

// Direct write - simpler and more reliable than batching
function writeToTerminal(data) {
  if (terminal) {
    terminal.write(data)
  }
}

// Debounced resize to avoid spamming server
function debouncedResize() {
  if (resizeTimeout) {
    clearTimeout(resizeTimeout)
  }
  resizeTimeout = setTimeout(() => {
    if (fitAddon && terminal) {
      fitAddon.fit()
      // Send resize event to server
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
          type: 'resize',
          cols: terminal.cols,
          rows: terminal.rows
        }))
      }
    }
  }, 150)
}

// Load WebGL renderer with canvas fallback
function loadRenderer() {
  if (!terminal) return

  try {
    webglAddon = new WebglAddon()
    webglAddon.onContextLoss(() => {
      console.warn('WebGL context lost, falling back to canvas')
      webglAddon?.dispose()
      webglAddon = null
      loadCanvasFallback()
    })
    terminal.loadAddon(webglAddon)
    console.log('Terminal using WebGL renderer')
  } catch (e) {
    console.warn('WebGL not available, using canvas fallback:', e.message)
    loadCanvasFallback()
  }
}

function loadCanvasFallback() {
  if (!terminal) return
  try {
    canvasAddon = new CanvasAddon()
    terminal.loadAddon(canvasAddon)
    console.log('Terminal using Canvas renderer')
  } catch (e) {
    console.warn('Canvas addon failed, using DOM renderer:', e.message)
  }
}

// Initialize terminal
function initTerminal() {
  // Clean up existing instances
  if (webglAddon) {
    webglAddon.dispose()
    webglAddon = null
  }
  if (canvasAddon) {
    canvasAddon.dispose()
    canvasAddon = null
  }
  if (terminal) {
    terminal.dispose()
  }

  terminal = new Terminal({
    cursorBlink: true,
    fontSize: 14,
    fontFamily: 'Menlo, Monaco, "Courier New", monospace',
    theme: {
      background: '#1e1e1e',
      foreground: '#d4d4d4',
      cursor: '#d4d4d4',
      cursorAccent: '#1e1e1e',
      selection: 'rgba(255, 255, 255, 0.3)',
      black: '#000000',
      red: '#cd3131',
      green: '#0dbc79',
      yellow: '#e5e510',
      blue: '#2472c8',
      magenta: '#bc3fbc',
      cyan: '#11a8cd',
      white: '#e5e5e5',
      brightBlack: '#666666',
      brightRed: '#f14c4c',
      brightGreen: '#23d18b',
      brightYellow: '#f5f543',
      brightBlue: '#3b8eea',
      brightMagenta: '#d670d6',
      brightCyan: '#29b8db',
      brightWhite: '#e5e5e5'
    },
    allowProposedApi: true,
    scrollback: 10000,
    // Performance optimizations
    fastScrollModifier: 'alt',
    fastScrollSensitivity: 5,
    smoothScrollDuration: 0
  })

  fitAddon = new FitAddon()
  terminal.loadAddon(fitAddon)
  terminal.loadAddon(new WebLinksAddon())

  terminal.open(terminalContainer.value)

  // Load WebGL/Canvas renderer for GPU acceleration
  loadRenderer()

  // Fit after a short delay to ensure container is fully rendered
  nextTick(() => {
    setTimeout(() => {
      fitAddon.fit()
    }, 50)
  })

  // Handle user input
  terminal.onData(data => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(data)
    }
  })

  // Set up resize observer with debouncing
  if (resizeObserver) {
    resizeObserver.disconnect()
  }

  resizeObserver = new ResizeObserver(() => {
    debouncedResize()
  })
  resizeObserver.observe(terminalContainer.value)
}

// Connect to WebSocket
function connect() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    return
  }

  connectionStatus.value = 'connecting'
  errorMessage.value = ''

  // Initialize terminal if not already done
  if (!terminal) {
    initTerminal()
  }

  // Clear terminal
  terminal.clear()
  terminal.write(`\x1b[33mConnecting to ${props.agentName}...\x1b[0m\r\n`)

  // Get token from localStorage
  const token = localStorage.getItem('token')
  if (!token) {
    errorMessage.value = 'Not authenticated. Please log in.'
    connectionStatus.value = 'disconnected'
    terminal.write('\x1b[31mError: Not authenticated\x1b[0m\r\n')
    return
  }

  // Build WebSocket URL - use agent-specific terminal endpoint
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const wsUrl = `${protocol}//${location.host}/api/agents/${encodeURIComponent(props.agentName)}/terminal?mode=${selectedMode.value}`

  try {
    ws = new WebSocket(wsUrl)
    ws.binaryType = 'arraybuffer'

    ws.onopen = () => {
      // Send auth message first
      ws.send(JSON.stringify({ type: 'auth', token }))
    }

    ws.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        // Binary data - terminal output
        writeToTerminal(new Uint8Array(event.data))
      } else {
        // JSON control message
        try {
          const msg = JSON.parse(event.data)
          if (msg.type === 'auth_success') {
            connectionStatus.value = 'connected'
            emit('connected')
            terminal.write('\x1b[32mConnected!\x1b[0m\r\n\r\n')
          } else if (msg.type === 'error') {
            terminal.write(`\r\n\x1b[31mError: ${msg.message}\x1b[0m\r\n`)
            errorMessage.value = msg.message
            if (msg.close) {
              ws.close()
            }
          }
        } catch (e) {
          // Not JSON, treat as text output
          writeToTerminal(event.data)
        }
      }
    }

    ws.onclose = (event) => {
      connectionStatus.value = 'disconnected'
      emit('disconnected')
      if (event.code !== 1000) {
        terminal.write('\r\n\x1b[33mConnection closed.\x1b[0m\r\n')
        if (event.reason) {
          terminal.write(`\x1b[33mReason: ${event.reason}\x1b[0m\r\n`)
        }
      }
    }

    ws.onerror = (error) => {
      console.error('WebSocket error:', error)
      errorMessage.value = 'Connection error. Please try again.'
      terminal.write('\r\n\x1b[31mConnection error\x1b[0m\r\n')
    }
  } catch (error) {
    console.error('Failed to create WebSocket:', error)
    errorMessage.value = 'Failed to connect: ' + error.message
    connectionStatus.value = 'disconnected'
  }
}

// Disconnect
function disconnect() {
  if (ws) {
    ws.close(1000, 'User disconnected')
    ws = null
  }
  connectionStatus.value = 'disconnected'
}

// Focus terminal
function focus() {
  if (terminal) {
    terminal.focus()
  }
}

// Fit terminal to container
function fit() {
  if (fitAddon) {
    fitAddon.fit()
  }
}

// Watch for mode changes
watch(selectedMode, () => {
  // Only allow mode change when disconnected
  if (connectionStatus.value === 'connected') {
    return
  }
})

// Watch for fullscreen changes to refit terminal
watch(() => props.isFullscreen, () => {
  nextTick(() => {
    if (fitAddon) {
      setTimeout(() => fitAddon.fit(), 50)
    }
  })
})

// Lifecycle
onMounted(() => {
  initTerminal()
  if (props.autoConnect) {
    // Small delay to ensure everything is rendered
    setTimeout(connect, 100)
  }
})

onBeforeUnmount(() => {
  // Close WebSocket first
  if (ws) {
    try {
      ws.close()
    } catch (e) {
      console.warn('Error closing WebSocket:', e)
    }
    ws = null
  }

  // Disconnect resize observer
  if (resizeObserver) {
    try {
      resizeObserver.disconnect()
    } catch (e) {
      console.warn('Error disconnecting resize observer:', e)
    }
    resizeObserver = null
  }

  if (resizeTimeout) {
    clearTimeout(resizeTimeout)
    resizeTimeout = null
  }

  // Dispose addons before terminal (order matters)
  // Wrap in try-catch to prevent errors from blocking unmount
  if (webglAddon) {
    try {
      webglAddon.dispose()
    } catch (e) {
      console.warn('Error disposing WebGL addon:', e)
    }
    webglAddon = null
  }

  if (canvasAddon) {
    try {
      canvasAddon.dispose()
    } catch (e) {
      console.warn('Error disposing Canvas addon:', e)
    }
    canvasAddon = null
  }

  // Dispose terminal last
  if (terminal) {
    try {
      terminal.dispose()
    } catch (e) {
      console.warn('Error disposing terminal:', e)
    }
    terminal = null
  }
})

// Expose methods
defineExpose({
  connect,
  disconnect,
  focus,
  fit
})
</script>

<style scoped>
.terminal-wrapper {
  min-height: 400px;
}

/* Ensure terminal fills container properly */
:deep(.xterm) {
  height: 100%;
  padding: 8px;
}

:deep(.xterm-viewport) {
  overflow-y: auto !important;
}

:deep(.xterm-screen) {
  height: 100%;
}
</style>
