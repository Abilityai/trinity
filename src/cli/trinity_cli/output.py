"""Output formatting for Trinity CLI.

Table (human-readable) by default. Use --format json for piping/scripting.
"""

import json
import sys
from typing import Any

import click


def format_output(data: Any, fmt: str = "table"):
    """Format and print data according to the chosen format."""
    if fmt == "json":
        click.echo(json.dumps(data, indent=2, default=str))
    elif fmt == "table":
        if isinstance(data, list):
            _print_table(data)
        elif isinstance(data, dict):
            _print_dict(data)
        else:
            click.echo(str(data))


def _print_table(rows: list[dict]):
    """Print a list of dicts as a table."""
    if not rows:
        click.echo("(no results)")
        return

    # Use rich for nice tables
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    console = Console(file=sys.stdout)
    table = Table(show_header=True, header_style="bold")

    keys = list(rows[0].keys())
    for key in keys:
        table.add_column(key)

    # Cells are Text, never markup: values are server data (a library author's
    # description or successor), and Rich parses `[...]` in a plain string, so
    # `[/]` raised MarkupError and took the whole listing down (ent#736).
    for row in rows:
        table.add_row(*[Text(str(row.get(k, ""))) for k in keys])

    console.print(table)


def _print_dict(data: dict):
    """Print a single dict as key-value pairs."""
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    console = Console(file=sys.stdout)
    table = Table(show_header=True, header_style="bold")
    table.add_column("Field")
    table.add_column("Value")

    for key, value in data.items():
        table.add_row(Text(str(key)), Text(str(value)))

    console.print(table)
