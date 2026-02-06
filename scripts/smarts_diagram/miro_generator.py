"""Miro diagram generator for SMARTS pipeline.

Converts parsed agent specs into Miro board items (shapes, connectors, frames).
"""

from dataclasses import dataclass, field
from typing import Any

from scripts.smarts_diagram.parser import AgentSpec


@dataclass
class MiroItem:
    """Base class for Miro board items."""

    item_type: str  # sticky_note, shape, connector, frame, text
    data: dict[str, Any] = field(default_factory=dict)
    style: dict[str, Any] = field(default_factory=dict)
    position: dict[str, float] = field(default_factory=dict)
    geometry: dict[str, float] = field(default_factory=dict)


# =============================================================================
# Layout Configuration - Clean horizontal flow design
# =============================================================================

# Canvas dimensions
CANVAS_WIDTH = 3000
CANVAS_HEIGHT = 2000

# Spacing
HORIZONTAL_SPACING = 350  # Between agents in same row
VERTICAL_SPACING = 300  # Between rows
CARD_WIDTH = 280
CARD_HEIGHT = 180

# Row Y positions (from top)
ROW_TITLE = 50
ROW_DATA_SOURCES = 200
ROW_MARKET_CONTEXT = 500
ROW_PIPELINE = 900
ROW_OVERSIGHT_FEEDBACK = 1300
ROW_DATABASE = 1650

# Starting X position for centering
START_X = 200

# Agent positions - explicit placement for clean layout
AGENT_POSITIONS = {
    # Market Context row (2 agents, centered)
    "market-regime": (START_X + 400, ROW_MARKET_CONTEXT),
    "news-sentiment": (START_X + 400 + HORIZONTAL_SPACING + 100, ROW_MARKET_CONTEXT),
    # Pipeline row (4 agents, left to right flow)
    "discovery": (START_X, ROW_PIPELINE),
    "analysis": (START_X + HORIZONTAL_SPACING, ROW_PIPELINE),
    "decision": (START_X + HORIZONTAL_SPACING * 2, ROW_PIPELINE),
    "execution": (START_X + HORIZONTAL_SPACING * 3, ROW_PIPELINE),
    # Oversight and Feedback row
    "portfolio-manager": (START_X + HORIZONTAL_SPACING * 0.5, ROW_OVERSIGHT_FEEDBACK),
    "feedback": (START_X + HORIZONTAL_SPACING * 2.5, ROW_OVERSIGHT_FEEDBACK),
}

# Agent colors by layer (Miro sticky note color names)
LAYER_COLORS = {
    "market_context": "cyan",
    "pipeline": "light_green",
    "oversight": "orange",
    "feedback": "violet",
}

# Data source colors (hex for shapes)
DATA_SOURCE_COLORS = {
    "alpaca": ("#2196F3", "#1976D2"),  # Blue
    "polygon": ("#4CAF50", "#388E3C"),  # Green
    "vix": ("#FF9800", "#F57C00"),  # Orange
}

# Database color
DB_COLOR = ("#FFF9C4", "#F9A825")  # Light yellow


def create_sticky_note(
    content: str,
    x: float,
    y: float,
    width: float = CARD_WIDTH,
    color: str = "light_yellow",
) -> MiroItem:
    """Create a Miro sticky note."""
    return MiroItem(
        item_type="sticky_note",
        data={"content": content, "shape": "rectangle"},
        style={"fillColor": color, "textAlign": "left", "textAlignVertical": "top"},
        position={"x": x, "y": y},
        geometry={"width": width},
    )


def create_shape(
    content: str,
    x: float,
    y: float,
    width: float = 200,
    height: float = 80,
    shape: str = "round_rectangle",
    fill_color: str = "#FFFFFF",
    border_color: str = "#000000",
    font_size: int = 14,
) -> MiroItem:
    """Create a Miro shape."""
    return MiroItem(
        item_type="shape",
        data={"content": content, "shape": shape},
        style={
            "fillColor": fill_color,
            "borderColor": border_color,
            "borderWidth": "2.0",
            "fontFamily": "open_sans",
            "fontSize": str(font_size),
            "textAlign": "center",
            "textAlignVertical": "middle",
        },
        position={"x": x, "y": y},
        geometry={"width": width, "height": height},
    )


def create_text(
    content: str,
    x: float,
    y: float,
    width: float = 400,
    font_size: int = 24,
) -> MiroItem:
    """Create a Miro text item."""
    return MiroItem(
        item_type="text",
        data={"content": content},
        style={
            "fontSize": str(font_size),
            "fontFamily": "open_sans",
            "textAlign": "center",
        },
        position={"x": x, "y": y},
        geometry={"width": width},
    )


