// @vitest-environment jsdom
/**
 * trinity-enterprise#595 — a Direct call's start, between the server's `auth_ok`
 * and the browser's mic grant.
 *
 * The connect timer bounds connection plus authentication only. The time a person
 * spends on the mic-permission prompt is theirs: a start whose prompt stays open past
 * the timer must not fail, or the grant that follows would start a mic for a call
 * that is already inactive, which neither Stop nor unmount would then reach.
 *
 * And a mic grant that lands after the start did fail (the socket closed, or the person
 * pressed Stop, while the prompt was open) is stopped at once, with no polling left
 * behind, the start settled `false`, and a call started since then left alone.
 *
 * The REAL composable; the browser edges (`WebSocket`, mic, player) are scripted.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { mount } from '@vue/test-utils'
import { FakeVoiceSocket, deferred, flush } from './helpers/fakeVoiceSocket'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ token: 'PLATFORM-JWT-SENTINEL', authHeader: {} }),
}))

const audio = vi.hoisted(() => ({ micStarts: [], players: [], nextMic: null }))
vi.mock('@/utils/audio', () => ({
  createAudioPlayer: () => {
    const p = { play: vi.fn(), stop: vi.fn(), pause: vi.fn(), resume: vi.fn(),
      isPlaying: () => false, getLevel: () => 0, getAmplitude: () => 0 }
    audio.players.push(p)
    return p
  },
  startMicCapture: (onData) => {
    const mic = { stop: vi.fn(), onData }
    audio.micStarts.push(mic)
    if (audio.nextMic) {
      const d = audio.nextMic
      audio.nextMic = null
      return d.promise.then(() => mic)
    }
    return Promise.resolve(mic)
  },
}))

vi.mock('@/api', () => ({ default: { post: vi.fn(() => Promise.resolve({ data: {} })) } }))

import { useDirectVoiceSession } from '@/composables/useDirectVoiceSession'
import { VOICE_CONNECT_TIMEOUT_MS, VOICE_TIMEOUT_REASON } from '@/composables/useVoiceSession'

const TICKET = 'D'.repeat(20) + '_-' + 'k'.repeat(21) // 43 url-safe chars
const SID = 'vs_direct-595'
const START = { voice_session_id: SID, websocket_url: `/ws/voice/${SID}`, websocket_ticket: TICKET,
  portal_session_id: 'thread-1', runtime: 'claude-code' }

beforeEach(() => {
  FakeVoiceSocket.reset()
  audio.micStarts = []
  audio.players = []
  audio.nextMic = null
  vi.useFakeTimers()
  vi.stubGlobal('WebSocket', FakeVoiceSocket)
  vi.stubGlobal('window', { location: { protocol: 'https:', host: 'app.example.com' }, isSecureContext: true })
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: vi.fn() } })
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

// Admitted by the server, with the mic-permission prompt still open.
async function promptOpen(call) {
  const request = deferred()
  const mic = deferred()
  audio.nextMic = mic
  const started = call.startWith(() => request.promise)
  request.resolve(START)
  await flush()
  const sock = FakeVoiceSocket.instances.at(-1)
  sock.open()
  expect(sock.sent).toEqual([{ type: 'auth', ticket: TICKET }])
  sock.serverSends({ type: 'auth_ok' })
  await flush()
  expect(audio.micStarts.length).toBe(1)
  return { sock, mic, started }
}

describe('a Direct call whose mic prompt outlasts the connect timer', () => {
  it('still starts when the person allows the mic, and Stop then releases it', async () => {
    const call = useDirectVoiceSession()
    const { sock, mic, started } = await promptOpen(call)

    vi.advanceTimersByTime(VOICE_CONNECT_TIMEOUT_MS * 2)   // the prompt stays open
    mic.resolve()
    await flush()

    void call.stop()                     // resolves on the bridge's `saved` frame
    expect(audio.micStarts[0].stop).toHaveBeenCalledTimes(1)
    expect(sock.sent.at(-1)).toEqual({ type: 'end' })
    expect(await started).toBe(true)
    expect(call.error.value).toBe(null)
  })

  it('still starts, and unmounting the call releases the mic', async () => {
    let call
    const wrapper = mount(defineComponent({
      setup() { call = useDirectVoiceSession(); return () => h('div') },
    }))
    const { mic, started } = await promptOpen(call)

    vi.advanceTimersByTime(VOICE_CONNECT_TIMEOUT_MS * 2)
    mic.resolve()
    await flush()

    wrapper.unmount()
    expect(audio.micStarts[0].stop).toHaveBeenCalledTimes(1)
    expect(await started).toBe(true)
  })
})

describe('a mic grant that lands after the Direct start failed', () => {
  it('is stopped at once, and leaves no polling and no live call behind', async () => {
    const call = useDirectVoiceSession()
    const { sock, mic, started } = await promptOpen(call)

    sock.serverCloses(4001)              // the start fails while the prompt is open
    expect(await started).toBe(false)
    expect(call.error.value).toBe(null)  // admitted, so the close is not a refusal
    expect(call.isActive.value).toBe(false)

    mic.resolve()                        // the person clicks Allow afterwards
    await flush()

    expect(audio.micStarts[0].stop).toHaveBeenCalledTimes(1)
    expect(audio.players.at(-1).stop).toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)   // no amplitude poll, no connect timer
    expect(call.isActive.value).toBe(false)
    expect(call.status.value).toBe('idle')
  })

  it('after Stop during the prompt, the late grant is stopped, the start settles, and a later call is untouched', async () => {
    const call = useDirectVoiceSession()
    const first = await promptOpen(call)

    void call.stop()                     // the person gives up while the prompt is open
    expect(first.sock.sent.at(-1)).toEqual({ type: 'end' })
    first.sock.serverSends({ type: 'saved', messages_saved: 0 })
    expect(call.isActive.value).toBe(false)

    // A new call, fully started, before the first prompt is answered.
    const request = deferred()
    const second = call.startWith(() => request.promise)
    request.resolve(START)
    await flush()
    const sock2 = FakeVoiceSocket.instances.at(-1)
    sock2.open()
    sock2.serverSends({ type: 'auth_ok' })
    await flush()
    expect(await second).toBe(true)

    first.mic.resolve()                  // the first prompt's Allow lands now
    await flush()

    expect(audio.micStarts[0].stop).toHaveBeenCalledTimes(1)
    expect(await first.started).toBe(false)
    expect(audio.micStarts[1].stop).not.toHaveBeenCalled()
    expect(sock2.closedByClient).toBe(false)
    expect(call.isActive.value).toBe(true)
  })

  it('a start that never got `auth_ok` is still bounded by the timer, and starts no mic', async () => {
    const call = useDirectVoiceSession()
    const request = deferred()
    const started = call.startWith(() => request.promise)
    request.resolve(START)
    await flush()
    const sock = FakeVoiceSocket.instances.at(-1)
    sock.open()

    vi.advanceTimersByTime(VOICE_CONNECT_TIMEOUT_MS + 1)
    expect(await started).toBe(false)
    expect(sock.closedByClient).toBe(true)
    sock.serverSends({ type: 'auth_ok' })   // late, on the abandoned socket
    await flush()
    expect(audio.micStarts).toEqual([])
    expect(call.error.value).toBe(VOICE_TIMEOUT_REASON)
  })
})
