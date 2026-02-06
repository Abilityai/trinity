"""
SMARTS Daily Summary Service

Generates and sends daily trading summaries to Telegram.
Pulls data from Supabase integration_context table and Alpaca API.
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx

from credentials import CredentialManager

logger = logging.getLogger(__name__)

# Redis URL from environment
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")


class SmartsSummaryService:
    """
    Service for generating and sending SMARTS trading summaries.

    Pulls agent activity from Supabase integration_context and
    portfolio data from Alpaca, formats into readable Telegram messages.
    """

    def __init__(self):
        """Initialize the summary service."""
        import redis as redis_lib
        self._redis = redis_lib.from_url(REDIS_URL, decode_responses=True)
        self._supabase_url: Optional[str] = None
        self._supabase_key: Optional[str] = None
        self._telegram_token: Optional[str] = None
        self._telegram_chat_id: Optional[str] = None
        self._alpaca_api_key: Optional[str] = None
        self._alpaca_secret_key: Optional[str] = None

    async def _load_credentials(self) -> None:
        """Load credentials from Redis."""
        import json

        # Helper to get credential value from Redis
        # Credentials are stored as JSON with 'value' key at credential:{name}
        def get_cred_value(name: str) -> Optional[str]:
            try:
                raw = self._redis.get(f"credential:{name}")
                if raw:
                    data = json.loads(raw)
                    return data.get("value")
            except Exception as e:
                logger.warning(f"Failed to get credential {name}: {e}")
            return None

        # Supabase
        self._supabase_url = get_cred_value("SUPABASE_URL")
        self._supabase_key = get_cred_value("SUPABASE_ANON_KEY")
        if not self._supabase_key:
            self._supabase_key = get_cred_value("SUPABASE_SERVICE_KEY")

        # Telegram
        self._telegram_token = get_cred_value("TELEGRAM_BOT_TOKEN")
        self._telegram_chat_id = get_cred_value("TELEGRAM_CHAT_ID")

        # Alpaca
        self._alpaca_api_key = get_cred_value("ALPACA_API_KEY")
        self._alpaca_secret_key = get_cred_value("ALPACA_SECRET_KEY")

    async def _query_supabase(self, query: str) -> list[dict[str, Any]]:
        """Execute a query against Supabase via PostgREST."""
        if not self._supabase_url or not self._supabase_key:
            await self._load_credentials()

        if not self._supabase_url or not self._supabase_key:
            logger.error("Supabase credentials not configured")
            return []

        url = f"{self._supabase_url}/rest/v1/rpc/get_context_summary"

        async with httpx.AsyncClient() as client:
            try:
                # Use the helper function or direct query
                response = await client.get(
                    f"{self._supabase_url}/rest/v1/integration_context",
                    params={
                        "select": "*",
                        "order": "created_at.desc",
                        "limit": "100",
                        "created_at": f"gte.{(datetime.utcnow() - timedelta(hours=24)).isoformat()}Z"
                    },
                    headers={
                        "apikey": self._supabase_key,
                        "Authorization": f"Bearer {self._supabase_key}",
                    },
                    timeout=30.0,
                )

                if response.status_code == 200:
                    return response.json()
                else:
                    logger.error(f"Supabase query failed: {response.status_code} {response.text}")
                    return []
            except Exception as e:
                logger.exception(f"Failed to query Supabase: {e}")
                return []

    async def _get_alpaca_portfolio(self) -> dict[str, Any]:
        """Get portfolio data from Alpaca."""
        if not self._alpaca_api_key:
            await self._load_credentials()

        if not self._alpaca_api_key or not self._alpaca_secret_key:
            logger.error("Alpaca credentials not configured")
            return {}

        async with httpx.AsyncClient() as client:
            try:
                # Get account info
                account_resp = await client.get(
                    "https://paper-api.alpaca.markets/v2/account",
                    headers={
                        "APCA-API-KEY-ID": self._alpaca_api_key,
                        "APCA-API-SECRET-KEY": self._alpaca_secret_key,
                    },
                    timeout=30.0,
                )

                # Get positions
                positions_resp = await client.get(
                    "https://paper-api.alpaca.markets/v2/positions",
                    headers={
                        "APCA-API-KEY-ID": self._alpaca_api_key,
                        "APCA-API-SECRET-KEY": self._alpaca_secret_key,
                    },
                    timeout=30.0,
                )

                # Get today's orders
                orders_resp = await client.get(
                    "https://paper-api.alpaca.markets/v2/orders",
                    params={"status": "all", "limit": 50},
                    headers={
                        "APCA-API-KEY-ID": self._alpaca_api_key,
                        "APCA-API-SECRET-KEY": self._alpaca_secret_key,
                    },
                    timeout=30.0,
                )

                return {
                    "account": account_resp.json() if account_resp.status_code == 200 else {},
                    "positions": positions_resp.json() if positions_resp.status_code == 200 else [],
                    "orders": orders_resp.json() if orders_resp.status_code == 200 else [],
                }
            except Exception as e:
                logger.exception(f"Failed to get Alpaca data: {e}")
                return {}

    def _escape_markdown(self, text: str) -> str:
        """Escape special Markdown characters in text."""
        if not text:
            return ""
        # Characters that need escaping in Telegram Markdown
        special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in special_chars:
            text = text.replace(char, f'\\{char}')
        return text

    async def _send_telegram(self, message: str, parse_mode: str = "HTML") -> bool:
        """Send a message to Telegram using HTML formatting."""
        if not self._telegram_token:
            await self._load_credentials()

        if not self._telegram_token or not self._telegram_chat_id:
            logger.error("Telegram credentials not configured")
            return False

        api_url = f"https://api.telegram.org/bot{self._telegram_token}/sendMessage"

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    api_url,
                    json={
                        "chat_id": self._telegram_chat_id,
                        "text": message,
                        "parse_mode": parse_mode,
                    },
                    timeout=30.0,
                )

                result = response.json()
                if result.get("ok"):
                    logger.info("Telegram message sent successfully")
                    return True
                else:
                    # Try without parse_mode if HTML fails
                    logger.warning(f"Telegram HTML error: {result.get('description')}, retrying without formatting")
                    response = await client.post(
                        api_url,
                        json={
                            "chat_id": self._telegram_chat_id,
                            "text": message,
                        },
                        timeout=30.0,
                    )
                    result = response.json()
                    if result.get("ok"):
                        return True
                    logger.error(f"Telegram error: {result.get('description')}")
                    return False
            except Exception as e:
                logger.exception(f"Failed to send Telegram message: {e}")
                return False

    def _format_portfolio_summary(self, portfolio: dict[str, Any]) -> str:
        """Format portfolio data as Telegram message using HTML."""
        account = portfolio.get("account", {})
        positions = portfolio.get("positions", [])

        if not account:
            return "Portfolio data unavailable"

        portfolio_value = float(account.get("portfolio_value", 0))
        last_equity = float(account.get("last_equity", portfolio_value))
        daily_change = portfolio_value - last_equity
        daily_pct = (daily_change / last_equity * 100) if last_equity else 0

        cash = float(account.get("cash", 0))
        buying_power = float(account.get("buying_power", 0))

        # Format positions
        pos_lines = []
        for pos in sorted(positions, key=lambda p: abs(float(p.get("unrealized_pl", 0))), reverse=True)[:5]:
            symbol = pos.get("symbol", "???")
            qty = int(float(pos.get("qty", 0)))
            unrealized_pl = float(pos.get("unrealized_pl", 0))
            unrealized_plpc = float(pos.get("unrealized_plpc", 0)) * 100
            side = "Long" if qty > 0 else "Short"
            pl_emoji = "🟢" if unrealized_pl >= 0 else "🔴"
            pos_lines.append(f"  {pl_emoji} <b>{symbol}</b>: {side} {abs(qty)} | ${unrealized_pl:+,.2f} ({unrealized_plpc:+.1f}%)")

        change_emoji = "📈" if daily_change >= 0 else "📉"

        msg = f"""💰 <b>Portfolio</b>