def format_agent_content(agent: AgentSpec) -> str:
    """Format agent details for sticky note content."""
    lines = [f"<b>{agent.name.upper()}</b>"]

    if agent.role:
        # Truncate role if too long
        role = agent.role[:80] + "..." if len(agent.role) > 80 else agent.role
        lines.append(f"<i>{role}</i>")

    lines.append("")

    # Add schedule
    if agent.schedules:
        cron = agent.schedules[0].cron
        lines.append(f"<b>Cron:</b> {cron}")

    # Add writes (output)
    if agent.writes_to:
        ctx_types = [c.context_type for c in agent.writes_to if c.context_type]
        if ctx_types:
            lines.append(f"<b>Output:</b> {ctx_types[0]}")

    return "\n".join(lines)


def generate_title() -> list[MiroItem]:
    """Generate title and section labels."""
    items = []

    # Main title
    items.append(
        create_text(
            "<b>SMARTS Trading Pipeline Architecture</b>",
            START_X + HORIZONTAL_SPACING * 1.5,
            ROW_TITLE,
            width=600,
            font_size=32,
        )
    )

    # Section labels
    items.append(
        create_text(
            "<b>DATA SOURCES</b>",
            START_X - 100,
            ROW_DATA_SOURCES,
            width=200,
            font_size=16,
        )
    )

    items.append(
        create_text(
            "<b>MARKET CONTEXT</b>",
            START_X - 100,
            ROW_MARKET_CONTEXT,
            width=200,
            font_size=16,
        )
    )

    items.append(
        create_text(
            "<b>TRADING PIPELINE</b>",
            START_X - 100,
            ROW_PIPELINE,
            width=200,
            font_size=16,
        )
    )

    items.append(
        create_text(
            "<b>OVERSIGHT & FEEDBACK</b>",
            START_X - 100,
            ROW_OVERSIGHT_FEEDBACK,
            width=200,
            font_size=16,
        )
    )

    items.append(
        create_text(
            "<b>PERSISTENCE</b>",
            START_X - 100,
            ROW_DATABASE,
            width=200,
            font_size=16,
        )
    )

    return items


def generate_data_sources() -> list[MiroItem]:
    """Generate data source nodes."""
    items = []
    y = ROW_DATA_SOURCES

    # Alpaca Markets
    fill, border = DATA_SOURCE_COLORS["alpaca"]
    items.append(
        create_shape(
            "<b>Alpaca Markets</b>\nOrders & Portfolio",
            START_X + 200,
            y,
            width=200,
            height=70,
            fill_color=fill,
            border_color=border,
        )
    )

    # Polygon.io
    fill, border = DATA_SOURCE_COLORS["polygon"]
    items.append(
        create_shape(
            "<b>Polygon.io</b>\nNews API",
            START_X + 500,
            y,
            width=200,
            height=70,
            fill_color=fill,
            border_color=border,
        )
    )

    # VIX Data
    fill, border = DATA_SOURCE_COLORS["vix"]
    items.append(
        create_shape(
            "<b>Market Data</b>\nVIX Level",
            START_X + 800,
            y,
            width=200,
            height=70,
            fill_color=fill,
            border_color=border,
        )
    )

    return items


def generate_database_nodes() -> list[MiroItem]:
    """Generate database table nodes."""
    items = []
    y = ROW_DATABASE
    fill, border = DB_COLOR

    tables = [
        ("integration_context\n(Central Hub)", START_X + 100),
        ("trading_evaluations", START_X + 400),
        ("pm_directives", START_X + 700),
        ("feedback_metrics", START_X + 1000),
    ]

    for table_name, x in tables:
        items.append(
            create_shape(
                table_name,
                x,
                y,
                width=180,
                height=60,
                shape="rectangle",
                fill_color=fill,
                border_color=border,
                font_size=12,
            )
        )

    return items


def generate_agent_nodes(agents: list[AgentSpec]) -> list[tuple[MiroItem, str]]:
    """Generate sticky note nodes for each agent."""
    nodes = []

    for agent in agents:
        if agent.name not in AGENT_POSITIONS:
            continue

        x, y = AGENT_POSITIONS[agent.name]
        color = LAYER_COLORS.get(agent.layer, "light_yellow")
        content = format_agent_content(agent)

        node = create_sticky_note(content, x, y, color=color)
        nodes.append((node, agent.name))

    return nodes


