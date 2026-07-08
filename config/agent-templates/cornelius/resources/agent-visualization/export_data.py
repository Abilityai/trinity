#!/usr/bin/env python3
"""
export_data.py — build data.json for the agent-visualization prototype from the
REAL Brain Dependency Graph.

Reads (read-only):
  - resources/brain-graph/data/graph_enrichments.json   (layer, lifecycle, staleness, typed edges, tensions)
  - resources/local-brain-search/data/q_values.json      (usage heat, optional)
  - resources/local-brain-search/data/brain_graph.pkl    (reference-scope overlay, optional — needs networkx)
  - Brain/05-Meta/Thinking/THINKING-REGISTRY.md           (incubation topics, optional)

Writes:
  - resources/agent-visualization/data.json  (matches SPEC.md §7 contract)
  - resources/agent-visualization/data.js    (same payload as window.AGENT_DATA, for file://)

The visual prototype is built against the fixture-shaped contract, so a real export
drops in unchanged. Run from the repo root:

    python3 resources/agent-visualization/export_data.py

No third-party deps. Pure stdlib.
"""
import datetime
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))

# Reuse the CANONICAL scope primitive (single source of truth — do not duplicate
# folder names here). memory_config holds the pure primitives (no faiss/networkx,
# so the import is cheap and safe). If it can't be imported, fail OPEN — the orb
# is a read-only picture, so a broken import should render the full graph, never
# crash or silently blank the canvas.
sys.path.insert(0, os.path.join(REPO, "resources/local-brain-search"))
try:
    from memory_config import (
        scope_of, scope_id, in_read_scope, resolve_read_scope, CORE_FOLDERS,
        SCOPE_FAMILIES, is_reference_scope, scope_kind, reference_folders,
    )
except Exception:
    CORE_FOLDERS = frozenset({"02-Permanent", "03-MOCs", "AI Extracted Notes", "01-Sources"})
    SCOPE_FAMILIES = frozenset({"Books"})
    def scope_of(nid):
        return str(nid).replace("\\", "/").split("/", 1)[0]
    def scope_id(nid):
        parts = str(nid).replace("\\", "/").split("/")
        if parts[0] in SCOPE_FAMILIES and len(parts) >= 2:
            return parts[0] + "/" + parts[1]
        return parts[0]
    def in_read_scope(nid, read_scope):
        return scope_id(nid) in read_scope or scope_of(nid) in read_scope
    def resolve_read_scope(raw=None):
        return CORE_FOLDERS
    def is_reference_scope(x):
        return False
    def scope_kind(x):
        return "cognitive"
    def reference_folders():
        return frozenset()

ENRICH = os.path.join(REPO, "resources/brain-graph/data/graph_enrichments.json")
QVALS = os.path.join(REPO, "resources/local-brain-search/data/q_values.json")
GRAPH_PKL = os.path.join(REPO, "resources/local-brain-search/data/brain_graph.pkl")
REGISTRY = os.path.join(REPO, "Brain/05-Meta/Thinking/THINKING-REGISTRY.md")
DASHBOARD = os.path.join(REPO, "dashboard.yaml")
BRAIN_GRAPH = os.path.join(REPO, "resources/brain-graph/run_brain_graph.sh")
VAULT = os.path.join(REPO, "Brain")
OUT = os.path.join(HERE, "data.json")
OUT_JS = os.path.join(HERE, "data.js")

TODAY = datetime.date.today()

# layer name -> altitude number (1 = raw signal at surface, 7 = bedrock index at core)
LAYER_NUM = {
    "signal": 1, "impression": 2, "insight": 3, "framework": 4,
    "lens": 5, "synthesis": 6, "index": 7,
}

# ── Canvas config — what export_data puts on the orb ─────────────────────────
# Every value here is overridable from viz_config.json (sibling file). Missing
# keys fall back to these defaults, so the file is optional and may be partial.
# Edit viz_config.json, then re-run /brain-orb (or `python3 export_data.py`).
VIZ_CONFIG = os.path.join(HERE, "viz_config.json")

DEFAULT_CONFIG = {
    # budgets that keep the canvas legible
    "max_edges": 1600,           # edge budget for a readable canvas
    "node_content_cap": 6000,    # max chars of body embedded per node
    # how many nodes to keep per layer. The giant "insight" layer is down-sampled;
    # the small/structural layers are kept ~whole. Total on canvas ≈ sum of these
    # plus a bounded number of guaranteed "always-include" extras (see selection).
    "layer_caps": {
        "signal": 18, "impression": 18, "insight": 240, "framework": 90,
        "lens": 14, "synthesis": 110, "index": 30,
    },
    # WHAT goes on the canvas (applied in main()): a weighted salience score plus
    # guaranteed sets so the important structure is never sampled out.
    "selection": {
        "weights": {            # salience = Σ weight · signal (each normalised 0..1)
            "lifecycle": 1.0,   #   maturity (reflective → generative)
            "in_degree": 1.2,   #   inbound authority (√-compressed) — the foundations
            "degree": 0.4,      #   total connectivity
            "recency": 0.6,     #   recently touched = current thinking
        },
        "recency_full_days": 14,        # touched within N days → full recency score
        "always_include": {
            "hubs": 40,                 # top-N most-referenced (in-degree) notes
            "tension_endpoints": True,  # both ends of every real tension (red seams)
            "recent_days": 14,          # include notes touched within N days...
            "recent_max": 60,           # ...up to this many (highest-scoring first)
            "min_ai_inferred": 12,      # floor of ai-inferred notes (the membrane story)
        },
    },
}


