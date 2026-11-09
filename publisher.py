#!/usr/bin/env python3
"""
Universal MQTT heartbeat publisher.

Publishes a small JSON heartbeat every HEARTBEAT_INTERVAL seconds in the shape
the Heartbeat Lifecycle subscriber expects:

    topic:   <device_id>/heartbeat            (matches subscriber's "+/heartbeat")
    payload: {"heartbeat": {"imei": "<device_id>", "hostname": ..., ...}}

The subscriber looks for any top-level object containing an "imei" key and uses
that value to look up DEVICE_MAPPING, so the ID printed by `--show-id` is what
goes into the subscriber's .env.

Runs unchanged on Windows 10/11 and Linux (Debian, Ubuntu, Raspberry Pi OS...).

Usage:
    python publisher.py              run forever
    python publisher.py --show-id    print this machine's device ID and exit
    python publisher.py --once       send a single heartbeat and exit (connection test)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from paho.mqtt import client as mqtt_client

try:  # optional: adds uptime / CPU / RAM to the heartbeat
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

BASE_DIR = Path(__file__).resolve().parent
# Explicit path so it works no matter what the working directory is
# (systemd, Task Scheduler running as SYSTEM, double-click, ...).
load_dotenv(BASE_DIR / ".env")

log = logging.getLogger("heartbeat")

SUBSCRIBER_TIMEOUT = 20  # subscriber's TIMEOUT_THRESHOLD, used only for a sanity warning
PAYLOAD_KEY = "heartbeat"

JUNK_SERIALS = {
    "", "0", "none", "null", "n/a", "na", "default string", "to be filled by o.e.m.",
    "to be filled by oem", "system serial number", "not specified", "not applicable",
    "123456789", "0123456789", "chassis serial number", "invalid",
}


# --------------------------------------------------------------------------- #
# Config helpers
# --------------------------------------------------------------------------- #
def env_str(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, "").strip()
    return value if value else default


def env_int(name: str, default: int) -> int:
    value = env_str(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        sys.exit(f"[Config Error] {name} must be a whole number, got {value!r}")


def env_bool(name: str, default: bool) -> bool:
    value = env_str(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Device identity
# --------------------------------------------------------------------------- #
def _machine_id() -> str | None:
    """Stable per-OS-install ID. Same 32-hex-char format on Windows and Linux."""
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value).replace("-", "").lower()
        except OSError:
            return None

    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(path).read_text().strip()
            if value:
                return value.lower()
        except OSError:
            continue
    return None


def _hardware_serial() -> str | None:
    """BIOS/board serial (the one on the sticker). Often junk on cheap mini PCs."""
    serial = ""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "(Get-CimInstance Win32_BIOS).SerialNumber"],
                capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            serial = result.stdout.strip()
        elif sys.platform.startswith("linux"):
            # x86 boards (needs root), then ARM boards like Raspberry Pi
            for path in ("/sys/class/dmi/id/product_serial",
                         "/sys/firmware/devicetree/base/serial-number"):
                try:
                    serial = Path(path).read_text().strip().strip("\x00")
                    if serial:
                        break
                except OSError:
                    continue
    except (OSError, subprocess.SubprocessError):
        return None

    if serial.lower() in JUNK_SERIALS or len(set(serial)) <= 1:
        return None
    return serial


def resolve_device_id() -> tuple[str, str]:
    """Returns (device_id, where_it_came_from)."""
    explicit = env_str("DEVICE_ID")
    if explicit:
        return explicit, "DEVICE_ID in .env"

    source = (env_str("DEVICE_ID_SOURCE", "machine-id") or "machine-id").lower()
    if source not in ("machine-id", "serial", "hostname"):
        sys.exit(f"[Config Error] DEVICE_ID_SOURCE must be machine-id, serial or hostname, got {source!r}")

    if source == "hostname":
        return socket.gethostname(), "hostname"

    if source == "serial":
        serial = _hardware_serial()
        if serial:
            return serial, "hardware serial"
        log.warning("No usable hardware serial found (or no permission to read it); "
                    "falling back to machine-id.")

    machine_id = _machine_id()
    if machine_id:
        return machine_id, "machine-id"

    return f"{uuid.getnode():012x}", "MAC address (fallback)"


def topic_safe(value: str) -> str:
    """MQTT topic levels can't contain '/', '+' or '#'; keep it to plain characters."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


