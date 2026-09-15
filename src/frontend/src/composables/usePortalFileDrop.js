/**
 * Drag-and-drop and multi-file upload for the Workspace (ent#524).
 *
 * ONE implementation, three consumers — the conversation, the room, and the
 * rail's Files tab. The issue says so explicitly ("Do not build a second drop
 * implementation"), and the reason is visible in what it was fixing: the drop
 * gesture already existed on the Files panel and the batch bug existed there
 * too, because each surface had written its own `files?.[0]`.
 *
 * What this owns:
 *   - the drag-over state and the file-vs-text discrimination;
 *   - the batch loop, with per-file outcome;
 *   - the per-file rejection copy.
 *
 * What it does NOT own: where the files go. That is the caller's `upload`
 * function, so the destination can move to the ent#484/#486 working folder
 * without the gesture changing.
 */
import { markRaw, ref } from 'vue'

// Mirrors the server's per-file ceiling (`MAX_UPLOAD_BYTES`, 25 MiB). Checked
// client-side so a rejection names the file BEFORE 25 MiB crosses the wire; the
// server stays the authority and its refusal is rendered verbatim when the two
// disagree.
export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024

// A batch bound, not a policy: ten files is a plausible gesture, a hundred is a
// dropped folder. Refused by NAME rather than silently truncated — "the count
// uploaded always matches the count dropped or the difference is stated".
export const MAX_BATCH_FILES = 20

/**
 * Is this drag carrying FILES (as opposed to selected text, a link, or an
 * element being dragged within the page)?
 *
 * `dataTransfer.types` is the only thing readable during `dragover` — the file
 * list itself is not exposed until `drop`, by design — so this is the check,
 * and it is why dragging a link over the composer must not light the target.
 */
export function isFileDrag(dataTransfer) {
  const types = dataTransfer?.types
  if (!types) return false
  // `types` is a DOMStringList in some browsers and an array in others; both
  // answer to `includes` via Array.from.
  return Array.from(types).includes('Files')
}

/**
 * Why this file cannot be sent, or null when it can. Returns the sentence the
 * chip shows — it names the file and the limit, never "upload failed".
 */
export function rejectionFor(file) {
  if (!file) return 'That item is not a file.'
  if (typeof file.size === 'number' && file.size > MAX_UPLOAD_BYTES) {
    return `Too large (${formatBytes(file.size)}) — the limit is ${formatBytes(MAX_UPLOAD_BYTES)}.`
  }
  if (file.size === 0) return 'That file is empty.'
  return null
}

export function formatBytes(n) {
  if (!Number.isFinite(n)) return ''
  if (n >= 1048576) return `${(n / 1048576).toFixed(1)} MB`
  if (n >= 1024) return `${Math.max(1, Math.round(n / 1024))} KB`
  return `${n} B`
}

/**
 * A rate-limited batch says which files were accepted and when to retry, rather
 * than half-succeeding in silence (ent#524 AC). `Retry-After` is seconds.
 */
export function rateLimitMessage(err) {
  const after = Number(err?.response?.headers?.['retry-after'])
  if (Number.isFinite(after) && after > 0) {
    const mins = Math.ceil(after / 60)
    return after < 60
      ? `Too many uploads just now — try again in ${Math.ceil(after)}s.`
      : `Too many uploads just now — try again in ${mins} min.`
  }
  return 'Too many uploads just now — try again shortly.'
}

/**
 * Turn a failure into the sentence its own chip shows. The server's own detail
 * wins when it sent one; the generic line is the last resort, never the first.
 */
export function uploadFailureReason(err) {
  if (err?.response?.status === 429) return rateLimitMessage(err)
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail.trim()
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') {
    return detail.message
  }
  if (err?.response?.status === 413) return 'The server rejected it as too large.'
  return "Couldn't upload."
}

/**
 * The chip's state, as ONE verdict rather than a pair of booleans read in order.
 *
 * A file in a batch has three outcomes and no fourth — in flight, refused, sent
 * — so the chip reads a state the way `utils/loadingState.js::viewState` has
 * every data surface read one. That is also what keeps the #1927 ratchet honest
 * here: `v-if="f.uploading"` is indistinguishable, to a scanner, from the bare
 * fetch-in-flight gate the ratchet exists to stop, and the fix it wants is
 * exactly this — decide once, render from the decision.
 *
 * @returns {'uploading'|'failed'|'sent'}
 */
export function attachmentState(entry) {
  if (entry?.error) return 'failed'
  if (entry?.uploading) return 'uploading'
  return 'sent'
}

/**
 * @param {(file: File) => Promise<any>} upload  one file; the caller decides
 *   where it goes. Rejections are per-file and never abort the batch.
 * @param {object} [options]
 * @param {() => boolean} [options.disabled]  drop is ignored while true.
 */
