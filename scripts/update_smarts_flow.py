#!/usr/bin/env python3
"""
Update SMARTS Analysis Flow Diagram in Miro.

This script retrieves a complete analysis flow from Supabase and
visualizes it on a Miro board, showing the full decision chain
from market regime through execution.

Unlike the architecture diagram (update_smarts_diagram.py) which shows
the static pipeline structure, this shows a LIVE execution flow with
actual data from a specific trading analysis.

Usage:
    python scripts/update_smarts_flow.py              # Auto-select best flow
    python scripts/update_smarts_flow.py -s AAPL      # Specific symbol
    python scripts/update_smarts_flow.py --hours 48   # Look back 48 hours
    python scripts/update_smarts_flow.py --dry-run    # Preview without updating

Environment variables:
    SUPABASE_URL         - Supabase project URL
    SUPABASE_ANON_KEY    - Supabase anon/service key
    MIRO_ACCESS_TOKEN    - Miro API access token
    MIRO_BOARD_ID        - Default Miro board ID
"""

import sys
from pathlib import Path

# Add project root to path for imports
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from scripts.smarts_diagram.flow_visualizer import main  # noqa: E402

if __name__ == "__main__":
    main()
