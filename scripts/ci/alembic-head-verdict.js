/**
 * Per-PR verdict for the pre-merge Alembic head watcher (#2533).
 *
 * Extracted from the workflow's inline `github-script` body so the one piece
 * of logic that can publish a FALSE ALL-CLEAR is executable by a test.
 * Everything here is pure — no `fs`, no `github`, no `core`, no network — so
 * the workflow keeps owning I/O and API calls and this owns only the decision.
 *
 * CommonJS on purpose: `actions/github-script` runs the inline body under
 * `require()`, so an ESM `export` here would be unloadable by the one caller
 * that matters, and it would fail at RUN time in the job that posts.
 *
 * Background (#2533): `schema-parity`'s single-head guard is correct but
 * STALE — it runs on the `pull_request` event, and GitHub recomputes
 * `refs/pull/N/merge` when the base advances without re-triggering workflows.
 * The watcher re-runs the same guard (`scripts/ci/check_alembic_heads.py`,
 * unmodified) over an in-memory `git merge-tree` of each open migration PR
 * against the live `dev` tip. This file turns one such run into a commit
 * status and a sticky comment.
 *
 * Two rules are load-bearing and are why the arms below are not symmetric:
 *
 *   1. Absence of a verdict is its own state (#2029). `conflict` and
 *      `unknown` post NO status — `success` would be a false all-clear on a
 *      check that never ran, and nothing corrects that; `failure` would blame
 *      the PR for something that is not this defect.
 *   2. A clean PR never GAINS a sticky comment. It only ever edits one that
 *      already exists, into a resolved body. Publishing a green comment on
 *      every migration PR is noise the signal then hides in.
 */

/** Marker that identifies this workflow's sticky comment. */
const MARKER = '<!-- alembic-head-watch -->';

/** Commit-status context. Deliberately NOT a required check — see the workflow header. */
const CONTEXT = 'alembic-head-watch';

/** GitHub truncates a commit-status description past this; do it ourselves so it stays legible. */
const MAX_DESCRIPTION = 140;

function truncate(text, limit = MAX_DESCRIPTION) {
  if (text.length <= limit) return text;
  return `${text.slice(0, limit - 1)}…`;
}

/**
 * Best-effort read of `check_alembic_heads.py`'s own output.
 *
 * The guard is reused UNMODIFIED (#2533), so this parses its printed shape
 * rather than asking it for structured output. That is brittle by nature, so
 * every caller must degrade gracefully: a parse that yields nothing still
 * produces a correct count-only verdict, never a wrong one and never a crash.
 * The full text is quoted verbatim in the comment regardless, so a reader
 * always has the authoritative answer even when this returns empty.
 *
 * @param {string} text combined stdout+stderr of one guard run
 * @returns {{heads: string[], forkPoint: string|null}}
 */
function parseGuardOutput(text) {
  const source = String(text || '');
  const heads = [];
  const headLine = /^\s*•\s+(\S+)/gm;
  let match;
  while ((match = headLine.exec(source)) !== null) heads.push(match[1]);
  const fork = /^They fork at:\s+(\S+)/m.exec(source);
  return { heads, forkPoint: fork ? fork[1] : null };
}

function forkDescription(heads) {
  const count = heads.length;
  const noun = count === 1 ? 'head' : 'heads';
  if (count === 0) {
    // The guard failed in a shape this parser did not recognise. Say exactly
    // that rather than inventing a number — the comment carries the real text.
    return 'Alembic head check failed when merged into dev — see the PR comment';
  }
  return truncate(`${count} ${noun} if merged into dev: ${heads.join(', ')}`);
}

function forkBody({ detail, heads, devHead, headSha, runUrl }) {
  const rechain = devHead
    ? `rechain this PR's revision off \`${devHead}\` (the current \`dev\` head)`
    : "rechain this PR's revision off the current `dev` head";
  const merge =
    heads.length >= 2
      ? `\`alembic merge -m "…" ${heads.slice(0, 2).join(' ')}\``
      : '`alembic merge -m "…" <head-a> <head-b>`';
  return [
    MARKER,
    '⚠️ **Alembic head fork if this PR is merged into `dev`.**',
    '',
    "`dev` has advanced since this PR's checks last ran. GitHub recomputes the merge ref when the " +
      'base moves but does **not** re-trigger workflows, so a green `schema-parity` here can describe ' +
      'a base that no longer exists (#2533).',
    '',
    '<details><summary><code>scripts/ci/check_alembic_heads.py</code> against <code>dev</code> + this PR, merged in memory</summary>',
    '',
    '```',
    String(detail || '').trim(),
    '```',
    '',
    '</details>',
    '',
    `**Fix**: ${rechain}, or — if the forked revision may already be applied somewhere — add a merge ` +
      `revision (${merge}), whose tuple \`down_revision\` converges the line from any starting state. ` +
      'See Architectural Invariant #3.',
    '',
    footer({ headSha, runUrl }),
  ].join('\n');
}