<b>Value:</b> ${portfolio_value:,.2f} {change_emoji} ${daily_change:+,.2f} ({daily_pct:+.1f}%)
<b>Cash:</b> ${cash:,.2f}
<b>Buying Power:</b> ${buying_power:,.2f}
<b>Positions:</b> {len(positions)} open

<b>Top Positions:</b>
{chr(10).join(pos_lines) if pos_lines else '  No positions'}"""

        return msg

    def _format_agent_context(self, context: dict[str, Any]) -> str:
        """Format a single agent context entry with full reasoning using HTML."""
        context_type = context.get("context_type", "unknown")
        symbol = context.get("symbol", "")
        data = context.get("context_data", {})
        created_at = context.get("created_at", "")

        # Parse timestamp
        if created_at:
            try:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                time_str = dt.strftime("%H:%M UTC")
            except Exception:
                time_str = ""
        else:
            time_str = ""

        symbol_str = f" - {symbol}" if symbol else ""

        # Helper to escape HTML special chars
        def esc(text: Any) -> str:
            if text is None:
                return "N/A"
            s = str(text)
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        if context_type == "market_regime":
            regime = esc(data.get("market_regime", data.get("regime", "unknown"))).upper()
            vix = data.get("vix_level", "N/A")
            spy = data.get("spy_price", "N/A")
            description = esc(data.get("description", ""))
            warnings = data.get("warning_flags", [])
            warnings_str = "\n".join([f"  ⚠️ {esc(w)}" for w in warnings[:5]]) if warnings else "  None"

            return f"""🌍 <b>MARKET REGIME</b> ({time_str})

