// Trinity voice chat (VOICE-001): the microphone AudioWorklet processor.
//
// Served as a static file so `audioWorklet.addModule` loads it from the page's
// own origin. It used to be inlined into a `blob:` URL, which the page's
// Content-Security-Policy (`script-src 'self'`, in dev and in production)
// blocks — the console showed "Loading the script 'blob:…' violates the
// following Content Security Policy directive" on every call, and capture
// silently fell back to the deprecated ScriptProcessor path. Keep this file
// dependency-free: it runs on the audio rendering thread.
class MicCapture extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0]?.[0]
    if (ch) this.port.postMessage(ch.slice())
    return true
  }
}
registerProcessor('trinity-mic-capture', MicCapture)
