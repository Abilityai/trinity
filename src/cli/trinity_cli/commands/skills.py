"""Skills library commands."""

import click

from ..client import TrinityClient
from ..output import format_output
from ..skill_rows import skill_table_rows


@click.group()
def skills():
    """Browse the skills library."""
    pass


@skills.command("list")
@click.option("--format", "fmt", type=click.Choice(["json", "table"]), default="table", help="Output format")
def list_skills(fmt):
    """List all available skills."""
    client = TrinityClient()
    data = client.get("/api/skills/library")
    if fmt == "table" and isinstance(data, list):
        format_output(skill_table_rows(data), fmt)
    else:
        format_output(data, fmt)


@skills.command("get")
@click.argument("name")
@click.option("--format", "fmt", type=click.Choice(["json", "table"]), default="table", help="Output format")
def get_skill(name, fmt):
    """Get details for a specific skill."""
    client = TrinityClient()
    data = client.get(f"/api/skills/library/{name}")
    format_output(data, fmt)
