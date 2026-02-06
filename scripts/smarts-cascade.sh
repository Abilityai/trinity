#!/bin/bash
# SMARTS Trading Pipeline - One-Click Cascade Trigger
# Usage: ./scripts/smarts-cascade.sh [watchlist]
#
# This script triggers the full SMARTS trading pipeline in sequence:
# 1. Market Context (market-regime + news-sentiment in parallel)
# 2. Discovery (scan for opportunities)
# 3. Analysis (deep analysis of opportunities)
# 4. Decision (convert to BUY/SELL/HOLD)
# 5. Execution (submit orders to Alpaca paper trading)
# 6. Feedback (track outcomes)
#
# All agents communicate via Supabase integration_context table.

set -e

# Configuration
WATCHLIST="${1:-SPY,QQQ,IWM,AAPL,MSFT,GOOGL,NVDA,TSLA}"
API="http://localhost:8000/api"
TOKEN="${TRINITY_API_TOKEN:-$(cat ~/.trinity/token 2>/dev/null || echo '')}"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

log() { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $1"; }
success() { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
error() { echo -e "${RED}✗${NC} $1"; }

# Check if token is available
if [ -z "$TOKEN" ]; then
    error "No API token found. Set TRINITY_API_TOKEN or login first."
    echo ""
    echo "To get a token:"
    echo "  export TRINITY_API_TOKEN=\$(curl -s -X POST http://localhost:8000/api/auth/admin/login \\"
    echo "    -H 'Content-Type: application/json' \\"
    echo "    -d '{\"username\":\"admin\",\"password\":\"YOUR_PASSWORD\"}' | jq -r '.access_token')"
    exit 1
fi

# Trigger agent and wait for response
trigger_agent() {
    local agent=$1
    local message=$2
    local timeout=${3:-600}  # 10 min default

    log "Triggering $agent..."

    response=$(curl -s -X POST "$API/agents/$agent/chat" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"message\": \"$message\"}" \
        --max-time $timeout 2>&1)

    # Check for curl errors
    if [ $? -ne 0 ]; then
        warn "$agent: Request failed or timed out"
        return 1
    fi

    # Check for API errors
    if echo "$response" | jq -e '.error' > /dev/null 2>&1; then
        warn "$agent error: $(echo $response | jq -r '.error')"
        return 1
    fi

    # Check for detail error (FastAPI format)
    if echo "$response" | jq -e '.detail' > /dev/null 2>&1; then
        warn "$agent error: $(echo $response | jq -r '.detail')"
        return 1
    fi

    success "$agent completed"
    return 0
}

echo "╔═══════════════════════════════════════════════════════╗"
echo "║       SMARTS Trading Pipeline - Cascade Test          ║"
echo "╚═══════════════════════════════════════════════════════╝"
echo ""
echo "Watchlist: $WATCHLIST"
echo "Timestamp: $(date)"
echo ""

# Verify agents are running
log "Verifying agents are running..."
AGENTS=("market-regime" "news-sentiment" "discovery" "analysis" "decision" "execution" "portfolio-manager" "feedback")
ALL_RUNNING=true

for agent in "${AGENTS[@]}"; do
    status=$(curl -s "$API/agents/$agent" -H "Authorization: Bearer $TOKEN" | jq -r '.status // "unknown"')
    if [ "$status" != "running" ]; then
        warn "$agent is not running (status: $status)"
        ALL_RUNNING=false
    fi
done

if [ "$ALL_RUNNING" = false ]; then
    warn "Some agents are not running. Pipeline may not complete fully."
    echo ""
fi

# Phase 1: Market Context (parallel)
log "═══ Phase 1: Market Context ═══"
trigger_agent "market-regime" \
    "Analyze current market regime. Check VIX, SPY trend vs 50/200 MA, market breadth. Write market_regime context to integration_context table." &
PID1=$!

trigger_agent "news-sentiment" \
    "Analyze news sentiment for symbols: $WATCHLIST. Check for earnings, major news events. Write news_sentiment context to integration_context." &
PID2=$!

wait $PID1 $PID2
echo ""

# Phase 2: Discovery
log "═══ Phase 2: Opportunity Scan ═══"
trigger_agent "discovery" \
    "Scan for trading opportunities in: $WATCHLIST. Check RSI, MACD, support/resistance, volume. Apply regime adjustments from market_regime context. Write scanner_opportunity contexts for any opportunities with score >= 45."
echo ""

# Phase 3: Analysis
log "═══ Phase 3: Deep Analysis ═══"
trigger_agent "analysis" \
    "Analyze all recent scanner_opportunity contexts in integration_context. For each opportunity: model 3 scenarios (optimistic/base/pessimistic), calculate expected value, determine stance (bullish/bearish/neutral). Write analysis context for each."
echo ""

# Phase 4: Decision
log "═══ Phase 4: Trade Decision ═══"
trigger_agent "decision" \
    "Review all recent analysis contexts. Check portfolio state via Alpaca. For each high-confidence analysis: determine action (BUY/SELL/HOLD), calculate position size, set entry/stop/target. Check pm_directive for any blocks. Write decision context."
echo ""

# Phase 5: Execution
log "═══ Phase 5: Order Execution ═══"
trigger_agent "execution" \
    "Review all recent decision contexts with action != HOLD. For each: verify market is open, check buying power, submit bracket order to Alpaca (paper trading). Record fill status. Write execution context."
echo ""

# Phase 6: Feedback (background)
log "═══ Phase 6: Feedback ═══"
trigger_agent "feedback" \
    "Update performance metrics. Check for any closed positions. Calculate win rate, profit factor. Write feedback_metrics context." &
PID_FEEDBACK=$!

# Portfolio Manager check
log "═══ Risk Check ═══"
trigger_agent "portfolio-manager" \
    "Check portfolio health. Verify no emergency conditions (daily loss > 12%, position loss > 8%, VIX > 35). Report status." &
PID_PM=$!

wait $PID_FEEDBACK $PID_PM

echo ""
echo "╔═══════════════════════════════════════════════════════╗"
echo "║              Pipeline Cascade Complete                ║"
echo "╚═══════════════════════════════════════════════════════╝"
echo ""

# Summary
log "Pipeline finished at $(date)"
echo ""
echo "To check results in Supabase, run this SQL:"
echo "────────────────────────────────────────────"
echo ""
echo "SELECT context_type, symbol,"
echo "       context_data->>'confidence' as confidence,"
echo "       context_data->>'regime' as regime,"
echo "       context_data->>'action' as action,"
echo "       created_at"
echo "FROM integration_context"
echo "WHERE created_at > NOW() - INTERVAL '1 hour'"
echo "ORDER BY created_at DESC;"
echo ""
echo "────────────────────────────────────────────"
echo ""
echo "Context type counts:"
echo ""
echo "SELECT context_type, COUNT(*)"
echo "FROM integration_context"
echo "WHERE created_at > NOW() - INTERVAL '1 hour'"
echo "GROUP BY context_type;"
echo ""
