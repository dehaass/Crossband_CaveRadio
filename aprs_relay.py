"""Send an addressed APRS message to Dire Wolf over KISS TCP.

Dire Wolf handles AX.25 modulation, audio, and PTT. This script only builds
the AX.25/APRS UI frame and sends it to Dire Wolf's KISS listener.

Example::

	python aprs_relay.py --source N0CALL --destination UNIT7 \
		--message "Relay test" --message-id 001
"""

import argparse
import socket
import re
import time
import threading

try:
	from config import settings
	DEFAULT_RETRIES = getattr(settings, "aprs_retries", 0)
	DEFAULT_TX_DELAY = getattr(settings, "aprs_tx_delay_seconds", 1.0)
except (ImportError, RuntimeError):
	DEFAULT_RETRIES = 0
	DEFAULT_TX_DELAY = 1.0


KISS_FEND = 0xC0
KISS_FESC = 0xDB
KISS_TFEND = 0xDC
KISS_TFESC = 0xDD
KISS_DATA_FRAME = 0x00
DEFAULT_KISS_HOST = "127.0.0.1"
DEFAULT_KISS_PORT = 8001
DEFAULT_LISTEN_SECONDS = 30
APRS_MESSAGE_MAX_LENGTH = 67
RETRIES = DEFAULT_RETRIES
ACK_TIMEOUT_SECONDS = 30


class AprsSendResult(list):
	"""Packets sent and the delivery status reported by the remote station."""

	def __init__(self, packets, message_id, attempts, acknowledgement):
		super().__init__(packets)
		self.message_id = message_id
		self.attempts = attempts
		self.acknowledgement = acknowledgement

	@property
	def acknowledged(self):
		return self.acknowledgement == "ack"


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
	id_suffix = ""
	if message_id is not None:
		if not message_id.isdigit() or not 1 <= len(message_id) <= 3:
			raise ValueError("APRS message ID must contain 1 to 3 digits")
		id_suffix = "{" + message_id
	if len(message) + len(id_suffix) > APRS_MESSAGE_MAX_LENGTH:
		raise ValueError("APRS message text and ID must fit in 67 characters")
	info = f":{destination_text:<9}:{message}"
	info += id_suffix
	return info.encode("ascii")


def split_aprs_message(message, message_id=None):
	"""Split text into APRS-sized numbered segments when necessary."""
	id_length = 0 if message_id is None else len(str(message_id)) + 1
	if id_length and (not str(message_id).isdigit() or not 1 <= len(str(message_id)) <= 3):
		raise ValueError("APRS message ID must contain 1 to 3 digits")
	if len(message) + id_length <= APRS_MESSAGE_MAX_LENGTH:
		return [message]

	segment_count = 1
	while True:
		prefix_length = len(f"({segment_count}/{segment_count}) ")
		chunk_size = APRS_MESSAGE_MAX_LENGTH - id_length - prefix_length
		if chunk_size <= 0:
			raise ValueError("APRS message ID leaves no room for segmented text")
		new_count = (len(message) + chunk_size - 1) // chunk_size
		if new_count == segment_count:
			break
		segment_count = new_count

	return [
		f"({index}/{segment_count}) {message[offset:offset + chunk_size]}"
		for index, offset in enumerate(range(0, len(message), chunk_size), start=1)
	]


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


class KissDecoder:
	"""Decode a stream of KISS TCP bytes into AX.25 frames."""

	def __init__(self):
		self._frame = bytearray()
		self._escaped = False

	def feed(self, data):
		frames = []
		for byte in data:
			if byte == KISS_FEND:
				if self._frame and self._frame[0] == KISS_DATA_FRAME:
					frames.append(bytes(self._frame[1:]))
				self._frame.clear()
				self._escaped = False
				continue
			if not self._frame and byte != KISS_DATA_FRAME:
				continue
			if self._escaped:
				if byte == KISS_TFEND:
					self._frame.append(KISS_FEND)
				elif byte == KISS_TFESC:
					self._frame.append(KISS_FESC)
				else:
					self._frame.clear()
				self._escaped = False
			elif byte == KISS_FESC:
				self._escaped = True
			else:
				self._frame.append(byte)
		return frames


