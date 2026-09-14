"""Converge the rooms hotfix line with dev (ent#443)

A MERGE revision. It carries no DDL and is not a schema change — its only job is
to give the graph one head again.

WHY THE GRAPH FORKED

`0044_shared_sessions_oss` landed on `main` as a hotfix, so it chained off
`0038_portal_chat_state` — `main`'s head, and the only correct parent for that
line: naming `dev`'s head instead would have referenced a revision `main` does
not carry, i.e. a boot failure on the very line it shipped to.

`dev` had meanwhile grown `0039`…`0043` off that SAME parent. Two children of
0038, so two heads — visible only now, at the back-merge. `check_alembic_heads`
was green on the hotfix PR (its tree was single-head) and green on `dev` (which
had never seen 0044); neither branch could see the fork alone.

WHY THIS AND NOT A RENUMBER

`alembic upgrade head` is singular and resolves its target BEFORE applying
anything, so a two-head graph applies ZERO revisions — silently. Not just the
forked one: EVERY revision since the fork stops arriving, on every install, with
no error.

Repointing `0044.down_revision` at `0043` would produce one head and look fixed.
It is not: an install that already ran the hotfix is stamped at 0044, so it would
be AT head and `upgrade head` would apply nothing — `0039`…`0043` would never
arrive there, permanently and silently. A tuple `down_revision` converges from
either starting state, which is the whole point of a merge revision:

  * stamped 0044 (took the hotfix) → applies 0039…0043, then this;
  * stamped 0043 (dev line)        → applies 0044, then this;
  * fresh                          → applies everything, then this.

All three were exercised against a real PostgreSQL before the hotfix merged.

Revision ID: 0045_merge_rooms_hotfix
Revises: 0043_subscription_headroom_history, 0044_shared_sessions_oss
Create Date: 2026-08-21
"""

# revision identifiers, used by Alembic.
revision = "0045_merge_rooms_hotfix"
down_revision = ("0043_subscription_headroom_history", "0044_shared_sessions_oss")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op. A merge revision joins two lines; it does not change the schema."""


def downgrade() -> None:
    """No-op — the mirror of `upgrade`. Downgrading past this re-forks the graph,
    which is a thing to do deliberately, not a thing this revision should undo
    any state for."""
