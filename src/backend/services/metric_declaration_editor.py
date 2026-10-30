"""Edit ONE `metrics:` item in an agent's `template.yaml` (trinity-enterprise#1006).

The UI declares, edits and retires metrics by writing the agent's
`template.yaml`, which stays the single source of truth (operator ruling
2026-10-28: no second registry in the database). This module is the pure half
of that write: given the file's text, it produces the new text. The I/O half —
reading the file out of the container, the compare-and-swap, the audited write
and the registry refresh — is `services/metric_declaration_service.py`.

## Only the target entry changes

A `template.yaml` is hand-authored. Re-serialising the whole document through a
YAML dumper would drop every comment and re-order keys the author chose, so the
editor works on LINES: it finds the top-level `metrics:` key, the block under
it and the span of each item, then replaces, appends or removes exactly one
item's lines. Every other byte of the file is left as it was.

## Verified, or nothing is written

A line editor can be fooled by a layout it does not understand. So the result
is parsed again with the same hardened loader the platform reads templates
with, and compared with the intended result: every top-level key other than
`metrics` must be unchanged and the `metrics:` list must equal the old list
with exactly this one change applied. Anything else — and the layouts refused
up front (flow style, anchors, aliases, tags, a duplicated `metrics:` key) —
raises `TemplateNotEditable` and the caller writes nothing.

## Validation is the loader's

`validate` runs the candidate entry through `template_metrics`, the parser the
registry reconciles with, so a form write and a hand edit cannot disagree about
what is valid. Three rules are the form's own: a cadence is required (every
metric declared here is stale-checkable from day one), a duplicate name is
refused before the parser sees it, and the 50-metric cap is refused rather
than truncated. Name and type cannot change on an edit — points are stored by
name and read in the type's units.
"""

import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

import yaml

from services import template_metrics
from utils.safe_yaml import load_template_yaml

# Form-owned keys, in the order an entry is written. `x-recorded-by` is the
# form's free-text note; every OTHER `x-` key on an existing entry is preserved
# across an edit, because some other tool wrote it.
ENTRY_KEY_ORDER = (
    "name", "type", "label", "description", "unit", "cadence", "direction",
    "aggregation", "values", "dimensions", "warning_threshold", "critical_threshold",
)
RECORDED_BY_KEY = "x-recorded-by"
MAX_RECORDED_BY_LEN = 200

OPS = ("add", "replace", "remove")

# Codes a caller can branch on; the HTTP layer maps them.
TEMPLATE_NOT_EDITABLE = "template_not_editable"
TEMPLATE_UNREADABLE = "template_unreadable"


class TemplateNotEditable(Exception):
    """The edit cannot be made safely. Nothing must be written."""

    code = TEMPLATE_NOT_EDITABLE

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class TemplateUnreadable(TemplateNotEditable):
    """The template does not parse, or is not a mapping."""

    code = TEMPLATE_UNREADABLE


class MetricNotFound(Exception):
    """No `metrics:` entry carries that name."""


class DeclarationInvalid(Exception):
    """The candidate declaration has named errors, each keyed by form field."""

    def __init__(self, errors: List[Dict[str, str]]):
        super().__init__("; ".join(e["message"] for e in errors))
        self.errors = errors


def template_hash(raw: str) -> str:
    """The compare-and-swap token: sha256 of the exact text."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Building an entry from form fields
# ---------------------------------------------------------------------------


def _clean(value: Any) -> Any:
    """Strip strings; map empties to None so they are omitted."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, (list, dict)) and not value:
        return None
    return value


def _clean_status_values(values: Any) -> Any:
    if not isinstance(values, list):
        return _clean(values)
    out = []
    for item in values:
        if isinstance(item, dict):
            cleaned = {k: _clean(item.get(k)) for k in ("value", "color", "label")}
            out.append({k: v for k, v in cleaned.items() if v is not None})
        else:
            out.append(item)
    return out or None


