"""
Agent Dashboard endpoint.

Reads and serves dashboard configuration from dashboard.yaml file.
"""
import errno
import logging
import os
import traceback
from pathlib import Path
from typing import Optional, Dict, Any, List

from fastapi import APIRouter

from ..safe_yaml import AliasPolicy, HardenedYamlError, load_hardened_yaml
from ..utils.credential_sanitizer import sanitize_text
from ..utils.helpers import iso_z_from_mtime

logger = logging.getLogger(__name__)
router = APIRouter()


def get_dashboard_path() -> Path:
    """Get the fixed path to dashboard.yaml."""
    return Path("/home/developer/dashboard.yaml")


class _DashboardIsALink(Exception):
    """`dashboard.yaml` is a link (live or dangling), or a file with more than
    one name on disk (a hard link)."""


def _read_dashboard(path: Path) -> Optional[tuple]:
    """(text, stat) of the file at `path`, or None when nothing is there.

    Opened with O_NOFOLLOW: a link at that name is never followed, so what is
    parsed (and quoted back in a parse error) is always the file at the fixed
    path, never another file a link points at. A link, live or dangling, raises
    `_DashboardIsALink`. O_NOFOLLOW cannot see a hard link, so a file with more
    than one name is refused the same way: the agent can be asked in chat to
    give another file this name, and Files refuses a hard-linked file below the
    owner tier for the same reason (ent#820). O_NONBLOCK keeps a FIFO from
    hanging the open.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise _DashboardIsALink() from None
        raise
    try:
        f = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)  # fdopen does not close a descriptor it refuses (a directory)
        raise
    with f:
        st = os.fstat(f.fileno())
        if st.st_nlink > 1:
            raise _DashboardIsALink()
        data = f.read()
    return data.decode("utf-8"), st


def validate_widget(widget: Dict[str, Any], index: int) -> Optional[str]:
    """Validate a single widget. Returns error message if invalid."""
    valid_types = [
        'metric', 'status', 'progress', 'text', 'markdown',
        'table', 'list', 'link', 'image', 'divider', 'spacer'
    ]

    widget_type = widget.get('type')
    if not widget_type:
        return f"Widget {index}: missing 'type' field"

    if widget_type not in valid_types:
        return f"Widget {index}: invalid type '{widget_type}'. Valid types: {', '.join(valid_types)}"

    # ent#479: a widget carrying `metric: <name>` is BOUND — the backend fills
    # its `value` (and a status widget's `color`) from the declared metric
    # registry on every read, so requiring the author to write a number here
    # would require them to write a FAKE number forever. `label` stays
    # required: it is the widget's own copy, not a measurement.
    #
    # An agent on an older base image still has the strict rule, so the guide
    # tells authors to keep a placeholder `value:` until their image carries
    # this change — the backend overwrites it when the binding resolves, so
    # the placeholder is never what an operator sees. Compatibility check D-003
    # (`services/compatibility/static_checks.py::_BOUND_FILLED`) applies the same
    # exemption — keep the two in step (#3186).
    bound = bool(widget.get('metric'))

    # Type-specific validation
    if widget_type == 'metric':
        if 'label' not in widget:
            return f"Widget {index} (metric): missing required 'label' field"
        if 'value' not in widget and not bound:
            return f"Widget {index} (metric): missing required 'value' field"

    elif widget_type == 'status':
        if 'label' not in widget:
            return f"Widget {index} (status): missing required 'label' field"
        if 'value' not in widget and not bound:
            return f"Widget {index} (status): missing required 'value' field"
        if 'color' not in widget and not bound:
            return f"Widget {index} (status): missing required 'color' field"

    elif widget_type == 'progress':
        if 'label' not in widget:
            return f"Widget {index} (progress): missing required 'label' field"
        if 'value' not in widget and not bound:
            return f"Widget {index} (progress): missing required 'value' field"

    elif widget_type == 'text':
        if 'content' not in widget:
            return f"Widget {index} (text): missing required 'content' field"

    elif widget_type == 'markdown':
        if 'content' not in widget:
            return f"Widget {index} (markdown): missing required 'content' field"

    elif widget_type == 'table':
        if 'columns' not in widget:
            return f"Widget {index} (table): missing required 'columns' field"
        if 'rows' not in widget:
            return f"Widget {index} (table): missing required 'rows' field"

    elif widget_type == 'list':
        if 'items' not in widget:
            return f"Widget {index} (list): missing required 'items' field"

    elif widget_type == 'link':
        if 'label' not in widget:
            return f"Widget {index} (link): missing required 'label' field"
        if 'url' not in widget:
            return f"Widget {index} (link): missing required 'url' field"

    elif widget_type == 'image':
        if 'src' not in widget:
            return f"Widget {index} (image): missing required 'src' field"
        if 'alt' not in widget:
            return f"Widget {index} (image): missing required 'alt' field"

    return None


def validate_section(section: Dict[str, Any], index: int) -> List[str]:
    """Validate a section. Returns list of error messages."""
    errors = []

    if 'widgets' not in section:
        errors.append(f"Section {index}: missing required 'widgets' field")
        return errors

    if not isinstance(section['widgets'], list):
        errors.append(f"Section {index}: 'widgets' must be a list")
        return errors

    # Validate layout
    layout = section.get('layout', 'grid')
    if layout not in ['grid', 'list']:
        errors.append(f"Section {index}: invalid layout '{layout}'. Valid values: grid, list")

    # Validate columns
    columns = section.get('columns', 3)
    if not isinstance(columns, int) or columns < 1 or columns > 4:
        errors.append(f"Section {index}: columns must be 1-4, got {columns}")

    # Validate widgets
    for widget_index, widget in enumerate(section['widgets']):
        error = validate_widget(widget, widget_index)
        if error:
            errors.append(f"Section {index}, {error}")

    return errors


def _shape_error(config: Any) -> Optional[str]:
    """The first place `config` is not the nested mappings the validator walks,
    as a sentence its author can act on; None when the shape is walkable.

    Without it a list where a mapping belongs surfaced as the raw
    AttributeError/TypeError text (`'str' object has no attribute 'get'`).
    """
    if not isinstance(config, dict):
        return "dashboard.yaml must be a mapping (key: value pairs) at the top level"
    sections = config.get('sections')
    if not isinstance(sections, list):
        return None  # validate_dashboard names a missing or non-list `sections`
    for section_index, section in enumerate(sections):
        if not isinstance(section, dict):
            return f"dashboard.yaml: section {section_index} must be a mapping (key: value pairs)"
        widgets = section.get('widgets')
        if not isinstance(widgets, list):
            continue  # validate_dashboard names it
        for widget_index, widget in enumerate(widgets):
            if not isinstance(widget, dict):
                return (
                    f"dashboard.yaml: section {section_index}, widget {widget_index} "
                    "must be a mapping (key: value pairs)"
                )
    return None


def _no_dashboard(error: str) -> Dict[str, Any]:
    return {
        "has_dashboard": False,
        "config": None,
        "last_modified": None,
        "error": error,
    }


def validate_dashboard(config: Dict[str, Any]) -> List[str]:
    """Validate dashboard configuration. Returns list of error messages.

    Top-level structural errors (missing title/sections) are fatal.
    Widget-level errors are warnings — bad widgets are stripped so the
    rest of the dashboard still renders.
    """
    errors = []

    if 'title' not in config:
        errors.append("Missing required 'title' field")

    if 'sections' not in config:
        errors.append("Missing required 'sections' field")
        return errors

    if not isinstance(config['sections'], list):
        errors.append("'sections' must be a list")
        return errors

    # Validate refresh interval
    refresh = config.get('refresh', 30)
    if not isinstance(refresh, (int, float)) or refresh < 5:
        errors.append(f"'refresh' must be a number >= 5, got {refresh}")

    # Validate each section — strip invalid widgets instead of rejecting
    for section_index, section in enumerate(config['sections']):
        if 'widgets' not in section:
            errors.append(f"Section {section_index}: missing required 'widgets' field")
            continue
        if not isinstance(section['widgets'], list):
            errors.append(f"Section {section_index}: 'widgets' must be a list")
            section['widgets'] = []
            continue

        layout = section.get('layout', 'grid')
        if layout not in ['grid', 'list']:
            errors.append(f"Section {section_index}: invalid layout '{layout}'. Valid values: grid, list")

        columns = section.get('columns', 3)
        if not isinstance(columns, int) or columns < 1 or columns > 4:
            errors.append(f"Section {section_index}: columns must be 1-4, got {columns}")

        # Validate widgets and strip bad ones
        valid_widgets = []
        for widget_index, widget in enumerate(section['widgets']):
            error = validate_widget(widget, widget_index)
            if error:
                errors.append(f"Section {section_index}, {error} (widget stripped)")
            else:
                valid_widgets.append(widget)
        section['widgets'] = valid_widgets

    # Remove sections that ended up with zero widgets
    config['sections'] = [s for s in config['sections'] if s.get('widgets')]

    return errors


@router.get("/api/dashboard")
async def get_dashboard():
    """
    Get agent dashboard configuration.

    Returns:
    - has_dashboard: Whether agent has a dashboard.yaml file
    - config: Parsed dashboard configuration (title, sections, widgets)
    - last_modified: File modification timestamp
    - error: Error message if parsing failed
    """
    dashboard_path = get_dashboard_path()

    try:
        # Read and parse YAML
        read = _read_dashboard(dashboard_path)
        if read is None:
            return {
                "has_dashboard": False,
                "config": None,
                "last_modified": None,
                "error": "No dashboard.yaml found at /home/developer/dashboard.yaml"
            }
        content, stat = read
        # #1965: REJECT, matching the backend policy for agent-authored
        # workspace YAML (`compatibility/static_checks._parse_yaml`). The
        # backend proxies this endpoint and every consumer walks the parsed
        # structure, so an anchor bomb here amplifies twice.
        config = load_hardened_yaml(
            content, kind="workspace_yaml", alias_policy=AliasPolicy.REJECT
        )

        if config is None:
            return {
                "has_dashboard": False,
                "config": None,
                "last_modified": None,
                "error": "dashboard.yaml is empty"
            }

        shape = _shape_error(config)
        if shape:
            return _no_dashboard(shape)

        # Validate configuration — strips bad widgets, keeps valid ones
        errors = validate_dashboard(config)

        # Fatal: no title or sections structure is completely broken
        has_sections = isinstance(config.get('sections'), list) and len(config.get('sections', [])) > 0
        if 'title' not in config or not has_sections:
            return {
                "has_dashboard": False,
                "config": None,
                "last_modified": None,
                "error": f"Validation errors: {'; '.join(errors)}"
            }

        # The modification time of the file that was read
        last_modified = iso_z_from_mtime(stat.st_mtime)

        # Ensure defaults
        if 'refresh' not in config:
            config['refresh'] = 30

        return {
            "has_dashboard": True,
            "config": config,
            "last_modified": last_modified,
            "error": None,
            "warnings": errors if errors else None
        }

    except _DashboardIsALink:
        return _no_dashboard("Failed to read dashboard.yaml: it is a link, and links are not followed")
    except HardenedYamlError as e:
        # #1965: the only parse-failure arm. `load_hardened_yaml` converts every
        # yaml.YAMLError into a HardenedYamlError (a ValueError, not a
        # YAMLError), so a refused bomb and a plain syntax error both land here
        # with their named code. Without this arm they would fall through to
        # the generic `except Exception` below and surface as an unnamed
        # "Failed to read dashboard.yaml", which is the timeout-shaped answer
        # the AC rules out. `static_checks._parse_yaml` records the same trap on
        # the backend side.
        return {
            "has_dashboard": False,
            "config": None,
            "last_modified": None,
            "error": f"{e.code}: {e}",
        }
    except UnicodeDecodeError:
        # `read_text` on bytes that are not UTF-8 (a ValueError, so ahead of the
        # generic arm).
        logger.warning("dashboard.yaml is not valid UTF-8 text")
        return _no_dashboard("Failed to read dashboard.yaml: the file is not valid UTF-8 text")
    except OSError as e:
        logger.error(
            "Failed to read dashboard.yaml (%s): %s",
            type(e).__name__, sanitize_text(traceback.format_exc()),
        )
        return _no_dashboard(
            f"Failed to read dashboard.yaml: the file could not be read ({type(e).__name__})"
        )
    except Exception as e:
        # Not ours to return: the text of an exception we did not foresee goes
        # to the log (scrubbed) and the answer names only its class.
        logger.error(
            "Failed to read dashboard.yaml (%s): %s",
            type(e).__name__, sanitize_text(traceback.format_exc()),
        )
        return _no_dashboard(f"Failed to read dashboard.yaml: {type(e).__name__}")
