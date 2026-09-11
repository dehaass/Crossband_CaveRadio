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

from radiomsg import RadioMsgParser, expected_checksum


SOURCE_CALLSIGN = "VE6LF"
APRS_DESTINATION_CALLSIGN = "VE6SDH"
FLDIGI_HOSTNAME = "127.0.0.1"
FLDIGI_PORT = 7362
FLDIGI_MODEM = "THOR4"
KISS_HOSTNAME = "127.0.0.1"
KISS_PORT = 8001
POLL_INTERVAL_SECONDS = 0.5
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
    """Represent a RadioMSG block as one APRS-compatible line."""
    text = " ".join(message.raw.splitlines())
    if len(text) > 67:
        raise ValueError(
            f"RadioMSG is {len(text)} characters; APRS messages support at most 67"
        )
    return text


class CrossbandRelay:
    """Run the Fldigi receiver and APRS service together."""

    def __init__(self):
        self.log = logging.getLogger("Crossband_Relay")
        self._stop_event = threading.Event()
        self._radio_parser = RadioMsgParser()
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
        self.log.info("Starting APRS service")
        self._aprs.start()

        self.log.info("Connecting to Fldigi at %s:%d", FLDIGI_HOSTNAME, FLDIGI_PORT)
        self._fldigi = pyfldigi.Client(hostname=FLDIGI_HOSTNAME, port=FLDIGI_PORT)
        self.log.info("Fldigi version: %s", self._fldigi.name)
        if self._fldigi.modem.name != FLDIGI_MODEM:
            self.log.info("Switching Fldigi modem to %s", FLDIGI_MODEM)
            self._fldigi.modem.name = FLDIGI_MODEM

    def run(self):
        """Poll Fldigi until stop() or Ctrl+C is requested."""
        if self._fldigi is None:
            self.start()
        self.log.info("Crossband relay is listening")
        try:
            while not self._stop_event.is_set():
                rx_data = self._fldigi.text.get_rx_data()
                if rx_data:
                    if isinstance(rx_data, bytes):
                        rx_data = rx_data.decode("utf-8", errors="replace")
                    self.log.debug("Fldigi RX: %r", rx_data)
                    for message in self._radio_parser.feed(rx_data):
                        self._handle_radio_message(message)
                time.sleep(POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            self.log.info("Stopping after Ctrl+C")
        finally:
            self.stop()

    def stop(self):
        self._stop_event.set()
        self._aprs.stop()

    def _handle_radio_message(self, message):
        log_entry = dataclasses.asdict(message)
        log_entry["transport"] = "fldigi"
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
                "Ignoring RadioMSG with invalid checksum: received=%s expected=%s raw=%s",
                message.checksum,
                expected,
                message.raw,
            )
            return
        try:
            aprs_text = radio_msg_to_aprs_text(message)
            self._aprs.send_message(APRS_DESTINATION_CALLSIGN, aprs_text)
        except (RuntimeError, OSError, ValueError) as error:
            self.log.error("Unable to relay RadioMSG over APRS: %s", error)
            return

        self.log.info("Relayed RadioMSG from %s to APRS %s", message.from_call, APRS_DESTINATION_CALLSIGN)
        write_traffic_log(
            {"transport": "aprs", "destination": APRS_DESTINATION_CALLSIGN, "message": aprs_text},
            self.log,
        )

    def _get_recent_fldigi_message(self, message_number):
        """Return the Nth most recent valid Fldigi log entry, or None."""
        try:
            with open(TRAFFIC_LOG_PATH, "r", encoding="utf-8") as traffic_log:
                for line in reversed(traffic_log.readlines()):
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        entry.get("transport") == "fldigi"
                        and entry.get("checksum_valid") is True
                    ):
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
        command_match = re.fullmatch(r"msg\s+([1-9][0-9]*)", command, re.IGNORECASE)
        if command_match is None:
            return

        message_number = int(command_match.group(1))
        message = self._get_recent_fldigi_message(message_number)
        if message is None:
            response = f"No Fldigi message {message_number} is available"
        else:
            try:
                response = " ".join(message["raw"].splitlines())
                if len(response) > 67:
                    raise ValueError("message is too long for APRS")
            except ValueError as error:
                self.log.warning("Cannot return Fldigi message %d: %s", message_number, error)
                response = f"Fldigi message {message_number} is too long for APRS"

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
                "transport": "aprs",
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
