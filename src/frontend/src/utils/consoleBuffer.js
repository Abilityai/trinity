// Lightweight capped ring buffer of recent console errors/warnings, captured
// early at app bootstrap so the in-app bug reporter (HelpChatWidget) can attach
// "what was on the console" to a report (#1116).
//
// Captured entries are scrubbed for secrets/PII at *report time* (see
// utils/scrub.js) and shown to the user before anything leaves the browser —
// this module only buffers; it never transmits.

const MAX_ENTRIES = 60;
const MAX_LEN = 600;
const buffer = []; // { level, ts, text }
let installed = false;

function push(level, args) {
  try {
    const text = args
      .map((a) => {
        if (a instanceof Error) return `${a.name}: ${a.message}`;
        if (typeof a === 'string') return a;
        try { return JSON.stringify(a); } catch { return String(a); }
      })
      .join(' ')
      .slice(0, MAX_LEN);
    if (!text) return;
    buffer.push({ level, ts: new Date().toISOString(), text });
    if (buffer.length > MAX_ENTRIES) buffer.shift();
  } catch {
    // Never let buffering break the app or the original console call.
  }
}

/**
 * Patch console.error/warn and window error handlers to tee into the ring
 * buffer. Idempotent. Originals are always still called.
 */
export function installConsoleBuffer() {
  if (installed || typeof window === 'undefined') return;
  installed = true;

  for (const level of ['error', 'warn']) {
    const original = console[level]?.bind(console);
    if (!original) continue;
    console[level] = (...args) => {
      push(level, args);
      original(...args);
    };
  }

  window.addEventListener('error', (e) => {
    push('error', [e?.message || 'window error', e?.filename, e?.lineno].filter(Boolean));
  });
  window.addEventListener('unhandledrejection', (e) => {
    const r = e?.reason;
    push('error', [`unhandledrejection: ${r instanceof Error ? r.message : r}`]);
  });
}

/**
 * Snapshot of the most-recent entries as display strings, oldest→newest.
 * @param {number} limit
 * @returns {string[]}
 */
export function getConsoleBuffer(limit = MAX_ENTRIES) {
  return buffer
    .slice(-limit)
    .map((e) => `[${e.ts}] ${e.level.toUpperCase()}: ${e.text}`);
}

/** Test/util helper — clear the buffer. */
export function clearConsoleBuffer() {
  buffer.length = 0;
}
