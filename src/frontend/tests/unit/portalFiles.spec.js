/**
 * trinity#2582 + trinity-enterprise#548 — the Files tab's pure rules, and the
 * preview component's non-negotiables.
 *
 * `vitest.config.js` pins `environment: 'node'` and this repo has no component
 * mount harness, so the split is deliberate: every RULE lives in
 * `components/portal/portalFiles.js` and is exercised directly here, while the
 * component's security and accessibility properties are asserted as source
 * guards — the established idiom for the parts a unit test cannot reach.
 *
 * The guards below are the ones that would be quiet if they broke. An inline
 * `<svg>` instead of `<img>` renders identically until someone uploads a
 * scripted SVG. A bubble-phase Escape listener works perfectly until a turn is
 * in flight. A `Range` header works everywhere except the deployment whose
 * portal base URL differs. None of those show up in a screenshot.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { join } from 'path'
import { stripComments } from './helpers/stripComments'

import {
  IMAGE_PREVIEW_CAP_BYTES,
  TEXT_PREVIEW_CAP_BYTES,
  errorDetail,
  extensionOf,
  fileActions,
  flattenFiles,
  humanSize,
  isPreviewable,
  neighbour,
  previewCapNotice,
  previewKind,
  sameOriginPath,
} from '@/components/portal/portalFiles'

const SRC = fileURLToPath(new URL('../../src', import.meta.url))
const read = (rel) => readFileSync(join(SRC, rel), 'utf8')
const code = (rel) => stripComments(read(rel))

const PREVIEW = code('components/portal/PortalFilePreview.vue')
const RAIL_FILES = code('components/portal/PortalRailFiles.vue')

const upload = (filename, mime = null, size = 10) =>
  ({ filename, mime_type: mime, size_bytes: size, uploaded_at: '2026-09-07T10:00:00Z' })
const doc = (id, filename, mime = null, size = 10) =>
  ({ id, filename, mime_type: mime, size_bytes: size, created_at: '2026-09-07T10:00:00Z',
     download_url: `/api/files/${id}?sig=t&download=1` })

// ---------------------------------------------------------------------------
// previewKind
// ---------------------------------------------------------------------------

describe('ent#548 — what the modal can render', () => {
  it('decides images by MIME, svg included', () => {
    expect(previewKind(upload('a.png', 'image/png'))).toBe('image')
    expect(previewKind(upload('a.jpg', 'image/jpeg'))).toBe('image')
    expect(previewKind(upload('a.svg', 'image/svg+xml'))).toBe('image')
    expect(previewKind(upload('a.webp', 'image/webp'))).toBe('image')
  })

  it('decides text by EXTENSION, because the MIME lies on exactly the cases that matter', () => {
    // A shared `.md` arrives as text/plain (python-magic sniffs bytes, not
    // names), so a MIME-first rule would render it as code.
    expect(previewKind(doc('d1', 'README.md', 'text/plain'))).toBe('markdown')
    // Python's mimetypes maps `.ts` to video/mp2t — a MIME-first rule would
    // call a TypeScript file a video and refuse to preview it.
    expect(previewKind(upload('main.ts', 'video/mp2t'))).toBe('text')
    // ...and `.toml` to nothing at all.
    expect(previewKind(upload('pyproject.toml', null))).toBe('text')
    expect(previewKind(upload('data.csv', 'text/csv'))).toBe('text')
  })

  it('falls back to a bare text/* or application/json for an unknown extension', () => {
    expect(previewKind(upload('notes', 'text/plain'))).toBe('text')
    expect(previewKind(upload('payload', 'application/json'))).toBe('text')
  })

  it('says none for everything else, and never throws on junk', () => {
    expect(previewKind(upload('archive.zip', 'application/zip'))).toBe('none')
    expect(previewKind(upload('a.pdf', 'application/pdf'))).toBe('none')
    expect(previewKind(null)).toBe('none')
    expect(previewKind({})).toBe('none')
    expect(isPreviewable(upload('archive.zip', 'application/zip'))).toBe(false)
  })

  it('ignores a charset parameter on the MIME', () => {
    expect(previewKind(upload('a.png', 'image/png; charset=binary'))).toBe('image')
  })

  it('extensionOf handles dotfiles and trailing dots without inventing one', () => {
    expect(extensionOf('a.tar.gz')).toBe('gz')
    expect(extensionOf('.gitignore')).toBe('')     // a dotfile has no extension
    expect(extensionOf('noext')).toBe('')
    expect(extensionOf('trailing.')).toBe('')
  })
})

// ---------------------------------------------------------------------------
// flattenFiles + neighbour
// ---------------------------------------------------------------------------

describe('#2582 — one list owns the render order AND the preview index', () => {
  const groups = (uploads, documents) => ([
    { kind: 'upload', itemsByAgent: uploads },
    { kind: 'document', itemsByAgent: documents },
  ])

  it('walks participants in order, groups in order, items in order', () => {
    const rows = flattenFiles({
      participants: ['scout', 'sage'],
      groups: groups(
        { scout: [upload('u1.txt')], sage: [upload('u2.txt')] },
        { scout: [doc('d1', 'd1.pdf')], sage: [] },
      ),
    })
    expect(rows.map((r) => `${r.agent}/${r.kind}/${r.item.filename || r.item.id}`)).toEqual([
      'scout/upload/u1.txt',
      'scout/document/d1.pdf',
      'sage/upload/u2.txt',
    ])
  })

  it('equals the order the template renders, because the template filters THIS list', () => {
    // The template is `v-for="row in rowsIn(agent, group)"` with
    // `rowsIn = allRows.filter(...)`, so a filter of the flat list cannot
    // reorder it. Pinned as source, since the drift this prevents is silent.
    expect(RAIL_FILES).toMatch(/allRows\.value\.filter\(/)
    expect(RAIL_FILES).toMatch(/v-for="row in rowsIn\(agent, group\)"/)
    expect(RAIL_FILES).toMatch(/GROUP_ORDER = \['upload', 'document'\]/)
  })

  it('keys a share by id and an upload by filename', () => {
    // The inbox write is an OVERWRITE, so a name IS the identity for an upload;
    // a share has a stable id and two shares can share a display name.
    const rows = flattenFiles({
      participants: ['scout'],
      groups: groups({ scout: [upload('same.txt')] }, { scout: [doc('d1', 'same.txt')] }),
    })
    expect(new Set(rows.map((r) => r.key)).size).toBe(2)
  })

  it('tolerates missing agents, missing groups and non-array items', () => {
    expect(flattenFiles()).toEqual([])
    expect(flattenFiles({ participants: ['x'], groups: groups({}, {}) })).toEqual([])
    expect(flattenFiles({ participants: ['x'], groups: [{ kind: 'upload', itemsByAgent: { x: null } }] })).toEqual([])
  })

  it('takes groups as data, so ent#484\'s shared folder is a third entry', () => {
    const rows = flattenFiles({
      participants: ['scout'],
      groups: [
        ...groups({ scout: [upload('u.txt')] }, { scout: [doc('d1', 'd.pdf')] }),
        { kind: 'folder', itemsByAgent: { scout: [doc('f1', 'shared.txt')] } },
      ],
    })
    expect(rows.map((r) => r.kind)).toEqual(['upload', 'document', 'folder'])
  })
})

describe('ent#548 — next/previous walks the previewable subset', () => {
  const rows = flattenFiles({
    participants: ['scout'],
    groups: [{
      kind: 'upload',
      itemsByAgent: {
        scout: [
          upload('a.png', 'image/png'),      // 0 previewable
          upload('b.zip', 'application/zip'), // 1 NOT
          upload('c.md', 'text/plain'),      // 2 previewable
          upload('d.zip', 'application/zip'), // 3 NOT
        ],
      },
    }],
  })

  it('skips a non-previewable row in both directions', () => {
    expect(neighbour(rows, 0, 1)).toBe(2)
    expect(neighbour(rows, 2, -1)).toBe(0)
  })

  it('stops at the ends rather than wrapping', () => {
    // A lightbox that loops gives the reader no way to tell they have seen
    // everything.
    expect(neighbour(rows, 2, 1)).toBe(null)
    expect(neighbour(rows, 0, -1)).toBe(null)
  })

  it('walks from a non-previewable row too — a .zip still opens its card', () => {
    expect(neighbour(rows, 1, 1)).toBe(2)
    expect(neighbour(rows, 1, -1)).toBe(0)
  })

  it('returns null for an empty or absent list', () => {
    expect(neighbour([], 0, 1)).toBe(null)
    expect(neighbour(undefined, 0, -1)).toBe(null)
  })
})

// ---------------------------------------------------------------------------
// fileActions
// ---------------------------------------------------------------------------

describe('ent#548 — the permission matrix the UI mirrors', () => {
  it('my own upload gets a real delete', () => {
    expect(fileActions({ kind: 'upload' }, { owned: false }))
      .toEqual({ download: true, remove: 'delete', revoke: false })
  })

  it('a viewer gets remove-from-my-list and no revoke', () => {
    expect(fileActions({ kind: 'document' }, { owned: false }))
      .toEqual({ download: true, remove: 'dismiss', revoke: false })
  })

  it('the owner gets both', () => {
    expect(fileActions({ kind: 'document' }, { owned: true }))
      .toEqual({ download: true, remove: 'dismiss', revoke: true })
  })

  it('fails closed on an unknown ownership answer', () => {
    // The bug to avoid is offering a destructive action the server refuses, so
    // anything that is not exactly `true` is a viewer.
    for (const owned of [undefined, null, 'true', 1]) {
      expect(fileActions({ kind: 'document' }, { owned }).revoke).toBe(false)
    }
    expect(fileActions({ kind: 'document' }).revoke).toBe(false)
  })

  it('an upload is never revocable, whoever is asking', () => {
    expect(fileActions({ kind: 'upload' }, { owned: true }).revoke).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// sameOriginPath / caps / errors
// ---------------------------------------------------------------------------

describe('#2582 — url and size helpers', () => {
  it('reduces an absolute same-origin url to a path', () => {
    expect(sameOriginPath('https://portal.example.com/api/files/f1?sig=t&download=1',
                          'https://portal.example.com'))
      .toBe('/api/files/f1?sig=t&download=1')
  })

  it('handles a RELATIVE url without throwing', () => {
    // `portal_documents` emits a relative url whenever no portal base URL is
    // configured, and `new URL(relative)` throws. This is the case a naive
    // implementation breaks on, in production, on the default install.
    expect(sameOriginPath('/api/files/f1?sig=t&download=1', 'https://portal.example.com'))
      .toBe('/api/files/f1?sig=t&download=1')
    expect(sameOriginPath('/api/files/f1?sig=t')).toBe('/api/files/f1?sig=t')
  })

  it('leaves a genuinely cross-origin url alone rather than pretending', () => {
    expect(sameOriginPath('https://elsewhere.example/api/files/f1', 'https://portal.example.com'))
      .toBe('https://elsewhere.example/api/files/f1')
  })

  it('returns junk unchanged instead of throwing', () => {
    expect(sameOriginPath('')).toBe('')
    expect(sameOriginPath(null)).toBe('')
  })

  it('states the cap only when something was actually truncated', () => {
    expect(previewCapNotice(TEXT_PREVIEW_CAP_BYTES, 1_100_000))
      .toMatch(/Showing the first .* of .* · Download the full file/)
    expect(previewCapNotice(500, 500)).toBe('')
    expect(previewCapNotice(500, 0)).toBe('')
  })

  it('has caps that are stated, not implicit', () => {
    expect(TEXT_PREVIEW_CAP_BYTES).toBe(256 * 1024)
    expect(IMAGE_PREVIEW_CAP_BYTES).toBe(10 * 1024 * 1024)
  })

  it('humanSize is tabular-friendly and never NaN', () => {
    expect(humanSize(512)).toBe('512 B')
    expect(humanSize(2048)).toBe('2.0 KB')
    expect(humanSize(5 * 1024 * 1024)).toBe('5.0 MB')
    expect(humanSize(undefined)).toBe('0 B')
  })
})

describe('#2582 — the server names the reason, even through a Blob body', () => {
  it('reads a JSON detail out of a Blob response', async () => {
    // With responseType 'blob', `err.response.data` is a Blob and the usual
    // `.detail` idiom yields undefined — so the promised named reason would
    // degrade to a generic line for exactly the verbs this issue added.
    const blob = new Blob([JSON.stringify({ detail: 'The agent isn’t running right now.' })])
    expect(await errorDetail({ response: { data: blob } })).toBe('The agent isn’t running right now.')
  })

  it('falls back cleanly when the Blob is not JSON', async () => {
    const blob = new Blob(['<html>502</html>'])
    expect(await errorDetail({ response: { data: blob } }, 'fallback')).toBe('fallback')
  })

  it('still reads a plain JSON body', async () => {
    expect(await errorDetail({ response: { data: { detail: 'Too many file requests just now.' } } }))
      .toBe('Too many file requests just now.')
    expect(await errorDetail({ response: { data: { detail: { message: 'Named.' } } } })).toBe('Named.')
  })

  it('never returns undefined', async () => {
    expect(await errorDetail(null, 'x')).toBe('x')
    expect(await errorDetail({}, 'x')).toBe('x')
  })
})

// ---------------------------------------------------------------------------
// The component's non-negotiables, as source guards
// ---------------------------------------------------------------------------

describe('ent#548 — PortalFilePreview cannot execute what it renders', () => {
  it('renders an image ONLY through <img>, never inline svg and never v-html', () => {
    // ent#548 lists svg among the image types. An inline <svg> from an uploaded
    // file executes script; <img> never does.
    expect(PREVIEW).toMatch(/<img\s/)
    expect(PREVIEW).not.toMatch(/v-html/)
    expect(PREVIEW).not.toMatch(/innerHTML/)
    expect(PREVIEW).not.toMatch(/<svg[^>]*v-/)
  })

  it('renders markdown through the one sanitiser, not a second renderer', () => {
    expect(PREVIEW).toMatch(/<PortalMarkdown/)
    expect(PREVIEW).not.toMatch(/from '@\/utils\/markdown'/)
  })

  it('renders other text by interpolation inside a <pre>', () => {
    expect(PREVIEW).toMatch(/<pre[\s\S]{0,400}\{\{ text \}\}/)
  })

  it('never sends a Range header', () => {
    // CORS `allow_headers` in main.py does not list Range, so a ranged preview
    // dies silently wherever the portal base URL is genuinely cross-origin —
    // and slicing keeps the preview off /api/files/{id}'s counter path.
    expect(PREVIEW).not.toMatch(/Range/)
    expect(PREVIEW).toMatch(/blob\.slice\(0, TEXT_PREVIEW_CAP_BYTES\)/)
  })
})

describe('ent#548 — PortalFilePreview keyboard and lifecycle', () => {
  it('registers Escape and the arrows in the CAPTURE phase and preventDefaults', () => {
    // The conversation's turn-cancel listener is on `document` in the BUBBLE
    // phase, so a bubble listener here would let Escape cancel an in-flight turn
    // before `shouldCancelOnEscape` ever saw `defaultPrevented`.
    expect(PREVIEW).toMatch(/addEventListener\('keydown', onKeydown, \{ capture: true \}\)/)
    expect(PREVIEW).toMatch(/removeEventListener\('keydown', onKeydown, \{ capture: true \}\)/)
    const handler = PREVIEW.slice(PREVIEW.indexOf('function onKeydown'))
    expect(handler).toMatch(/e\.key === 'Escape'[\s\S]{0,120}preventDefault\(\)/)
    expect(handler).toMatch(/ArrowLeft/)
    expect(handler).toMatch(/ArrowRight/)
  })

  it('arms the listener above every await', () => {
    const mounted = PREVIEW.slice(PREVIEW.indexOf('onMounted('))
    const listenAt = mounted.indexOf("addEventListener('keydown'")
    const awaitAt = mounted.indexOf('await')
    expect(listenAt).toBeGreaterThan(-1)
    expect(awaitAt).toBeGreaterThan(listenAt)
  })

  it('revokes its object URLs on navigate and on unmount', () => {
    expect(PREVIEW).toMatch(/revokeObjectURL/)
    expect(PREVIEW).toMatch(/onBeforeUnmount\([\s\S]{0,200}revoke\(\)/)
  })

  it('is a dialog with a focus trap and initial focus on the safe action', () => {
    expect(PREVIEW).toMatch(/role="dialog"/)
    expect(PREVIEW).toMatch(/aria-modal="true"/)
    expect(PREVIEW).toMatch(/function trapTab/)
    expect(PREVIEW).toMatch(/portal-file-preview-close"\]'\)[\s\S]{0,120}\.focus\(\)/)
  })

  it('sits BELOW ConfirmDialog, which is also teleported at z-50', () => {
    // Equal z-index means DOM insertion order decides, which neither component
    // controls — so a confirm raised from the preview could render behind it.
    expect(PREVIEW).toMatch(/z-40/)
    expect(PREVIEW).not.toMatch(/z-50/)
  })

  it('yields to an overlay that already claimed the keystroke', () => {
    // Capture listeners on `document` fire in registration order and the tab
    // body mounts first, so a confirm it raised marks the event before this
    // modal sees it. Without the guard one Escape closes both.
    const handler = PREVIEW.slice(PREVIEW.indexOf('function onKeydown'))
    const guardAt = handler.indexOf('e.defaultPrevented')
    const escapeAt = handler.indexOf("e.key === 'Escape'")
    expect(guardAt).toBeGreaterThan(-1)
    expect(escapeAt).toBeGreaterThan(guardAt)
  })

  it('the delete confirm owns Escape too, in the capture phase', () => {
    // ConfirmDialog.vue has NO key handling of its own, so an unguarded Escape
    // on an open confirm dismisses nothing and reaches the conversation's
    // bubble-phase turn-cancel listener with `defaultPrevented` false — killing
    // an in-flight turn. Same shape as the preview's, for the same reason.
    expect(RAIL_FILES).toMatch(
      /addEventListener\('keydown', onConfirmKeydown, \{ capture: true \}\)/,
    )
    expect(RAIL_FILES).toMatch(
      /removeEventListener\('keydown', onConfirmKeydown, \{ capture: true \}\)/,
    )
    const handler = RAIL_FILES.slice(RAIL_FILES.indexOf('function onConfirmKeydown'))
    expect(handler).toMatch(
      /e\.key !== 'Escape' \|\| pending\.value === null[\s\S]{0,80}preventDefault\(\)/,
    )
  })

  it('is mounted with v-if, never v-show', () => {
    // The rail column and the mobile sheet are SIBLINGS in Portal.vue, so a
    // phone with the sheet open mounts the tab body twice — and a teleported
    // overlay ignores its ancestor's `hidden`.
    expect(RAIL_FILES).toMatch(/<PortalFilePreview\s[\s\S]{0,80}v-if="previewIndex !== null"/)
    expect(RAIL_FILES).not.toMatch(/<PortalFilePreview[\s\S]{0,80}v-show/)
  })
})

describe('#2582 / ent#548 — the design-system contract on both files', () => {
  const PALETTE = /(?:^|[\s"'`:])(?:bg|text|border|ring|from|to|via|fill|stroke|divide|placeholder|shadow|outline|decoration|accent|caret)-(?:red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose|slate|zinc|neutral|stone)-\d{2,3}/

  it('uses zero raw non-gray palette classes — a net-new file starts at zero and stays there', () => {
    for (const [name, src] of [['preview', PREVIEW], ['rail files', RAIL_FILES]]) {
      expect(src, name).not.toMatch(PALETTE)
    }
  })

  it('has no hardcoded hex colours', () => {
    for (const [name, src] of [['preview', PREVIEW], ['rail files', RAIL_FILES]]) {
      // `rounded-[10px]` and friends are arbitrary VALUES, not colours.
      const hex = src.match(/#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b/g) || []
      expect(hex, name).toEqual([])
    }
  })

  it('gates loading on the verdict, never on a bare in-flight flag', () => {
    expect(PREVIEW).toMatch(/viewState\(/)
    expect(PREVIEW).toMatch(/view\.state === 'loading'/)
    for (const [name, src] of [['preview', PREVIEW], ['rail files', RAIL_FILES]]) {
      expect(src, name).not.toMatch(/v-if="(?:store|feeds|portal)\.loading"/)
      expect(src, name).not.toMatch(/animate-spin\b/)
    }
  })

  it('never a blank modal: the card covers an unknown type, an over-cap image AND a failed fetch', () => {
    expect(PREVIEW).toMatch(/portal-file-preview-card/)
    const reason = PREVIEW.slice(PREVIEW.indexOf('const cardReason'))
    expect(reason).toMatch(/failed\.value/)
    expect(PREVIEW).toMatch(/size\.value > IMAGE_PREVIEW_CAP_BYTES/)
    // A failed fetch must NOT become a retry loop — that is a different promise.
    expect(PREVIEW).not.toMatch(/<LoadFailed/)
  })

  it('Download uses the SERVER flag for a share and the blob route only for an upload', () => {
    // The split is what makes `?download=1` load-bearing rather than
    // decorative. A share has a signed URL the server now serves `attachment`,
    // so a plain anchor click saves it — natively streamed, no memory spike,
    // and no programmatic blob save, which is the classic iOS Safari failure on
    // a mobile-first surface. An upload has no URL at all, so it has no choice.
    const fn = RAIL_FILES.slice(RAIL_FILES.indexOf('async function download(row)'))
    const body = fn.slice(0, fn.indexOf('\n}\n'))
    expect(body).toMatch(/if \(row\.kind !== 'upload'\)/)
    const sharePath = body.slice(0, body.indexOf('busyKey.value = row.key'))
    expect(sharePath).toMatch(/a\.href = row\.item\.download_url/)
    expect(sharePath).toMatch(/a\.download = /)          // AC-3's attribute, belt-and-braces
    expect(sharePath).not.toMatch(/loadBlob/)             // no fetch on the share path
    const uploadPath = body.slice(body.indexOf('busyKey.value = row.key'))
    expect(uploadPath).toMatch(/await loadBlob\(row\)/)
    expect(uploadPath).toMatch(/createObjectURL/)
    expect(uploadPath).toMatch(/revokeObjectURL/)
  })

  it('composes primitives rather than hand-rolling them', () => {
    expect(PREVIEW).toMatch(/BaseButton/)
    expect(RAIL_FILES).toMatch(/import BaseButton from '@\/components\/base\/BaseButton\.vue'/)
    expect(RAIL_FILES).toMatch(/import ConfirmDialog from '@\/components\/ConfirmDialog\.vue'/)
  })

  it('restates the consequence per case, and each verb has a per-row InlineError', () => {
    expect(RAIL_FILES).toMatch(/CONFIRM_COPY/)
    for (const verb of ['delete', 'dismiss', 'revoke']) {
      expect(RAIL_FILES).toMatch(new RegExp(`\\b${verb}: \\{`))
    }
    expect(RAIL_FILES).toMatch(/removed within a day/)     // the 24h grace, said plainly
    expect(RAIL_FILES).toMatch(/It stays shared/)          // dismissal is not a revoke
    expect(RAIL_FILES).toMatch(/rowErrors\[row\.key\]/)
    expect(RAIL_FILES).toMatch(/await errorDetail\(err/)
  })
})