export function usePortalFileDrop(upload, { disabled = () => false } = {}) {
  const dragging = ref(false)
  // One entry per file in the gesture, each with its OWN progress and outcome —
  // the batch has no shared verdict, because "one failure does not fail the
  // batch" is only true if there is nowhere for a shared one to live.
  const entries = ref([])
  const batchNotice = ref('')

  let dragDepth = 0
  // The in-flight batch, so a caller can ASK whether the gesture has landed
  // (#2794). Without this the only way to know was to poll `entry.uploading`.
  let inFlight = null

  // dragenter/dragleave fire for every child element the pointer crosses, so a
  // boolean toggled on leave flickers the affordance off while the pointer is
  // still inside. Counting depth is the standard fix.
  function onDragEnter(e) {
    if (!isFileDrag(e.dataTransfer) || disabled()) return
    dragDepth += 1
    dragging.value = true
  }

  function onDragOver(e) {
    if (!isFileDrag(e.dataTransfer) || disabled()) return
    // Without this the browser navigates to the file on drop.
    e.preventDefault()
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
    dragging.value = true
  }

  function onDragLeave() {
    dragDepth = Math.max(0, dragDepth - 1)
    if (!dragDepth) dragging.value = false
  }

  function onDrop(e) {
    dragDepth = 0
    dragging.value = false
    if (!isFileDrag(e.dataTransfer) || disabled()) return
    e.preventDefault()
    return addFiles(e.dataTransfer.files)
  }

  /**
   * The batch. Every file gets an entry before any upload starts, so the person
   * sees the whole gesture land at once rather than watching it appear one file
   * at a time; each then resolves independently.
   */
  async function addFiles(fileList) {
    const files = Array.from(fileList || [])
    if (!files.length) return []

    batchNotice.value = ''
    let batch = files
    if (files.length > MAX_BATCH_FILES) {
      batch = files.slice(0, MAX_BATCH_FILES)
      batchNotice.value =
        `Only the first ${MAX_BATCH_FILES} of ${files.length} files were added.`
    }

    const mine = batch.map((file) => {
      const rejection = rejectionFor(file)
      const entry = {
        name: file.name,
        size: file.size,
        uploading: !rejection,
        error: rejection || '',
        done: false,
        // #2794: the handle, kept so the SAME bytes can reach a SECOND
        // destination without asking the person to pick the file again —
        // which is what escalating a 1:1 into a room needs (the file has
        // reached one agent's inbox; the room's other participants still need
        // it, and a room-native drop is `one upload per participant`).
        //
        // `markRaw` is belt-and-braces: Vue's `reactive` already declines to
        // proxy a `File` (it is not a plain object), but that is a fact about
        // an internal type table rather than a promise, and a proxied `File`
        // fails deep inside `FormData.append` where the cause is invisible.
        file: markRaw(file),
      }
      entries.value.push(entry)
      return { file, entry, rejection }
    })

    // Sequential, not `Promise.all`: the per-email limiter (ent#287) counts
    // requests, and firing twenty at once is the surest way to trip it on a
    // gesture that would have succeeded spread over a second. A batch that does
    // trip it still reports per file, which is the AC.
    //
    // Chained onto whatever is already running rather than started beside it:
    // two overlapping drops would otherwise interleave their requests, which is
    // the burst the sequencing exists to avoid, and `settled()` could then
    // resolve while the earlier batch was still going.
    const run = Promise.resolve(inFlight).then(async () => {
      for (const { file, entry, rejection } of mine) {
        if (rejection) continue
        try {
          await upload(file)
          entry.done = true
        } catch (err) {
          entry.error = uploadFailureReason(err)
        } finally {
          entry.uploading = false
        }
      }
    })
    inFlight = run
    await run
    // Only the LAST batch clears the marker; an earlier one finishing must not
    // report a later one as settled.
    if (inFlight === run) inFlight = null
    return entries.value
  }

  /**
   * Resolves once nothing is uploading (#2794).
   *
   * A send that happens while a chip is still spinning must not simply leave
   * the file behind — "never silently dropped" is the rule. Waiting is the
   * honest option and the cheap one: uploads are seconds, and the alternative
   * (send now, tell them afterwards what did not make it) asks the person to
   * fix something they cannot see the state of.
   *
   * Never rejects: a failed upload is recorded on its own entry, and a caller
   * asking "has the gesture landed?" wants that answer, not an exception.
   */
  async function settled() {
    // A batch can chain another onto itself, so loop rather than await once.
    while (inFlight) {
      try {
        await inFlight
      } catch {
        // Per-file failures already live on their entries.
        break
      }
    }
    return entries.value
  }

  function clear() {
    entries.value = []
    batchNotice.value = ''
    dragDepth = 0
    dragging.value = false
  }

  function removeAt(i) {
    entries.value.splice(i, 1)
  }

  return {
    dragging,
    entries,
    batchNotice,
    addFiles,
    settled,
    clear,
    removeAt,
    handlers: { onDragEnter, onDragOver, onDragLeave, onDrop },
  }
}
