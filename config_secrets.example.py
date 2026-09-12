"""Template for config_secrets.py.

Copy this file to config_secrets.py (which is gitignored) and fill in your
station's real callsigns and frequency. config.py imports config_secrets.py
and will refuse to start if it is missing.
"""

from dataclasses import dataclass


@dataclass
class Secrets:
    source_callsign: str = "N0CALL"
    aprs_destination_callsign: str = "N0CALL"
    radiomsg_source_callsign: str = "N0CALL"
    fldigi_frequency_hz: int = 0


secrets = Secrets()