<b>Regime:</b> {regime}
<b>VIX:</b> {vix} | <b>SPY:</b> ${spy}

<b>Assessment:</b>
<i>{description}</i>

<b>Warning Flags:</b>
{warnings_str}"""

        elif context_type == "scanner_opportunity":
            score = data.get("score", "N/A")
            price = data.get("current_price", "N/A")
            rsi = data.get("rsi", "N/A")
            change = data.get("price_change", 0)
            rec = data.get("recommendation", {})
            setup = esc(rec.get("setup", "N/A"))
            rationale = esc(rec.get("rationale", ""))
            trade_idea = esc(rec.get("trade_idea", ""))
            signals = data.get("signals", [])
            signals_str = ", ".join([esc(s) for s in signals[:4]]) if signals else "None"

            return f"""🔍 <b>DISCOVERY</b>{symbol_str} ({time_str})

<b>Score:</b> {score}/100 | <b>Price:</b> ${price} ({change:+.2f}% today)
<b>RSI:</b> {rsi} | <b>Setup:</b> {setup}

<b>Signals:</b> {signals_str}

<b>Rationale:</b>
<i>{rationale}</i>

<b>Trade Idea:</b>
<i>{trade_idea}</i>"""

        elif context_type == "analysis":
            stance = esc(data.get("stance", "N/A"))
            confidence = esc(data.get("confidence_level", data.get("confidence", "N/A")))
            ev_pct = data.get("expected_value_pct", 0)
            price = data.get("current_price", "N/A")

            # Scenarios
            scenarios = data.get("scenarios", {})
            base = scenarios.get("base", {})
            optimistic = scenarios.get("optimistic", {})
            pessimistic = scenarios.get("pessimistic", {})

            # Recommendation
            rec = data.get("recommendation", {})
            action = esc(rec.get("action", "N/A"))
            entry = esc(rec.get("entry_price", "N/A"))
            stop = rec.get("stop_loss", "N/A")
            targets = rec.get("profit_targets", [])
            targets_str = ", ".join([f"${t}" for t in targets]) if targets else "N/A"

            # Catalysts and risks
            catalysts = data.get("key_catalysts", [])
            catalysts_str = "\n".join([f"  ✅ {esc(c)}" for c in catalysts[:3]]) if catalysts else "  None"
            risks = data.get("key_risks", [])
            risks_str = "\n".join([f"  ⚠️ {esc(r)}" for r in risks[:3]]) if risks else "  None"

            return f"""📊 <b>ANALYSIS</b>{symbol_str} ({time_str})

<b>Stance:</b> {stance} | <b>Confidence:</b> {confidence}
<b>Expected Value:</b> {ev_pct:.2f}% | <b>Price:</b> ${price}

