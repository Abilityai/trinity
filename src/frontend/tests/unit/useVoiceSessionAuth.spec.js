/**
 * trinity-enterprise#595 — the voice composable authenticates its socket with
 * the start response's single-use ticket, sent as the first frame; no session
 * token appears in any URL.
 *
 * The REAL composable is imported (the #2559 lesson: a spec that only reads the
 * source as text could not see what the browser actually sends). The browser
 * edges are scripted: `WebSocket` (tests/unit/helpers/fakeVoiceSocket.js), the
 * mic and player (`utils/audio`), and the platform session — whose token is a
 * sentinel that must never reach the socket.
 *
 * Pinned:
 *   - the URL is exactly the same-origin session path; the first frame is
 *     `{type:'auth', ticket}`; nothing else is sent before `auth_ok`;
 *   - the mic and player start only after `auth_ok`, and `startWith` resolves
 *     true only after the mic is live;
 *   - every failure (unusable response, refusal, close-only, network error,
 *     timeout, mic denied, cancellation) resolves FALSE with words and releases
 *     what it owned; late HTTP / mic / socket events cannot touch a later call;
 *   - the post-admission frames (`task`, `saved`) behave as before.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { FakeVoiceSocket, deferred, flush, settledState } from './helpers/fakeVoiceSocket'

const PLATFORM_TOKEN = 'PLATFORM-JWT-SENTINEL'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({
    token: 'PLATFORM-JWT-SENTINEL',
    authHeader: { Authorization: 'Bearer PLATFORM-JWT-SENTINEL' },
  }),
}))

const audio = vi.hoisted(() => ({ micStarts: [], players: [], nextMic: null }))
vi.mock('@/utils/audio', () => ({
  createAudioPlayer: () => {
    const p = { play: vi.fn(), stop: vi.fn(), getAmplitude: () => 0 }
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

vi.mock('axios', () => ({ default: { post: vi.fn(() => Promise.resolve({ data: {} })) } }))

import {
  useVoiceSession,
  VOICE_CONNECT_TIMEOUT_MS,
  VOICE_TIMEOUT_REASON,
  VOICE_MIC_DENIED_REASON,
  VOICE_CANCELLED_REASON,
  VOICE_BAD_START_REASON,
  SAVED_FRAME_TIMEOUT_MS,
} from '@/composables/useVoiceSession'

const TICKET = 'T'.repeat(20) + '_-' + 'k'.repeat(21) // 43 url-safe chars
const SID = 'vs_AbC-12_xyz'
const START = { voice_session_id: SID, websocket_url: `/ws/voice/${SID}`, websocket_ticket: TICKET,
  websocket_ticket_expires_in: 30, portal_session_id: 'thread-1' }

beforeEach(() => {
  FakeVoiceSocket.reset()
  audio.micStarts = []
  audio.players = []
  audio.nextMic = null
  vi.stubGlobal('WebSocket', FakeVoiceSocket)
  vi.stubGlobal('window', { location: { protocol: 'https:', host: 'app.example.com' }, isSecureContext: true })
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: vi.fn() } })
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function begin(payload = START, opts = { restStop: false }) {
  const voice = useVoiceSession('scribe')
  const request = deferred()
  const started = voice.startWith(() => request.promise, opts)
  return { voice, request, started }
}

async function opened(payload = START) {
  const run = begin(payload)
  run.request.resolve(payload)
  await flush()
  const sock = FakeVoiceSocket.instances.at(-1)
  sock.open()
  return { ...run, sock }
}

async function admitted() {
  const run = await opened()
  run.sock.serverSends({ type: 'auth_ok' })
  await flush()
  return run
}

describe('the socket carries no credential in its URL; the ticket is the first frame', () => {
  it('opens exactly the same-origin session path and authenticates with the response ticket', async () => {
    const { sock, voice, started } = await opened()
    expect(sock.url).toBe(`wss://app.example.com/ws/voice/${SID}`)
    expect(sock.url).not.toContain('?')
    expect(sock.url).not.toContain(PLATFORM_TOKEN)
    expect(sock.sent).toEqual([{ type: 'auth', ticket: TICKET }])
    // Not ready yet: transport open is not authorization.
    expect(audio.micStarts).toEqual([])
    expect(audio.players).toEqual([])
    expect(voice.status.value).toBe('connecting')
    expect(await settledState(started)).toBe('pending')
  })

  it('starts media only after auth_ok, and resolves true only once the mic is live', async () => {
    const { sock, voice, started } = await opened()
    audio.nextMic = deferred()
    const mic = audio.nextMic
    sock.serverSends({ type: 'auth_ok' })
    await flush()
    expect(audio.micStarts.length).toBe(1)
    expect(await settledState(started)).toBe('pending')
    expect(voice.status.value).toBe('connecting')
    mic.resolve()
    expect(await started).toBe(true)
    expect(voice.status.value).toBe('listening')
    // Audio goes out on the same socket, after the auth frame.
    audio.micStarts[0].onData('AAAA')
    expect(sock.sent.at(-1)).toEqual({ type: 'audio', data: 'AAAA' })
  })

  it('ignores provider frames that arrive before admission', async () => {
    const { sock, voice, started } = await opened()
    sock.serverSends({ type: 'status', state: 'listening' })
    sock.serverSends({ type: 'audio', data: 'zz' })
    sock.serverSends({ type: 'transcript', role: 'assistant', text: 'early' })
    await flush()
    expect(voice.status.value).toBe('connecting')
    expect(voice.transcriptEntries.value).toEqual([])
    expect(audio.micStarts).toEqual([])
    expect(await settledState(started)).toBe('pending')
  })
})

describe('every failed start resolves false, with words, and releases what it owned', () => {
  it.each([
    ['no ticket', { ...START, websocket_ticket: undefined }],
    ['a ticket of the wrong shape', { ...START, websocket_ticket: 'short' }],
    ['a credential in the URL', { ...START, websocket_url: `/ws/voice/${SID}?token=x` }],
    ['a fragment', { ...START, websocket_url: `/ws/voice/${SID}#x` }],
    ['another origin', { ...START, websocket_url: `wss://other.example.com/ws/voice/${SID}` }],
    ['another session', { ...START, websocket_url: '/ws/voice/vs_other' }],
    ['an unusable session id', { ...START, voice_session_id: '../x', websocket_url: '/ws/voice/../x' }],
  ])('an unusable start response (%s) opens no socket', async (_name, payload) => {
    const run = begin()
    run.request.resolve(payload)
    await flush()
    // Checked BEFORE awaiting the start: a client that opened the socket anyway
    // fails here by assertion, not by the start hanging until the test times out.
    expect(FakeVoiceSocket.instances).toEqual([])
    expect(await run.started).toBe(false)
    expect(run.voice.error.value).toBe(VOICE_BAD_START_REASON)
    expect(run.voice.isActive.value).toBe(false)
  })

  it('a start request that fails says why, and logs no request object', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    const run = begin()
    run.request.reject({ response: { status: 503, data: { detail: 'Voice is unavailable right now. Try again shortly.' } },
      config: { headers: { Authorization: `Bearer ${PLATFORM_TOKEN}` } } })
    expect(await run.started).toBe(false)
    expect(run.voice.error.value).toBe('Voice is unavailable right now. Try again shortly.')
    expect(FakeVoiceSocket.instances).toEqual([])
    const logged = JSON.stringify([...warn.mock.calls, ...err.mock.calls])
    expect(logged).not.toContain(PLATFORM_TOKEN)
  })

  it('a refusal status before admission fails the start with the server\'s words', async () => {
    const { sock, voice, started } = await opened()
    sock.serverSends({ type: 'status', state: 'ended', reason: 'error',
      message: 'The voice connection expired or could not be verified. Start a new call.' })
    sock.serverCloses(4001)
    expect(await started).toBe(false)
    expect(voice.error.value).toBe('The voice connection expired or could not be verified. Start a new call.')
    expect(audio.micStarts).toEqual([])
    expect(sock.closedByClient).toBe(true)
    expect(voice.isActive.value).toBe(false)
  })

  it.each([[4001], [1006]])('a close with no words (%i) still fails the start with words', async (code) => {
    const { sock, voice, started } = await opened()
    sock.serverCloses(code)
    expect(await settledState(started)).toBe(false)
    expect(voice.error.value).toBeTruthy()
    expect(voice.isActive.value).toBe(false)
  })

  it('a transport error fails the start', async () => {
    const { sock, voice, started } = await opened()
    sock.fail()
    expect(await started).toBe(false)
    expect(voice.error.value).toBe('Voice connection error')
    expect(sock.closedByClient).toBe(true)
  })

  it('connection plus authentication is bounded', async () => {
    vi.useFakeTimers()
    const { sock, voice, started } = await opened()
    vi.advanceTimersByTime(VOICE_CONNECT_TIMEOUT_MS + 1)
    expect(await started).toBe(false)
    expect(voice.error.value).toBe(VOICE_TIMEOUT_REASON)
    expect(sock.closedByClient).toBe(true)
    // A late acknowledgement on the abandoned socket starts nothing.
    sock.serverSends({ type: 'auth_ok' })
    await flush()
    expect(audio.micStarts).toEqual([])
  })

  it('an ended status during mic setup fails the start but keeps the socket for the saved frame', async () => {
    // The call was authorized, so the bridge is closing it out: the transcript
    // write and its `saved` frame are still coming. Settling the start must not
    // close the socket under them, or the Workspace reloads before the rows exist.
    const { sock, voice, started } = await opened()
    audio.nextMic = deferred()
    const mic = audio.nextMic
    sock.serverSends({ type: 'auth_ok' })
    await flush()
    sock.serverSends({ type: 'status', state: 'ended', reason: 'error', message: 'The provider closed the call.' })
    expect(await started).toBe(false)
    expect(voice.error.value).toBe('The provider closed the call.')
    expect(sock.closedByClient).toBe(false)
    expect(voice.isActive.value).toBe(true)                 // still waiting for `saved`
    sock.serverSends({ type: 'saved', messages_saved: 2, duration_seconds: 1 })
    expect(voice.saved.value?.messages_saved).toBe(2)
    expect(voice.isActive.value).toBe(false)
    expect(sock.closedByClient).toBe(true)
    // The mic grant that lands afterwards is stopped at once.
    mic.resolve()
    await flush()
    expect(audio.micStarts[0].stop).toHaveBeenCalled()
  })

  it('an ended status during mic setup with no saved frame still cleans up after the wait', async () => {
    vi.useFakeTimers()
    const { sock, voice, started } = await opened()
    audio.nextMic = deferred()
    sock.serverSends({ type: 'auth_ok' })
    await flush()
    sock.serverSends({ type: 'status', state: 'ended', reason: 'error', message: 'gone' })
    expect(await started).toBe(false)
    expect(voice.isActive.value).toBe(true)
    vi.advanceTimersByTime(SAVED_FRAME_TIMEOUT_MS + 1)
    expect(voice.isActive.value).toBe(false)
    expect(sock.closedByClient).toBe(true)
  })

  it('a denied microphone fails the start and closes the call', async () => {
    const { sock, voice, started } = await opened()
    audio.nextMic = deferred()
    const mic = audio.nextMic
    sock.serverSends({ type: 'auth_ok' })
    await flush()
    mic.reject(new Error('NotAllowedError'))
    expect(await started).toBe(false)
    expect(voice.error.value).toBe(VOICE_MIC_DENIED_REASON)
    expect(sock.sent).toContainEqual({ type: 'end' })
    expect(sock.closedByClient).toBe(true)
    expect(audio.players[0].stop).toHaveBeenCalled()
  })
})

describe('cancellation settles the attempt, and nothing late reaches a later call', () => {
  it('cancelled while the start request is in flight: false, and the late answer opens no socket', async () => {
    const run = begin()
    await flush()
    await run.voice.stop()
    expect(await run.started).toBe(false)
    expect(run.voice.error.value).toBe(VOICE_CANCELLED_REASON)
    run.request.resolve(START)
    await flush()
    expect(FakeVoiceSocket.instances).toEqual([])
  })

  it('cancelled while the socket authenticates: false, socket closed, a late auth_ok starts nothing', async () => {
    const { sock, voice, started } = await opened()
    await voice.stop()
    expect(await started).toBe(false)
    expect(sock.closedByClient).toBe(true)
    sock.serverSends({ type: 'auth_ok' })
    await flush()
    expect(audio.micStarts).toEqual([])
  })

  it('cancelled during the mic prompt: a late grant is stopped at once', async () => {
    const { sock, voice, started } = await opened()
    audio.nextMic = deferred()
    const mic = audio.nextMic
    sock.serverSends({ type: 'auth_ok' })
    await flush()
    await voice.stop()
    expect(await started).toBe(false)
    mic.resolve()
    await flush()
    expect(audio.micStarts[0].stop).toHaveBeenCalled()
    expect(audio.players[0].stop).toHaveBeenCalled()
    expect(voice.status.value).not.toBe('listening')
  })

  it('a stale socket from a failed attempt cannot clean up the next call', async () => {
    const voice = useVoiceSession('scribe')
    const first = voice.startWith(() => Promise.resolve(START), { restStop: false })
    await flush()
    const old = FakeVoiceSocket.instances[0]
    old.open()
    old.serverSends({ type: 'status', state: 'ended', reason: 'error', message: 'no' })
    expect(await first).toBe(false)

    const second = voice.startWith(() => Promise.resolve({ ...START, websocket_ticket: 'n'.repeat(43) }), { restStop: false })
    await flush()
    const fresh = FakeVoiceSocket.instances[1]
    fresh.open()
    expect(fresh.sent).toEqual([{ type: 'auth', ticket: 'n'.repeat(43) }])
    fresh.serverSends({ type: 'auth_ok' })
    expect(await second).toBe(true)

    // The old socket's late events land on nothing.
    old.serverSends({ type: 'saved', messages_saved: 0 })
    old.serverCloses(1006)
    await flush()
    expect(voice.isActive.value).toBe(true)
    expect(voice.status.value).toBe('listening')
    expect(fresh.closedByClient).toBe(false)
  })
})

describe('after admission the call behaves as before', () => {
  it('task frames update the badge and the canvas version; saved ends the call', async () => {
    const { sock, voice, started } = await admitted()
    expect(await started).toBe(true)
    sock.serverSends({ type: 'task', state: 'started', task_id: 't1', label: 'Count PRs', running: 1 })
    expect(voice.backgroundTasks.value.length).toBe(1)
    const before = voice.panelVersion.value
    sock.serverSends({ type: 'task', state: 'finished', task_id: 't1', label: 'Count PRs', running: 0 })
    expect(voice.panelVersion.value).toBe(before + 1)
    sock.serverSends({ type: 'status', state: 'ended', reason: 'cap', message: 'Time is up.' })
    expect(audio.micStarts[0].stop).toHaveBeenCalled()
    expect(voice.isActive.value).toBe(true)                 // waiting for `saved`
    sock.serverSends({ type: 'saved', messages_saved: 3, duration_seconds: 9 })
    expect(voice.saved.value.messages_saved).toBe(3)
    expect(voice.isActive.value).toBe(false)
    expect(voice.endReason.value).toBe('cap')
  })

  it('End sends the end frame and resolves with the saved frame', async () => {
    const { sock, voice } = await admitted()
    const result = voice.stop()
    expect(sock.sent).toContainEqual({ type: 'end' })
    sock.serverSends({ type: 'saved', messages_saved: 1, duration_seconds: 2 })
    expect((await result).messages_saved).toBe(1)
  })
})
