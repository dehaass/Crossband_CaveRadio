# Implements a crossband relay between Fldigi (using RadioMSG style messages) and APRS networks
# saves all traffic in a json log file for future use.

"""Crossband relay between Fldigi RadioMSG traffic and APRS."""

import dataclasses
from datetime import datetime
import json
import logging
import queue
import re
import threading
import time

from aprs_relay import AprsService
from config import settings


from Modem_App_Control import DirewolfController, FldigiController
from QDX_Fldigi_coms import FldigiReceiver, format_radiomsg
from radiomsg import expected_checksum
from relay_control_api import RelayControlServer


SOURCE_CALLSIGN = settings.source_callsign
APRS_DESTINATION_CALLSIGN = settings.aprs_destination_callsign
KISS_HOSTNAME = settings.kiss_hostname
KISS_PORT = settings.kiss_port
POLL_INTERVAL_SECONDS = settings.poll_interval_seconds
HEALTH_INTERVAL_SECONDS = settings.health_interval_seconds
TRAFFIC_LOG_PATH = settings.traffic_log_path


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


def aprs_delivery_fields(result):
    """Return APRS delivery metadata for a persisted traffic record."""
    return {
        "message_id": result.message_id,
        "attempts": result.attempts,
        "acknowledgement": result.acknowledgement,
        "acknowledged": result.acknowledged,
    }


