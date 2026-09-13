"""Single place (if you don't count the config_secrets.py file) to edit user-configurable settings for the whole relay system.

Edit the values in ``settings`` below to configure this station. Every other
module imports ``settings`` from here instead of hard-coding values.
"""

import os
from dataclasses import dataclass

try:
    from config_secrets import secrets
except ImportError as error:
    raise RuntimeError(
        "config_secrets.py is missing. Copy config_secrets.example.py to "
        "config_secrets.py and fill in your station's callsigns/frequency."
    ) from error

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


@dataclass
class Settings:
    # --- Station identity (see config_secrets.py) ---
    source_callsign: str = secrets.source_callsign
    aprs_destination_callsign: str = secrets.aprs_destination_callsign
    radiomsg_source_callsign: str = secrets.radiomsg_source_callsign  # used when transmitting RadioMSG frames

    # --- Fldigi ---
    fldigi_hostname: str = "127.0.0.1"
    fldigi_port: int = 7362
    fldigi_modem: str = "THOR4"
    fldigi_frequency_hz: int = secrets.fldigi_frequency_hz  # see config_secrets.py
    fldigi_sideband: str = "USB"  # e.g., "USB" or "LSB" NOT IMPLEMENTED YET
    fldigi_cursor_position: int = 1400  # initial cursor position in fldigi GUI
    fldigi_squelch: float = 20.0  # squelch_level (0-100); squelch is enabled automatically when > 0
    fldigi_executable: str = "fldigi"
    fldigi_headless: bool = False  # when True, run under xvfb-run; required when there is no DISPLAY (e.g. Raspberry Pi over SSH)
    fldigi_display: str = ":0"  # X display to use when fldigi_headless is False and launching over SSH
    fldigi_start_args: list = None
    # fldigi_config_path: str = None
    # fldigi_start_args: list = field(default_factory=lambda: ["-display", ":99"])
    fldigi_config_path: str = os.path.join(PROJECT_ROOT, "modemConfigFiles/.fldigi/")

    # --- Dire Wolf / KISS (APRS) ---
    kiss_hostname: str = "127.0.0.1"
    kiss_port: int = 8001
    direwolf_executable: str = "direwolf"
    direwolf_config_path: str = os.path.join(PROJECT_ROOT, "modemConfigFiles/direwolf.conf")

    # --- Soundcard detection (checked with `arecord -l` before starting each modem) ---
    fldigi_soundcard_name: str = "UNIT 2"  # QDX radio's ALSA capture device name
    direwolf_soundcard_name: str = "USB PnP Sound Device"  # USB sound card (e.g. digirig) name

    # --- Relay timing/behaviour ---
    poll_interval_seconds: float = 0.5
    fldigi_rx_idle_seconds: float = 1.5
    fldigi_raw_preview_limit: int = 4000
    health_interval_seconds: int = 300

    # --- Relay control API (between Crossband_Relay.py and the website) ---
    relay_api_host: str = "127.0.0.1"
    relay_api_port: int = 5050

    # --- Logging ---
    traffic_log_path: str = os.path.join(PROJECT_ROOT, "messages.jsonl")


settings = Settings()