def decode_ax25_ui_frame(frame):
	"""Return AX.25 addresses and APRS information from a UI frame."""
	addresses = []
	position = 0
	while position + 7 <= len(frame):
		address_bytes = frame[position:position + 7]
		callsign = bytes(value >> 1 for value in address_bytes[:6]).decode("ascii").rstrip()
		ssid = (address_bytes[6] >> 1) & 0x0F
		addresses.append(callsign + (f"-{ssid}" if ssid else ""))
		position += 7
		if address_bytes[6] & 0x01:
			break
	if not addresses or position + 2 > len(frame):
		raise ValueError("Incomplete AX.25 UI frame")
	if frame[position:position + 2] != b"\x03\xF0":
		raise ValueError("Unsupported AX.25 frame control/PID")
	info = frame[position + 2:].decode("ascii", errors="replace")
	return addresses, info


def parse_aprs_information(info):
	"""Classify an APRS information field as a message, ACK, or other packet."""
	if not info.startswith(":") or len(info) < 11 or info[10] != ":":
		return {"type": "other", "text": info}
	addressee = info[1:10].strip()
	text = info[11:]
	ack_match = re.fullmatch(r"(ack|rej)([0-9]{1,3})", text)
	if ack_match:
		return {"type": ack_match.group(1), "from": addressee, "message_id": ack_match.group(2)}
	message_match = re.fullmatch(r"(.*?)(?:\{([0-9]{1,3}))?", text)
	return {
		"type": "message",
		"to": addressee,
		"message": message_match.group(1),
		"message_id": message_match.group(2),
	}