def _deep_merge(base, override):
    """Recursively overlay `override` onto `base` (nested dicts merged, not replaced)."""
    out = dict(base)
    for k, v in (override or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_viz_config():
    """viz_config.json overlaid on DEFAULT_CONFIG (file optional / may be partial)."""
    try:
        with open(VIZ_CONFIG) as f:
            user = json.load(f)
    except Exception:
        user = {}
    return _deep_merge(DEFAULT_CONFIG, user)


CONFIG = load_viz_config()
LAYER_CAP = CONFIG["layer_caps"]
MAX_EDGES = CONFIG["max_edges"]
NODE_CONTENT_CAP = CONFIG["node_content_cap"]

# which scheduled skill maps to which orb state (for the live activity feed)
SKILL_STATE = {
    "incubation-loop": "thinking",
    "domain-watch": "ingesting",
    "ai-crystallize": "converged",
    "refresh-index": "ingesting",
}


# ============================ small shared helpers ============================
def _run(cmd, timeout):
    """Run a subprocess and return stdout, or None if it fails/times out."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return None


def git_log(*args, timeout=30):
    """`git -C REPO log <args>` stdout, or None on failure (lets callers distinguish
    'no data' from 'command failed' — e.g. learned_7d must stay None, not 0)."""
    return _run(["git", "-C", REPO, "log", *args], timeout)


def read_text(path, limit=None):
    """Read a file (lossy decode), returning '' if it is missing or unreadable.
    `limit` caps the number of characters read (None = whole file)."""
    try:
        with open(path, errors="ignore") as f:
            return f.read(limit) if limit is not None else f.read()
    except Exception:
        return ""


def read_json(path):
    """Parse a JSON file, returning {} if it is missing or invalid."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def strip_frontmatter(txt):
    """Drop a leading YAML frontmatter block (--- ... ---) if present."""
    if txt.startswith("---"):
        end = txt.find("\n---", 3)
        if end != -1:
            return txt[end + 4:]
    return txt


def _date(s):
    try:
        return datetime.date.fromisoformat(s)
    except Exception:
        return None


def phase_from_score(score):
    if score >= 0.5:
        return "generative"
    if score >= 0.2:
        return "crystallizing"
    return "reflective"


def provenance_from_path(note_id):
    p = note_id.replace("\\", "/")
    # Reference-scope notes (Company, …) are external mutable FACTS, not cognition —
    # they carry provenance: reference. Check the scope KIND first so they are never
    # mis-coloured as "encountered" research (they share no cognitive lifecycle).
    if is_reference_scope(scope_of(note_id)):
        return "reference"
    if "AI Crystallizations" in p:
        return "ai-inferred"
    if "AI Extracted Notes" in p:
        return "originated"
    if "Document Insights" in p:
        return "encountered"
    if "02-Permanent" in p:
        return "endorsed"
    if "00-Inbox" in p or "01-Sources" in p:
        return "encountered"
    return "encountered"


def title_from_id(note_id):
    base = os.path.basename(note_id)
    return re.sub(r"\.md$", "", base)


def reference_overlay(render_scope):
    """Reference-kind nodes + edges, pulled from the LBS graph (best-effort).

    Reference scopes (Company, …) are deliberately EXCLUDED from the BDG
    enrichments — the Phase-6f lifecycle opt-out: reference records carry no
    lifecycle/layer, so `classify.bootstrap` skips them. That means the
    enrichment-only node set the orb normally reads never contains them. To still
    DISPLAY a mounted reference scope, overlay its nodes + edges straight from the
    LBS graph (`brain_graph.pkl`), synthesizing enrichment-shaped metadata (no
    lifecycle — correct: they have none by design).

    Returns (nodes, edges) shaped like the enrichment payload, or ({}, {}) when
    no reference scope is in view, or the graph can't be read (no networkx / no
    pickle). Fail-open: the orb simply omits reference nodes, never crashes.
    """
    refs = reference_folders()
    if not refs:
        return {}, {}
    # which reference folders does this view show? (render_scope=None → full graph)
    want = refs if render_scope is None else (refs & set(render_scope))
    if not want:
        return {}, {}   # no reference scope mounted — skip the pickle load entirely
    try:
        import pickle
        with open(GRAPH_PKL, "rb") as f:
            G = pickle.load(f)   # networkx DiGraph (import happens during unpickle)
    except Exception:
        return {}, {}
    nodes = {nid: {"layer": "reference", "lifecycle": 0.0, "staleness_score": 0.0}
             for nid in G.nodes() if scope_of(nid) in want}
    if not nodes:
        return {}, {}
    edges = {}
    for u, v, d in G.edges(data=True):
        # an edge joins the overlay if it touches a reference node; the other
        # endpoint is kept too (the both-endpoints-on-canvas check later drops it
        # if that endpoint isn't selected). explicit → references (wiki-link);
        # semantic → associates (similarity), carrying its weight as confidence.
        if scope_of(u) in want or scope_of(v) in want:
            etype = "references" if d.get("type") == "explicit" else "associates"
            conf = float(d.get("weight", 1.0) or 1.0)
            edges[f"{u}||{v}"] = {"edge_type": etype, "confidence": round(conf, 2)}
    return nodes, edges


# ==================== fallback graph (no enrichments) ====================
# Folder → layer/lifecycle heuristics for vaults that haven't run the Brain
# Dependency Graph pipeline yet (the public template's seeded KB, or any fresh
# user vault — trinity-enterprise#76). Deterministic; the full BDG path takes
# over automatically once graph_enrichments.json exists.
_FALLBACK_LAYER = {
    "00-Inbox": "signal", "01-Sources": "signal",
    "Document Insights": "impression", "Books": "impression",
    "AI Extracted Notes": "insight", "02-Permanent": "insight",
    "06-Belief-System": "framework",
    "05-Meta": "lens", "08-Meta-Cognitive": "lens",
    "04-Output": "synthesis",
    "03-MOCs": "index",
}
_FALLBACK_LIFECYCLE = {
    "03-MOCs": 0.55, "02-Permanent": 0.45, "04-Output": 0.4,
    "AI Extracted Notes": 0.35, "06-Belief-System": 0.35,
}
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")


def build_fallback_enrichments():
    """Enrichment-shaped {nodes, edges, tensions} scanned straight from the
    vault: every Brain/**/*.md is a node (layer/lifecycle from its folder),
    every resolvable [[wikilink]] is a `references` edge. Wikilink targets are
    TITLES, not paths — resolved via a basename + H1 index (sorted walk, first
    occurrence wins, so resolution is deterministic)."""
    nodes, edges = {}, {}
    title_to_id = {}
    bodies = {}
    if not os.path.isdir(VAULT):
        return {"nodes": nodes, "edges": edges, "tensions": []}
    files = sorted(
        os.path.join(dp, f)
        for dp, _dn, fn in os.walk(VAULT) for f in fn if f.endswith(".md")
    )
    for path in files:
        nid = os.path.relpath(path, VAULT).replace("\\", "/")
        top = scope_of(nid)
        nodes[nid] = {
            "layer": _FALLBACK_LAYER.get(top, "insight"),
            "lifecycle": _FALLBACK_LIFECYCLE.get(top, 0.2),
            "staleness_score": 0.0,
        }
        stem = os.path.splitext(os.path.basename(nid))[0].strip().lower()
        title_to_id.setdefault(stem, nid)
        body = read_text(path, limit=200_000)
        bodies[nid] = body
        m = re.search(r"^#\s+(.+)$", body[:2000], re.M)
        if m:
            title_to_id.setdefault(m.group(1).strip().lower(), nid)
    for nid, body in bodies.items():
        for m in _WIKILINK.finditer(body):
            tgt = title_to_id.get(m.group(1).strip().lower())
            if tgt and tgt != nid:
                edges.setdefault(f"{nid}||{tgt}",
                                 {"edge_type": "references", "confidence": 0.6})
    return {"nodes": nodes, "edges": edges, "tensions": []}


def main():
    have_enrich = os.path.exists(ENRICH)
    if have_enrich:
        enrich = read_json(ENRICH)
    else:
        print(f"enrichments not found at {ENRICH} — using vault wikilink fallback",
              file=sys.stderr)
        enrich = build_fallback_enrichments()
    raw_nodes = enrich.get("nodes", {})
    raw_edges = enrich.get("edges", {})
    # Tensions are stored by the BDG detector as {note_a, note_b, similarity, ...};
    # normalize to the canonical {a, b, strength} the orb uses everywhere, drop
    # malformed/self rows, sort strongest-first, and CAP to the strongest seams. The
    # cap matters: a loose detector run can persist thousands of pairs, and the
    # tension-endpoint always-include below would otherwise force every endpoint onto
    # the canvas. ~40 productive contradictions is the readable ceiling.
    # A PRODUCTIVE contradiction is high similarity (about the same thing) AND high
    # stance divergence (opposite conclusions) — so rank by similarity × divergence,
    # not similarity alone (pure sim≈1.0 is a near-duplicate note, not a tension). The
    # detector stores divergence in the description ("stance divergence 0.NN").
    TENSION_CAP = 40
    def _t_sim(t):
        v = t.get("strength", t.get("similarity", t.get("score", 0.6)))
        try:
            return round(float(v), 2)
        except Exception:
            return 0.6
    def _t_div(t):
        m = re.search(r"stance divergence\s*([0-9.]+)", t.get("description", "") or "")
        try:
            return float(m.group(1)) if m else 0.5
        except Exception:
            return 0.5
    _cand = []
    for t in (enrich.get("tensions", []) or []):
        a = t.get("a") or t.get("note_a") or t.get("source")
        b = t.get("b") or t.get("note_b") or t.get("target")
        if not (a and b and a != b):
            continue
        if a.rsplit("/", 1)[-1].startswith("CHANGELOG") or b.rsplit("/", 1)[-1].startswith("CHANGELOG"):
            continue              # changelog boilerplate is similar but isn't a contradiction
        sim = _t_sim(t)
        if sim >= 0.95:           # near-identical notes are duplicates, not contradictions
            continue
        _cand.append({"a": a, "b": b, "strength": sim,
                      "description": t.get("description", ""), "_score": sim * _t_div(t)})
    _cand.sort(key=lambda t: -t["_score"])
    # cap to the strongest seams, limiting how often any single note dominates the list
    raw_tensions, _seen = [], {}
    for t in _cand:
        if len(raw_tensions) >= TENSION_CAP:
            break
        if _seen.get(t["a"], 0) >= 3 or _seen.get(t["b"], 0) >= 3:
            continue
        _seen[t["a"]] = _seen.get(t["a"], 0) + 1
        _seen[t["b"]] = _seen.get(t["b"], 0) + 1
        raw_tensions.append({k: v for k, v in t.items() if k != "_score"})

    # ---- scope: which scopes does this orb render? --------------------------
    # `BRAIN_READ_SCOPE` decides which scopes are mounted on the canvas. The orb
    # is a read-only PICTURE, so — unlike the retrieval/learn path, which fails
    # CLOSED to core — it defaults to the FULL graph (no surprise narrowing) and
    # also accepts an "all" convenience token. Set the env to mount/unmount
    # scopes; turning one off removes its nodes AND their edges before sizing,
    # brightness, hubs and selection are computed, so the picture is honest:
    #   (unset) / all              -> full graph (every scope) — the default
    #   core                       -> the honest cognitive fingerprint only
    #   core,document-insights      -> fingerprint + encountered research
    #   core,document-insights,meta,inbox,output -> everything, named explicitly
    # Token meanings (core, slugs) come from the canonical resolve_read_scope(),
    # so the orb agrees with search/learn/fingerprint on what each scope name is.
    raw_scope = os.environ.get("BRAIN_READ_SCOPE", "").strip()
    if not raw_scope or raw_scope.lower() == "all":
        render_scope = None  # no filter — full graph
    else:
        render_scope = resolve_read_scope(raw_scope)

    # Overlay reference-kind scopes (Company, …). They are excluded from the BDG
    # enrichments by design (no lifecycle), so when their scope is in view, pull
    # the nodes + edges straight from the LBS graph — otherwise they could never
    # render. Merged BEFORE the filter so the scope filter handles them uniformly.
    ref_nodes, ref_edges = reference_overlay(render_scope)
    if ref_nodes:
        raw_nodes = {**raw_nodes, **ref_nodes}
        raw_edges = {**raw_edges, **ref_edges}

    if render_scope is not None:
        raw_nodes = {nid: m for nid, m in raw_nodes.items() if in_read_scope(nid, render_scope)}
        _keep = set(raw_nodes)
        raw_edges = {k: m for k, m in raw_edges.items()
                     if "||" in k and all(p in _keep for p in k.split("||", 1))}
        raw_tensions = [t for t in raw_tensions
                        if (t.get("a") or t.get("source")) in _keep
                        and (t.get("b") or t.get("target")) in _keep]

    qvals = read_json(QVALS)   # optional heat signal

    # ---- degree (over the full graph, for salience) -------------------------
    # degree    = total connectivity (drives node SIZE + selection salience)
    # in_degree = inbound links only, i.e. how many notes point TO this one
    #             (structural authority / "load-bearing"-ness) -> drives BRIGHTNESS.
    #             Edge keys are "src||tgt" (src points to tgt), so a node's
    #             in-degree is how often it appears as the tgt.
    degree = {}
    in_degree = {}
    edge_pairs = []
    for key, meta in raw_edges.items():
        if "||" not in key:
            continue
        src, tgt = key.split("||", 1)
        edge_pairs.append((src, tgt, meta))
        degree[src] = degree.get(src, 0) + 1
        degree[tgt] = degree.get(tgt, 0) + 1
        in_degree[tgt] = in_degree.get(tgt, 0) + 1
    max_deg = max(degree.values()) if degree else 1
    max_in_deg = max(in_degree.values()) if in_degree else 1

    # recency: most-recent touch per file (git history + frontmatter dates).
    # Built first because the selection score below uses it.
    git_recency = build_git_recency()

    # the agent's converged conclusions (parsed once, here, so the selection block
    # below can GUARANTEE their nodes render — the voice must see + highlight the whole
    # converged set, not a sampled slice). converged_ids maps each slug to its node id.
    registry = read_text(REGISTRY)
    converged = parse_converged(registry)
    converged_ids = {"05-Meta/Thinking/" + c["topic"] + ".md" for c in converged}

    # ---- decide WHAT goes on the canvas (all knobs in viz_config.json) -------
    # 1. score every node by a weighted salience (lifecycle, inbound authority,
    #    total connectivity, recency);
    # 2. guarantee "always-include" sets so important structure is never sampled
    #    out (top hubs, both ends of every tension, recent notes, an ai-inferred
    #    floor for the membrane);
    # 3. fill each layer up to its cap by score — preserving the 7-layer altitude.
    SEL = CONFIG["selection"]
    W = SEL["weights"]
    rec_full = max(1, int(SEL.get("recency_full_days", 14)))

    _age = {}
    def age_of(nid):
        if nid not in _age:
            _age[nid] = note_age_days(nid, git_recency)
        return _age[nid]

    def score(nid, meta):
        life = float(meta.get("lifecycle", 0.0) or 0.0)
        in_n = (in_degree.get(nid, 0) / max_in_deg) ** 0.5
        deg_n = degree.get(nid, 0) / max_deg
        rec_n = max(0.0, 1.0 - age_of(nid) / rec_full)
        return (W["lifecycle"] * life + W["in_degree"] * in_n
                + W["degree"] * deg_n + W["recency"] * rec_n)

    scored = {nid: score(nid, meta) for nid, meta in raw_nodes.items()}

    selected = {}
    def take(nid):
        if nid and nid in raw_nodes and nid not in selected:
            selected[nid] = raw_nodes[nid]

    inc = SEL.get("always_include", {})
    # guarantee every mounted reference node renders — a reference scope is a
    # small, curated, finite set (CRM-like records: org / people / products /
    # clients), and the point of mounting it is to see the whole cluster, not a
    # score-sampled slice. (Reference nodes carry no cognitive layer, so the
    # per-layer caps below would otherwise drop most of them.)
    for nid in list(raw_nodes):
        if is_reference_scope(scope_of(nid)):
            take(nid)
    # pull in the cognitive endpoint of any in-scope cross-kind bridge (reference
    # node ↔ cognitive note), so a mounted reference scope shows how it connects
    # to the brain instead of floating as an island. take() no-ops when that
    # endpoint is out of scope (e.g. a 04-Output bridge under scope=core,company).
    for key in list(raw_edges):
        if "||" not in key:
            continue
        s, t = key.split("||", 1)
        if is_reference_scope(scope_of(s)) ^ is_reference_scope(scope_of(t)):
            take(t if is_reference_scope(scope_of(s)) else s)
    # guarantee every in-scope BOOK (family-scope) node renders. A mounted book is a
    # small, finite literature set whose whole point is to be a browsable/highlightable
    # cluster — the voice highlights it BY SCOPE, so its notes must all be on the canvas,
    # not a score-sampled slice. raw_nodes is already scope-filtered, so this only fires
    # for books actually in view (no book nodes exist under scope=core).
    for nid in list(raw_nodes):
        if scope_of(nid) in SCOPE_FAMILIES:
            take(nid)
    # guarantee every CONVERGED thinking-topic node renders — these are the agent's
    # finished conclusions; the voice enumerates + highlights the whole set, so none
    # may be sampled out. (take() no-ops for any that aren't in raw_nodes/scope.)
    for nid in converged_ids:
        take(nid)
    # top hubs by inbound authority
    for nid in sorted(in_degree, key=lambda k: -in_degree[k])[: int(inc.get("hubs", 0) or 0)]:
        take(nid)
    # both endpoints of every real tension, so red seams can render
    if inc.get("tension_endpoints", True):
        for t in raw_tensions:
            take(t.get("a") or t.get("source"))
            take(t.get("b") or t.get("target"))
    # recently-touched notes = current thinking (bounded, best-scoring first)
    r_days = int(inc.get("recent_days", 0) or 0)
    if r_days:
        recent_ids = sorted((nid for nid in raw_nodes if age_of(nid) <= r_days),
                            key=lambda k: -scored[k])
        for nid in recent_ids[: int(inc.get("recent_max", 60) or 60)]:
            take(nid)
    # guarantee a floor of ai-inferred notes (the endorsement-membrane story)
    min_ai = int(inc.get("min_ai_inferred", 0) or 0)
    if min_ai:
        have_ai = sum(1 for nid in selected if provenance_from_path(nid) == "ai-inferred")
        for nid in sorted((n for n in raw_nodes if provenance_from_path(n) == "ai-inferred"),
                          key=lambda k: -scored[k]):
            if have_ai >= min_ai:
                break
            if nid not in selected:
                take(nid)
                have_ai += 1

    # fill each layer up to its cap by score (preserves the 7-layer altitude) --
    by_layer = {}
    for nid, meta in raw_nodes.items():
        by_layer.setdefault(meta.get("layer", "insight"), []).append(nid)
    for layer, ids in by_layer.items():
        cap = int(LAYER_CAP.get(layer, 30))
        have = sum(1 for nid in selected if raw_nodes[nid].get("layer", "insight") == layer)
        for nid in sorted(ids, key=lambda k: -scored[k]):
            if have >= cap:
                break
            if nid not in selected:
                selected[nid] = raw_nodes[nid]
                have += 1

    # ---- build node records -------------------------------------------------
    out_nodes = []
    for nid, meta in selected.items():
        layer_name = meta.get("layer", "insight")
        life = float(meta.get("lifecycle", 0.0) or 0.0)
        stale = float(meta.get("staleness_score", 0.0) or 0.0)
        deg_n = degree.get(nid, 0) / max_deg
        # in-degree on a sqrt (compressed) scale: foundational notes dominate the
        # brightness, but the gradient stays readable instead of collapsing to a
        # few bright dots, and the lifecycle base keeps no node fully black.
        in_n = (in_degree.get(nid, 0) / max_in_deg) ** 0.5
        q = float(qvals.get(nid, 0.0) or 0.0)
        # heat -> brightness: inbound authority is the DOMINANT term, blended with
        # lifecycle; a real usage Q-value still wins when one exists.
        heat = q if q > 0 else max(0.0, min(1.0, 0.12 + in_n * 0.85 + life * 0.32))
        out_nodes.append({
            "id": nid,
            "title": title_from_id(nid),
            "layer": LAYER_NUM.get(layer_name, 3),
            "lifecycle": phase_from_score(life),
            "provenance": provenance_from_path(nid),
            "scope": scope_of(nid),
            "core": scope_of(nid) in CORE_FOLDERS,
            "kind": scope_kind(nid),   # cognitive (claims) | reference (external facts)
            "heat": round(heat, 3),
            "stale": round(stale, 3),
            "degree": degree.get(nid, 0),
            "in_degree": in_degree.get(nid, 0),
            "age_days": note_age_days(nid, git_recency),
            "converged": nid in converged_ids,   # a finished thinking conclusion — highlightable as a group
            "content": note_content(nid),
        })

    sel_ids = set(selected.keys())

    # ---- edges among the selected subset ------------------------------------
    # Keep only edges with both endpoints on the canvas, preferring structural
    # ones (derives-from / instantiates) for the skeleton. Within the MAX_EDGES
    # budget we go connectivity-FIRST: give every node its single strongest edge
    # before spending the rest on the global structural ranking — otherwise the
    # budget is eaten by dense hub-to-hub edges and high-salience peripheral
    # nodes are left as stranded dots.
    pref = {"derives-from": 0, "instantiates": 1, "references": 2, "associates": 3}

    def edge_rank(e):
        return (pref.get(e[2].get("edge_type", "associates"), 4),
                -float(e[2].get("confidence", 0.5) or 0.5))

    cand = sorted((e for e in edge_pairs
                   if e[0] in sel_ids and e[1] in sel_ids and e[0] != e[1]),
                  key=edge_rank)

    out_edges = []
    seen = set()

    def add_edge(s, t, m):
        if (s, t) in seen:
            return False
        seen.add((s, t))
        out_edges.append({
            "source": s, "target": t,
            "type": m.get("edge_type", "associates"),
            "weight": round(float(m.get("confidence", 0.5) or 0.5), 2),
        })
        return True

    # pass 0: guarantee cross-kind bridges (reference ↔ cognitive) render — they
    # are the connection-target payoff of a reference scope and are few, so they
    # take priority over the salience-ranked fill below.
    for s, t, m in cand:
        if len(out_edges) >= MAX_EDGES:
            break
        if is_reference_scope(scope_of(s)) ^ is_reference_scope(scope_of(t)):
            add_edge(s, t, m)
    # pass 1: weave — give each node its best edge so nothing strands
    connected = set(s for e in out_edges for s in (e["source"], e["target"]))
    for s, t, m in cand:
        if len(out_edges) >= MAX_EDGES:
            break
        if s in connected and t in connected:
            continue
        if add_edge(s, t, m):
            connected.add(s)
            connected.add(t)
    # pass 2: fill the remaining budget with the strongest structural edges
    for s, t, m in cand:
        if len(out_edges) >= MAX_EDGES:
            break
        add_edge(s, t, m)

    # ---- tensions -----------------------------------------------------------
    out_tensions = []
    for t in raw_tensions:
        a = t.get("a") or t.get("source")
        b = t.get("b") or t.get("target")
        if a in sel_ids and b in sel_ids:
            out_tensions.append({"a": a, "b": b, "strength": round(float(t.get("strength", 0.6)), 2)})

    # If the live graph has no tension edges yet, synthesize a few demo seams so
    # the "tension pulses red / immune to staleness" feature is demonstrable.
    # Flagged demo:true so they are never mistaken for detected contradictions.
    if not out_tensions and len(out_nodes) >= 6:
        hot = sorted(out_nodes, key=lambda n: -n["heat"])[:12]
        for i in range(0, min(6, len(hot) - 1), 2):
            out_tensions.append({
                "a": hot[i]["id"], "b": hot[i + 1]["id"],
                "strength": round(0.6 + 0.05 * i, 2), "demo": True,
            })

    # ---- incubation topics + real converged conclusions ---------------------
    # (registry + converged already parsed above so selection could guarantee the nodes)
    incubation = parse_registry(registry)

    # ---- briefing: live metrics + activity (surface what matters) -----------
    metrics = build_metrics(raw_nodes, have_enrich)
    activity = build_activity()
    recent = build_recent(out_nodes)

    # per-scope counts on the canvas — drives the orb's scope legend / briefing
    scope_counts = {}
    for n in out_nodes:
        scope_counts[n["scope"]] = scope_counts.get(n["scope"], 0) + 1

    data = {
        "meta": {
            "source": ("Brain Dependency Graph (real export)" if have_enrich
                       else "vault wikilink export (fallback)"),
            "exported": TODAY.isoformat(),
            "total_nodes": len(raw_nodes),
            "total_edges": len(raw_edges),
            "sampled_nodes": len(out_nodes),
            "sampled_edges": len(out_edges),
            # scope view: what is mounted, the core set, and the on-canvas breakdown
            "render_scope": sorted(render_scope) if render_scope is not None else "all",
            "core_folders": sorted(CORE_FOLDERS),
            "reference_folders": sorted(reference_folders()),  # registered reference scopes (Company, …)
            "scope_counts": scope_counts,
        },
        "nodes": out_nodes,
        "edges": out_edges,
        "tensions": out_tensions,
        "incubation": incubation,
        "converged": converged,
        "metrics": metrics,
        "activity": activity,
        "recent": recent,
    }

    # Atomic write: the brain-orb hooks promise a failed export never blanks
    # the previous data.json (trinity-enterprise#76).
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, OUT)
    # data.js exists for the standalone file:// prototype (fetch() is
    # CORS-blocked there). Only emit it when the prototype page is present —
    # the Trinity-served orb never reads it, and it's a second multi-MB copy.
    _report = [f"Wrote {OUT}"]
    if os.path.exists(os.path.join(HERE, "index.html")):
        with open(OUT_JS, "w") as f:
            f.write("/* auto-generated by export_data.py — do not edit by hand */\n")
            f.write("window.AGENT_DATA = ")
            json.dump(data, f, separators=(",", ":"))
            f.write(";\n")
        _report.append(f"Wrote {OUT_JS}")
    # Progress goes to STDERR: the brain-orb hooks run this exporter as a
    # subprocess and their own stdout must stay a single JSON document.
    _scope_label = "all (full graph)" if render_scope is None else ",".join(sorted(render_scope))
    _report += [
        f"  scope:   {_scope_label}",
        f"  nodes:   {len(out_nodes)} (of {len(raw_nodes)})",
        f"  edges:   {len(out_edges)} (of {len(raw_edges)})",
        f"  tensions:{len(out_tensions)}  incubation:{len(incubation)}  converged:{len(converged)}",
        f"  activity:{len(activity)} events  metrics:{len(metrics.get('cards', []))} cards",
    ]
    print("\n".join(_report), file=sys.stderr)


