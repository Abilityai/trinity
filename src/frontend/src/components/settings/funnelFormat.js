/**
 * Pure decisions for the activation-funnel panel (ent#184 → ent#545).
 *
 * The enterprise `GET /api/enterprise/telemetry/funnel` answers
 * `installation_id` as a string once the install's id has been minted and as
 * `null` before — since ent#545 the read never mints (a GET used to create the
 * identity row, so the footer could never be empty; now it can, and it has to
 * say so). Kept pure and here so it is unit-testable under the node-only
 * vitest config (`vitest.config.js` pins `environment: 'node'`, no component
 * mounting); the panel renders what these functions return.
 */

/**
 * The footer's leading phrase when the backend says no install id exists yet
 * (`null`). Names how one comes to exist so the state is not a dead end — and
 * names the action an operator can actually take: the updates opt-in on this
 * same Settings page (General → Security & product updates). The other writer,
 * the onboarding wizard's first product event, only fires on an empty fleet or
 * an explicit `?onboarding=1`, so it is not offered as the way.
 */
export const INSTALL_ID_NOT_MINTED =
  'No install id yet — one is minted when this instance opts in to security & product updates (Settings → General)'

/**
 * The footer's leading phrase when the wire carried something that is neither
 * an id nor the explicit `null`: an absent key, a blank, a non-string. That is
 * not a claim about minting, so it must not read as one.
 */
export const INSTALL_ID_UNAVAILABLE = 'Install id unavailable'

/**
 * `{ state, text }` for the footer. Three honest states, never a blank
 * "Install  ·":
 *   - `minted`      — a non-blank string: `Install <id>` (trimmed). A pre-bump
 *                     enterprise backend, still minting, always lands here, so
 *                     the panel renders across the two-repo landing window.
 *   - `not_minted`  — exactly `null`, the backend's own "nothing has minted it".
 *   - `unavailable` — anything else; an absent key or a malformed value is not
 *                     evidence that the id does not exist.
 */
export function installIdFooter(installationId) {
  if (installationId === null) {
    return { state: 'not_minted', text: INSTALL_ID_NOT_MINTED }
  }
  if (typeof installationId === 'string' && installationId.trim() !== '') {
    return { state: 'minted', text: `Install ${installationId.trim()}` }
  }
  return { state: 'unavailable', text: INSTALL_ID_UNAVAILABLE }
}
