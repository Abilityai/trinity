#!/usr/bin/env node
/**
 * #2705 — every package a dynamic `import('…')` under src/ names must be listed in
 * `optimizeDeps.include` (vite.config.js).
 *
 * Why a rule, not the scanner's own verdict: Vite 8's dependency scanner tree-shakes
 * each script block before it records imports, so a dynamic bare import inside an
 * unexported, side-effect-free helper — `CanvasDiagram.vue`'s `ensureMermaid()` in a
 * plain `<script>` block that only `<script setup>` calls — is never pre-bundled. The
 * first served page whose module graph includes that file then triggers a
 * re-optimisation and a full reload of every open page (the #2705 flake: every
 * request the dedupe spec counts, twice). Static imports are always recorded, even
 * unused; only dynamic ones can be missed, and whether one IS found is an accident
 * of how its caller is wired (`QrCode.vue`'s is found only because `onMounted`
 * references the caller). A rule that lists them all is explicit and cannot drift
 * with the scanner; a guard that ran the scanner would sit on `optimizeDeps()`,
 * which Vite 8 already prints as deprecated.
 *
 *   node scripts/scan-dynamic-imports.mjs [src-dir]
 *
 * Enforced by tests/unit/optimizeDepsIncludeGuard.spec.js.
 */
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// A specifier that is not a package: relative, root, the `@/` alias, `#` imports,
// virtual modules, data/http URLs.
const NOT_BARE_RE = /^(?:\.{1,2}(?:\/|$)|\/|@\/|~\/|#|virtual:|\0|data:|https?:)/
// Not an optimizer key even when bare: the optimizer pre-bundles JS only.
const ASSET_RE = /\.(?:css|scss|sass|less|styl|svg|png|jpe?g|gif|webp|avif|ico|json|wasm|woff2?|ttf|otf|eot|mp3|mp4|webm)(?:\?.*)?$/i
// `import(` that is a call, not `import.meta` (`.` ≠ `(`) and not `foo.import(`.
const DYNAMIC_IMPORT_RE = /(?<![.\w$])import\s*\(\s*([^)]*?)\s*\)/g
// The closing tag may carry whitespace before its `>` (`</script >` is valid HTML).
const SCRIPT_BLOCK_RE = /<script\b[^>]*>([\s\S]*?)<\/script\s*>/gi

/**
 * Blank comments, keep strings — a tokenizer, not a regex. `'image/*'`, `'**\/*.vue'`
 * and `"http://x"` are strings; a regex stripper that reads `/*` or `//` inside them
 * eats real code, possibly a later `import('pkg')`, and nothing here could notice.
 * Newlines inside comments are kept so reported line numbers stay true.
 *
 * A regex literal is kept too (`/[/*]+/` must not open a comment), told from a
 * division by what precedes the slash: an operator, an opening bracket or a keyword
 * starts a literal; an identifier, a number or a closing bracket is a division.
 * Known limit, rare in this tree: a template literal whose `${}` nests another
 * backtick.
 */
const REGEX_PRECEDING_KEYWORD_RE = /(?:^|[^\w$.])(?:return|typeof|instanceof|in|of|new|delete|void|throw|case|do|else|yield|await)\s*$/

function regexMayStart(out) {
  const trimmed = out.replace(/\s+$/, '')
  if (trimmed === '') return true
  const last = trimmed[trimmed.length - 1]
  if ('(,=:[!&|?{};+-*%<>~^'.includes(last)) return true
  return REGEX_PRECEDING_KEYWORD_RE.test(trimmed)
}

export function blankComments(code) {
  const src = String(code ?? '')
  let out = ''
  let i = 0
  let state = 'code' // code | single | double | template | regex | line | block
  let inClass = false // inside `[...]` of a regex literal, where `/` does not close it
  while (i < src.length) {
    const c = src[i]
    const d = src[i + 1]
    if (state === 'code') {
      if (c === '/' && d === '/') { state = 'line'; i += 2; continue }
      if (c === '/' && d === '*') { state = 'block'; i += 2; continue }
      if (c === '/' && regexMayStart(out)) { state = 'regex'; inClass = false }
      else if (c === "'") state = 'single'
      else if (c === '"') state = 'double'
      else if (c === '`') state = 'template'
      out += c; i += 1; continue
    }
    if (state === 'regex') {
      if (c === '\\') { out += c + (d ?? ''); i += 2; continue }
      if (c === '[') inClass = true
      else if (c === ']') inClass = false
      else if ((c === '/' && !inClass) || c === '\n') state = 'code'
      out += c; i += 1; continue
    }
    if (state === 'line') {
      if (c === '\n') { state = 'code'; out += c }
      i += 1; continue
    }
    if (state === 'block') {
      if (c === '*' && d === '/') { state = 'code'; i += 2; continue }
      if (c === '\n') out += c
      i += 1; continue
    }
    // Inside a string or template literal: copy verbatim, honour escapes.
    if (c === '\\') { out += c + (d ?? ''); i += 2; continue }
    if (
      (state === 'single' && (c === "'" || c === '\n')) ||
      (state === 'double' && (c === '"' || c === '\n')) ||
      (state === 'template' && c === '`')
    ) state = 'code'
    out += c; i += 1
  }
  return out
}

