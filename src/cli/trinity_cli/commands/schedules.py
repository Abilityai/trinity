"""Schedule management commands."""

import click

from ..client import TrinityClient
from ..output import format_output


@click.group()
def schedules():
    """Manage agent schedules."""
    pass


@schedules.command("list")
@click.argument("agent")
@click.option("--format", "fmt", type=click.Choice(["json", "table"]), default="table", help="Output format")
def list_schedules(agent, fmt):
    """List schedules for an agent."""
    client = TrinityClient()
    data = client.get(f"/api/agents/{agent}/schedules")
    if fmt == "table" and isinstance(data, list):
        rows = [
            {
                "id": s.get("id", ""),
                "skill": s.get("skill_name", ""),
                "cron": s.get("cron_expression", ""),
                "enabled": s.get("enabled", ""),
            }
            for s in data
        ]
        format_output(rows, fmt)
    else:
        format_output(data, fmt)


@schedules.command("trigger")
@click.argument("agent")
@click.argument("schedule_id")
def trigger_schedule(agent, schedule_id):
    """Trigger a schedule immediately."""
    client = TrinityClient()
    data = client.post(f"/api/agents/{agent}/schedules/{schedule_id}/trigger")
    # #1968: `data` was fetched and thrown away, so the command could not tell
    # the user which run it had just started. The response now carries a real
    # execution_id; print it, guarded, since an older backend still omits it.
    execution_id = (data or {}).get("execution_id")
    if execution_id:
        click.echo(
            f"Triggered schedule {schedule_id} on '{agent}' (execution {execution_id})"
        )
    else:
        click.echo(f"Triggered schedule {schedule_id} on '{agent}'")
