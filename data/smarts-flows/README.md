# SMARTS Execution Flows

This directory contains JSON exports of SMARTS trading pipeline execution flows.

## File Naming Convention

```
{symbol}_{YYYY-MM-DD}_{description}.json
```

Example: `MSFT_2026-02-05_full_flow.json`

## Contents

Each JSON file contains the complete execution flow:

1. **MARKET_REGIME** - VIX, SPY levels, warnings, breadth analysis
2. **NEWS_SENTIMENT** - Sentiment scores and key factors for relevant stocks
3. **DISCOVERY** - Scanner opportunities with technical signals
4. **ANALYSIS** - Stance, scenarios, catalysts, risks, expected value
5. **DECISION** - Action, execution plan, risk management
6. **PM_DIRECTIVE** - Portfolio manager restrictions and risk assessments
7. **EXECUTION** - Final execution status, blocked steps, compliance notes

## Usage

Generate new flow exports:

```bash
# Export most recent flow
python3 scripts/update_smarts_flow.py --dry-run --json > data/smarts-flows/SYMBOL_DATE.json

# Or use the Python API directly
from scripts.smarts_diagram.flow_visualizer import SupabaseClient
client = SupabaseClient()
flow = client.get_complete_flow(symbol="MSFT", hours=24)
```

## Note

These files are gitignored by default as they contain live trading data.
Add specific files to git if you want to preserve them as examples.
