"""Template-source bound for agent-created agents (trinity-enterprise#591).

One instance-wide allowlist (`system_settings['agent_creation_allowlist']`, a
JSON array). Empty or absent = unbounded, the behaviour before #591. While it
has entries, a machine principal outside `UNBOUNDED_SCOPES` may create an agent
only from a listed source; people and `trinity-system` are never bounded.

A create-time limit, not containment: it decides which source the platform
builds from, never what the created agent does afterwards (the other grants
govern that, requirements/auth.md §2.9a).

Pure apart from three lazy reads (the setting row, the bundled-catalog root, and
the live fleet for the impact view), so it imports from no router, no
`dependencies` and no Docker module at load time: crud, system_service, the
templates router and the settings router all import it, and the unit harnesses
that rebuild `services.*` around crud must not drag the auth module in.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

SETTING_KEY = "agent_creation_allowlist"
MAX_ENTRIES = 100
MAX_ENTRY_LEN = 200

CODE_SOURCE = "template_source_not_allowed"
CODE_UPLOAD = "template_upload_not_allowed"
CODE_INVALID = "invalid_template_sources"
AUDIT_ACTION = "template_source_refused"

# Who is never bounded: the same allowlist as `dependencies.ADMIN_GATE_SCOPES`
# (JWT = None, a person's `user` key, `trinity-system`). Kept as a copy so this
# leaf never imports the auth module; `test_ent591` pins the two equal. Every
# other scope (agent, connector, ops, portal_delegate, one invented tomorrow) is
# bounded, and so is a principal with no `mcp_scope` at all (#2323).
UNBOUNDED_SCOPES = frozenset({None, "user", "system"})
_SCOPE_ABSENT = object()

# crud's own grammar, mirrored (parity pinned in test_ent591): `local:` names
# (`_LOCAL_TEMPLATE_NAME_RE`), GitHub owner/repo parts (`_GITHUB_REPO_PATH_RE`)
# and refs (`_GITHUB_REF_RE`). Always `fullmatch`: `re.match` admits a trailing
# newline.
_LOCAL_NAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")
_GH_PART_RE = re.compile(r"[A-Za-z0-9_.-]+")
_GH_REF_RE = re.compile(r"(?!-)(?!.*\.\.)(?!\.)(?!.*/\.)(?!.*\.$)(?!.*\.lock$)[A-Za-z0-9_./-]+")

_SETTINGS_PATH = "Settings → Agents → Template sources for agent-created agents"


# ---------------------------------------------------------------------------
# Entries (what an admin writes)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    kind: str                    # "local" | "github_owner" | "github_repo"
    name: str = ""               # local name, exact
    owner: str = ""              # lowercased
    repo: str = ""               # lowercased
    ref: Optional[str] = None    # exact; github_repo only

    @property
    def text(self) -> str:
        if self.kind == "local":
            return f"local:{self.name}"
        if self.kind == "github_owner":
            return f"github:{self.owner}/*"
        return f"github:{self.owner}/{self.repo}" + (f"@{self.ref}" if self.ref else "")


_EXAMPLES = "e.g. local:scout, github:acme/*, github:acme/tools or github:acme/tools@v2"


def parse_entry(raw: Any) -> Tuple[Optional[Entry], Optional[str]]:
    """`(entry, None)` or `(None, reason)`; the reason names the fix and an example."""
    if not isinstance(raw, str):
        return None, f"An entry must be text, {_EXAMPLES}."
    text = raw.strip()
    if not text:
        return None, f"Empty entry. Remove the line, or write a source, {_EXAMPLES}."
    if len(text) > MAX_ENTRY_LEN:
        return None, f"Longer than {MAX_ENTRY_LEN} characters."
    if text.startswith("local:"):
        name = text[len("local:"):]
        if name == "*":
            return None, "local:* is not allowed: name each bundled template, e.g. local:scout."
        if not _LOCAL_NAME_RE.fullmatch(name):
            return None, ("Use local:<template> with letters, digits, '.', '_' or '-', "
                          "starting with a letter or digit, e.g. local:scout.")
        return Entry("local", name=name), None
    if text.startswith("github:"):
        body = text[len("github:"):]
        ref = None
        if "@" in body:
            body, ref = body.rsplit("@", 1)
            if not ref:
                return None, "Nothing after '@'. Write the branch or tag, e.g. github:acme/tools@v2, or drop the '@'."
            if not _GH_REF_RE.fullmatch(ref):
                return None, f"'{ref}' is not a valid branch or tag name."
        owner, sep, repo = body.partition("/")
        if not sep or not owner or not repo or "/" in repo:
            return None, "Use github:<owner>/<repo> or github:<owner>/*, e.g. github:acme/*."
        if not _GH_PART_RE.fullmatch(owner):
            return None, "The owner may use letters, digits, '.', '_' or '-' only, e.g. github:acme/*."
        if repo == "*":
            if ref is not None:
                return None, "github:<owner>/* covers every repo and branch; it takes no @ref."
            return Entry("github_owner", owner=owner.lower()), None
        if not _GH_PART_RE.fullmatch(repo):
            return None, "The repository may use letters, digits, '.', '_' or '-' only, e.g. github:acme/tools."
        return Entry("github_repo", owner=owner.lower(), repo=repo.lower(), ref=ref), None
    if text == "*":
        return None, "A bare * is not allowed. Leave the list empty to allow any source."
    return None, f"Start with local: or github:, {_EXAMPLES}."


def parse_entries(raw_list: Any) -> Tuple[List[Entry], List[dict]]:
    """Every entry parsed: `(entries, errors)`. Duplicates (after normalising)
    are dropped; an error names its index, the entry as written, and the fix."""
    if not isinstance(raw_list, list):
        return [], [{"index": None, "entry": None, "reason": "The list must be a JSON array of strings."}]
    if len(raw_list) > MAX_ENTRIES:
        return [], [{"index": None, "entry": None, "reason": f"At most {MAX_ENTRIES} entries."}]
    entries: List[Entry] = []
    errors: List[dict] = []
    seen = set()
    for i, raw in enumerate(raw_list):
        entry, reason = parse_entry(raw)
        if reason:
            errors.append({"index": i, "entry": raw if isinstance(raw, str) else None, "reason": reason})
            continue
        if entry.text not in seen:
            seen.add(entry.text)
            entries.append(entry)
    return entries, errors


# ---------------------------------------------------------------------------
# The stored list
# ---------------------------------------------------------------------------

@dataclass
class Stored:
    entries: List[Entry] = field(default_factory=list)
    bounded: bool = False
    invalid: bool = False        # the row holds something the API never writes

    @property
    def texts(self) -> List[str]:
        return [e.text for e in self.entries]


def read_stored() -> Stored:
    """The saved list. No row (or `[]`) = unbounded. A row that is not a JSON
    list bounds agents to NOTHING; a list with some bad entries keeps the valid
    ones. Both mark `invalid`, which the panel shows (fail closed, visibly)."""
    from database import db  # lazy: the conftest re-imports `database`
    raw = db.get_setting_value(SETTING_KEY)
    if raw is None:
        return Stored()
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        value = None
    if not isinstance(value, list):
        logger.error("[ent#591] %s holds a non-list value; agents are bounded to no source", SETTING_KEY)
        return Stored(bounded=True, invalid=True)
    entries, errors = parse_entries(value[:MAX_ENTRIES])
    invalid = bool(errors) or len(value) > MAX_ENTRIES
    if invalid:
        logger.error("[ent#591] %s holds %d invalid entr(y/ies); they admit nothing", SETTING_KEY, len(errors))
    return Stored(entries=entries, bounded=bool(value), invalid=invalid)


def write_entries(entries: List[Entry]) -> None:
    """Persist (canonical text). An empty list deletes the row: unbounded."""
    from database import db
    if not entries:
        db.delete_setting(SETTING_KEY)
        return
    db.set_setting(SETTING_KEY, json.dumps([e.text for e in entries]))


# ---------------------------------------------------------------------------
# A create's source (what an agent asks for)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    kind: str                    # "blank" | "local" | "github" | "unrecognised"
    name: str = ""
    owner: str = ""
    repo: str = ""
    ref: Optional[str] = None    # the template's own @ref

    @property
    def text(self) -> Optional[str]:
        """Canonical, never the raw string: this is what reaches the refusal,
        the audit row and the log."""
        if self.kind == "local":
            return f"local:{self.name}"
        if self.kind == "github":
            return f"github:{self.owner}/{self.repo}" + (f"@{self.ref}" if self.ref else "")
        return None


def source_of(template: Optional[str]) -> Source:
    """Mirror of crud's dispatch (`_parse_github_ref`): split on the LAST '@',
    an empty ref is no ref, and the repo path is exactly owner/repo. Anything
    crud would not build from as written is `unrecognised`."""
    if template is None or template == "":
        return Source("blank")
    if not isinstance(template, str):
        return Source("unrecognised")
    if template.startswith("local:"):
        name = template[len("local:"):]
        return Source("local", name=name) if _LOCAL_NAME_RE.fullmatch(name) else Source("unrecognised")
    if template.startswith("github:"):
        body, ref = template[len("github:"):], None
        if "@" in body:
            body, ref = body.rsplit("@", 1)
            ref = ref or None
            if ref is not None and not _GH_REF_RE.fullmatch(ref):
                return Source("unrecognised")
        owner, sep, repo = body.partition("/")
        if not (sep and _GH_PART_RE.fullmatch(owner) and _GH_PART_RE.fullmatch(repo)):
            return Source("unrecognised")
        return Source("github", owner=owner.lower(), repo=repo.lower(), ref=ref)
    return Source("unrecognised")


def effective_ref(source: Source, *, source_branch: Optional[str] = None,
                  fork: bool = False, copy: bool = False,
                  source_mode: Optional[bool] = None) -> Optional[str]:
    """The branch or tag the create will really CLONE, or None when that is the
    repo's default branch and not knowable here (a pinned entry then never
    matches). crud moves a template's `@ref` into `source_branch`, else the
    body's `source_branch` (model default `main`) stands. Three builds ignore it:
    `fork_to_own` copies the default branch; a copy treats `main` as "the default
    branch" (crud's copy staging); and a working-branch agent
    (`source_mode: false`) clones the default branch, because startup.sh passes
    `-b` only in source mode."""
    if fork:
        return None
    branch = source.ref or source_branch or "main"
    if copy:
        return None if branch == "main" else branch
    if source_mode is False:
        return None
    return branch


def _bundled_root() -> Path:
    """The bundled (curated) catalog root, read where crud reads it — the first
    of `_LOCAL_TEMPLATE_ROOTS`, which the create harnesses repoint."""
    from services.agent_service.crud import _LOCAL_TEMPLATE_ROOTS
    return _LOCAL_TEMPLATE_ROOTS[0]


def is_bundled(name: str) -> bool:
    """Does `local:<name>` resolve in the bundled catalog? A same-named upload in
    the deploy-local store, which any creator can replace, never counts. The
    name is regex-checked first, so it cannot traverse."""
    if not _LOCAL_NAME_RE.fullmatch(name or ""):
        return False
    try:
        return (_bundled_root() / name / "template.yaml").is_file()
    except Exception:  # noqa: BLE001 — an unreadable catalog admits nothing
        logger.warning("[ent#591] bundled catalog unreadable", exc_info=True)
        return False


def is_hidden(name: str) -> bool:
    """Is the bundled `local:<name>` marked `hidden: true`? Such a template is
    creatable by id but absent from `GET /api/templates` (#1513), so an agent
    reading `allowed_for_agents` never sees it. Read with the hardened loader;
    an unreadable file reads as not hidden (this only labels the admin view)."""
    if not is_bundled(name):
        return False
    try:
        from utils.safe_yaml import load_template_yaml
        data = load_template_yaml((_bundled_root() / name / "template.yaml").read_text())
    except Exception:  # noqa: BLE001
        return False
    return isinstance(data, dict) and bool(data.get("hidden"))


def match(entries: Iterable[Entry], source: Source, ref: Optional[str]) -> Optional[Entry]:
    """The entry that admits this source, or None. An unpinned entry is
    preferred over a pinned one, so a pin's extra condition (source mode,
    `enforce`) applies only when nothing looser admits the create."""
    entries = list(entries)
    if source.kind == "local":
        hit = next((e for e in entries if e.kind == "local" and e.name == source.name), None)
        return hit if hit is not None and is_bundled(source.name) else None
    if source.kind != "github":
        return None
    for e in entries:
        if e.kind == "github_owner" and e.owner == source.owner:
            return e
        if e.kind == "github_repo" and e.owner == source.owner and e.repo == source.repo and e.ref is None:
            return e
    for e in entries:
        if (e.kind == "github_repo" and e.owner == source.owner and e.repo == source.repo
                and e.ref is not None and e.ref == ref):
            return e
    return None


def allows(entries: Iterable[Entry], source: Source, ref: Optional[str]) -> bool:
    return match(entries, source, ref) is not None


# ---------------------------------------------------------------------------
# Who is bounded, and the refusal
# ---------------------------------------------------------------------------

def is_bounded_principal(current_user: Any) -> bool:
    """Bounded unless the scope is in `UNBOUNDED_SCOPES`. A principal without
    the attribute is bounded (never `getattr(..., None)`, which would make the
    absent attribute the privileged JWT value — the #2323 trap)."""
    scope = getattr(current_user, "mcp_scope", _SCOPE_ABSENT)
    return scope is _SCOPE_ABSENT or scope not in UNBOUNDED_SCOPES


@dataclass(frozen=True)
class Refusal:
    code: str
    message: str
    source: Optional[str]
    allowed_sources: List[str]
    list_unreadable: bool = False   # the stored row is damaged (fail closed)

    def detail(self, **extra) -> dict:
        body = {"code": self.code, "message": self.message, "allowed_sources": self.allowed_sources}
        if self.code == CODE_SOURCE:
            body["template"] = self.source
        if self.list_unreadable:
            body["list_unreadable"] = True
        body.update(extra)
        return body


_LEAD = "This instance limits which templates an agent may create agents from"


def _source_message(source: Source) -> str:
    if source.kind == "blank":
        what = "and a blank agent (no template) is not allowed while the list is set"
    elif source.kind == "unrecognised":
        what = "and this template is not a recognised local: or github: source"
    else:
        what = f"and {source.text} is not on the list"
    return (f"{_LEAD}, {what}. Create the agent from one of allowed_sources instead, or ask a "
            f"person to create it: people are not limited by this list. Only an instance admin "
            f"can change the list ({_SETTINGS_PATH}).")


def _unreadable_message(allowed: List[str]) -> str:
    """A damaged row: never "X is not on the list", which would send the agent
    hunting for a source while the real cause is the list itself."""
    if allowed:
        return (f"{_LEAD}, but the saved list could not be read in full, so agents are limited "
                f"to allowed_sources, the entries that could be read, until an instance admin "
                f"saves it again ({_SETTINGS_PATH}). Create the agent from one of them, or ask a "
                f"person to create it: people are not limited by this list.")
    return (f"{_LEAD}, but the saved list could not be read, so agents may not create agents "
            f"from any template until an instance admin saves it again ({_SETTINGS_PATH}). Ask "
            f"a person to create the agent meanwhile: people are not limited by this list.")


UPLOAD_MESSAGE = (
    f"{_LEAD}, so an agent cannot deploy an uploaded archive: an upload is never on the list. "
    "Create the agent from one of allowed_sources, or ask a person to deploy it."
)


def _decide(current_user: Any, config: Any, *, upload: bool,
            stored: Optional[Stored]) -> Tuple[Optional[Refusal], Optional[Entry]]:
    """`(refusal, None)`, or `(None, the admitting entry)` — the entry is None
    when the create is not bounded at all."""
    if not is_bounded_principal(current_user):
        return None, None
    stored = stored if stored is not None else read_stored()
    if not stored.bounded:
        return None, None
    allowed = stored.texts
    if upload:
        return Refusal(CODE_UPLOAD, UPLOAD_MESSAGE, None, allowed, stored.invalid), None
    source = source_of(getattr(config, "template", None))
    ref = effective_ref(
        source,
        source_branch=getattr(config, "source_branch", None),
        fork=bool(getattr(config, "fork_to_own", None)),
        copy=getattr(config, "import_intent", None) == "copy",
        source_mode=getattr(config, "source_mode", None),
    )
    hit = match(stored.entries, source, ref)
    if hit is not None:
        return None, hit
    message = _unreadable_message(allowed) if stored.invalid else _source_message(source)
    return Refusal(CODE_SOURCE, message, source.text, allowed, stored.invalid), None


def refusal_for(current_user: Any, config: Any = None, *, upload: bool = False,
                stored: Optional[Stored] = None) -> Optional[Refusal]:
    """None when this create may go ahead; else the refusal. The principal is
    judged first, so a person's create never reads the setting."""
    return _decide(current_user, config, upload=upload, stored=stored)[0]


def manifest_refusals(current_user: Any, members: Iterable[Tuple[str, str, Optional[str]]]
                      ) -> Tuple[List[Tuple[str, str, Optional[str], Refusal]], List[str]]:
    """One validator for a system deploy's preview AND its write (ent#1030):
    `members` is `(final_name, short_name, template)`. Returns the refused
    members and the allowed list; the setting is read once."""
    if not is_bounded_principal(current_user):
        return [], []
    stored = read_stored()
    if not stored.bounded:
        return [], []
    refused = []
    for final_name, short_name, template in members:
        r = refusal_for(current_user, _MemberConfig(template), stored=stored)
        if r is not None:
            refused.append((final_name, short_name, template, r))
    return refused, stored.texts


@dataclass(frozen=True)
class _MemberConfig:
    """A manifest member creates from `template` with the model's default
    `source_branch` (`main`) — `_member_agent_config` / deploy build exactly that."""
    template: Optional[str]
    source_branch: str = "main"
    fork_to_own: Any = None
    import_intent: Optional[str] = None


def manifest_message(refused: List[Tuple[str, str, Optional[str], Refusal]]) -> str:
    listed = ", ".join(f"{short}={r.source or ('no template' if not t else 'unrecognised')}"
                       for _, short, t, r in refused)
    if refused and refused[0][3].list_unreadable:
        in_full = " in full" if refused[0][3].allowed_sources else ""
        return (f"{_LEAD}, but the saved list could not be read{in_full}, so nothing was "
                f"deployed: {len(refused)} member(s) of this manifest use a template agents may "
                f"not use until an instance admin saves the list again ({_SETTINGS_PATH}): "
                f"{listed}. Ask a person to deploy the manifest meanwhile.")
    return (f"{_LEAD}. {len(refused)} member(s) of this manifest use a template that is not on "
            f"the list, so nothing was deployed: {listed}. Change those templates to allowed "
            "sources, or ask a person to deploy the manifest.")


# ---------------------------------------------------------------------------
# Enforcement (raise + audit)
# ---------------------------------------------------------------------------

async def audit_refusal(request: Any, current_user: Any, *, code: str,
                        source: Optional[str] = None, target: Optional[str] = None,
                        **details) -> None:
    """One `template_source_refused` AUTHORIZATION row. Best-effort, and safe
    with `request=None` (background callers)."""
    try:
        from services.platform_audit_service import (
            AuditEventType, platform_audit_service, principal_actor,
        )
        await platform_audit_service.log(
            event_type=AuditEventType.AUTHORIZATION,
            event_action=AUDIT_ACTION,
            source="api",
            **principal_actor(current_user),
            actor_ip=getattr(getattr(request, "client", None), "host", None),
            target_type="agent",
            target_id=target,
            endpoint=(getattr(request, "scope", None) or {}).get("path"),
            request_id=getattr(getattr(request, "state", None), "request_id", None),
            details={"bound": SETTING_KEY, "code": code, "template": source,
                     "method": getattr(request, "method", None), **details},
        )
    except Exception:  # noqa: BLE001
        logger.warning("[ent#591] refusal audit failed (%s)", code)


def _pin_source_mode(config: Any, hit: Optional[Entry]) -> None:
    """A create admitted only by a pinned entry must track that ref, which only
    source mode does. When the caller left the mode unset, make it explicit:
    ent#705 honours an explicit `source_mode` and so cannot turn the create into
    a working branch cloned from the default branch (ruled 2026-11-06). An
    explicit `source_mode: false` never reaches here — it matches no pin."""
    if hit is None or hit.ref is None:
        return
    if bool(getattr(config, "fork_to_own", None)) or getattr(config, "import_intent", None) == "copy":
        return  # neither clones by mode; a pin was matched on their own rule
    fields_set = getattr(config, "model_fields_set", None)
    if fields_set is None or "source_mode" in fields_set:
        return
    config.source_mode = True
    logger.info("[ent#591] %s matched a pinned entry (%s): created in source mode",
                getattr(config, "name", None), hit.text)


async def enforce(request: Any, current_user: Any, config: Any = None, *,
                  upload: bool = False) -> None:
    """Refuse with a named 403 and one audit row, or return. Call it before any
    side effect of the create it guards. A create admitted only by a pinned
    entry leaves in source mode (`_pin_source_mode`)."""
    refusal, hit = _decide(current_user, config, upload=upload, stored=None)
    if refusal is None:
        _pin_source_mode(config, hit)
        return
    from fastapi import HTTPException
    from utils.helpers import sanitize_agent_name
    ephemeral = bool(getattr(config, "ephemeral", None))
    raw_name = getattr(config, "name", None)
    # The slug the create would use, never the raw body (the ent#164 rule: a row
    # names an agent that can exist, and a raw name can carry what a row must
    # not). A ghost's name is a base the create would suffix: it never exists,
    # so it is detail, not the target.
    name = (sanitize_agent_name(raw_name) or None) if isinstance(raw_name, str) else None
    await audit_refusal(
        request, current_user, code=refusal.code, source=refusal.source,
        target=None if (ephemeral or upload) else name,
        ephemeral=ephemeral, requested_name=name if ephemeral else None,
    )
    raise HTTPException(status_code=403, detail=refusal.detail())


# ---------------------------------------------------------------------------
# Reads for the admin panel and the template list
# ---------------------------------------------------------------------------

def annotate_templates(current_user: Any, templates: List[dict]) -> List[dict]:
    """`allowed_for_agents` per catalog entry, for a bounded principal while a
    list is set; otherwise the list is returned untouched (byte-identical)."""
    if not is_bounded_principal(current_user):
        return templates
    stored = read_stored()
    if not stored.bounded:
        return templates
    out = []
    for t in templates:
        if not isinstance(t, dict):
            out.append(t)
            continue
        source = source_of(t.get("id"))
        # A copy: the catalog may hand out cached dicts another caller reads.
        out.append({**t, "allowed_for_agents": allows(stored.entries, source, effective_ref(source))})
    return out


def in_use(stored: Stored) -> List[dict]:
    """The templates live agent-created agents use, each allowed or refused
    under the saved list. Raises on a failed read (the caller degrades)."""
    from database import db
    from services.docker_service import agent_container_states, list_all_agents_fast
    # `list_all_agents_fast` answers [] when Docker cannot be read (#2196), so
    # the tri-state reader decides "unreadable" first: None is "couldn't read",
    # never "none". Containers that exist but list as nothing is a failed read too.
    states = agent_container_states()
    if states is None:
        raise RuntimeError("Docker could not be read")
    agents = list_all_agents_fast()
    if states and not agents:
        raise RuntimeError("Docker listed no agents while containers exist")
    created = set(db.list_agent_created_agent_names())
    counts: dict = {}
    for agent in agents:
        if agent.name not in created:
            continue
        source = source_of(getattr(agent, "template", None))
        key = source.text or ("no template" if source.kind == "blank" else "unrecognised")
        row = counts.setdefault(key, {
            "source": key,
            "agents": 0,
            "allowed": (not stored.bounded) or allows(stored.entries, source, effective_ref(source)),
        })
        row["agents"] += 1
    return sorted(counts.values(), key=lambda r: (r["allowed"], r["source"]))


def describe() -> dict:
    """The admin GET: the saved list, its state, listed `local:` names that are
    not bundled, and the impact read (None when it could not be read)."""
    stored = read_stored()
    try:
        usage = in_use(stored)
    except Exception:  # noqa: BLE001 — the panel says "couldn't read", never "none"
        logger.warning("[ent#591] in-use read failed", exc_info=True)
        usage = None
    return {
        "entries": stored.texts,
        "bounded": stored.bounded,
        "stored_invalid": stored.invalid,
        "not_bundled": [e.text for e in stored.entries if e.kind == "local" and not is_bundled(e.name)],
        "hidden": [e.text for e in stored.entries if e.kind == "local" and is_hidden(e.name)],
        "in_use": usage,
        "max_entries": MAX_ENTRIES,
    }
