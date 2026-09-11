# Implements a crossband relay between Fldigi (using RadioMSG style messages) and APRS networks
# saves all traffic in a json log file for future use.

"""Crossband relay between Fldigi RadioMSG traffic and APRS."""

import dataclasses
from datetime import datetime
import json
import logging
import os
import re
import sys
import threading
import time

from aprs_relay import AprsService

# Allow this script to use the local pyFldigi source tree.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "pyFldigi"))

import pyfldigi

from QDX_Fldigi_coms import format_radiomsg, send_radiomsg
from radiomsg import RadioMsgParser, expected_checksum


SOURCE_CALLSIGN = "VE6LF"
APRS_DESTINATION_CALLSIGN = "VE6SDH"
FLDIGI_HOSTNAME = "127.0.0.1"
FLDIGI_PORT = 7362
FLDIGI_MODEM = "THOR4"
KISS_HOSTNAME = "127.0.0.1"
KISS_PORT = 8001
POLL_INTERVAL_SECONDS = 0.5
FLDIGI_RX_IDLE_SECONDS = 1.5
FLDIGI_RAW_PREVIEW_LIMIT = 4000
HEALTH_INTERVAL_SECONDS = 300
TRAFFIC_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "messages.jsonl")


def write_traffic_log(entry, log, received_at=None):
    received_at = received_at if received_at is not None else time.time()
    ordered_entry = {
        "received_at": datetime.fromtimestamp(received_at).strftime("%Y-%m-%d %H:%M:%S"),
        "transport": entry.get("transport", "unknown"),
    }
    ordered_entry.update(
        key_value for key_value in entry.items()
        if key_value[0] not in {"received_at", "transport"}
    )
    try:
        with open(TRAFFIC_LOG_PATH, "a", encoding="utf-8") as traffic_log:
            traffic_log.write(json.dumps(ordered_entry) + "\n")
    except OSError as error:
        log.error("Unable to write traffic log: %s", error)


def radio_msg_to_aprs_text(message):
    """Represent a RadioMSG block as one line for APRS segmentation."""
    return " ".join(message.raw.splitlines())


