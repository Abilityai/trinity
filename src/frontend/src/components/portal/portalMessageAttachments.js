/**
 * What a sent Workspace message carried (#3265).
 *
 * The composer uploads a file the moment it is attached; the turn only NAMES
 * it. These helpers turn the composer's chip entries into the message's list,
 * the list into the request field, and keep one thumbnail per uploaded image
 * for the life of the tab. (The 1:1 → room carry of #2794 is a different
 * concern and lives in `portalAttachments.js`.)
 *
 * Message attachment shape (the server's `PortalMessageAttachment`, plus `file`
 * on a message sent from this tab):
 *   { filename, size_bytes, mime_type, failed, error, file? }
 */
import { markRaw } from 'vue'
import { previewKind } from './portalFiles'

/**
 * The composer's entries → the sent message's list. Called once the uploads
 * have settled, so every entry is either sent or failed. `serverName` is the
 * name the upload route stored the file under (it can differ from the picked
 * name), and it is what the server checks the turn against.
 */
export function sentAttachments(entries = []) {
  return entries.map((e) => {
    const failed = !!e.error
    return {
      filename: e.serverName || e.name,
      size_bytes: Number.isFinite(e.size) ? e.size : null,
      mime_type: e.file?.type || null,
      failed,
      error: failed ? e.error : null,
      // The local bytes, so the thumbnail costs no request on the way out.
      file: !failed && e.file ? markRaw(e.file) : null,
    }
  })
}

/** The message's list → the request's `attachments` field (null = none). */
export function attachmentsForRequest(list) {
  if (!list || !list.length) return null
  return list.map((a) => (a.failed
    ? { filename: a.filename, failed: true, error: a.error || null }
    : { filename: a.filename }))
}

/** Is this attachment shown as a thumbnail? Decided by type, as the rail does. */
export function isImageAttachment(a) {
  if (!a || a.failed) return false
  return previewKind({ filename: a.filename, mime_type: a.mime_type || a.file?.type }) === 'image'
}

// One object URL per (agent, filename), for the life of the tab. The upload
// read route is rate-limited per person (20/min, 100/h), so a thread that
// re-renders, or that is opened twice, must not fetch the same picture again.
const thumbs = new Map()

function key(agent, filename) {
  return `${agent}\u0000${filename}`
}

/**
 * The thumbnail URL for an uploaded image: from the local file when this tab
 * sent it, otherwise fetched once through `fetchBlob(agent, filename)`.
 * Concurrent callers share one request. A failed fetch is forgotten, so a later
 * render may try again; it rejects so the caller can fall back to a chip.
 */
export function thumbnailUrl(agent, attachment, fetchBlob) {
  const k = key(agent, attachment.filename)
  if (!thumbs.has(k)) {
    const p = attachment.file
      ? Promise.resolve(URL.createObjectURL(attachment.file))
      : Promise.resolve()
        .then(() => fetchBlob(agent, attachment.filename))
        .then((b) => URL.createObjectURL(b))
    thumbs.set(k, p)
    p.catch(() => thumbs.delete(k))
  }
  return thumbs.get(k)
}

/** Test seam: forget every cached thumbnail. */
export function _resetThumbnails() {
  thumbs.clear()
}
