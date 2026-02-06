"""SMARTS Pipeline Miro Diagram Generator.

This package provides tools to parse SMARTS agent templates and generate
Miro diagrams that visualize the pipeline architecture.
"""

from scripts.smarts_diagram.parser import AgentSpec, parse_agent_templates
from scripts.smarts_diagram.miro_generator import generate_miro_diagram, MiroItem
from scripts.smarts_diagram.miro_client import MiroClient

__all__ = [
    "AgentSpec",
    "parse_agent_templates",
    "generate_miro_diagram",
    "MiroItem",
    "MiroClient",
]
