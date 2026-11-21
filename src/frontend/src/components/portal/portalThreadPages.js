// trinity-enterprise#594 — a Workspace thread read past its window.
//
// The thread read is a WINDOW (the newest 100 typed turns plus the spoken rows
// of the calls among them, under a row ceiling — #2694); "Show earlier" reads
// the next older window with the `?before=` cursor. These are the two ways the
// transcript's rows (`historyRow`-mapped, oldest-first) change shape with
// pages on screen, kept pure so the component holds no array surgery of its own.
//
// Both re-pair overlapping turns (#3166) across the page seam: a reply whose
// question is on the other page could not be paired by the per-page read.
import { pairRepliesWithQuestions } from './portalUtils'

const pairMapped = (rows) => pairRepliesWithQuestions(rows, { executionIdOf: (m) => m?.executionId })

// An older page goes above what is on screen. A row already on screen (the two
// reads overlap only by accident, but a duplicate bubble would be a defect) is
// kept once, in its on-screen copy.
export function prependEarlier(existing = [], older = []) {
  const have = new Set(existing.map((m) => m?.id).filter(Boolean))
  const fresh = older.filter((m) => !m?.id || !have.has(m.id))
  return pairMapped([...fresh, ...existing])
}

// A reload of the NEWEST window for the thread already on screen (a call
// ended, a session was adopted): keep every row above the first one the window
// also holds, then the window — so earlier pages survive and every row the
// window covers is the server's fresh copy. A window sharing no row with the
// screen cannot be placed, and says so (`reset`): the caller starts over.
export function mergeWindow(existing = [], freshWindow = []) {
  const freshIds = new Set(freshWindow.map((m) => m?.id).filter(Boolean))
  const cut = existing.findIndex((m) => m?.id && freshIds.has(m.id))
  if (cut === -1) return { rows: [...freshWindow], reset: true }
  return { rows: pairMapped([...existing.slice(0, cut), ...freshWindow]), reset: false }
}
