"""Transmit one RadioMSG test message through the local Fldigi instance."""

import logging

import pyfldigi

from QDX_Fldigi_coms import FLDIGI_HOSTNAME, FLDIGI_PORT, MODEM_NAME, send_radiomsg


FROM_CALLSIGN = "SURF"
TO_CALLSIGN = "*"
MESSAGE = "RadioMSG transmit test"
VIA_CALLSIGN = None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s : %(message)s")
    log = logging.getLogger("test_radiomsg_tx")

    log.info("Connecting to Fldigi at %s:%d", FLDIGI_HOSTNAME, FLDIGI_PORT)
    client = pyfldigi.Client(hostname=FLDIGI_HOSTNAME, port=FLDIGI_PORT)
    if client.modem.name != MODEM_NAME:
        client.modem.name = MODEM_NAME

    wire_message = send_radiomsg(
        client,
        MESSAGE,
        from_call=FROM_CALLSIGN,
        to_call=TO_CALLSIGN,
        via=VIA_CALLSIGN,
    )
    log.info("Transmitted RadioMSG: %r", wire_message)


if __name__ == "__main__":
    main()
