#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║          CEREBRO QUANT v5 — Sistema Unificado de Trading        ║
║  5 IAs · Vetos Cruzados · Dashboard Rich · Bot Telegram         ║
╚══════════════════════════════════════════════════════════════════╝

Uso:
    python cerebro_quant_v5.py

Requiere config_alpaca.json en el mismo directorio.
"""

# ── Imports ───────────────────────────────────────────────────────────────────
import asyncio
import json
import logging
import math
import os
import re
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import aiofiles
import aiohttp
import numpy as np

from groq import AsyncGroq
from rich.align import Align
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.console import Console
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters as tg_filters,
)
from zoneinfo import ZoneInfo

# ── Paths & logging ───────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
BRAIN_DIR   = BASE_DIR / "brain"
LOGS_DIR    = BASE_DIR / "logs"
CONFIG_FILE = BASE_DIR / "config_alpaca.json"

BRAIN_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-16s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS_DIR / "cerebro_v5.log"),
    ],
)
logger = logging.getLogger("CerebroQuant")

NY_TZ = ZoneInfo("America/New_York")


# ══════════════════════════════════════════════════════════════════════════════
# 1. SESSION MANAGER — Cuenta regresiva NY + PIN de riesgo
# ══════════════════════════════════════════════════════════════════════════════

class SessionManager:
    NY_OPEN  = time(9, 30)
    NY_CLOSE = time(16, 0)
    SESSION_DURATION_SECONDS = 6 * 3600 + 30 * 60  # 23400 s

    def get_ny_now(self) -> datetime:
        return datetime.now(tz=NY_TZ)

    def is_market_open(self) -> bool:
        try:
            now = self.get_ny_now()
            return now.weekday() < 5 and self.NY_OPEN <= now.time() < self.NY_CLOSE
        except Exception as exc:
            logger.error("SessionManager.is_market_open: %s", exc)
            return False

    def get_countdown_seconds(self) -> int:
        try:
            if not self.is_market_open():
                return 0
            now = self.get_ny_now()
            close = now.replace(hour=self.NY_CLOSE.hour, minute=self.NY_CLOSE.minute,
                                second=0, microsecond=0)
            return max(0, int((close - now).total_seconds()))
        except Exception as exc:
            logger.error("SessionManager.get_countdown_seconds: %s", exc)
            return 0

    def get_countdown_str(self) -> str:
        s = self.get_countdown_seconds()
        if s <= 0:
            return "00h 00m 00s"
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h:02d}h {m:02d}m {sec:02d}s"

    def get_session_progress(self) -> float:
        try:
            if not self.is_market_open():
                return 0.0
            now = self.get_ny_now()
            open_t = now.replace(hour=self.NY_OPEN.hour, minute=self.NY_OPEN.minute,
                                 second=0, microsecond=0)
            elapsed = (now - open_t).total_seconds()
            return round(min(100.0, max(0.0, elapsed / self.SESSION_DURATION_SECONDS * 100)), 2)
        except Exception as exc:
            logger.error("SessionManager.get_session_progress: %s", exc)
            return 0.0

    def get_progress_bar(self, width: int = 20) -> str:
        pct = self.get_session_progress()
        filled = int(pct / 100.0 * width)
        return f"[{'█' * filled}{'░' * (width - filled)}] {int(pct)}%"

    def get_risk_pin(self) -> dict:
        try:
            if not self.is_market_open():
                return {"level": "VETADO", "reason": "Mercado cerrado", "emoji": "🔴"}
            t = self.get_ny_now().time()
            if t < time(10, 0):
                return {"level": "RIESGO_ALTO", "reason": "Apertura volátil (primeros 30min)", "emoji": "🟡"}
            if t >= time(15, 30):
                return {"level": "RIESGO_ALTO", "reason": "Cierre volátil (últimos 30min)", "emoji": "🟡"}
            return {"level": "SEGURO", "reason": "Ventana segura 10:00-15:30", "emoji": "🟢"}
        except Exception as exc:
            return {"level": "VETADO", "reason": f"Error: {exc}", "emoji": "🔴"}


# ══════════════════════════════════════════════════════════════════════════════
# 2. PRICE FEED — Triangulación TwelveData + Finnhub + yfinance
# ══════════════════════════════════════════════════════════════════════════════

class PriceFeed:
    def __init__(self, config: dict) -> None:
        md = config.get("market_data", {})
        self.twelvedata_key = md.get("twelvedata_key", md.get("twelvedata_api_key", ""))
        self.finnhub_key    = md.get("finnhub_key",    md.get("finnhub_api_key", ""))
        self._session: Optional[aiohttp.ClientSession] = None

    async def _sess(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    async def _twelvedata(self, symbol: str) -> Optional[float]:
        if not self.twelvedata_key:
            return None
        try:
            url = f"https://api.twelvedata.com/price?symbol={symbol}&apikey={self.twelvedata_key}"
            async with (await self._sess()).get(url) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                p = data.get("price")
                return float(p) if p else None
        except Exception:
            return None

    async def _finnhub(self, symbol: str) -> Optional[float]:
        if not self.finnhub_key:
            return None
        try:
            url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={self.finnhub_key}"
            async with (await self._sess()).get(url) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                p = data.get("c")
                return float(p) if p else None
        except Exception:
            return None

    async def _yfinance(self, symbol: str) -> Optional[float]:
        try:
            loop = asyncio.get_running_loop()
            def _fetch():
                import yfinance as yf
                p = yf.Ticker(symbol).fast_info.last_price
                return float(p) if p and p == p else None  # NaN check
            return await loop.run_in_executor(None, _fetch)
        except Exception:
            return None

    async def get_price(self, symbol: str) -> float:
        results = await asyncio.gather(
            self._twelvedata(symbol),
            self._finnhub(symbol),
            self._yfinance(symbol),
            return_exceptions=True,
        )
        valid = [r for r in results if isinstance(r, float)]
        if not valid:
            raise RuntimeError(f"Todas las fuentes fallaron para {symbol}")
        avg = sum(valid) / len(valid)
        logger.info("Precio %s = %.4f (media de %d fuentes)", symbol, avg, len(valid))
        return avg

    async def get_prices_bulk(self, symbols: list) -> dict:
        results = await asyncio.gather(*[self.get_price(s) for s in symbols], return_exceptions=True)
        return {s: r for s, r in zip(symbols, results) if isinstance(r, float)}

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ══════════════════════════════════════════════════════════════════════════════
# 3. MACRO FILTER — Calendario Finnhub + circuit breaker ±15min
# ══════════════════════════════════════════════════════════════════════════════

class MacroFilter:
    def __init__(self, config: dict, telegram_alert_fn: Optional[Callable] = None) -> None:
        self.telegram_alert_fn = telegram_alert_fn
        self.events: list = []
        self.paused: bool = False
        self.pause_reason: str = ""
        self.window_min: int = 15
        self._lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None
        md = config.get("market_data", {})
        self.finnhub_key = md.get("finnhub_key", md.get("finnhub_api_key", ""))

    async def _sess(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    async def load_calendar_finnhub(self) -> None:
        if not self.finnhub_key:
            self.events = []
            return
        try:
            url = f"https://finnhub.io/api/v1/calendar/economic?token={self.finnhub_key}"
            async with (await self._sess()).get(url) as r:
                if r.status != 200:
                    self.events = []
                    return
                data = await r.json()
            raw = data.get("economicCalendar", [])
            today = datetime.now(NY_TZ).date()
            parsed = []
            for ev in (raw if isinstance(raw, list) else []):
                try:
                    if (ev.get("impact") or "").lower() != "high":
                        continue
                    ts = ev.get("time") or ev.get("date") or ""
                    fmt = "%Y-%m-%d %H:%M:%S" if " " in ts else "%Y-%m-%d"
                    dt_utc = datetime.strptime(ts, fmt).replace(tzinfo=ZoneInfo("UTC"))
                    dt_ny  = dt_utc.astimezone(NY_TZ)
                    if dt_ny.date() != today:
                        continue
                    parsed.append({"name": ev.get("event", "Evento"), "time": dt_ny,
                                   "impact": "high", "country": ev.get("country", "US")})
                except Exception:
                    continue
            async with self._lock:
                self.events = sorted(parsed, key=lambda e: e["time"])
            logger.info("MacroFilter: %d eventos de alto impacto hoy", len(self.events))
        except Exception as exc:
            logger.error("MacroFilter.load_calendar_finnhub: %s", exc)
            self.events = []

    def is_blocked(self) -> bool:
        now = datetime.now(NY_TZ)
        window = timedelta(minutes=self.window_min)
        for ev in self.events:
            if abs((now - ev["time"]).total_seconds()) <= window.total_seconds():
                return True
        return False

    def authorize_trade(self) -> tuple:
        if self.paused:
            return False, f"Pausa manual: {self.pause_reason}"
        if self.is_blocked():
            now = datetime.now(NY_TZ)
            window = timedelta(minutes=self.window_min)
            for ev in self.events:
                if abs((now - ev["time"]).total_seconds()) <= window.total_seconds():
                    return False, f"Bloqueado por {ev['name']} a las {ev['time'].strftime('%H:%M')} (±{self.window_min}min)"
        return True, "OK"

    def get_next_event(self) -> Optional[dict]:
        now = datetime.now(NY_TZ)
        for ev in self.events:
            if ev["time"] > now:
                return ev
        return None

    async def monitor_loop(self) -> None:
        was_blocked = False
        while True:
            try:
                await self.load_calendar_finnhub()
                now_blocked = self.is_blocked()
                if now_blocked and not was_blocked:
                    _, reason = self.authorize_trade()
                    msg = f"⚠️ MACRO BLOCK ACTIVADO\n{reason}"
                    if self.telegram_alert_fn:
                        await self.telegram_alert_fn(msg)
                elif was_blocked and not now_blocked:
                    msg = "✅ MACRO BLOCK LIBERADO — operaciones autorizadas"
                    if self.telegram_alert_fn:
                        await self.telegram_alert_fn(msg)
                was_blocked = now_blocked
            except Exception as exc:
                logger.error("MacroFilter.monitor_loop: %s", exc)
            await asyncio.sleep(60)

    def format_report(self) -> str:
        now = datetime.now(NY_TZ)
        auth, reason = self.authorize_trade()
        next_ev = self.get_next_event()
        lines = [
            "═" * 50,
            "       📅  MACRO FILTER — REPORTE",
            "═" * 50,
            f"  Hora NY   : {now.strftime('%H:%M:%S')}",
            f"  Estado    : {'🔴 BLOQUEADO' if not auth else '🟢 AUTORIZADO'}",
            f"  Razón     : {reason}",
            f"  Eventos   : {len(self.events)} hoy",
            "─" * 50,
        ]
        if self.events:
            for ev in self.events:
                marker = "▶" if ev["time"] > now else "✓"
                lines.append(f"  {marker} {ev['time'].strftime('%H:%M')}  {ev['name'][:35]}")
        else:
            lines.append("  (Sin eventos de alto impacto para hoy)")
        if next_ev:
            diff = int((next_ev["time"] - now).total_seconds() / 60)
            lines.append(f"\n  Próximo: {next_ev['name']} en {diff}min")
        lines.append("═" * 50)
        return "\n".join(lines)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ══════════════════════════════════════════════════════════════════════════════
# 4. FORENSE LOGGER — aiofiles + Lock + Distancia Euclidiana
# ══════════════════════════════════════════════════════════════════════════════

class ForenseLogger:
    def __init__(self, telegram_alert_fn: Optional[Callable] = None) -> None:
        self.telegram_alert_fn = telegram_alert_fn
        self._lock = asyncio.Lock()
        self.max_records = 500
        self.file_path = BRAIN_DIR / "perdidas_forense.json"

    async def load(self) -> list:
        try:
            if not self.file_path.exists():
                return []
            async with aiofiles.open(self.file_path, "r", encoding="utf-8") as fh:
                content = await fh.read()
            if not content.strip():
                return []
            data = json.loads(content)
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.error("ForenseLogger.load: %s", exc)
            return []

    async def save(self, records: list) -> None:
        async with self._lock:
            try:
                truncated = records[-self.max_records:]
                async with aiofiles.open(self.file_path, "w", encoding="utf-8") as fh:
                    await fh.write(json.dumps(truncated, indent=2, default=str))
            except Exception as exc:
                logger.error("ForenseLogger.save: %s", exc)

    async def registrar_perdida(self, trade: dict) -> bool:
        try:
            record = {**trade, "timestamp": datetime.now(timezone.utc).isoformat()}
            record.setdefault("volatilidad", 0.0)
            record.setdefault("sentimiento", 0.0)
            record.setdefault("rsi", 50.0)
            record.setdefault("macd", 0.0)
            record.setdefault("symbol", "?")
            record.setdefault("ia", "?")
            record.setdefault("pnl", 0.0)
            records = await self.load()
            records.append(record)
            await self.save(records)
            msg = (f"🔴 PÉRDIDA REGISTRADA\nSímbolo: {record['symbol']}\n"
                   f"P&L: {record['pnl']:.2f}\nIA: {record['ia']}")
            logger.warning(msg.replace("\n", " | "))
            if self.telegram_alert_fn:
                await self.telegram_alert_fn(msg)
            return True
        except Exception as exc:
            logger.error("ForenseLogger.registrar_perdida: %s", exc)
            return False

    def _normalize(self, ctx: dict) -> list:
        vol  = max(0.0, min(1.0, float(ctx.get("volatilidad", 0)) / 100.0))
        sent = max(0.0, min(1.0, (float(ctx.get("sentimiento", 0)) + 1.0) / 2.0))
        rsi  = max(0.0, min(1.0, float(ctx.get("rsi", 50)) / 100.0))
        macd = max(0.0, min(1.0, (float(ctx.get("macd", 0)) + 5.0) / 10.0))
        return [vol, sent, rsi, macd]

    async def calcular_similitud_euclidiana(self, ctx: dict) -> tuple:
        try:
            records = await self.load()
            if not records:
                return 0.0, None
            cur = self._normalize(ctx)
            max_dist = math.sqrt(4)
            best_sim, best_trade = 0.0, None
            for r in records:
                dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(cur, self._normalize(r))))
                sim  = max(0.0, min(100.0, (1.0 - dist / max_dist) * 100.0))
                if sim > best_sim:
                    best_sim, best_trade = sim, r
            return round(best_sim, 2), best_trade
        except Exception as exc:
            logger.error("ForenseLogger.calcular_similitud: %s", exc)
            return 0.0, None

    async def evaluar_veto(self, ctx: dict) -> tuple:
        sim, trade = await self.calcular_similitud_euclidiana(ctx)
        if sim >= 90.0:
            symbol = (trade or {}).get("symbol", "?")
            pnl    = (trade or {}).get("pnl", 0.0)
            ts     = (trade or {}).get("timestamp", "?")[:10]
            return True, f"Match {sim:.1f}% con pérdida histórica — {symbol} P&L={pnl:.2f} [{ts}]"
        return False, "OK"

    async def generar_reporte(self) -> str:
        records = await self.load()
        total = len(records)
        if total == 0:
            return "═" * 50 + "\n  🔬 FORENSE — Sin registros todavía.\n" + "═" * 50
        pnls = [float(r.get("pnl", 0)) for r in records]
        ia_counts: dict = {}
        rsi_b = {"<30": 0, "30-70": 0, ">70": 0}
        vol_b = {"<20%": 0, "20-50%": 0, ">50%": 0}
        for r in records:
            ia_counts[r.get("ia", "?")] = ia_counts.get(r.get("ia", "?"), 0) + 1
            rsi = float(r.get("rsi", 50))
            if rsi < 30:    rsi_b["<30"] += 1
            elif rsi < 70:  rsi_b["30-70"] += 1
            else:           rsi_b[">70"] += 1
            vol = float(r.get("volatilidad", 0))
            if vol < 20:    vol_b["<20%"] += 1
            elif vol < 50:  vol_b["20-50%"] += 1
            else:           vol_b[">50%"] += 1
        lines = [
            "═" * 50,
            "      🔬  FORENSE — REPORTE DE PÉRDIDAS",
            "═" * 50,
            f"  Total       : {total}",
            f"  P&L total   : {sum(pnls):.2f}",
            f"  P&L promedio: {sum(pnls)/total:.2f}",
            f"  Peor trade  : {min(pnls):.2f}",
            "─" * 50,
            "  🤖 IAs con más pérdidas:",
        ]
        for ia, cnt in sorted(ia_counts.items(), key=lambda x: -x[1])[:5]:
            lines.append(f"    {ia:20s}: {cnt}")
        lines += ["─" * 50, "  📊 Distribución RSI:"]
        for b, cnt in rsi_b.items():
            lines.append(f"    RSI {b:8s}: {cnt:3d} ({cnt/total*100:.1f}%)")
        lines += ["─" * 50, "  📊 Distribución Volatilidad:"]
        for b, cnt in vol_b.items():
            lines.append(f"    Vol {b:8s}: {cnt:3d} ({cnt/total*100:.1f}%)")
        lines.append("═" * 50)
        return "\n".join(lines)

    async def exportar_hf_dataset(self, env_var: str = "HF_TOKEN") -> None:
        records = await self.load()
        dataset = {
            "description": "Cerebro Quant v5 — Negative training set",
            "total_records": len(records),
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "records": records,
        }
        out = BRAIN_DIR / "dataset_hf.json"
        async with aiofiles.open(out, "w") as fh:
            await fh.write(json.dumps(dataset, indent=2, default=str))
        logger.info("ForenseLogger: dataset HF guardado en %s (%d registros)", out, len(records))


# ══════════════════════════════════════════════════════════════════════════════
# 5. WHALE TRACKER — Sentimiento institucional Finnhub + veto < -0.7
# ══════════════════════════════════════════════════════════════════════════════

class WhaleTracker:
    def __init__(self, config: dict, telegram_alert_fn: Optional[Callable] = None) -> None:
        self.telegram_alert_fn = telegram_alert_fn
        self._lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None
        self._state: dict = {}
        self.file_path = BRAIN_DIR / "whale_flow.json"
        md = config.get("market_data", {})
        self.finnhub_key = md.get("finnhub_key", md.get("finnhub_api_key", ""))

    async def _sess(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))
        return self._session

    async def get_sentiment(self, symbol: str) -> float:
        if not self.finnhub_key:
            return 0.1
        try:
            url = (f"https://finnhub.io/api/v1/stock/institutional-ownership"
                   f"?symbol={symbol}&token={self.finnhub_key}")
            async with (await self._sess()).get(url) as r:
                if r.status != 200:
                    return 0.1
                data = await r.json()
            entries = data.get("ownership", [])
            if not entries:
                return 0.1
            pos, neg = 0.0, 0.0
            for e in entries:
                change = float(e.get("change", 0) or 0)
                shares = abs(float(e.get("share", 0) or 0))
                if change > 0:   pos += shares
                elif change < 0: neg += shares
            total = pos + neg
            if total == 0:
                return 0.0
            return max(-1.0, min(1.0, (pos - neg) / total))
        except Exception as exc:
            logger.error("WhaleTracker.get_sentiment %s: %s", symbol, exc)
            return 0.1

    def should_veto_buy(self, sentiment: float) -> bool:
        return sentiment < -0.7

    async def save_state(self) -> None:
        async with self._lock:
            try:
                payload = {"updated_at": datetime.now(timezone.utc).isoformat(),
                           "sentiments": self._state}
                async with aiofiles.open(self.file_path, "w") as fh:
                    await fh.write(json.dumps(payload, indent=2))
            except Exception as exc:
                logger.error("WhaleTracker.save_state: %s", exc)

    async def run_loop(self, symbols: list) -> None:
        logger.info("WhaleTracker: iniciado para %s", symbols)
        while True:
            try:
                new_state = {}
                for symbol in symbols:
                    sentiment = await self.get_sentiment(symbol)
                    new_state[symbol] = round(sentiment, 4)
                    if self.should_veto_buy(sentiment):
                        msg = (f"🐋 WHALE ALERT — {symbol}\n"
                               f"Sentimiento institucional: {sentiment:.3f}\n"
                               f"BUY vetado por posicionamiento bajista.")
                        logger.warning(msg.replace("\n", " | "))
                        if self.telegram_alert_fn:
                            await self.telegram_alert_fn(msg)
                self._state = new_state
                await self.save_state()
            except Exception as exc:
                logger.error("WhaleTracker.run_loop: %s", exc)
            await asyncio.sleep(60)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ══════════════════════════════════════════════════════════════════════════════
# 6. VETO ENGINE — 5 vetos en cadena (sesión → PIN → macro → forense → whale)
# ══════════════════════════════════════════════════════════════════════════════

class VetoEngine:
    def __init__(self, session: SessionManager, macro: MacroFilter,
                 forense: ForenseLogger, whale: WhaleTracker) -> None:
        self.session = session
        self.macro   = macro
        self.forense = forense
        self.whale   = whale

    async def check_all(self, symbol: str, market_context: dict, side: str = "BUY") -> tuple:
        # 1. Mercado abierto
        if not self.session.is_market_open():
            return False, "Mercado cerrado"

        # 2. Risk PIN
        pin = self.session.get_risk_pin()
        if pin["level"] == "VETADO":
            return False, f"PIN VETADO: {pin['reason']}"

        # 3. Macro filter
        auth, reason = self.macro.authorize_trade()
        if not auth:
            return False, f"MacroFilter: {reason}"

        # 4. Forense euclidiana
        vetoed, reason = await self.forense.evaluar_veto(market_context)
        if vetoed:
            return False, f"Forense: {reason}"

        # 5. Whale (solo compras)
        if side.upper() == "BUY":
            sentiment = await self.whale.get_sentiment(symbol)
            if self.whale.should_veto_buy(sentiment):
                return False, f"WhaleTracker: sentimiento bajista ({sentiment:.3f})"

        logger.info("VetoEngine [%s/%s]: ✓ AUTORIZADO", symbol, side)
        return True, "AUTORIZADO"


# ══════════════════════════════════════════════════════════════════════════════
# 7. TRADING ENGINE — Comité 5 IAs + Alpaca
# ══════════════════════════════════════════════════════════════════════════════

def _lr_predict(X, y, x):
    from sklearn.linear_model import LinearRegression
    return float(LinearRegression().fit(X, y).predict([x])[0])

def _ridge_predict(X, y, x):
    from sklearn.linear_model import Ridge
    return float(Ridge(alpha=1.0).fit(X, y).predict([x])[0])

def _lasso_predict(X, y, x):
    from sklearn.linear_model import Lasso
    return float(Lasso(alpha=0.1, max_iter=2000).fit(X, y).predict([x])[0])


class TradingEngine:
    _SX = [
        [30,-0.5,0.8,-1.0,-2.0],[28,-0.8,0.6,-1.5,-3.0],[25,-1.2,0.5,-2.0,-4.0],
        [32,-0.3,0.9,-0.5,-1.0],[40, 0.1,1.0, 0.2, 0.5],[50, 0.0,1.0, 0.0, 0.0],
        [55, 0.2,1.1, 0.5, 1.0],[60, 0.5,1.2, 1.0, 2.0],[65, 0.8,1.3, 1.5, 3.0],
        [70, 1.0,1.5, 2.0, 4.0],[75, 1.2,1.6, 2.5, 5.0],[80, 1.5,1.8, 3.0, 6.0],
        [85, 2.0,2.0, 3.5, 7.0],[20,-2.0,0.4,-3.0,-5.0],[22,-1.8,0.5,-2.5,-4.5],
        [45, 0.0,0.9, 0.1, 0.2],[48, 0.1,1.0, 0.3, 0.6],[52, 0.2,1.1, 0.4, 0.8],
        [58, 0.6,1.3, 1.2, 2.4],[62, 0.9,1.4, 1.8, 3.5],[67, 1.1,1.5, 2.2, 4.2],
        [72, 1.3,1.7, 2.7, 5.3],[77, 1.6,1.9, 3.2, 6.2],[82, 1.8,2.1, 3.7, 7.2],
        [35,-0.1,0.9,-0.1,-0.2],[38, 0.0,1.0, 0.0, 0.1],[42, 0.1,1.0, 0.2, 0.4],
        [46, 0.1,1.0, 0.3, 0.6],[56, 0.3,1.2, 0.7, 1.4],[63, 0.7,1.3, 1.4, 2.8],
    ]
    _SY = [
        -0.8,-0.9,-1.0,-0.6,-0.1, 0.0, 0.2, 0.4, 0.6, 0.7,
         0.8, 0.9, 1.0,-1.0,-0.9, 0.0, 0.1, 0.1, 0.5, 0.7,
         0.8, 0.9, 0.9, 1.0,-0.1, 0.0, 0.1, 0.1, 0.3, 0.6,
    ]

    def __init__(self, config: dict, veto_engine: VetoEngine,
                 price_feed: PriceFeed, forense: ForenseLogger) -> None:
        self.veto_engine = veto_engine
        self.price_feed  = price_feed
        self.forense     = forense
        self.on_signal: Optional[Callable] = None
        self.on_log:    Optional[Callable] = None

        tp = config.get("trading_params", {})
        self.min_confidence = float(tp.get("min_confidence",
                               tp.get("min_confidence_execution",
                               tp.get("min_confidence_signal", 60.0))))
        self.order_qty = int(tp.get("order_qty", 1))

        alpaca = config.get("alpaca", {})
        self.alpaca_key    = alpaca.get("api_key", "")
        self.alpaca_secret = alpaca.get("secret_key", "")
        self.alpaca_url    = alpaca.get("base_url", "https://paper-api.alpaca.markets").rstrip("/")

        groq = config.get("groq", {})
        self.groq_key   = groq.get("api_key", "")
        self.groq_model = groq.get("model", "llama-3.3-70b-versatile")

        self.cardinal_file = BRAIN_DIR / "cardinal_state.json"
        self._http: Optional[aiohttp.ClientSession] = None
        self._history: dict = {}

    async def _sess(self) -> aiohttp.ClientSession:
        if not self._http or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._http

    def _features(self, symbol: str, price: float) -> list:
        h = self._history.get(symbol, [])
        h.append(price)
        h = h[-30:]
        self._history[symbol] = h
        n = len(h)
        if n >= 2:
            gains  = [max(0, h[i]-h[i-1]) for i in range(1, n)]
            losses = [max(0, h[i-1]-h[i]) for i in range(1, n)]
            ag = sum(gains[-14:])  / min(len(gains), 14)
            al = sum(losses[-14:]) / min(len(losses), 14)
            rsi = 100.0 if al == 0 else 100 - 100/(1 + ag/al)
        else:
            rsi = 50.0
        def ema(data, p):
            if not data: return 0.0
            k, e = 2/(p+1), data[0]
            for v in data[1:]: e = v*k + e*(1-k)
            return e
        macd = (ema(h, 12) - ema(h, 26)) if n >= 2 else 0.0
        pc1 = ((h[-1]-h[-2])/h[-2]*100) if n >= 2 else 0.0
        pc5 = ((h[-1]-h[-6])/h[-6]*100) if n >= 6 else 0.0
        return [rsi, macd, 1.0, pc1, pc5]

    def ia1(self, f): return max(-1.0, min(1.0, _lr_predict(self._SX, self._SY, f)))
    def ia2(self, f): return max(-1.0, min(1.0, _ridge_predict(self._SX, self._SY, f)))
    def ia3(self, f): return max(-1.0, min(1.0, _lasso_predict(self._SX, self._SY, f)))

    async def ia4_cardinal(self, f: list, symbol: str) -> tuple:
        try:
            state = {}
            if self.cardinal_file.exists():
                async with aiofiles.open(self.cardinal_file) as fh:
                    state = json.loads(await fh.read())
            sym = state.get(symbol, {"accuracy": 100.0, "trades": 0})
            if int(sym.get("trades", 0)) > 10 and float(sym.get("accuracy", 100)) < 55.0:
                return 0.0, False
            rsi_s  = (f[0] - 50) / 50
            macd_s = max(-1.0, min(1.0, f[1] / 2.0))
            return max(-1.0, min(1.0, 0.6*rsi_s + 0.4*macd_s)), True
        except Exception as exc:
            logger.error("Cardinal IA4: %s", exc)
            return 0.0, False

    async def ia5_groq(self, symbol: str, price: float, ctx: str) -> float:
        if not self.groq_key:
            return 0.0
        try:
            prompt = (f"Analista quant. Activo: {symbol}, Precio: {price:.4f}. "
                      f"Contexto: {ctx}. Responde SOLO con un número de -1.0 a +1.0.")
            payload = {"model": self.groq_model,
                       "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": 16, "temperature": 0.1}
            headers = {"Authorization": f"Bearer {self.groq_key}",
                       "Content-Type": "application/json"}
            async with (await self._sess()).post(
                "https://api.groq.com/openai/v1/chat/completions",
                json=payload, headers=headers
            ) as r:
                if r.status != 200:
                    return 0.0
                data = await r.json()
            raw = data["choices"][0]["message"]["content"].strip()
            matches = re.findall(r"-?\d+\.?\d*", raw)
            return max(-1.0, min(1.0, float(matches[0]))) if matches else 0.0
        except Exception as exc:
            logger.error("IA5 Groq: %s", exc)
            return 0.0

    async def get_committee_vote(self, symbol: str, features: list,
                                 price: float, ctx: str) -> dict:
        s1 = self.ia1(features)
        s2 = self.ia2(features)
        s3 = self.ia3(features)
        s4, cardinal_active = await self.ia4_cardinal(features, symbol)
        s5 = await self.ia5_groq(symbol, price, ctx)
        votes = {"IA1_LR": s1, "IA2_Ridge": s2, "IA3_Lasso": s3,
                 "IA4_Cardinal": s4 if cardinal_active else None, "IA5_Groq": s5}
        active = [v for v in votes.values() if v is not None]
        if not active:
            return {"signal": 0.0, "confidence": 0.0, "votes": votes,
                    "cardinal_active": cardinal_active, "direction": "HOLD"}
        consensus   = sum(active) / len(active)
        confidence  = abs(consensus) * 100
        direction   = ("HOLD" if abs(consensus) < 0.3 or confidence < self.min_confidence
                       else ("BUY" if consensus >= 0.3 else "SELL"))
        logger.info("Comité [%s]: señal=%.3f conf=%.1f dir=%s", symbol, consensus, confidence, direction)
        return {"signal": round(consensus, 4), "confidence": round(confidence, 2),
                "votes": votes, "cardinal_active": cardinal_active, "direction": direction}

    async def execute_order(self, symbol: str, side: str, qty: int) -> Optional[dict]:
        if not self.alpaca_key or not self.alpaca_secret:
            return None
        try:
            headers = {"APCA-API-KEY-ID": self.alpaca_key,
                       "APCA-API-SECRET-KEY": self.alpaca_secret,
                       "Content-Type": "application/json"}
            payload = {"symbol": symbol, "qty": str(qty),
                       "side": side.lower(), "type": "market", "time_in_force": "day"}
            async with (await self._sess()).post(
                f"{self.alpaca_url}/v2/orders", json=payload, headers=headers
            ) as r:
                data = await r.json()
                if r.status not in (200, 201):
                    logger.error("Alpaca orden fallida [%s]: %s", symbol, data)
                    return None
                logger.info("Orden ejecutada: %s %s x%d", side, symbol, qty)
                return data
        except Exception as exc:
            logger.error("execute_order %s: %s", symbol, exc)
            return None

    async def scan_and_trade(self, symbols: list) -> None:
        logger.info("TradingEngine: scan iniciado para %s", symbols)
        last_trades: dict = {}
        while True:
            try:
                prices = await self.price_feed.get_prices_bulk(symbols)
                for symbol in symbols:
                    try:
                        price = prices.get(symbol)
                        if not price:
                            continue
                        features = self._features(symbol, price)
                        ctx = (f"RSI={features[0]:.1f}, MACD={features[1]:.3f}, "
                               f"Precio={price:.4f}, Cambio1d={features[3]:.2f}%")
                        vote = await self.get_committee_vote(symbol, features, price, ctx)
                        direction  = vote["direction"]
                        confidence = vote["confidence"]

                        sig_info = {"symbol": symbol, "direction": direction,
                                    "confidence": confidence, "authorized": False,
                                    "veto_reason": "Por evaluar"}
                        if direction == "HOLD" or confidence < self.min_confidence:
                            sig_info["veto_reason"] = "Confianza insuficiente"
                            if self.on_signal: self.on_signal(sig_info)
                            continue

                        mctx = {"volatilidad": abs(features[3]), "sentimiento": vote["signal"],
                                "rsi": features[0], "macd": features[1]}
                        authorized, veto_reason = await self.veto_engine.check_all(symbol, mctx, direction)
                        sig_info["authorized"]  = authorized
                        sig_info["veto_reason"] = veto_reason
                        if self.on_signal: self.on_signal(sig_info)
                        if self.on_log: self.on_log(f"[{symbol}] {direction} conf={confidence:.1f}% → {veto_reason}")

                        if not authorized:
                            continue

                        order = await self.execute_order(symbol, direction, self.order_qty)
                        if order:
                            entry = float(order.get("filled_avg_price") or price)
                            prev  = last_trades.get(symbol)
                            if prev:
                                pnl = (price - prev["entry"])*self.order_qty if prev["side"] == "BUY" \
                                      else (prev["entry"] - price)*self.order_qty
                                if pnl < 0:
                                    await self.forense.registrar_perdida({
                                        "symbol": symbol, "ia": "COMMITTEE", "pnl": pnl,
                                        "side": prev["side"], "entry_price": prev["entry"],
                                        "exit_price": price, **mctx,
                                    })
                            last_trades[symbol] = {"side": direction, "entry": entry}
                    except Exception as exc:
                        logger.error("scan_and_trade %s: %s", symbol, exc)
            except Exception as exc:
                logger.error("scan_and_trade loop: %s", exc)
            await asyncio.sleep(30)

    async def close(self):
        if self._http and not self._http.closed:
            await self._http.close()


# ══════════════════════════════════════════════════════════════════════════════
# 8. TELEGRAM BOT — Comandos + chat conversacional Groq
# ══════════════════════════════════════════════════════════════════════════════

class TelegramBot:
    def __init__(self, config: dict, session: SessionManager, macro: MacroFilter,
                 forense: ForenseLogger, veto: VetoEngine) -> None:
        self.session = session
        self.macro   = macro
        self.forense = forense
        self.veto    = veto
        tg = config.get("telegram", {})
        self.token   = tg.get("token", tg.get("bot_token", ""))
        self.chat_id = str(tg.get("chat_id", ""))
        groq = config.get("groq", {})
        self.groq_key   = groq.get("api_key", "")
        self.groq_model = groq.get("model", "llama-3.3-70b-versatile")
        self.application = None
        if self.token:
            try:
                self.application = Application.builder().token(self.token).build()
                self._register()
                logger.info("TelegramBot: inicializado")
            except Exception as exc:
                logger.error("TelegramBot init: %s — deshabilitado", exc)

    def _register(self):
        app = self.application
        app.add_handler(CommandHandler("start",   self.cmd_start))
        app.add_handler(CommandHandler("status",  self.cmd_status))
        app.add_handler(CommandHandler("macro",   self.cmd_macro))
        app.add_handler(CommandHandler("forense", self.cmd_forense))
        app.add_handler(CommandHandler("ayuda",   self.cmd_start))
        app.add_handler(MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, self.handle_text))

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = ("```\n"
               "╔═══════════════════════════════════════╗\n"
               "║   🧠 CEREBRO QUANT v5 — EN LÍNEA      ║\n"
               "╚═══════════════════════════════════════╝\n\n"
               "  /status  →  Estado mercado + sesión NY\n"
               "  /macro   →  Calendario económico\n"
               "  /forense →  Análisis de pérdidas\n"
               "  /ayuda   →  Este menú\n\n"
               "O escríbeme para hablar con la IA.\n```")
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        pin  = self.session.get_risk_pin()
        bar  = self.session.get_progress_bar(18)
        pin_e = {"SEGURO": "✅", "RIESGO_ALTO": "⚠️", "VETADO": "🛑"}[pin["level"]]
        auth, reason = self.macro.authorize_trade()
        next_ev = self.macro.get_next_event()
        msg = (
            "```\n═══════════════════════════════════════════\n"
            "📌 RADAR — NUEVA YORK\n═══════════════════════════════════════════\n\n"
            f"⏰ HORA NY   : {self.session.get_ny_now().strftime('%I:%M:%S %p')}\n"
            f"📊 MERCADO   : {'✅ ABIERTO' if self.session.is_market_open() else '🔴 CERRADO'}\n"
            f"⏳ CIERRE    : {self.session.get_countdown_str()}\n"
            f"   {bar}\n\n"
            f"🛡️  PIN        : {pin_e} {pin['level']}\n"
            f"   {pin['reason']}\n\n"
            f"🌍 MACRO     : {'✅ OK' if auth else '🛑 BLOQUEADO'}\n"
            f"   {reason}\n"
        )
        if next_ev:
            msg += f"\n⏰ Próximo : {next_ev['time'].strftime('%H:%M')} {next_ev['name']}\n"
        msg += "═══════════════════════════════════════════\n```"
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_macro(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        report = self.macro.format_report()
        for chunk in [report[i:i+4000] for i in range(0, len(report), 4000)]:
            await update.message.reply_text(f"```\n{chunk}\n```", parse_mode="Markdown")

    async def cmd_forense(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        report = await self.forense.generar_reporte()
        for chunk in [report[i:i+4000] for i in range(0, len(report), 4000)]:
            await update.message.reply_text(f"```\n{chunk}\n```", parse_mode="Markdown")

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.lower()
        if any(w in text for w in ["status","estado","puedo operar","mercado","hora","sesion"]):
            await self.cmd_status(update, context); return
        if any(w in text for w in ["forense","perdida","pérdida","errores"]):
            await self.cmd_forense(update, context); return
        if any(w in text for w in ["macro","evento","calendario","nfp","cpi"]):
            await self.cmd_macro(update, context); return
        await update.message.reply_text("🧠 Consultando IA...")
        try:
            pin = self.session.get_risk_pin()
            auth, reason = self.macro.authorize_trade()
            system = (f"Eres el asistente de Cerebro Quant v5. "
                      f"PIN={pin['level']}, Macro={'OK' if auth else reason}, "
                      f"Mercado={'abierto' if self.session.is_market_open() else 'cerrado'}. "
                      f"Responde en español, conciso y profesional.")
            client = AsyncGroq(api_key=self.groq_key)
            resp = await client.chat.completions.create(
                model=self.groq_model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": update.message.text}],
                max_tokens=400,
            )
            await update.message.reply_text(resp.choices[0].message.content.strip())
        except Exception as exc:
            await update.message.reply_text(f"⚠️ Error IA: {exc}")

    async def send_alert(self, message: str):
        if not self.application:
            return
        try:
            await self.application.bot.send_message(
                chat_id=self.chat_id, text=f"🚨 ALERTA\n\n{message}"
            )
        except Exception as exc:
            logger.error("TelegramBot.send_alert: %s", exc)

    async def run_polling(self):
        if not self.application:
            logger.warning("TelegramBot: sin token, polling deshabilitado")
            while True:
                await asyncio.sleep(3600)
        logger.info("TelegramBot: iniciando polling...")
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling(drop_pending_updates=True)
        while True:
            await asyncio.sleep(3600)


# ══════════════════════════════════════════════════════════════════════════════
# 9. DASHBOARD — Terminal Bloomberg con Rich
# ══════════════════════════════════════════════════════════════════════════════

class Dashboard:
    def __init__(self, session: SessionManager, macro: MacroFilter,
                 forense: ForenseLogger, whale: WhaleTracker, veto: VetoEngine) -> None:
        self.session = session
        self.macro   = macro
        self.forense = forense
        self.whale   = whale
        self.veto    = veto
        self.signals: list = []
        self.log_lines: list = []

    def push_log(self, line: str):
        self.log_lines.append(line)
        self.log_lines = self.log_lines[-8:]

    def _header(self) -> Panel:
        ny = self.session.get_ny_now()
        t  = Text(f"🧠  CEREBRO QUANT v5   │   NY: {ny.strftime('%Y-%m-%d  %I:%M:%S %p')}",
                  style="bold white on dark_blue")
        return Panel(Align.center(t), style="bold blue")

    def _session_panel(self) -> Panel:
        pin   = self.session.get_risk_pin()
        color = {"SEGURO": "green", "RIESGO_ALTO": "yellow", "VETADO": "red"}[pin["level"]]
        t = Text()
        t.append(f"  {'☀️  ABIERTO' if self.session.is_market_open() else '🌙 CERRADO'}\n\n", style="bold")
        t.append(f"  ⏳ Cierre en  : ", style="dim")
        t.append(f"{self.session.get_countdown_str()}\n", style="bold cyan")
        t.append(f"  {self.session.get_progress_bar(22)}\n\n", style="cyan")
        t.append(f"  🛡️  PIN         : ", style="dim")
        t.append(f"{pin['level']}\n", style=f"bold {color}")
        t.append(f"  {pin['reason']}\n", style="dim")
        return Panel(t, title="[bold]⏰ SESIÓN NY[/bold]", border_style=color)

    def _macro_panel(self) -> Panel:
        auth, reason = self.macro.authorize_trade()
        next_ev = self.macro.get_next_event()
        t = Text()
        if auth:
            t.append("  ✅ OPERACIONES PERMITIDAS\n", style="bold green")
        else:
            t.append("  🛑 BLOQUEADO\n", style="bold red")
            t.append(f"  {reason}\n", style="dim red")
        if next_ev:
            t.append(f"\n  ⏰ Próximo: ", style="dim")
            t.append(f"{next_ev['time'].strftime('%H:%M')} {next_ev['name']}\n", style="yellow")
        else:
            t.append("\n  Sin eventos críticos hoy\n", style="dim green")
        return Panel(t, title="[bold]🌍 FILTRO MACRO[/bold]", border_style="blue")

    def _signals_panel(self) -> Panel:
        table = Table(show_header=True, header_style="bold magenta", expand=True)
        table.add_column("Activo", width=10)
        table.add_column("Dir.", width=6)
        table.add_column("Conf.", width=7)
        table.add_column("Veto", width=24)
        if not self.signals:
            table.add_row("—", "—", "—", "Esperando señales...")
        else:
            for sig in self.signals[-6:]:
                dc = "green" if sig.get("direction") == "BUY" else ("red" if sig.get("direction") == "SELL" else "yellow")
                vc = "green" if sig.get("authorized") else "red"
                table.add_row(
                    sig.get("symbol", "?"),
                    Text(sig.get("direction", "HOLD"), style=f"bold {dc}"),
                    f"{sig.get('confidence', 0):.0f}%",
                    Text(str(sig.get("veto_reason", ""))[:24], style=vc),
                )
        return Panel(table, title="[bold]🎯 SEÑALES EN VIVO[/bold]", border_style="magenta")

    def _whale_panel(self) -> Panel:
        t = Text()
        state = self.whale._state
        if state:
            for sym, sent in state.items():
                bar   = "█" * int(abs(sent) * 10) + "░" * (10 - int(abs(sent) * 10))
                color = "green" if sent >= 0 else "red"
                t.append(f"  {sym:<8} ", style="dim")
                t.append(f"[{bar}] {sent:+.2f}\n", style=color)
        else:
            t.append("  Cargando datos institucionales...\n", style="dim")
        return Panel(t, title="[bold]🦈 TIBURONES[/bold]", border_style="yellow")

    def _log_panel(self) -> Panel:
        t = Text()
        for line in (self.log_lines or ["  Sistema en línea..."]):
            t.append(f"  {line}\n", style="dim")
        return Panel(t, title="[bold]📋 LOG[/bold]", border_style="dim")

    def build(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=5),
        )
        layout["body"].split_row(Layout(name="left", ratio=1), Layout(name="right", ratio=2))
        layout["left"].split_column(Layout(name="session"), Layout(name="macro"))
        layout["right"].split_column(Layout(name="signals"), Layout(name="whale"))
        layout["header"].update(self._header())
        layout["session"].update(self._session_panel())
        layout["macro"].update(self._macro_panel())
        layout["signals"].update(self._signals_panel())
        layout["whale"].update(self._whale_panel())
        layout["footer"].update(self._log_panel())
        return layout

    async def run(self, refresh: float = 1.0) -> None:
        with Live(self.build(), refresh_per_second=1, screen=True) as live:
            while True:
                try:
                    live.update(self.build())
                    await asyncio.sleep(refresh)
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.error("Dashboard: %s", exc)
                    await asyncio.sleep(refresh)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        print(f"❌ No se encontró {CONFIG_FILE}")
        print("   Copia config_template.json → config_alpaca.json y rellena tus credenciales")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return json.load(f)


async def main() -> None:
    config  = load_config()
    symbols = config.get("trading_params", {}).get("simbolos", ["SPY", "QQQ"])

    print("🧠 Iniciando Cerebro Quant v5...")

    session    = SessionManager()
    price_feed = PriceFeed(config)
    macro      = MacroFilter(config)
    forense    = ForenseLogger()
    whale      = WhaleTracker(config)
    veto       = VetoEngine(session, macro, forense, whale)
    trading    = TradingEngine(config, veto, price_feed, forense)
    bot        = TelegramBot(config, session, macro, forense, veto)
    dashboard  = Dashboard(session, macro, forense, whale, veto)

    # Cablear callbacks de alerta
    macro.telegram_alert_fn   = bot.send_alert
    forense.telegram_alert_fn = bot.send_alert
    whale.telegram_alert_fn   = bot.send_alert

    # Conectar señales al dashboard
    trading.on_signal = lambda sig: dashboard.signals.append(sig)
    trading.on_log    = dashboard.push_log

    # Calendario inicial
    try:
        await macro.load_calendar_finnhub()
    except Exception as exc:
        logger.warning("Calendario macro inicial: %s", exc)

    print(f"✅ Sistemas listos. Activos: {symbols}")

    await asyncio.gather(
        dashboard.run(),
        macro.monitor_loop(),
        whale.run_loop(symbols),
        trading.scan_and_trade(symbols),
        bot.run_polling(),
        return_exceptions=True,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n💤 Cerebro Quant v5 detenido")
