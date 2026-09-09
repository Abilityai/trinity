"""What a gitignore sweep DID, and how it is reported (#2529).

Split out of `gitignore.py` (#1028): #2529 added ~700 lines to that module and
pushed it past the 800-line critical threshold this package exists to keep
everything under. The seam is a real one rather than a size-driven cut —
`gitignore.py` owns what the file CONTAINS and the commands that write it;
this module owns the answer to *what did that just do*: the tagged probe output
git emits, the record parsed out of it, and the three places that record is
reported (an operator alarm, the commit message, the sync result).

Pure of Docker and of the filesystem, which is what makes the parser testable
against a captured blob. `gitignore.py` reaches these through the module object
(`gitignore_sweep.<name>`), never a from-import — a from-import freezes the
binding and quietly detaches a test that patches the owning module.
"""
import re
import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, Any, List, Tuple

from database import db, GitSyncResult
from utils.credential_sanitizer import scrub_secret_and_urls

logger = logging.getLogger(__name__)


# Per-line output tags for the sweep probes (#2529). `container_exec_run` does
# NOT pass `demux=True` (docker_utils.py), so stdout and stderr arrive
# interleaved in ONE blob — a begin/end region parser would happily swallow a
# stray `grep: ...` line as a payload path. A per-line prefix cannot: anything
# without the tag is not ours, wherever it lands.
_SWEEP_TAG = "TRINITY-2529"
_SWEEP_TAG_BEFORE = f"{_SWEEP_TAG}-untracked-before: "
_SWEEP_TAG_AFTER = f"{_SWEEP_TAG}-untracked-after: "
_SWEEP_TAG_REMOVED = f"{_SWEEP_TAG}-removed: "
_SWEEP_TAG_REMOVED_COUNT = f"{_SWEEP_TAG}-removed-count: "
_SWEEP_TAG_SHADOW = f"{_SWEEP_TAG}-shadow: "


# Every probe list is line-capped in-container. `container_exec_run` reads the
# whole exec output into memory as one blob, and both probed sets are unbounded
# in exactly the cases this code exists for:
#
#   * the untracked probe runs BEFORE the canonical block is written on a
#     pre-canonical agent, so nothing is ignored yet and `$HOME` answers with
#     every file under `.local/lib/python3.13/site-packages`, `.npm`, `.cache`;
#   * `$ignored` is tens of thousands of paths on the #1596 population — an
#     agent with a committed `node_modules/` (44 GB repos were observed) — which
#     is precisely the fleet this migration was written for.
#
# So the cap is not paranoia. `head -n CAP+1` makes truncation self-announcing
# (a full CAP+1 lines means "there were more"), and the REMOVED count is emitted
# separately and exactly, so a capped list never turns into an undercounted
# claim about how many files a Push untracked.
_SWEEP_PROBE_LINE_CAP = 2000


# `git check-ignore -v` prints `<source>:<line>:<pattern>\t<pathname>`. The
# source is a filename and the pattern may itself contain a colon, so the split
# is anchored on the LAST `:<digits>:` rather than on the first colon.
_CHECK_IGNORE_RE = re.compile(r"^(?P<source>.*):(?P<line>\d+):(?P<pattern>.*)\t(?P<path>.*)$")


