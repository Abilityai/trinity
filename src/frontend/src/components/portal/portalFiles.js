/**
 * The Files tab's pure rules (#2582 / ent#548) — what to preview, in what
 * order, and who may do what to a row.
 *
 * Pure by design: `vitest.config.js` pins `environment: 'node'`, so a rule that
 * lives in a `.vue` file is a rule no unit test can reach. Everything here is a
 * function of its arguments.
 *
 * ## `flattenFiles` owns BOTH the render order and the preview index
 *
 * The template used to render two `<ul>`s per participant while the modal
 * walked its own list. Two orderings drift, and when they do the modal opens the
 * WRONG FILE — silently, with no error anywhere. So there is one flat list, the
 * template renders it with a computed group header, and `neighbour()` indexes
 * into the same array.
 *
 * ## Why `groups` is a parameter and not two hard-coded collections
 *
 * ent#548's Technical Notes require preview and delete to "carry over unchanged"
 * when ent#484's per-agent shared working folder lands. With this signature that
 * folder is a third entry in `groups` and nothing structural changes. It is free
 * now and a rewrite later.
 */

/** The text preview ceiling. Stated in the UI, not only here — AC 7. */
export const TEXT_PREVIEW_CAP_BYTES = 256 * 1024

/**
 * Above this an image shows its card rather than being fetched. The upload cap
 * is 25 MiB and an agent share may be up to 50 MB, so this is a real gate: a
 * preview is a convenience and pulling 50 MB through a docker exec to satisfy
 * one is not.
 */
export const IMAGE_PREVIEW_CAP_BYTES = 10 * 1024 * 1024

const IMAGE_MIMES = new Set([
  'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/svg+xml',
  'image/bmp', 'image/avif',
])

const MARKDOWN_EXTS = new Set(['md', 'markdown', 'mdx'])

// Extension-first, and that is not laziness. Python's `mimetypes` — which is
// what `_read_inbox` guesses an upload's type with — maps `.ts` to
// `video/mp2t` and `.toml` to nothing at all; and an agent-shared `.md` arrives
// as `text/plain` because python-magic sniffs bytes, not names. Deciding
// markdown-vs-code from the MIME therefore gets both wrong.
const TEXT_EXTS = new Set([
  'txt', 'text', 'log', 'csv', 'tsv', 'json', 'jsonl', 'yaml', 'yml', 'toml',
  'ini', 'cfg', 'conf', 'env', 'xml', 'html', 'htm', 'css', 'scss',
  'js', 'jsx', 'ts', 'tsx', 'vue', 'py', 'rb', 'go', 'rs', 'java', 'kt',
  'c', 'h', 'cpp', 'hpp', 'cs', 'php', 'sh', 'bash', 'zsh', 'sql', 'r',
  'swift', 'lua', 'pl', 'dockerfile', 'gitignore', 'diff', 'patch',
])

export function extensionOf(filename) {
  const name = String(filename || '')
  const dot = name.lastIndexOf('.')
  if (dot <= 0 || dot === name.length - 1) return ''
  return name.slice(dot + 1).toLowerCase()
}

/**
 * `'image' | 'markdown' | 'text' | 'none'` — what the modal can render.
 *
 * Images are decided by MIME (the server detects it from the bytes for a share,
 * and guesses it from the extension for an upload — either way it is the
 * trustworthy half for binary). Text is decided by EXTENSION, for the reason
 * above the `TEXT_EXTS` list.
 *
 * `image/svg+xml` IS an image here, and rendering it safely is the component's
 * job: `<img :src>` never executes the script an uploaded SVG may carry, an
 * inline `<svg>` would.
 */
export function previewKind(file) {
  if (!file) return 'none'
  const mime = String(file.mime_type || '').toLowerCase().split(';')[0].trim()
  if (IMAGE_MIMES.has(mime)) return 'image'
  const ext = extensionOf(file.filename)
  if (MARKDOWN_EXTS.has(ext)) return 'markdown'
  if (TEXT_EXTS.has(ext)) return 'text'
  // A bare `text/*` with an unknown extension is still readable text.
  if (mime.startsWith('text/')) return 'text'
  if (mime === 'application/json') return 'text'
  return 'none'
}

export function isPreviewable(file) {
  return previewKind(file) !== 'none'
}

/**
 * The rows the tab renders, flattened, in render order.
 *
 * @param {object} opts
 *   participants  string[]  the agents, in the order the tab shows them
 *   groups        Array<{ kind, itemsByAgent, actions? }>  ordered; uploads and
 *                 documents are the first two today, ent#484's folder can be a
 *                 third with no other change.
 * @returns {Array<{ key, agent, kind, item }>} — `key` is stable per row.
 */
export function flattenFiles({ participants = [], groups = [] } = {}) {
  const rows = []
  for (const agent of Array.isArray(participants) ? participants : []) {
    for (const group of Array.isArray(groups) ? groups : []) {
      const items = group?.itemsByAgent?.[agent]
      for (const item of Array.isArray(items) ? items : []) {
        rows.push({
          // `id` for a share (stable), `filename` for an upload (the inbox
          // write is an OVERWRITE, so a name IS the identity there).
          key: `${agent}:${group.kind}:${item?.id || item?.filename || rows.length}`,
          agent,
          kind: group.kind,
          item,
        })
      }
    }
  }
  return rows
}

