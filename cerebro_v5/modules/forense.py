"""
Cerebro Quant v5 — Forense Logger
Records losing trades, computes similarity to past losses, and manages the
forensic veto mechanism to avoid repeating losing patterns.
"""

import asyncio
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import aiofiles

logger = logging.getLogger(__name__)


class ForenseLogger:
    """
    Forensic trade logger: stores losing trades, computes Euclidean similarity,
    and provides veto signals when the current market context closely matches
    historical losses.
    """

    def __init__(self, base_dir: str, telegram_alert_fn: Optional[Callable] = None) -> None:
        self.base_dir = Path(base_dir)
        self.telegram_alert_fn = telegram_alert_fn
        self._lock = asyncio.Lock()
        self.max_records = 500

        brain_dir = self.base_dir / "brain"
        brain_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = brain_dir / "perdidas_forense.json"

    # ──────────────────────────────────────────────────────────────────────────
    # File I/O
    # ──────────────────────────────────────────────────────────────────────────

    async def load(self) -> list:
        """Load records from JSON file. Returns [] if file is missing or corrupt."""
        try:
            if not self.file_path.exists():
                return []
            async with aiofiles.open(self.file_path, "r", encoding="utf-8") as fh:
                content = await fh.read()
            if not content.strip():
                return []
            records = json.loads(content)
            if not isinstance(records, list):
                logger.warning("ForenseLogger: file contains non-list data — resetting")
                return []
            return records
        except (json.JSONDecodeError, IOError) as exc:
            logger.error("ForenseLogger.load error: %s", exc)
            return []
        except Exception as exc:
            logger.error("ForenseLogger.load unexpected error: %s", exc)
            return []

    async def save(self, records: list) -> None:
        """Save records to JSON file with lock. Truncates to max_records (newest first)."""
        async with self._lock:
            try:
                # Keep only the most recent max_records entries
                truncated = records[-self.max_records:]
                content = json.dumps(truncated, indent=2, default=str, ensure_ascii=False)
                async with aiofiles.open(self.file_path, "w", encoding="utf-8") as fh:
                    await fh.write(content)
                logger.debug("ForenseLogger: saved %d records", len(truncated))
            except (IOError, TypeError) as exc:
                logger.error("ForenseLogger.save error: %s", exc)
            except Exception as exc:
                logger.error("ForenseLogger.save unexpected error: %s", exc)

    # ──────────────────────────────────────────────────────────────────────────
    # Trade registration
    # ──────────────────────────────────────────────────────────────────────────

    async def registrar_perdida(self, trade: dict) -> bool:
        """
        Record a losing trade.
        Adds a UTC timestamp, appends to the store, and alerts via Telegram if configured.
        Returns True on success, False on error.
        """
        try:
            record = dict(trade)
            record["timestamp"] = datetime.now(timezone.utc).isoformat()

            # Ensure required fields exist with defaults
            record.setdefault("volatilidad", 0.0)
            record.setdefault("sentimiento", 0.0)
            record.setdefault("rsi", 50.0)
            record.setdefault("macd", 0.0)
            record.setdefault("symbol", "UNKNOWN")
            record.setdefault("ia", "UNKNOWN")
            record.setdefault("pnl", 0.0)

            records = await self.load()
            records.append(record)
            await self.save(records)

            symbol = record.get("symbol", "?")
            pnl = record.get("pnl", 0.0)
            ia = record.get("ia", "?")
            msg = (
                f"🔴 PÉRDIDA REGISTRADA\n"
                f"Símbolo : {symbol}\n"
                f"P&L     : {pnl:.2f}\n"
                f"IA      : {ia}\n"
                f"Hora    : {record['timestamp']}"
            )
            logger.warning("ForenseLogger: %s", msg.replace("\n", " | "))

            if self.telegram_alert_fn is not None:
                try:
                    await self.telegram_alert_fn(msg)
                except Exception as alert_exc:
                    logger.error("ForenseLogger: telegram alert failed: %s", alert_exc)

            return True

        except Exception as exc:
            logger.error("ForenseLogger.registrar_perdida error: %s", exc)
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # Similarity computation
    # ──────────────────────────────────────────────────────────────────────────

    def _normalize_context(self, ctx: dict) -> list:
        """
        Normalize market context to a 4-element feature vector in [0, 1].
        Fields: volatilidad (0-100), sentimiento (-1 to +1), rsi (0-100), macd (-5 to +5).
        """
        vol = float(ctx.get("volatilidad", 0.0))
        sent = float(ctx.get("sentimiento", 0.0))
        rsi = float(ctx.get("rsi", 50.0))
        macd = float(ctx.get("macd", 0.0))

        # Normalize each feature to [0, 1]
        vol_n = max(0.0, min(1.0, vol / 100.0))
        sent_n = max(0.0, min(1.0, (sent + 1.0) / 2.0))
        rsi_n = max(0.0, min(1.0, rsi / 100.0))
        macd_n = max(0.0, min(1.0, (macd + 5.0) / 10.0))

        return [vol_n, sent_n, rsi_n, macd_n]

    def _euclidean_distance(self, a: list, b: list) -> float:
        """Compute Euclidean distance between two equal-length vectors."""
        return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))

    async def calcular_similitud_euclidiana(self, contexto_actual: dict) -> tuple:
        """
        Compute max Euclidean similarity (0-100) between contexto_actual and
        every stored losing trade.

        Returns (max_similarity, matching_trade_or_None).
        max_similarity=0 means no records found.
        """
        try:
            records = await self.load()
            if not records:
                return 0.0, None

            current_vec = self._normalize_context(contexto_actual)

            # Max possible distance for 4 normalized features: sqrt(4) = 2.0
            max_possible_distance = math.sqrt(4)

            best_sim = 0.0
            best_trade = None

            for record in records:
                try:
                    record_vec = self._normalize_context(record)
                    dist = self._euclidean_distance(current_vec, record_vec)
                    # Convert distance to similarity: 0 dist = 100%, max dist = 0%
                    sim = (1.0 - dist / max_possible_distance) * 100.0
                    sim = max(0.0, min(100.0, sim))
                    if sim > best_sim:
                        best_sim = sim
                        best_trade = record
                except Exception as record_exc:
                    logger.debug("ForenseLogger: record similarity error: %s", record_exc)
                    continue

            return round(best_sim, 2), best_trade

        except Exception as exc:
            logger.error("ForenseLogger.calcular_similitud_euclidiana error: %s", exc)
            return 0.0, None

    async def evaluar_veto(self, contexto: dict) -> tuple:
        """
        Return (True, reason) if the context matches a historical loss with >= 90% similarity.
        Otherwise (False, "OK").
        """
        try:
            sim, trade = await self.calcular_similitud_euclidiana(contexto)
            if sim >= 90.0:
                symbol = trade.get("symbol", "?") if trade else "?"
                pnl = trade.get("pnl", 0.0) if trade else 0.0
                ts = trade.get("timestamp", "?") if trade else "?"
                reason = (
                    f"Match {sim:.1f}% con pérdida histórica — "
                    f"{symbol} P&L={pnl:.2f} [{ts[:10]}]"
                )
                logger.warning("ForenseLogger veto: %s", reason)
                return True, reason
            return False, "OK"
        except Exception as exc:
            logger.error("ForenseLogger.evaluar_veto error: %s", exc)
            return False, f"Error en evaluar_veto: {exc}"

    # ──────────────────────────────────────────────────────────────────────────
    # Reporting
    # ──────────────────────────────────────────────────────────────────────────

    async def generar_reporte(self) -> str:
        """Generate a formatted forensic report with stats, IA breakdown, and distributions."""
        try:
            records = await self.load()
            total = len(records)

            if total == 0:
                lines = [
                    "═" * 50,
                    "      🔬  FORENSE — REPORTE DE PÉRDIDAS",
                    "═" * 50,
                    "  Sin registros de pérdidas todavía.",
                    "═" * 50,
                ]
                return "\n".join(lines)

            pnls = [float(r.get("pnl", 0.0)) for r in records]
            total_loss = sum(pnls)
            avg_loss = total_loss / total
            worst = min(pnls)

            # Count losses by IA
            ia_counts: dict = {}
            for r in records:
                ia = r.get("ia", "UNKNOWN")
                ia_counts[ia] = ia_counts.get(ia, 0) + 1
            top_ia = sorted(ia_counts.items(), key=lambda x: x[1], reverse=True)[:5]

            # RSI distribution buckets
            rsi_buckets = {"<30": 0, "30-50": 0, "50-70": 0, ">70": 0}
            for r in records:
                rsi = float(r.get("rsi", 50.0))
                if rsi < 30:
                    rsi_buckets["<30"] += 1
                elif rsi < 50:
                    rsi_buckets["30-50"] += 1
                elif rsi < 70:
                    rsi_buckets["50-70"] += 1
                else:
                    rsi_buckets[">70"] += 1

            # Volatility distribution
            vol_buckets = {"<20%": 0, "20-50%": 0, ">50%": 0}
            for r in records:
                vol = float(r.get("volatilidad", 0.0))
                if vol < 20:
                    vol_buckets["<20%"] += 1
                elif vol < 50:
                    vol_buckets["20-50%"] += 1
                else:
                    vol_buckets[">50%"] += 1

            lines = [
                "═" * 50,
                "      🔬  FORENSE — REPORTE DE PÉRDIDAS",
                "═" * 50,
                f"  Total registros   : {total}",
                f"  Pérdida total     : {total_loss:.2f}",
                f"  Pérdida promedio  : {avg_loss:.2f}",
                f"  Peor operación    : {worst:.2f}",
                "─" * 50,
                "  🤖  TOP IAs CON MÁS PÉRDIDAS:",
            ]
            for ia_name, count in top_ia:
                lines.append(f"    {ia_name:20s} : {count} pérdidas")

            lines += [
                "─" * 50,
                "  📊  DISTRIBUCIÓN RSI:",
            ]
            for bucket, count in rsi_buckets.items():
                pct = (count / total * 100) if total else 0
                lines.append(f"    RSI {bucket:8s} : {count:3d} ({pct:.1f}%)")

            lines += [
                "─" * 50,
                "  📊  DISTRIBUCIÓN VOLATILIDAD:",
            ]
            for bucket, count in vol_buckets.items():
                pct = (count / total * 100) if total else 0
                lines.append(f"    Vol {bucket:8s} : {count:3d} ({pct:.1f}%)")

            lines.append("═" * 50)
            return "\n".join(lines)

        except Exception as exc:
            logger.error("ForenseLogger.generar_reporte error: %s", exc)
            return f"Error generando reporte forense: {exc}"

    # ──────────────────────────────────────────────────────────────────────────
    # HuggingFace Dataset Export
    # ──────────────────────────────────────────────────────────────────────────

    async def exportar_hf_dataset(self, hf_token_env: str = "HF_TOKEN") -> None:
        """
        Export losing trades as a negative training set.
        Reads HF token from the environment variable named by hf_token_env — never hardcoded.
        Saves the prepared dataset to brain/dataset_hf.json (does NOT upload automatically).
        """
        try:
            hf_token = os.environ.get(hf_token_env, "")
            if not hf_token:
                logger.warning(
                    "ForenseLogger.exportar_hf_dataset: env var '%s' not set — "
                    "dataset will be prepared but cannot be uploaded without the token.",
                    hf_token_env,
                )

            records = await self.load()
            dataset = {
                "description": "Cerebro Quant v5 — Negative training set (losing trades)",
                "version": "1.0",
                "total_records": len(records),
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "hf_token_env_var": hf_token_env,
                "features": ["volatilidad", "sentimiento", "rsi", "macd"],
                "label": "loss",
                "records": records,
                "upload_note": (
                    "To upload this dataset to HuggingFace Hub, set the environment variable "
                    f"'{hf_token_env}' and use the `datasets` library: "
                    "Dataset.from_list(records).push_to_hub('your-repo-id', token=os.environ[hf_token_env])"
                ),
            }

            dataset_path = self.base_dir / "brain" / "dataset_hf.json"
            content = json.dumps(dataset, indent=2, default=str, ensure_ascii=False)
            async with aiofiles.open(dataset_path, "w", encoding="utf-8") as fh:
                await fh.write(content)

            logger.info(
                "ForenseLogger: HF dataset prepared at %s (%d records)",
                dataset_path,
                len(records),
            )

        except Exception as exc:
            logger.error("ForenseLogger.exportar_hf_dataset error: %s", exc)