class AprsService:
	"""Maintain a KISS connection and exchange APRS packets in the background.

	Example::

		service = AprsService("VE6LF", on_packet=handle_packet)
		service.start()
		service.send_message("VE6SDH", "Relay test", message_id="001")
		service.stop()
	"""

	def __init__(self, source, host=DEFAULT_KISS_HOST, port=DEFAULT_KISS_PORT,
				 path=None, on_packet=None, on_error=None, auto_ack=True,
				 retries=None, tx_delay=None):
		self.source = source
		self.host = host
		self.port = port
		self.path = list(path or [])
		self.on_packet = on_packet
		self.on_error = on_error
		self.auto_ack = auto_ack
		self.retries = DEFAULT_RETRIES if retries is None else retries
		self.tx_delay = DEFAULT_TX_DELAY if tx_delay is None else tx_delay
		self._connection = None
		self._thread = None
		self._stop_event = threading.Event()
		self._send_lock = threading.Lock()
		self._pending_acks = {}
		self._pending_acks_lock = threading.Lock()
		self._next_message_id = int(time.time()) % 1000
		self._started_at = None
		self._last_packet_at = None
		self._last_error = None

	@property
	def running(self):
		"""Whether the background receive loop is active."""
		return self._thread is not None and self._thread.is_alive()

	@property
	def connected(self):
		"""Whether the KISS socket is currently open."""
		return self._connection is not None

	def health_snapshot(self):
		"""Return connection state suitable for monitoring or health logging."""
		return {
			"running": self.running,
			"connected": self.connected,
			"host": self.host,
			"port": self.port,
			"started_at": self._started_at,
			"last_packet_at": self._last_packet_at,
			"last_error": self._last_error,
		}

	def start(self):
		"""Connect to Dire Wolf and start receiving packets."""
		if self.running:
			return
		self._connection = socket.create_connection((self.host, self.port), timeout=10)
		self._connection.settimeout(1.0)
		self._stop_event.clear()
		self._started_at = time.time()
		self._last_error = None
		self._thread = threading.Thread(target=self._receive_loop, name="aprs-receive", daemon=True)
		self._thread.start()

	def send_message(self, destination, message, message_id=None, retries=None):
		"""Transmit one addressed APRS message and wait for its ACK or REJ."""
		message_id = str(message_id) if message_id is not None else self._allocate_message_id()
		segments = split_aprs_message(message, message_id)
		ack_key = (destination.upper(), message_id)
		ack_event = threading.Event()
		with self._pending_acks_lock:
			if ack_key in self._pending_acks:
				raise ValueError(f"APRS message ID {message_id} is already awaiting a response from {destination}")
			self._pending_acks[ack_key] = {"event": ack_event, "response": None}

		packets = []
		acknowledgement = None
		attempts = 0
		retry_limit = self.retries if retries is None else retries
		try:
			if retry_limit == 0:
				attempts = 1
				for segment in segments:
					packets.append(self._send_info(aprs_message_info(destination, segment, message_id)))
				if ack_event.wait(self.tx_delay):
					with self._pending_acks_lock:
						acknowledgement = self._pending_acks[ack_key]["response"]
			else:
				for attempts in range(1, retry_limit + 2):
					for segment in segments:
						packets.append(self._send_info(aprs_message_info(destination, segment, message_id)))
					if ack_event.wait(ACK_TIMEOUT_SECONDS):
						with self._pending_acks_lock:
							acknowledgement = self._pending_acks[ack_key]["response"]
						break
		finally:
			with self._pending_acks_lock:
				self._pending_acks.pop(ack_key, None)
		return AprsSendResult(packets, message_id, attempts, acknowledgement)

	def _allocate_message_id(self):
		with self._pending_acks_lock:
			message_id = f"{self._next_message_id:03d}"
			self._next_message_id = (self._next_message_id + 1) % 1000
			return message_id

	def send_ack(self, destination, message_id):
		"""Transmit an APRS acknowledgement for a received message ID."""
		message_id = str(message_id)
		if not message_id.isdigit() or not 1 <= len(message_id) <= 3:
			raise ValueError("APRS message ID must contain 1 to 3 digits")
		destination_call, destination_ssid = parse_callsign(destination)
		destination_text = destination_call + (f"-{destination_ssid}" if destination_ssid else "")
		if len(destination_text) > 9:
			raise ValueError("APRS message addressee must fit in 9 characters")
		info = f":{destination_text:<9}:ack{message_id}".encode("ascii")
		return self._send_info(info)

	def _send_info(self, info):
		if self._connection is None or not self.running:
			raise RuntimeError("AprsService.start() must be called before transmitting")
		frame = ax25_ui_frame(self.source, "APRS", self.path, info)
		packet = kiss_encode(frame)
		with self._send_lock:
			self._connection.sendall(packet)
		return packet

	def stop(self):
		"""Stop receiving and close the KISS connection."""
		self._stop_event.set()
		connection = self._connection
		if connection is not None:
			try:
				connection.shutdown(socket.SHUT_RDWR)
			except OSError:
				pass
			try:
				connection.close()
			except OSError:
				pass
		self._connection = None
		if self._thread is not None and self._thread is not threading.current_thread():
			self._thread.join(timeout=2)
		self._thread = None

	def _receive_loop(self):
		decoder = KissDecoder()
		try:
			while not self._stop_event.is_set():
				try:
					data = self._connection.recv(4096)
				except socket.timeout:
					continue
				if not data:
					break
				for frame in decoder.feed(data):
					self._handle_frame(frame)
		except OSError as error:
			if not self._stop_event.is_set():
				self._report_error(error)
		finally:
			self._connection = None

	def _handle_frame(self, frame):
		try:
			addresses, info = decode_ax25_ui_frame(frame)
			parsed = parse_aprs_information(info)
			packet = {
				"source": addresses[1] if len(addresses) > 1 else addresses[0],
				"destination": addresses[0],
				"path": addresses[2:],
				"info": info,
				"parsed": parsed,
			}
		except (IndexError, ValueError) as error:
			self._report_error(error)
			return
		if parsed["type"] in {"ack", "rej"}:
			ack_key = (packet["source"].upper(), parsed["message_id"])
			with self._pending_acks_lock:
				pending_ack = self._pending_acks.get(ack_key)
				if pending_ack is not None:
					pending_ack["response"] = parsed["type"]
					pending_ack["event"].set()
		if (
			self.auto_ack
			and parsed["type"] == "message"
			and parsed.get("message_id")
			and parsed.get("to", "").upper() == self.source.upper()
		):
			try:
				self.send_ack(packet["source"], parsed["message_id"])
			except (RuntimeError, OSError, ValueError) as error:
				self._report_error(error)
		if self.on_packet is not None:
			self._last_packet_at = time.time()
			self.on_packet(packet)

	def _report_error(self, error):
		self._last_error = str(error)
		if self.on_error is not None:
			self.on_error(error)


