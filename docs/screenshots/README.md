# Screenshots

**This directory is the single place UI screenshots live.** Every screenshot referenced by
`docs/` or `README.md` is here, flat, with one entry in [`MANIFEST.yaml`](MANIFEST.yaml).

Do not add screenshots anywhere else. The previous three stores (`docs/user-docs/images/`,
`docs/assets/screenshots/`, and this one) drifted apart because the capture tooling wrote
here while the docs embedded the other two — so the freshest set was the one nobody rendered.

## Refreshing

```bash
/capture-screenshots            # everything in the manifest
/capture-screenshots agent-git  # one entry
```

The skill reads `MANIFEST.yaml`, drives each `route` at the declared viewport, writes the
`file`, and updates `captured_at` + `captured_commit`. Adding a new screenshot means adding
a manifest entry — the capture skill has no separate list.

## Verifying

```bash
python3 scripts/ci/check_screenshots.py          # integrity (blocking in CI)
python3 scripts/ci/check_screenshots.py --strict # integrity + staleness
```

`screenshot-guard.yml` runs this on every PR touching docs, the README, or the frontend.
It **blocks** on integrity — a manifest entry with no file, a file with no entry, a broken
`![](…)` path, a `sources` path that no longer exists — and **warns** on staleness, because
a UI PR legitimately makes a screenshot stale in the same commit that changes the view.
Recapture is a follow-up, not a merge blocker.

Staleness = the newest commit over an entry's `sources` is newer than its `captured_at`.
That's why `sources` matters more than the date: it answers *did the thing in the picture
actually change*, instead of nagging on age alone.

## The one exception

Release-note screenshots under `docs/assets/screenshots/whats-new/<version>/` are **frozen**
point-in-time snapshots and must never be refreshed — a current screenshot in an old release
note is wrong. They are deliberately outside this store and outside the manifest.

Brand assets (`docs/assets/trinity-hero.webp`, `trinity-explainer.gif`, logos) are not
screenshots and are also out of scope.

## Conventions

- **Names are stable slugs**, never numbered. The old `01-`…`31-` prefixes are why nothing
  could safely embed these: inserting a screen renumbered the set and broke every reference.
- **Flat** — no subdirectories, so a manifest `file` is always a bare filename.
- **1440×900 at 2× (dark theme)** per the manifest `viewport`. Uniform viewport is what makes
  the images look like one set rather than a scrapbook.
- **Demo data only.** Never capture against a real fleet: a superseded screenshot here leaked
  a real face, a real `$2377.70` lifetime spend, and personal knowledge-base counts into a
  public repo for a month. Run `/create-demo-agent-fleet` first.
- **New captures should be WebP** where the tooling supports it — these are ~500 KB retina
  PNGs and git keeps every version forever, so a full refresh permanently adds ~8 MB to
  clone size. The existing PNGs are not worth re-encoding (history already holds them).