@dataclass(frozen=True)
class GitignoreSweep:
    """What a Push's `.gitignore` migration actually did (#2529).

    Every field is ADVISORY reporting — a failure to compute any of them returns
    the empty sweep and never breaks the Push.

    removed:   paths this Push untracked (`git rm --cached`). The honest answer
               to "5 files pushed" vs "5 files pushed, 4 deletions".
    unignored: paths that became newly un-ignored and are still untracked — the
               inverted-duplicate case (a user's `!.env.production` above their
               own `.env.*`), which the SAME Push then stages via `git add -A`.
               KNOWN CONTAMINATION: this is `after - before` across two execs
               against a LIVE container, so a file the agent's own session
               creates in that window is reported here too. Folding both probes
               into the two execs the Push already ran narrows the window; it
               does not close it.
    shadowed:  `"!rule -> deciding managed pattern"` for every agent negation a
               managed line defeats. Two residuals live here: a negation under a
               dir-form canonical pattern (git does not descend into an excluded
               directory, so it is inert at ANY position) and a negation the
               protected floor deliberately refuses.
    """

    removed: Tuple[str, ...] = ()
    unignored: Tuple[str, ...] = ()
    shadowed: Tuple[str, ...] = ()
    #: How many paths were ACTUALLY untracked. Equals ``len(removed)`` unless
    #: the in-container list hit ``_SWEEP_PROBE_LINE_CAP``, in which case the
    #: list is a prefix and this is still exact — an undercounted "untracked N
    #: file(s)" would be a false claim on the #1596 population, where N is
    #: routinely five figures.
    removed_total: int = 0

    @property
    def removed_count(self) -> int:
        """Exact number untracked — the list may be a `_SWEEP_PROBE_LINE_CAP` prefix."""
        return max(self.removed_total, len(self.removed))

    @property
    def changed_tracking(self) -> bool:
        """Did this Push change WHAT IS IN THE REPO, in either direction?

        The gate for every reporting surface. It is deliberately not
        ``bool(self.removed)``: review reproduced the mirror image of the bug
        #2529 fixes — hoisting the defaults block above the user region can
        newly UN-ignore a path an agent had negated, and the same Push's
        `git add -A` then COMMITS it. `removed`-only gates made that addition
        exactly as silent as the deletions this issue exists to end.
        `shadowed` is NOT in the gate: it is standing advice about the file,
        not a change this Push made.
        """
        return bool(self.removed or self.unignored)

    def summary_line(self) -> str:
        """One line for a commit message / an HTTP error detail, or ``""``."""
        clauses = []
        if self.removed:
            clauses.append(
                f"untracked {self.removed_count} file(s) that now match "
                ".gitignore (working tree untouched)"
            )
        if self.unignored:
            clauses.append(
                f"newly un-ignored {len(self.unignored)} path(s), now committed "
                "by this same sync"
            )
        if not clauses:
            return ""
        return "Trinity: " + "; ".join(clauses)


def _tagged_output_lines(output: Any, tag: str) -> List[str]:
    """Every line of a probe blob carrying ``tag``, with the tag stripped.

    Anything without the tag — an interleaved stderr line, git's own chatter —
    is simply not ours. `container_exec_run` does not demux, so this is the
    whole defence against a stray `grep: ...` being read as a path.
    """
    if not isinstance(output, str):
        return []
    return [
        line[len(tag):].rstrip("\r")
        for line in output.splitlines()
        if line.startswith(tag)
    ]


def _shadowed_negations(check_ignore_lines: List[str]) -> Tuple[str, ...]:
    """Turn ``git check-ignore -v`` output into ``"!rule -> deciding pattern"``.

    THE TRAP: ``git check-ignore -v`` exits **0 even when the deciding rule is
    itself a negation** — i.e. even when the path is NOT ignored. (Verified:
    `.env.example` gives plain_rc=1 but verbose_rc=0.) So the verdict must come
    from the deciding PATTERN TEXT — a leading ``!`` means the negation won —
    never from the exit code.

    Only rules THIS PLATFORM wrote are reported. An agent whose own broad rule
    defeats its own negation is its own business; a managed line defeating it is
    ours to explain, and covers both residuals #2529 knowingly leaves: a
    negation under a dir-form canonical pattern (inert at any position, because
    git does not descend into an excluded directory) and a negation the
    protected floor deliberately refuses.
    """
    # Deliberately a function-local import: `gitignore` imports THIS module at
    # its top level (#1028), so a module-level `from . import gitignore` here
    # would close the cycle at import time. The one constant we need is read
    # off the module object at call time, which is also the monkeypatch rule.
    from . import gitignore

    managed = set(gitignore._GITIGNORE_MANAGED_LINES)
    out: List[str] = []
    for line in check_ignore_lines:
        match = _CHECK_IGNORE_RE.match(line)
        if not match:
            continue
        pattern = match.group("pattern")
        if pattern.startswith("!"):
            continue  # the negation WON — nothing to report
        if pattern not in managed:
            continue  # the agent's own rule, not ours
        entry = f"!{match.group('path')} -> {pattern}"
        if entry not in out:
            out.append(entry)
    return tuple(out)


