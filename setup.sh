#!/usr/bin/env bash
set -euo pipefail

# ── Trader's Cockpit — Setup Script ────────────────────────────────────────
# Run this once on your EC2 instance to set up the dashboard system.
# Usage: bash setup.sh

COCKPIT_DIR="/opt/cockpit"
OUTPUT_DIR="/var/www/cockpit"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "═══════════════════════════════════════════════════════"
echo "  Trader's Cockpit — Setup"
echo "═══════════════════════════════════════════════════════"

# 1. Create installation directory
echo "[1/7] Creating directories..."
sudo mkdir -p "$COCKPIT_DIR"
sudo mkdir -p "$OUTPUT_DIR"
sudo mkdir -p "$COCKPIT_DIR/manual_data"

# 2. Copy project files
echo "[2/7] Copying project files..."
sudo cp "$SCRIPT_DIR/config.py" "$COCKPIT_DIR/"
sudo cp "$SCRIPT_DIR/collect_data.py" "$COCKPIT_DIR/"
sudo cp "$SCRIPT_DIR/render.py" "$COCKPIT_DIR/"
sudo cp "$SCRIPT_DIR/generate.py" "$COCKPIT_DIR/"
sudo cp "$SCRIPT_DIR/requirements.txt" "$COCKPIT_DIR/"
sudo cp -r "$SCRIPT_DIR/templates" "$COCKPIT_DIR/"
sudo cp -r "$SCRIPT_DIR/static" "$COCKPIT_DIR/"

# 3. Create virtualenv and install deps
echo "[3/7] Setting up Python virtual environment..."
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install Python 3.10+ first."
    exit 1
fi
sudo python3 -m venv "$COCKPIT_DIR/venv"
sudo "$COCKPIT_DIR/venv/bin/pip" install --upgrade pip
sudo "$COCKPIT_DIR/venv/bin/pip" install -r "$COCKPIT_DIR/requirements.txt"

# 4. Copy static files to output
echo "[4/7] Copying static files to output directory..."
sudo cp "$COCKPIT_DIR/static/style.css" "$OUTPUT_DIR/"

# 5. Set up systemd (optional)
echo "[5/7] Installing systemd units..."
if [ -d /etc/systemd/system ]; then
    sudo cp "$SCRIPT_DIR/cockpit.service" /etc/systemd/system/
    sudo cp "$SCRIPT_DIR/cockpit.timer" /etc/systemd/system/
    sudo systemctl daemon-reload
    echo "  Systemd units installed. Enable with:"
    echo "    sudo systemctl enable --now cockpit.timer"
else
    echo "  systemd not found — skipping. Use cron instead:"
    echo "  30 8 * * 1-5 $COCKPIT_DIR/venv/bin/python $COCKPIT_DIR/generate.py"
fi

# 6. Nginx config
echo "[6/7] Nginx configuration..."
if [ -d /etc/nginx/sites-available ]; then
    sudo cp "$SCRIPT_DIR/nginx.conf" /etc/nginx/sites-available/cockpit
    if [ ! -L /etc/nginx/sites-enabled/cockpit ]; then
        sudo ln -s /etc/nginx/sites-available/cockpit /etc/nginx/sites-enabled/cockpit
    fi
    echo "  Nginx config installed. Test and reload:"
    echo "    sudo nginx -t && sudo systemctl reload nginx"
else
    echo "  Nginx not found — skipping. Install nginx and re-run,"
    echo "  or manually serve $OUTPUT_DIR."
fi

# 7. First run (dry run without API key)
echo "[7/7] Test generation..."
if [ -n "${FMP_API_KEY:-}" ]; then
    sudo -E "$COCKPIT_DIR/venv/bin/python" "$COCKPIT_DIR/generate.py"
    echo "  Dashboard generated at $OUTPUT_DIR/index.html"
else
    echo "  Skipping live generation (FMP_API_KEY not set)."
    echo "  To test with sample data:"
    echo "    $COCKPIT_DIR/venv/bin/python $COCKPIT_DIR/generate.py --from-json /path/to/market_data.json --output-dir $OUTPUT_DIR"
fi

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  Setup complete!"
echo "═══════════════════════════════════════════════════════"
echo ""
echo "Next steps:"
echo "  1. Set your API key:"
echo "     export FMP_API_KEY='your-key-here'"
echo "     # Or add to /etc/environment or cockpit.service"
echo ""
echo "  2. Enable scheduled updates:"
echo "     sudo systemctl enable --now cockpit.timer"
echo ""
echo "  3. Enable nginx:"
echo "     sudo nginx -t && sudo systemctl reload nginx"
echo ""
echo "  4. Manual data overrides:"
echo "     Drop JSON files in $COCKPIT_DIR/manual_data/"
echo "     Supported: dark_pool.json, gamma_squeeze.json,"
echo "     options.json, wsb.json, sentiment.json,"
echo "     trade_setups.json, watchlist_additions.json,"
echo "     market_regime.json, skew_data.json"
echo ""
echo "  5. Test a generation:"
echo "     $COCKPIT_DIR/venv/bin/python $COCKPIT_DIR/generate.py"
echo ""
