"""Parser for the RadioMSG-style message protocol relayed over Fldigi/Thor4.

Each message is a block of lines with no fixed field order (aside from the
header always being first), terminated by a trailing checksum line::

    [from]:[to]
    via:[relay]        <- optional relay/repeater callsign
    sms:[message text]
    [checksum]

Example::

    unit5:*
    sms:Try again
    lmdo

    unit5:unit7
    sms:Testing,how copy?
    via:unit1
    fkgc
"""

import re
import time
from dataclasses import dataclass, field
from typing import List, Optional

# Not anchored to the start of the line: noise decoded by Thor4 right before a real
# transmission locks in can end up glued onto the front of the same line with no newline.
HEADER_RE = re.compile(r'([A-Za-z0-9_\-]+):([A-Za-z0-9_\-*]+)\s*$')
VIA_RE = re.compile(r'via:\s*(.*)$', re.IGNORECASE)
SMS_RE = re.compile(r'sms:\s*(.*)$', re.IGNORECASE)
RLY_RE = re.compile(r'rly:\s*(.*)$', re.IGNORECASE)
ID_RE = re.compile(r'id:\s*(.*)$', re.IGNORECASE)
POS_RE = re.compile(r'pos:\s*(.*)$', re.IGNORECASE)
PIC_RE = re.compile(r'pic:\s*(.*)$', re.IGNORECASE)
RD_RE = re.compile(r'rd:\s*(.*)$', re.IGNORECASE)
RO_RE = re.compile(r'ro:\s*(.*)$', re.IGNORECASE)
TIM_RE = re.compile(r'tim:\s*(.*)$', re.IGNORECASE)
CHECKSUM_RE = re.compile(r'([A-Za-z0-9]+)\s*$')

# Control characters (e.g. SOH 0x01 at message start, EOT 0x04 at message end) can end up
# glued directly onto the header/checksum text with no newline separating them.
CONTROL_CHAR_RE = re.compile(r'[\x00-\x1f\x7f]')

SOH = '\x01'  # RadioMSG prefixes every message buffer with this before computing/sending the CRC

