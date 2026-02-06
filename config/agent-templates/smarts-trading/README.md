# SMARTS Trinity Multi-Agent Trading System

A distributed trading system implemented as 8 specialized Trinity agents that communicate via shared Supabase database.

## Architecture

```
Pipeline Flow:

┌──────────────┐   ┌──────────────┐
│   Market     │   │   News/      │
│   Regime     │   │  Sentiment   │
└──────┬───────┘   └──────┬───────┘
       │                  │
       └────────┬─────────┘
                ▼
       ┌──────────────┐
       │   Discovery  │
       │   (Scanner)  │
       └──────┬───────┘
              ▼
       ┌──────────────┐
       │   Analysis   │
       └──────┬───────┘
              ▼
       ┌──────────────┐   ┌──────────────┐
       │   Decision   │◄──│   Portfolio  │
       │    Maker     │   │   Manager    │
       └──────┬───────┘   └──────────────┘
              ▼
       ┌──────────────┐
       │  Execution   │
       └──────┬───────┘
              ▼
       ┌──────────────┐
       │   Feedback   │
       └──────────────┘
```

## Agents

| Agent | Template Dir | Purpose |
|-------|--------------|---------|
| **market-regime** | Market Regime Agent | Detect bull/bear/neutral/volatile conditions |
| **news-sentiment** | News/Sentiment Agent | Analyze news, earnings, and sentiment |
| **discovery** | Discovery Agent (Scanner) | Find trading opportunities via technical scanning |
| **analysis** | Analysis Agent | Deep analysis with scenario modeling |
| **decision** | Decision Maker Agent | BUY/SELL/HOLD decisions with position sizing |
| **execution** | Execution Agent | Order validation and Alpaca submission |
| **portfolio-manager** | Portfolio Manager | Emergency oversight and risk triggers |
| **feedback** | Feedback Agent | Track outcomes and calculate metrics |

## Communication Flow

Agents communicate via Supabase `integration_context` table with TTL:

| Context Type | Written By | Consumed By |
|--------------|------------|-------------|
| `market_regime` | Market Regime Agent | Discovery, Analysis, Decision |
| `news_sentiment` | News/Sentiment Agent | Discovery, Analysis |
| `scanner_opportunity` | Discovery Agent | Analysis |
| `analysis` | Analysis Agent | Decision |
| `decision` | Decision Agent | Execution |
| `execution` | Execution Agent | Feedback |
| `pm_directive` | Portfolio Manager | Decision, Execution |
| `feedback_metrics` | Feedback Agent | All agents |

## Required Environment Variables

```bash
# Alpaca Trading API (Required)
ALPACA_API_KEY=your-api-key
ALPACA_SECRET_KEY=your-secret-key
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# Supabase Database (Required)
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=your-service-role-key
SUPABASE_PROJECT_ID=your-project-id

# Shared Folder (Optional, defaults to /shared)
MASSIVE_FOLDER=/shared
```

## Database Tables

| Table | Purpose |
|-------|---------|
| `integration_context` | Inter-agent communication via context types |
| `trading_evaluations` | Persistent trading decision history |
| `trading_metrics` | Aggregated performance by period |
| `pm_directives` | Portfolio Manager emergency commands |
| `agent_configurations` | Personality-based configuration |

## Personality Configurations

Three trading personalities are available:

### Conservative
- Min confidence for trades: 70%
- Max position size: 2.5%
- Risk-reward minimum: 1:4
- RSI oversold threshold: 25

### Balanced (Default)
- Min confidence for trades: 60%
- Max position size: 3.0%
- Risk-reward minimum: 1:3
- RSI oversold threshold: 30

### Aggressive
- Min confidence for trades: 50%
- Max position size: 5.0%
- Risk-reward minimum: 1:2
- RSI oversold threshold: 35

## Schedule Summary

| Agent | Primary Schedule |
|-------|------------------|
| Market Regime | Hourly + pre-market (9 AM) |
| News/Sentiment | Every 30 min + pre-market (8:30 AM) |
| Discovery | 4x/hour + opening bell (9:35 AM) + power hour (3 PM) |
| Analysis | 4x/hour + pre-decision |
| Decision | Every 30 min (15, 45) |
| Execution | Every 5 min + fill monitor (every min) |
| Portfolio Manager | Every 5 min + VIX check (15 min) |
| Feedback | Every 5 min + hourly + daily report (4:30 PM) |

## File Structure

Each agent contains:
```
agent-name/
├── CLAUDE.md           # Agent brain (identity, tools, workflows)
├── config.yaml         # Trinity metadata, MCP servers, schedule
├── .mcp.json.template  # MCP config with ${VAR} placeholders
└── .gitignore          # Excludes secrets
```

## Getting Started

1. **Deploy Agent Templates**: Create agents from each template in Trinity UI
2. **Configure Credentials**: Set up Alpaca and Supabase credentials per agent
3. **Apply Database Migration**: Run the migration (already applied to smarts-v2 project)
4. **Set Personality**: Configure each agent's personality in `agent_configurations`
5. **Start with Paper Trading**: Always test with paper trading first!

## Safety Features

- **Paper Trading Mode**: Always test in paper mode first
- **PM Emergency Stops**: Auto-halt on daily loss > 12%
- **Position Limits**: Per-personality max position sizing
- **Bracket Orders**: All trades include TP/SL by default
- **Directive Compliance**: All agents check PM directives before acting

## Safety Notes

- **EXECUTION AGENT** submits real orders to Alpaca
- Always test with paper trading credentials first
- Never commit `.env` or `.mcp.json` files (contain secrets)
- All trading decisions require explicit confirmation through the pipeline