def generate_flow_arrows() -> list[dict[str, Any]]:
    """Generate the main flow arrow data.

    Returns connector metadata (will be resolved to IDs after item creation).
    """
    # Define the primary data flow connections
    flows = [
        # Market context flows down to discovery
        ("market-regime", "discovery", "market_regime", "#2196F3"),
        ("news-sentiment", "discovery", "news_sentiment", "#2196F3"),
        # Market context also flows to analysis
        ("market-regime", "analysis", "market_regime", "#2196F3"),
        ("news-sentiment", "analysis", "news_sentiment", "#2196F3"),
        # Main pipeline flow (left to right)
        ("discovery", "analysis", "scanner_opportunity", "#4CAF50"),
        ("analysis", "decision", "analysis", "#4CAF50"),
        ("decision", "execution", "decision", "#FF9800"),
        # PM directive flows
        ("portfolio-manager", "decision", "pm_directive", "#F44336"),
        ("portfolio-manager", "execution", "pm_directive", "#F44336"),
        # Feedback flows
        ("execution", "feedback", "execution", "#9C27B0"),
        ("feedback", "portfolio-manager", "feedback_metrics", "#9C27B0"),
        # Decision writes to trading_evaluations which PM reads
        ("decision", "portfolio-manager", "trading_evaluations", "#FF9800"),
    ]

    return [
        {"source": src, "target": tgt, "label": label, "color": color}
        for src, tgt, label, color in flows
    ]


def generate_miro_diagram(agents: list[AgentSpec]) -> dict[str, Any]:
    """Generate complete Miro diagram from agent specs.

    Args:
        agents: List of parsed AgentSpec objects

    Returns:
        Dictionary containing all Miro items to create
    """
    items: list[dict[str, Any]] = []
    agent_item_map: dict[str, int] = {}  # Maps agent name to item index

    # Generate title and labels
    titles = generate_title()
    for item in titles:
        items.append(
            {
                "type": item.item_type,
                "data": item.data,
                "style": item.style,
                "position": item.position,
                "geometry": item.geometry,
            }
        )

    # Generate data sources
    data_sources = generate_data_sources()
    for node in data_sources:
        items.append(
            {
                "type": node.item_type,
                "data": node.data,
                "style": node.style,
                "position": node.position,
                "geometry": node.geometry,
            }
        )

    # Generate agent nodes
    agent_nodes = generate_agent_nodes(agents)
    for node, agent_name in agent_nodes:
        agent_item_map[agent_name] = len(items)
        items.append(
            {
                "type": node.item_type,
                "data": node.data,
                "style": node.style,
                "position": node.position,
                "geometry": node.geometry,
            }
        )

    # Generate database nodes
    db_nodes = generate_database_nodes()
    for node in db_nodes:
        items.append(
            {
                "type": node.item_type,
                "data": node.data,
                "style": node.style,
                "position": node.position,
                "geometry": node.geometry,
            }
        )

    # Generate flow arrows
    flows = generate_flow_arrows()
    connector_data = []
    for flow in flows:
        src, tgt = flow["source"], flow["target"]
        if src in agent_item_map and tgt in agent_item_map:
            connector_data.append(
                {
                    "source_index": agent_item_map[src],
                    "target_index": agent_item_map[tgt],
                    "label": flow["label"],
                    "color": flow["color"],
                }
            )

    return {
        "items": items,
        "connectors": connector_data,
        "metadata": {
            "title": "SMARTS Trading Pipeline Architecture",
            "description": "Auto-generated diagram showing the SMARTS agent pipeline",
            "agent_count": len(agents),
        },
    }


if __name__ == "__main__":
    # Test generation
    from pathlib import Path

    from scripts.smarts_diagram.parser import parse_agent_templates

    templates_dir = Path(__file__).parent.parent.parent / "config" / "agent-templates"
    agents = parse_agent_templates(templates_dir)

    diagram = generate_miro_diagram(agents)

    print("Generated Miro Diagram:")
    print(f"  Items: {len(diagram['items'])}")
    print(f"  Connectors: {len(diagram['connectors'])}")
    print(f"  Metadata: {diagram['metadata']}")

    # Print items summary
    print("\nItems by type:")
    type_counts: dict[str, int] = {}
    for item in diagram["items"]:
        item_type = item["type"]
        type_counts[item_type] = type_counts.get(item_type, 0) + 1
    for item_type, count in type_counts.items():
        print(f"  {item_type}: {count}")
