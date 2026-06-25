// Client-side secret/PII scrubbing for in-app bug reports (#1116).
//
// Ported from the intake Worker's scrub rules (trinity-ops-agent/bug-intake/
// src/scrub.js). Reports land in the PUBLIC abilityai/trinity repo, so we scrub
// in the browser BEFORE the user reviews the payload, and the Worker scrubs
// again server-side as defense-in-depth. Keep the two rule sets aligned.

const REDACTED = '[REDACTED]';
const EMAIL_MASK = '[email]';
const IP_MASK = '[private-ip]';

const RULES = [
  [/Bearer\s+[A-Za-z0-9._~+/-]+=*/gi, `Bearer ${REDACTED}`],
  [/Authorization:\s*\S+/gi, `Authorization: ${REDACTED}`],
  [/trinity_mcp_[A-Za-z0-9]+/g, REDACTED],
  [/sk-ant-[A-Za-z0-9_-]+/g, REDACTED],
  [/sk-[A-Za-z0-9_-]{16,}/g, REDACTED],
  [/gh[pos_ru]_[A-Za-z0-9]{20,}/g, REDACTED],
  [/github_pat_[A-Za-z0-9_]{20,}/g, REDACTED],
  [/xox[baprs]-[A-Za-z0-9-]+/g, REDACTED],
  [/AIza[0-9A-Za-z_-]{35}/g, REDACTED],
  [/AKIA[0-9A-Z]{16}/g, REDACTED],
  [/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, REDACTED],
  [/([?&](?:sig|token|access_token|api_key|apikey|key|password|secret|auth)=)[^&\s"'<>]+/gi, `$1${REDACTED}`],
  [/(https?:\/\/)[^/\s:@]+:[^/\s:@]+@/gi, `$1${REDACTED}@`],
  [/((?:password|passwd|secret|api[_-]?key|apikey|access[_-]?token|auth[_-]?token|client[_-]?secret|private[_-]?key|credential)["']?\s*[:=]\s*["']?)[^\s"',&}{]+/gi, `$1${REDACTED}`],
  [/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g, EMAIL_MASK],
  [/\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b/g, IP_MASK],
];

/**
 * Scrub a single string. Non-strings return ''.
 * @param {unknown} text
 * @returns {string}
 */
export function scrub(text) {
  if (typeof text !== 'string' || text.length === 0) return '';
  let out = text;
  for (const [pattern, replacement] of RULES) out = out.replace(pattern, replacement);
  return out;
}

/** Scrub an array of strings (e.g. console lines). */
export function scrubLines(lines) {
  return Array.isArray(lines) ? lines.map(scrub) : [];
}