<b>Scenarios:</b>
  📈 Optimistic: ${optimistic.get('price_target', 'N/A')} (+{optimistic.get('return_pct', 0):.1f}%) - {int(optimistic.get('probability', 0)*100)}%
  ➡️ Base: ${base.get('price_target', 'N/A')} (+{base.get('return_pct', 0):.1f}%) - {int(base.get('probability', 0)*100)}%
  📉 Pessimistic: ${pessimistic.get('price_target', 'N/A')} ({pessimistic.get('return_pct', 0):.1f}%) - {int(pessimistic.get('probability', 0)*100)}%

<b>Recommendation:</b> {action}
  Entry: {entry} | Stop: ${stop}
  Targets: {targets_str}

<b>Key Catalysts:</b>
{catalysts_str}

<b>Key Risks:</b>
{risks_str}"""

        elif context_type == "decision":
            decision = data.get("decision", {})
            action = esc(decision.get("action", data.get("action", "N/A")))
            confidence = esc(decision.get("confidence", "N/A"))
            urgency = esc(decision.get("urgency", "N/A"))
            rationale = esc(decision.get("rationale", ""))[:400]

            # Current position
            current = data.get("current_situation", {})
            existing_pos = esc(current.get("existing_position", "NONE"))
            current_pnl = current.get("current_pnl", 0)
            current_pnl_pct = current.get("current_pnl_pct", 0)

            # Execution plan
            plan = data.get("execution_plan", {})
            step1 = plan.get("step_1", {})
            step2 = plan.get("step_2", {})

            # Expected outcomes
            outcomes = data.get("expected_outcomes", {})
            combined_ev = outcomes.get("combined_expected_value", 0)

            # Risk management
            risk = data.get("risk_management", {})
            stop_loss = risk.get("stop_loss", "N/A")
            max_loss = risk.get("max_position_loss", 0)

            return f"""🎯 <b>DECISION</b>{symbol_str} ({time_str})

<b>Action:</b> {action}
<b>Confidence:</b> {confidence} | <b>Urgency:</b> {urgency}

<b>Current Position:</b> {existing_pos}
  P&L: ${current_pnl:+,.2f} ({current_pnl_pct:+.2f}%)

<b>Execution Plan:</b>
  Step 1: {esc(step1.get('action', 'N/A'))} - {step1.get('quantity', 'N/A')} shares ({esc(step1.get('order_type', 'N/A'))})
  Step 2: {esc(step2.get('action', 'N/A'))} - {step2.get('estimated_shares', 'N/A')} shares @ ${step2.get('limit_price', 'N/A')}

<b>Expected Value:</b> ${combined_ev:,.2f}
<b>Stop Loss:</b> ${stop_loss} (Max Loss: ${max_loss:,.2f})

<b>Rationale:</b>
<i>{rationale}</i>"""

        elif context_type == "execution":
            status = esc(data.get("execution_status", data.get("status", "N/A")))
            blocking_reason = esc(data.get("blocking_reason", "None"))

            # Decision details
            decision_details = data.get("decision_details", {})
            action = esc(decision_details.get("action", "N/A"))

            # Blocked steps
            blocked = data.get("blocked_steps", [])
            blocked_str = ""
            for step in blocked[:2]:
                step_action = esc(step.get("action", "N/A"))
                step_status = esc(step.get("status", "N/A"))
                step_reason = esc(step.get("reason", ""))
                blocked_str += f"\n  • {step_action}: {step_status}\n    <i>{step_reason}</i>"

            # PM directive
            pm_status = esc(data.get("pm_directive_status", "N/A"))
            pm_details = data.get("pm_directive_details", {})
            restrictions = pm_details.get("restrictions", [])
            restrictions_str = ", ".join([esc(r) for r in restrictions]) if restrictions else "None"

            # Financial impact
            impact = data.get("financial_impact", {})
            opportunity_cost = impact.get("opportunity_cost", 0)

            compliance = esc(data.get("compliance_note", ""))

            return f"""⚡ <b>EXECUTION</b>{symbol_str} ({time_str})

<b>Status:</b> {status}
<b>Intended Action:</b> {action}
<b>Blocking Reason:</b> {blocking_reason}

<b>Blocked Steps:</b>{blocked_str if blocked_str else " None"}

<b>PM Directive:</b> {pm_status}
  Restrictions: {restrictions_str}

