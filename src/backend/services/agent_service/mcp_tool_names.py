"""Canonical dedicated-MCP-tool naming (#846).

Single source of truth for the deterministic, collision-free ``chat_with_<slug>``
tool name the Trinity MCP server registers per MCP-exposed agent. The backend
computes the name (both the per-agent GET endpoint and the internal poll
endpoint use this helper) and the MCP server consumes it verbatim — killing the
split-brain that an independent client-side slug would create across
restarts/replicas.

Pure (no Docker / DB imports) so it unit-tests trivially and the per-agent GET
can call it cheaply.
"""

import hashlib
from typing import Dict, Iterable

_PREFIX = "chat_with_"


def _slugify(name: str) -> str:
    """``name`` → lowercase, non-[a-z0-9_] → ``_``, collapse runs, trim ``_``."""
    out = []
    for ch in name.lower():
        if ("a" <= ch <= "z") or ("0" <= ch <= "9") or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    slug = "".join(out)
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug.strip("_")


def compute_tool_names(names: Iterable[str]) -> Dict[str, str]:
    """Deterministic ``{agent_name: chat_with_<slug>}`` over the **full set**.

    Computed over the sorted, de-duplicated input so independent backend
    restarts / replicas agree. On an agent-vs-agent base-slug collision (e.g.
    ``my-agent`` and ``my_agent`` both slugify to ``my_agent``), every colliding
    name — and any name whose slug is empty — gets a stable ``_<sha1(name)[:4]>``
    suffix so each tool name is distinct AND stable across runs.
    """
    sorted_names = sorted(set(names))
    bases = {n: _slugify(n) for n in sorted_names}

    base_counts: Dict[str, int] = {}
    for n in sorted_names:
        base_counts[bases[n]] = base_counts.get(bases[n], 0) + 1

    result: Dict[str, str] = {}
    used: set = set()
    for n in sorted_names:
        base = bases[n]
        # A unique, non-empty base needs no suffix — unless (vanishingly rare) it
        # already equals a suffixed name assigned earlier this pass.
        if base and base_counts[base] == 1:
            candidate = f"{_PREFIX}{base}"
            if candidate not in used:
                result[n] = candidate
                used.add(candidate)
                continue
        # Colliding base, empty slug, or the rare clash above: append a stable
        # sha1 suffix, lengthening deterministically until globally unique. A
        # 4-hex prefix is only 16 bits (birthday-bound across same-base agents),
        # so we grow k on a real prefix collision rather than assume uniqueness —
        # the result stays deterministic and stable across runs (sorted input).
        digest = hashlib.sha1(n.encode("utf-8")).hexdigest()
        k = 4
        while True:
            suffix = digest[:k]
            candidate = f"{_PREFIX}{base}_{suffix}" if base else f"{_PREFIX}{suffix}"
            if candidate not in used:
                break
            if k >= len(digest):
                # Astronomically unlikely (full sha1 collision within one set).
                candidate = f"{candidate}_{len(used)}"
                break
            k += 1
        result[n] = candidate
        used.add(candidate)
    return result


def compute_tool_name(name: str) -> str:
    """Single-agent convenience — the tool name when ``name`` is the only agent.

    Note: a name that collides with another exposed agent gets a hash suffix only
    in the full-set computation (``compute_tool_names``). The per-agent GET/PUT use
    ``resolve_tool_name`` (which folds in the live exposed set) so the UI never
    shows a bare name that differs from the suffixed tool actually registered.
    """
    return compute_tool_names([name])[name]


def resolve_tool_name(agent_name: str, exposed_names: Iterable[str]) -> str:
    """The tool name ``agent_name`` resolves to within the live exposed set (#846).

    The per-agent GET/PUT call this so the name shown in the UI matches what the
    MCP server actually registers — including the collision suffix when another
    exposed agent shares the same base slug. ``agent_name`` is folded in even when
    not currently exposed, so a disabled agent still previews the name it would
    get against the current set.
    """
    names = set(exposed_names)
    names.add(agent_name)
    return compute_tool_names(names)[agent_name]


def build_tool_description(agent_name: str) -> str:
    """Name-only tool description — metadata-free by design (#846).

    The dedicated-tool description is advertised **globally**: FastMCP filters the
    advertised tool list per session by ``canAccess``, and the dedicated
    ``chat_with_<slug>`` tools use only the connector-tier gate, so EVERY
    non-connector MCP session (any ``user``/``agent``/``system`` key) sees this
    description regardless of whether it can access the agent. It therefore must
    NOT carry per-agent metadata. Embedding the ``trinity.template`` Docker label
    here leaked the template / repo identifier cross-tenant to callers who cannot
    access the agent (and opened a prompt-injection surface into the advertised
    description). The agent name is already intrinsic to the ``chat_with_<slug>``
    tool name, so a name-only description adds no disclosure beyond the tool name
    itself.
    """
    return f'Chat directly with the "{agent_name}" agent.'
