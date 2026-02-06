#!/usr/bin/env python3
"""Update SMARTS pipeline diagram in Miro.

This script parses all SMARTS agent templates, generates a diagram,
and updates the Miro board via REST API.

Usage:
    python scripts/update_smarts_diagram.py

Environment variables:
    MIRO_ACCESS_TOKEN - Miro API access token (required)
    MIRO_BOARD_ID - Target Miro board ID (required)

The board will be cleared and recreated with current architecture.
"""

import argparse
import os
import sys
from pathlib import Path

# Add project root to path for imports
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from scripts.smarts_diagram.miro_client import MiroClient, MiroClientError  # noqa: E402
from scripts.smarts_diagram.miro_generator import generate_miro_diagram  # noqa: E402
from scripts.smarts_diagram.parser import parse_agent_templates  # noqa: E402


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Update SMARTS pipeline diagram in Miro"
    )
    parser.add_argument(
        "--templates-dir",
        type=str,
        default=str(project_root / "config" / "agent-templates"),
        help="Path to agent templates directory",
    )
    parser.add_argument(
        "--board-id",
        type=str,
        default=os.getenv("MIRO_BOARD_ID"),
        help="Miro board ID (default: MIRO_BOARD_ID env var)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and generate diagram without updating Miro",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="Don't clear existing items before creating new ones",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose output",
    )

    args = parser.parse_args()

    # Validate environment
    if not args.dry_run:
        if not os.getenv("MIRO_ACCESS_TOKEN"):
            print("Error: MIRO_ACCESS_TOKEN environment variable is required")
            print("Get your token from: https://miro.com/app/settings/user-profile/apps")
            return 1

        if not args.board_id:
            print("Error: MIRO_BOARD_ID environment variable or --board-id is required")
            return 1

    # Parse agent templates
    print(f"Parsing SMARTS agent templates from: {args.templates_dir}")
    agents = parse_agent_templates(args.templates_dir)

    if not agents:
        print("Error: No SMARTS agents found in templates directory")
        return 1

    print(f"Found {len(agents)} SMARTS agents:")
    for agent in agents:
        print(f"  - {agent.name} ({agent.layer})")
        if args.verbose:
            print(f"      Description: {agent.description}")
            print(f"      Schedules: {len(agent.schedules)}")
            print(f"      Reads: {[c.context_type for c in agent.reads_from]}")
            print(f"      Writes: {[c.context_type for c in agent.writes_to]}")

    # Generate diagram data
    print("\nGenerating Miro diagram...")
    diagram = generate_miro_diagram(agents)

    print("Generated diagram with:")
    print(f"  - {len(diagram['items'])} items")
    print(f"  - {len(diagram['connectors'])} connectors")

    if args.dry_run:
        print("\nDry run mode - not updating Miro board")
        print("Diagram data generated successfully")
        return 0

    # Update Miro board
    print(f"\nUpdating Miro board: {args.board_id}")
    try:
        client = MiroClient(board_id=args.board_id)

        # Verify board access
        board = client.get_board()
        print(f"Connected to board: {board.get('name', 'Unknown')}")

        # Update board
        result = client.update_board(
            diagram,
            clear_first=not args.no_clear,
        )

        print("\nDiagram updated successfully!")
        print(f"  Items created: {result['items_created']}")
        print(f"  Connectors created: {result['connectors_created']}")
        print(f"\nView board at: {result['board_url']}")

        return 0

    except MiroClientError as e:
        print(f"\nError updating Miro board: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
