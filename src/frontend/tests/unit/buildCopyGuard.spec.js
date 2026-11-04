/**
 * Nothing a person reads during setup or on the Build page mentions the Agent Builder:
 * their own guide builds the agents now (final review, 3 Nov).
 */
import { readFileSync, readdirSync, statSync } from 'fs'
import { join } from 'path'
import { fileURLToPath } from 'url'
import { describe, expect, it } from 'vitest'

const SRC = fileURLToPath(new URL('../../src/components/', import.meta.url))
// The template with its comments taken out, repeated until none is left (a comment
// rebuilt by taking one out is taken out too).
function withoutComments(text) {
  let before
  let now = text
  do {
    before = now
    now = now.replace(/<!--[\s\S]*?-->/g, '')
  } while (now !== before)
  return now
}
const vueFiles = (dir) => readdirSync(dir).flatMap((name) => {
  const path = join(dir, name)
  return statSync(path).isDirectory() ? vueFiles(path) : path.endsWith('.vue') ? [path] : []
})

describe('setup and the Build page', () => {
  it('never tell a person about the Agent Builder', () => {
    for (const file of [...vueFiles(join(SRC, 'onboarding')), ...vueFiles(join(SRC, 'portal/builder'))]) {
      const template = withoutComments(readFileSync(file, 'utf8').split('<script')[0])
      expect(template, file).not.toMatch(/Agent Builder/)
    }
  })
})