<b>Financial Impact:</b>
  Opportunity Cost: ${opportunity_cost:,.2f}

<b>Compliance:</b>
<i>{compliance}</i>"""

        elif context_type == "pm_directive":
            status = esc(data.get("status", "N/A"))
            expires = esc(data.get("expires_at", "N/A"))

            # Restrictions
            restrictions = data.get("restrictions", [])
            restrictions_str = ""
            for r in restrictions[:3]:
                r_type = esc(r.get("type", "N/A"))
                r_reason = esc(r.get("reason", ""))
                affected = r.get("affected_symbols", [])
                affected_str = ", ".join(affected[:5]) if affected else ""
                restrictions_str += f"\n  🚫 {r_type}\n    <i>{r_reason}</i>"
                if affected_str:
                    restrictions_str += f"\n    Symbols: {affected_str}"

            # Risk assessment
            risk = data.get("risk_assessment", {})
            leverage = risk.get("leverage", 0)
            portfolio_value = risk.get("portfolio_value", 0)

            # Breaches
            breaches = risk.get("breaches", [])
            breach_str = ""
            for b in breaches[:2]:
                b_type = esc(b.get("type", "N/A"))
                b_severity = esc(b.get("severity", "N/A"))
                positions = b.get("positions", [])
                if positions:
                    pos_str = ", ".join([f"{p['symbol']} ({p['concentration']*100:.1f}%)" for p in positions[:4]])
                    breach_str += f"\n  ⚠️ {b_type} ({b_severity}): {pos_str}"

            # Warnings
            warnings = risk.get("warnings", [])
            warnings_str = ""
            for w in warnings[:3]:
                w_type = esc(w.get("type", "N/A"))
                w_severity = esc(w.get("severity", "N/A"))
                warnings_str += f"\n  ⚠️ {w_type} ({w_severity})"

            return f"""🚨 <b>PM DIRECTIVE</b> ({time_str})

<b>Status:</b> {status}
<b>Expires:</b> {expires}
<b>Portfolio:</b> ${portfolio_value:,.2f} | <b>Leverage:</b> {leverage:.2f}x

<b>Restrictions:</b>{restrictions_str if restrictions_str else " None"}

<b>Risk Breaches:</b>{breach_str if breach_str else " None"}

<b>Warnings:</b>{warnings_str if warnings_str else " None"}"""

        elif context_type == "news_sentiment":
            sentiment = esc(data.get("sentiment", "N/A"))
            direction = esc(data.get("direction", "N/A"))
            score = data.get("sentiment_score", 0)
            theme = esc(data.get("theme", ""))

            # Key factors
            factors = data.get("key_factors", [])
            factors_str = "\n".join([f"  • {esc(f)}" for f in factors[:4]]) if factors else "  None"

            # Catalysts and risks
            catalysts = data.get("catalysts", [])
            catalysts_str = ", ".join([esc(c) for c in catalysts[:3]]) if catalysts else "None"
            risks = data.get("risks", [])
            risks_str = ", ".join([esc(r) for r in risks[:3]]) if risks else "None"

            # Recent news
            news = data.get("recent_news", [])
            news_str = "\n".join([f"  📰 {esc(n)}" for n in news[:3]]) if news else "  None"

            return f"""📰 <b>NEWS SENTIMENT</b>{symbol_str} ({time_str})

<b>Sentiment:</b> {sentiment} ({direction}) | <b>Score:</b> {score:.2f}
<b>Theme:</b> {theme}

<b>Key Factors:</b>
{factors_str}

<b>Catalysts:</b> {catalysts_str}
<b>Risks:</b> {risks_str}

<b>Recent Headlines:</b>
{news_str}"""

        elif context_type == "scanner_summary":
            results = data.get("scan_results", [])
            top = data.get("top_opportunity", {})
            regime = esc(data.get("market_regime", "N/A"))
            regime_desc = esc(data.get("regime_description", ""))

            results_str = ""
            for r in results[:5]:
                ticker = r.get("ticker", "N/A")
                score = r.get("score", 0)
                price = r.get("price", 0)
                change = r.get("change_pct", 0)
                level = r.get("opportunity_level", "N/A")
                emoji = "🔥" if level == "high" else "📊" if level == "moderate" else "📉"
                results_str += f"\n  {emoji} <b>{ticker}</b>: Score {score} | ${price:.2f} ({change:+.2f}%)"

            return f"""🔎 <b>SCANNER SUMMARY</b> ({time_str})

