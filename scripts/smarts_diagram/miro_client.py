"""Miro REST API client for board operations.

Handles creating, updating, and managing items on a Miro board.
"""

import os
import time
from typing import Any

import requests


class MiroClientError(Exception):
    """Exception raised for Miro API errors."""

    pass


class MiroClient:
    """Client for Miro REST API v2."""

    BASE_URL = "https://api.miro.com/v2"

    def __init__(self, token: str | None = None, board_id: str | None = None) -> None:
        """Initialize Miro client.

        Args:
            token: Miro access token. If not provided, reads from MIRO_ACCESS_TOKEN env var.
            board_id: Default board ID to operate on. If not provided, reads from MIRO_BOARD_ID env var.
        """
        self.token = token or os.getenv("MIRO_ACCESS_TOKEN")
        self.board_id = board_id or os.getenv("MIRO_BOARD_ID")

        if not self.token:
            raise MiroClientError(
                "Miro access token required. Set MIRO_ACCESS_TOKEN environment variable "
                "or pass token to constructor."
            )

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            }
        )

        # Rate limiting
        self._last_request_time = 0.0
        self._min_request_interval = 0.1  # 100ms between requests

    def _rate_limit(self) -> None:
        """Ensure we don't exceed rate limits."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)
        self._last_request_time = time.time()

    def _request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an API request.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            endpoint: API endpoint (relative to base URL)
            data: Request body data
            params: Query parameters

        Returns:
            Response JSON data

        Raises:
            MiroClientError: If the API request fails
        """
        self._rate_limit()

        url = f"{self.BASE_URL}{endpoint}"

        try:
            response = self.session.request(
                method=method,
                url=url,
                json=data,
                params=params,
            )

            if response.status_code == 429:
                # Rate limited - wait and retry
                retry_after = int(response.headers.get("Retry-After", 5))
                time.sleep(retry_after)
                return self._request(method, endpoint, data, params)

            if response.status_code >= 400:
                error_msg = f"Miro API error: {response.status_code}"
                try:
                    error_data = response.json()
                    if "message" in error_data:
                        error_msg = f"{error_msg} - {error_data['message']}"
                    if "context" in error_data:
                        error_msg = f"{error_msg} - {error_data['context']}"
                except Exception:
                    error_msg = f"{error_msg} - {response.text}"
                raise MiroClientError(error_msg)

            if response.status_code == 204:
                return {}

            return response.json()

        except requests.RequestException as e:
            raise MiroClientError(f"Request failed: {e}") from e

    def create_board(
        self,
        name: str,
        description: str = "",
        team_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new Miro board.

        Args:
            name: Board name
            description: Board description
            team_id: Team ID to create board in (optional)

        Returns:
            Created board data including 'id'
        """
        data: dict[str, Any] = {
            "name": name,
            "description": description,
        }

        if team_id:
            data["teamId"] = team_id

        return self._request("POST", "/boards", data)

    def get_board(self, board_id: str | None = None) -> dict[str, Any]:
        """Get board information.

        Args:
            board_id: Board ID (uses default if not provided)

        Returns:
            Board data
        """
        bid = board_id or self.board_id
        if not bid:
            raise MiroClientError("Board ID required")
        return self._request("GET", f"/boards/{bid}")

    def get_board_items(
        self,
        board_id: str | None = None,
        item_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get items from a board.

        Args:
            board_id: Board ID (uses default if not provided)
            item_type: Filter by item type (sticky_note, shape, etc.)
            limit: Maximum items to return

        Returns:
            List of board items
        """
        bid = board_id or self.board_id
        if not bid:
            raise MiroClientError("Board ID required")

        params: dict[str, Any] = {"limit": limit}
        if item_type:
            params["type"] = item_type

        result = self._request("GET", f"/boards/{bid}/items", params=params)
        return result.get("data", [])

    def delete_all_items(self, board_id: str | None = None) -> int:
        """Delete all items from a board.

        Args:
            board_id: Board ID (uses default if not provided)

        Returns:
            Number of items deleted
        """
        bid = board_id or self.board_id
        if not bid:
            raise MiroClientError("Board ID required")

        deleted = 0
        while True:
            items = self.get_board_items(bid, limit=50)
            if not items:
                break

            for item in items:
                self.delete_item(item["id"], bid)
                deleted += 1

        return deleted

    def delete_item(self, item_id: str, board_id: str | None = None) -> None:
        """Delete an item from a board.

        Args:
            item_id: Item ID to delete
            board_id: Board ID (uses default if not provided)
        """
        bid = board_id or self.board_id
        if not bid:
            raise MiroClientError("Board ID required")
        self._request("DELETE", f"/boards/{bid}/items/{item_id}")

    def create_sticky_note(
        self,
        content: str,
        x: float,
        y: float,
        width: float = 200,
        color: str = "light_yellow",
        board_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a sticky note.

        Args:
            content: Note content (supports HTML formatting)
            x: X position
            y: Y position
            width: Note width
            color: Fill color (light_yellow, light_green, light_blue, etc.)
            board_id: Board ID (uses default if not provided)

        Returns:
            Created item data
        """
        bid = board_id or self.board_id
        if not bid:
            raise MiroClientError("Board ID required")

        data = {
            "data": {"content": content, "shape": "rectangle"},
            "style": {"fillColor": color, "textAlign": "left", "textAlignVertical": "top"},
            "position": {"x": x, "y": y, "origin": "center"},
            "geometry": {"width": width},
        }

        return self._request("POST", f"/boards/{bid}/sticky_notes", data)

    def create_shape(
        self,
        content: str,
        x: float,
        y: float,
        width: float = 160,
        height: float = 60,
        shape: str = "rectangle",
        fill_color: str = "#FFFFFF",
        border_color: str = "#000000",
        board_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a shape.

        Args:
            content: Shape content
            x: X position
            y: Y position
            width: Shape width
            height: Shape height
            shape: Shape type (rectangle, round_rectangle, circle, etc.)
            fill_color: Fill color (hex)
            border_color: Border color (hex)
            board_id: Board ID (uses default if not provided)

        Returns:
            Created item data
        """
        bid = board_id or self.board_id
        if not bid:
            raise MiroClientError("Board ID required")

        data = {
            "data": {"content": content, "shape": shape},
            "style": {
                "fillColor": fill_color,
                "borderColor": border_color,
                "borderWidth": "2.0",
                "fontFamily": "open_sans",
                "fontSize": "14",
            },
            "position": {"x": x, "y": y, "origin": "center"},
            "geometry": {"width": width, "height": height},
        }

        return self._request("POST", f"/boards/{bid}/shapes", data)

    def create_frame(
        self,
        title: str,
        x: float,
        y: float,
        width: float,
        height: float,
        fill_color: str = "#FFFFFF",
        board_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a frame.

        Args:
            title: Frame title
            x: X position (center)
            y: Y position (center)
            width: Frame width
            height: Frame height
            fill_color: Background color (hex)
            board_id: Board ID (uses default if not provided)

        Returns:
            Created item data
        """
        bid = board_id or self.board_id
        if not bid:
            raise MiroClientError("Board ID required")

        data = {
            "data": {"title": title, "type": "freeform"},
            "style": {"fillColor": fill_color},
            "position": {"x": x, "y": y, "origin": "center"},
            "geometry": {"width": width, "height": height},
        }

        return self._request("POST", f"/boards/{bid}/frames", data)

    def create_connector(
        self,
        start_item_id: str,
        end_item_id: str,
        label: str = "",
        color: str = "#000000",
        board_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a connector between two items.

        Args:
            start_item_id: Start item ID
            end_item_id: End item ID
            label: Connector label
            color: Line color (hex)
            board_id: Board ID (uses default if not provided)

        Returns:
            Created connector data
        """
        bid = board_id or self.board_id
        if not bid:
            raise MiroClientError("Board ID required")

        data: dict[str, Any] = {
            "startItem": {"id": start_item_id},
            "endItem": {"id": end_item_id},
            "style": {
                "strokeColor": color,
                "strokeWidth": "2.0",
                "startStrokeCap": "none",
                "endStrokeCap": "stealth",
            },
        }

        if label:
            data["captions"] = [{"content": label, "position": "50%"}]

        return self._request("POST", f"/boards/{bid}/connectors", data)

    def create_text(
        self,
        content: str,
        x: float,
        y: float,
        width: float = 200,
        font_size: int = 14,
        board_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a text item.

        Args:
            content: Text content
            x: X position
            y: Y position
            width: Text box width
            font_size: Font size
            board_id: Board ID (uses default if not provided)

        Returns:
            Created item data
        """
        bid = board_id or self.board_id
        if not bid:
            raise MiroClientError("Board ID required")

        data = {
            "data": {"content": content},
            "style": {"fontSize": str(font_size), "fontFamily": "open_sans"},
            "position": {"x": x, "y": y, "origin": "center"},
            "geometry": {"width": width},
        }

        return self._request("POST", f"/boards/{bid}/texts", data)

    def update_board(
        self,
        diagram_data: dict[str, Any],
        board_id: str | None = None,
        clear_first: bool = True,
    ) -> dict[str, Any]:
        """Update a board with diagram data.

        Args:
            diagram_data: Diagram data from generate_miro_diagram()
            board_id: Board ID (uses default if not provided)
            clear_first: Whether to delete existing items first

        Returns:
            Summary of created items
        """
        bid = board_id or self.board_id
        if not bid:
            raise MiroClientError("Board ID required")

        # Clear existing items if requested
        if clear_first:
            deleted = self.delete_all_items(bid)
            print(f"Deleted {deleted} existing items")

        created_items: list[dict[str, Any]] = []
        item_id_map: dict[int, str] = {}  # Maps item index to created item ID

        # Create all items
        for idx, item in enumerate(diagram_data["items"]):
            item_type = item["type"]
            pos = item.get("position", {})
            geom = item.get("geometry", {})
            style = item.get("style", {})
            data = item.get("data", {})

            created: dict[str, Any] | None = None

            try:
                if item_type == "sticky_note":
                    created = self.create_sticky_note(
                        content=data.get("content", ""),
                        x=pos.get("x", 0),
                        y=pos.get("y", 0),
                        width=geom.get("width", 200),
                        color=style.get("fillColor", "light_yellow"),
                        board_id=bid,
                    )
                elif item_type == "shape":
                    created = self.create_shape(
                        content=data.get("content", ""),
                        x=pos.get("x", 0),
                        y=pos.get("y", 0),
                        width=geom.get("width", 160),
                        height=geom.get("height", 60),
                        shape=data.get("shape", "rectangle"),
                        fill_color=style.get("fillColor", "#FFFFFF"),
                        border_color=style.get("borderColor", "#000000"),
                        board_id=bid,
                    )
                elif item_type == "frame":
                    created = self.create_frame(
                        title=data.get("title", ""),
                        x=pos.get("x", 0),
                        y=pos.get("y", 0),
                        width=geom.get("width", 500),
                        height=geom.get("height", 200),
                        fill_color=style.get("fillColor", "#FFFFFF"),
                        board_id=bid,
                    )
                elif item_type == "text":
                    created = self.create_text(
                        content=data.get("content", ""),
                        x=pos.get("x", 0),
                        y=pos.get("y", 0),
                        width=geom.get("width", 200),
                        font_size=int(style.get("fontSize", 14)),
                        board_id=bid,
                    )

                if created:
                    created_items.append(created)
                    item_id_map[idx] = created.get("id", "")
            except MiroClientError as e:
                print(f"  Warning: Failed to create {item_type}: {e}")

        # Create connectors
        connectors_created = 0
        for conn in diagram_data.get("connectors", []):
            source_idx = conn.get("source_index")
            target_idx = conn.get("target_index")

            if source_idx in item_id_map and target_idx in item_id_map:
                try:
                    self.create_connector(
                        start_item_id=item_id_map[source_idx],
                        end_item_id=item_id_map[target_idx],
                        label=conn.get("label", ""),
                        color=conn.get("color", "#000000"),
                        board_id=bid,
                    )
                    connectors_created += 1
                except MiroClientError as e:
                    print(f"Warning: Failed to create connector: {e}")

        return {
            "items_created": len(created_items),
            "connectors_created": connectors_created,
            "board_id": bid,
            "board_url": f"https://miro.com/app/board/{bid}/",
        }


if __name__ == "__main__":
    # Test client connectivity
    try:
        client = MiroClient()
        board = client.get_board()
        print(f"Connected to board: {board.get('name', 'Unknown')}")
        print(f"Board ID: {client.board_id}")

        items = client.get_board_items(limit=10)
        print(f"Current items on board: {len(items)}")

    except MiroClientError as e:
        print(f"Error: {e}")
