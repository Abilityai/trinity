// @vitest-environment jsdom
/**
 * #4456: on a Direct call a pause is not the end of a turn.
 *
 * Before, every pause of 850 ms ended an utterance and its transcript was sent as a
 * whole turn; the next phrase then counted as talking over the agent, cancelled that
 * turn and went out alone. Now what the person says is collected and sent ONCE, after
 * they have been quiet for `GRACE_MS` (`HOLD_UNFINISHED_MS` when the sentence looks
 * unfinished), measured from the end of their last utterance.
 *
 * The composable is driven the way the browser drives it: the endpointer's onset
 * (`_speechStart`), its utterance (`_handleUtterance`) or discard (`_discard`), and the
 * bridge's frames. The clock is faked, so every boundary is asserted to the millisecond.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useDirectVoiceSession } from '../../src/composables/useDirectVoiceSession'
import {
  GRACE_MS, HOLD_LOST_MS, HOLD_UNFINISHED_MS, heldWord, holdDelay, joinHeld,
} from '../../src/components/portal/portalDirectCall'

const STT_MS = 400
const SAMPLES = new Float32Array(16000)

function session(overrides = {}) {
  setActivePinia(createPinia())
  const hooks = { onTranscript: vi.fn(), onBargeIn: vi.fn(), onStop: vi.fn(), onAnswer: vi.fn(), ...overrides }
  const call = useDirectVoiceSession(hooks)
  call.active.value = true
  return { hooks, call }
}
const pass = (ms) => vi.advanceTimersByTime(ms)
const onset = (call, duringPlayback = false) => call._speechStart({ duringPlayback })
/** The person stops talking: the endpointer hands over an utterance (its seq is the next one). */
const endUtterance = (call, duringPlayback = false) => call._handleUtterance({ samples: SAMPLES, duringPlayback })
const hear = (call, seq, text) => call._handleFrame({ type: 'transcript_live', seq, text })
const thinking = (call, id = 'e1') => call._handleFrame({ type: 'speak_state', state: 'thinking', execution_id: id })
const idle = (call, id = 'e1') => call._handleFrame({ type: 'speak_state', state: 'idle', execution_id: id })
/** One whole fragment: spoken, ended, transcribed `STT_MS` later. */
function fragment(call, seq, text) {
  onset(call)
  pass(600)
  endUtterance(call)
  pass(STT_MS)
  hear(call, seq, text)
}
const sent = (hooks) => hooks.onTranscript.mock.calls.map(([text]) => text)

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

describe('the pure rules', () => {
  it('joins fragments in the order they were spoken, not the order they came back', () => {
    expect(joinHeld([{ seq: 2, text: 'portal room file.' }, { seq: 1, text: ' I want to look at the ' }]))
      .toBe('I want to look at the portal room file.')
    expect(joinHeld([])).toBe('')
  })
  it('a finished sentence waits the short quiet period, an unfinished one the long hold', () => {
    expect(holdDelay('Open the file.')).toBe(GRACE_MS)
    expect(holdDelay('I want to look at the.')).toBe(HOLD_UNFINISHED_MS)
    expect(holdDelay('send it to')).toBe(HOLD_UNFINISHED_MS)
    expect(holdDelay('Open the file.', { long: true })).toBe(HOLD_UNFINISHED_MS)
    expect(GRACE_MS).toBeGreaterThanOrEqual(1000)
    expect(GRACE_MS).toBeLessThanOrEqual(1500)
  })
  it.each(['never mind', 'Nevermind.', 'cancel', 'Stop.', 'ok stop'])('"%s" takes collected words back', (t) => {
    expect(heldWord(t)).toBe('drop')
  })
  it.each(['wait', 'Hold on.', 'hang on', 'one sec', 'One second.', 'pause'])('"%s" asks for a moment', (t) => {
    expect(heldWord(t)).toBe('wait')
  })
  it.each(['wait for the tests', 'stop the build', 'be quiet', 'yes', ''])('"%s" is just words', (t) => {
    expect(heldWord(t)).toBeNull()
  })
})

