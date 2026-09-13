# Python script for starting and configuring the cave radio and VHF radio communication modems (Fldigi and Dire Wolf)
"""Start, configure, and gracefully stop the Fldigi and Dire Wolf modem apps.

Intended primarily for a Raspberry Pi (Raspbian) host. This module only
covers starting/configuring/stopping the two applications; autonomous
startup, watchdog restarts, and integration with the relay are handled
elsewhere.
"""

import logging
import os
import signal
import socket
import subprocess
import time

import pyfldigi

from config import settings

FLDIGI_START_TIMEOUT_SECONDS = 10
FLDIGI_STOP_TIMEOUT_SECONDS = 5
DIREWOLF_START_TIMEOUT_SECONDS = 10
DIREWOLF_STOP_TIMEOUT_SECONDS = 5

log = logging.getLogger("Modem_App_Control")


class FldigiController:
    """Launch, configure, and shut down the Fldigi application."""

    def __init__(self):
        self._app_monitor = pyfldigi.ApplicationMonitor(
            hostname=settings.fldigi_hostname, port=settings.fldigi_port
        )
        self.client = None

    def start(self, headless=True):
        """Launch fldigi and apply the configured modem/frequency/squelch settings."""
        log.info("Starting fldigi (headless=%s)", headless)
        self._launch(headless=headless)
        self.client = pyfldigi.Client(hostname=settings.fldigi_hostname, port=settings.fldigi_port)
        self.configure()
        return self.client

    def _launch(self, headless):
        """Start the fldigi process directly, since ApplicationMonitor.start() has no --config-dir option."""
        args = [
            settings.fldigi_executable,
            "--arq-server-address", settings.fldigi_hostname,
            "--arq-server-port", str(settings.fldigi_port),
        ]
        if settings.fldigi_config_path:
            args.extend(["--config-dir", settings.fldigi_config_path])
        env = os.environ.copy()
        if headless:
            args = ["xvfb-run", *args, "-display", ":99"]
        else:
            # An SSH session has no DISPLAY of its own; point at the Pi's local X server instead.
            env["DISPLAY"] = settings.fldigi_display
        # Audio device enumeration (PortAudio/ALSA/Pulse) depends on this subset of the
        # environment; log it to compare against an interactive terminal launch if the
        # soundcard list ever differs between the two.
        log.info(
            "Launching fldigi as uid=%s with XDG_RUNTIME_DIR=%s PULSE_SERVER=%s "
            "DBUS_SESSION_BUS_ADDRESS=%s DISPLAY=%s HOME=%s",
            os.getuid(),
            env.get("XDG_RUNTIME_DIR"),
            env.get("PULSE_SERVER"),
            env.get("DBUS_SESSION_BUS_ADDRESS"),
            env.get("DISPLAY"),
            env.get("HOME"),
        )
        self._app_monitor.process = subprocess.Popen(args, env=env)

        start = time.time()
        while True:
            try:
                if self._app_monitor.client.fldigi.name() == "fldigi":
                    return
            except ConnectionRefusedError:
                pass
            if time.time() - start >= FLDIGI_START_TIMEOUT_SECONDS:
                log.warning("Timed out waiting for fldigi to respond over XML-RPC")
                return
            time.sleep(0.5)

    def configure(self, modem=None, frequency_hz=None, squelch_level=None):
        """Apply modem, frequency, and squelch settings right after startup.

        For changes while fldigi is already running, use
        QDX_Fldigi_coms.FldigiReceiver instead.
        """
        if self.client is None:
            raise RuntimeError("fldigi is not started")
        modem = settings.fldigi_modem if modem is None else modem
        frequency_hz = settings.fldigi_frequency_hz if frequency_hz is None else frequency_hz
        squelch_level = settings.fldigi_squelch if squelch_level is None else squelch_level

        if self.client.modem.name != modem:
            log.info("Setting fldigi modem to %s", modem)
            self.client.modem.name = modem
        log.info("Setting fldigi frequency to %.0f Hz", frequency_hz)
        self.client.rig.frequency = frequency_hz
        log.info("Setting fldigi squelch level to %.1f", squelch_level)
        self.client.main.squelch_level = squelch_level
        self.client.main.squelch = squelch_level > 0

    def is_running(self):
        return self._app_monitor.is_running()

    def restart(self, headless=True):
        """Stop and relaunch fldigi, reapplying the configured settings."""
        self.stop()
        return self.start(headless=headless)

    def stop(self):
        """Gracefully shut down fldigi, falling back to a hard kill if needed."""
        log.info("Stopping fldigi")
        try:
            self._app_monitor.stop()
        except Exception as error:
            log.error("Graceful fldigi shutdown failed, killing: %s", error)
            self._app_monitor.kill()
        finally:
            self.client = None


class DirewolfController:
    """Launch and stop the Dire Wolf application as a subprocess."""

    def __init__(self):
        self._process = None

    def start(self):
        """Launch direwolf with the configured executable and config file."""
        if self.is_running():
            log.warning("direwolf is already running")
            return self._process
        args = [settings.direwolf_executable, "-c", settings.direwolf_config_path]
        log.info("Starting direwolf: %s", " ".join(args))
        self._process = subprocess.Popen(args)
        self._wait_until_ready()
        return self._process

    def _wait_until_ready(self):
        """Block until direwolf's KISS TCP port accepts connections, or timeout."""
        start = time.time()
        while True:
            try:
                with socket.create_connection((settings.kiss_hostname, settings.kiss_port), timeout=1):
                    return
            except OSError:
                pass
            if time.time() - start >= DIREWOLF_START_TIMEOUT_SECONDS:
                log.warning("Timed out waiting for direwolf to open its KISS port")
                return
            time.sleep(0.5)

    def is_running(self):
        return self._process is not None and self._process.poll() is None

    def restart(self):
        """Stop and relaunch direwolf."""
        self.stop()
        return self.start()

    def stop(self, timeout=DIREWOLF_STOP_TIMEOUT_SECONDS):
        """Gracefully stop direwolf with SIGTERM, escalating to SIGKILL if needed."""
        if not self.is_running():
            self._process = None
            return
        log.info("Stopping direwolf")
        self._process.send_signal(signal.SIGTERM)
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("direwolf did not exit gracefully, killing")
            self._process.kill()
            self._process.wait(timeout=timeout)
        self._process = None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s : %(message)s")
    fldigi = FldigiController()
    direwolf = DirewolfController()
    try:
        fldigi.start(settings.fldigi_headless)
        direwolf.start()
        log.info("fldigi and direwolf are running; press Ctrl+C to stop")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stopping after Ctrl+C")
    finally:
        direwolf.stop()
        fldigi.stop()


if __name__ == "__main__":
    main()
