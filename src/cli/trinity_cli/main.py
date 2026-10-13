"""Trinity CLI — main entry point.

Usage:
    trinity init                    # Set up and authenticate
    trinity login                   # Log in to an instance
    trinity deploy .                # Deploy local agent directory
    trinity agents list             # List agents
    trinity chat my-agent "hello"   # Chat with an agent
    trinity logs my-agent           # View agent logs
    trinity profile list            # Show all profiles
    trinity profile use prod        # Switch to a profile
"""

import click

from . import __version__
from .commands.agents import agents
from .commands.auth import init, login, logout, status
from .commands.chat import chat_history, chat_with_agent, logs
from .commands.deploy import deploy
from .commands.health import health
from .commands.profiles import profile
from .commands.schedules import schedules
from .commands.skills import skills
from .commands.tags import tags


@click.group()
@click.version_option(version=__version__, prog_name="trinity")
@click.option("--profile", "profile_name", envvar="TRINITY_PROFILE", default=None,
              help="Profile to use (overrides TRINITY_PROFILE env var)")
@click.pass_context
def cli(ctx, profile_name):
    """Command-line interface for Trinity, the operating system for the AI-native company.

    Get started:

        trinity init          Configure instance and log in

        trinity agents list   List your agents

        trinity chat <agent> "message"   Chat with an agent

    Manage multiple instances with profiles:

        trinity profile list  Show configured profiles

        trinity profile use <name>  Switch active profile
    """
    ctx.ensure_object(dict)
    ctx.obj["profile"] = profile_name


# Auth commands (top-level)
cli.add_command(init)
cli.add_command(login)
cli.add_command(logout)
cli.add_command(status)

# Profile management
cli.add_command(profile)

# Deploy command (top-level)
cli.add_command(deploy)

# Resource commands (groups)
cli.add_command(agents)
cli.add_command(health)
cli.add_command(skills)
cli.add_command(schedules)
cli.add_command(tags)

# Standalone commands
cli.add_command(chat_with_agent)
cli.add_command(chat_history, name="history")
cli.add_command(logs)


def main():
    cli()


if __name__ == "__main__":
    main()
