#!/usr/bin/env bash
# Starts the full cave radio relay system: the radio relay process (fldigi +
# direwolf + APRS/RadioMSG relay) and the web dashboard, together.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

cd "$PROJECT_DIR"

"$PYTHON" Crossband_Relay.py >> "$LOG_DIR/relay.log" 2>&1 &
RELAY_PID=$!

"$PYTHON" CaveRelay_Website/app.py >> "$LOG_DIR/website.log" 2>&1 &
WEBSITE_PID=$!

cleanup() {
	echo "Stopping cave relay system..."
	kill "$RELAY_PID" "$WEBSITE_PID" 2>/dev/null || true
	wait "$RELAY_PID" "$WEBSITE_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Cave radio relay running (pid $RELAY_PID), logging to $LOG_DIR/relay.log"
echo "Web dashboard running (pid $WEBSITE_PID) at http://127.0.0.1:5000, logging to $LOG_DIR/website.log"
echo "Press Ctrl+C to stop both."

wait -n "$RELAY_PID" "$WEBSITE_PID"
