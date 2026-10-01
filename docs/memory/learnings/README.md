# Learnings fragments

One file per lesson, folded into [`../learnings.md`](../learnings.md) at the next release cut.

## Why fragments

`learnings.md` is append-only and every PR appends at the same tail, so any two PRs opened on the same day conflict there. On the merge train that was four of five conflicting members on the first run, and it recurs *between* members after each merge (a member resolved against `dev` re-conflicts the moment its sibling lands), costing one push plus a required-checks re-run per member. A fragment file has a unique name, so two PRs never touch the same path.

## Writing one

`/review` Step 7 (and anyone with a durable lesson) creates:

```
docs/memory/learnings/YYYY-MM-DD-<slug>.md
```

containing exactly one ledger entry in the ledger's own format:

```markdown
## YYYY-MM-DD — [pitfall|pattern|architecture] — <one-line title>
**Context**: where it bit (file / PR / issue)
**Lesson**: the durable rule, stated so it's actionable next time
```

Same rules as the ledger: one entry per *class* of error, written for the `/autoplan` reader. Never edit `learnings.md` directly for a new entry.

## Reading them

`/autoplan`, `/bug-escape-analysis` and anything else that consults the ledger reads `learnings.md` **and** every `docs/memory/learnings/[0-9]*.md`. Until the next release the fragments *are* the newest part of the ledger.

## Folding (release cut)

`/release` appends the fragments to `learnings.md` in filename (date) order and deletes them in the same commit, so `main` never carries fragments and the ledger stays one file for readers on a release.

```bash
ls docs/memory/learnings/[0-9]*.md 2>/dev/null | sort | while read -r f; do
  printf '\n' >> docs/memory/learnings.md
  cat "$f" >> docs/memory/learnings.md
  git rm -q "$f"
done
git add docs/memory/learnings.md
```