# --------------------------------------------------------------------------- #
# Heartbeat payload
# --------------------------------------------------------------------------- #
def build_payload(device_id: str, seq: int) -> str:
    body: dict = {
        "imei": device_id,  # key name the subscriber looks for
        "seq": seq,
        "ts": int(time.time()),
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
    }
    if psutil is not None:
        try:
            body["uptime_s"] = int(time.time() - psutil.boot_time())
            body["cpu_pct"] = psutil.cpu_percent(interval=None)
            body["mem_pct"] = psutil.virtual_memory().percent
        except Exception:  # never let optional stats break the heartbeat
            pass
    return json.dumps({PAYLOAD_KEY: body}, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# Publisher
# --------------------------------------------------------------------------- #
class HeartbeatPublisher:
    def __init__(self, device_id: str):
        self.device_id = device_id
        self.host = env_str("HIVEMQ_HOST")
        if not self.host:
            sys.exit("[Config Error] HIVEMQ_HOST is empty. Copy .env.example to .env and fill it in.")
        self.port = env_int("HIVEMQ_PORT", 8883)
        self.interval = max(1, env_int("HEARTBEAT_INTERVAL", 5))
        self.qos = env_int("MQTT_QOS", 1)
        if self.qos not in (0, 1):
            sys.exit("[Config Error] MQTT_QOS must be 0 or 1.")

        template = env_str("HEARTBEAT_TOPIC", "{device_id}/heartbeat")
        try:
            self.topic = template.format(device_id=topic_safe(device_id),
                                         hostname=topic_safe(socket.gethostname()))
        except (KeyError, IndexError):
            sys.exit("[Config Error] HEARTBEAT_TOPIC may only use {device_id} and {hostname}.")

        if self.interval * 2 > SUBSCRIBER_TIMEOUT:
            log.warning("HEARTBEAT_INTERVAL=%ss is close to the subscriber's %ss timeout; "
                        "one lost packet could trigger a false DOWN alert. 5s is recommended.",
                        self.interval, SUBSCRIBER_TIMEOUT)

        self.connected = threading.Event()
        self.stop_event = threading.Event()
        self._announce_next_publish = True

        # Unique client ID per process: two clients with the same ID kick each other off the broker.
        client_id = f"hb-{topic_safe(device_id)[:40]}-{uuid.uuid4().hex[:6]}"
        self.client = mqtt_client.Client(
            callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
        )
        user = env_str("HIVEMQ_USER")
        if user:
            self.client.username_pw_set(user, env_str("HIVEMQ_PASSWORD", ""))
        if env_bool("MQTT_TLS", True):
            self.client.tls_set()  # uses the OS certificate store; HiveMQ Cloud requires TLS
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    # -- callbacks ---------------------------------------------------------- #
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("Broker refused connection: %s (check HIVEMQ_USER / HIVEMQ_PASSWORD)", reason_code)
            return
        log.info("Connected to %s:%s", self.host, self.port)
        self._announce_next_publish = True
        self.connected.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected.clear()
        if not self.stop_event.is_set():
            log.warning("Disconnected (%s). Reconnecting automatically...", reason_code)

    # -- main loop ---------------------------------------------------------- #
    def start(self):
        # connect_async + loop_start: paho keeps retrying in the background,
        # so it also survives booting before the network/DNS is ready.
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()

    def stop(self):
        self.stop_event.set()
        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()

    def publish_once(self, seq: int) -> bool:
        info = self.client.publish(self.topic, build_payload(self.device_id, seq),
                                   qos=self.qos, retain=False)
        if info.rc != mqtt_client.MQTT_ERR_SUCCESS:
            log.warning("Publish failed (rc=%s); will retry next interval.", info.rc)
            return False
        if self._announce_next_publish:
            log.info("Heartbeats flowing -> %s every %ss", self.topic, self.interval)
            self._announce_next_publish = False
        log.debug("Heartbeat #%s sent", seq)
        return True

    def run_forever(self):
        self.start()
        seq = 0
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                if self.connected.is_set():
                    seq += 1
                    self.publish_once(seq)
                else:
                    log.debug("Not connected; skipping heartbeat.")
            except Exception:
                log.exception("Unexpected error while publishing; continuing.")
            elapsed = time.monotonic() - started
            self.stop_event.wait(max(0.0, self.interval - elapsed))
        log.info("Publisher stopped.")

    def run_once(self, timeout: float = 20.0) -> int:
        self.start()
        if not self.connected.wait(timeout):
            log.error("Could not connect to %s:%s within %ss.", self.host, self.port, int(timeout))
            self.stop()
            return 1
        info = self.client.publish(self.topic, build_payload(self.device_id, 1), qos=1, retain=False)
        try:
            info.wait_for_publish(timeout=10)
        except (RuntimeError, ValueError) as err:
            log.error("Publish failed: %s", err)
            self.stop()
            return 1
        ok = info.is_published()
        log.info("Single heartbeat %s -> %s", "delivered" if ok else "NOT confirmed", self.topic)
        self.stop()
        return 0 if ok else 1


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def setup_logging():
    level = getattr(logging, (env_str("LOG_LEVEL", "INFO") or "INFO").upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers: list[logging.Handler] = []
    if sys.stdout is not None:  # pythonw.exe (Windows background task) has no console
        handlers.append(logging.StreamHandler(sys.stdout))
    if env_bool("LOG_TO_FILE", True):
        log_dir = BASE_DIR / "logs"
        log_dir.mkdir(exist_ok=True)
        handlers.append(RotatingFileHandler(log_dir / "publisher.log", maxBytes=1_000_000,
                                            backupCount=5, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(fmt)
    logging.basicConfig(level=level, handlers=handlers)
    logging.getLogger("paho").setLevel(logging.WARNING)


def main() -> int:
    parser = argparse.ArgumentParser(description="MQTT heartbeat publisher")
    parser.add_argument("--show-id", action="store_true", help="print this device's ID and exit")
    parser.add_argument("--once", action="store_true", help="send one heartbeat and exit")
    args = parser.parse_args()

    setup_logging()
    device_id, source = resolve_device_id()

    if args.show_id:
        print(f"Device ID: {device_id}   (source: {source})")
        print(f'Add to the subscriber\'s DEVICE_MAPPING:  "{device_id}": "<friendly name>"')
        return 0

    if psutil is not None:
        psutil.cpu_percent(interval=None)  # prime: the first reading is always 0.0

    publisher = HeartbeatPublisher(device_id)
    log.info("Device ID %s (source: %s) | host %s", device_id, source, socket.gethostname())

    if args.once:
        return publisher.run_once()

    def handle_signal(signum, _frame):
        log.info("Received signal %s, shutting down...", signum)
        publisher.stop_event.set()

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, sig_name):
            signal.signal(getattr(signal, sig_name), handle_signal)

    try:
        publisher.run_forever()
    finally:
        publisher.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
