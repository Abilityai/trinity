"""Declaring a metric from the UI edits ONE `metrics:` item (trinity-enterprise#1006).

The form writes into the agent's `template.yaml`, which stays the single source
of truth. These tests pin the two properties that make that safe to do to a
hand-authored file:

* **Only the target entry changes.** Every other byte — other top-level keys,
  other metric entries, comments — is identical before and after. A rewrite of
  the whole document through a YAML dumper would drop every comment and reorder
  keys the author chose, which is the failure this editor exists to avoid.
* **The result is verified, or nothing is written.** The new text is parsed
  again and compared with the intended result; a layout the line editor cannot
  change safely (flow style, anchors) is refused as `template_not_editable`.

Validation runs the SAME parser the registry uses (`template_metrics`), so the
form and a hand edit cannot disagree about whether a declaration is valid.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytest.importorskip("yaml", reason="backend venv required")

from services import metric_declaration_editor as ed  # noqa: E402
from utils.safe_yaml import load_template_yaml  # noqa: E402


BASE = """\
# Sales companion — hand-written header comment
name: sales-companion
display_name: "Sales Companion"   # inline comment kept

metrics:
  # the headline number
  - name: close_rate
    type: percentage
    label: Close rate
    cadence: 1w
  - name: leads
    type: counter   # counted daily
    cadence: 1d

schedules:
  - name: nightly
    cron: "0 2 * * *"
