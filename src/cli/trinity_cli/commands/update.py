"""`trinity update` — update the Trinity install on THIS host (#3007).

Runs the checkout's `scripts/deploy/update.sh` with every remaining argument, so
`trinity update --check`, `trinity update --to v1.0.0`, `trinity update --verify`
and `trinity update --rollback` all mean what the script's `--help` says.
"""

import subprocess
import sys

import click

from ..host_update import NotACheckout, build_command, find_update_script


@click.command(
    "update",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True,
                      "help_option_names": []},
)
@click.option("--dir", "directory", default=None,
              help="Trinity checkout to update (default: $TRINITY_HOME, else the current directory)")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def update(directory, args):
    """Update this host's Trinity install (runs scripts/deploy/update.sh).

    Detects the install shape, runs a preflight that changes nothing on failure,
    backs up the database, updates, and verifies the published URL. Pass --help
    to see the script's own options.
    """
    try:
        script = find_update_script(directory)
    except NotACheckout as exc:
        raise click.ClickException(str(exc))
    sys.exit(subprocess.call(build_command(script, list(args))))