<b>Market Regime:</b> {regime}
<i>{regime_desc}</i>

<b>Top Opportunity:</b> {esc(top.get('ticker', 'N/A'))} (Score: {top.get('score', 'N/A')})
<i>{esc(top.get('reason', ''))}</i>

<b>Scan Results:</b>{results_str}"""

        elif context_type == "feedback_metrics":
            summary = data.get("summary", {})
            win_rate = summary.get("win_rate", 0)
            pnl = summary.get("total_pnl_dollars", 0)
            trades = summary.get("trades_closed", 0)

            return f"""📈 <b>FEEDBACK METRICS</b> ({time_str})

<b>Win Rate:</b> {win_rate*100:.1f}%
<b>Total P&L:</b> ${pnl:,.2f}
<b>Trades Closed:</b> {trades}"""

        else:
            # Generic fallback
            keys = list(data.keys())[:5]
            preview = ", ".join(keys)
            return f"""📋 <b>{context_type.upper()}</b>{symbol_str} ({time_str})

Keys: {preview}"""

    def _get_sent_context_ids(self) -> set[str]:
        """Get the set of context IDs that have already been sent."""
        try:
            sent_raw = self._redis.smembers("smarts:sent_context_ids")
            return set(sent_raw) if sent_raw else set()
        except Exception as e:
            logger.warning(f"Failed to get sent context IDs: {e}")
            return set()

    def _mark_context_sent(self, context_id: str) -> None:
        """Mark a context ID as sent. Expires after 24 hours."""
        try:
            self._redis.sadd("smarts:sent_context_ids", context_id)
            # Set expiry on the set (24 hours)
            self._redis.expire("smarts:sent_context_ids", 86400)
        except Exception as e:
            logger.warning(f"Failed to mark context as sent: {e}")

    def _clear_sent_contexts(self) -> None:
        """Clear all sent context tracking (for fresh daily summary)."""
        try:
            self._redis.delete("smarts:sent_context_ids")
        except Exception as e:
            logger.warning(f"Failed to clear sent contexts: {e}")

    async def generate_and_send_summary(self, force_all: bool = False) -> dict[str, Any]:
        """
        Generate and send the full daily summary to Telegram.

        Args:
            force_all: If True, send all contexts regardless of whether they were sent before.
                       If False (default), only send new contexts not previously sent.

        Returns status information about what was sent.
        """
        await self._load_credentials()

        results = {
            "success": False,
            "messages_sent": 0,
            "contexts_skipped": 0,
            "errors": [],
        }

        now = datetime.utcnow()
        date_str = now.strftime("%b %d, %Y")

        # Get already-sent context IDs (for deduplication)
        sent_ids = set() if force_all else self._get_sent_context_ids()

        # 1. Send header
        header = f"""📊 <b>SMARTS Daily Summary</b>
{date_str}
"""
        if not await self._send_telegram(header):
            results["errors"].append("Failed to send header")
        else:
            results["messages_sent"] += 1

        # 2. Get and send portfolio summary (always send - it's live data)
        portfolio = await self._get_alpaca_portfolio()
        if portfolio:
            portfolio_msg = self._format_portfolio_summary(portfolio)
            if not await self._send_telegram(portfolio_msg):
                results["errors"].append("Failed to send portfolio summary")
            else:
                results["messages_sent"] += 1

        # 3. Get agent contexts from Supabase
        contexts = await self._query_supabase("")

        if not contexts:
            await self._send_telegram("<i>No agent activity in the last 24 hours</i>")
            results["messages_sent"] += 1
        else:
            # Filter out already-sent contexts
            new_contexts = []
            for ctx in contexts:
                ctx_id = ctx.get("id", "")
                if ctx_id and ctx_id in sent_ids:
                    results["contexts_skipped"] += 1
                    continue
                new_contexts.append(ctx)

            if not new_contexts:
                await self._send_telegram("<i>No new agent activity since last summary</i>")
                results["messages_sent"] += 1
            else:
                # Group by context type
                by_type: dict[str, list[dict[str, Any]]] = {}
                for ctx in new_contexts:
                    ctx_type = ctx.get("context_type", "other")
                    if ctx_type not in by_type:
                        by_type[ctx_type] = []
                    by_type[ctx_type].append(ctx)

                # Define order - all agent context types in pipeline order
                type_order = [
                    "market_regime",      # Market Regime Agent
                    "news_sentiment",     # News Sentiment Agent
                    "scanner_summary",    # Discovery Agent summary
                    "scanner_opportunity",# Discovery Agent opportunities
                    "analysis",           # Analysis Agent
                    "decision",           # Decision Agent
                    "execution",          # Execution Agent
                    "pm_directive",       # Portfolio Manager Agent
                    "feedback_metrics",   # Feedback Agent
                ]

                # Send each type's contexts
                for ctx_type in type_order:
                    if ctx_type not in by_type:
                        continue

                    type_contexts = by_type[ctx_type][:5]  # Limit to 5 per type

                    for ctx in type_contexts:
                        ctx_id = ctx.get("id", "")
                        msg = self._format_agent_context(ctx)
                        if msg:
                            # Telegram has 4096 char limit
                            if len(msg) > 4000:
                                msg = msg[:4000] + "..."

                            if not await self._send_telegram(msg):
                                results["errors"].append(f"Failed to send {ctx_type}")
                            else:
                                results["messages_sent"] += 1
                                # Mark as sent to avoid duplicates
                                if ctx_id:
                                    self._mark_context_sent(ctx_id)

        # 4. Send footer
        orders = portfolio.get("orders", [])
        today_orders = [o for o in orders if o.get("submitted_at", "").startswith(now.strftime("%Y-%m-%d"))]
        filled_orders = [o for o in today_orders if o.get("status") == "filled"]

        new_count = len(new_contexts) if contexts and 'new_contexts' in locals() else 0
        skipped = results.get("contexts_skipped", 0)

        footer = f"""📈 <b>Activity Stats</b>

