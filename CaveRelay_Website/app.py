"""Cave Relay operations dashboard and HTTP API."""

import json
import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, jsonify, render_template, request

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import settings  # noqa: E402
from relay_control_api import RelayControlClient  # noqa: E402

OPERATIONS_LOG_PATH = Path(
    os.environ.get("CAVE_RELAY_LOG_PATH", PROJECT_ROOT / "logs" / "cave_relay.jsonl")
)


class JsonLogFormatter(logging.Formatter):
    """Format Python log records for reliable filtering in the dashboard."""

    def format(self, record):
        entry = {
            "timestamp": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds"),
            "level": record.levelname,
            "source": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def configure_operations_logging():
    """Persist logs from all relay components without duplicating handlers."""
    OPERATIONS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    log_path = str(OPERATIONS_LOG_PATH.resolve())
    if any(getattr(handler, "baseFilename", None) == log_path for handler in root_logger.handlers):
        return
    handler = RotatingFileHandler(OPERATIONS_LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(JsonLogFormatter())
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)


def read_operations_log(limit=500, level=None, source=None):
    entries = []
    if not OPERATIONS_LOG_PATH.exists():
        return entries
    try:
        lines = OPERATIONS_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return entries
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if level and entry.get("level") != level:
            continue
        if source and entry.get("source") != source:
            continue
        entries.append(entry)
        if len(entries) >= limit:
            break
    return entries


configure_operations_logging()

app = Flask(__name__)
relay_client = RelayControlClient()
startup_log = logging.getLogger("CaveRelay_Website")


def read_log(limit=100):
    entries = []
    log_path = Path(settings.traffic_log_path)
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


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/logs")
def logs():
    return render_template("logs.html")


@app.get("/api/messages")
def messages():
    try:
        limit = min(max(int(request.args.get("limit", 100)), 1), 500)
    except ValueError:
        limit = 100
    return jsonify(read_log(limit))


@app.get("/api/logs")
def operation_logs():
    try:
        limit = min(max(int(request.args.get("limit", 500)), 1), 1000)
    except ValueError:
        limit = 500
    level = str(request.args.get("level", "")).upper() or None
    source = str(request.args.get("source", "")).strip() or None
    return jsonify(read_operations_log(limit, level, source))


@app.get("/api/health")
def health():
    try:
        return jsonify(relay_client.health())
    except ConnectionError as error:
        return jsonify({"error": str(error)}), 503


@app.post("/api/send/aprs")
def send_aprs():
    data = request.get_json(silent=True) or {}
    destination = str(data.get("destination", "")).strip()
    message = str(data.get("message", "")).strip()
    message_id = data.get("message_id")
    if not destination or not message:
        return jsonify({"error": "Destination and message are required."}), 400
    try:
        result = relay_client.send_aprs(destination, message, message_id=message_id or None)
    except ConnectionError as error:
        return jsonify({"error": str(error)}), 503
    status = 200 if result.get("ok") else 503
    return jsonify(result), status


@app.post("/api/send/fldigi")
def send_fldigi():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message", "")).strip()
    via = str(data.get("via", "")).strip() or None
    if not message:
        return jsonify({"error": "Message is required."}), 400
    try:
        result = relay_client.send_fldigi(message, via=via)
    except ConnectionError as error:
        return jsonify({"error": str(error)}), 503
    status = 200 if result.get("ok") else 503
    return jsonify(result), status


if __name__ == "__main__":
    app.run(host=os.environ.get("CAVE_RELAY_HOST", "0.0.0.0"), port=int(os.environ.get("CAVE_RELAY_PORT", "5000")), debug=False)