/** The script blocks of an SFC (all of them — three components carry two), with their line offsets. */
export function scriptBlocks(source) {
  const blocks = []
  for (const m of String(source ?? '').matchAll(SCRIPT_BLOCK_RE)) {
    const opener = m[0].indexOf('>') + 1
    const start = m.index + opener
    blocks.push({ code: m[1], line: lineOf(source, start) })
  }
  return blocks
}

function lineOf(text, index) {
  let line = 1
  for (let i = 0; i < index; i += 1) if (text[i] === '\n') line += 1
  return line
}

function literalOf(arg) {
  const s = arg.trim()
  let m = /^'([^']*)'$/.exec(s) || /^"([^"]*)"$/.exec(s)
  if (m) return { specifier: m[1] }
  m = /^`([^`]*)`$/.exec(s)
  if (m) {
    // A relative template (`./x/${y}.vue`) cannot name a package; anything else can.
    if (/^(?:\.{1,2}\/|\/|@\/|~\/)/.test(m[1])) return { skip: true }
    return { unscannable: true }
  }
  return { unscannable: true }
}

/**
 * Every dynamic import in one source file.
 *   specifiers: [{ specifier, file, line }]  — bare packages a literal `import('…')` names
 *   unscannable: [{ file, line, source }]    — a dynamic import whose specifier is not a string literal
 */
export function extractDynamicImports(source, file = '<memory>') {
  const blocks = file.endsWith('.vue') ? scriptBlocks(source) : [{ code: String(source ?? ''), line: 1 }]
  const specifiers = []
  const unscannable = []
  for (const block of blocks) {
    const code = blankComments(block.code)
    for (const m of code.matchAll(DYNAMIC_IMPORT_RE)) {
      const line = block.line + lineOf(code, m.index) - 1
      // `import('x', { with: … })`: the specifier is the first argument.
      const arg = m[1].split(',')[0]
      const lit = literalOf(arg)
      if (lit.skip) continue
      if (lit.unscannable) { unscannable.push({ file, line, source: m[0].trim() }); continue }
      const specifier = lit.specifier
      if (NOT_BARE_RE.test(specifier) || ASSET_RE.test(specifier)) continue
      specifiers.push({ specifier, file, line })
    }
  }
  return { specifiers, unscannable }
}

/** `@scope/name/sub` → `@scope/name`; `name/sub` → `name`. */
export function packageRoot(specifier) {
  const parts = specifier.split('/')
  return specifier.startsWith('@') ? parts.slice(0, 2).join('/') : parts[0]
}

function listSourceFiles(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (name === 'node_modules' || name === 'dist' || name.startsWith('.')) continue
    const p = join(dir, name)
    if (statSync(p).isDirectory()) listSourceFiles(p, out)
    else if (/\.(?:vue|m?js|ts)$/.test(name)) out.push(p)
  }
  return out
}

/** Scan a tree. Files are reported relative to `srcDir`'s parent (i.e. `src/...`). */
export function scanDynamicImports(srcDir) {
  const root = resolve(srcDir)
  const base = resolve(root, '..')
  const specifiers = []
  const unscannable = []
  for (const file of listSourceFiles(root).sort()) {
    const rel = relative(base, file)
    const found = extractDynamicImports(readFileSync(file, 'utf8'), rel)
    specifiers.push(...found.specifiers)
    unscannable.push(...found.unscannable)
  }
  return { specifiers, unscannable }
}

const isMain = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)
if (isMain) {
  const arg = process.argv[2] || 'src'
  let scanRoot = resolve(arg)
  if (existsSync(join(scanRoot, 'src')) && !existsSync(join(scanRoot, 'App.vue'))) scanRoot = join(scanRoot, 'src')
  const { specifiers, unscannable } = scanDynamicImports(scanRoot)
  for (const s of specifiers) console.log(`${s.specifier}\t${s.file}:${s.line}`)
  for (const u of unscannable) console.log(`UNSCANNABLE\t${u.file}:${u.line}\t${u.source}`)
  console.log(`${specifiers.length} dynamic bare import(s), ${unscannable.length} unscannable`)
}
