"""
SMARTS Flow Visualizer - Miro Diagram Generator

Retrieves a complete agent analysis flow from Supabase and creates
a detailed Miro visualization showing the full decision chain.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx

from scripts.smarts_diagram.miro_client import MiroClient, MiroClientError


@dataclass
class FlowContext:
    """A single context entry in the flow."""

    id: str
    context_type: str
    symbol: str | None
    created_at: datetime
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AnalysisFlow:
    """Complete analysis flow for a symbol."""

    symbol: str
    start_time: datetime
    end_time: datetime
    contexts: list[FlowContext] = field(default_factory=list)

    @property
    def duration_minutes(self) -> float:
        """Total flow duration in minutes."""
        return (self.end_time - self.start_time).total_seconds() / 60


# =============================================================================
# Supabase Data Retrieval
# =============================================================================


class SupabaseClient:
    """Client for querying Supabase integration_context table."""

    def __init__(self, url: str | None = None, key: str | None = None) -> None:
        """Initialize Supabase client."""
        self.url = url or os.getenv("SUPABASE_URL")
        self.key = key or os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_SERVICE_KEY")

        if not self.url or not self.key:
            raise ValueError(
                "Supabase credentials required. Set SUPABASE_URL and SUPABASE_ANON_KEY "
                "environment variables."
            )

    def get_recent_contexts(
        self,
        hours: int = 24,
        limit: int = 500,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get recent contexts from Supabase."""
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat() + "Z"

        params: dict[str, str] = {
            "select": "*",
            "order": "created_at.asc",
            "limit": str(limit),
            "created_at": f"gte.{since}",
        }

        if symbol:
            params["symbol"] = f"eq.{symbol}"

        with httpx.Client(timeout=30.0) as client:
            response = client.get(
                f"{self.url}/rest/v1/integration_context",
                params=params,
                headers={
                    "apikey": self.key,
                    "Authorization": f"Bearer {self.key}",
                },
            )

            if response.status_code == 200:
                return response.json()
            else:
                raise ValueError(f"Supabase query failed: {response.status_code} {response.text}")

    def get_complete_flow(self, symbol: str | None = None, hours: int = 24) -> AnalysisFlow | None:
        """
        Get a complete analysis flow for a symbol.

        A complete flow includes: market_regime → scanner_opportunity → analysis → decision → execution
        """
        contexts_raw = self.get_recent_contexts(hours=hours, symbol=symbol)

        if not contexts_raw:
            return None

        # Parse contexts
        contexts: list[FlowContext] = []
        for raw in contexts_raw:
            try:
                created_at = datetime.fromisoformat(raw["created_at"].replace("Z", "+00:00"))
            except (ValueError, KeyError):
                created_at = datetime.utcnow()

            contexts.append(
                FlowContext(
                    id=raw.get("id", ""),
                    context_type=raw.get("context_type", "unknown"),
                    symbol=raw.get("symbol"),
                    created_at=created_at,
                    data=raw.get("context_data", {}),
                )
            )

        if not contexts:
            return None

        # If no symbol specified, find the most complete flow
        if symbol is None:
            # Group by symbol
            by_symbol: dict[str, list[FlowContext]] = {}
            for ctx in contexts:
                if ctx.symbol:
                    if ctx.symbol not in by_symbol:
                        by_symbol[ctx.symbol] = []
                    by_symbol[ctx.symbol].append(ctx)

            # Find symbol with most complete flow
            best_symbol = None
            best_count = 0
            flow_types = {"scanner_opportunity", "analysis", "decision", "execution"}

            for sym, sym_contexts in by_symbol.items():
                types_present = {c.context_type for c in sym_contexts}
                count = len(types_present & flow_types)
                if count > best_count:
                    best_count = count
                    best_symbol = sym

            if best_symbol:
                symbol = best_symbol
            else:
                # Just take the first symbol we find
                symbol = next((c.symbol for c in contexts if c.symbol), "UNKNOWN")

        # Filter to this symbol + global contexts
        flow_contexts = [
            c for c in contexts if c.symbol == symbol or c.context_type in ("market_regime", "news_sentiment", "pm_directive")
        ]

        if not flow_contexts:
            return None

        # Sort by time
        flow_contexts.sort(key=lambda c: c.created_at)

        return AnalysisFlow(
            symbol=symbol or "UNKNOWN",
            start_time=flow_contexts[0].created_at,
            end_time=flow_contexts[-1].created_at,
            contexts=flow_contexts,
        )


