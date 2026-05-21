"""
Cerebro Quant v5 — Trading Engine
Five-IA committee vote system with Alpaca paper trading execution.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiofiles
import aiohttp
import numpy as np

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Lazy sklearn imports (only used inside sync functions run in executor)
# ──────────────────────────────────────────────────────────────────────────────

def _fit_and_predict_linear(X, y, x_new):
    """Train LinearRegression and return prediction for x_new."""
    from sklearn.linear_model import LinearRegression
    model = LinearRegression()
    model.fit(X, y)
    return float(model.predict([x_new])[0])


def _fit_and_predict_ridge(X, y, x_new):
    """Train Ridge regression and return prediction for x_new."""
    from sklearn.linear_model import Ridge
    model = Ridge(alpha=1.0)
    model.fit(X, y)
    return float(model.predict([x_new])[0])


def _fit_and_predict_lasso(X, y, x_new):
    """Train Lasso regression and return prediction for x_new."""
    from sklearn.linear_model import Lasso
    model = Lasso(alpha=0.1, max_iter=2000)
    model.fit(X, y)
    return float(model.predict([x_new])[0])


class TradingEngine:
    """
    Committee-based trading engine:
    - 5 IAs vote on each trade (signal in [-1, +1])
    - Consensus must exceed min_confidence before executing
    - All orders go through VetoEngine before execution
    - Losses are recorded with ForenseLogger
    """

    # Features: [rsi, macd, volume_ratio, price_change_1d, price_change_5d]
    FEATURE_NAMES = ["rsi", "macd", "volume_ratio", "price_change_1d", "price_change_5d"]

    # Synthetic training set (30 labelled observations) used when no historical data exists.
    # Labels are signals in [-1, +1].
    _SYNTHETIC_X = [
        [30.0, -0.5, 0.8, -1.0, -2.0],
        [28.0, -0.8, 0.6, -1.5, -3.0],
        [25.0, -1.2, 0.5, -2.0, -4.0],
        [32.0, -0.3, 0.9, -0.5, -1.0],
        [40.0,  0.1, 1.0,  0.2,  0.5],
        [50.0,  0.0, 1.0,  0.0,  0.0],
        [55.0,  0.2, 1.1,  0.5,  1.0],
        [60.0,  0.5, 1.2,  1.0,  2.0],
        [65.0,  0.8, 1.3,  1.5,  3.0],
        [70.0,  1.0, 1.5,  2.0,  4.0],
        [75.0,  1.2, 1.6,  2.5,  5.0],
        [80.0,  1.5, 1.8,  3.0,  6.0],
        [85.0,  2.0, 2.0,  3.5,  7.0],
        [20.0, -2.0, 0.4, -3.0, -5.0],
        [22.0, -1.8, 0.5, -2.5, -4.5],
        [45.0,  0.0, 0.9,  0.1,  0.2],
        [48.0,  0.1, 1.0,  0.3,  0.6],
        [52.0,  0.2, 1.1,  0.4,  0.8],
        [58.0,  0.6, 1.3,  1.2,  2.4],
        [62.0,  0.9, 1.4,  1.8,  3.5],
        [67.0,  1.1, 1.5,  2.2,  4.2],
        [72.0,  1.3, 1.7,  2.7,  5.3],
        [77.0,  1.6, 1.9,  3.2,  6.2],
        [82.0,  1.8, 2.1,  3.7,  7.2],
        [35.0, -0.1, 0.9, -0.1, -0.2],
        [38.0,  0.0, 1.0,  0.0,  0.1],
        [42.0,  0.1, 1.0,  0.2,  0.4],
        [46.0,  0.1, 1.0,  0.3,  0.6],
        [56.0,  0.3, 1.2,  0.7,  1.4],
        [63.0,  0.7, 1.3,  1.4,  2.8],
    ]

    _SYNTHETIC_Y = [
        -0.8, -0.9, -1.0, -0.6, -0.1,
         0.0,  0.2,  0.4,  0.6,  0.7,
         0.8,  0.9,  1.0, -1.0, -0.9,
         0.0,  0.1,  0.1,  0.5,  0.7,
         0.8,  0.9,  0.9,  1.0, -0.1,
         0.0,  0.1,  0.1,  0.3,  0.6,
    ]

    def __init__(self, config: dict, veto_engine, price_feed, forense) -> None:
        self.config = config
        self.veto_engine = veto_engine
        self.price_feed = price_feed
        self.forense = forense

        tp = config.get("trading_params", {})
        # Support both field name variants
        self.min_confidence: float = float(
            tp.get("min_confidence",
            tp.get("min_confidence_execution",
            tp.get("min_confidence_signal", 60.0)))
        )
        self.order_qty: int = int(tp.get("order_qty", 1))

        alpaca = config.get("alpaca", {})
        self.alpaca_key: str = alpaca.get("api_key", "")
        self.alpaca_secret: str = alpaca.get("secret_key", "")
        self.alpaca_base_url: str = alpaca.get(
            "base_url", "https://paper-api.alpaca.markets"
        ).rstrip("/")

        groq_cfg = config.get("groq", {})
        self.groq_api_key: str = groq_cfg.get("api_key", "")
        self.groq_model: str = groq_cfg.get("model", "llama-3.3-70b-versatile")

        base_dir = Path(config.get("base_dir", "."))
        brain_dir = base_dir / "brain"
        brain_dir.mkdir(parents=True, exist_ok=True)
        self.cardinal_state_path = brain_dir / "cardinal_state.json"

        self._http_session: Optional[aiohttp.ClientSession] = None
        self._history: dict = {}  # symbol → list of price observations

    # ──────────────────────────────────────────────────────────────────────────
    # HTTP session helpers
    # ──────────────────────────────────────────────────────────────────────────

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self._http_session = aiohttp.ClientSession(timeout=timeout)
        return self._http_session

    # ──────────────────────────────────────────────────────────────────────────
    # Feature helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _compute_features(self, symbol: str, price: float) -> list:
        """
        Compute [rsi, macd, volume_ratio, price_change_1d, price_change_5d]
        from the in-memory price history for the symbol.
        Returns a list of floats using safe defaults when history is short.
        """
        history = self._history.get(symbol, [])
        history.append(price)
        if len(history) > 30:
            history = history[-30:]
        self._history[symbol] = history

        n = len(history)

        # RSI (14-period approximation)
        if n >= 2:
            gains = [max(0.0, history[i] - history[i - 1]) for i in range(1, n)]
            losses = [max(0.0, history[i - 1] - history[i]) for i in range(1, n)]
            avg_gain = sum(gains[-14:]) / min(len(gains), 14)
            avg_loss = sum(losses[-14:]) / min(len(losses), 14)
            if avg_loss == 0:
                rsi = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi = 100.0 - (100.0 / (1.0 + rs))
        else:
            rsi = 50.0

        # MACD approximation (EMA12 - EMA26)
        def ema(data, period):
            if not data:
                return 0.0
            k = 2.0 / (period + 1)
            e = data[0]
            for v in data[1:]:
                e = v * k + e * (1 - k)
            return e

        if n >= 2:
            ema12 = ema(history, 12)
            ema26 = ema(history, 26)
            macd = ema12 - ema26
        else:
            macd = 0.0

        # Volume ratio (placeholder — use 1.0 when unavailable)
        volume_ratio = 1.0

        # Price changes
        price_change_1d = ((history[-1] - history[-2]) / history[-2] * 100.0) if n >= 2 else 0.0
        price_change_5d = ((history[-1] - history[-6]) / history[-6] * 100.0) if n >= 6 else 0.0

        return [rsi, macd, volume_ratio, price_change_1d, price_change_5d]

    # ──────────────────────────────────────────────────────────────────────────
    # IA 1 — Linear Regression
    # ──────────────────────────────────────────────────────────────────────────

    def ia1_linear_regression(self, features: list) -> float:
        """
        sklearn LinearRegression trained on synthetic data.
        Returns signal clamped to [-1, +1].
        """
        try:
            loop = asyncio.get_event_loop()
            signal = _fit_and_predict_linear(
                self._SYNTHETIC_X, self._SYNTHETIC_Y, features
            )
            return float(max(-1.0, min(1.0, signal)))
        except Exception as exc:
            logger.error("IA1 LinearRegression error: %s", exc)
            return 0.0

    # ──────────────────────────────────────────────────────────────────────────
    # IA 2 — Ridge
    # ──────────────────────────────────────────────────────────────────────────

    def ia2_ridge(self, features: list) -> float:
        """sklearn Ridge(alpha=1.0) signal, clamped to [-1, +1]."""
        try:
            signal = _fit_and_predict_ridge(
                self._SYNTHETIC_X, self._SYNTHETIC_Y, features
            )
            return float(max(-1.0, min(1.0, signal)))
        except Exception as exc:
            logger.error("IA2 Ridge error: %s", exc)
            return 0.0

    # ──────────────────────────────────────────────────────────────────────────
    # IA 3 — Lasso
    # ──────────────────────────────────────────────────────────────────────────

    def ia3_lasso(self, features: list) -> float:
        """sklearn Lasso(alpha=0.1) signal, clamped to [-1, +1]."""
        try:
            signal = _fit_and_predict_lasso(
                self._SYNTHETIC_X, self._SYNTHETIC_Y, features
            )
            return float(max(-1.0, min(1.0, signal)))
        except Exception as exc:
            logger.error("IA3 Lasso error: %s", exc)
            return 0.0

    # ──────────────────────────────────────────────────────────────────────────
    # IA 4 — Cardinal (accuracy-gated moving-average)
    # ──────────────────────────────────────────────────────────────────────────

    async def _load_cardinal_state(self) -> dict:
        """Load cardinal accuracy state from JSON file."""
        try:
            if not self.cardinal_state_path.exists():
                return {}
            async with aiofiles.open(self.cardinal_state_path, "r", encoding="utf-8") as fh:
                content = await fh.read()
            if not content.strip():
                return {}
            return json.loads(content)
        except Exception as exc:
            logger.error("Cardinal state load error: %s", exc)
            return {}

    async def _save_cardinal_state(self, state: dict) -> None:
        """Save cardinal accuracy state to JSON file."""
        try:
            async with aiofiles.open(self.cardinal_state_path, "w", encoding="utf-8") as fh:
                await fh.write(json.dumps(state, indent=2, ensure_ascii=False))
        except Exception as exc:
            logger.error("Cardinal state save error: %s", exc)

    async def ia4_cardinal(self, features: list, symbol: str) -> tuple:
        """
        Cardinal IA: tracks accuracy per symbol.
        - If accuracy < 55% AND trades_count > 10: returns (0.0, False) — learning mode
        - Otherwise: computes a weighted moving average signal from features

        Returns (signal: float, is_active: bool)
        """
        try:
            state = await self._load_cardinal_state()
            sym_state = state.get(symbol, {"accuracy": 100.0, "trades_count": 0})

            accuracy = float(sym_state.get("accuracy", 100.0))
            trades_count = int(sym_state.get("trades_count", 0))

            if trades_count > 10 and accuracy < 55.0:
                logger.info(
                    "Cardinal [%s]: APRENDIENDO (accuracy=%.1f%%, trades=%d)",
                    symbol, accuracy, trades_count,
                )
                return 0.0, False

            # Simple weighted signal: RSI-based with MACD confirmation
            rsi = features[0]       # 0-100
            macd = features[1]      # ~[-5, +5]

            # Normalise RSI to [-1, +1]: RSI < 50 → negative, > 50 → positive
            rsi_signal = (rsi - 50.0) / 50.0
            # Normalise MACD
            macd_signal = max(-1.0, min(1.0, macd / 2.0))

            # Weighted combination
            signal = 0.6 * rsi_signal + 0.4 * macd_signal
            signal = max(-1.0, min(1.0, signal))

            return signal, True

        except Exception as exc:
            logger.error("IA4 Cardinal error for %s: %s", symbol, exc)
            return 0.0, False

    # ──────────────────────────────────────────────────────────────────────────
    # IA 5 — Groq LLM
    # ──────────────────────────────────────────────────────────────────────────

    async def ia5_groq(self, symbol: str, price: float, context_text: str) -> float:
        """
        Query Groq API (llama-3.3-70b-versatile) for a quant signal.
        Returns float in [-1, +1]. Returns 0.0 on any error.
        """
        if not self.groq_api_key:
            logger.debug("IA5 Groq: no API key configured, returning 0.0")
            return 0.0

        try:
            prompt = (
                f"Eres un analista quant. "
                f"Activo: {symbol}, Precio: {price:.4f}. "
                f"Contexto: {context_text}. "
                f"Responde SOLO con un número de -1.0 (venta fuerte) a +1.0 (compra fuerte)."
            )

            payload = {
                "model": self.groq_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 16,
                "temperature": 0.1,
            }
            headers = {
                "Authorization": f"Bearer {self.groq_api_key}",
                "Content-Type": "application/json",
            }

            session = await self._get_http_session()
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    logger.warning("Groq HTTP %s for %s", resp.status, symbol)
                    return 0.0
                data = await resp.json()

            content = data["choices"][0]["message"]["content"].strip()
            # Extract the first float-looking token
            import re
            matches = re.findall(r"-?\d+\.?\d*", content)
            if not matches:
                logger.warning("Groq: could not parse float from: '%s'", content)
                return 0.0
            signal = float(matches[0])
            signal = max(-1.0, min(1.0, signal))
            logger.info("IA5 Groq [%s]: signal=%.3f (raw='%s')", symbol, signal, content)
            return signal

        except (aiohttp.ClientError, KeyError, ValueError, IndexError) as exc:
            logger.error("IA5 Groq error for %s: %s", symbol, exc)
            return 0.0
        except Exception as exc:
            logger.error("IA5 Groq unexpected error for %s: %s", symbol, exc)
            return 0.0

    # ──────────────────────────────────────────────────────────────────────────
    # Committee vote
    # ──────────────────────────────────────────────────────────────────────────

    async def get_committee_vote(
        self,
        symbol: str,
        features: list,
        price: float,
        context_text: str,
    ) -> dict:
        """
        Run all 5 IAs, collect signals, compute consensus.

        Returns:
            {
                "signal": float,          # mean of active IA signals
                "confidence": float,      # abs(signal) * 100
                "votes": dict,            # {ia_name: signal}
                "cardinal_active": bool,
                "direction": "BUY"|"SELL"|"HOLD"
            }
        """
        try:
            # Run sync IAs synchronously (they use sklearn which releases GIL minimally)
            ia1_sig = self.ia1_linear_regression(features)
            ia2_sig = self.ia2_ridge(features)
            ia3_sig = self.ia3_lasso(features)

            # Cardinal and Groq are async
            ia4_sig, cardinal_active = await self.ia4_cardinal(features, symbol)
            ia5_sig = await self.ia5_groq(symbol, price, context_text)

            votes = {
                "IA1_LinearReg": ia1_sig,
                "IA2_Ridge": ia2_sig,
                "IA3_Lasso": ia3_sig,
                "IA4_Cardinal": ia4_sig if cardinal_active else None,
                "IA5_Groq": ia5_sig,
            }

            # Only count active IAs in consensus
            active_signals = [v for v in votes.values() if v is not None]

            if not active_signals:
                return {
                    "signal": 0.0,
                    "confidence": 0.0,
                    "votes": votes,
                    "cardinal_active": cardinal_active,
                    "direction": "HOLD",
                }

            consensus = sum(active_signals) / len(active_signals)
            confidence = abs(consensus) * 100.0

            if abs(consensus) < 0.3 or confidence < self.min_confidence:
                direction = "HOLD"
            elif consensus >= 0.3:
                direction = "BUY"
            else:
                direction = "SELL"

            logger.info(
                "Committee [%s]: signal=%.3f confidence=%.1f dir=%s votes=%s",
                symbol,
                consensus,
                confidence,
                direction,
                {k: f"{v:.3f}" if v is not None else "INACTIVE" for k, v in votes.items()},
            )

            return {
                "signal": round(consensus, 4),
                "confidence": round(confidence, 2),
                "votes": votes,
                "cardinal_active": cardinal_active,
                "direction": direction,
            }

        except Exception as exc:
            logger.error("get_committee_vote error for %s: %s", symbol, exc)
            return {
                "signal": 0.0,
                "confidence": 0.0,
                "votes": {},
                "cardinal_active": False,
                "direction": "HOLD",
            }

    # ──────────────────────────────────────────────────────────────────────────
    # Alpaca order execution
    # ──────────────────────────────────────────────────────────────────────────

    async def execute_order(self, symbol: str, side: str, qty: int) -> Optional[dict]:
        """
        Submit a market order to Alpaca paper trading API.
        Returns the order dict on success, None on error.
        """
        if not self.alpaca_key or not self.alpaca_secret:
            logger.error("execute_order: Alpaca credentials not configured")
            return None

        try:
            url = f"{self.alpaca_base_url}/v2/orders"
            headers = {
                "APCA-API-KEY-ID": self.alpaca_key,
                "APCA-API-SECRET-KEY": self.alpaca_secret,
                "Content-Type": "application/json",
            }
            payload = {
                "symbol": symbol,
                "qty": str(qty),
                "side": side.lower(),
                "type": "market",
                "time_in_force": "day",
            }

            session = await self._get_http_session()
            async with session.post(url, json=payload, headers=headers) as resp:
                response_data = await resp.json()
                if resp.status not in (200, 201):
                    logger.error(
                        "Alpaca order failed [%s/%s/%d]: HTTP %s — %s",
                        symbol,
                        side,
                        qty,
                        resp.status,
                        response_data,
                    )
                    return None

                order_id = response_data.get("id", "?")
                logger.info(
                    "Alpaca order submitted: %s %s x%d → order_id=%s",
                    side,
                    symbol,
                    qty,
                    order_id,
                )
                return response_data

        except aiohttp.ClientError as exc:
            logger.error("execute_order network error for %s: %s", symbol, exc)
            return None
        except Exception as exc:
            logger.error("execute_order unexpected error for %s: %s", symbol, exc)
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # Main scan-and-trade loop
    # ──────────────────────────────────────────────────────────────────────────

    async def scan_and_trade(self, symbols: list) -> None:
        """
        Continuous trading loop:
        1. Fetch prices for all symbols
        2. Compute features and committee vote
        3. If confidence >= min_confidence: run veto engine
        4. If authorised: execute order
        5. On filled loss: register with ForenseLogger
        """
        logger.info("TradingEngine: scan_and_trade started for %s", symbols)

        # Track last known prices for P&L estimation
        last_prices: dict = {}
        # Track open orders per symbol: {symbol: {side, entry_price, order}}
        open_trades: dict = {}

        while True:
            try:
                # ── Fetch prices ─────────────────────────────────────────────
                try:
                    prices = await self.price_feed.get_prices_bulk(symbols)
                except Exception as price_exc:
                    logger.error("scan_and_trade: price fetch error: %s", price_exc)
                    prices = {}

                for symbol in symbols:
                    try:
                        price = prices.get(symbol)
                        if price is None:
                            logger.warning("scan_and_trade: no price for %s, skipping", symbol)
                            continue

                        # ── Compute features ─────────────────────────────────
                        features = self._compute_features(symbol, price)
                        rsi = features[0]
                        macd_val = features[1]

                        context_text = (
                            f"RSI={rsi:.1f}, MACD={macd_val:.3f}, "
                            f"Precio={price:.4f}, "
                            f"Cambio1d={features[3]:.2f}%, Cambio5d={features[4]:.2f}%"
                        )

                        # ── Committee vote ────────────────────────────────────
                        vote = await self.get_committee_vote(
                            symbol, features, price, context_text
                        )
                        direction = vote["direction"]
                        confidence = vote["confidence"]

                        if direction == "HOLD" or confidence < self.min_confidence:
                            logger.debug(
                                "scan_and_trade [%s]: HOLD (confidence=%.1f)", symbol, confidence
                            )
                            continue

                        # ── Market context for veto ───────────────────────────
                        market_context = {
                            "volatilidad": abs(features[3]),  # use 1d change as vol proxy
                            "sentimiento": vote["signal"],
                            "rsi": rsi,
                            "macd": macd_val,
                        }

                        # ── Veto check ────────────────────────────────────────
                        authorized, veto_reason = await self.veto_engine.check_all(
                            symbol, market_context, side=direction
                        )

                        if not authorized:
                            logger.info(
                                "scan_and_trade [%s]: VETADO — %s", symbol, veto_reason
                            )
                            continue

                        # ── Execute order ─────────────────────────────────────
                        order = await self.execute_order(symbol, direction, self.order_qty)
                        if order is None:
                            logger.warning("scan_and_trade [%s]: order execution failed", symbol)
                            continue

                        entry_price = float(order.get("filled_avg_price") or price)
                        last_prices[symbol] = entry_price
                        open_trades[symbol] = {
                            "side": direction,
                            "entry_price": entry_price,
                            "order": order,
                            "features": market_context,
                        }

                        # ── Check for loss on the previous position ───────────
                        # (simplified: detect if price moved against position)
                        prev_trade = open_trades.get(symbol)
                        if prev_trade and symbol in last_prices:
                            prev_entry = prev_trade.get("entry_price", price)
                            prev_side = prev_trade.get("side", "BUY")
                            if prev_side == "BUY":
                                pnl = (price - prev_entry) * self.order_qty
                            else:
                                pnl = (prev_entry - price) * self.order_qty

                            if pnl < 0:
                                trade_record = {
                                    "symbol": symbol,
                                    "ia": "COMMITTEE",
                                    "pnl": pnl,
                                    "side": prev_side,
                                    "entry_price": prev_entry,
                                    "exit_price": price,
                                    "confidence": confidence,
                                    **prev_trade.get("features", {}),
                                }
                                try:
                                    await self.forense.registrar_perdida(trade_record)
                                except Exception as forense_exc:
                                    logger.error(
                                        "scan_and_trade: forense error: %s", forense_exc
                                    )

                    except Exception as sym_exc:
                        logger.error(
                            "scan_and_trade: symbol %s error: %s", symbol, sym_exc
                        )
                        continue

            except Exception as loop_exc:
                logger.error("scan_and_trade: loop error: %s", loop_exc)

            await asyncio.sleep(30)

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
