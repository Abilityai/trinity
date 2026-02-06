"""Parser for SMARTS agent templates.

Extracts architecture details from agent config.yaml and CLAUDE.md files.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Schedule:
    """Represents a scheduled task for an agent."""

    name: str
    cron: str
    message: str
    timezone: str = "America/New_York"
    market_hours_only: bool = False


@dataclass
class ContextFlow:
    """Represents a context type read or written by an agent."""

    context_type: str
    direction: str  # 'read' or 'write'
    table: str = "integration_context"
    description: str = ""


@dataclass
class AgentSpec:
    """Specification of a SMARTS agent extracted from templates."""

    name: str
    description: str
    version: str = "1.0.0"
    role: str = ""

    # MCP servers required
    mcp_servers: list[str] = field(default_factory=list)

    # Schedule configuration
    schedules: list[Schedule] = field(default_factory=list)

    # Context flows (inputs and outputs)
    reads_from: list[ContextFlow] = field(default_factory=list)
    writes_to: list[ContextFlow] = field(default_factory=list)

    # Key responsibilities extracted from CLAUDE.md
    responsibilities: list[str] = field(default_factory=list)

    # External integrations
    external_integrations: list[str] = field(default_factory=list)

    # Pipeline position (for layout)
    layer: str = ""  # market_context, pipeline, oversight, feedback

    # Raw config for reference
    raw_config: dict[str, Any] = field(default_factory=dict)


# Mapping of agent names to pipeline layers
AGENT_LAYERS = {
    "market-regime": "market_context",
    "news-sentiment": "market_context",
    "discovery": "pipeline",
    "analysis": "pipeline",
    "decision": "pipeline",
    "execution": "pipeline",
    "portfolio-manager": "oversight",
    "feedback": "feedback",
}

# Context type colors for arrows
CONTEXT_COLORS = {
    "market_regime": "#2196F3",  # Blue - market data
    "news_sentiment": "#2196F3",  # Blue - market data
    "scanner_opportunity": "#4CAF50",  # Green - analysis
    "analysis": "#4CAF50",  # Green - analysis
    "decision": "#FF9800",  # Orange - decision/execution
    "execution": "#FF9800",  # Orange - execution
    "pm_directive": "#F44336",  # Red - PM directives
    "feedback_metrics": "#9C27B0",  # Purple - feedback
}


def parse_config_yaml(config_path: Path) -> dict[str, Any]:
    """Parse the config.yaml file for an agent."""
    with open(config_path) as f:
        return yaml.safe_load(f)


def parse_claude_md(claude_md_path: Path) -> dict[str, Any]:
    """Extract structured information from CLAUDE.md file."""
    with open(claude_md_path) as f:
        content = f.read()

    result: dict[str, Any] = {
        "role": "",
        "responsibilities": [],
        "reads_from": [],
        "writes_to": [],
        "external_integrations": [],
    }

    # Extract role from first paragraph after header
    role_match = re.search(
        r"Your role is to (.+?)(?:\.|$)", content, re.IGNORECASE | re.DOTALL
    )
    if role_match:
        result["role"] = role_match.group(1).strip()

    # Extract responsibilities
    resp_section = re.search(
        r"## Responsibilities\s*\n((?:\d+\.\s+\*\*[^*]+\*\*[^\n]+\n?)+)",
        content,
        re.MULTILINE,
    )
    if resp_section:
        responsibilities = re.findall(
            r"\d+\.\s+\*\*([^*]+)\*\*:?\s*([^\n]+)", resp_section.group(1)
        )
        result["responsibilities"] = [f"{title}: {desc}" for title, desc in responsibilities]

    # Extract input context types
    input_section = re.search(
        r"## Input (?:Context|Data)\s*\n((?:.*?\n)+?)(?=\n##|\Z)", content, re.MULTILINE
    )
    if input_section:
        context_types = re.findall(
            r"\|\s*`?(\w+)`?\s*\|", input_section.group(1), re.MULTILINE
        )
        for ctx in context_types:
            if ctx not in ("Context", "Type", "Usage", "Source", "Data", "Metric"):
                result["reads_from"].append(ctx)

    # Extract output context type from "Output Format" section
    output_section = re.search(
        r"context_type['\"]?\s*[:=]\s*['\"]?(\w+)", content, re.MULTILINE
    )
    if output_section:
        result["writes_to"].append(output_section.group(1))

    # Extract external integrations
    if "Alpaca" in content:
        result["external_integrations"].append("Alpaca Markets")
    if "Polygon" in content:
        result["external_integrations"].append("Polygon.io")
    if "Supabase" in content:
        result["external_integrations"].append("Supabase")
    if "Redis" in content:
        result["external_integrations"].append("Redis")

    return result


def parse_agent_template(template_dir: Path) -> AgentSpec | None:
    """Parse a single agent template directory."""
    config_path = template_dir / "config.yaml"
    claude_md_path = template_dir / "CLAUDE.md"

    if not config_path.exists() or not claude_md_path.exists():
        return None

    config = parse_config_yaml(config_path)
    claude_info = parse_claude_md(claude_md_path)

    # Parse schedules
    schedules = []
    for sched in config.get("schedule", []):
        schedules.append(
            Schedule(
                name=sched.get("name", ""),
                cron=sched.get("cron", ""),
                message=sched.get("message", ""),
                timezone=sched.get("timezone", "America/New_York"),
                market_hours_only=sched.get("market_hours_only", False),
            )
        )

    # Parse context flows
    reads_from = []
    for ctx_type in claude_info.get("reads_from", []):
        reads_from.append(
            ContextFlow(
                context_type=ctx_type,
                direction="read",
            )
        )

    writes_to = []
    output_config = config.get("output", {})
    if output_config:
        writes_to.append(
            ContextFlow(
                context_type=output_config.get("context_type", ""),
                direction="write",
                table=output_config.get("table", "integration_context"),
            )
        )

    # Additional writes from config
    if config.get("output", {}).get("persist_to"):
        writes_to.append(
            ContextFlow(
                context_type=config["output"]["persist_to"],
                direction="write",
                table=config["output"]["persist_to"],
            )
        )

    agent_name = config.get("name", template_dir.name)

    return AgentSpec(
        name=agent_name,
        description=config.get("description", ""),
        version=config.get("version", "1.0.0"),
        role=claude_info.get("role", ""),
        mcp_servers=config.get("mcp_servers", []),
        schedules=schedules,
        reads_from=reads_from,
        writes_to=writes_to,
        responsibilities=claude_info.get("responsibilities", []),
        external_integrations=claude_info.get("external_integrations", []),
        layer=AGENT_LAYERS.get(agent_name, "pipeline"),
        raw_config=config,
    )


def parse_agent_templates(templates_dir: str | Path) -> list[AgentSpec]:
    """Parse all SMARTS agent templates and extract architecture details.

    Args:
        templates_dir: Path to the agent-templates directory

    Returns:
        List of AgentSpec objects for each SMARTS agent
    """
    templates_path = Path(templates_dir)
    smarts_agents = [
        "market-regime",
        "news-sentiment",
        "discovery",
        "analysis",
        "decision",
        "execution",
        "portfolio-manager",
        "feedback",
    ]

    agents = []
    for agent_name in smarts_agents:
        agent_dir = templates_path / agent_name
        if agent_dir.exists():
            spec = parse_agent_template(agent_dir)
            if spec:
                agents.append(spec)

    return agents


def get_data_flow_connections(agents: list[AgentSpec]) -> list[dict[str, Any]]:
    """Extract data flow connections between agents.

    Returns a list of connections with source, target, and context type.
    """
    # Build a map of what each agent writes
    writer_map: dict[str, str] = {}
    for agent in agents:
        for ctx in agent.writes_to:
            if ctx.context_type:
                writer_map[ctx.context_type] = agent.name

    # Build connections based on what each agent reads
    connections = []
    for agent in agents:
        for ctx in agent.reads_from:
            if ctx.context_type in writer_map:
                source = writer_map[ctx.context_type]
                connections.append(
                    {
                        "source": source,
                        "target": agent.name,
                        "context_type": ctx.context_type,
                        "color": CONTEXT_COLORS.get(ctx.context_type, "#9E9E9E"),
                    }
                )

    # Add PM directive connections (PM -> Decision, Execution)
    connections.append(
        {
            "source": "portfolio-manager",
            "target": "decision",
            "context_type": "pm_directive",
            "color": CONTEXT_COLORS["pm_directive"],
        }
    )
    connections.append(
        {
            "source": "portfolio-manager",
            "target": "execution",
            "context_type": "pm_directive",
            "color": CONTEXT_COLORS["pm_directive"],
        }
    )

    # Add feedback loop connections
    connections.append(
        {
            "source": "feedback",
            "target": "portfolio-manager",
            "context_type": "feedback_metrics",
            "color": CONTEXT_COLORS["feedback_metrics"],
        }
    )

    return connections


if __name__ == "__main__":
    # Test parsing
    templates_dir = Path(__file__).parent.parent.parent / "config" / "agent-templates"
    agents = parse_agent_templates(templates_dir)

    print(f"Parsed {len(agents)} SMARTS agents:\n")
    for agent in agents:
        print(f"  {agent.name}:")
        print(f"    Layer: {agent.layer}")
        print(f"    Role: {agent.role[:60]}..." if agent.role else "    Role: N/A")
        print(f"    Reads: {[c.context_type for c in agent.reads_from]}")
        print(f"    Writes: {[c.context_type for c in agent.writes_to]}")
        print(f"    Schedules: {len(agent.schedules)}")
        print()

    print("\nData Flow Connections:")
    connections = get_data_flow_connections(agents)
    for conn in connections:
        print(f"  {conn['source']} --[{conn['context_type']}]--> {conn['target']}")