# =============================================================================
# Miro Flow Diagram Generation
# =============================================================================

# Layout configuration - positioned below architecture diagram (which ends ~Y=1800)
FLOW_START_X = 200
FLOW_START_Y = 2200  # Below architecture diagram
CARD_WIDTH = 450
CARD_HEIGHT = 350
HORIZONTAL_SPACING = 550
VERTICAL_SPACING = 450

# Context type colors (Miro sticky note colors)
CONTEXT_COLORS = {
    "market_regime": "cyan",
    "news_sentiment": "light_blue",
    "scanner_opportunity": "light_green",
    "analysis": "yellow",
    "decision": "orange",
    "execution": "red",
    "pm_directive": "violet",
    "feedback_metrics": "gray",
}

# Flow order - determines horizontal position
FLOW_ORDER = [
    "market_regime",
    "news_sentiment",
    "scanner_opportunity",
    "analysis",
    "decision",
    "execution",
]


def format_timestamp(dt: datetime) -> str:
    """Format timestamp for display."""
    return dt.strftime("%H:%M:%S UTC")


def format_market_regime_content(data: dict[str, Any], created_at: datetime) -> str:
    """Format market regime context for Miro card."""
    regime = data.get("market_regime", data.get("regime", "N/A")).upper()
    vix = data.get("vix_level", "N/A")
    spy = data.get("spy_price", "N/A")
    description = data.get("description", "")
    warnings = data.get("warning_flags", [])

    lines = [
        "<b>🌍 MARKET REGIME</b>",
        f"<i>{format_timestamp(created_at)}</i>",
        "<br>",
        f"<b>Regime:</b> {regime}",
        f"<b>VIX:</b> {vix}",
        f"<b>SPY:</b> ${spy}",
        "<br>",
        "<b>Assessment:</b>",
        description,
    ]

    if warnings:
        lines.append("<br>")
        lines.append("<b>Warnings:</b>")
        for w in warnings:
            lines.append(f"• {w}")

    return "<br>".join(lines)


def format_news_sentiment_content(data: dict[str, Any], created_at: datetime, symbol: str) -> str:
    """Format news sentiment context for Miro card."""
    sentiment = data.get("sentiment", "N/A")
    direction = data.get("direction", "")
    score = data.get("sentiment_score", 0)
    theme = data.get("theme", "")
    factors = data.get("key_factors", [])

    lines = [
        "<b>📰 NEWS SENTIMENT</b>",
        f"<i>{format_timestamp(created_at)} | {symbol}</i>",
        "<br>",
        f"<b>Sentiment:</b> {sentiment} ({direction})",
        f"<b>Score:</b> {score:.2f}",
        f"<b>Theme:</b> {theme}",
    ]

    if factors:
        lines.append("<br>")
        lines.append("<b>Key Factors:</b>")
        for f in factors:
            lines.append(f"• {f}")

    return "<br>".join(lines)


def format_scanner_content(data: dict[str, Any], created_at: datetime, symbol: str) -> str:
    """Format scanner opportunity context for Miro card."""
    score = data.get("score", "N/A")
    price = data.get("current_price", "N/A")
    rsi = data.get("rsi", "N/A")
    change = data.get("price_change", 0)
    signals = data.get("signals", [])
    rec = data.get("recommendation", {})
    setup = rec.get("setup", "N/A")
    rationale = rec.get("rationale", "")
    trade_idea = rec.get("trade_idea", "")

    lines = [
        "<b>🔍 DISCOVERY</b>",
        f"<i>{format_timestamp(created_at)} | {symbol}</i>",
        "<br>",
        f"<b>Score:</b> {score}/100",
        f"<b>Price:</b> ${price} ({change:+.2f}%)",
        f"<b>RSI:</b> {rsi}",
        f"<b>Setup:</b> {setup}",
        "<br>",
        "<b>Signals:</b>",
        ", ".join(signals[:4]),
        "<br>",
        "<b>Rationale:</b>",
        rationale,
        "<br>",
        "<b>Trade Idea:</b>",
        trade_idea,
    ]

    return "<br>".join(lines)


