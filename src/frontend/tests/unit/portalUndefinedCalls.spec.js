/**
 * Every function called in the portal SFCs must actually exist.
 *
 * A block-level edit to PortalConversation.vue deleted `markFailed` while both
 * of its call sites remained. `vite build` compiles that happily — it is a
 * runtime `ReferenceError` — and there is no ESLint in this project, so the
 * whole test suite and CI went green while EVERY failed send in the Workspace
 * threw and rendered no error and no Retry. That is the second time the same
 * user-visible bug shipped, so it gets a guard rather than another fix.
 *
 * Deliberately narrow: it checks the `<script setup>` block of the portal
 * components for calls to bare identifiers that are neither defined there nor
 * imported. It is not a type checker and does not try to be — it catches
 * exactly the "called something that isn't there" class.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'fs'
import { fileURLToPath } from 'url'
import { dirname, join } from 'path'

const here = dirname(fileURLToPath(import.meta.url))
const portalDir = join(here, '../../src/components/portal')
const viewsDir = join(here, '../../src/views')

// Language, DOM and framework names a component may call without defining.
const AMBIENT = new Set([
  'if', 'for', 'while', 'switch', 'catch', 'return', 'typeof', 'await', 'new',
  'function', 'super', 'this', 'Array', 'Object', 'String', 'Number', 'Boolean',
  'Math', 'JSON', 'Date', 'Promise', 'Error', 'Set', 'Map', 'RegExp', 'BigInt',
  'parseInt', 'parseFloat', 'isNaN', 'encodeURIComponent', 'decodeURIComponent',
  'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'requestAnimationFrame',
  'fetch', 'console', 'window', 'document', 'navigator', 'localStorage', 'alert',
  'FormData', 'File', 'Blob', 'FileReader', 'URL', 'TextDecoder', 'AbortController',
  'defineProps', 'defineEmits', 'defineExpose', 'withDefaults', 'MediaRecorder',
  'SpeechRecognition', 'webkitSpeechRecognition', 'Audio', 'CustomEvent', 'Event',
  // Keywords that precede a parenthesised list rather than call anything.
  'async', 'else', 'do', 'try',
])

function scriptOf(source) {
  const m = source.match(/<script setup[^>]*>([\s\S]*?)<\/script>/)
  if (!m) return ''
  // Strip comments and string/template literals first: prose like "a chat (…)"
  // in a comment otherwise reads as a call to `chat`.
  return m[1]
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1 ')
    .replace(/`(?:\\.|\$\{[^}]*\}|[^`\\])*`/g, '``')
    .replace(/'(?:\\.|[^'\\])*'/g, "''")
    .replace(/"(?:\\.|[^"\\])*"/g, '""')
}

function definedNames(script) {
  const names = new Set()
  for (const re of [
    /\bfunction\s+([A-Za-z_$][\w$]*)/g,
    /\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)/g,
    /\b(?:const|let|var)\s*\{([^}]*)\}/g,     // destructured
    /import\s+([A-Za-z_$][\w$]*)\s+from/g,
    /import\s*\{([^}]*)\}\s*from/g,
    // `const x = (…) => …` is caught above, but a plain reassignment or a
    // template-only helper defined as an expression needs this.
    /\b([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(/g,
  ]) {
    let m
    while ((m = re.exec(script))) {
      for (const part of m[1].split(',')) {
        // `a as b` binds b; `{ a: b }` binds b; `x = 1` binds x.
        const name = part
          .split(/\bas\b/).pop()
          .split(':').pop()
          .split('=')[0]
          .trim()
          .replace(/^\.\.\./, '')
        if (name) names.add(name)
      }
    }
  }
  return names
}

function calledNames(script) {
  const names = new Set()
  // A bare `name(` not preceded by `.`, and not a declaration site.
  const re = /(^|[^\w$.])([a-z_$][\w$]*)\s*\(/g
  let m
  while ((m = re.exec(script))) names.add(m[2])
  return names
}

function sfcFiles() {
  const files = readdirSync(portalDir)
    .filter((f) => f.endsWith('.vue'))
    .map((f) => ['components/portal/' + f, join(portalDir, f)])
  files.push(['views/Portal.vue', join(viewsDir, 'Portal.vue')])
  return files
}

describe('portal components call only functions that exist', () => {
  for (const [label, path] of sfcFiles()) {
    it(`${label} has no calls to undefined identifiers`, () => {
      const script = scriptOf(readFileSync(path, 'utf8'))
      if (!script) return
      const defined = definedNames(script)
      const missing = [...calledNames(script)].filter(
        (n) => !defined.has(n) && !AMBIENT.has(n)
      )
      expect(missing, `${label} calls undefined: ${missing.join(', ')}`).toEqual([])
    })
  }
})
