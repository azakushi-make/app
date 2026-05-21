#!/usr/bin/env bash
# ══════════════════════════════════════════════
#  CEREBRO QUANT v5 — Instalador Automático
# ══════════════════════════════════════════════
set -e

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   🧠  CEREBRO QUANT v5  —  INSTALADOR    ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Detectar entorno ──────────────────────────
if [ -n "$TERMUX_VERSION" ]; then
    echo "📱 Detectado: Termux (Android)"
    pkg update -y && pkg install python3 -y 2>/dev/null || true
else
    echo "🖥️  Detectado: Linux/macOS"
fi

# ── Crear entorno virtual ─────────────────────
echo "📦 Creando entorno virtual..."
python3 -m venv env
source env/bin/activate

# ── Instalar dependencias ──────────────────────
echo "📥 Instalando dependencias..."
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "✅ Dependencias instaladas"

# ── Crear directorios ─────────────────────────
mkdir -p brain logs
echo "📁 Directorios creados: brain/ logs/"

# ── Config inicial ────────────────────────────
if [ ! -f "config_alpaca.json" ]; then
    cp config_template.json config_alpaca.json
    echo ""
    echo "⚠️  ACCIÓN REQUERIDA:"
    echo "   Edita config_alpaca.json con tus credenciales:"
    echo "   nano config_alpaca.json"
else
    echo "✅ config_alpaca.json ya existe"
fi

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   ✅  INSTALACIÓN COMPLETA               ║"
echo "║                                          ║"
echo "║   Para ejecutar:                         ║"
echo "║     source env/bin/activate              ║"
echo "║     python main.py                       ║"
echo "╚══════════════════════════════════════════╝"
echo ""