/**
 * The next previewable row in `dir` (+1 / -1), or null at the end.
 *
 * STOPS rather than wrapping: a lightbox that loops gives the reader no way to
 * tell they have seen everything. Non-previewable rows are skipped, which is
 * ent#548's "never a blank modal" clause meeting its navigation clause — a list
 * where every third file is a `.zip` must step over them, not open blank.
 */
export function neighbour(rows, index, dir) {
  const list = Array.isArray(rows) ? rows : []
  const step = dir < 0 ? -1 : 1
  for (let i = index + step; i >= 0 && i < list.length; i += step) {
    if (isPreviewable(list[i]?.item)) return i
  }
  return null
}

/**
 * Which verbs a row offers (#2582 / ent#548). The UI mirrors the server's
 * matrix off the roster payload (#2128) — it never invents an affordance the
 * service would refuse.
 *
 *   my own upload           → download + delete (real)
 *   agent share, viewer     → download + remove from my list
 *   agent share, owner      → download + remove from my list + delete for everyone
 *
 * `owned` comes from the roster card and is session-type dependent by
 * construction: an owner signed in with a magic-link portal token reads false
 * and gets the viewer affordance. That is the ent#358 rule, not a bug — and
 * because `service.portal_owns_agent` resolves the SAME membership, the button
 * and the gate cannot disagree.
 */
export function fileActions(row, { owned = false } = {}) {
  if (row?.kind === 'upload') {
    return { download: true, remove: 'delete', revoke: false }
  }
  return { download: true, remove: 'dismiss', revoke: owned === true }
}

/**
 * Routes the backend serves on EVERY hostname that fronts it. The portal page's
 * own origin proxies `/api/*`, so a url under this prefix names bytes that are
 * reachable same-origin whatever origin it was minted with.
 */
const BACKEND_API_PREFIX = '/api/'

/**
 * A same-origin path for a `download_url` that may be absolute or relative.
 *
 * `base` is required because `portal_documents` emits a RELATIVE url whenever
 * no portal base URL is configured (`get_portal_base_url()` falls back to
 * `public_chat_url`, which can be `''`), and `new URL(relative)` throws.
 *
 * A portal base URL that resolves to ANOTHER origin is a supported production
 * topology, not a misconfiguration: a deployment may front its agents on a
 * dedicated public hostname while the portal page is served from the app
 * hostname (#2733). For `/api/*` that origin is decoration — the same backend
 * answers on both, and a share's authority is its `?sig=` token rather than the
 * hostname the url was built with — so the url is reduced to a path and the
 * fetch stays inside CSP `connect-src 'self'`. Widening the CSP instead is not
 * available: the portal base URL is a runtime setting and cannot be baked into
 * a static nginx or Vite header.
 *
 * Any other cross-origin url is returned unchanged. Reducing one to a path
 * would point the fetch at a route this origin does not serve.
 */
export function sameOriginPath(url, base = '') {
  const raw = String(url || '')
  if (!raw) return ''
  try {
    const parsed = new URL(raw, base || 'http://localhost')
    const origin = base ? new URL(base).origin : null
    const crossOrigin = origin !== null && parsed.origin !== origin
    if (crossOrigin && !parsed.pathname.startsWith(BACKEND_API_PREFIX)) return raw
    return `${parsed.pathname}${parsed.search}`
  } catch {
    return raw
  }
}

/** The stated cap line — AC 7 asks for it in the UI, not only in code. */
export function previewCapNotice(shownBytes, totalBytes) {
  const total = Number(totalBytes) || 0
  const shown = Number(shownBytes) || 0
  if (!total || shown >= total) return ''
  return `Showing the first ${humanSize(shown)} of ${humanSize(total)} · Download the full file`
}

export function humanSize(n) {
  const bytes = Number(n) || 0
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/**
 * The server's named reason, through an axios error whose body may be a Blob.
 *
 * `portalHttp` is a bare `axios.create()` and its interceptor rejects, so every
 * other call site reads `err.response.data.detail`. With
 * `responseType: 'blob'` that `data` is a **Blob**, so the idiom yields
 * `undefined` and "the server names the reason" quietly degrades to a generic
 * string for exactly the verbs this issue added. Read the blob first.
 */
export async function errorDetail(err, fallback = 'Something went wrong.') {
  const data = err?.response?.data
  if (data && typeof data.text === 'function') {
    try {
      const parsed = JSON.parse(await data.text())
      const detail = parsed?.detail
      if (typeof detail === 'string' && detail.trim()) return detail.trim()
      if (detail && typeof detail.message === 'string') return detail.message
    } catch { /* not JSON — fall through to the generic line */ }
    return fallback
  }
  const detail = data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail.trim()
  if (detail && typeof detail.message === 'string') return detail.message
  return fallback
}

/**
 * Mark a preview read without mutating the original download URL.
 *
 * The result is always same-origin for the `/api/files/...` url a share
 * carries, so the preview `fetch()` is permitted by `connect-src 'self'` even
 * when `download_url` was built off a portal base URL on another hostname
 * (#2733). Download keeps the absolute url: it is the shareable link, and it
 * saves through an anchor, which `connect-src` does not govern.
 */
export function sharePreviewPath(url, base) {
  const parsed = new URL(url, base)
  parsed.searchParams.set('preview', '1')
  return sameOriginPath(parsed.href, base)
}