describe('one turn for one thought', () => {
  it('two fragments with a pause between them are ONE turn, in order', () => {
    const { hooks, call } = session()
    fragment(call, 1, 'I want to look at the')
    pass(2000 - STT_MS)                       // a 2 s pause since the first fragment ended
    fragment(call, 2, 'portal room file.')

    pass(GRACE_MS - STT_MS - 1)
    expect(hooks.onTranscript).not.toHaveBeenCalled()
    pass(1)
    expect(sent(hooks)).toEqual(['I want to look at the portal room file.'])
    expect(hooks.onBargeIn).not.toHaveBeenCalled()
  })

  it('a fragment that LOOKS finished still waits: the provider punctuates nearly everything', () => {
    const { hooks, call } = session()
    fragment(call, 1, 'I want to open the portal.')
    pass(GRACE_MS - STT_MS - 100)             // they carry on just inside the quiet period
    fragment(call, 2, 'And the room file.')
    pass(GRACE_MS - STT_MS)
    expect(sent(hooks)).toEqual(['I want to open the portal. And the room file.'])
  })

  it('the quiet period is counted from the end of speech, so transcription runs inside it', () => {
    const { hooks, call } = session()
    onset(call); pass(500); endUtterance(call)
    pass(STT_MS)
    hear(call, 1, 'What time is it?')
    pass(GRACE_MS - STT_MS - 1)
    expect(hooks.onTranscript).not.toHaveBeenCalled()
    pass(1)
    expect(sent(hooks)).toEqual(['What time is it?'])
  })

  it('a transcript slower than the quiet period is sent as it lands, not a period later', () => {
    const { hooks, call } = session()
    onset(call); pass(500); endUtterance(call)
    pass(GRACE_MS + 700)
    hear(call, 1, 'What time is it?')
    pass(0)
    expect(sent(hooks)).toEqual(['What time is it?'])
  })
})

describe('more speech keeps what was collected', () => {
  it('a start of speech cancels the pending send; nothing goes while they talk', () => {
    const { hooks, call } = session()
    fragment(call, 1, 'Open the report.')
    pass(GRACE_MS - STT_MS - 1)
    onset(call)
    pass(20000)                               // a long second phrase
    expect(hooks.onTranscript).not.toHaveBeenCalled()
    endUtterance(call)
    pass(STT_MS)
    hear(call, 2, 'And the appendix.')
    pass(GRACE_MS - STT_MS)
    expect(sent(hooks)).toEqual(['Open the report. And the appendix.'])
  })

  it('they started again before the first transcript arrived: it is kept, not sent', () => {
    const { hooks, call } = session()
    onset(call); pass(500); endUtterance(call)
    pass(200)
    onset(call)                               // speaking again, transcript 1 still in flight
    pass(300)
    hear(call, 1, 'I want to look at the.')
    pass(HOLD_UNFINISHED_MS + GRACE_MS)
    expect(hooks.onTranscript).not.toHaveBeenCalled()
    endUtterance(call)
    pass(STT_MS)
    hear(call, 2, 'Portal room file.')
    pass(GRACE_MS - STT_MS)
    expect(sent(hooks)).toEqual(['I want to look at the. Portal room file.'])
  })

  it('nothing is sent while a later transcript is still on its way, however slow', () => {
    const { hooks, call } = session()
    fragment(call, 1, 'Open the report.')
    onset(call); pass(500); endUtterance(call)
    pass(GRACE_MS + 2000)                     // transcript 2 is slow
    expect(hooks.onTranscript).not.toHaveBeenCalled()
    hear(call, 2, 'And the appendix.')
    pass(0)
    expect(sent(hooks)).toEqual(['Open the report. And the appendix.'])
  })

  it('transcripts that come back out of order are sent in spoken order', () => {
    const { hooks, call } = session()
    onset(call); pass(3000); endUtterance(call)       // a long first fragment…
    onset(call); pass(300); endUtterance(call)        // …and a short second one
    pass(200)
    hear(call, 2, 'room file.')
    pass(GRACE_MS)
    expect(hooks.onTranscript).not.toHaveBeenCalled() // fragment 1 is still being transcribed
    hear(call, 1, 'I want to look at the portal')
    pass(0)
    expect(sent(hooks)).toEqual(['I want to look at the portal room file.'])
  })

  it('a continuation inside the quiet period never counts as talking over the agent', () => {
    const { hooks, call } = session()
    fragment(call, 1, 'Summarise the report.')
    fragment(call, 2, 'In three bullets.')
    expect(hooks.onBargeIn).not.toHaveBeenCalled()
    expect(hooks.onStop).not.toHaveBeenCalled()
    pass(GRACE_MS)
    expect(sent(hooks)).toEqual(['Summarise the report. In three bullets.'])
  })
})