# CRC-16 table ported verbatim from RadioMSG's RMsgCheckSum.Crc16() (Java), which is in turn
# based on the Pskmail Crc16 class. This is a reversed (LSB-first) CRC-16/ARC variant.
_CRC16_TABLE = [
    0x0000, 0xC0C1, 0xC181, 0x0140, 0xC301, 0x03C0, 0x0280, 0xC241,
    0xC601, 0x06C0, 0x0780, 0xC741, 0x0500, 0xC5C1, 0xC481, 0x0440,
    0xCC01, 0x0CC0, 0x0D80, 0xCD41, 0x0F00, 0xCFC1, 0xCE81, 0x0E40,
    0x0A00, 0xCAC1, 0xCB81, 0x0B40, 0xC901, 0x09C0, 0x0880, 0xC841,
    0xD801, 0x18C0, 0x1980, 0xD941, 0x1B00, 0xDBC1, 0xDA81, 0x1A40,
    0x1E00, 0xDEC1, 0xDF81, 0x1F40, 0xDD01, 0x1DC0, 0x1C80, 0xDC41,
    0x1400, 0xD4C1, 0xD581, 0x1540, 0xD701, 0x17C0, 0x1680, 0xD641,
    0xD201, 0x12C0, 0x1380, 0xD341, 0x1100, 0xD1C1, 0xD081, 0x1040,
    0xF001, 0x30C0, 0x3180, 0xF141, 0x3300, 0xF3C1, 0xF281, 0x3240,
    0x3600, 0xF6C1, 0xF781, 0x3740, 0xF501, 0x35C0, 0x3480, 0xF441,
    0x3C00, 0xFCC1, 0xFD81, 0x3D40, 0xFF01, 0x3FC0, 0x3E80, 0xFE41,
    0xFA01, 0x3AC0, 0x3B80, 0xFB41, 0x3900, 0xF9C1, 0xF881, 0x3840,
    0x2800, 0xE8C1, 0xE981, 0x2940, 0xEB01, 0x2BC0, 0x2A80, 0xEA41,
    0xEE01, 0x2EC0, 0x2F80, 0xEF41, 0x2D00, 0xEDC1, 0xEC81, 0x2C40,
    0xE401, 0x24C0, 0x2580, 0xE541, 0x2700, 0xE7C1, 0xE681, 0x2640,
    0x2200, 0xE2C1, 0xE381, 0x2340, 0xE101, 0x21C0, 0x2080, 0xE041,
    0xA001, 0x60C0, 0x6180, 0xA141, 0x6300, 0xA3C1, 0xA281, 0x6240,
    0x6600, 0xA6C1, 0xA781, 0x6740, 0xA501, 0x65C0, 0x6480, 0xA441,
    0x6C00, 0xACC1, 0xAD81, 0x6D40, 0xAF01, 0x6FC0, 0x6E80, 0xAE41,
    0xAA01, 0x6AC0, 0x6B80, 0xAB41, 0x6900, 0xA9C1, 0xA881, 0x6840,
    0x7800, 0xB8C1, 0xB981, 0x7940, 0xBB01, 0x7BC0, 0x7A80, 0xBA41,
    0xBE01, 0x7EC0, 0x7F80, 0xBF41, 0x7D00, 0xBDC1, 0xBC81, 0x7C40,
    0xB401, 0x74C0, 0x7580, 0xB541, 0x7700, 0xB7C1, 0xB681, 0x7640,
    0x7200, 0xB2C1, 0xB381, 0x7340, 0xB101, 0x71C0, 0x7080, 0xB041,
    0x5000, 0x90C1, 0x9181, 0x5140, 0x9301, 0x53C0, 0x5280, 0x9241,
    0x9601, 0x56C0, 0x5780, 0x9741, 0x5500, 0x95C1, 0x9481, 0x5440,
    0x9C01, 0x5CC0, 0x5D80, 0x9D41, 0x5F00, 0x9FC1, 0x9E81, 0x5E40,
    0x5A00, 0x9AC1, 0x9B81, 0x5B40, 0x9901, 0x59C0, 0x5880, 0x9841,
    0x8801, 0x48C0, 0x4980, 0x8941, 0x4B00, 0x8BC1, 0x8A81, 0x4A40,
    0x4E00, 0x8EC1, 0x8F81, 0x4F40, 0x8D01, 0x4DC0, 0x4C80, 0x8C41,
    0x4400, 0x84C1, 0x8581, 0x4540, 0x8701, 0x47C0, 0x4680, 0x8641,
    0x8201, 0x42C0, 0x4380, 0x8341, 0x4100, 0x81C1, 0x8081, 0x4040,
]


def crc16(text):
    """Port of RadioMSG's RMsgCheckSum.Crc16(): returns the 4-character lowercase checksum."""
    crc = 0xFFFF
    for b in text.encode('utf-8'):
        crc = (crc >> 8) ^ _CRC16_TABLE[(crc ^ b) & 0xFF]

    hex_str = ('ffff' + format(crc, 'x'))[-4:]
    # Remap digits 0-9 to letters g-p so the checksum only ever contains letters (faster to
    # transmit via varicode/CCIR476 -- no case shifting needed), matching RMsgCheckSum.Crc16().
    return ''.join(chr(ord(c) + 55) if c < 'a' else c for c in hex_str)


def expected_checksum(
    from_call, to_call, message, via=None, rly=None, msg_id=None,
    position=None, picture=None, received_date=None, received_offset=None,
    time_sync=None, password='',
):
    """Reconstructs the wire buffer RadioMSG hashes and returns the expected checksum for it."""
    buffer = SOH + from_call.lower() + ':' + to_call.lower() + '\n'
    if message:
        buffer += 'sms:' + message + '\n'
    if via:
        buffer += 'via:' + via.lower() + '\n'
    if rly:
        buffer += 'rly:' + rly.lower() + '\n'
    if position:
        buffer += 'pos:' + position + '\n'
    if picture:
        buffer += 'pic:' + picture + '\n'
    if msg_id:
        buffer += 'id:' + msg_id + '\n'
    if received_date:
        buffer += 'rd:' + received_date + '\n'
    elif received_offset:
        buffer += 'ro:' + received_offset + '\n'
    if time_sync:
        buffer += 'tim:' + time_sync + '\n'
    return crc16(buffer + password)


