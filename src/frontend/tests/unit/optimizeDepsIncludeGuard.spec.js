import { describe, it, expect, beforeAll } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { resolveConfig } from 'vite'
import {
  blankComments,
  extractDynamicImports,
  packageRoot,
  scanDynamicImports,
} from '../../scripts/scan-dynamic-imports.mjs'

/**
 * #2705 — every package a dynamic `import('…')` under src/ names is listed in
 * `optimizeDeps.include`, so the dev server pre-bundles it at startup instead of
 * discovering it mid-session.
 *
 * The class: Vite 8's dependency scanner tree-shakes each script block before it
 * records imports, so a dynamic bare import inside an unexported, side-effect-free
 * helper is invisible to it (`CanvasDiagram.vue`'s `ensureMermaid()` in a plain
 * `<script>` block). The first served page that includes the file then triggers a
 * re-optimisation and a full reload of EVERY open page — which is why
 * `agent-detail-request-dedupe.spec.js` saw every request twice, and why a
 * developer's tabs all reload on the first workspace open after a container start.
 * Static imports are always found, even unused; only dynamic ones can be missed, and
 * whether one is found depends on how its caller happens to be wired. So the rule
 * lists them all — read off the RESOLVED config, the object Vite actually uses, not
 * the config's source text.
 *
 *   node scripts/scan-dynamic-imports.mjs src
 */

const here = dirname(fileURLToPath(import.meta.url))
const FRONTEND = resolve(here, '../..')

describe('scanner — what counts as a dynamic bare import', () => {
  it('finds a dynamic import inside an unexported helper of a plain <script> block', () => {
    const sfc = `<template><div /></template>
<script>
async function ensure() {
  return (await import('mermaid')).default
}
</script>
<script setup>
const x = 1
</script>`
    const { specifiers, unscannable } = extractDynamicImports(sfc, 'a.vue')
    expect(specifiers).toEqual([{ specifier: 'mermaid', file: 'a.vue', line: 4 }])
    expect(unscannable).toEqual([])
  })

  it('scans every <script> block of an SFC, with true line numbers', () => {
    const sfc = `<template>
  <input accept="image/*,text/*" />
</template>
<script>
const a = await import('first-pkg')
</script >
<script setup>
const b = await import("@scope/second/sub")
</script>`
    const { specifiers } = extractDynamicImports(sfc, 'b.vue')
    expect(specifiers.map(s => [s.specifier, s.line])).toEqual([['first-pkg', 5], ['@scope/second/sub', 8]])
  })

  it('ignores comments, JSDoc types and strings that only look like comment openers', () => {
    const js = `/** @param {import('vue').Ref} el */
// const x = import('commented-out')
const accept = 'image/*'
const glob = '**/*.vue'
const url = "http://example.com//x"
export async function load() { return import('real-pkg') }
/* import('also-commented') */`
    const { specifiers, unscannable } = extractDynamicImports(js, 'c.js')
    expect(specifiers).toEqual([{ specifier: 'real-pkg', file: 'c.js', line: 6 }])
    expect(unscannable).toEqual([])
  })

  it('keeps newlines inside a blanked block comment so later lines stay true', () => {
    const code = 'a\n/* one\ntwo */\nb'
    expect(blankComments(code)).toBe('a\n\n\nb')
  })

  it('does not mistake a regex literal for a comment opener, and a division for a regex', () => {
    const js = `const re = /[/*]+/g
const also = value.replace(/\\/\\*[\\s\\S]*?\\*\\//g, '')
const ratio = total / count / 2
export const load = () => import('after-regex')`
    const { specifiers, unscannable } = extractDynamicImports(js, 'g.js')
    expect(specifiers.map(s => s.specifier)).toEqual(['after-regex'])
    expect(unscannable).toEqual([])
  })

  it('does not report static imports, relative or aliased dynamic imports, import.meta, or assets', () => {
    const js = `import vue from 'vue'
import { x } from '@heroicons/vue/24/outline'
const a = () => import('./local.js')
const b = () => import('../up.vue')
const c = () => import('@/views/Portal.vue')
const d = () => import(\`./views/\${name}.vue\`)
const e = import.meta.glob('./widgets/*.vue')
const f = () => import('some-pkg/style.css')`
    const { specifiers, unscannable } = extractDynamicImports(js, 'd.js')
    expect(specifiers).toEqual([])
    expect(unscannable).toEqual([])
  })

  it('reports a dynamic import whose specifier is not a string literal as unscannable', () => {
    const js = `const name = 'x'
const a = () => import(name)
const b = () => import(\`\${name}\`)`
    const { specifiers, unscannable } = extractDynamicImports(js, 'e.js')
    expect(specifiers).toEqual([])
    expect(unscannable.map(u => u.line)).toEqual([2, 3])
  })

  it('keeps a subpath specifier verbatim and knows its package root', () => {
    const { specifiers } = extractDynamicImports(`import('@heroicons/vue/24/solid')`, 'f.js')
    expect(specifiers[0].specifier).toBe('@heroicons/vue/24/solid')
    expect(packageRoot('@heroicons/vue/24/solid')).toBe('@heroicons/vue')
    expect(packageRoot('qrcode/lib/browser')).toBe('qrcode')
  })
})

describe('rule — every dynamic bare import under src/ is pre-bundled (#2705)', () => {
  let include
  let deps
  let scan

  beforeAll(async () => {
    // The resolved config is what the dev server runs with — `resolveConfig` is
    // Vite's public API for it and loads vite.config.js the way `vite` itself does.
    const config = await resolveConfig(
      { root: FRONTEND, configFile: resolve(FRONTEND, 'vite.config.js'), logLevel: 'error' },
      'serve',
    )
    include = Array.isArray(config.optimizeDeps?.include) ? config.optimizeDeps.include : []
    const pkg = JSON.parse(readFileSync(resolve(FRONTEND, 'package.json'), 'utf8'))
    deps = { ...(pkg.dependencies || {}), ...(pkg.devDependencies || {}) }
    scan = scanDynamicImports(resolve(FRONTEND, 'src'))
  })

  it('lists every package a dynamic import names', () => {
    const missing = scan.specifiers
      .filter(s => !include.includes(s.specifier))
      .map(s => `${s.specifier}  (${s.file}:${s.line})`)
    expect(
      missing,
      `These dynamic bare imports are not in optimizeDeps.include (vite.config.js). The dev ` +
      `server would discover them mid-session and full-reload every open page. Add them:\n  ` +
      missing.join('\n  '),
    ).toEqual([])
  })

  it('has no dynamic import the rule cannot read', () => {
    const where = scan.unscannable.map(u => `${u.file}:${u.line}  ${u.source}`)
    expect(
      where,
      `A dynamic import with a non-literal specifier cannot be checked against ` +
      `optimizeDeps.include. Use a string literal (or a relative template) instead:\n  ` +
      where.join('\n  '),
    ).toEqual([])
  })

  it('names only real dependencies, so a typo or a stale entry cannot hide', () => {
    const unknown = include.filter(entry => !deps[packageRoot(entry)])
    expect(
      unknown,
      `optimizeDeps.include names packages that are not in package.json: ${unknown.join(', ')}`,
    ).toEqual([])
  })
})
