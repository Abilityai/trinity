/**
 * Per-PR verdict for the nightly cross-PR regression sweep (#2462).
 *
 * Extracted from the `comment` job's inline `github-script` body so the one
 * piece of new logic that can produce a FALSE ALL-CLEAR is executable by a
 * test. Everything here is pure — no `fs`, no `github`, no `core` — so the
 * workflow keeps owning I/O and API calls and this owns only the decision.
 *
 * CommonJS on purpose: `actions/github-script` runs the inline body under
 * `require()`, so an ESM `export` here would be unloadable by the one caller
 * that matters. Vitest imports it through CJS interop.
 *
 * Background: legs are per (PR, seed) since #2462, because the previous shape
 * ran six full suites in one 25-minute job and was cancelled every night for
 * twelve consecutive nights. A PR's verdict is therefore the union of its
 * seeds, and "how many seeds make a complete answer" is a question this file
 * must answer the same way `discover` builds the matrix.
 */

/**
 * @param {Array<{seed: string|number, regression: boolean, merge_conflict: boolean, head_sha: string}>} legs
 * @param {Array<string>} expectedSeeds
 * @returns {{status: 'unverified'|'merge_conflict'|'regression'|'clean', missing?: string[], regressed?: object[], headSha?: string, total?: number}}
 */
function verdictFor(legs, expectedSeeds) {
  const seen = new Set((legs || []).map((l) => String(l.seed)));
  const missing = (expectedSeeds || []).filter((s) => !seen.has(String(s)));

  // An INCOMPLETE set is not a clean one. #2029's rule — absence of a verdict
  // is its own state — applies per seed: two green seeds and one that never
  // reported says nothing about the third, and a tick published on that
  // partial answer is the false all-clear the rule exists to prevent.
  //
  // Checked FIRST, before the conflict and regression arms. A missing seed
  // means we do not know what that seed would have said, and that is true
  // whatever the seeds that did report happen to say.
  if (missing.length > 0) return { status: 'unverified', missing };

  // Reachable only when expectedSeeds is empty AND no legs reported, i.e. a
  // misconfiguration rather than a result. Guarded explicitly so it cannot
  // fall through to `clean` on an empty array.
  if (legs.length === 0) return { status: 'unverified', missing: [] };

  const headSha = legs[0].head_sha;

  // A conflict is not a test result — the suite never ran — so it outranks the
  // regression arm rather than being merged with it.
  if (legs.some((l) => l.merge_conflict)) {
    return { status: 'merge_conflict', headSha, total: legs.length };
  }

  // ANY seed is enough to condemn. pytest-randomly is here precisely because
  // an order-dependent failure appears under one seed and not another, so "one
  // seed regressed" IS the finding — averaging it away would delete the signal
  // the seeds exist to produce.
  const regressed = legs.filter((l) => l.regression);
  if (regressed.length > 0) {
    return { status: 'regression', headSha, regressed, total: legs.length };
  }

  return { status: 'clean', headSha, total: legs.length };
}

/** Group per-leg status objects by PR number, input order preserved. */
function groupByPr(statuses) {
  const byPr = new Map();
  for (const s of statuses || []) {
    const pr = s.pr_number;
    if (!byPr.has(pr)) byPr.set(pr, []);
    byPr.get(pr).push(s);
  }
  return byPr;
}

module.exports = { verdictFor, groupByPr }
