// Canonical Gemini Live voice list (#28) — the single frontend source of truth
// for both the AgentWorkspace per-session picker and the VoIP settings picker.
// Mirrors the backend `GEMINI_VOICE_NAMES` in src/backend/config.py; a backend
// unit test asserts the two lists agree so they can't silently drift.
export const DEFAULT_VOICE_NAME = 'Kore'

export const VOICES = [
  { id: 'Kore', label: 'Kore — Firm' },
  { id: 'Zephyr', label: 'Zephyr — Bright' },
  { id: 'Puck', label: 'Puck — Upbeat' },
  { id: 'Aoede', label: 'Aoede — Breezy' },
  { id: 'Charon', label: 'Charon — Informational' },
  { id: 'Fenrir', label: 'Fenrir — Excitable' },
  { id: 'Gacrux', label: 'Gacrux — Mature' },
]

export const VOICE_IDS = VOICES.map((v) => v.id)
