"""Journey Impact — parse the declaration and decide the gate (#2350, Gate G1).

Every epic and user-facing feature declares what it does to the promise list:

    Journey Impact: new: J11
    Journey Impact: extends: J03
    Journey Impact: none: refactor, no user-visible behaviour changes

WHY THE GATE IS THE POINT. Ownership alone collapses into "PM nags, Eng
defers". An intake FIELD alone is worse than nothing: everything would be
declared `none:` in three weeks and the field would read as compliance. So the
burden sits on the `new:` path — declaring a new promise obliges the same PR to
carry a skeleton for it — which is what stops `none:` being the cheap escape.
Choosing `none:` becomes a visible claim that the change touches no promise,
made in writing, next to a diff a reviewer can compare it against.

Pure functions, stdlib only: the decision is unit-tested rather than discovered
in CI logs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

# `new`/`extends` name a journey id (J01..J99). `none` takes free text.
_DECL_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?journey\s*impact(?:\*\*)?\s*[:\-]\s*(?P<rest>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_KIND_RE = re.compile(
    r"^(?P<kind>new|extends|none)\b\s*[:\-]?\s*(?P<arg>.*)$", re.IGNORECASE
)
_JOURNEY_ID_RE = re.compile(r"^J\d{2}$")

# A skeleton lives in the journey tier (#2335). Anything else is a test that
# happens to mention a journey.
_SKELETON_DIR = "tests/journeys/"

# `strict=True` is what makes an xfail a claim rather than a shrug: it fails the
# build the day the behaviour starts working, which is exactly when the
# skeleton should stop being a skeleton.
_STRICT_XFAIL_RE = re.compile(r"xfail\s*\([^)]*strict\s*=\s*True", re.IGNORECASE | re.DOTALL)


@dataclass
class Declaration:
    kind: Optional[str] = None          # new | extends | none | None (absent)
    journey: Optional[str] = None       # J-id for new/extends
    reason: Optional[str] = None        # free text for none
    raw: Optional[str] = None
    error: Optional[str] = None         # malformed, with the reason


@dataclass
class Verdict:
    ok: bool
    # Every path says WHAT it read and WHY it decided (#2350 AC 5). A gate that
    # fails without naming the declaration it acted on is a gate people learn
    # to re-run rather than read.
    lines: list = field(default_factory=list)

    def say(self, msg: str) -> "Verdict":
        self.lines.append(msg)
        return self


def parse_declaration(body: Optional[str]) -> Declaration:
    """Read the Journey Impact declaration out of an issue or PR body."""
    if not body:
        return Declaration()
    matches = _DECL_RE.findall(body)
    if not matches:
        return Declaration()
    # Last wins: an edited body keeps the correction, not the draft above it.
    raw = matches[-1].strip()
    # Cut at the comment opener rather than matching a `<!-- ... -->` pair.
    # `_DECL_RE` is line-bounded (`.+?` never crosses a newline), so `raw` is
    # one line and everything from `<!--` on is comment — including the
    # UNCLOSED case, which a pair-matching regex silently leaves in the reason
    # (`none: fine <!-- todo` would have been read as the reason "fine <!--
    # todo"). This is also why CodeQL's py/bad-tag-filter fires on the pair
    # form: it cannot handle a comment spanning newlines. Truncating removes
    # the construct and is strictly more correct for this input.
    raw = raw.split("<!--", 1)[0].strip()
    if not raw or raw.lower() in {"new:", "extends:", "none:", "_none_", "tbd"}:
        return Declaration(raw=raw, error="the declaration is present but empty")

    m = _KIND_RE.match(raw)
    if not m:
        return Declaration(
            raw=raw,
            error=f"{raw!r} is not one of `new: <id>`, `extends: <id>`, `none: <reason>`",
        )
    kind = m.group("kind").lower()
    arg = m.group("arg").strip().strip("`").strip()

    if kind == "none":
        if not arg:
            # AC 4. A bare `none` is the whole failure mode this gate exists to
            # prevent: it is indistinguishable from not having thought about it.
            return Declaration(kind=kind, raw=raw,
                               error="`none` needs a reason — `none: <why this touches no promise>`")
        return Declaration(kind=kind, reason=arg, raw=raw)

    journey = arg.split()[0].strip(",.") if arg else ""
    if not _JOURNEY_ID_RE.match(journey):
        return Declaration(
            kind=kind, raw=raw,
            error=f"`{kind}` needs a journey id like J03, got {journey or '(nothing)'}",
        )
    return Declaration(kind=kind, journey=journey, raw=raw)


def looks_like_skeleton(path: str, content: str, journey: Optional[str] = None) -> bool:
    """Does this changed file carry a journey skeleton — for `journey`?

    A `strict=True` xfail asserting the journey's invariants is sufficient
    (AC 3): the point is a named, failing-on-success claim in the tier, not a
    finished harness. A passing test obviously also counts.

    The id binding is not pedantry. Without it, declaring `new: J11` is
    satisfied by touching ANY file in the tier — including an unrelated edit to
    an existing journey — which turns the obligation back into the compliance
    theatre the gate exists to prevent. The skeleton must name the promise it
    is standing in for, in its path or its body.
    """
    if not path.startswith(_SKELETON_DIR):
        return False
    if path.endswith(("catalog.yaml", "conftest.py", "__init__.py")):
        return False
    has_test = bool(
        _STRICT_XFAIL_RE.search(content)
        or re.search(r"^\s*def test_", content, re.MULTILINE)
    )
    if not has_test:
        return False
    if journey is None:
        return True
    hay = f"{path}\n{content}".upper()
    # NOT `\b`: the id is routinely embedded underscore-delimited
    # (`test_j07_journey.py`), and `_` is a word character, so `\bJ07\b` never
    # matches the very filename the convention produces. Delimit on the
    # characters that would actually make it a DIFFERENT id instead — a letter
    # or digit either side — so J07 does not match J071 or XJ07.
    pat = rf"(?<![A-Z0-9]){re.escape(journey.upper())}(?![A-Z0-9])"
    return re.search(pat, hay) is not None


def decide(
    *,
    pr_declaration: Declaration,
    epic_declarations: Iterable[tuple] = (),   # (issue_ref, Declaration)
    changed_files: Iterable[tuple] = (),       # (path, content)
) -> Verdict:
    """The gate. Fails on a malformed declaration, or on `new:` with no skeleton."""
    v = Verdict(ok=True)

    if pr_declaration.error:
        v.ok = False
        return v.say(f"PR declaration: {pr_declaration.raw!r}").say(
            f"REJECTED — {pr_declaration.error}"
        )

    obligations = []
    if pr_declaration.kind:
        v.say(f"PR declares Journey Impact: {pr_declaration.raw!r}")
        if pr_declaration.kind == "new":
            obligations.append(("this PR", pr_declaration.journey))
    else:
        # Absence is not (yet) a failure: adopting the field must not red-X
        # every in-flight PR and every dependabot bump on day one. It is
        # reported loudly so the gap is visible, and tightening this is a
        # deliberate follow-up once the templates have been in use.
        v.say("PR declares no Journey Impact — allowed for now, but the "
              "template asks for one; see .github/pull_request_template.md")

    for ref, decl in epic_declarations:
        if decl.error:
            v.ok = False
            v.say(f"epic {ref} declaration: {decl.raw!r}")
            v.say(f"REJECTED — epic {ref}: {decl.error}")
            return v
        if decl.kind:
            v.say(f"epic {ref} declares Journey Impact: {decl.raw!r}")
            if decl.kind == "new":
                obligations.append((f"epic {ref}", decl.journey))

    if not obligations:
        return v.say("PASSED — no `new:` declaration, so no skeleton is owed.")

    files = list(changed_files)
    tier_tests = [p for p, c in files if looks_like_skeleton(p, c)]
    matched = []
    for who, journey in obligations:
        hits = [p for p, c in files if looks_like_skeleton(p, c, journey)]
        if hits:
            matched.append(f"{journey} ({', '.join(hits)})")
            continue
        v.ok = False
        if tier_tests:
            # The confusing case: they DID add a journey test, it just does not
            # name the promise they declared. Say that, rather than "none found".
            v.say(
                f"REJECTED — {who} declares `new: {journey}`, and this PR does "
                f"touch the journey tier ({', '.join(tier_tests)}) — but none of "
                f"those files names {journey}. The skeleton has to name the "
                f"promise it stands in for, or the obligation is satisfied by "
                f"any unrelated edit in the tier."
            )
        else:
            v.say(
                f"REJECTED — {who} declares `new: {journey}`, which obliges this "
                f"PR to carry a journey skeleton under {_SKELETON_DIR}. None found "
                f"in {len(files)} changed file(s)."
            )
        v.say(
            f"A `strict=True` xfail naming {journey} is enough:\n"
            f"    @pytest.mark.xfail(strict=True, reason='{journey} not built yet')\n"
            f"    def test_{journey.lower()}_promise(): ...\n"
            f"Declaring a new promise and shipping nothing that asserts it is "
            f"the gap this gate exists to close."
        )
        return v
    return v.say("PASSED — skeleton present for " + "; ".join(matched))
