#!/usr/bin/env python3
"""
MÓDULO DASHBOARD — Cerebro Quant v5
Terminal profesional estilo Bloomberg usando Rich
"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.console import Console
from rich.align import Align

logger = logging.getLogger("Dashboard")


class Dashboard:
    def __init__(self, session, macro, forense, whale, veto):
        self.session = session
        self.macro = macro
        self.forense = forense
        self.whale = whale
        self.veto = veto
        self.console = Console()
        self.log_lines: list = []
        self.signals: list = []  # populated by trading engine externally

    def push_log(self, line: str):
        self.log_lines.append(line)
        self.log_lines = self.log_lines[-8:]

    # ─── PANELS ──────────────────────────────────────────────────────────────

    def _panel_header(self) -> Panel:
        ny = self.session.get_ny_now()
        title = Text("🧠  CEREBRO QUANT v5 — PRODUCTION", style="bold white on dark_blue")
        subtitle = Text(f"  NY: {ny.strftime('%Y-%m-%d  %I:%M:%S %p')}  │  Powered by 5 IAs + Groq LLaMA", style="dim")
        return Panel(Align.center(title + Text("\n") + subtitle), style="bold blue")

    def _panel_session(self) -> Panel:
        pin = self.session.get_risk_pin()
        bar = self.session.get_progress_bar(width=22)
        countdown = self.session.get_countdown_str()
        open_status = "☀️  ABIERTO" if self.session.is_market_open() else "🌙 CERRADO"

        pin_colors = {"SEGURO": "green", "RIESGO_ALTO": "yellow", "VETADO": "red"}
        color = pin_colors.get(pin["level"], "white")

        t = Text()
        t.append(f"  {open_status}\n\n", style="bold")
        t.append(f"  ⏳ Cierre en  : ", style="dim")
        t.append(f"{countdown}\n", style="bold cyan")
        t.append(f"  {bar}\n\n", style="cyan")
        t.append(f"  🛡️  PIN         : ", style="dim")
        t.append(f"{pin['level']}\n", style=f"bold {color}")
        t.append(f"  Razón         : {pin['reason']}\n", style="dim")

        return Panel(t, title="[bold]⏰ SESIÓN NY[/bold]", border_style=color)

    def _panel_macro(self) -> Panel:
        auth, reason = self.macro.authorize_trade()
        next_ev = self.macro.get_next_event()

        t = Text()
        if auth:
            t.append("  ✅ OPERACIONES PERMITIDAS\n", style="bold green")
        else:
            t.append("  🛑 BLOQUEADO\n", style="bold red")
            t.append(f"  {reason}\n", style="dim red")

        t.append("\n")
        if next_ev:
            t.append(f"  ⏰ Próximo: ", style="dim")
            t.append(f"{next_ev.get('time','?')} {next_ev.get('event','?')}\n", style="yellow")
            t.append(f"  Impacto : {next_ev.get('impact','?')}\n", style="dim")
        else:
            t.append("  Sin eventos críticos hoy\n", style="dim green")

        return Panel(t, title="[bold]🌍 FILTRO MACRO[/bold]", border_style="blue")

    def _panel_signals(self) -> Panel:
        table = Table(show_header=True, header_style="bold magenta", expand=True)
        table.add_column("Activo", style="cyan", width=10)
        table.add_column("Dir.", width=6)
        table.add_column("Conf.", width=7)
        table.add_column("Veto", width=22)

        if not self.signals:
            table.add_row("—", "—", "—", "Esperando señales...")
        else:
            for sig in self.signals[-6:]:
                dir_color = "green" if sig.get("direction") == "BUY" else ("red" if sig.get("direction") == "SELL" else "yellow")
                veto_color = "green" if sig.get("authorized") else "red"
                table.add_row(
                    sig.get("symbol", "?"),
                    Text(sig.get("direction", "HOLD"), style=f"bold {dir_color}"),
                    f"{sig.get('confidence', 0):.0f}%",
                    Text(sig.get("veto_reason", "OK")[:22], style=veto_color)
                )

        return Panel(table, title="[bold]🎯 SEÑALES EN VIVO[/bold]", border_style="magenta")

    def _panel_whale(self) -> Panel:
        t = Text()
        try:
            import json, os
            from pathlib import Path
            state_file = Path(__file__).parent.parent / "brain" / "whale_flow.json"
            if state_file.exists():
                with open(state_file) as f:
                    state = json.load(f)
                for sym, sent in state.get("sentiments", {}).items():
                    bar = "█" * int(abs(sent) * 10) + "░" * (10 - int(abs(sent) * 10))
                    color = "green" if sent >= 0 else "red"
                    t.append(f"  {sym:<8} ", style="dim")
                    t.append(f"[{bar}] {sent:+.2f}\n", style=color)
            else:
                t.append("  Sin datos institucionales\n", style="dim")
        except Exception:
            t.append("  Cargando...\n", style="dim")

        return Panel(t, title="[bold]🦈 TIBURONES[/bold]", border_style="yellow")

    def _panel_log(self) -> Panel:
        t = Text()
        for line in self.log_lines:
            t.append(f"  {line}\n", style="dim")
        if not self.log_lines:
            t.append("  Sistema en línea...\n", style="dim")
        return Panel(t, title="[bold]📋 LOG DEL SISTEMA[/bold]", border_style="dim")

    # ─── LAYOUT ──────────────────────────────────────────────────────────────

    def build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=4),
            Layout(name="body"),
            Layout(name="footer", size=6),
        )
        layout["body"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="right", ratio=2),
        )
        layout["left"].split_column(
            Layout(name="session"),
            Layout(name="macro"),
        )
        layout["right"].split_column(
            Layout(name="signals"),
            Layout(name="whale"),
        )

        layout["header"].update(self._panel_header())
        layout["session"].update(self._panel_session())
        layout["macro"].update(self._panel_macro())
        layout["signals"].update(self._panel_signals())
        layout["whale"].update(self._panel_whale())
        layout["footer"].update(self._panel_log())

        return layout

    # ─── MAIN LOOP ────────────────────────────────────────────────────────────

    async def run(self, refresh_interval: float = 1.0):
        logger.info("🖥️  Dashboard iniciando...")
        with Live(self.build_layout(), refresh_per_second=1, screen=True) as live:
            while True:
                try:
                    live.update(self.build_layout())
                    await asyncio.sleep(refresh_interval)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Dashboard error: {e}")
                    await asyncio.sleep(refresh_interval)
