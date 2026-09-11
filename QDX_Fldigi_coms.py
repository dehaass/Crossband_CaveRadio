# Python script for QDX Cave Radio communications
# Communicates with Fldigi to decode RadioMSG style messages sent from QDX cave radios
# This script can run on a server connected to a QDX radio and interact with Fldigi for message decoding

import os
import sys
import time
import json
import logging
import dataclasses

# Allow running against the local pyFldigi source without needing to `pip install` it first.
# sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pyFldigi'))

import pyfldigi  # noqa: E402  (import after sys.path tweak above)
from radiomsg import RadioMsgParser  # noqa: E402

FLDIGI_HOSTNAME = '127.0.0.1'
FLDIGI_PORT = 7362
MODEM_NAME = 'THOR4'
POLL_INTERVAL_SECONDS = 0.5
MESSAGE_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'messages.jsonl')


def save_message(msg, log):
    """Append a parsed message to a JSON-lines log for use by later relay/logging steps."""
    try:
        with open(MESSAGE_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(dataclasses.asdict(msg)) + '\n')
    except OSError as e:
        log.error('Failed to write message log: %s', e)


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
                    save_message(msg, log)
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        log.info('Stopped by user')


if __name__ == '__main__':
    main()

