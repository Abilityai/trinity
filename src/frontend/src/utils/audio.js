/**
 * Audio utilities for voice chat (VOICE-001).
 *
 * Handles microphone capture (PCM 16kHz mono) and audio playback (PCM 24kHz mono)
 * using the Web Audio API. Uses AudioWorklet where available, falls back to
 * deprecated ScriptProcessor for older browsers.
 */

const INPUT_SAMPLE_RATE = 16000
const OUTPUT_SAMPLE_RATE = 24000

// The AudioWorklet processor is a static file under `public/`, loaded from the
// page's own origin. It was inlined into a `blob:` URL, which the CSP's
// `script-src 'self'` (dev and production alike) blocks — every call logged a
// CSP violation and silently fell back to the deprecated ScriptProcessor.
export const MIC_WORKLET_URL = '/mic-capture.worklet.js'

/**
 * Start capturing audio from the microphone.
 * Tries AudioWorklet first, falls back to ScriptProcessor.
 *
 * @param {Function} onData - Called with base64-encoded PCM chunks
 * @returns {{ stop: Function }} Control object
 */
export async function startMicCapture(onData) {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      sampleRate: INPUT_SAMPLE_RATE,
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    }
  })

  const audioContext = new AudioContext({ sampleRate: INPUT_SAMPLE_RATE })
  const source = audioContext.createMediaStreamSource(stream)

  let processorNode = null

  // Try AudioWorklet first (avoids deprecated ScriptProcessor warning)
  let useWorklet = false
  try {
    await audioContext.audioWorklet.addModule(MIC_WORKLET_URL)
    const workletNode = new AudioWorkletNode(audioContext, 'trinity-mic-capture')
    workletNode.port.onmessage = (e) => {
      const base64 = arrayBufferToBase64(float32ToPcm16(e.data).buffer)
      onData(base64)
    }
    source.connect(workletNode)
    processorNode = workletNode
    useWorklet = true
  } catch (_) {
    // Fall back to ScriptProcessor
  }

  if (!useWorklet) {
    const bufferSize = 4096
    const processor = audioContext.createScriptProcessor(bufferSize, 1, 1)
    processor.onaudioprocess = (event) => {
      const float32 = event.inputBuffer.getChannelData(0)
      const base64 = arrayBufferToBase64(float32ToPcm16(float32).buffer)
      onData(base64)
    }
    source.connect(processor)
    processor.connect(audioContext.destination)
    processorNode = processor
  }

  return {
    stop() {
      try { processorNode?.disconnect() } catch (_) {}
      try { source.disconnect() } catch (_) {}
      audioContext.close().catch(() => {})
      stream.getTracks().forEach(t => t.stop())
    }
  }
}

/**
 * Create an audio player for PCM 24kHz mono output with amplitude monitoring.
 *
 * @returns {{ play: Function, getAmplitude: Function, stop: Function }}
 */
export function createAudioPlayer() {
  let audioContext = null
  let nextStartTime = 0
  let analyser = null
  let analyserData = null

  function ensureContext() {
    if (!audioContext || audioContext.state === 'closed') {
      audioContext = new AudioContext({ sampleRate: OUTPUT_SAMPLE_RATE })
      nextStartTime = 0
      analyser = audioContext.createAnalyser()
      analyser.fftSize = 256
      analyser.smoothingTimeConstant = 0.6
      analyserData = new Uint8Array(analyser.frequencyBinCount)
      analyser.connect(audioContext.destination)
    }
    if (audioContext.state === 'suspended') {
      audioContext.resume()
    }
    return audioContext
  }

  return {
    /**
     * Queue a PCM audio chunk for playback.
     * @param {string} base64Data - Base64-encoded PCM 16-bit LE audio
     */
    play(base64Data) {
      const ctx = ensureContext()
      const pcmBytes = base64ToArrayBuffer(base64Data)
      const float32 = pcm16ToFloat32(new Int16Array(pcmBytes))

      const buffer = ctx.createBuffer(1, float32.length, OUTPUT_SAMPLE_RATE)
      buffer.getChannelData(0).set(float32)

      const bufSource = ctx.createBufferSource()
      bufSource.buffer = buffer
      bufSource.connect(analyser)

      const now = ctx.currentTime
      if (nextStartTime < now) nextStartTime = now
      bufSource.start(nextStartTime)
      nextStartTime += buffer.duration
    },

    /**
     * Get current output amplitude as 0–1 float.
     * Returns 0 if no audio is playing.
     */
    getAmplitude() {
      if (!analyser || !analyserData) return 0
      analyser.getByteFrequencyData(analyserData)
      let sum = 0
      for (let i = 0; i < analyserData.length; i++) sum += analyserData[i]
      return sum / (analyserData.length * 255)
    },

    stop() {
      if (audioContext) {
        audioContext.close().catch(() => {})
        audioContext = null
        analyser = null
        analyserData = null
        nextStartTime = 0
      }
    }
  }
}

// ── Conversion helpers ──────────────────────────────────────────────────────

function float32ToPcm16(float32) {
  const pcm16 = new Int16Array(float32.length)
  for (let i = 0; i < float32.length; i++) {
    const s = Math.max(-1, Math.min(1, float32[i]))
    pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF
  }
  return pcm16
}

function pcm16ToFloat32(int16) {
  const float32 = new Float32Array(int16.length)
  for (let i = 0; i < int16.length; i++) {
    float32[i] = int16[i] / (int16[i] < 0 ? 0x8000 : 0x7FFF)
  }
  return float32
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer)
  let binary = ''
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i])
  return btoa(binary)
}

function base64ToArrayBuffer(base64) {
  const binary = atob(base64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
  return bytes.buffer
}
