"""Detect required ALSA capture devices (radio + USB sound card) via `arecord -l`.

Both fldigi (through the QDX radio's built-in soundcard) and Dire Wolf
(through a USB sound card such as a digirig) depend on their ALSA capture
device being plugged in. Card numbers shift depending on plug order, so
devices are matched by a name substring instead of a fixed card index.
"""

import logging
import re
import subprocess

log = logging.getLogger("soundcard_check")

_CARD_LINE_RE = re.compile(r"^card \d+: \S+ \[(?P<name>[^\]]+)\]")


def list_capture_card_names(timeout=5):
    """Return the descriptive names of currently attached ALSA capture devices."""
    try:
        result = subprocess.run(
            ["arecord", "-l"], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        log.error("Unable to run 'arecord -l' to detect soundcards: %s", error)
        return []
    card_names = []
    for line in result.stdout.splitlines():
        match = _CARD_LINE_RE.match(line.strip())
        if match:
            card_names.append(match.group("name"))
    return card_names


def find_soundcard(name_substring, card_names=None):
    """Return True if an attached capture device's name contains name_substring."""
    card_names = list_capture_card_names() if card_names is None else card_names
    return any(name_substring.lower() in name.lower() for name in card_names)


def require_soundcard(name_substring, purpose):
    """Raise RuntimeError with a clear message if the named capture device is missing."""
    card_names = list_capture_card_names()
    if find_soundcard(name_substring, card_names):
        return
    detected = ", ".join(card_names) if card_names else "none"
    message = (
        f"Required soundcard for {purpose} not found (looking for a capture device "
        f"matching '{name_substring}'). Detected capture devices: {detected}. "
        "Check the USB connection and run 'arecord -l' to verify."
    )
    log.error(message)
    raise RuntimeError(message)
