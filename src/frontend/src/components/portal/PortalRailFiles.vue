<!--
  The rail's Files tab body (trinity-enterprise#475, extended by trinity#2582 +
  trinity-enterprise#548) — send a file to an agent in this chat (drop or pick),
  see what you sent, download or preview either list, and remove a file. A room
  groups by participating agent and picks the recipient with a select — one drop
  zone, not one per agent.

  Reads `stores/portalRailFeeds.js`; never fetches its own listing. The shell
  owns the feed (`composables/usePortalRailFeeds.js`) so the collapsed rail can
  signal a newly shared file with this body unmounted, and so the
  container-backed inbox (`uploads`) is read only while this tab is on screen,
  after an upload from ANY surface, and after a delete.

  Per-agent inbox scoping is unchanged: the same client-portal routes the drawer
  called, roster-gated server-side. Loading is a SKELETON keyed on the feed's
  verdict (AC 6 as amended 2026-09-06 — never the scanline, never a spinner,
  never a bare `loading` gate); a failed first fetch is `LoadFailed`, a failed
  refresh keeps the lists and raises `InlineError` (#1926).

  ## Rows come from ONE flat projection

  `portalFiles.js::flattenFiles` owns both the render order AND the preview
  index. Rendering two `<ul>`s while the modal walked its own list is how a
  lightbox silently opens the wrong file, so there is one `v-for` over the flat
  list with a computed group header.

  ## The delete matrix is the server's, mirrored

  `fileActions(row, { owned })` decides what a row offers, and `owned` comes off
  the roster card (#2128 — the roster payload is THE capability channel here).
  `service.portal_owns_agent` resolves the same membership, so the button and
  the gate cannot disagree: "Delete for everyone" is not OFFERED to a viewer
  rather than being offered and then refused. It is session-type dependent by
  construction (ent#358) — a non-owner admin is a viewer here, and so is an
  owner signed in with a magic-link portal token — and the confirm copy says so.
-->
<template>
  <div
    class="space-y-5"
    data-testid="portal-rail-files"
    @dragover.prevent="dragging = isFileDrag($event.dataTransfer)"
    @dragleave.prevent="dragging = false"
    @drop.prevent="onDropFiles"
  >
    <!-- Send -->
    <div>
      <label v-if="participants.length > 1" class="block text-[11px] text-gray-500 dark:text-gray-400 mb-2">
        Send to
        <select v-model="target" class="mt-0.5 w-full text-xs rounded border-gray-300 dark:border-gray-600 dark:bg-gray-800" data-testid="portal-rail-files-target">
          <option v-for="p in participants" :key="p" :value="p">{{ p }}</option>
        </select>
      </label>
      <label
        class="flex flex-col items-center justify-center gap-1 text-sm rounded-xl border-2 border-dashed px-3 py-5 cursor-pointer transition"
        :class="[dragging ? 'border-action-primary-500 bg-action-primary-50 dark:bg-action-primary-900/20' : 'border-gray-300 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800', uploading ? 'opacity-60 pointer-events-none' : '']"
        data-testid="portal-rail-files-drop"
      >
        <svg class="w-6 h-6 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 01-.88-7.9A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" /></svg>
        <span class="font-medium text-center">{{ uploading ? 'Sending…' : `Drop a file, or click to send to ${targetName}` }}</span>
        <input type="file" multiple class="hidden" :disabled="uploading" @change="onPick" />
      </label>
      <InlineError v-if="uploadError" :message="uploadError" @dismiss="uploadError = ''" />
      <p v-else-if="uploadOk" class="mt-2 text-xs text-status-success-600 dark:text-status-success-400" role="status">{{ uploadOk }}</p>
    </div>

    <!-- Lists -->
    <PortalSkeleton v-if="view.state === 'loading'" variant="rail" />

    <LoadFailed
      v-else-if="view.state === 'failed'"
      title="Couldn't load files"
      :message="feeds.error || 'The files for this chat could not be read.'"
      :retrying="feeds.loading"
      @retry="feeds.refresh({ uploads: true })"
    />

    <template v-else>
      <InlineError v-if="view.stale" :message="feeds.error" @dismiss="feeds.error = null" />

      <section v-for="agent in participants" :key="agent" class="space-y-4" :data-testid="`portal-rail-files-${agent}`">
        <div v-if="participants.length > 1" class="flex items-center gap-2 min-w-0">
          <PortalAvatar :name="agent" :size="18" />
          <span class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 truncate">{{ agent }}</span>
          <span class="ml-auto text-xs text-gray-400">{{ countLabel(agent) }}</span>
        </div>

        <div v-for="group in GROUP_ORDER" :key="group" class="space-y-2">
          <h4 class="text-[11px] font-semibold uppercase tracking-wide text-gray-400">
            {{ group === 'upload' ? 'Files you sent' : `Files from ${agent}` }}
          </h4>
          <div v-if="group === 'upload' && !feeds.uploadsLoaded[agent]" class="h-3 w-24 rounded animate-pulse motion-reduce:animate-none bg-gray-100 dark:bg-gray-800/60" aria-busy="true"></div>
          <div v-else-if="!rowsIn(agent, group).length" class="text-xs text-gray-400 py-1">
            {{ group === 'upload' ? 'Nothing sent yet.' : 'Nothing shared with you yet.' }}
          </div>
          <ul v-else class="space-y-2">
            <li
              v-for="row in rowsIn(agent, group)"
              :key="row.key"
              class="rounded-xl border border-gray-200 dark:border-gray-800 p-3"
            >
              <div class="flex items-center gap-3">
                <FileIcon :mime="row.item.mime_type" />
                <div class="min-w-0 flex-1">
                  <button
                    type="button"
                    class="block w-full truncate text-left text-sm font-medium hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:focus-visible:ring-action-primary-400/40 rounded"
                    :data-testid="`portal-rail-files-open-${row.key}`"
                    @click="openPreview(row)"
                  >
                    {{ row.item.filename }}
                  </button>
                  <div class="text-xs text-gray-400 tabular-nums">
                    {{ humanSize(row.item.size_bytes) }}<span v-if="stampOf(row)"> · {{ formatDate(stampOf(row)) }}</span>
                  </div>
                </div>
                <div class="flex shrink-0 items-center gap-1">
                  <BaseButton
                    size="sm"
                    variant="secondary"
                    :loading="busyKey === row.key && busyVerb === 'download'"
                    :data-testid="`portal-rail-files-download-${row.key}`"
                    @click="download(row)"
                  >
                    Download
                  </BaseButton>
                  <button
                    type="button"
                    class="rounded-md p-1.5 text-gray-500 hover:bg-gray-100 hover:text-status-danger-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:text-gray-400 dark:hover:bg-gray-750 dark:hover:text-status-danger-400 dark:focus-visible:ring-action-primary-400/40"
                    :aria-label="actionsFor(row).remove === 'delete' ? `Delete ${row.item.filename}` : `Remove ${row.item.filename} from my list`"
                    :data-testid="`portal-rail-files-remove-${row.key}`"
                    @click="ask(row, actionsFor(row).remove)"
                  >
                    <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" /></svg>
                  </button>
                  <!-- Two ghost icon buttons rather than a hand-rolled dropdown:
                       no menu primitive exists, and inventing one for two verbs
                       would be the lookalike the contract forbids. -->
                  <button
                    v-if="actionsFor(row).revoke"
                    type="button"
                    class="rounded-md p-1.5 text-gray-500 hover:bg-gray-100 hover:text-status-danger-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:text-gray-400 dark:hover:bg-gray-750 dark:hover:text-status-danger-400 dark:focus-visible:ring-action-primary-400/40"
                    :aria-label="`Delete ${row.item.filename} for everyone`"
                    :data-testid="`portal-rail-files-revoke-${row.key}`"
                    @click="ask(row, 'revoke')"
                  >
                    <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" /></svg>
                  </button>
                </div>
              </div>
              <InlineError
                v-if="rowErrors[row.key]"
                class="mt-2"
                :message="rowErrors[row.key]"
                @dismiss="clearRowError(row.key)"
              />
            </li>
          </ul>
        </div>
      </section>
    </template>

    <PortalFilePreview
      v-if="previewIndex !== null"
      :rows="allRows"
      :index="previewIndex"
      :load-blob="loadBlob"
      @close="previewIndex = null"
      @navigate="(i) => { previewIndex = i }"
      @download="download(allRows[previewIndex])"
    />

    <ConfirmDialog
      v-model:visible="confirmOpen"
      :title="confirmCopy.title"
      :message="confirmCopy.message"
      :confirm-text="confirmCopy.confirmText"
      @confirm="runPending"
      @cancel="pending = null"
    />
  </div>
</template>

<script setup>
import { computed, h, ref, watch } from 'vue'
import { isFileDrag, rejectionFor, uploadFailureReason } from '@/composables/usePortalFileDrop'
import { useClientPortalStore } from '@/stores/clientPortal'
import { usePortalRailFeedsStore } from '@/stores/portalRailFeeds'
import BaseButton from '@/components/base/BaseButton.vue'
import ConfirmDialog from '@/components/ConfirmDialog.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import PortalAvatar from './PortalAvatar.vue'
import PortalFilePreview from './PortalFilePreview.vue'
import PortalSkeleton from './PortalSkeleton.vue'
import { feedView } from './portalRail'
import {
  errorDetail,
  fileActions,
  flattenFiles,
  humanSize,
  sameOriginPath,
} from './portalFiles'

const props = defineProps({
  participants: { type: Array, default: () => [] },
})

const feeds = usePortalRailFeedsStore()
const portal = useClientPortalStore()

const dragging = ref(false)

// ent#524: dragging selected text or a link over this panel used to light the
// drop zone and then do nothing on release. The shared rule answers it.
function onDropFiles(e) {
  dragging.value = false
  if (!isFileDrag(e.dataTransfer)) return
  return uploadBatch(e.dataTransfer.files)
}
const uploading = ref(false)
const uploadError = ref('')
const uploadOk = ref('')
const target = ref(null)

const participants = computed(() => props.participants)
const targetName = computed(() => target.value || participants.value[0] || 'the agent')

// Lists are always rendered around the send zone — the two "Nothing … yet."
// lines ARE the empty copy — so the verdict only decides loading / failed /
// ready (count is pinned at 1: an agent with no files is a ready tab).
const view = computed(() => feedView({
  participants: participants.value,
  hasLoaded: feeds.hasLoaded,
  error: feeds.error,
  count: 1,
}))

// The render order, and therefore the preview order. `flattenFiles` takes the
// groups as data so ent#484's shared working folder can become a third entry
// with no structural change (ent#548's "carries over unchanged").
const GROUP_ORDER = ['upload', 'document']

const allRows = computed(() => flattenFiles({
  participants: participants.value,
  groups: [
    { kind: 'upload', itemsByAgent: feeds.uploads },
    { kind: 'document', itemsByAgent: feeds.documents },
  ],
}))

function rowsIn(agent, kind) {
  return allRows.value.filter((r) => r.agent === agent && r.kind === kind)
}
function stampOf(row) {
  return row.kind === 'upload' ? row.item.uploaded_at : row.item.created_at
}
function countLabel(agent) {
  const n = rowsIn(agent, 'document').length
  return n ? `${n} shared` : 'nothing shared'
}

// #2128: the capability channel for this surface is the roster payload, not the
// JWT-gated feature-flag endpoint (which returns nothing for an external
// client). `owned` fails closed, so an unknown agent offers the viewer verbs.
function ownsAgent(agentName) {
  return (portal.agents || []).some((a) => a.name === agentName && a.owned === true)
}
function actionsFor(row) {
  return fileActions(row, { owned: ownsAgent(row.agent) })
}

// The recipient follows the participant SET (joined key, not array identity).
const participantsKey = computed(() => participants.value.join(' '))
watch(participantsKey, () => {
  if (!participants.value.includes(target.value)) target.value = participants.value[0] || null
}, { immediate: true })

// Small inline SVG file icon (picks a hue by type — one fact, shape + hue).
const FileIcon = (p) => {
  const t = (p.mime || '').toLowerCase()
  const cls = t.startsWith('image/') ? 'text-status-success-500'
    : t.includes('pdf') ? 'text-status-danger-500'
    : (t.startsWith('text/') || t.includes('json') || t.includes('csv')) ? 'text-action-primary-500'
    : 'text-gray-400'
  return h('svg', { class: `w-6 h-6 shrink-0 ${cls}`, fill: 'none', viewBox: '0 0 24 24', stroke: 'currentColor', 'aria-hidden': 'true' }, [
    h('path', { 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'stroke-width': 2, d: 'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z' }),
  ])
}

// ent#524: the whole selection, not `[0]`. This surface already HAD the drop
// gesture and dropped every file after the first — silently, with a success
// message — which is half the defect the issue was filed for. Both entry points
// now go through the shared batch, so per-file progress and per-file failure are
// the same here as on the conversation.
function onPick(e) {
  const files = e.target.files
  const p = uploadBatch(files)
  e.target.value = ''
  return p
}

async function uploadBatch(fileList) {
  const files = Array.from(fileList || [])
  if (!files.length) return
  const agent = targetName.value
  uploading.value = true
  uploadError.value = ''
  uploadOk.value = ''
  const sent = []
  const failed = []
  for (const file of files) {
    const rejection = rejectionFor(file)
    if (rejection) { failed.push(`${file.name}: ${rejection}`); continue }
    try {
      const res = await feeds.upload(agent, file)
      sent.push(res?.filename || file.name)
    } catch (err) {
      failed.push(`${file.name}: ${uploadFailureReason(err)}`)
    }
  }
  uploading.value = false
  // Both halves are stated. A batch that half-succeeded used to report only the
  // success, which is exactly how four dropped files became one with no notice.
  if (sent.length) {
    uploadOk.value = sent.length === 1
      ? `Sent “${sent[0]}” to ${agent}.`
      : `Sent ${sent.length} files to ${agent}.`
  }
  if (failed.length) uploadError.value = failed.join(' · ')
}

// ---- per-row verbs ----------------------------------------------------------

const rowErrors = ref({})
const busyKey = ref(null)
const busyVerb = ref(null)

function setRowError(key, message) {
  rowErrors.value = { ...rowErrors.value, [key]: message }
}
function clearRowError(key) {
  const next = { ...rowErrors.value }
  delete next[key]
  rowErrors.value = next
}

/**
 * The bytes of one row, for the preview and for Download.
 *
 * An agent share is a signed public URL (`?sig=…&download=1`) fetched
 * same-origin; an upload has NO DB row and therefore no URL, so it goes through
 * the authenticated portal route. Neither reuses `/files/preview`: that route is
 * platform-JWT-gated, reads only the agent container's `/home/developer`, and
 * serves `inline` unconditionally — so it can serve neither an external client
 * nor agent-shared bytes, which live on the backend host.
 */
async function loadBlob(row) {
  if (!row) throw new Error('no row')
  if (row.kind === 'upload') {
    return portal.fetchUploadBlob(row.agent, row.item.filename)
  }
  const res = await fetch(sameOriginPath(row.item.download_url, window.location.origin))
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.blob()
}

const previewIndex = ref(null)

function openPreview(row) {
  const i = allRows.value.findIndex((r) => r.key === row.key)
  if (i < 0) return
  // A non-previewable row still opens: ent#548's card (name / size / type +
  // Download) is the answer for a `.zip`, not a disabled name.
  previewIndex.value = i
}

/**
 * Save one row. TWO paths, and the split is the point of the server-side flag.
 *
 * An agent share has a signed URL that the server now serves
 * `Content-Disposition: attachment` (the one-way `?download=1`), so the browser
 * saves it from a plain anchor click — natively streamed, no memory spike, and
 * no programmatic blob save, which is the classic iOS Safari failure on a
 * mobile-first surface. That is what the flag is FOR; fetching the bytes here
 * only to hand them back would make it decorative and would pull up to 50 MB
 * into the tab to do it.
 *
 * A client upload has no URL at all — no DB row, no token — so it must go
 * through the authenticated portal route as a blob. There is no alternative
 * there, which is why the blob path exists at all.
 *
 * AC-3 also names the `download` ATTRIBUTE. It is set on the anchor below, and
 * it is honest to note that a browser ignores it cross-origin — which is
 * exactly why the server-side flag is the mechanism and the attribute is the
 * belt-and-braces, not the other way round.
 */
async function download(row) {
  if (!row) return
  clearRowError(row.key)

  if (row.kind !== 'upload') {
    const a = document.createElement('a')
    a.href = row.item.download_url
    a.download = row.item.filename || 'download'
    a.rel = 'noopener'
    document.body.appendChild(a)
    a.click()
    a.remove()
    return
  }

  busyKey.value = row.key
  busyVerb.value = 'download'
  let url = null
  try {
    const blob = await loadBlob(row)
    url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = row.item.filename || 'download'
    document.body.appendChild(a)
    a.click()
    a.remove()
  } catch (err) {
    setRowError(row.key, await errorDetail(err, "Couldn't download that file."))
  } finally {
    if (url) setTimeout(() => URL.revokeObjectURL(url), 0)
    busyKey.value = null
    busyVerb.value = null
  }
}

// ---- remove / revoke, confirmed once ----------------------------------------

const pending = ref(null)      // { row, verb }
const confirmOpen = computed({
  get: () => pending.value !== null,
  set: (v) => { if (!v) pending.value = null },
})

const CONFIRM_COPY = {
  delete: {
    title: 'Delete this file?',
    body: (name, agent) => `“${name}” will be removed from ${agent}'s inbox. The agent will no longer be able to read it.`,
    confirmText: 'Delete',
  },
  dismiss: {
    title: 'Remove this from your list?',
    // The consequence restated, and the session-type caveat with it: a viewer
    // (which a non-owner admin, and an owner on a portal token, both are)
    // cannot revoke, so "for you" is the honest word.
    body: (name) => `“${name}” will stop appearing in your Files list. It stays shared — the agent's owner can still see it, and so can anyone else it was shared with.`,
    confirmText: 'Remove',
  },
  revoke: {
    title: 'Delete this file for everyone?',
    body: (name) => `The download link for “${name}” stops working now, and the file is removed within a day. Everyone it was shared with loses access.`,
    confirmText: 'Delete for everyone',
  },
}

const confirmCopy = computed(() => {
  const p = pending.value
  if (!p) return { title: '', message: '', confirmText: 'Confirm' }
  const copy = CONFIRM_COPY[p.verb] || CONFIRM_COPY.dismiss
  return {
    title: copy.title,
    message: copy.body(p.row.item.filename, p.row.agent),
    confirmText: copy.confirmText,
  }
})

function ask(row, verb) {
  clearRowError(row.key)
  pending.value = { row, verb }
}

async function runPending() {
  const p = pending.value
  pending.value = null
  if (!p) return
  const { row, verb } = p
  busyKey.value = row.key
  busyVerb.value = verb
  try {
    if (verb === 'delete') {
      await portal.deleteUpload(row.agent, row.item.filename)
    } else {
      await portal.deleteDocument(row.agent, row.item.id, verb === 'revoke' ? 'everyone' : 'me')
    }
    // The list AND the dot both move (ent#548 AC 4): `refresh` bumps `version`,
    // which is what the seen-marking and the signal watch.
    if (previewIndex.value !== null && allRows.value[previewIndex.value]?.key === row.key) {
      previewIndex.value = null
    }
    await feeds.refresh({ uploads: true })
  } catch (err) {
    setRowError(row.key, await errorDetail(err, "Couldn't remove that file."))
  } finally {
    busyKey.value = null
    busyVerb.value = null
  }
}

function formatDate(iso) {
  try { return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) }
  catch { return iso }
}
</script>
