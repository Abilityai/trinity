/**
 * #2157 — the Workspace speaker choice survives a reload.
 *
 * Narration was doubly hard to find: the toggle reset to off on every page load,
 * and (before this issue) never rendered at all for an agent riding the platform
 * default voice — so the only signal it existed was an agent claiming the
 * opposite ("this surface is text-only, reach me on Slack"). Persisting the
 * choice is the UX half of that fix.
 *
 * There is no component-mount harness in this project (no @vue/test-utils), so
 * this is a source-structure guard in the shape of `workspaceRoomsGate.spec.js`:
 * comments are stripped first, since a comment explaining the rule necessarily
 * contains the strings a text scan looks for.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

const SRC = readFileSync(
  fileURLToPath(new URL('../../src/components/portal/PortalConversation.vue', import.meta.url)),
  'utf8'
)

// Strip block + line comments so the guards scan code, not prose about the code.
const CODE = SRC.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

describe('#2157 portal voice-mode persistence', () => {
  it('reads the remembered choice instead of hard-coding off', () => {
    expect(CODE).toContain('const voiceMode = ref(loadVoiceMode())')
    expect(CODE).not.toContain('const voiceMode = ref(false)')
  })

  it('keys the memory per agent, so one agent\'s choice does not speak for another', () => {
    expect(CODE).toMatch(/voiceModeKey\s*=\s*computed\(\(\)\s*=>\s*`trinity\.portal\.voiceMode\./)
    expect(CODE).toContain('props.agent?.name')
  })

  it('writes on change and re-reads when the agent switches', () => {
    expect(CODE).toMatch(/watch\(voiceMode,[\s\S]{0,200}setItem\(voiceModeKey\.value/)
    // Window widened for ent#440, which added an "end any live voice
    // conversation" line to the same watcher. The rule being guarded is
    // unchanged: switching agents RE-READS that agent's stored choice.
    expect(CODE).toMatch(/watch\(\(\)\s*=>\s*props\.agent\?\.name[\s\S]{0,600}loadVoiceMode\(\)/)
  })

  it('never lets a storage failure break the conversation', () => {
    // Private-mode Safari throws on getItem/setItem; a chat must not die for it.
    expect(CODE).toMatch(/try\s*\{\s*return localStorage\.getItem/)
    expect(CODE).toMatch(/try\s*\{\s*localStorage\.setItem\(voiceModeKey\.value/)
  })

  it('still speaks only when the client has narration on AND the agent can be narrated', () => {
    // ent#440 added one more condition — a live voice conversation narrates
    // through its own state machine, so this branch must NOT also speak (it
    // would say every reply twice, once raw and once cleaned for the ear).
    // The #2157 rule is asserted term by term so it survives that addition
    // without the guard degrading into "some line mentions voiceMode".
    expect(CODE).toContain('if (voiceMode.value && ttsEnabled.value && data.response')
    // ent#440 review: the guard gained a second term — a Stop pressed while the
    // agent was thinking left `voiceConvLive` false by the time this branch ran
    // (it is evaluated after the turn's await), so the raw reply was spoken for
    // a loop the user had already ended. Pinned as a RULE, not a literal.
    expect(CODE).toMatch(/voiceMode\.value && ttsEnabled\.value && data\.response[\s\S]{0,120}!voiceConvLive\.value[\s\S]{0,80}speak\(data\.response\)/)
    expect(CODE).toMatch(/!voiceLoopEndedDuringTurn\) speak\(data\.response\)/)
    expect(CODE).toContain('const ttsEnabled = computed(() => !!props.agent.voice_available)')
  })
})