def _parse_gitignore_sweep(merge_output: Any, sweep_output: Any) -> GitignoreSweep:
    """Assemble a :class:`GitignoreSweep` from the two execs' interleaved blobs."""
    before = _tagged_output_lines(merge_output, _SWEEP_TAG_BEFORE)
    after = _tagged_output_lines(sweep_output, _SWEEP_TAG_AFTER)
    removed = tuple(
        dict.fromkeys(_tagged_output_lines(sweep_output, _SWEEP_TAG_REMOVED))
    )

    # The exact count is emitted separately (`wc -l` on the full list), so a
    # capped list never becomes an undercounted claim about how many files this
    # Push untracked. Fall back to the list length when the count line is
    # missing or unparseable — a wrong count is worse than a conservative one.
    total = len(removed)
    for raw in _tagged_output_lines(sweep_output, _SWEEP_TAG_REMOVED_COUNT):
        try:
            total = max(total, int(raw.strip()))
        except ValueError:
            logger.warning("unparseable sweep removed-count %r — using the list length", raw)

    # `unignored` is a SET DIFFERENCE, so a truncated operand would manufacture
    # entries that are only "new" because the other side was cut off. Both
    # probes announce truncation by returning the full cap+1 lines; when either
    # does, drop the field rather than report a fiction. It is advisory anyway.
    if len(before) > _SWEEP_PROBE_LINE_CAP or len(after) > _SWEEP_PROBE_LINE_CAP:
        logger.warning(
            "gitignore sweep: the untracked probe hit its %s-line cap "
            "(before=%s, after=%s) — unignored_paths suppressed rather than "
            "computed from a truncated set",
            _SWEEP_PROBE_LINE_CAP, len(before), len(after),
        )
        unignored: Tuple[str, ...] = ()
    else:
        unignored = tuple(sorted(set(after) - set(before)))

    return GitignoreSweep(
        removed=removed,
        removed_total=total,
        unignored=unignored,
        shadowed=_shadowed_negations(
            _tagged_output_lines(sweep_output, _SWEEP_TAG_SHADOW)
        ),
    )


def _coerce_sweep(value: Any) -> GitignoreSweep:
    """Anything that is not a real :class:`GitignoreSweep` becomes the empty one.

    Required, not defensive noise: `tests/unit/test_ent123_tokenless_clone.py`
    patches `_migrate_workspace_gitignore` with a bare `AsyncMock()`, and a
    `MagicMock` landing in a `List[str]` response field fails Pydantic
    validation at the very return the test is asserting on.
    """
    return value if isinstance(value, GitignoreSweep) else GitignoreSweep()


async def _emit_gitignore_untracked_alert(
    agent_name: str, sweep: GitignoreSweep
) -> None:
    """File an operator-queue entry naming the paths a Push untracked (#2529).

    THE surface that outlives the session. Every other one — the API response,
    the MCP result, the toast, the commit message — is read by whoever ran the
    Push, and BOTH confirmed field incidents were unattended 15-minute auto-sync
    cycles whose damage surfaced two months later. `sync_health_service`'s
    `git_bloat` entry (#1595) is the precedent for exactly this reasoning.

    Routed through the #1677 BUDGET seam, not `db.create_operator_queue_item`.
    The sibling `git_bloat`/`sync_failing` emitters are direct creates because
    their cadence is the 60-second platform poller's; this one fires from
    `sync_to_github`, which an AGENT can drive — `git_sync` is an MCP tool an
    agent-scoped key may call on itself. A repeated `git add -f <ignored>` +
    sync loop yields a fresh `removed` set every time, and the id is timestamped
    rather than idempotent, so nothing upstream bounds the volume. That is the
    `_alert_skill_not_found` shape (#1410) the budget seam exists for, and
    `gitignore-untracked-` joins `_RESERVED_ID_PREFIXES` so an agent cannot
    pre-create the id and silently suppress its own alert via the sink's
    `on_conflict_do_nothing` (the C2 class).

    Best-effort by construction: `create_bounded_alert` never raises and returns
    False when refused, and this still swallows, because an alerting failure
    must not fail a Push either.
    """
    if not sweep.changed_tracking:
        return
    try:
        from services.operator_queue_service import create_bounded_alert
        from utils.helpers import utc_now_iso

        now = utc_now_iso()
        shown = list(sweep.removed[:20])
        gained = list(sweep.unignored[:20])

        # Both directions, named separately, because the operator response
        # differs: a removal is recoverable from disk, an addition is already
        # in the remote's history and may need a credential rotated.
        parts = []
        if sweep.removed:
            parts.append(
                f"removed {sweep.removed_count} file(s) from the index because "
                "they match an ignore rule (the working tree is untouched, but "
                "the deletion is committed and pushed) — if one was meant to "
                "stay, negate it in the agent's own `.gitignore`, below the "
                "managed defaults block, and re-add it with `git add -f`"
            )
        if sweep.unignored:
            parts.append(
                f"newly UN-ignored {len(sweep.unignored)} path(s) that this same "
                "sync then committed — an agent rule now beats a managed default "
                "that previously hid them; if any is a secret, ROTATE it and "
                "remove the rule, because it is already in the remote's history"
            )
        if sweep.removed and sweep.unignored:
            title = "Push changed which files are tracked (.gitignore sweep)"
        elif sweep.removed:
            title = "Push untracked files that now match .gitignore"
        else:
            title = "Push committed files that were previously gitignored"

        # An unignored-ONLY entry drops to `medium`, and the reason is the
        # contamination `GitignoreSweep.unignored` documents: it is
        # `after - before` across two execs against a LIVE container, so a file
        # the agent's own session happens to create in that window is reported
        # here too. A removal is a confirmed destructive act and keeps `high`;
        # an addition is "look at this" and can be a false positive, and a band
        # that cries wolf stops being read. Volume is bounded either way by the
        # #1677 budget seam.
        priority = "high" if sweep.removed else "medium"

        await create_bounded_alert(
            agent_name,
            {
                "id": f"gitignore-untracked-{agent_name}-{now}",
                "agent_name": agent_name,
                "type": "gitignore_untracked",
                "status": "pending",
                "priority": priority,
                "title": title,
                "question": f"{agent_name}: this Push " + "; and it ".join(parts) + ".",
                "context": {
                    "removed_paths": shown,
                    "removed_count": sweep.removed_count,
                    "shadowed_negations": list(sweep.shadowed[:20]),
                    "unignored_paths": gained,
                    "unignored_count": len(sweep.unignored),
                },
                "created_at": now,
            },
        )
        logger.warning(
            "gitignore_untracked emitted for %s: %s untracked (%s); "
            "%s newly un-ignored (%s)",
            agent_name, sweep.removed_count, ", ".join(shown) or "-",
            len(sweep.unignored), ", ".join(gained) or "-",
        )
    except Exception:
        logger.exception("failed to emit gitignore_untracked alert")


