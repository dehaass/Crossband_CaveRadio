# Python script for QDX Cave Radio communications
# Communicates with Fldigi to decode RadioMSG style messages sent from QDX cave radios
# This script can run on a server connected to a QDX radio and interact with Fldigi for message decoding

import os
import re
import sys
import time
import logging
from collections import deque

import pyfldigi


from radiomsg import RadioMsgParser, expected_checksum  # noqa: E402
from config import settings

FLDIGI_HOSTNAME = settings.fldigi_hostname
FLDIGI_PORT = settings.fldigi_port
MODEM_NAME = settings.fldigi_modem
POLL_INTERVAL_SECONDS = settings.poll_interval_seconds

# Bounds how much S/N history is kept in memory, regardless of what window a
# client later asks for; the dashboard graph only offers up to 30 minutes.
SN_HISTORY_MAX_SECONDS = 1800

# Sentinel S/N value recorded (and understood by the dashboard graph) when
# fldigi has no reading, representing an effectively dead/no-signal channel.
NO_SIGNAL_SN = -60.0

SOURCE_CALLSIGN_RADIOMSG = settings.radiomsg_source_callsign # name used for transmitting RadioMSG style messages through fldigi.


def format_radiomsg(from_call, to_call, message, via=None, rly=None, msg_id=None,
                    position=None, picture=None, received_date=None,
                    received_offset=None, time_sync=None):
    """Build a RadioMSG wire message ready for Fldigi transmission."""
    fields = [f"{from_call.lower()}:{to_call.lower()}\n"]
    if message:
        fields.append(f"sms:{message}\n")
    if via:
        fields.append(f"via:{via.lower()}\n")
    if rly:
        fields.append(f"rly:{rly.lower()}\n")
    if position:
        fields.append(f"pos:{position}\n")
    if picture:
        fields.append(f"pic:{picture}\n")
    if msg_id:
        fields.append(f"id:{msg_id}\n")
    if received_date:
        fields.append(f"rd:{received_date}\n")
    elif received_offset:
        fields.append(f"ro:{received_offset}\n")
    if time_sync:
        fields.append(f"tim:{time_sync}\n")

    buffer = "\x01" + "".join(fields)
    checksum = expected_checksum(
        from_call, to_call, message, via, rly, msg_id, position, picture,
        received_date, received_offset, time_sync,
    )
    return buffer + checksum + "\x04"


def send_radiomsg(client, message, from_call=SOURCE_CALLSIGN_RADIOMSG,
                  to_call="*", via=None, rly=None, msg_id=None,
                  position=None, picture=None, received_date=None,
                  received_offset=None, time_sync=None):
    """Format and transmit one RadioMSG message through an existing Fldigi client."""
    wire_message = format_radiomsg(
        from_call, to_call, message, via, rly, msg_id, position, picture,
        received_date, received_offset, time_sync,
    )
    client.main.send(wire_message, block=True, timeout=100)
    return wire_message


