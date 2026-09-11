"""Cave Relay operations dashboard and HTTP API."""

import json
import logging
import os
import sys
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Crossband_Relay import CrossbandRelay, TRAFFIC_LOG_PATH  # noqa: E402

app = Flask(__name__)
relay = CrossbandRelay()
relay_lock = threading.Lock()
startup_log = logging.getLogger("CaveRelay_Website")


def read_log(limit=100):
    entries = []
    log_path = Path(TRAFFIC_LOG_PATH)
    if not log_path.exists():
        return entries
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return entries
    for line in reversed(lines):
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(entries) >= limit:
            break
    return entries


def ensure_relay_started():
    with relay_lock:
        if relay._fldigi is None or not relay._aprs.running:
            relay.start()


def initialize_relay():
    """Start radio services independently of the first web request."""
    try:
        relay.run()
        startup_log.info("APRS and Fldigi services started")
    except Exception as error:
        relay._fldigi_last_error = str(error)
        startup_log.exception("Unable to start APRS/Fldigi services at website startup")


threading.Thread(target=initialize_relay, name="relay-startup", daemon=True).start()


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/messages")
def messages():
    try:
        limit = min(max(int(request.args.get("limit", 100)), 1), 500)
    except ValueError:
        limit = 100
    return jsonify(read_log(limit))


@app.get("/api/health")
def health():
    return jsonify(relay.health_snapshot())


@app.post("/api/send/aprs")
def send_aprs():
    data = request.get_json(silent=True) or {}
    destination = str(data.get("destination", "")).strip()
    message = str(data.get("message", "")).strip()
    message_id = data.get("message_id")
    if not destination or not message:
        return jsonify({"error": "Destination and message are required."}), 400
    try:
        ensure_relay_started()
        packets = relay._aprs.send_message(destination, message, message_id=message_id or None)
    except (OSError, RuntimeError, ValueError) as error:
        return jsonify({"error": str(error)}), 503
    return jsonify({"ok": True, "packets": len(packets)})


@app.post("/api/send/fldigi")
def send_fldigi():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message", "")).strip()
    via = str(data.get("via", "")).strip() or None
    if not message:
        return jsonify({"error": "Message is required."}), 400
    try:
        ensure_relay_started()
        wire_message = relay.transmit_radiomsg(
            message,
            from_call="SURF",
            to_call="*",
            via=via,
        )
    except (OSError, RuntimeError, ValueError) as error:
        return jsonify({"error": str(error)}), 503
    return jsonify({"ok": True, "wire_message": wire_message})


if __name__ == "__main__":
    app.run(host=os.environ.get("CAVE_RELAY_HOST", "127.0.0.1"), port=int(os.environ.get("CAVE_RELAY_PORT", "5000")), debug=False)