def format_analysis_content(data: dict[str, Any], created_at: datetime, symbol: str) -> str:
    """Format analysis context for Miro card."""
    stance = data.get("stance", "N/A")
    confidence = data.get("confidence_level", data.get("confidence", "N/A"))
    ev_pct = data.get("expected_value_pct", 0)
    price = data.get("current_price", "N/A")

    scenarios = data.get("scenarios", {})
    base = scenarios.get("base", {})
    optimistic = scenarios.get("optimistic", {})
    pessimistic = scenarios.get("pessimistic", {})

    rec = data.get("recommendation", {})
    action = rec.get("action", "N/A")
    entry = rec.get("entry_price", "N/A")
    stop = rec.get("stop_loss", "N/A")
    targets = rec.get("profit_targets", [])

    catalysts = data.get("key_catalysts", [])
    risks = data.get("key_risks", [])

    lines = [
        "<b>📊 ANALYSIS</b>",
        f"<i>{format_timestamp(created_at)} | {symbol}</i>",
        "<br>",
        f"<b>Stance:</b> {stance}",
        f"<b>Confidence:</b> {confidence}",
        f"<b>Expected Value:</b> {ev_pct:.2f}%",
        f"<b>Price:</b> ${price}",
        "<br>",
        "<b>Scenarios:</b>",
        f"📈 Optimistic: ${optimistic.get('price_target', 'N/A')} ({int(optimistic.get('probability', 0)*100)}%)",
        f"➡️ Base: ${base.get('price_target', 'N/A')} ({int(base.get('probability', 0)*100)}%)",
        f"📉 Pessimistic: ${pessimistic.get('price_target', 'N/A')} ({int(pessimistic.get('probability', 0)*100)}%)",
        "<br>",
        f"<b>Action:</b> {action}",
        f"<b>Entry:</b> ${entry} | <b>Stop:</b> ${stop}",
        f"<b>Targets:</b> {', '.join([f'${t}' for t in targets[:3]])}",
    ]

    if catalysts:
        lines.append("<br>")
        lines.append("<b>Catalysts:</b>")
        for c in catalysts:
            lines.append(f"✅ {c}")

    if risks:
        lines.append("<br>")
        lines.append("<b>Risks:</b>")
        for r in risks:
            lines.append(f"⚠️ {r}")

    return "<br>".join(lines)


def format_decision_content(data: dict[str, Any], created_at: datetime, symbol: str) -> str:
    """Format decision context for Miro card."""
    decision = data.get("decision", {})
    action = decision.get("action", data.get("action", "N/A"))
    confidence = decision.get("confidence", "N/A")
    urgency = decision.get("urgency", "N/A")
    rationale = decision.get("rationale", "")

    current = data.get("current_situation", {})
    existing_pos = current.get("existing_position", "NONE")
    current_pnl = current.get("current_pnl", 0)

    plan = data.get("execution_plan", {})
    step1 = plan.get("step_1", {})
    step2 = plan.get("step_2", {})

    outcomes = data.get("expected_outcomes", {})
    combined_ev = outcomes.get("combined_expected_value", 0)

    risk = data.get("risk_management", {})
    stop_loss = risk.get("stop_loss", "N/A")

    lines = [
        "<b>🎯 DECISION</b>",
        f"<i>{format_timestamp(created_at)} | {symbol}</i>",
        "<br>",
        f"<b>ACTION: {action}</b>",
        f"<b>Confidence:</b> {confidence}",
        f"<b>Urgency:</b> {urgency}",
        "<br>",
        f"<b>Current Position:</b> {existing_pos}",
        f"<b>Current P&L:</b> ${current_pnl:+,.2f}",
        "<br>",
        "<b>Execution Plan:</b>",
        f"1. {step1.get('action', 'N/A')} - {step1.get('quantity', 'N/A')} shares",
        f"2. {step2.get('action', 'N/A')} @ ${step2.get('limit_price', 'N/A')}",
        "<br>",
        f"<b>Expected Value:</b> ${combined_ev:,.2f}",
        f"<b>Stop Loss:</b> ${stop_loss}",
        "<br>",
        "<b>Rationale:</b>",
        rationale,
    ]

    return "<br>".join(lines)


