/* Brain Orb voice — mic capture worklet (#58 Phase 3, trinity-enterprise#60).
 * Shipped as a same-origin file (not a blob: URL) so it loads under prod CSP
 * script-src 'self' — a blob: worklet would be blocked. Forwards each raw
 * Float32 mic frame to the main thread, which downsamples to 16k PCM. */
class MicCapture extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) this.port.postMessage(ch.slice());
    return true;
  }
}
registerProcessor('mic-capture', MicCapture);
