// #2749 — the Chat tab's paperclip picker offers ZIP files, which the upload
// service already accepts (stored as-is). Before, only drag-and-drop could
// attach one.
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')
const chatInput = read('../../src/components/chat/ChatInput.vue')

describe('chat file picker', () => {
  it('accepts ZIP files', () => {
    const accept = chatInput.match(/type="file"[\s\S]*?accept="([^"]+)"/)[1].split(',')
    expect(accept).toContain('.zip')
    expect(accept).toContain('application/zip')
  })
})