def format_execution_content(data: dict[str, Any], created_at: datetime, symbol: str) -> str:
    """Format execution context for Miro card."""
    status = data.get("execution_status", data.get("status", "N/A"))
    blocking_reason = data.get("blocking_reason", "None")

    decision_details = data.get("decision_details", {})
    action = decision_details.get("action", "N/A")

    blocked = data.get("blocked_steps", [])
    pm_status = data.get("pm_directive_status", "N/A")
    pm_details = data.get("pm_directive_details", {})
    restrictions = pm_details.get("restrictions", [])

    impact = data.get("financial_impact", {})
    opportunity_cost = impact.get("opportunity_cost", 0)

    compliance = data.get("compliance_note", "")

    lines = [
        "<b>⚡ EXECUTION</b>",
        f"<i>{format_timestamp(created_at)} | {symbol}</i>",
        "<br>",
        f"<b>STATUS: {status}</b>",
        f"<b>Intended Action:</b> {action}",
        f"<b>Blocking Reason:</b> {blocking_reason}",
        "<br>",
        "<b>Blocked Steps:</b>",
    ]

    if blocked:
        for step in blocked[:2]:
            lines.append(f"• {step.get('action', 'N/A')}: {step.get('status', 'N/A')}")
    else:
        lines.append("• None")

    lines.extend(
        [
            "<br>",
            f"<b>PM Directive:</b> {pm_status}",
            f"<b>Restrictions:</b> {', '.join(restrictions[:3]) if restrictions else 'None'}",
            "<br>",
            f"<b>Opportunity Cost:</b> ${opportunity_cost:,.2f}",
            "<br>",
            "<b>Compliance:</b>",
            compliance,
        ]
    )

    return "<br>".join(lines)


def format_pm_directive_content(data: dict[str, Any], created_at: datetime) -> str:
    """Format PM directive context for Miro card."""
    status = data.get("status", "N/A")
    expires = data.get("expires_at", "N/A")

    restrictions = data.get("restrictions", [])
    risk = data.get("risk_assessment", {})
    leverage = risk.get("leverage", 0)
    portfolio_value = risk.get("portfolio_value", 0)
    breaches = risk.get("breaches", [])
    warnings = risk.get("warnings", [])

    lines = [
        "<b>🚨 PM DIRECTIVE</b>",
        f"<i>{format_timestamp(created_at)}</i>",
        "<br>",
        f"<b>Status:</b> {status}",
        f"<b>Expires:</b> {expires}",
        f"<b>Portfolio:</b> ${portfolio_value:,.2f}",
        f"<b>Leverage:</b> {leverage:.2f}x",
        "<br>",
        "<b>Restrictions:</b>",
    ]

    if restrictions:
        for r in restrictions[:3]:
            lines.append(f"🚫 {r.get('type', 'N/A')}")
    else:
        lines.append("• None")

    if breaches:
        lines.append("<br>")
        lines.append("<b>Breaches:</b>")
        for b in breaches[:2]:
            lines.append(f"⚠️ {b.get('type', 'N/A')} ({b.get('severity', 'N/A')})")

    if warnings:
        lines.append("<br>")
        lines.append("<b>Warnings:</b>")
        for w in warnings[:2]:
            lines.append(f"⚠️ {w.get('type', 'N/A')}")

    return "<br>".join(lines)


