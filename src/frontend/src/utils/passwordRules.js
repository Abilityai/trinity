/**
 * The first-run password rules, client side (trinity-enterprise#709).
 *
 * A mirror of `src/backend/utils/password_validation.py` (OWASP ASVS 2.1) for
 * the inline hints only — the backend stays the boundary and names the rule
 * that failed. The common-password list is server-side only.
 */
export const MIN_PASSWORD_LENGTH = 12

export function passwordRequirements(p = '') {
  return [
    { id: 'length', label: `${MIN_PASSWORD_LENGTH}+ characters`, met: p.length >= MIN_PASSWORD_LENGTH },
    { id: 'upper', label: 'Uppercase letter', met: /[A-Z]/.test(p) },
    { id: 'lower', label: 'Lowercase letter', met: /[a-z]/.test(p) },
    { id: 'digit', label: 'Number', met: /[0-9]/.test(p) },
    { id: 'symbol', label: 'Symbol (!@#$…)', met: /[^A-Za-z0-9]/.test(p) },
  ]
}

export function meetsPasswordRequirements(p = '') {
  return passwordRequirements(p).every((r) => r.met)
}
