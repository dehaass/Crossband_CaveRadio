# Python script for QDX Cave Radio communications
# Communicates with Fldigi to decode RadioMSG style messages sent from QDX cave radios
# This script can run on a server connected to a QDX radio and interact with Fldigi for message decoding

import os
import sys
import time
import logging

# Allow running against the local pyFldigi source without needing to `pip install` it first.
LOCAL_PYFLDIGI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pyFldigi')
if os.path.isdir(LOCAL_PYFLDIGI_PATH):
    sys.path.insert(0, LOCAL_PYFLDIGI_PATH)

import pyfldigi  # noqa: E402  (import after sys.path tweak above)
from radiomsg import RadioMsgParser, expected_checksum  # noqa: E402

FLDIGI_HOSTNAME = '127.0.0.1'
FLDIGI_PORT = 7362
MODEM_NAME = 'THOR4'
POLL_INTERVAL_SECONDS = 0.5

SOURCE_CALLSIGN_RADIOMSG = "SURF" # name used for transmitting RadioMSG style messages through fldigi.


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

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s : %(message)s')
    log = logging.getLogger('QDX_Fldigi_coms')

    log.info('Connecting to Fldigi at %s:%d', FLDIGI_HOSTNAME, FLDIGI_PORT)
    client = pyfldigi.Client(hostname=FLDIGI_HOSTNAME, port=FLDIGI_PORT)

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