def format_context_content(ctx: FlowContext) -> str:
    """Format context content based on type."""
    formatters = {
        "market_regime": lambda: format_market_regime_content(ctx.data, ctx.created_at),
        "news_sentiment": lambda: format_news_sentiment_content(ctx.data, ctx.created_at, ctx.symbol or ""),
        "scanner_opportunity": lambda: format_scanner_content(ctx.data, ctx.created_at, ctx.symbol or ""),
        "analysis": lambda: format_analysis_content(ctx.data, ctx.created_at, ctx.symbol or ""),
        "decision": lambda: format_decision_content(ctx.data, ctx.created_at, ctx.symbol or ""),
        "execution": lambda: format_execution_content(ctx.data, ctx.created_at, ctx.symbol or ""),
        "pm_directive": lambda: format_pm_directive_content(ctx.data, ctx.created_at),
    }

    formatter = formatters.get(ctx.context_type)
    if formatter:
        return formatter()

    # Generic fallback
    return f"<b>{ctx.context_type.upper()}</b><br><i>{format_timestamp(ctx.created_at)}</i><br><br>Data keys: {', '.join(list(ctx.data.keys())[:8])}"


def generate_flow_diagram(flow: AnalysisFlow) -> dict[str, Any]:
    """
    Generate Miro diagram data for an analysis flow.

    Layout:
    - Section title at top
    - Two columns on left: Market Regime | News Sentiment (stacked)
    - Main pipeline row: Discovery → Analysis → Decision → Execution
    - PM Directive below the pipeline (if present)

    Returns dict with items and connectors to create on Miro.
    """
    items: list[dict[str, Any]] = []
    connectors: list[dict[str, Any]] = []

    # Layout constants for this section
    section_x = FLOW_START_X
    section_y = FLOW_START_Y
    news_stack_spacing = 320  # Vertical spacing for stacked news cards

    # Section title
    items.append(
        {
            "type": "text",
            "data": {"content": "<b>═══════════════════════════════════════════════════════════</b>"},
            "style": {"fontSize": "24", "fontFamily": "open_sans", "textAlign": "center"},
            "position": {"x": section_x + 1200, "y": section_y - 280},
            "geometry": {"width": 1600},
        }
    )

    items.append(
        {
            "type": "text",
            "data": {"content": f"<b>SMARTS Analysis Flow: {flow.symbol}</b>"},
            "style": {"fontSize": "36", "fontFamily": "open_sans", "textAlign": "center"},
            "position": {"x": section_x + 1200, "y": section_y - 230},
            "geometry": {"width": 1000},
        }
    )

    # Subtitle with timing
    duration = flow.duration_minutes
    items.append(
        {
            "type": "text",
            "data": {
                "content": f"{flow.start_time.strftime('%Y-%m-%d %H:%M')} → {flow.end_time.strftime('%H:%M')} UTC  |  Duration: {duration:.1f} min  |  {len(flow.contexts)} contexts"
            },
            "style": {"fontSize": "18", "fontFamily": "open_sans", "textAlign": "center"},
            "position": {"x": section_x + 1200, "y": section_y - 180},
            "geometry": {"width": 1000},
        }
    )

    # Track positions for connectors
    context_positions: dict[str, int] = {}  # context_type -> item index

    # Group contexts by type for positioning
    by_type: dict[str, list[FlowContext]] = {}
    for ctx in flow.contexts:
        if ctx.context_type not in by_type:
            by_type[ctx.context_type] = []
        by_type[ctx.context_type].append(ctx)

    # === LAYOUT ===
    # Column 0: Market Regime (top) + News Sentiment (stacked below)
    # Column 1-4: Main pipeline (Discovery → Analysis → Decision → Execution)
    # Row below: PM Directive (centered)

    pipeline_start_x = section_x + HORIZONTAL_SPACING  # Start pipeline after context column

    # --- Market Regime (Column 0, top) ---
    if "market_regime" in by_type:
        ctx = by_type["market_regime"][0]
        color = CONTEXT_COLORS.get("market_regime", "cyan")
        x = section_x
        y = section_y

        item_idx = len(items)
        items.append(
            {
                "type": "sticky_note",
                "data": {"content": format_context_content(ctx), "shape": "rectangle"},
                "style": {"fillColor": color, "textAlign": "left", "textAlignVertical": "top"},
                "position": {"x": x, "y": y},
                "geometry": {"width": CARD_WIDTH},
            }
        )
        context_positions["market_regime"] = item_idx

    # --- News Sentiment (Column 0, stacked below market regime) ---
    if "news_sentiment" in by_type:
        for row_idx, ctx in enumerate(by_type["news_sentiment"]):
            color = CONTEXT_COLORS.get("news_sentiment", "light_blue")
            x = section_x
            y = section_y + VERTICAL_SPACING + row_idx * news_stack_spacing

            item_idx = len(items)
            items.append(
                {
                    "type": "sticky_note",
                    "data": {"content": format_context_content(ctx), "shape": "rectangle"},
                    "style": {"fillColor": color, "textAlign": "left", "textAlignVertical": "top"},
                    "position": {"x": x, "y": y},
                    "geometry": {"width": CARD_WIDTH},
                }
            )
            if "news_sentiment" not in context_positions:
                context_positions["news_sentiment"] = item_idx

    # --- Main Pipeline (Columns 1-4) ---
    pipeline_types = ["scanner_opportunity", "analysis", "decision", "execution"]
    for col_idx, ctx_type in enumerate(pipeline_types):
        if ctx_type not in by_type:
            continue

        ctx = by_type[ctx_type][0]  # Take first (should only be one)
        color = CONTEXT_COLORS.get(ctx_type, "light_yellow")
        x = pipeline_start_x + col_idx * HORIZONTAL_SPACING
        y = section_y + VERTICAL_SPACING // 2  # Center vertically with context column

        item_idx = len(items)
        items.append(
            {
                "type": "sticky_note",
                "data": {"content": format_context_content(ctx), "shape": "rectangle"},
                "style": {"fillColor": color, "textAlign": "left", "textAlignVertical": "top"},
                "position": {"x": x, "y": y},
                "geometry": {"width": CARD_WIDTH},
            }
        )
        context_positions[ctx_type] = item_idx

    # --- PM Directive (below pipeline, centered) ---
    if "pm_directive" in by_type:
        ctx = by_type["pm_directive"][0]
        color = CONTEXT_COLORS.get("pm_directive", "violet")
        # Position below decision/execution
        x = pipeline_start_x + 1.5 * HORIZONTAL_SPACING
        y = section_y + VERTICAL_SPACING * 2

        item_idx = len(items)
        items.append(
            {
                "type": "sticky_note",
                "data": {"content": format_context_content(ctx), "shape": "rectangle"},
                "style": {"fillColor": color, "textAlign": "left", "textAlignVertical": "top"},
                "position": {"x": x, "y": y},
                "geometry": {"width": CARD_WIDTH},
            }
        )
        context_positions["pm_directive"] = item_idx

    # Create connectors between flow stages
    flow_connections = [
        ("market_regime", "scanner_opportunity", "#2196F3", "market context"),
        ("news_sentiment", "scanner_opportunity", "#2196F3", "news"),
        ("scanner_opportunity", "analysis", "#4CAF50", "opportunity"),
        ("analysis", "decision", "#FF9800", "analysis"),
        ("decision", "execution", "#F44336", "decision"),
        ("pm_directive", "decision", "#9C27B0", "directive"),
        ("pm_directive", "execution", "#9C27B0", "directive"),
    ]

    for source, target, color, label in flow_connections:
        if source in context_positions and target in context_positions:
            connectors.append(
                {
                    "source_index": context_positions[source],
                    "target_index": context_positions[target],
                    "label": label,
                    "color": color,
                }
            )

    return {
        "items": items,
        "connectors": connectors,
        "metadata": {
            "symbol": flow.symbol,
            "start_time": flow.start_time.isoformat(),
            "end_time": flow.end_time.isoformat(),
            "duration_minutes": flow.duration_minutes,
            "context_count": len(flow.contexts),
        },
    }


