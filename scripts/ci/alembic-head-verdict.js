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
 *
 *   3. Every value rendered here is ATTACKER-CONTROLLED. Revision ids and
 *      filenames come out of the PR's own files — any fork author on a public
 *      repo — and land in a comment that carries `github-actions[bot]`'s voice.
 *      So the guard output is fenced against its own backticks (`fenced`) and
 *      an id is only rendered into the copy-pasteable `alembic merge` command
 *      when it looks like an id (`isSafeRevisionId`).
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
 * A revision id we are willing to put inside a command a human may paste.
 *
 * EVERYTHING THIS MODULE RENDERS IS ATTACKER-CONTROLLED. The heads and the
 * quoted guard output are read out of the *PR's own* revision files — on a
 * public repo that means any fork author picks them — and the result is posted
 * as a comment authored by `github-actions[bot]`, which carries the repo's
 * voice. A revision id is `revision = "<any string>"`, so `\S+` (what
 * `parseGuardOutput` captures) happily includes `$(…)`, backticks and `;`.
 * The `alembic merge` line invites a maintainer to paste it into a shell, so an
 * id that does not look like an id is not rendered into that command at all —
 * the generic `<head-a> <head-b>` placeholder is the safe degradation, and the
 * verbatim guard output in the fenced block above it still names the real ids.
 *
 * Alembic ids are ≤255 (Invariant #3) and Trinity's are `NNNN_<table>_<change>`.
 */
const SAFE_REVISION_ID = /^[A-Za-z0-9._-]{1,255}$/;

function isSafeRevisionId(id) {
  return SAFE_REVISION_ID.test(String(id ?? ''));
}

/**
 * Fence `text` so its own backtick runs cannot terminate the block.
 *
 * CommonMark closes a fenced block on the first line whose leading run of
 * backticks is at least as long as the opening one, so a hard-coded ``` fence
 * around author-controlled text lets a revision id or filename containing a
 * newline plus ``` escape into the comment as live markdown — enough to forge
 * reassuring prose inside a bot-authored comment. Opening with one backtick
 * more than the longest run present makes that unreachable for any input.
 */
function fenced(text) {
  const body = String(text ?? '').trim();
  const longest = (body.match(/`+/g) || []).reduce((n, run) => Math.max(n, run.length), 0);
  const fence = '`'.repeat(Math.max(3, longest + 1));
  return [fence, body, fence];
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
  const pair = heads.slice(0, 2);
  const merge =
    pair.length >= 2 && pair.every(isSafeRevisionId)
      ? `\`alembic merge -m "…" ${pair.join(' ')}\``
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
    ...fenced(detail),
    '',
    '</details>',
    '',
    `**Fix**: ${rechain}, or — if the forked revision may already be applied somewhere — add a merge ` +
      `revision (${merge}), whose tuple \`down_revision\` converges the line from any starting state. ` +
      'See Architectural Invariant #3.',
    '',
    'Push the fix and `schema-parity` re-checks it against the merge ref immediately; this comment ' +
      'clears on the next push to `dev` touching `src/backend/migrations/versions/**`.',
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

/** Matches the run id inside an Actions run URL — the only per-run token in a body. */
const RUN_ID_IN_URL = /(\/actions\/runs\/)\d+/g;

/**
 * Is an existing sticky already saying the same thing as the new body?
 *
 * Load-bearing, and not `===` (#2533 M8). `footer()` embeds this run's URL,
 * whose id is unique to every run, so literal equality is UNREACHABLE — the
 * caller's "skip if unchanged" branch would be dead code and a flagged PR's
 * sticky would be rewritten on every sweep (~5/day), re-rendering a comment
 * that has not changed. Normalising the run id away makes the documented
 * behaviour real while keeping the link in the body, where a `conflict` — which
 * deliberately posts no status, so no `target_url` — has no other way to point
 * at the run that produced it.
 *
 * @param {string} existingBody body currently on the PR
 * @param {string} nextBody     body this run would write
 * @returns {boolean} true when the only difference is which run wrote it
 */
function stickyBodiesMatch(existingBody, nextBody) {
  const normalise = (body) => String(body || '').replace(RUN_ID_IN_URL, '$1<run>');
  return normalise(existingBody) === normalise(nextBody);
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

module.exports = {
  verdictFor,
  parseGuardOutput,
  stickyBodiesMatch,
  isSafeRevisionId,
  fenced,
  MARKER,
  CONTEXT,
  MAX_DESCRIPTION,
};