describe('words are never stranded', () => {
  it('noise that was not speech resumes the wait where it was; it does not restart it', () => {
    const { hooks, call } = session()
    fragment(call, 1, 'Open the report.')     // due GRACE_MS after the utterance ended
    pass(300)
    onset(call)                               // a cough
    pass(200)
    call._discard({ duringPlayback: false })
    pass(GRACE_MS - STT_MS - 500 - 1)
    expect(hooks.onTranscript).not.toHaveBeenCalled()
    pass(1)
    expect(sent(hooks)).toEqual(['Open the report.'])
  })

  it('noise that outlasts the quiet period sends as soon as it is known to be noise', () => {
    const { hooks, call } = session()
    fragment(call, 1, 'Open the report.')
    onset(call)
    pass(GRACE_MS + 500)
    expect(hooks.onTranscript).not.toHaveBeenCalled()
    call._discard({ duringPlayback: false })
    pass(0)
    expect(sent(hooks)).toEqual(['Open the report.'])
  })

  it('a transcript that never arrives does not hold the rest for ever', () => {
    const { hooks, call } = session()
    fragment(call, 1, 'Open the report.')
    onset(call); pass(500); endUtterance(call)        // utterance 2: its transcript is lost
    pass(HOLD_LOST_MS - 1)
    expect(hooks.onTranscript).not.toHaveBeenCalled()
    pass(1)
    expect(sent(hooks)).toEqual(['Open the report.'])
  })

  it('an utterance the bridge could not hear releases what was collected', () => {
    const { hooks, call } = session()
    fragment(call, 1, 'Open the report.')
    onset(call); pass(500); endUtterance(call)
    pass(STT_MS)
    call._handleFrame({ type: 'not_heard', seq: 2, reason: 'empty' })
    pass(GRACE_MS - STT_MS - 1)
    expect(hooks.onTranscript).not.toHaveBeenCalled()
    pass(1)
    expect(sent(hooks)).toEqual(['Open the report.'])
  })

  it('muting mid-phrase ends that phrase: the words collected so far still go', () => {
    const { hooks, call } = session()
    fragment(call, 1, 'Open the report.')
    onset(call)
    pass(200)
    call.toggleMute()
    pass(GRACE_MS)
    expect(sent(hooks)).toEqual(['Open the report.'])
  })

  it('a spoken yes to a waiting approval answers at once, and the collected words still go', () => {
    const { hooks, call } = session()
    call.promptAsk({ id: 'a1', kind: 'approval', options: ['Approve', 'Reject'] })
    call._handleFrame({ type: 'spoken', text: 'Approve merge? Yes or no.', ask: true })
    call._promptPlayed()
    pass(1000)
    fragment(call, 1, 'Open the report.')
    onset(call); pass(300); endUtterance(call)
    pass(100)
    hear(call, 2, 'Yes.')
    expect(hooks.onAnswer).toHaveBeenCalledWith(expect.objectContaining({ id: 'a1' }), 'yes')   // no wait
    expect(hooks.onTranscript).not.toHaveBeenCalled()
    pass(GRACE_MS)
    expect(sent(hooks)).toEqual(['Open the report.'])
  })
})