def build_entry(fields: Dict[str, Any], *, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The canonical `metrics:` entry for these form fields.

    Keys in `ENTRY_KEY_ORDER`, empties omitted, then every `x-` key of
    `existing` (an edit) except the one the form owns, then `x-recorded-by`.
    Values are passed through as given: a wrong type is the parser's to name
    in `validate`, so the error comes back on the right field.
    """
    entry: Dict[str, Any] = {}
    for key in ENTRY_KEY_ORDER:
        value = fields.get(key)
        value = _clean_status_values(value) if key == "values" else _clean(value)
        if value is not None:
            entry[key] = value
    if isinstance(existing, dict):
        for key, value in existing.items():
            if isinstance(key, str) and key.startswith("x-") and key != RECORDED_BY_KEY:
                entry[key] = value
    recorded_by = _clean(fields.get("recorded_by"))
    if recorded_by is not None:
        entry[RECORDED_BY_KEY] = recorded_by
    return entry


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_ERR_PREFIX = re.compile(r"^metrics\[\d+\](?:\.([a-z_]+))?(?:\[\d+\])*(?:\.[a-z_]+)?:\s*")
_REQUIRED_KEY = re.compile(r"'(name|type|values)'")


def _field_error(message: str) -> Dict[str, str]:
    """A parser error string → `{field, code, message}` for inline display."""
    match = _ERR_PREFIX.match(message)
    field = match.group(1) if match and match.group(1) else None
    text = message[match.end():] if match else message
    if field is None:
        found = _REQUIRED_KEY.search(message)
        field = found.group(1) if found else "_form"
    text = text[:1].upper() + text[1:] if text else message
    return {"field": field, "code": "metric_invalid", "message": text}


def _find(entries: List[Any], name: str) -> Optional[int]:
    for index, entry in enumerate(entries):
        if isinstance(entry, dict) and entry.get("name") == name:
            return index
    return None


def validate(old_entries: Any, op: str, name: str, entry: Dict[str, Any]) -> None:
    """Raise `DeclarationInvalid` unless `entry` may be written as `op`.

    `old_entries` is the template's current `metrics:` list as parsed. Only the
    candidate entry is run through the parser: an unrelated entry that is
    already broken is the template's existing state, not this write's error.
    """
    entries = old_entries if isinstance(old_entries, list) else []
    errors: List[Dict[str, str]] = []

    if op == "replace":
        index = _find(entries, name)
        if index is None:
            raise MetricNotFound(name)
        old = entries[index]
        if entry.get("name") != name:
            errors.append({"field": "name", "code": "metric_name_immutable",
                           "message": "A metric's name can't change. Retire it and declare a new one."})
        elif entry.get("type") != old.get("type"):
            errors.append({"field": "type", "code": "metric_type_immutable",
                           "message": "A metric's type can't change once declared, so earlier points keep their meaning."})
        if errors:
            raise DeclarationInvalid(errors)
    elif op == "add":
        if _find(entries, entry.get("name")) is not None:
            errors.append({"field": "name", "code": "metric_duplicate",
                           "message": f"This agent already declares {entry.get('name')}. Choose another name."})
        elif len(entries) >= template_metrics.MAX_DECLARED_METRICS:
            errors.append({"field": "_form", "code": "metric_cap_reached",
                           "message": (f"This agent already declares {template_metrics.MAX_DECLARED_METRICS} "
                                       "metrics, the most allowed. Retire one to declare another.")})
        if errors:
            raise DeclarationInvalid(errors)
    else:
        raise ValueError(f"validate does not handle op {op!r}")

    if not entry.get("cadence"):
        errors.append({"field": "cadence", "code": "cadence_required",
                       "message": "Choose how often this metric is recorded."})
    shape = [_field_error(m) for m in template_metrics.metric_shape_errors([entry])]
    if any(e["field"] == "name" for e in shape):
        # The parser stops at a bad name; check the rest under a stand-in so
        # the form shows every field's error at once, not one per save.
        stand_in = {**entry, "name": "name_placeholder"}
        shape += [e for e in (_field_error(m) for m in template_metrics.metric_shape_errors([stand_in]))
                  if e["field"] != "name"]
    errors.extend(shape)
    if errors:
        raise DeclarationInvalid(errors)


# ---------------------------------------------------------------------------
# Locating the block and its items
# ---------------------------------------------------------------------------

_KEY_RE = re.compile(r"^metrics[ \t]*:(?P<rest>.*)$")
_EMPTY_INLINE = {"[]", "~", "null", "Null", "NULL"}
_UNSAFE_ITEM_START = ("{", "[", "&", "*", "!", "|", ">")


def _body(line: str) -> str:
    return line.rstrip("\r\n")


def _indent(line: str) -> int:
    body = _body(line)
    return len(body) - len(body.lstrip(" "))


def _is_blank(line: str) -> bool:
    return not _body(line).strip()


def _is_comment(line: str) -> bool:
    return _body(line).lstrip(" ").startswith("#")


def _is_content(line: str) -> bool:
    return not _is_blank(line) and not _is_comment(line)


def _is_item_start(line: str, indent: int) -> bool:
    body = _body(line)
    if _indent(line) != indent:
        return False
    rest = body[indent:]
    return rest == "-" or rest.startswith("- ")


def _drop_comment(rest: str) -> str:
    """The value after `metrics:`, without a trailing ` # comment`."""
    for marker in (" #", "\t#"):
        cut = rest.find(marker)
        if cut != -1:
            rest = rest[:cut]
    return rest.strip()


class _Layout:
    """Where the `metrics:` key, its block and each item's lines are."""

    def __init__(self) -> None:
        self.key: Optional[int] = None          # line index of `metrics:`
        self.empty_inline = False               # `metrics: []` / `~` / `null`
        self.item_indent = 2
        self.end = 0                            # first line after the last item
        self.spans: List[Tuple[int, int, int]] = []   # (comment_start, item_start, end)


def _layout(lines: List[str]) -> _Layout:
    layout = _Layout()
    keys = [i for i, line in enumerate(lines) if _KEY_RE.match(_body(line))]
    if not keys:
        return layout
    if len(keys) > 1:
        raise TemplateNotEditable("template.yaml declares `metrics:` more than once.")
    key = layout.key = keys[0]
    rest = _drop_comment(_KEY_RE.match(_body(lines[key])).group("rest"))
    if rest in _EMPTY_INLINE:
        layout.empty_inline = True
        layout.end = key + 1
        return layout
    if rest:
        raise TemplateNotEditable(
            "The metrics: block uses a layout this form can't edit safely "
            "(a one-line list, an anchor, an alias or a tag).")

    first = next((j for j in range(key + 1, len(lines)) if _is_content(lines[j])), None)
    if first is None or (_indent(lines[first]) == 0 and not _is_item_start(lines[first], 0)):
        # `metrics:` with nothing under it: null, i.e. no entries.
        layout.empty_inline = True
        layout.end = key + 1
        return layout
    indent = _indent(lines[first])
    if not _is_item_start(lines[first], indent):
        raise TemplateNotEditable("The metrics: block is not a list.")
    layout.item_indent = indent

    end = len(lines)
    for j in range(first, len(lines)):
        line = lines[j]
        if not _is_content(line):
            continue
        if _indent(line) < indent or (_indent(line) == indent and not _is_item_start(line, indent)):
            end = j
            break
    # Blank lines and comments at the very end belong to whatever follows.
    while end > first and not _is_content(lines[end - 1]):
        end -= 1
    layout.end = end

    starts = [j for j in range(first, end) if _is_item_start(lines[j], indent)]
    for start in starts:
        item_text = _body(lines[start])[indent + 1:].lstrip(" ")
        if item_text.startswith(_UNSAFE_ITEM_START):
            raise TemplateNotEditable(
                "A metrics: entry uses a layout this form can't edit safely "
                "(a one-line mapping, an anchor, an alias or a tag).")
    spans = []
    for n, start in enumerate(starts):
        comment_start = start
        floor = key + 1 if n == 0 else starts[n - 1] + 1
        while (comment_start - 1 >= floor and _is_comment(lines[comment_start - 1])
               and _indent(lines[comment_start - 1]) >= indent):
            comment_start -= 1
        spans.append([comment_start, start, None])
    for n, span in enumerate(spans):
        span[2] = spans[n + 1][0] if n + 1 < len(spans) else end
    layout.spans = [tuple(s) for s in spans]
    return layout


# ---------------------------------------------------------------------------
# Emitting
# ---------------------------------------------------------------------------


class _Dumper(yaml.SafeDumper):
    """Indent nested lists under their key, the way people write them."""

    def increase_indent(self, flow=False, indentless=False):  # noqa: D401
        return super().increase_indent(flow, False)


def _emit_item(entry: Dict[str, Any], indent: int, newline: str) -> List[str]:
    text = yaml.dump(
        [entry], Dumper=_Dumper, default_flow_style=False, sort_keys=False,
        allow_unicode=True, width=4096,
    )
    pad = " " * indent
    return [(pad + line if line else line) + newline for line in text.split("\n")[:-1]]


# ---------------------------------------------------------------------------
# Parse, apply, verify
# ---------------------------------------------------------------------------


def _parse(raw: str) -> Dict[str, Any]:
    try:
        doc = load_template_yaml(raw)
    except Exception as e:  # noqa: BLE001 — the hardened loader raises its own
        raise TemplateUnreadable(
            f"template.yaml could not be parsed ({type(e).__name__}).") from e
    if not isinstance(doc, dict):
        raise TemplateUnreadable("template.yaml is not a mapping.")
    return doc


def _entries_of(doc: Dict[str, Any]) -> List[Any]:
    block = doc.get("metrics")
    if block is None:
        return []
    if not isinstance(block, list):
        raise TemplateNotEditable("The metrics: block is not a list.")
    return block


def read_declarations(raw: str) -> Dict[str, Any]:
    """The editor's read: the entries as written, the hash, and editability.

    Raises `TemplateUnreadable` when the file does not parse. A file that
    parses but whose layout the line editor refuses comes back with
    `editable: False` and the reason, so the UI can say so before the person
    fills in a form that could never be saved.
    """
    doc = _parse(raw)
    try:
        entries = _entries_of(doc)
    except TemplateNotEditable as e:
        return {"template_hash": template_hash(raw), "editable": False,
                "reason": e.message, "entries": []}
    reason = None
    try:
        layout = _layout(raw.splitlines(keepends=True))
        if len(layout.spans) != len(entries):
            reason = "The metrics: block uses a layout this form can't edit safely."
    except TemplateNotEditable as e:
        reason = e.message
    return {
        "template_hash": template_hash(raw),
        "editable": reason is None,
        "reason": reason,
        "entries": [e for e in entries if isinstance(e, dict)],
    }


def apply(raw: str, op: str, name: str, entry: Optional[Dict[str, Any]] = None) -> str:
    """The new template text with one `metrics:` item added, replaced or removed.

    Raises `MetricNotFound`, `TemplateUnreadable` or `TemplateNotEditable`; in
    every one of those cases the caller writes nothing.
    """
    if op not in OPS:
        raise ValueError(f"unknown op {op!r}")
    if op != "remove" and not isinstance(entry, dict):
        raise ValueError("add/replace need an entry")

    old_doc = _parse(raw)
    old_entries = _entries_of(old_doc)
    newline = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.splitlines(keepends=True)
    layout = _layout(lines)
    if layout.key is not None and not layout.empty_inline and len(layout.spans) != len(old_entries):
        raise TemplateNotEditable("The metrics: block uses a layout this form can't edit safely.")

    index = None if op == "add" else _find(old_entries, name)
    if op != "add" and index is None:
        raise MetricNotFound(name)

    if op == "add":
        expected = list(old_entries) + [entry]
        if layout.key is None:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] = lines[-1] + newline
            new_lines = lines + ["metrics:" + newline] + _emit_item(entry, 2, newline)
        elif layout.empty_inline:
            new_lines = (lines[:layout.key] + ["metrics:" + newline]
                         + _emit_item(entry, 2, newline) + lines[layout.key + 1:])
        else:
            new_lines = (lines[:layout.end] + _emit_item(entry, layout.item_indent, newline)
                         + lines[layout.end:])
    elif op == "replace":
        expected = list(old_entries)
        expected[index] = entry
        _, start, end = layout.spans[index]
        new_lines = (lines[:start] + _emit_item(entry, layout.item_indent, newline)
                     + lines[end:])
    else:
        expected = [e for i, e in enumerate(old_entries) if i != index]
        comment_start, _, end = layout.spans[index]
        if expected:
            new_lines = lines[:comment_start] + lines[end:]
        else:
            new_lines = lines[:layout.key] + ["metrics: []" + newline] + lines[layout.end:]

    new_raw = "".join(new_lines)
    _verify(old_doc, new_raw, expected)
    return new_raw


def _verify(old_doc: Dict[str, Any], new_raw: str, expected: List[Any]) -> None:
    """Refuse unless the new text means exactly the intended change."""
    try:
        new_doc = load_template_yaml(new_raw)
    except Exception as e:  # noqa: BLE001
        raise TemplateNotEditable(
            "This change would leave template.yaml unparseable, so it was not made.") from e
    if not isinstance(new_doc, dict):
        raise TemplateNotEditable("This change would break template.yaml, so it was not made.")
    if set(new_doc) - {"metrics"} != set(old_doc) - {"metrics"}:
        raise TemplateNotEditable("This change would alter other parts of template.yaml, so it was not made.")
    for key, value in old_doc.items():
        if key != "metrics" and new_doc.get(key) != value:
            raise TemplateNotEditable("This change would alter other parts of template.yaml, so it was not made.")
    if (new_doc.get("metrics") or []) != expected:
        raise TemplateNotEditable("The metrics: block uses a layout this form can't edit safely.")
