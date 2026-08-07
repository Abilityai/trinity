"""
Agent Dashboard endpoint.

Reads and serves dashboard configuration from dashboard.yaml file.
"""
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

from fastapi import APIRouter
import yaml

from ..safe_yaml import AliasPolicy, HardenedYamlError, load_hardened_yaml
from ..utils.helpers import iso_z_from_mtime

logger = logging.getLogger(__name__)
router = APIRouter()


def get_dashboard_path() -> Path:
    """Get the fixed path to dashboard.yaml."""
    return Path("/home/developer/dashboard.yaml")


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

    # Type-specific validation
    if widget_type == 'metric':
        if 'label' not in widget:
            return f"Widget {index} (metric): missing required 'label' field"
        if 'value' not in widget:
            return f"Widget {index} (metric): missing required 'value' field"

    elif widget_type == 'status':
        if 'label' not in widget:
            return f"Widget {index} (status): missing required 'label' field"
        if 'value' not in widget:
            return f"Widget {index} (status): missing required 'value' field"
        if 'color' not in widget:
            return f"Widget {index} (status): missing required 'color' field"

    elif widget_type == 'progress':
        if 'label' not in widget:
            return f"Widget {index} (progress): missing required 'label' field"
        if 'value' not in widget:
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

    if not dashboard_path.exists():
        return {
            "has_dashboard": False,
            "config": None,
            "last_modified": None,
            "error": "No dashboard.yaml found at /home/developer/dashboard.yaml"
        }

    try:
        # Read and parse YAML
        content = dashboard_path.read_text()
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

        # Get file modification time
        stat = dashboard_path.stat()
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

    except HardenedYamlError as e:
        # #1965: listed BEFORE the YAMLError arm and separately from it, because
        # HardenedYamlError is a ValueError — not a YAMLError. Without its own
        # arm a refused bomb would fall through to the generic `except
        # Exception` below and surface as an unnamed "Failed to read
        # dashboard.yaml", which is the timeout-shaped answer the AC rules out.
        # `static_checks._parse_yaml` records the same trap on the backend side.
        return {
            "has_dashboard": False,
            "config": None,
            "last_modified": None,
            "error": f"{e.code}: {e}",
        }
    except yaml.YAMLError as e:
        # Get line number from YAML error if available
        error_msg = str(e)
        if hasattr(e, 'problem_mark') and e.problem_mark:
            mark = e.problem_mark
            error_msg = f"YAML parse error at line {mark.line + 1}, column {mark.column + 1}: {e.problem}"

        return {
            "has_dashboard": False,
            "config": None,
            "last_modified": None,
            "error": error_msg
        }
    except Exception as e:
        logger.error(f"Failed to read dashboard.yaml: {e}")
        return {
            "has_dashboard": False,
            "config": None,
            "last_modified": None,
            "error": f"Failed to read dashboard.yaml: {str(e)}"
        }
