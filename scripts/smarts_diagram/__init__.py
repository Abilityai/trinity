"""SMARTS Pipeline Miro Diagram Generator.

This package provides tools to:
1. Parse SMARTS agent templates and generate architecture diagrams
2. Retrieve and visualize live analysis flows from Supabase
"""

from scripts.smarts_diagram.parser import AgentSpec, parse_agent_templates
from scripts.smarts_diagram.miro_generator import generate_miro_diagram, MiroItem
from scripts.smarts_diagram.miro_client import MiroClient
from scripts.smarts_diagram.flow_visualizer import (
    AnalysisFlow,
    FlowContext,
    SupabaseClient,
    generate_flow_diagram,
    update_miro_flow_diagram,
)

__all__ = [
    # Architecture diagram
    "AgentSpec",
    "parse_agent_templates",
    "generate_miro_diagram",
    "MiroItem",
    "MiroClient",
    # Flow visualizer
    "AnalysisFlow",
    "FlowContext",
    "SupabaseClient",
    "generate_flow_diagram",
    "update_miro_flow_diagram",
]
