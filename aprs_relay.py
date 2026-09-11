"""Send an addressed APRS message to Dire Wolf over KISS TCP.

Dire Wolf handles AX.25 modulation, audio, and PTT. This script only builds
the AX.25/APRS UI frame and sends it to Dire Wolf's KISS listener.

Example::

	python aprs_relay.py --source N0CALL --destination UNIT7 \
		--message "Relay test" --message-id 001
"""

import argparse
import socket


KISS_FEND = 0xC0
KISS_FESC = 0xDB
KISS_TFEND = 0xDC
KISS_TFESC = 0xDD
KISS_DATA_FRAME = 0x00
DEFAULT_KISS_HOST = "127.0.0.1"
DEFAULT_KISS_PORT = 8001


def parse_callsign(value):
	"""Return an uppercase callsign and optional numeric SSID."""
	callsign, separator, ssid_text = value.upper().partition("-")
	if not callsign or len(callsign) > 6 or not callsign.isalnum():
		raise ValueError(f"Invalid callsign: {value!r}")
	if not separator:
		return callsign, 0
	if not ssid_text.isdigit() or int(ssid_text) > 15:
		raise ValueError(f"Invalid SSID: {value!r}")
	return callsign, int(ssid_text)


def ax25_address(value, last=False):
	"""Encode one AX.25 address subfield."""
	callsign, ssid = parse_callsign(value)
	address = bytearray((ord(char) << 1 for char in callsign.ljust(6)))
	address.extend((0x60 | (ssid << 1) | (1 if last else 0),))
	return bytes(address)


def aprs_message_info(destination, message, message_id=None):
	"""Build the APRS information field for an addressed APRS message."""
	destination_call, destination_ssid = parse_callsign(destination)
	destination_text = destination_call
	if destination_ssid:
		destination_text += f"-{destination_ssid}"
	if len(destination_text) > 9:
		raise ValueError("APRS message addressee must fit in 9 characters")
	if len(message) > 67:
		raise ValueError("APRS message text must be 67 characters or fewer")
	info = f":{destination_text:<9}:{message}"
	if message_id is not None:
		if not message_id.isdigit() or not 1 <= len(message_id) <= 3:
			raise ValueError("APRS message ID must contain 1 to 3 digits")
		info += "{" + message_id
	return info.encode("ascii")


def ax25_ui_frame(source, destination, path, info):
	"""Build an AX.25 UI frame without an FCS, as expected by KISS."""
	addresses = [destination, source, *path]
	address_bytes = b"".join(
		ax25_address(address, last=index == len(addresses) - 1)
		for index, address in enumerate(addresses)
	)
	return address_bytes + bytes((0x03, 0xF0)) + info


def kiss_encode(frame):
	"""Wrap an AX.25 frame in a KISS data frame."""
	escaped = frame.replace(bytes((KISS_FESC,)), bytes((KISS_FESC, KISS_TFESC)))
	escaped = escaped.replace(bytes((KISS_FEND,)), bytes((KISS_FESC, KISS_TFEND)))
	return bytes((KISS_FEND, KISS_DATA_FRAME)) + escaped + bytes((KISS_FEND,))


def send_aprs_message(host, port, source, destination, message, path, message_id=None):
	"""Connect to Dire Wolf's KISS listener and transmit one APRS message."""
	info = aprs_message_info(destination, message, message_id)
	frame = ax25_ui_frame(source, "APRS", path, info)
	packet = kiss_encode(frame)
	with socket.create_connection((host, port), timeout=10) as connection:
		connection.sendall(packet)
	return packet


def main():
	parser = argparse.ArgumentParser(description="Send one APRS message through Dire Wolf KISS TCP")
	parser.add_argument("--source", required=True, help="Your station callsign, optionally with SSID")
	parser.add_argument("--destination", required=True, help="APRS recipient callsign, optionally with SSID")
	parser.add_argument("--message", required=True, help="Message text, up to 67 characters")
	parser.add_argument("--message-id", help="Optional APRS message ID, 1 to 3 digits")
	parser.add_argument("--path", default="", help="Comma-separated digipeater path, e.g. WIDE1-1,WIDE2-1")
	parser.add_argument("--host", default=DEFAULT_KISS_HOST)
	parser.add_argument("--port", type=int, default=DEFAULT_KISS_PORT)
	args = parser.parse_args()

	path = [item.strip() for item in args.path.split(",") if item.strip()]
	packet = send_aprs_message(
		args.host, args.port, args.source, args.destination,
		args.message, path, args.message_id,
	)
	print(f"Sent {len(packet)} KISS bytes to {args.host}:{args.port}")


if __name__ == "__main__":
	main()