def _augment_commit_message(message: Optional[str], sweep: GitignoreSweep) -> Optional[str]:
    """Name the untracked paths in the commit that carries their deletion.

    BEST-EFFORT, and the honest reason is worth stating: `git rm --cached` only
    STAGES. If this Push does not reach its own commit — or if the in-container
    auto-sync loop commits first, since the backend's `docker exec` runs outside
    the agent server's `_REPO_LOCK` — the deletions ride in someone else's commit
    with someone else's message. That is exactly what `47efd80` was. The
    operator-queue entry, not this, is the surface that does not depend on who
    commits.

    When the caller supplied no message we reproduce the agent server's own
    default (`Trinity sync: <ts>`, `agent_server/routers/git.py`) rather than
    dropping it, because supplying a message at all suppresses that default.
    """
    if not sweep.changed_tracking:
        return message
    subject = message or f"Trinity sync: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    body = ["", ""]

    if sweep.removed:
        total = sweep.removed_count
        shown = list(sweep.removed[:20])
        body.append(
            f"Trinity: untracked {total} file(s) that now match "
            ".gitignore (working tree untouched):"
        )
        body += [f"- {path}" for path in shown]
        if total > len(shown):
            body.append(f"- ... and {total - len(shown)} more")

    if sweep.unignored:
        # The commit that ADDS them is this one, so naming them here is the
        # only place the addition is visible in the repo's own history.
        gained = list(sweep.unignored[:20])
        if sweep.removed:
            body.append("")
        body.append(
            f"Trinity: newly un-ignored {len(sweep.unignored)} path(s), "
            "committed by this sync:"
        )
        body += [f"+ {path}" for path in gained]
        if len(sweep.unignored) > len(gained):
            body.append(f"+ ... and {len(sweep.unignored) - len(gained)} more")

    return subject + "\n".join(body)


def _with_sweep(result: GitSyncResult, sweep: GitignoreSweep) -> GitSyncResult:
    """Attach the sweep to a `GitSyncResult`, on the failure paths too.

    The router raises `HTTPException(detail=result.message)` for 409/400 and
    keeps NOTHING else, so a structured field alone is dead on exactly the paths
    where the index mutation has already happened. Folding the one-line summary
    into `message` here makes all four returns honest with one edit instead of
    per-status-code special-casing in the router.
    """
    summary = sweep.summary_line()
    message = result.message
    if summary and summary not in (message or ""):
        message = f"{message} — {summary}" if message else summary
    return result.model_copy(
        update={
            "message": message,
            "removed_paths": list(sweep.removed),
            "unignored_paths": list(sweep.unignored),
            "shadowed_negations": list(sweep.shadowed),
        }
    )
