/**
 * A scriptable stand-in for the browser `WebSocket`, for the voice composable
 * (trinity-enterprise#595). Records the URL it was opened with and every frame
 * the client sent; the test plays the server side (`open`, `serverSends`,
 * `serverCloses`, `fail`).
 */
export class FakeVoiceSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  static instances = []

  constructor(url) {
    this.url = url
    this.readyState = FakeVoiceSocket.CONNECTING
    this.sent = []
    this.closedByClient = false
    this.onopen = null
    this.onmessage = null
    this.onclose = null
    this.onerror = null
    FakeVoiceSocket.instances.push(this)
  }

  send(data) {
    if (this.readyState !== FakeVoiceSocket.OPEN) throw new Error('not open')
    this.sent.push(JSON.parse(data))
  }

  close() {
    this.closedByClient = true
    this.readyState = FakeVoiceSocket.CLOSED
  }

  // ---- the server side ----
  open() {
    this.readyState = FakeVoiceSocket.OPEN
    this.onopen?.()
  }

  serverSends(frame) {
    this.onmessage?.({ data: JSON.stringify(frame) })
  }

  serverCloses(code = 1006) {
    this.readyState = FakeVoiceSocket.CLOSED
    this.onclose?.({ code })
  }

  fail() {
    this.onerror?.(new Event('error'))
  }

  static reset() {
    FakeVoiceSocket.instances = []
  }
}

/** A deferred: a promise plus the hands that settle it. */
export function deferred() {
  let resolve, reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

/** Let queued microtasks run (a few rounds, for chained awaits). */
export async function flush(rounds = 5) {
  for (let i = 0; i < rounds; i++) await Promise.resolve()
}

/** Resolve-state of a promise without awaiting it forever. */
export async function settledState(promise) {
  const PENDING = Symbol('pending')
  const v = await Promise.race([promise, flush(10).then(() => PENDING)])
  return v === PENDING ? 'pending' : v
}