function conflictBody({ headSha, runUrl }) {
  return [
    MARKER,
    '🚧 **Alembic head check could not run — this PR conflicts with `dev`.**',
    '',
    '`git merge-tree` reported conflicts, so there is no merged tree to check. GitHub cannot compute ' +
      '`refs/pull/N/merge` in this state either, which is why a conflicting PR shows **no** checks at all.',
    '',
    'Merge `dev` into this branch and push. The head check re-runs automatically on the next push to ' +
      '`dev` touching `src/backend/migrations/versions/**`.',
    '',
    footer({ headSha, runUrl }),
  ].join('\n');
}

function resolvedBody({ devHead, headSha, runUrl }) {
  const head = devHead ? ` (\`${devHead}\`)` : '';
  return [
    MARKER,
    `✅ **Alembic head check clear** — merging this PR into \`dev\` leaves one head${head}.`,
    '',
    'Previously flagged; resolved.',
    '',
    footer({ headSha, runUrl }),
  ].join('\n');
}

function footer({ headSha, runUrl }) {
  const parts = ['_Advisory — this check does not block merge._'];
  if (headSha) parts.push(`head_sha: \`${headSha}\``);
  if (runUrl) parts.push(`[run](${runUrl})`);
  return parts.join(' · ');
}

/**
 * Decide what to publish for one evaluated PR.
 *
 * @param {object} result
 * @param {number} result.prNumber
 * @param {string} result.headSha       the SHA actually evaluated
 * @param {'clean'|'fork'|'conflict'|'unknown'} result.outcome
 * @param {string} [result.detail]      combined guard output (fork only)
 * @param {string} [result.devHead]     the head `dev` alone resolves to
 * @param {string} [result.runUrl]
 * @param {boolean} [result.dryRun]     `pull_request` self-test — evaluate, publish nothing
 * @returns {{status: object|null, comment: {body: string}|null, createIfMissing: boolean, summary: string}}
 */
function verdictFor(result) {
  const {
    prNumber,
    headSha,
    outcome,
    detail = '',
    devHead = null,
    runUrl = null,
    dryRun = false,
  } = result || {};

  const nothing = (summary) => ({
    status: null,
    comment: null,
    createIfMissing: false,
    summary,
  });

  // `unknown` is a leg that died before it produced an answer — a failed
  // fetch, a merge-tree that exited neither 0 nor 1, a refused extraction.
  // It must leave whatever is already on the PR exactly as it is (#2029).
  if (outcome === 'unknown') {
    return nothing(`PR #${prNumber}: no verdict — leg failed before the guard could run`);
  }

  if (outcome === 'conflict') {
    const decision = {
      // No status: `success` is a false all-clear on a check that never ran,
      // and `failure` blames this PR for a conflict, not for a head fork.
      status: null,
      comment: { body: conflictBody({ headSha, runUrl }) },
      createIfMissing: true,
      summary: `PR #${prNumber}: conflicts with dev — head check not evaluated`,
    };
    return dryRun ? { ...decision, status: null, comment: null } : decision;
  }

  if (outcome === 'fork') {
    const { heads } = parseGuardOutput(detail);
    const decision = {
      status: {
        state: 'failure',
        context: CONTEXT,
        description: forkDescription(heads),
        target_url: runUrl || undefined,
      },
      comment: { body: forkBody({ detail, heads, devHead, headSha, runUrl }) },
      createIfMissing: true,
      summary: `PR #${prNumber}: FORK — ${heads.length || '?'} heads when merged into dev`,
    };
    return dryRun ? { ...decision, status: null, comment: null } : decision;
  }

  if (outcome === 'clean') {
    const decision = {
      status: {
        state: 'success',
        context: CONTEXT,
        description: truncate('1 head when merged into dev'),
        target_url: runUrl || undefined,
      },
      // Body is built so an EXISTING sticky can be resolved. `createIfMissing`
      // is false, so a PR that was never flagged never gains a comment.
      comment: { body: resolvedBody({ devHead, headSha, runUrl }) },
      createIfMissing: false,
      summary: `PR #${prNumber}: clean — 1 head when merged into dev`,
    };
    return dryRun ? { ...decision, status: null, comment: null } : decision;
  }

  // An outcome this file does not recognise is a bug in its caller. Fail the
  // way `unknown` does — publish nothing — rather than guessing at a state.
  return nothing(`PR #${prNumber}: unrecognised outcome ${JSON.stringify(outcome)} — published nothing`);
}

module.exports = { verdictFor, parseGuardOutput, MARKER, CONTEXT, MAX_DESCRIPTION };