describe('carrying on after the words were already sent', () => {
  it('before the agent said anything: the turn is stopped and BOTH halves go again as one', () => {
    const { hooks, call } = session()
    fragment(call, 1, 'I want to open the portal.')
    pass(GRACE_MS)
    expect(sent(hooks)).toEqual(['I want to open the portal.'])
    call.speak('e1')                                   // the conversation started the turn
    fragment(call, 2, 'And the room file.')
    expect(hooks.onBargeIn).toHaveBeenCalledWith({ executionId: 'e1' })
    pass(GRACE_MS)
    expect(sent(hooks)).toEqual(['I want to open the portal.', 'I want to open the portal. And the room file.'])
  })

  it('while the first half is still being dispatched: the same', () => {
    let busy = false
    const { hooks, call } = session({ isBusy: () => busy })
    fragment(call, 1, 'I want to open the portal.')
    pass(GRACE_MS)
    busy = true                                        // sent, its execution not started yet
    fragment(call, 2, 'And the room file.')
    expect(hooks.onBargeIn).toHaveBeenCalledTimes(1)
    busy = false
    pass(GRACE_MS)
    expect(sent(hooks).at(-1)).toBe('I want to open the portal. And the room file.')
  })

  it('once the agent has started to answer, new words are talking over it and go alone', () => {
    const { hooks, call } = session()
    fragment(call, 1, 'I want to open the portal.')
    pass(GRACE_MS)
    call.speak('e1')
    call._handleFrame({ type: 'audio', data: 'AAE=' })
    fragment(call, 2, 'Actually, the room file.')
    expect(hooks.onBargeIn).toHaveBeenCalledTimes(1)
    pass(GRACE_MS)
    expect(sent(hooks).at(-1)).toBe('Actually, the room file.')
  })

  it('a send that never became a turn is not carried into a later one', () => {
    // The dispatch failed: no `thinking`, no `idle`. Much later another turn (a typed
    // one) is running when the person speaks. The stale words must not ride along.
    const { hooks, call } = session()
    fragment(call, 1, 'Open the portal.')
    pass(GRACE_MS)
    pass(60000)
    call.speak('typed-turn')
    fragment(call, 2, 'Stop that and list the files.')
    expect(hooks.onBargeIn).toHaveBeenCalledTimes(1)
    pass(GRACE_MS)
    expect(sent(hooks).at(-1)).toBe('Stop that and list the files.')
  })

  it('once the agent has started a tool, new words go alone: the action is not run twice', () => {
    const { hooks, call } = session()
    fragment(call, 1, 'Delete the draft.')
    pass(GRACE_MS)
    call.speak('e1')
    call._handleFrame({ type: 'activity', tool: 'Deleting draft.md' })
    fragment(call, 2, 'And email Bob.')
    expect(hooks.onBargeIn).toHaveBeenCalledTimes(1)
    pass(GRACE_MS)
    expect(sent(hooks).at(-1)).toBe('And email Bob.')
  })

  it('words the conversation only queued are not a turn to supersede: its queue joins them', () => {
    // A turn in front is still settling, so the conversation parks the words. The next
    // phrase must not cancel anything, and must not carry them a second time.
    const { hooks, call } = session({ onTranscript: vi.fn(() => 'queued'), isBusy: () => true })
    fragment(call, 1, 'Delete the draft.')
    pass(GRACE_MS)
    fragment(call, 2, 'And the notes.')
    expect(hooks.onBargeIn).not.toHaveBeenCalled()
    pass(GRACE_MS)
    expect(sent(hooks)).toEqual(['Delete the draft.', 'And the notes.'])
  })

  it('a dispatch the conversation reports as failed is forgotten', () => {
    let busy = true
    const { hooks, call } = session({ isBusy: () => busy })
    fragment(call, 1, 'Open the portal.')
    pass(GRACE_MS)
    call.forgetSent()
    fragment(call, 2, 'List the files.')               // a typed turn is being sent meanwhile
    expect(hooks.onBargeIn).not.toHaveBeenCalled()
    pass(GRACE_MS)
    expect(sent(hooks).at(-1)).toBe('List the files.')
  })

  it('a run the bridge starts on its own (an approved run) does not count as the sent turn starting', () => {
    const { hooks, call } = session()
    fragment(call, 1, 'Open the portal.')
    pass(GRACE_MS)
    pass(60000)                                        // its dispatch never became a turn
    thinking(call, 'approved-run')
    fragment(call, 2, 'Stop that and list the files.')
    pass(GRACE_MS)
    expect(sent(hooks).at(-1)).toBe('Stop that and list the files.')
  })

  it('a turn that finished is not carried into the next thing said', () => {
    const { hooks, call } = session()
    fragment(call, 1, 'Open the portal.')
    pass(GRACE_MS)
    call.speak('e1')
    idle(call)
    thinking(call, 'approved-run')                     // some other run starts
    fragment(call, 2, 'Stop that and list the files.')
    pass(GRACE_MS)
    expect(sent(hooks).at(-1)).toBe('Stop that and list the files.')
  })
})