"""

NEW = {"name": "proposals_sent", "type": "counter", "label": "Proposals sent", "cadence": "1w"}


def _metrics(text):
    return load_template_yaml(text).get("metrics")


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def test_add_appends_after_last_item_and_keeps_every_other_byte():
    out = ed.apply(BASE, "add", NEW["name"], NEW)
    head, tail = BASE.split("\nschedules:", 1)
    # The prefix up to the end of the last existing item is unchanged…
    assert out.startswith(head.rstrip("\n"))
    # …and so is everything from the next top-level key on.
    assert out.endswith("schedules:" + tail)
    assert "# Sales companion — hand-written header comment" in out
    assert '"Sales Companion"   # inline comment kept' in out
    assert "type: counter   # counted daily" in out
    names = [m["name"] for m in _metrics(out)]
    assert names == ["close_rate", "leads", "proposals_sent"]
    assert _metrics(out)[2] == NEW


def test_add_uses_the_blocks_own_indent():
    out = ed.apply(BASE, "add", NEW["name"], NEW)
    assert "\n  - name: proposals_sent\n    type: counter\n" in out


def test_add_to_indentless_block():
    text = "name: a\nmetrics:\n- name: x\n  type: gauge\nother: 1\n"
    out = ed.apply(text, "add", NEW["name"], NEW)
    assert out.startswith("name: a\nmetrics:\n- name: x\n  type: gauge\n- name: proposals_sent\n")
    assert out.endswith("other: 1\n")
    assert [m["name"] for m in _metrics(out)] == ["x", "proposals_sent"]


def test_add_when_there_is_no_metrics_block_appends_one():
    text = "name: a\n# trailing comment\n"
    out = ed.apply(text, "add", NEW["name"], NEW)
    assert out.startswith(text)
    assert _metrics(out) == [NEW]


def test_add_when_file_lacks_trailing_newline():
    out = ed.apply("name: a", "add", NEW["name"], NEW)
    assert out.startswith("name: a\n")
    assert _metrics(out) == [NEW]


@pytest.mark.parametrize("line", ["metrics: []", "metrics:", "metrics: ~", "metrics: null"])
def test_add_into_an_empty_block(line):
    text = f"name: a\n{line}\nother: 1\n"
    out = ed.apply(text, "add", NEW["name"], NEW)
    assert out.startswith("name: a\nmetrics:\n  - name: proposals_sent\n")
    assert out.endswith("other: 1\n")
    assert _metrics(out) == [NEW]


def test_add_keeps_a_column_zero_comment_that_precedes_the_next_key():
    text = "metrics:\n  - name: x\n    type: gauge\n\n# --- schedules ---\nschedules: []\n"
    out = ed.apply(text, "add", NEW["name"], NEW)
    assert out.endswith("\n\n# --- schedules ---\nschedules: []\n")
    assert [m["name"] for m in _metrics(out)] == ["x", "proposals_sent"]


def test_crlf_files_stay_crlf():
    text = BASE.replace("\n", "\r\n")
    out = ed.apply(text, "add", NEW["name"], NEW)
    assert "\n" not in out.replace("\r\n", "")
    assert [m["name"] for m in _metrics(out)][-1] == "proposals_sent"


# ---------------------------------------------------------------------------
# replace
# ---------------------------------------------------------------------------


def test_replace_changes_only_the_target_entry():
    edited = {"name": "leads", "type": "counter", "label": "Leads", "cadence": "1w"}
    out = ed.apply(BASE, "replace", "leads", edited)
    before, after = BASE.split("  - name: leads\n", 1)
    assert out.startswith(before)                      # close_rate + its comment untouched
    assert out.endswith("\nschedules:\n  - name: nightly\n    cron: \"0 2 * * *\"\n")
    assert _metrics(out)[1] == edited
    assert _metrics(out)[0] == _metrics(BASE)[0]


def test_replace_first_entry_keeps_its_leading_comment():
    edited = {"name": "close_rate", "type": "percentage", "label": "Close %", "cadence": "1d"}
    out = ed.apply(BASE, "replace", "close_rate", edited)
    assert "  # the headline number\n  - name: close_rate\n" in out
    assert _metrics(out)[0] == edited


def test_replace_unknown_metric_is_not_found():
    with pytest.raises(ed.MetricNotFound):
        ed.apply(BASE, "replace", "nope", {"name": "nope", "type": "gauge"})


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_drops_the_entry_and_its_leading_comment():
    out = ed.apply(BASE, "remove", "close_rate")
    assert "the headline number" not in out
    assert "close_rate" not in out
    assert "type: counter   # counted daily" in out
    assert [m["name"] for m in _metrics(out)] == ["leads"]
    assert load_template_yaml(out)["schedules"] == load_template_yaml(BASE)["schedules"]


def test_remove_last_remaining_entry_leaves_an_empty_list():
    text = "name: a\nmetrics:\n  - name: x\n    type: gauge\nother: 1\n"
    out = ed.apply(text, "remove", "x")
    assert out == "name: a\nmetrics: []\nother: 1\n"


def test_remove_unknown_metric_is_not_found():
    with pytest.raises(ed.MetricNotFound):
        ed.apply(BASE, "remove", "nope")


# ---------------------------------------------------------------------------
# layouts the line editor refuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "metrics: [{name: x, type: gauge}]\n",
    "metrics:\n  - {name: x, type: gauge}\n",
    "base: &m\n  name: x\n  type: gauge\nmetrics:\n  - *m\n",
    "metrics: &all\n  - name: x\n    type: gauge\n",
    "metrics:\n  - name: x\n    type: gauge\nmetrics:\n  - name: y\n    type: gauge\n",
    "metrics: !!seq\n  - name: x\n    type: gauge\n",
])
def test_unsafe_layouts_are_not_editable(text):
    with pytest.raises(ed.TemplateNotEditable):
        ed.apply(text, "add", NEW["name"], NEW)


def test_unparseable_template_is_not_editable():
    with pytest.raises(ed.TemplateNotEditable):
        ed.apply("name: [unclosed\n", "add", NEW["name"], NEW)


def test_read_declarations_reports_editability_and_hash():
    good = ed.read_declarations(BASE)
    assert good["editable"] is True and good["reason"] is None
    assert [e["name"] for e in good["entries"]] == ["close_rate", "leads"]
    assert good["template_hash"] == ed.template_hash(BASE)
    bad = ed.read_declarations("metrics: [{name: x, type: gauge}]\n")
    assert bad["editable"] is False and bad["reason"]
    assert [e["name"] for e in bad["entries"]] == ["x"]


# ---------------------------------------------------------------------------
# emitted YAML round-trips exactly
# ---------------------------------------------------------------------------


def test_awkward_strings_round_trip():
    entry = {
        "name": "odd", "type": "status", "label": "Rate: 50% #1 \"quoted\" 'single' — ünïcode",
        "description": "line one\nline two", "cadence": "PT30M",
        "values": [{"value": "yes", "color": "green", "label": "Yes"},
                   {"value": "no", "color": "red", "label": "No: really"}],
        "x-recorded-by": "the nightly-review skill",
    }
    out = ed.apply(BASE, "add", "odd", entry)
    assert _metrics(out)[-1] == entry


# ---------------------------------------------------------------------------
# build_entry: canonical shape from form fields
# ---------------------------------------------------------------------------


def test_build_entry_orders_keys_and_drops_empties():
    entry = ed.build_entry({
        "name": "close_rate", "type": "percentage", "label": "Close rate",
        "description": "", "unit": "%", "cadence": "1w", "direction": "up_good",
        "aggregation": "last", "values": None, "dimensions": [],
        "warning_threshold": 25, "critical_threshold": None, "recorded_by": "  skill  ",
    })
    assert list(entry) == ["name", "type", "label", "unit", "cadence", "direction",
                           "aggregation", "warning_threshold", "x-recorded-by"]
    assert entry["x-recorded-by"] == "skill"


def test_build_entry_keeps_existing_extension_keys_on_edit():
    existing = {"name": "a", "type": "gauge", "x-owner": "ops", "x-recorded-by": "old"}
    entry = ed.build_entry({"name": "a", "type": "gauge", "cadence": "1d", "recorded_by": ""},
                           existing=existing)
    assert entry["x-owner"] == "ops"
    assert "x-recorded-by" not in entry


# ---------------------------------------------------------------------------
# validate: the loader's named errors, keyed by field
# ---------------------------------------------------------------------------


def _fields(exc):
    return {e["field"] for e in exc.value.errors}


def test_validate_accepts_a_good_entry():
    ed.validate(_metrics(BASE), "add", NEW["name"], NEW)


def test_duplicate_name_is_refused():
    with pytest.raises(ed.DeclarationInvalid) as exc:
        ed.validate(_metrics(BASE), "add", "leads", {**NEW, "name": "leads"})
    assert _fields(exc) == {"name"}
    assert exc.value.errors[0]["code"] == "metric_duplicate"


def test_cap_is_refused_not_truncated():
    many = [{"name": f"m{i}", "type": "gauge"} for i in range(50)]
    with pytest.raises(ed.DeclarationInvalid) as exc:
        ed.validate(many, "add", NEW["name"], NEW)
    assert exc.value.errors[0]["code"] == "metric_cap_reached"


def test_cadence_is_required_on_a_form_write():
    with pytest.raises(ed.DeclarationInvalid) as exc:
        ed.validate([], "add", "x", {"name": "x", "type": "gauge"})
    assert _fields(exc) == {"cadence"}
    assert exc.value.errors[0]["code"] == "cadence_required"


@pytest.mark.parametrize("entry,field", [
    ({"name": "Bad-Name", "type": "gauge", "cadence": "1d"}, "name"),
    ({"name": "x", "type": "histogram", "cadence": "1d"}, "type"),
    ({"name": "x", "type": "gauge", "cadence": "30s"}, "cadence"),
    ({"name": "x", "type": "gauge", "cadence": "P1M"}, "cadence"),
    ({"name": "x", "type": "status", "cadence": "1d"}, "values"),
    ({"name": "x", "type": "gauge", "cadence": "1d", "dimensions": ["Bad"]}, "dimensions"),
    ({"name": "x", "type": "gauge", "cadence": "1d", "direction": "sideways"}, "direction"),
])
def test_loader_errors_come_back_keyed_by_field(entry, field):
    with pytest.raises(ed.DeclarationInvalid) as exc:
        ed.validate([], "add", entry["name"], entry)
    assert field in _fields(exc)
    assert all(e["message"] for e in exc.value.errors)


def test_preexisting_broken_entries_do_not_block_an_unrelated_add():
    broken = [{"name": "old", "type": "nope"}]
    ed.validate(broken, "add", NEW["name"], NEW)


def test_edit_cannot_change_name_or_type():
    with pytest.raises(ed.DeclarationInvalid) as exc:
        ed.validate(_metrics(BASE), "replace", "leads",
                    {"name": "leads", "type": "gauge", "cadence": "1d"})
    assert exc.value.errors[0]["code"] == "metric_type_immutable"
    with pytest.raises(ed.DeclarationInvalid) as exc:
        ed.validate(_metrics(BASE), "replace", "leads",
                    {"name": "leads2", "type": "counter", "cadence": "1d"})
    assert exc.value.errors[0]["code"] == "metric_name_immutable"
