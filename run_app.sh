#!/usr/bin/env bash
# Launch the EfficientAssetRipper GUI (PySide6) in the background and verify it.
#
# Usage: ./run_app.sh
set -u

echo " Launching EfficientAssetRipper GUI..."
echo

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

LOG=/tmp/opencode/app_run.log
mkdir -p "$(dirname "$LOG")"

if [ ! -f main.py ]; then
    echo "ERROR: main.py not found in $SCRIPT_DIR (run this from the repo root)."
    exit 1
fi

if [ ! -f venv/bin/activate ]; then
    echo "ERROR: venv not found. Create it first:"
    echo "  python -m venv venv"
    echo "  source venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

source venv/bin/activate

# The GUI must run in the background so this script (and any caller) returns.
# Verify by PID, never with pgrep -f "python main.py" (matches the checking
# shell itself and reports phantom processes).
nohup python main.py > "$LOG" 2>&1 &
APP_PID=$!
echo "PID: $APP_PID"
sleep 3

if ps -p "$APP_PID" > /dev/null; then
    echo "RUNNING"
    status=0
else
    echo "NOT_RUNNING"
    status=1
fi