def receive_aprs_frames(connection, listen_seconds):
	"""Receive and print decoded APRS frames for CLI compatibility."""
	decoder = KissDecoder()
	connection.settimeout(1.0)
	deadline = time.monotonic() + listen_seconds
	while time.monotonic() < deadline:
		try:
			data = connection.recv(4096)
		except socket.timeout:
			continue
		if not data:
			break
		for frame in decoder.feed(data):
			try:
				addresses, info = decode_ax25_ui_frame(frame)
				parsed = parse_aprs_information(info)
			except ValueError as error:
				print(f"Received undecoded frame: {error}")
				continue
			print(f"Received {addresses[1] if len(addresses) > 1 else addresses[0]} -> {parsed}")


def send_aprs_message(host, port, source, destination, message, path, message_id=None):
	"""Connect to Dire Wolf and transmit an APRS message, segmented if needed."""
	packets = []
	with socket.create_connection((host, port), timeout=10) as connection:
		for segment in split_aprs_message(message, message_id):
			info = aprs_message_info(destination, segment, message_id)
			packet = kiss_encode(ax25_ui_frame(source, "APRS", path, info))
			connection.sendall(packet)
			packets.append(packet)
	return packets


def main():
	parser = argparse.ArgumentParser(description="Send one APRS message through Dire Wolf KISS TCP")
	parser.add_argument("--source", help="Your station callsign, optionally with SSID")
	parser.add_argument("--destination", help="APRS recipient callsign, optionally with SSID")
	parser.add_argument("--message", help="Message text, up to 67 characters")
	parser.add_argument("--message-id", help="Optional APRS message ID, 1 to 3 digits")
	parser.add_argument("--path", default="", help="Comma-separated digipeater path, e.g. WIDE1-1,WIDE2-1")
	parser.add_argument("--listen-seconds", type=float, default=DEFAULT_LISTEN_SECONDS, help="Seconds to listen for incoming APRS packets after transmitting")
	parser.add_argument("--receive-only", action="store_true", help="Listen without transmitting a message")
	parser.add_argument("--host", default=DEFAULT_KISS_HOST)
	parser.add_argument("--port", type=int, default=DEFAULT_KISS_PORT)
	args = parser.parse_args()
	if not args.receive_only and not all((args.source, args.destination, args.message)):
		parser.error("--source, --destination, and --message are required unless --receive-only is used")
	if args.receive_only and not args.source:
		args.source = "N0CALL"

	path = [item.strip() for item in args.path.split(",") if item.strip()]
	with socket.create_connection((args.host, args.port), timeout=10) as connection:
		if not args.receive_only:
			packets = []
			for segment in split_aprs_message(args.message, args.message_id):
				info = aprs_message_info(args.destination, segment, args.message_id)
				packet = kiss_encode(ax25_ui_frame(args.source, "APRS", path, info))
				connection.sendall(packet)
				packets.append(packet)
			print(f"Sent {len(packets)} APRS packet(s) to {args.host}:{args.port}")
		print(f"Listening for APRS packets for {args.listen_seconds:g} seconds...")
		receive_aprs_frames(connection, args.listen_seconds)


if __name__ == "__main__":
	main()