class FldigiReceiver:
    """Runtime interaction with a running fldigi instance: rx polling/parsing,
    live modem/frequency/squelch changes, and message transmission.

    Operates on a `pyfldigi.Client` created by Modem_App_Control.FldigiController;
    this class never starts, stops, or does the initial configuration of fldigi.
    """

    def __init__(self, client):
        self.client = client
        self.log = logging.getLogger("QDX_Fldigi_coms")
        self._parser = RadioMsgParser()
        self._raw_text = ""
        self._raw_chunks = deque()
        self._sn_samples = []
        self._sn_status = []
        self._sn_history = deque()
        self.last_rx_at = None
        self.sn_status = None
        self.sn_value = None

    def poll(self):
        """Read one chunk of rx data, returning any fully parsed (message, sn_samples, sn_status) tuples."""
        rx_data = self.client.text.get_rx_data()
        status = self._read_sn()
        self.sn_status = status
        self.sn_value = self._parse_sn_value(status) if status else None
        if self.sn_value is None:
            self._raw_text = ""
        # Record a floor value when there's no S/N reading, so the history graph
        # visibly drops instead of just freezing on the last received value.
        self._record_sn_history(self.sn_value if self.sn_value is not None else NO_SIGNAL_SN)
        if not rx_data:
            return []

        if isinstance(rx_data, bytes):
            rx_data = rx_data.decode("utf-8", errors="replace")
        if self.sn_value is not None:
            self.last_rx_at = time.time()
            self._raw_text = (self._raw_text + rx_data)[-settings.fldigi_raw_preview_limit:]
            self._raw_chunks.append((self.sn_value, rx_data))
            self._trim_raw_chunks()
        if status is not None:
            self._sn_status.append(status)
            value = self._parse_sn_value(status)
            if value is not None:
                self._sn_samples.append(value)
        self.log.debug("Fldigi RX: %r", rx_data)

        messages = self._parser.feed(rx_data)
        results = []
        for index, message in enumerate(messages):
            samples = self._sn_samples if index == 0 else []
            statuses = self._sn_status if index == 0 else []
            results.append((message, list(samples), list(statuses)))
        if messages:
            self._raw_text = ""
            self._raw_chunks.clear()
            self._sn_samples.clear()
            self._sn_status.clear()
        return results

    def _read_sn(self):
        try:
            status = self.client.main.status1
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

    def _record_sn_history(self, value):
        now = time.time()
        self._sn_history.append((now, value))
        cutoff = now - SN_HISTORY_MAX_SECONDS
        while self._sn_history and self._sn_history[0][0] < cutoff:
            self._sn_history.popleft()

    def sn_history_snapshot(self, window_seconds=None):
        """Return recent (timestamp, S/N) samples, oldest first, for graphing."""
        if window_seconds:
            cutoff = time.time() - window_seconds
            samples = (item for item in self._sn_history if item[0] >= cutoff)
        else:
            samples = self._sn_history
        return [{"t": timestamp, "sn": value} for timestamp, value in samples]

    def _trim_raw_chunks(self):
        """Keep only as many raw chunks as fit within the raw-preview character limit."""
        total = sum(len(text) for _, text in self._raw_chunks)
        while total > settings.fldigi_raw_preview_limit and len(self._raw_chunks) > 1:
            _, text = self._raw_chunks.popleft()
            total -= len(text)

    def raw_chunks_snapshot(self):
        """Raw decode text paired with the S/N reading at arrival, so a display-only
        squelch can filter noisy characters without touching fldigi's own squelch."""
        return [{"sn": sn, "text": text} for sn, text in self._raw_chunks]

    def set_modem(self, name):
        """Change the active modem while fldigi is running."""
        if self.client.modem.name != name:
            self.log.info("Setting fldigi modem to %s", name)
            self.client.modem.name = name

    def set_frequency(self, frequency_hz):
        """Change the rig frequency while fldigi is running."""
        self.log.info("Setting fldigi frequency to %.0f Hz", frequency_hz)
        self.client.rig.frequency = frequency_hz

    def set_squelch(self, squelch_level):
        """Change the squelch level/enable state while fldigi is running."""
        self.log.info("Setting fldigi squelch level to %.1f", squelch_level)
        self.client.main.squelch_level = squelch_level
        self.client.main.squelch = squelch_level > 0

    def health_snapshot(self):
        return {
            "receiving": self.sn_value is not None,
            "raw_text": self._raw_text,
            "raw_chunks": self.raw_chunks_snapshot(),
            "sn_status": self.sn_status,
            "sn_value": self.sn_value,
        }

    def transmit(self, message, **fields):
        """Format and send a RadioMSG message through the wrapped client."""
        return send_radiomsg(self.client, message, **fields)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s : %(message)s')
    log = logging.getLogger('QDX_Fldigi_coms')

    log.info('Connecting to Fldigi at %s:%d', FLDIGI_HOSTNAME, FLDIGI_PORT)
    client = pyfldigi.Client(hostname=FLDIGI_HOSTNAME, port=FLDIGI_PORT)
    # SDH - Take control of the rig to allow frequency changes from this client.
    # client.rig.take_control()

    log.info('Fldigi version: %s', client.name)

    if client.modem.name != MODEM_NAME:
        log.info('Switching modem from %s to %s', client.modem.name, MODEM_NAME)
        client.modem.name = MODEM_NAME

    parser = RadioMsgParser()

    log.info('Listening for received messages (Ctrl+C to stop)...')
    try:
        while True:
            rx_data = client.text.get_rx_data()
            if rx_data:
                if isinstance(rx_data, bytes):
                    rx_data = rx_data.decode('utf-8', errors='replace')
                log.debug('RX raw chunk: %r', rx_data)
                print(rx_data, end='', flush=True)

                for msg in parser.feed(rx_data):
                    log.info('Parsed message: %s', msg)
                    if not msg.checksum_valid:
                        log.warning('Checksum mismatch for message from %s: %s', msg.from_call, msg.raw)
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        log.info('Stopped by user')


if __name__ == '__main__':
    main()

