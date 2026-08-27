/**
 * Reading `GET /api/settings/ops/config` (#2411).
 *
 * The payload is nested TWICE, and the toggle in `Settings.vue` read it flat:
 *
 *     { settings: { ssh_access_enabled: { value: "true", default: "false",
 *                                         description: "...", is_default: false } } }
 *
 * `response.data.ssh_access_enabled` is therefore `undefined`, `undefined ===
 * 'true'` is `false`, and the SSH toggle rendered OFF no matter what was
 * stored. Worse, the click handler computes `!sshAccessEnabled.value`, so with
 * the state pinned to `false` the FIRST click always sent `true` — an operator
 * with ephemeral SSH enabled, clicking to disable it, re-enabled it and then
 * watched it render as off. The write path was always correct; only the read
 * was broken, which is why a change appeared to stick for the session and
 * silently reverted on reload.
 *
 * The rule lives here rather than inline because `Settings.vue` cannot be
 * mounted in this project's test setup (`@vue/test-utils` is not a dependency
 * and vitest runs `environment: 'node'`), so a rule kept in the SFC is a rule
 * no test can reach — which is exactly how a one-line read bug survived in a
 * security control. Issue #2411's own suggested fix,
 * `response.data.settings?.ssh_access_enabled`, is still wrong for the same
 * underlying reason: that resolves to the descriptor OBJECT, and
 * `object === 'true'` is `false`. The `.value` hop is the one that matters.
 */

/**
 * Read one ops setting as a boolean, from either payload shape.
 *
 * Accepts the descriptor form the endpoint actually returns
 * (`{ value: "true", … }`) and a bare `"true"`, because the only thing that
 * distinguishes them is a wrapper this reader does not need — and being
 * tolerant here costs nothing while removing a whole class of one-sided
 * mismatch.
 *
 * Everything unreadable degrades to `false`: absent key, absent `settings`,
 * a null payload, a number, an unexpected object. `false` is the SAFE
 * direction for every flag in this group — `ssh_access_enabled` defaults to
 * `"false"` server-side, and a security control that cannot read its own state
 * must not claim the permissive one.
 */
export function readOpsBool(payload, key) {
  const entry = payload?.settings?.[key]
  const raw = entry !== null && typeof entry === 'object' ? entry.value : entry
  // Only a string can be `"true"`; a real boolean `true` is accepted too, since
  // the column is TEXT today but nothing here should break if that changes.
  if (raw === true) return true
  return typeof raw === 'string' && raw.trim().toLowerCase() === 'true'
}

/**
 * The value to SEND for a boolean ops setting. `PUT /ops/config` takes strings
 * (`validate_ops_setting` requires `'true'`/`'false'` for a `bool` key), and
 * pairing the writer with the reader here keeps the two spellings in one file
 * rather than at opposite ends of a 3500-line SFC.
 */
export function opsBoolValue(enabled) {
  return enabled ? 'true' : 'false'
}
