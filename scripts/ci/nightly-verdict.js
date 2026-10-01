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

const SHA_RE = /^[0-9a-f]{40}$/;

/**
 * Build per-leg statuses from the TRUSTED `discover` matrix (#3103).
 *
 * The test job runs PR code, and that code can forge anything the job writes:
 * the status JSON, the artifact's file name, even the artifact itself (it can
 * read the runner's upload token). So the identity of a leg — which PR, which
 * seed, which head SHA — comes only from the matrix `discover` built before
 * any PR code ran. The artifact contributes two booleans and nothing else, and
 * a status whose identity fields disagree with the matrix is rejected rather
 * than trusted. A rejected or absent leg is simply missing, which
 * `verdictFor` already reports as unverified.
 *
 * `seed` is optional: integration-nightly runs one leg per PR and its matrix
 * carries none. A seedless entry skips the seed check and yields a leg with
 * no `seed` field.
 *
 * @param {{include: Array<{pr_number: number, seed?: string, head_sha: string}>}} matrix
 * @param {(pr: number, seed: string|undefined) => string|null} readStatus raw JSON text, or null when absent
 * @returns {{legs: object[], rejected: Array<{pr: number, seed: string, reason: string}>}}
 */
function legsFromMatrix(matrix, readStatus) {
  const legs = [];
  const rejected = [];
  for (const entry of (matrix && matrix.include) || []) {
    const pr = Number(entry.pr_number);
    const seed = entry.seed === undefined ? undefined : String(entry.seed);
    const reject = (reason) => rejected.push({ pr, seed, reason });

    if (!Number.isInteger(pr) || pr <= 0) { reject('matrix pr_number is not a positive integer'); continue; }
    if (!SHA_RE.test(String(entry.head_sha))) { reject('matrix head_sha is not a 40-char hex SHA'); continue; }

    const raw = readStatus(pr, seed);
    if (raw === null || raw === undefined) continue;

    let s;
    try {
      s = JSON.parse(raw);
    } catch (err) {
      reject(`status JSON does not parse: ${err.message}`);
      continue;
    }
    if (!s || typeof s !== 'object') { reject('status JSON is not an object'); continue; }
    if (Number(s.pr_number) !== pr) { reject(`status pr_number ${JSON.stringify(s.pr_number)} disagrees with the matrix`); continue; }
    if (seed !== undefined && String(s.seed) !== seed) { reject(`status seed ${JSON.stringify(s.seed)} disagrees with the matrix`); continue; }
    if (s.head_sha !== entry.head_sha) { reject('status head_sha disagrees with the matrix'); continue; }
    if (typeof s.merge_conflict !== 'boolean' || typeof s.regression !== 'boolean') {
      reject('merge_conflict/regression are not booleans');
      continue;
    }

    legs.push({
      pr_number: pr,
      ...(seed === undefined ? {} : { seed }),
      head_sha: entry.head_sha,
      merge_conflict: s.merge_conflict,
      regression: s.regression,
    });
  }
  return { legs, rejected };
}

module.exports = { verdictFor, groupByPr, legsFromMatrix }
