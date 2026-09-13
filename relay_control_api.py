"""Compact HTTP control API between the radio relay process and the website.

`RelayControlServer` runs inside Crossband_Relay.py's process and exposes
health/send actions on localhost. `RelayControlClient` is the thin client
used by CaveRelay_Website/app.py to reach it, instead of importing
CrossbandRelay directly and reaching into its internals.
"""

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from config import settings

log = logging.getLogger("relay_control_api")


class RelayControlServer:
    """Expose a CrossbandRelay instance's health/send actions over HTTP."""

    def __init__(self, relay, host=None, port=None):
        host = settings.relay_api_host if host is None else host
        port = settings.relay_api_port if port is None else port
        self._httpd = ThreadingHTTPServer((host, port), _build_handler(relay))
        self._thread = None

    def start(self):
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="relay-control-api", daemon=True
        )
        self._thread.start()
        log.info("Relay control API listening on %s:%d", *self._httpd.server_address[:2])

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


def _build_handler(relay):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_, *args):
            log.debug(format_, *args)

        def _send_json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length == 0:
                return {}
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/health":
                self._send_json(200, relay.health_snapshot())
            elif parsed.path == "/sn_history":
                window = urllib.parse.parse_qs(parsed.query).get("window", [None])[0]
                window_seconds = float(window) if window else None
                self._send_json(200, relay.sn_history_snapshot(window_seconds))
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):
            try:
                data = self._read_json()
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON body"})
                return

            if self.path == "/send/aprs":
                self._handle_send_aprs(data)
            elif self.path == "/send/fldigi":
                self._handle_send_fldigi(data)
            else:
                self._send_json(404, {"error": "not found"})

        def _handle_send_aprs(self, data):
            destination = str(data.get("destination", "")).strip()
            message = str(data.get("message", "")).strip()
            if not destination or not message:
                self._send_json(400, {"error": "destination and message are required"})
                return
            try:
                result = relay.send_aprs_message(destination, message, message_id=data.get("message_id"))
            except (OSError, RuntimeError, ValueError) as error:
                self._send_json(503, {"error": str(error)})
                return
            self._send_json(200, {
                "ok": True,
                "packets": len(result),
                "message_id": result.message_id,
                "attempts": result.attempts,
                "acknowledged": result.acknowledged,
            })

        def _handle_send_fldigi(self, data):
            message = str(data.get("message", "")).strip()
            via = data.get("via") or None
            if not message:
                self._send_json(400, {"error": "message is required"})
                return
            try:
                wire_message = relay.send_fldigi_message(message, via=via)
            except (OSError, RuntimeError, ValueError) as error:
                self._send_json(503, {"error": str(error)})
                return
            self._send_json(200, {"ok": True, "wire_message": wire_message})

    return Handler


class RelayControlClient:
    """Thin HTTP client used by the website to reach the radio relay process."""

    def __init__(self, host=None, port=None, timeout=10):
        self.host = settings.relay_api_host if host is None else host
        self.port = settings.relay_api_port if port is None else port
        self.timeout = timeout

    def health(self):
        return self._request("GET", "/health")

    def sn_history(self, window_seconds=None):
        path = "/sn_history" if not window_seconds else f"/sn_history?window={window_seconds}"
        return self._request("GET", path)

    def send_aprs(self, destination, message, message_id=None):
        return self._request("POST", "/send/aprs", {
            "destination": destination, "message": message, "message_id": message_id,
        })

    def send_fldigi(self, message, via=None):
        return self._request("POST", "/send/fldigi", {"message": message, "via": via})

    def _request(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(f"http://{self.host}:{self.port}{path}", data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            return json.loads(error.read())
        except urllib.error.URLError as error:
            raise ConnectionError(
                f"Unable to reach relay control API at {self.host}:{self.port}: {error}"
            ) from error