def update_miro_flow_diagram(
    flow: AnalysisFlow,
    board_id: str | None = None,
    clear_first: bool = True,
) -> dict[str, Any]:
    """
    Update Miro board with flow diagram.

    Args:
        flow: The analysis flow to visualize
        board_id: Miro board ID (uses MIRO_FLOW_BOARD_ID or MIRO_BOARD_ID env var if not provided)
        clear_first: Whether to clear existing items first

    Returns:
        Summary of created items
    """
    # Prefer MIRO_FLOW_BOARD_ID for flows, fall back to MIRO_BOARD_ID
    if board_id is None:
        board_id = os.getenv("MIRO_FLOW_BOARD_ID") or os.getenv("MIRO_BOARD_ID")

    diagram_data = generate_flow_diagram(flow)

    client = MiroClient(board_id=board_id)
    result = client.update_board(diagram_data, clear_first=clear_first)

    return {
        **result,
        "symbol": flow.symbol,
        "contexts": len(flow.contexts),
        "duration_minutes": flow.duration_minutes,
    }


# =============================================================================
# CLI Entry Point
# =============================================================================


def main() -> None:
    """Main entry point for flow visualization."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Visualize SMARTS analysis flow on Miro board",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Visualize most complete flow from last 24 hours
  python -m scripts.smarts_diagram.flow_visualizer

  # Visualize flow for specific symbol
  python -m scripts.smarts_diagram.flow_visualizer --symbol AAPL

  # Look back 48 hours
  python -m scripts.smarts_diagram.flow_visualizer --hours 48

  # Dry run (show data without updating Miro)
  python -m scripts.smarts_diagram.flow_visualizer --dry-run

Environment variables:
  SUPABASE_URL         Supabase project URL
  SUPABASE_ANON_KEY    Supabase anon/service key
  MIRO_ACCESS_TOKEN    Miro access token
  MIRO_FLOW_BOARD_ID   Miro board ID for flows (preferred)
  MIRO_BOARD_ID        Fallback Miro board ID
""",
    )

    parser.add_argument(
        "--symbol",
        "-s",
        help="Stock symbol to visualize (default: auto-select most complete flow)",
    )
    parser.add_argument(
        "--hours",
        "-H",
        type=int,
        default=24,
        help="Hours to look back (default: 24)",
    )
    parser.add_argument(
        "--board-id",
        "-b",
        help="Miro board ID (default: MIRO_FLOW_BOARD_ID or MIRO_BOARD_ID env var)",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="Don't clear existing items on the board",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print flow data without updating Miro",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON (for dry-run)",
    )

    args = parser.parse_args()

    print("🔍 Fetching analysis flow from Supabase...")

    try:
        client = SupabaseClient()
        flow = client.get_complete_flow(symbol=args.symbol, hours=args.hours)

        if not flow:
            print("❌ No analysis flow found")
            print(f"   Looked back {args.hours} hours")
            if args.symbol:
                print(f"   Symbol filter: {args.symbol}")
            return

        print(f"✅ Found flow for {flow.symbol}")
        print(f"   Start: {flow.start_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print(f"   End:   {flow.end_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print(f"   Duration: {flow.duration_minutes:.1f} minutes")
        print(f"   Contexts: {len(flow.contexts)}")

        # Show context breakdown
        by_type: dict[str, int] = {}
        for ctx in flow.contexts:
            by_type[ctx.context_type] = by_type.get(ctx.context_type, 0) + 1

        print("\n   Context types:")
        for ctx_type, count in sorted(by_type.items()):
            print(f"     {ctx_type}: {count}")

        if args.dry_run:
            diagram = generate_flow_diagram(flow)
            if args.json:
                print("\n" + json.dumps(diagram, indent=2, default=str))
            else:
                print("\n📊 Generated diagram:")
                print(f"   Items: {len(diagram['items'])}")
                print(f"   Connectors: {len(diagram['connectors'])}")
            return

        print("\n🎨 Updating Miro board...")
        result = update_miro_flow_diagram(
            flow,
            board_id=args.board_id,
            clear_first=not args.no_clear,
        )

        print("\n✅ Miro board updated!")
        print(f"   Items created: {result['items_created']}")
        print(f"   Connectors created: {result['connectors_created']}")
        print(f"   Board URL: {result['board_url']}")

    except ValueError as e:
        print(f"❌ Configuration error: {e}")
        raise SystemExit(1)
    except MiroClientError as e:
        print(f"❌ Miro API error: {e}")
        raise SystemExit(1)
    except Exception as e:
        print(f"❌ Error: {e}")
        raise


if __name__ == "__main__":
    main()