# ============================ recency ============================
def build_git_recency():
    """path (Brain-relative) -> most-recent commit date, from one git pass."""
    rec = {}
    out = git_log("--since=400 days ago", "--date=short",
                  "--pretty=format:@%ad", "--name-only", timeout=40)
    if out is None:
        return rec
    cur = None
    for line in out.splitlines():
        if line.startswith("@"):
            cur = line[1:].strip()
        elif line.endswith(".md") and cur:
            key = line[len("Brain/"):] if line.startswith("Brain/") else line
            if key not in rec:            # newest first -> first seen wins
                rec[key] = cur
    return rec


def note_content(nid, cap=NODE_CONTENT_CAP):
    """Full note body (YAML frontmatter stripped), capped so data.json stays reasonable."""
    path = os.path.join(VAULT, nid)
    if not os.path.exists(path):
        return ""
    txt = strip_frontmatter(read_text(path)).strip()
    if len(txt) > cap:
        txt = txt[:cap].rstrip() + "\n\n…(truncated — open the note in Obsidian for the rest)"
    return txt


def note_age_days(nid, git_recency):
    """Most-recent touch in days. Frontmatter `updated`/`created` vs git, whichever is newer."""
    dates = []
    g = git_recency.get(nid)
    if g and _date(g):
        dates.append(_date(g))
    path = os.path.join(VAULT, nid)
    if os.path.exists(path):
        head = read_text(path, limit=600)
        for field in ("updated", "created"):
            m = re.search(rf"^{field}:\s*([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})", head, re.M)
            if m and _date(m.group(1)):
                dates.append(_date(m.group(1)))
    if not dates:
        return 999
    return max(0, (TODAY - max(dates)).days)