class CrossbandRelay:
    """Run the Fldigi receiver and APRS service together."""

    def __init__(self, power_cycle_hook=None):
        self.log = logging.getLogger("Crossband_Relay")
        self._stop_event = threading.Event()
        self._monitor_thread = None
        self._aprs_worker_thread = None
        self._aprs_queue = queue.Queue()
        self._processed_aprs_ids = {}
        self._processed_aprs_lock = threading.Lock()
        self._fldigi_last_error = None
        self._direwolf_last_error = None
        self._power_cycle_hook = power_cycle_hook
        self._fldigi_transmitting = False
        self._fldigi_controller = FldigiController()
        self._fldigi_receiver = None
        self._direwolf_controller = DirewolfController()
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
        self.log.info("Starting Dire Wolf")
        try:
            self._direwolf_controller.start()
            self._direwolf_last_error = None
        except RuntimeError as error:
            self._direwolf_last_error = str(error)
            self.log.error("Dire Wolf did not start: %s", error)
        self.log.info("Starting APRS service")
        self._aprs.start()

        self.log.info("Starting Fldigi")
        try:
            client = self._fldigi_controller.start(headless=settings.fldigi_headless)
            self._fldigi_receiver = FldigiReceiver(client)
            self._fldigi_last_error = None
        except RuntimeError as error:
            self._fldigi_last_error = str(error)
            self.log.error("Fldigi did not start: %s", error)
        if self._monitor_thread is None or not self._monitor_thread.is_alive():
            self._monitor_thread = threading.Thread(
                target=self._health_monitor_loop,
                name="relay-health",
                daemon=True,
            )
            self._monitor_thread.start()
        if self._aprs_worker_thread is None or not self._aprs_worker_thread.is_alive():
            self._aprs_worker_thread = threading.Thread(
                target=self._aprs_command_worker_loop,
                name="aprs-command-worker",
                daemon=True,
            )
            self._aprs_worker_thread.start()

    def health_snapshot(self):
        """Return current APRS, Fldigi, and relay health state."""
        fldigi = {
            "connected": self._fldigi_receiver is not None,
            "running": self._fldigi_controller.is_running(),
            "last_error": self._fldigi_last_error,
            "transmitting": self._fldigi_transmitting,
        }
        if self._fldigi_receiver is not None:
            fldigi.update(self._fldigi_receiver.health_snapshot())
            try:
                fldigi["status"] = self._fldigi_receiver.client.main.status1
                fldigi["healthy"] = True
            except Exception as error:
                fldigi["healthy"] = False
                fldigi["last_error"] = str(error)
        else:
            fldigi["healthy"] = False
        aprs = self._aprs.health_snapshot()
        aprs["process_running"] = self._direwolf_controller.is_running()
        aprs["direwolf_error"] = self._direwolf_last_error
        return {
            "healthy": fldigi["healthy"] and self._aprs.connected and self._aprs.running,
            "fldigi": fldigi,
            "aprs": aprs,
        }

    def sn_history_snapshot(self, window_seconds=None):
        """Recent S/N samples for the modem noise graph, if fldigi is connected."""
        if self._fldigi_receiver is None:
            return []
        return self._fldigi_receiver.sn_history_snapshot(window_seconds)

    def power_cycle(self, subsystem):
        """Invoke the configured hardware power-cycle hook for a subsystem."""
        if self._power_cycle_hook is None:
            raise RuntimeError("No power_cycle_hook is configured")
        if subsystem not in {"fldigi", "aprs", "radio"}:
            raise ValueError("subsystem must be fldigi, aprs, or radio")
        self._power_cycle_hook(subsystem)

    def transmit_radiomsg(self, message, **fields):
        """Transmit RadioMSG while exposing the active TX state to health clients."""
        if self._fldigi_receiver is None:
            raise RuntimeError("Fldigi is not connected")
        self._fldigi_transmitting = True
        try:
            return self._fldigi_receiver.transmit(message, **fields)
        finally:
            self._fldigi_transmitting = False

    def send_aprs_message(self, destination, message, message_id=None):
        """Send and log an APRS message, for use by the control API or website."""
        result = self._aprs.send_message(destination, message, message_id=message_id)
        write_traffic_log(
            {
                "transport": "aprs_tx",
                "source": SOURCE_CALLSIGN,
                "destination": destination,
                "message": message,
                **aprs_delivery_fields(result),
            },
            self.log,
        )
        return result

    def send_fldigi_message(self, message, via=None):
        """Transmit and log a RadioMSG message, for use by the control API or website."""
        wire_message = self.transmit_radiomsg(
            message,
            from_call=settings.radiomsg_source_callsign,
            to_call="*",
            via=via,
        )
        write_traffic_log(
            {
                "transport": "fldigi_tx",
                "source": settings.radiomsg_source_callsign,
                "destination": "*",
                "via": via,
                "message": message,
                "raw": wire_message,
            },
            self.log,
        )
        return wire_message

    def _health_monitor_loop(self):
        while not self._stop_event.wait(HEALTH_INTERVAL_SECONDS):
            snapshot = self.health_snapshot()
            self.log.info("Health: %s", snapshot)
            if not snapshot["aprs"]["connected"] or not snapshot["aprs"]["running"]:
                self._restart_aprs()
            if not snapshot["fldigi"]["healthy"]:
                self._restart_fldigi()

    def _restart_aprs(self):
        self.log.warning("Restarting APRS")
        try:
            self._aprs.stop()
            if not self._direwolf_controller.is_running():
                self._direwolf_controller.restart()
            self._direwolf_last_error = None
            self._aprs.start()
        except (OSError, RuntimeError) as error:
            self._direwolf_last_error = str(error)
            self.log.error("Unable to restart APRS: %s", error)

    def _restart_fldigi(self):
        self.log.warning("Restarting Fldigi")
        try:
            client = self._fldigi_controller.restart(headless=settings.fldigi_headless)
            self._fldigi_receiver = FldigiReceiver(client)
            self._fldigi_last_error = None
        except Exception as error:
            self._fldigi_last_error = str(error)
            self.log.error("Unable to restart Fldigi: %s", error)

    def run(self):
        """Poll Fldigi until stop() or Ctrl+C is requested."""
        if self._fldigi_receiver is None:
            self.start()
        self.log.info("Crossband relay is listening")
        try:
            while not self._stop_event.is_set():
                for message, sn_samples, sn_status in self._fldigi_receiver.poll():
                    self._handle_radio_message(message, sn_samples, sn_status)
                time.sleep(POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            self.log.info("Stopping after Ctrl+C")
        finally:
            self.stop()

    def stop(self):
        self._stop_event.set()
        if self._aprs_queue is not None:
            self._aprs_queue.put(None)
        self._aprs.stop()
        self._direwolf_controller.stop()
        if self._fldigi_receiver is not None:
            self._fldigi_controller.stop()
            self._fldigi_receiver = None
        if self._monitor_thread is not None and self._monitor_thread is not threading.current_thread():
            self._monitor_thread.join(timeout=2)
        self._monitor_thread = None
        if self._aprs_worker_thread is not None and self._aprs_worker_thread is not threading.current_thread():
            self._aprs_worker_thread.join(timeout=2)
        self._aprs_worker_thread = None

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
            result = self._aprs.send_message(APRS_DESTINATION_CALLSIGN, aprs_text)
        except (RuntimeError, OSError, ValueError) as error:
            self.log.error("Unable to relay RadioMSG over APRS: %s", error)
            return

        self.log.info("Relayed RadioMSG from %s to APRS %s", message.from_call, APRS_DESTINATION_CALLSIGN)
        write_traffic_log(
            {"transport": "aprs_tx", "source": SOURCE_CALLSIGN, "destination": APRS_DESTINATION_CALLSIGN, "message": aprs_text, **aprs_delivery_fields(result)},
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

        message_id = parsed.get("message_id")
        if message_id is not None:
            now = time.time()
            dedup_key = (packet["source"].upper(), str(message_id))
            with self._processed_aprs_lock:
                expired = [k for k, timestamp in self._processed_aprs_ids.items() if now - timestamp > 300]
                for k in expired:
                    del self._processed_aprs_ids[k]

                if dedup_key in self._processed_aprs_ids:
                    self.log.info(
                        "Ignoring duplicate APRS message %s from %s (already processed)",
                        message_id,
                        packet["source"],
                    )
                    return
                self._processed_aprs_ids[dedup_key] = now

        self._aprs_queue.put(packet)

    def _aprs_command_worker_loop(self):
        while not self._stop_event.is_set():
            try:
                packet = self._aprs_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if packet is None:
                break
            try:
                self._process_aprs_command(packet)
            except Exception as error:
                self.log.error("Error processing APRS command from %s: %s", packet.get("source"), error)
            finally:
                self._aprs_queue.task_done()

    def _process_aprs_command(self, packet):
        parsed = packet["parsed"]
        source = packet["source"]
        command = parsed.get("message", "").strip()

        # Turnaround delay so the auto-ack frame finishes transmitting on RF
        time.sleep(0.5)

        if command.upper().startswith("M:"):
            fldigi_message = command[2:].strip()
            via_match = re.search(r"\s+V:\s*([A-Za-z0-9_-]+)\s*$", fldigi_message, re.IGNORECASE)
            via_call = via_match.group(1) if via_match else None
            if via_match:
                fldigi_message = fldigi_message[:via_match.start()].rstrip()
            if not fldigi_message:
                self.log.warning("Ignoring empty APRS M: command from %s", source)
                return
            if self._fldigi_receiver is None:
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
                result = self._aprs.send_message(source, acknowledgement)
            except (RuntimeError, OSError, ValueError) as error:
                self.log.error("Unable to acknowledge APRS M: command to %s: %s", source, error)
                result = None
            if result is not None:
                write_traffic_log(
                    {
                        "transport": "aprs_tx",
                        "source": SOURCE_CALLSIGN,
                        "destination": source,
                        "command": command,
                        "message": acknowledgement,
                        **aprs_delivery_fields(result),
                    },
                    self.log,
                )
            try:
                wire_message = self.transmit_radiomsg(
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
                result = self._aprs.send_message(
                    source,
                    response,
                )
            except (RuntimeError, OSError, ValueError) as error:
                self.log.error("Unable to send APRS health response to %s: %s", source, error)
                return
            self.log.info("Returned health status to APRS %s: %s", source, response)
            write_traffic_log(
                {
                    "transport": "aprs_tx",
                    "source": SOURCE_CALLSIGN,
                    "destination": source,
                    "command": command,
                    "message": response,
                    **aprs_delivery_fields(result),
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
            result = self._aprs.send_message(
                source,
                response,
            )
        except (RuntimeError, OSError, ValueError) as error:
            self.log.error("Unable to send APRS command response to %s: %s", source, error)
            return
        self.log.info("Returned Fldigi message %d to APRS %s", message_number, source)
        write_traffic_log(
            {
                "transport": "aprs_tx",
                "source": SOURCE_CALLSIGN,
                "destination": source,
                "command": command,
                "message": response,
                **aprs_delivery_fields(result),
            },
            self.log,
        )

    def _handle_aprs_error(self, error):
        self.log.error("APRS receive error: %s", error)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s : %(message)s")
    relay = CrossbandRelay()
    control_server = RelayControlServer(relay)
    control_server.start()
    try:
        relay.run()
    finally:
        control_server.stop()


if __name__ == "__main__":
    main()