class CrossbandRelay:
    """Run the Fldigi receiver and APRS service together."""

    def __init__(self, power_cycle_hook=None):
        self.log = logging.getLogger("Crossband_Relay")
        self._stop_event = threading.Event()
        self._monitor_thread = None
        self._fldigi_last_ok = None
        self._fldigi_last_error = None
        self._power_cycle_hook = power_cycle_hook
        self._radio_parser = RadioMsgParser()
        self._rx_sn_samples = []
        self._rx_sn_status = []
        self._fldigi_raw_text = ""
        self._fldigi_last_rx_at = None
        self._fldigi_sn_status = None
        self._fldigi_sn_value = None
        self._fldigi = None
        self._aprs = AprsService(
            source=SOURCE_CALLSIGN,
            host=KISS_HOSTNAME,
            port=KISS_PORT,
            path=[],
            on_packet=self._handle_aprs_packet,
            on_error=self._handle_aprs_error,
        )

    def start(self):
        self._stop_event.clear()
        self.log.info("Starting APRS service")
        self._aprs.start()

        self.log.info("Connecting to Fldigi at %s:%d", FLDIGI_HOSTNAME, FLDIGI_PORT)
        self._fldigi = pyfldigi.Client(hostname=FLDIGI_HOSTNAME, port=FLDIGI_PORT)
        self.log.info("Fldigi version: %s", self._fldigi.name)
        if self._fldigi.modem.name != FLDIGI_MODEM:
            self.log.info("Switching Fldigi modem to %s", FLDIGI_MODEM)
            self._fldigi.modem.name = FLDIGI_MODEM
        self._fldigi_last_ok = time.time()
        self._fldigi_last_error = None
        if self._monitor_thread is None or not self._monitor_thread.is_alive():
            self._monitor_thread = threading.Thread(
                target=self._health_monitor_loop,
                name="relay-health",
                daemon=True,
            )
            self._monitor_thread.start()

    def health_snapshot(self):
        """Return current APRS, Fldigi, and relay health state."""
        fldigi = {
            "connected": self._fldigi is not None,
            "last_ok": self._fldigi_last_ok,
            "last_error": self._fldigi_last_error,
            "receiving": self._fldigi_sn_value is not None,
            "raw_text": self._fldigi_raw_text,
            "sn_status": self._fldigi_sn_status,
            "sn_value": self._fldigi_sn_value,
        }
        if self._fldigi is not None:
            try:
                fldigi["status"] = self._fldigi.main.status1
                fldigi["healthy"] = True
            except Exception as error:
                fldigi["healthy"] = False
                fldigi["last_error"] = str(error)
        else:
            fldigi["healthy"] = False
        return {
            "healthy": fldigi["healthy"] and self._aprs.connected and self._aprs.running,
            "fldigi": fldigi,
            "aprs": self._aprs.health_snapshot(),
        }

    def power_cycle(self, subsystem):
        """Invoke the configured hardware power-cycle hook for a subsystem."""
        if self._power_cycle_hook is None:
            raise RuntimeError("No power_cycle_hook is configured")
        if subsystem not in {"fldigi", "aprs", "radio"}:
            raise ValueError("subsystem must be fldigi, aprs, or radio")
        self._power_cycle_hook(subsystem)

    def _health_monitor_loop(self):
        while not self._stop_event.wait(HEALTH_INTERVAL_SECONDS):
            snapshot = self.health_snapshot()
            self.log.info("Health: %s", snapshot)
            if not snapshot["aprs"]["connected"] or not snapshot["aprs"]["running"]:
                self._restart_aprs()
            if not snapshot["fldigi"]["healthy"]:
                self._restart_fldigi()

    def _restart_aprs(self):
        self.log.warning("Restarting APRS KISS connection")
        try:
            self._aprs.stop()
            self._aprs.start()
        except (OSError, RuntimeError) as error:
            self.log.error("Unable to restart APRS: %s", error)

    def _restart_fldigi(self):
        self.log.warning("Restarting Fldigi XML-RPC connection")
        try:
            self._fldigi = pyfldigi.Client(hostname=FLDIGI_HOSTNAME, port=FLDIGI_PORT)
            if self._fldigi.modem.name != FLDIGI_MODEM:
                self._fldigi.modem.name = FLDIGI_MODEM
            self._fldigi_last_ok = time.time()
            self._fldigi_last_error = None
        except Exception as error:
            self._fldigi_last_error = str(error)
            self.log.error("Unable to restart Fldigi: %s", error)

    def run(self):
        """Poll Fldigi until stop() or Ctrl+C is requested."""
        if self._fldigi is None:
            self.start()
        self.log.info("Crossband relay is listening")
        try:
            while not self._stop_event.is_set():
                rx_data = self._fldigi.text.get_rx_data()
                self._fldigi_last_ok = time.time()
                sn_status = self._read_fldigi_sn()
                self._fldigi_sn_status = sn_status
                self._fldigi_sn_value = self._parse_sn_value(sn_status) if sn_status else None
                if self._fldigi_sn_value is None:
                    self._fldigi_raw_text = ""
                if rx_data:
                    if isinstance(rx_data, bytes):
                        rx_data = rx_data.decode("utf-8", errors="replace")
                    if self._fldigi_sn_value is not None:
                        self._fldigi_last_rx_at = time.time()
                        self._fldigi_raw_text = (
                            self._fldigi_raw_text + rx_data
                        )[-FLDIGI_RAW_PREVIEW_LIMIT:]
                    if sn_status is not None:
                        self._rx_sn_status.append(sn_status)
                        sn_value = self._parse_sn_value(sn_status)
                        if sn_value is not None:
                            self._rx_sn_samples.append(sn_value)
                    self.log.debug("Fldigi RX: %r", rx_data)
                    messages = self._radio_parser.feed(rx_data)
                    for index, message in enumerate(messages):
                        samples = self._rx_sn_samples if index == 0 else []
                        statuses = self._rx_sn_status if index == 0 else []
                        self._handle_radio_message(message, samples, statuses)
                    if messages:
                        self._fldigi_raw_text = ""
                        self._rx_sn_samples.clear()
                        self._rx_sn_status.clear()
                time.sleep(POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            self.log.info("Stopping after Ctrl+C")
        finally:
            self.stop()

    def stop(self):
        self._stop_event.set()
        self._aprs.stop()
        if self._monitor_thread is not None and self._monitor_thread is not threading.current_thread():
            self._monitor_thread.join(timeout=2)
        self._monitor_thread = None

    def _read_fldigi_sn(self):
        try:
            status = self._fldigi.main.status1
            if status is None or not str(status).strip():
                return None
            return str(status)
        except Exception as error:
            self.log.debug("Unable to read Fldigi S/N status: %s", error)
            return None

    @staticmethod
    def _parse_sn_value(status):
        match = re.search(r"[-+]?\d+(?:\.\d+)?", status)
        return float(match.group()) if match else None

    def _handle_radio_message(self, message, sn_samples=None, sn_status=None):
        log_entry = dataclasses.asdict(message)
        log_entry["transport"] = "fldigi_rx"
        sn_samples = sn_samples or []
        sn_status = sn_status or []
        log_entry["sn_average"] = (
            sum(sn_samples) / len(sn_samples) if sn_samples else None
        )
        log_entry["sn_samples"] = sn_samples
        log_entry["sn_status"] = sn_status[-1] if sn_status else None
        write_traffic_log(log_entry, self.log, message.received_at)

        if not message.checksum_valid:
            expected = expected_checksum(
                message.from_call,
                message.to_call,
                message.message,
                message.via,
                message.rly,
                message.msg_id,
                message.position,
                message.picture,
                message.received_date,
                message.received_offset,
                message.time_sync,
            )
            self.log.warning(
                "Relaying RadioMSG with checksum mismatch: received=%s expected=%s raw=%s",
                message.checksum,
                expected,
                message.raw,
            )
        try:
            aprs_text = radio_msg_to_aprs_text(message)
            self._aprs.send_message(APRS_DESTINATION_CALLSIGN, aprs_text)
        except (RuntimeError, OSError, ValueError) as error:
            self.log.error("Unable to relay RadioMSG over APRS: %s", error)
            return

        self.log.info("Relayed RadioMSG from %s to APRS %s", message.from_call, APRS_DESTINATION_CALLSIGN)
        write_traffic_log(
            {"transport": "aprs_tx", "source": SOURCE_CALLSIGN, "destination": APRS_DESTINATION_CALLSIGN, "message": aprs_text},
            self.log,
        )

    def _get_recent_fldigi_message(self, message_number):
        """Return the Nth most recent Fldigi log entry, or None."""
        try:
            with open(TRAFFIC_LOG_PATH, "r", encoding="utf-8") as traffic_log:
                for line in reversed(traffic_log.readlines()):
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("transport") in {"fldigi", "fldigi_rx"}:
                        message_number -= 1
                        if message_number == 0:
                            return entry
        except OSError as error:
            self.log.error("Unable to read traffic log: %s", error)
        return None

    def _handle_aprs_packet(self, packet):
        log_entry = {"transport": "aprs_rx", **packet}
        write_traffic_log(log_entry, self.log)
        self.log.info("APRS RX from %s: %s", packet["source"], packet["parsed"])

        parsed = packet["parsed"]
        if parsed.get("type") != "message":
            return
        if parsed.get("to", "").upper() != SOURCE_CALLSIGN.upper():
            return

        command = parsed.get("message", "").strip()
        if command.upper().startswith("M:"):
            fldigi_message = command[2:].strip()
            via_match = re.search(r"\s+V:\s*([A-Za-z0-9_-]+)\s*$", fldigi_message, re.IGNORECASE)
            via_call = via_match.group(1) if via_match else None
            if via_match:
                fldigi_message = fldigi_message[:via_match.start()].rstrip()
            if not fldigi_message:
                self.log.warning("Ignoring empty APRS M: command")
                return
            if self._fldigi is None:
                self.log.error("Unable to relay APRS M: command: Fldigi is not connected")
                return
            wire_message = format_radiomsg(
                "SURF",
                "*",
                fldigi_message,
                via=via_call,
            )
            readable_message = " ".join(
                wire_message.strip("\x01\x04").splitlines()
            )
            acknowledgement = f"Sending RadioMSG: {readable_message}"
            try:
                self._aprs.send_message(packet["source"], acknowledgement)
            except (RuntimeError, OSError, ValueError) as error:
                self.log.error("Unable to acknowledge APRS M: command: %s", error)
                return
            write_traffic_log(
                {
                    "transport": "aprs_tx",
                    "source": SOURCE_CALLSIGN,
                    "destination": packet["source"],
                    "command": command,
                    "message": acknowledgement,
                },
                self.log,
            )
            try:
                wire_message = send_radiomsg(
                    self._fldigi,
                    fldigi_message,
                    from_call="SURF",
                    to_call="*",
                    via=via_call,
                )
            except (OSError, RuntimeError, ValueError) as error:
                self.log.error("Unable to relay APRS M: command through Fldigi: %s", error)
                return
            self.log.info("Relayed APRS M: command through Fldigi: %s", fldigi_message)
            write_traffic_log(
                {
                    "transport": "fldigi_tx",
                    "source": "SURF",
                    "destination": "*",
                    "via": via_call,
                    "command": command,
                    "message": fldigi_message,
                    "raw": wire_message,
                },
                self.log,
            )
            return

        if command in {"?", "? -v"}:
            snapshot = self.health_snapshot()
            aprs_healthy = snapshot["aprs"]["connected"] and snapshot["aprs"]["running"]
            fldigi_healthy = snapshot["fldigi"]["healthy"]
            if command == "? -v":
                response = json.dumps(snapshot, separators=(",", ":"), sort_keys=True)
            else:
                response = f"APRS:{'OK' if aprs_healthy else 'FAIL'} Fldigi:{'OK' if fldigi_healthy else 'FAIL'}"
            try:
                self._aprs.send_message(
                    APRS_DESTINATION_CALLSIGN,
                    response,
                    message_id=parsed.get("message_id"),
                )
            except (RuntimeError, OSError, ValueError) as error:
                self.log.error("Unable to send APRS health response: %s", error)
                return
            self.log.info("Returned health status to APRS %s: %s", APRS_DESTINATION_CALLSIGN, response)
            write_traffic_log(
                {
                    "transport": "aprs_tx",
                    "source": SOURCE_CALLSIGN,
                    "destination": APRS_DESTINATION_CALLSIGN,
                    "command": command,
                    "message": response,
                },
                self.log,
            )
            return

        command_match = re.fullmatch(r"msg\s+([1-9][0-9]*)", command, re.IGNORECASE)
        if command_match is None:
            return

        message_number = int(command_match.group(1))
        message = self._get_recent_fldigi_message(message_number)
        if message is None:
            response = f"No Fldigi message {message_number} is available"
        else:
            response = " ".join(message["raw"].splitlines())

        try:
            self._aprs.send_message(
                APRS_DESTINATION_CALLSIGN,
                response,
                message_id=parsed.get("message_id"),
            )
        except (RuntimeError, OSError, ValueError) as error:
            self.log.error("Unable to send APRS command response: %s", error)
            return
        self.log.info("Returned Fldigi message %d to APRS %s", message_number, APRS_DESTINATION_CALLSIGN)
        write_traffic_log(
            {
                "transport": "aprs_tx",
                "source": SOURCE_CALLSIGN,
                "destination": APRS_DESTINATION_CALLSIGN,
                "command": command,
                "message": response,
            },
            self.log,
        )

    def _handle_aprs_error(self, error):
        self.log.error("APRS receive error: %s", error)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s : %(message)s")
    CrossbandRelay().run()


if __name__ == "__main__":
    main()