# ============================ metrics ============================
def build_metrics(raw_nodes, have_enrich=True):
    """Live counts for the briefing rail. Authoritative phase counts from `status`."""
    ids = list(raw_nodes.keys())

    def count_prefix(p):
        return sum(1 for k in ids if k.startswith(p))

    fw = sum(1 for n in raw_nodes.values() if n.get("layer") == "framework")
    cards = [
        {"label": "notes indexed", "value": len(ids), "hint": "in the graph"},
        {"label": "permanent", "value": count_prefix("02-Permanent/"), "hint": "atomic insights"},
        {"label": "ai-extracted", "value": count_prefix("AI Extracted Notes/"), "hint": "your own, mined"},
        {"label": "doc insights", "value": count_prefix("Document Insights/"), "hint": "research extracts"},
        {"label": "frameworks", "value": fw, "hint": "structured models"},
    ]

    # authoritative lifecycle / health from the brain-graph engine
    phases = {}
    tension_count = stale_count = None
    # Only consult the BDG engine when enrichments exist — on the fallback path
    # (no pipeline bootstrapped) the call can only fail, and its 60s timeout
    # would eat most of the hook's export budget for nothing.
    st = _run([BRAIN_GRAPH, "status", "--json"], timeout=60) if have_enrich else None
    try:
        sj = json.loads(st[st.index("{"):st.rindex("}") + 1])
        phases = sj.get("lifecycle_phases", {})
        tension_count = sj.get("tension_count")
        stale_count = sj.get("stale_count")
    except Exception:
        # fallback: bucket from enrichment lifecycle scores
        for n in raw_nodes.values():
            ph = phase_from_score(float(n.get("lifecycle", 0) or 0))
            phases[ph] = phases.get(ph, 0) + 1

    return {
        "cards": cards,
        "phases": phases,                 # reflective / crystallizing / generative
        "tension_count": tension_count,
        "stale_count": stale_count,
        "domains": parse_dashboard_domains(),
        "learned_7d": git_added_count(7),
        "bottleneck": phases.get("crystallizing", 0),  # crystallizing notes awaiting synthesis
    }


