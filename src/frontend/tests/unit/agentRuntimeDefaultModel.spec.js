/**
 * #2981 checkpoint H (H2) — the per-runtime default model.
 *
 * `AgentDetail.vue::defaultModel` moved to a pure util so this node spec can
 * call it. Claude/Gemini/Codex are byte-identical to the inline table they
 * replace; OpenCode returns the harness default (ruling T4).
 *
 * @source-text-pin: two reads, neither is the module under test — the harness constant lives in a Python file a JS spec cannot import, and the AgentDetail.vue read proves the view consumes the util (a mount of AgentDetail runs five network loaders)
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defaultModelForRuntime, OPENCODE_DEFAULT_MODEL } from '../../src/utils/agentRuntime.js'

const HERE = dirname(fileURLToPath(import.meta.url))

describe('defaultModelForRuntime', () => {
  it.each([
    ['claude-code', 'sonnet'],
    ['claude', 'sonnet'],
    ['gemini-cli', 'gemini-2.5-flash'],
    ['gemini', 'gemini-2.5-flash'],
    ['codex', 'gpt-5.1-codex'],
    ['opencode', 'anthropic/claude-sonnet-5'],
    [undefined, 'sonnet'],
    [null, 'sonnet'],
    ['', 'sonnet'],
    ['unknown-runtime', 'sonnet'],
  ])('%s -> %s', (runtime, model) => {
    expect(defaultModelForRuntime(runtime)).toBe(model)
  })

  it('OPENCODE_DEFAULT_MODEL mirrors the harness constant', () => {
    const py = readFileSync(
      resolve(HERE, '../../../../docker/base-image/agent_server/services/opencode_runtime.py'), 'utf8')
    const m = py.match(/^DEFAULT_OPENCODE_MODEL\s*=\s*"([^"]+)"/m)
    expect(m).not.toBeNull()
    expect(OPENCODE_DEFAULT_MODEL).toBe(m[1])
  })

  it('AgentDetail.vue consumes the util instead of an inline table', () => {
    const src = readFileSync(resolve(HERE, '../../src/views/AgentDetail.vue'), 'utf8')
    expect(src).toContain('defaultModelForRuntime(')
    expect(src).not.toContain("'gpt-5.1-codex'")
  })
})
