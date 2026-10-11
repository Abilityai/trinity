"""Server refusals the CLI must name rather than swallow.

No third-party imports: the unit job imports this without the CLI's own
dependencies (`click`, `rich`).
"""

from typing import Any, Optional


def is_workspace_only_refusal(status_code: int, body: Optional[Any]) -> bool:
    """The operator floor's refusal (trinity-enterprise#837): a Workspace-only
    account answered with `403 {"detail": {"code": "workspace_only"}}`."""
    if status_code != 403 or not isinstance(body, dict):
        return False
    detail = body.get("detail")
    return isinstance(detail, dict) and detail.get("code") == "workspace_only"