def git_added_count(days):
    """Number of Brain/*.md notes added in the last `days` (None if git fails)."""
    out = git_log(f"--since={days} days ago", "--diff-filter=A",
                  "--name-only", "--pretty=format:")
    if out is None:
        return None
    return sum(1 for l in out.splitlines() if l.startswith("Brain/") and l.endswith(".md"))


def parse_dashboard_domains():
    """Domain distribution progress bars from dashboard.yaml (no pyyaml dependency)."""
    if not os.path.exists(DASHBOARD):
        return []
    txt = read_text(DASHBOARD)
    # grab the Domain Distribution block
    m = re.search(r"Domain Distribution(.*?)(?:\n  - title:|\Z)", txt, re.S)
    block = m.group(1) if m else txt
    domains = []
    for mm in re.finditer(r"label:\s*\"([^\"]+)\"\s*\n\s*value:\s*(\d+)\s*\n\s*color:\s*(\w+)", block):
        domains.append({"label": mm.group(1), "value": int(mm.group(2)), "color": mm.group(3)})
    return domains


# ============================ activity (live mirror) ============================
def _ago(d):
    days = (TODAY - d).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days}d ago"
    if days < 30:
        return f"{days // 7}w ago"
    return f"{days // 30}mo ago"


def build_activity():
    """Recent agent activity from git — the live pulse of what the agent has been doing."""
    out = git_log("-40", "--date=short", "--pretty=format:%ad%x09%s")
    if out is None:
        return []
    events = []
    for line in out.splitlines():
        if "\t" not in line:
            continue
        ds, subj = line.split("\t", 1)
        d = _date(ds)
        kind, label = "work", subj
        m = re.match(r"Scheduled:\s*([\w-]+)\s+(.*)", subj)
        if m:
            kind, label = m.group(1), m.group(2)
        elif subj.startswith("Seed thinking topic:"):
            kind, label = "incubation-loop", subj.replace("Seed thinking topic:", "seed:").strip()
        events.append({
            "kind": kind,
            "label": label[:60],
            "ago": _ago(d) if d else "",
            "state": SKILL_STATE.get(kind, "speaking"),
        })
        if len(events) >= 16:
            break
    return events