Orders Today: {len(today_orders)} ({len(filled_orders)} filled)
Contexts: {new_count} new, {skipped} already sent

<i>Generated at {now.strftime('%H:%M:%S')} UTC</i>"""

        if not await self._send_telegram(footer):
            results["errors"].append("Failed to send footer")
        else:
            results["messages_sent"] += 1

        results["success"] = len(results["errors"]) == 0
        return results


# Global instance
_summary_service: Optional[SmartsSummaryService] = None


def get_summary_service() -> SmartsSummaryService:
    """Get the global summary service instance."""
    global _summary_service
    if _summary_service is None:
        _summary_service = SmartsSummaryService()
    return _summary_service


# Scheduler for daily summaries
_scheduler: Optional["AsyncIOScheduler"] = None


def start_summary_scheduler() -> None:
    """
    Start the daily summary scheduler.

    Sends SMARTS summary at 21:30 UTC (4:30 PM EST / 5:30 PM EDT).
    This is after market close (4:00 PM ET).
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    global _scheduler

    if _scheduler is not None:
        logger.warning("Summary scheduler already running")
        return

    _scheduler = AsyncIOScheduler()

    async def send_daily_summary():
        """Scheduled task to send daily summary."""
        logger.info("Starting scheduled daily SMARTS summary")
        try:
            service = get_summary_service()
            result = await service.generate_and_send_summary()
            logger.info(f"Daily summary sent: {result['messages_sent']} messages, errors: {result['errors']}")
        except Exception as e:
            logger.exception(f"Failed to send daily summary: {e}")

    # Schedule for 21:30 UTC (4:30 PM EST after market close)
    _scheduler.add_job(
        send_daily_summary,
        CronTrigger(hour=21, minute=30),
        id="smarts_daily_summary",
        name="SMARTS Daily Summary",
        replace_existing=True,
        misfire_grace_time=3600,  # 1 hour grace period
    )

    _scheduler.start()
    logger.info("SMARTS summary scheduler started: Daily at 21:30 UTC (4:30 PM EST)")


def stop_summary_scheduler() -> None:
    """Stop the daily summary scheduler."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("SMARTS summary scheduler stopped")