describe('taking words back, and asking for a moment', () => {
  it('"never mind" with words collected and nothing running drops them, quietly', () => {
    const { hooks, call } = session()
    fragment(call, 1, 'Deploy it to production.')
    fragment(call, 2, 'Never mind.')
    pass(HOLD_LOST_MS)
    expect(hooks.onTranscript).not.toHaveBeenCalled()
    expect(hooks.onStop).not.toHaveBeenCalled()         // nothing was stopped
    expect(call.notice.value).toBe('')
    expect(call.liveText.value).toBe('')
  })

  it('"hold on" is not sent, and the sentence waits the long hold for the rest', () => {
    const { hooks, call } = session()
    fragment(call, 1, 'Send the report to Alice.')
    fragment(call, 2, 'Hold on.')
    pass(HOLD_UNFINISHED_MS - STT_MS - 1)
    expect(hooks.onTranscript).not.toHaveBeenCalled()
    pass(1)
    expect(sent(hooks)).toEqual(['Send the report to Alice.'])
  })

  it('with nothing collected, the same words are words for the agent', () => {
    const { hooks, call } = session()
    fragment(call, 1, 'Wait.')
    pass(GRACE_MS)
    expect(sent(hooks)).toEqual(['Wait.'])
  })

  it('a stop word while the agent runs still stops it at once and drops what was collected', () => {
    const { hooks, call } = session()
    thinking(call)
    fragment(call, 1, 'Also check the budget.')          // talks over: the turn is stopped
    thinking(call, 'e2')
    fragment(call, 2, 'Stop.')
    expect(hooks.onStop).toHaveBeenCalledTimes(1)       // no advance: immediate
    pass(HOLD_LOST_MS)
    expect(hooks.onTranscript).not.toHaveBeenCalled()
  })
})

describe('what the call shows', () => {
  it('"Got it" after a pause goes back to listening the moment they speak again', () => {
    const { call } = session()
    onset(call); pass(500); endUtterance(call)
    expect(call.status.value).toBe('transcribing')
    onset(call)
    expect(call.status.value).toBe('listening')
  })

  it('a transcript landing while they are still speaking leaves it on listening', () => {
    const { call } = session()
    onset(call); pass(500); endUtterance(call)
    onset(call)
    hear(call, 1, 'I want to look at the')
    expect(call.status.value).toBe('listening')
  })

  it('shows the whole sentence collected so far, not only the last fragment', () => {
    const { call } = session()
    fragment(call, 1, 'I want to look at the')
    fragment(call, 2, 'portal room file.')
    expect(call.liveText.value).toBe('I want to look at the portal room file.')
  })
})

describe('ending the call', () => {
  it('ending it sends what was collected: the person said it', async () => {
    const { hooks, call } = session()
    fragment(call, 1, 'Open the report.')
    await call.stop()
    expect(sent(hooks)).toEqual(['Open the report.'])
    pass(HOLD_LOST_MS)
    expect(hooks.onTranscript).toHaveBeenCalledTimes(1)
  })

  it('ending it because they left for another agent or thread sends nothing', async () => {
    const { hooks, call } = session()
    fragment(call, 1, 'Open the report.')
    await call.stop({ flush: false })
    pass(HOLD_LOST_MS)
    expect(hooks.onTranscript).not.toHaveBeenCalled()
  })

  it('a call the server ended sends nothing afterwards', () => {
    const { hooks, call } = session()
    fragment(call, 1, 'Open the report.')
    call._handleFrame({ type: 'status', state: 'ended', reason: 'cap' })
    pass(HOLD_LOST_MS)
    expect(hooks.onTranscript).not.toHaveBeenCalled()
  })

  it('a transcript still on the wire when the call ended starts nothing', async () => {
    const { hooks, call } = session()
    onset(call); pass(500); endUtterance(call)
    void call.stop({ flush: false })
    call.status.value = 'ended'
    hear(call, 1, 'Delete the draft.')
    pass(HOLD_LOST_MS)
    expect(hooks.onTranscript).not.toHaveBeenCalled()
  })

  it('Stop takes back what was collected', () => {
    const { hooks, call } = session()
    fragment(call, 1, 'Open the report.')
    call.interrupt()
    pass(HOLD_LOST_MS)
    expect(hooks.onTranscript).not.toHaveBeenCalled()
  })
})