def build_recent(out_nodes):
    """Newest notes among the sampled set — 'what the mind recently learned'."""
    aged = [n for n in out_nodes if n.get("age_days", 999) < 60]
    aged.sort(key=lambda n: n["age_days"])
    return [{"id": n["id"], "title": n["title"], "age_days": n["age_days"],
             "provenance": n["provenance"]} for n in aged[:12]]


# ============================ thinking registry ============================
def parse_converged(text):
    """The real converged conclusions from the Thinking Registry — the agent's actual output."""
    if not text:
        return []
    cm = re.search(r"##\s*Converged.*?\n(.*?)(?:\n##\s|\Z)", text, re.S | re.IGNORECASE)
    if not cm:
        return []
    out = []
    for line in cm.group(1).splitlines():
        if not line.strip().startswith("|"):
            continue
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) < 4:
            continue
        slug = cols[0]
        if not re.match(r"^[a-z0-9][a-z0-9-]+$", slug):
            continue
        out.append({
            "topic": slug,
            "question": cols[1],
            "runs": cols[2],
            "conclusion": cols[3],
        })
    return out[:80]   # the agent's full converged set (was capped at 12 — the voice/briefing could only see a fraction)


def parse_registry(text):
    """Extract incubation topics + confidence from the Thinking Registry tables."""
    if not text:
        return []
    topics = []
    # split into Active vs Converged sections
    conv_match = re.search(r"##\s*Converged.*", text, re.IGNORECASE)
    converged_text = text[conv_match.start():] if conv_match else ""

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|--") or line.startswith("|-"):
            continue
        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) < 2:
            continue
        slug = cols[0]
        if not re.match(r"^[a-z0-9][a-z0-9-]+$", slug):
            continue  # header rows or non-slug rows
        is_conv = converged_text and slug in converged_text and slug not in [t["topic"] for t in topics]
        # confidence: look for "(NN%)" or "(NN/NN" in the row
        conf = None
        m = re.search(r"\((\d{1,3})\s*%\)", line)
        if m:
            conf = int(m.group(1)) / 100.0
        else:
            m2 = re.search(r"\((\d{1,3})/(\d{1,3})", line)
            if m2:
                conf = int(m2.group(1)) / 100.0
        # runs
        runs = 0
        rm = re.search(r"\|\s*(\d+)\s*\|", line)
        if rm:
            try:
                runs = int(rm.group(1))
            except Exception:
                runs = 0
        if conf is None:
            conf = min(0.95, 0.3 + 0.04 * runs)
        topics.append({
            "topic": slug,
            "confidence": round(conf, 2),
            "converged": bool(is_conv),
            "runs": runs,
        })
    # de-dup keeping first occurrence, cap to a readable count
    seen = set()
    uniq = []
    for t in topics:
        if t["topic"] in seen:
            continue
        seen.add(t["topic"])
        uniq.append(t)
    return uniq[:12]


if __name__ == "__main__":
    main()
