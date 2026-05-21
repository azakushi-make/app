"""
Cerebro Quant v5 — Veto Engine
Aggregates all veto sources and provides a single authoritative go/no-go
decision before any order is placed.
"""

import logging

from .session_manager import SessionManager
from .macro_filter import MacroFilter
from .forense import ForenseLogger
from .whale_tracker import WhaleTracker

logger = logging.getLogger(__name__)


class VetoEngine:
    """
    Central veto arbiter that checks, in order:
    1. Market open (SessionManager)
    2. Risk PIN level (SessionManager)
    3. Macro event window (MacroFilter)
    4. Forensic Euclidean similarity (ForenseLogger)
    5. Whale / institutional sentiment (WhaleTracker) — BUY only
    """

    def __init__(
        self,
        session: SessionManager,
        macro: MacroFilter,
        forense: ForenseLogger,
        whale: WhaleTracker,
    ) -> None:
        self.session = session
        self.macro = macro
        self.forense = forense
        self.whale = whale

    async def check_all(
        self,
        symbol: str,
        market_context: dict,
        side: str = "BUY",
    ) -> tuple:
        """
        Run all veto checks for the given symbol and market context.

        Returns:
            (True,  "AUTORIZADO")  — all checks passed
            (False, reason: str)   — first check that triggered a veto

        Checks are executed in order and short-circuit on the first veto.
        """
        # ── 1. Market open ────────────────────────────────────────────────────
        try:
            if not self.session.is_market_open():
                reason = "Mercado cerrado"
                logger.info("VetoEngine [%s/%s]: ✗ %s", symbol, side, reason)
                return False, reason
            logger.debug("VetoEngine [%s/%s]: ✓ Mercado abierto", symbol, side)
        except Exception as exc:
            reason = f"Error verificando mercado: {exc}"
            logger.error("VetoEngine check_all session.is_market_open: %s", exc)
            return False, reason

        # ── 2. Risk PIN ───────────────────────────────────────────────────────
        try:
            risk_pin = self.session.get_risk_pin()
            pin_level = risk_pin.get("level", "VETADO")
            pin_reason = risk_pin.get("reason", "")
            if pin_level == "VETADO":
                reason = f"PIN VETADO: {pin_reason}"
                logger.info("VetoEngine [%s/%s]: ✗ %s", symbol, side, reason)
                return False, reason
            logger.debug(
                "VetoEngine [%s/%s]: ✓ Risk PIN = %s (%s)",
                symbol,
                side,
                pin_level,
                pin_reason,
            )
        except Exception as exc:
            reason = f"Error verificando risk PIN: {exc}"
            logger.error("VetoEngine check_all get_risk_pin: %s", exc)
            return False, reason

        # ── 3. Macro filter ───────────────────────────────────────────────────
        try:
            auth_ok, auth_reason = self.macro.authorize_trade()
            if not auth_ok:
                reason = f"MacroFilter: {auth_reason}"
                logger.info("VetoEngine [%s/%s]: ✗ %s", symbol, side, reason)
                return False, reason
            logger.debug("VetoEngine [%s/%s]: ✓ Macro OK", symbol, side)
        except Exception as exc:
            reason = f"Error en MacroFilter: {exc}"
            logger.error("VetoEngine check_all macro.authorize_trade: %s", exc)
            return False, reason

        # ── 4. Forensic euclidean similarity ─────────────────────────────────
        try:
            vetoed, forense_reason = await self.forense.evaluar_veto(market_context)
            if vetoed:
                reason = f"Forense: {forense_reason}"
                logger.info("VetoEngine [%s/%s]: ✗ %s", symbol, side, reason)
                return False, reason
            logger.debug("VetoEngine [%s/%s]: ✓ Forense OK", symbol, side)
        except Exception as exc:
            reason = f"Error en Forense: {exc}"
            logger.error("VetoEngine check_all forense.evaluar_veto: %s", exc)
            return False, reason

        # ── 5. Whale sentiment (BUY side only) ───────────────────────────────
        if side.upper() == "BUY":
            try:
                sentiment = await self.whale.get_sentiment(symbol)
                if self.whale.should_veto_buy(sentiment):
                    reason = (
                        f"WhaleTracker: sentimiento institucional bajista ({sentiment:.3f})"
                    )
                    logger.info("VetoEngine [%s/%s]: ✗ %s", symbol, side, reason)
                    return False, reason
                logger.debug(
                    "VetoEngine [%s/%s]: ✓ Whale OK (sentiment=%.3f)",
                    symbol,
                    side,
                    sentiment,
                )
            except Exception as exc:
                reason = f"Error en WhaleTracker: {exc}"
                logger.error("VetoEngine check_all whale.get_sentiment: %s", exc)
                return False, reason

        logger.info("VetoEngine [%s/%s]: ✓ AUTORIZADO — todos los checks OK", symbol, side)
        return True, "AUTORIZADO"