@dataclass
class RadioMsg:
    """A single parsed RadioMSG message."""

    from_call: str
    to_call: str
    message: str
    checksum: str
    via: Optional[str] = None
    rly: Optional[str] = None
    msg_id: Optional[str] = None
    position: Optional[str] = None
    picture: Optional[str] = None
    received_date: Optional[str] = None
    received_offset: Optional[str] = None
    time_sync: Optional[str] = None
    received_at: float = field(default_factory=time.time)
    raw: str = ''
    checksum_valid: Optional[bool] = None

    @property
    def is_broadcast(self):
        return self.to_call == '*'


class RadioMsgParser:
    """Incrementally parse decoded RX text into complete RadioMsg records.

    Feed it text as it arrives (it does not need to be line-aligned); completed
    messages are returned by :meth:`feed` as soon as their checksum line is seen.
    """

    def __init__(self):
        self._buffer = ''
        self._reset_current()

    def _reset_current(self):
        self._from_call = None
        self._to_call = None
        self._via = None
        self._rly = None
        self._id = None
        self._position = None
        self._picture = None
        self._received_date = None
        self._received_offset = None
        self._time_sync = None
        self._sms = None
        self._raw_lines = []

    @property
    def _in_message(self):
        return self._from_call is not None

    def feed(self, text):
        """Feed a chunk of decoded RX text. Returns a list of RadioMsg completed by this chunk."""
        if not text:
            return []
        self._buffer += text.replace('\r\n', '\n').replace('\r', '\n')
        messages = []
        while '\n' in self._buffer:
            line, self._buffer = self._buffer.split('\n', 1)
            line = CONTROL_CHAR_RE.sub('', line.strip())
            msg = self._process_line(line)
            if msg is not None:
                messages.append(msg)
        return messages

    def _process_line(self, line):
        # Check known field prefixes before the generic header pattern, since a line like
        # 'via:unit1' would otherwise also match the generic '<word>:<word>' header regex.
        via_match = VIA_RE.search(line)
        sms_match = SMS_RE.search(line)
        rly_match = RLY_RE.search(line)
        id_match = ID_RE.search(line)
        pos_match = POS_RE.search(line)
        pic_match = PIC_RE.search(line)
        rd_match = RD_RE.search(line)
        ro_match = RO_RE.search(line)
        tim_match = TIM_RE.search(line)
        if any((via_match, sms_match, rly_match, id_match, pos_match,
            pic_match, rd_match, ro_match, tim_match)):
            if not self._in_message:
                return None  # noise outside a message block
            self._raw_lines.append(line)
            if via_match:
                self._via = via_match.group(1)
            elif rly_match:
                self._rly = rly_match.group(1)
            elif id_match:
                self._id = id_match.group(1)
            elif pos_match:
                self._position = pos_match.group(1)
            elif pic_match:
                self._picture = pic_match.group(1)
            elif rd_match:
                self._received_date = rd_match.group(1)
            elif ro_match:
                self._received_offset = ro_match.group(1)
            elif tim_match:
                self._time_sync = tim_match.group(1)
            else:
                self._sms = sms_match.group(1)
            return None

        header_match = HEADER_RE.search(line)
        if header_match:
            if self._in_message:
                # Previous block never reached a checksum line; discard it as incomplete.
                self._reset_current()
            self._from_call, self._to_call = header_match.groups()
            self._raw_lines = [line]
            return None

        if not self._in_message or not line:
            return None  # noise outside a message block, or a blank line inside one

        self._raw_lines.append(line)

        if self._sms is None:
            # Reached a non-prefixed line before any sms: line -- malformed block, discard.
            self._reset_current()
            return None

        checksum_match = CHECKSUM_RE.search(line)
        checksum = checksum_match.group(1) if checksum_match else line

        msg = RadioMsg(
            from_call=self._from_call,
            to_call=self._to_call,
            message=self._sms,
            checksum=checksum,
            via=self._via,
            rly=self._rly,
            msg_id=self._id,
            position=self._position,
            picture=self._picture,
            received_date=self._received_date,
            received_offset=self._received_offset,
            time_sync=self._time_sync,
            raw='\n'.join(self._raw_lines),
        )
        # 'ssss' is a fixed sentinel checksum used by RadioMSG for Selcall/Telcall messages.
        msg.checksum_valid = (
            checksum.lower() == 'ssss'
            or checksum.lower() == expected_checksum(
                msg.from_call, msg.to_call, msg.message, msg.via, msg.rly, msg.msg_id,
                msg.position, msg.picture, msg.received_date, msg.received_offset,
                msg.time_sync,
            )
        )
        self._reset_current()
        return msg
