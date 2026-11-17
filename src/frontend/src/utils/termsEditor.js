/**
 * Terms editor rules (trinity-enterprise#1107) — the client-side half of the
 * server's named refusals (`terms_service.validate_text` / `validate_title`),
 * so the Settings panel names the problem before the request; the server
 * still decides.
 */
export const TERMS_TEXT_MAX_BYTES = 200_000
export const TERMS_TITLE_MAX_CHARS = 200

/** `{ field, message }` naming what is wrong and what to do, or null. */
export function termsClientError({ title, text }) {
  if (!String(text ?? '').trim()) {
    return { field: 'text', message: 'The text is empty. Paste the terms (Markdown), or restore the default.' }
  }
  if (new TextEncoder().encode(String(text)).length > TERMS_TEXT_MAX_BYTES) {
    return { field: 'text', message: `The text is over ${TERMS_TEXT_MAX_BYTES / 1000} KB. Shorten it, or link to the full document.` }
  }
  if (String(title ?? '').trim().length > TERMS_TITLE_MAX_CHARS) {
    return { field: 'title', message: `The title is over ${TERMS_TITLE_MAX_CHARS} characters. Use a short title, e.g. "Terms of use".` }
  }
  return null
}
