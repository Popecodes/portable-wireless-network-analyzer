#!/usr/bin/env python3
"""
============================================================
PNA V2 - Raspberry Pi Core
Software v1.0

Full portable network analyzer UI/core for:
- Raspberry Pi 4
- ESP32-S3 radio coprocessor
- 4.0" 480x320 ST7796S SPI TFT
- dual rotary encoders
- Ethernet diagnostics
- passive Wi-Fi/BLE/RF analysis
- resilient CSV logging
- terminal/SSH fallback UI
- safe shutdown

DISPLAY WIRING
--------------
TFT CS       -> GPIO8  / physical pin 24 / SPI0 CE0
TFT RESET    -> GPIO24 / physical pin 18
TFT DC/RS    -> GPIO25 / physical pin 22
TFT SDI MOSI -> GPIO10 / physical pin 19
TFT SCK      -> GPIO11 / physical pin 23
TFT SDO MISO -> GPIO9  / physical pin 21 (not required to draw)
TFT LED      -> existing power harness; not controlled here
TFT VCC      -> existing power harness; not controlled here
TFT GND      -> common GND

TOUCH PINS
----------
Ignored.

LEFT ENCODER
------------
CLK GPIO5 / pin 29
DT  GPIO6 / pin 31
SW  GPIO13 / pin 33
Rotate = page
Click  = STATUS/home
Hold 5 sec = safe shutdown

RIGHT ENCODER
-------------
CLK GPIO16 / pin 36
DT  GPIO20 / pin 38
SW  GPIO21 / pin 40
Rotate = select Wi-Fi/BLE/Ethernet entry
Ethernet click = ping or close details; hold = inspect device

ESP32 UART
----------
/dev/serial0 @ 115200
ESP32 GPIO44 TX -> Pi GPIO15 RX / physical pin 10

Notes
-----
- Uses the TFT directly through /dev/spidev0.0.
- No framebuffer overlay is required.
- If the TFT cannot initialize, the PNA continues headless over SSH.
- Logging errors do not crash the analyzer. If storage fails, logging
  disables itself and the analyzer keeps running.
============================================================
"""

import argparse
import csv
import math
import ipaddress
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import shutil
import socket
import subprocess
import sys
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path

import serial

from gpiozero import (
    Button,
    Device,
    OutputDevice,
    RotaryEncoder,
)
from gpiozero.pins.lgpio import LGPIOFactory


# ============================================================
# VERSION / CONFIG
# ============================================================

CORE_VERSION = "1.0"
ESP_EXPECTED_VERSION = "1.0"

SERIAL_PORT = "/dev/serial0"
SERIAL_BAUD = 115200

from wifi_monitor_backend import WiFiMonitor

ESP_TIMEOUT = 30
WIFI_TIMEOUT = 60
BLE_TIMEOUT = 50
RF_TIMEOUT = 30

PAGES = [
    "STATUS",
    "WIFI",
    "CHANNELS",
    "RF",
    "SIGNAL",
    "BLE",
    "ETHERNET",
    "MONITOR",
    "SYSTEM",
]

# GPIO mapping
LEFT_CLK = 5
LEFT_DT = 6
LEFT_SW = 13

RIGHT_CLK = 16
RIGHT_DT = 20
RIGHT_SW = 21

TFT_DC = 25
TFT_RESET = 24
TFT_SPI_BUS = 0
TFT_SPI_DEVICE = 0
TFT_SPI_SPEED = 32_000_000

TFT_WIDTH = 480
TFT_HEIGHT = 320

# ST7796S MADCTL.
# 0x28 = landscape with BGR.
# If the image is mirrored/rotated on your exact module, try:
# 0xE8, 0x48, or 0x88.
TFT_MADCTL = 0xE8

LOG_DIR = Path("/home/prestonp/pna/logs")
WIFI_LOG_FILE = LOG_DIR / "wifi_samples.csv"
BLE_LOG_FILE = LOG_DIR / "ble_samples.csv"
RF_LOG_FILE = LOG_DIR / "rf_samples.csv"

LOG_FLUSH_INTERVAL = 8.0
LOG_MAX_PENDING = 50

DISPLAY_REFRESH = 0.35
TERMINAL_REFRESH = 0.50

Device.pin_factory = LGPIOFactory()


# ============================================================
# OPTIONAL DISPLAY IMPORTS
# ============================================================

DISPLAY_IMPORT_ERROR = None

try:
    import spidev
    from PIL import Image, ImageDraw, ImageFont
except Exception as exc:
    spidev = None
    Image = None
    ImageDraw = None
    ImageFont = None
    DISPLAY_IMPORT_ERROR = str(exc)


# ============================================================
# GENERAL HELPERS
# ============================================================

def clamp(value, low, high):
    return max(low, min(high, value))


def quality(rssi):
    if rssi >= -50:
        return "EXCELLENT"
    if rssi >= -60:
        return "GOOD"
    if rssi >= -70:
        return "FAIR"
    if rssi >= -80:
        return "WEAK"
    return "POOR"


def quality_short(rssi):
    q = quality(rssi)
    return {
        "EXCELLENT": "EXCL",
        "GOOD": "GOOD",
        "FAIR": "FAIR",
        "WEAK": "WEAK",
        "POOR": "POOR",
    }[q]


def signal_percent(rssi):
    # Practical UI mapping, not RF calibration.
    return clamp(int((rssi + 100) * 2), 0, 100)


def signal_bar_text(rssi, width=12):
    filled = int(signal_percent(rssi) / 100 * width)
    return "#" * filled + "." * (width - filled)


def safe_text(value, maximum=64):
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text[:maximum]


def run_cmd(command, timeout=1.5):
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def read_text_file(path, default=""):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except Exception:
        return default


def format_uptime(seconds):
    seconds = int(max(0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"

    if minutes:
        return f"{minutes}m {seconds:02d}s"

    return f"{seconds}s"


# ============================================================
# RESILIENT CSV LOGGER
# ============================================================

class SafeCSVLogger:
    """
    Buffered logger that fails open.

    If the SD card/filesystem throws an I/O error, the analyzer
    continues running and this logger disables itself instead of
    crashing the whole PNA.
    """

    def __init__(self, path, header, enabled=True):
        self.path = Path(path)
        self.header = list(header)
        self.enabled = bool(enabled)

        self.handle = None
        self.writer = None
        self.pending = []

        self.last_flush = time.time()
        self.error = ""

        if self.enabled:
            self._open()

    @property
    def status(self):
        if not self.enabled:
            if self.error:
                return f"OFF ({self.error[:28]})"
            return "OFF"
        return "ON"

    def _open(self):
        try:
            self.path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            new_file = (
                not self.path.exists()
                or self.path.stat().st_size == 0
            )

            self.handle = open(
                self.path,
                "a",
                newline="",
                encoding="utf-8",
                buffering=8192,
            )

            self.writer = csv.writer(self.handle)

            if new_file:
                self.writer.writerow(self.header)
                self.handle.flush()

        except OSError as exc:
            self._disable(exc)

    def _disable(self, exc):
        self.error = f"{type(exc).__name__}: {exc}"
        self.enabled = False
        self.pending.clear()

        if self.handle:
            try:
                self.handle.close()
            except Exception:
                pass

        self.handle = None
        self.writer = None

    def append(self, row):
        if not self.enabled:
            return

        self.pending.append(list(row))

        now = time.time()

        if (
            len(self.pending) >= LOG_MAX_PENDING
            or now - self.last_flush >= LOG_FLUSH_INTERVAL
        ):
            self.flush()

    def flush(self):
        if (
            not self.enabled
            or not self.pending
            or self.writer is None
        ):
            return

        rows = self.pending
        self.pending = []

        try:
            self.writer.writerows(rows)
            self.handle.flush()
            self.last_flush = time.time()

        except OSError as exc:
            self._disable(exc)

    def close(self):
        if self.enabled:
            self.flush()

        if self.handle:
            try:
                self.handle.close()
            except Exception:
                pass

        self.handle = None


# ============================================================
# ST7796S SPI DISPLAY
# ============================================================

class ST7796SDisplay:
    """
    Small direct SPI driver for the red 4.0" 480x320 ST7796S
    module. Touch is intentionally unused.
    """

    def __init__(self):
        if spidev is None or Image is None:
            raise RuntimeError(
                "Display dependencies missing: "
                + str(DISPLAY_IMPORT_ERROR)
            )

        if not Path("/dev/spidev0.0").exists():
            raise RuntimeError(
                "/dev/spidev0.0 not found; SPI is not enabled"
            )

        self.width = TFT_WIDTH
        self.height = TFT_HEIGHT

        self.dc = OutputDevice(
            TFT_DC,
            active_high=True,
            initial_value=False,
        )

        self.reset = OutputDevice(
            TFT_RESET,
            active_high=True,
            initial_value=True,
        )

        self.spi = spidev.SpiDev()
        self.spi.open(
            TFT_SPI_BUS,
            TFT_SPI_DEVICE,
        )

        self.spi.mode = 0
        self.spi.max_speed_hz = TFT_SPI_SPEED
        self.spi.lsbfirst = False

        self._init_panel()

        # Lookup tables for faster RGB565 conversion.
        self._r565 = [
            (value & 0xF8) << 8
            for value in range(256)
        ]
        self._g565 = [
            (value & 0xFC) << 3
            for value in range(256)
        ]
        self._b565 = [
            value >> 3
            for value in range(256)
        ]

    def _spi_write(self, data):
        if not data:
            return

        if isinstance(data, int):
            data = bytes([data])

        # writebytes2 uses the Linux SPI write path and handles
        # payloads larger than the kernel SPI transfer buffer.
        self.spi.writebytes2(data)

    def command(self, cmd, data=b""):
        self.dc.off()
        self._spi_write(bytes([cmd]))

        if data:
            self.dc.on()
            self._spi_write(bytes(data))

    def _hardware_reset(self):
        self.reset.on()
        time.sleep(0.020)

        self.reset.off()
        time.sleep(0.120)

        self.reset.on()
        time.sleep(0.200)

    def _init_panel(self):
        self._hardware_reset()

        # Software reset
        self.command(0x01)
        time.sleep(0.150)

        # Sleep out
        self.command(0x11)
        time.sleep(0.120)

        # Manufacturer command unlock
        self.command(0xF0, [0xC3])
        self.command(0xF0, [0x96])

        # RGB565
        self.command(0x3A, [0x55])

        # Landscape / BGR
        self.command(0x36, [TFT_MADCTL])

        # Inversion off, normal display mode
        self.command(0x20)
        self.command(0x13)

        # Relock manufacturer command set
        self.command(0xF0, [0x3C])
        self.command(0xF0, [0x69])

        # Display on
        self.command(0x29)
        time.sleep(0.050)

        self.set_window(
            0,
            0,
            self.width - 1,
            self.height - 1,
        )

    def set_window(self, x0, y0, x1, y1):
        self.command(
            0x2A,
            [
                (x0 >> 8) & 0xFF,
                x0 & 0xFF,
                (x1 >> 8) & 0xFF,
                x1 & 0xFF,
            ],
        )

        self.command(
            0x2B,
            [
                (y0 >> 8) & 0xFF,
                y0 & 0xFF,
                (y1 >> 8) & 0xFF,
                y1 & 0xFF,
            ],
        )

    def _rgb565_bytes(self, image):
        image = image.convert("RGB")

        if image.size != (self.width, self.height):
            image = image.resize(
                (self.width, self.height)
            )

        raw = image.tobytes()

        pixels = self.width * self.height
        out = bytearray(pixels * 2)

        r565 = self._r565
        g565 = self._g565
        b565 = self._b565

        src = 0
        dst = 0

        for _ in range(pixels):
            r = raw[src]
            g = raw[src + 1]
            b = raw[src + 2]

            value = (
                r565[r]
                | g565[g]
                | b565[b]
            )

            out[dst] = (value >> 8) & 0xFF
            out[dst + 1] = value & 0xFF

            src += 3
            dst += 2

        return out

    def show(self, image):
        payload = self._rgb565_bytes(image)

        self.set_window(
            0,
            0,
            self.width - 1,
            self.height - 1,
        )

        self.dc.off()
        self._spi_write(bytes([0x2C]))

        self.dc.on()
        self._spi_write(payload)

    def solid(self, rgb):
        image = Image.new(
            "RGB",
            (self.width, self.height),
            rgb,
        )
        self.show(image)

    def close(self):
        try:
            self.spi.close()
        except Exception:
            pass

        try:
            self.dc.close()
        except Exception:
            pass

        try:
            self.reset.close()
        except Exception:
            pass


# ============================================================
# DISPLAY UI RENDERER
# ============================================================

class TFTUI:
    BG = (8, 12, 18)
    PANEL = (18, 27, 38)
    PANEL_2 = (25, 38, 53)

    TEXT = (235, 241, 247)
    MUTED = (145, 160, 177)

    ACCENT = (51, 194, 255)
    GREEN = (66, 211, 146)
    YELLOW = (250, 204, 21)
    RED = (255, 91, 91)
    PURPLE = (181, 126, 255)

    def __init__(self, display):
        self.display = display
        self.width = display.width
        self.height = display.height

        self.font_small = self._font(13)
        self.font_body = self._font(15)
        self.font_medium = self._font(18)
        self.font_large = self._font(25)
        self.font_title = self._font(17, bold=True)

    def _font(self, size, bold=False):
        candidates = []

        if bold:
            candidates.extend([
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            ])
        else:
            candidates.extend([
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            ])

        for path in candidates:
            if Path(path).exists():
                try:
                    return ImageFont.truetype(path, size)
                except Exception:
                    pass

        return ImageFont.load_default()

    def new_frame(self):
        image = Image.new(
            "RGB",
            (self.width, self.height),
            self.BG,
        )

        return image, ImageDraw.Draw(image)

    def text(
        self,
        draw,
        xy,
        text,
        font=None,
        fill=None,
        anchor=None,
    ):
        draw.text(
            xy,
            safe_text(text, 100),
            font=font or self.font_body,
            fill=fill or self.TEXT,
            anchor=anchor,
        )

    def panel(
        self,
        draw,
        box,
        fill=None,
        outline=None,
        radius=8,
    ):
        draw.rounded_rectangle(
            box,
            radius=radius,
            fill=fill or self.PANEL,
            outline=outline,
        )

    def header(self, draw, pna, page):
        draw.rectangle(
            (0, 0, self.width, 34),
            fill=self.PANEL,
        )

        self.text(
            draw,
            (12, 17),
            "PNA V2",
            self.font_title,
            self.ACCENT,
            anchor="lm",
        )

        self.text(
            draw,
            (108, 17),
            page,
            self.font_title,
            self.TEXT,
            anchor="lm",
        )

        esp_ok = pna.esp_online()

        status_color = (
            self.GREEN
            if esp_ok
            else self.RED
        )

        draw.ellipse(
            (342, 10, 356, 24),
            fill=status_color,
        )

        self.text(
            draw,
            (363, 17),
            "ESP",
            self.font_small,
            self.MUTED,
            anchor="lm",
        )

        self.text(
            draw,
            (404, 17),
            f"W{len(pna.networks)} B{len(pna.ble_devices)}",
            self.font_small,
            self.TEXT,
            anchor="lm",
        )

    def footer(self, draw, page):
        y = self.height - 24

        draw.rectangle(
            (0, y, self.width, self.height),
            fill=self.PANEL,
        )

        if page in ("WIFI", "BLE"):
            hint = "L page   R select   R click open"
        elif page == "SIGNAL":
            hint = "L page   L click home   R click Wi-Fi"
        else:
            hint = "L page   L click home   hold L 5s power"

        self.text(
            draw,
            (12, y + 12),
            hint,
            self.font_small,
            self.MUTED,
            anchor="lm",
        )

    def signal_color(self, rssi):
        if rssi >= -60:
            return self.GREEN
        if rssi >= -72:
            return self.YELLOW
        return self.RED

    def bar(
        self,
        draw,
        box,
        fraction,
        color=None,
        background=None,
    ):
        x0, y0, x1, y1 = box

        fraction = clamp(float(fraction), 0.0, 1.0)

        draw.rounded_rectangle(
            box,
            radius=4,
            fill=background or self.PANEL_2,
        )

        width = int((x1 - x0) * fraction)

        if width > 0:
            draw.rounded_rectangle(
                (x0, y0, x0 + width, y1),
                radius=4,
                fill=color or self.ACCENT,
            )

    def draw_status(self, draw, pna):
        uptime = format_uptime(
            time.time() - pna.start_time
        )

        left = (12, 48, 230, 188)
        right = (242, 48, 468, 188)

        self.panel(draw, left)
        self.panel(draw, right)

        self.text(
            draw,
            (24, 62),
            "SYSTEM",
            self.font_title,
            self.ACCENT,
        )

        status_lines = [
            (
                "Raspberry Pi",
                "ONLINE",
                self.GREEN,
            ),
            (
                "ESP32",
                "ONLINE"
                if pna.esp_online()
                else "WAITING",
                self.GREEN
                if pna.esp_online()
                else self.YELLOW,
            ),
            (
                "UART",
                "ONLINE"
                if pna.serial is not None
                else "RETRY",
                self.GREEN
                if pna.serial is not None
                else self.YELLOW,
            ),
            (
                "Display",
                "ONLINE"
                if pna.display is not None
                else "HEADLESS",
                self.GREEN
                if pna.display is not None
                else self.YELLOW,
            ),
            (
                "Uptime",
                uptime,
                self.TEXT,
            ),
        ]

        y = 88

        for label, value, color in status_lines:
            self.text(
                draw,
                (24, y),
                label,
                self.font_small,
                self.MUTED,
            )

            self.text(
                draw,
                (128, y),
                value,
                self.font_small,
                color,
            )

            y += 21

        self.text(
            draw,
            (254, 62),
            "RADIO",
            self.font_title,
            self.PURPLE,
        )

        radio_lines = [
            ("Wi-Fi APs", len(pna.networks)),
            ("BLE devices", len(pna.ble_devices)),
            ("RF channels", len(pna.rf_channels)),
            (
                "Wi-Fi scan",
                pna.scan_state.get("wifi", "IDLE"),
            ),
            (
                "RF sweep",
                pna.scan_state.get("rf", "IDLE"),
            ),
            (
                "BLE scan",
                pna.scan_state.get("ble", "IDLE"),
            ),
        ]

        y = 88

        for label, value in radio_lines:
            self.text(
                draw,
                (254, y),
                label,
                self.font_small,
                self.MUTED,
            )

            self.text(
                draw,
                (360, y),
                value,
                self.font_small,
                self.TEXT,
            )

            y += 19

        # Strongest Wi-Fi/BLE summary
        bottom = (12, 198, 468, 286)
        self.panel(draw, bottom)

        strongest_wifi = (
            pna.sorted_networks()[0]
            if pna.networks
            else None
        )

        strongest_ble = (
            pna.sorted_ble()[0]
            if pna.ble_devices
            else None
        )

        self.text(
            draw,
            (24, 212),
            "STRONGEST",
            self.font_title,
            self.ACCENT,
        )

        if strongest_wifi:
            self.text(
                draw,
                (24, 240),
                f"Wi-Fi  {strongest_wifi['ssid'][:22]}",
                self.font_body,
                self.TEXT,
            )
            self.text(
                draw,
                (326, 240),
                f"{strongest_wifi['rssi']} dBm",
                self.font_body,
                self.signal_color(
                    strongest_wifi["rssi"]
                ),
            )
        else:
            self.text(
                draw,
                (24, 240),
                "Wi-Fi  waiting for scan...",
                self.font_body,
                self.MUTED,
            )

        if strongest_ble:
            self.text(
                draw,
                (24, 264),
                f"BLE    {strongest_ble['name'][:22]}",
                self.font_body,
                self.TEXT,
            )
            self.text(
                draw,
                (326, 264),
                f"{strongest_ble['rssi']} dBm",
                self.font_body,
                self.signal_color(
                    strongest_ble["rssi"]
                ),
            )
        else:
            self.text(
                draw,
                (24, 264),
                "BLE    waiting for advertisements...",
                self.font_body,
                self.MUTED,
            )

    def draw_wifi(self, draw, pna):
        networks = pna.sorted_networks()

        self.text(
            draw,
            (12, 48),
            "Nearby access points",
            self.font_medium,
            self.ACCENT,
        )

        self.text(
            draw,
            (352, 50),
            f"{len(networks)} found",
            self.font_small,
            self.MUTED,
        )

        if not networks:
            self.text(
                draw,
                (24, 100),
                "Waiting for ESP32 Wi-Fi scan...",
                self.font_medium,
                self.MUTED,
            )
            return

        pna.wifi_selection = clamp(
            pna.wifi_selection,
            0,
            len(networks) - 1,
        )

        rows = 7

        start = clamp(
            pna.wifi_selection - rows // 2,
            0,
            max(0, len(networks) - rows),
        )

        y = 78

        for index in range(
            start,
            min(start + rows, len(networks)),
        ):
            net = networks[index]
            selected = index == pna.wifi_selection

            fill = (
                self.PANEL_2
                if selected
                else self.PANEL
            )

            outline = (
                self.ACCENT
                if selected
                else None
            )

            self.panel(
                draw,
                (12, y, 468, y + 28),
                fill=fill,
                outline=outline,
                radius=6,
            )

            self.text(
                draw,
                (20, y + 14),
                net["ssid"][:23],
                self.font_small,
                self.TEXT,
                anchor="lm",
            )

            self.text(
                draw,
                (264, y + 14),
                f"CH{net['channel']:>2}",
                self.font_small,
                self.MUTED,
                anchor="lm",
            )

            self.text(
                draw,
                (311, y + 14),
                net["security"][:9],
                self.font_small,
                self.MUTED,
                anchor="lm",
            )

            self.text(
                draw,
                (406, y + 14),
                f"{net['rssi']:>4}dBm",
                self.font_small,
                self.signal_color(net["rssi"]),
                anchor="lm",
            )

            y += 31

    def draw_channels(self, draw, pna):
        counts = Counter()

        for network in pna.networks.values():
            channel = network["channel"]

            if 1 <= channel <= 11:
                counts[channel] += 1

        maximum = max(
            [counts[ch] for ch in range(1, 12)]
            + [1]
        )

        self.text(
            draw,
            (12, 48),
            "Wi-Fi AP channel occupancy",
            self.font_medium,
            self.ACCENT,
        )

        y = 78

        for ch in range(1, 12):
            count = counts[ch]

            self.text(
                draw,
                (18, y),
                f"CH {ch:>2}",
                self.font_small,
                self.TEXT,
            )

            self.bar(
                draw,
                (70, y + 2, 418, y + 14),
                count / maximum,
                self.ACCENT,
            )

            self.text(
                draw,
                (430, y),
                str(count),
                self.font_small,
                self.MUTED,
            )

            y += 18

        if counts:
            busiest = max(
                range(1, 12),
                key=lambda ch: counts[ch],
            )

            self.text(
                draw,
                (18, 280),
                f"Busiest by AP count: CH {busiest} ({counts[busiest]} APs)",
                self.font_small,
                self.YELLOW,
            )

    def draw_rf(self, draw, pna):
        self.text(
            draw,
            (12, 48),
            "Passive 2.4 GHz RF activity",
            self.font_medium,
            self.PURPLE,
        )

        self.text(
            draw,
            (328, 50),
            "activity / RSSI",
            self.font_small,
            self.MUTED,
        )

        fresh = {
            ch: item
            for ch, item in pna.rf_channels.items()
            if time.time() - item["last_seen"] <= RF_TIMEOUT
        }

        if not fresh:
            self.text(
                draw,
                (24, 100),
                "Waiting for passive RF sweep...",
                self.font_medium,
                self.MUTED,
            )
            return

        y = 76

        for ch in range(1, 12):
            item = fresh.get(ch)

            self.text(
                draw,
                (16, y),
                f"{ch:>2}",
                self.font_small,
                self.TEXT,
            )

            if item is None:
                self.bar(
                    draw,
                    (45, y + 2, 322, y + 14),
                    0,
                    self.PURPLE,
                )

                self.text(
                    draw,
                    (336, y),
                    "-",
                    self.font_small,
                    self.MUTED,
                )
            else:
                activity = item["activity"]

                self.bar(
                    draw,
                    (45, y + 2, 322, y + 14),
                    activity / 100,
                    self.PURPLE,
                )

                self.text(
                    draw,
                    (336, y),
                    f"{activity:>3}%",
                    self.font_small,
                    self.TEXT,
                )

                peak_text = (
                    f"{item['peak_rssi']:>4} dBm"
                    if item["peak_rssi"] > -127
                    else "  -- dBm"
                )

                self.text(
                    draw,
                    (388, y),
                    peak_text,
                    self.font_small,
                    self.signal_color(
                        item["peak_rssi"]
                    )
                    if item["peak_rssi"] > -127
                    else self.MUTED,
                )

            y += 18

        busiest = max(
            fresh.values(),
            key=lambda item: (
                item["activity"],
                item["packets"],
            ),
        )

        self.text(
            draw,
            (16, 278),
            (
                f"Peak activity: CH {busiest['channel']}  "
                f"{busiest['activity']}%  "
                f"{busiest['packets']} packets"
            ),
            self.font_small,
            self.YELLOW,
        )

    def draw_signal(self, draw, pna):
        networks = pna.sorted_networks()

        if not networks:
            self.text(
                draw,
                (24, 100),
                "No Wi-Fi AP selected yet.",
                self.font_medium,
                self.MUTED,
            )
            return

        pna.wifi_selection = clamp(
            pna.wifi_selection,
            0,
            len(networks) - 1,
        )

        network = networks[pna.wifi_selection]

        self.text(
            draw,
            (14, 49),
            network["ssid"][:30],
            self.font_large,
            self.TEXT,
        )

        self.text(
            draw,
            (15, 80),
            network["bssid"],
            self.font_small,
            self.MUTED,
        )

        rssi = network["rssi"]

        self.text(
            draw,
            (16, 111),
            f"{rssi} dBm",
            self.font_large,
            self.signal_color(rssi),
        )

        self.text(
            draw,
            (154, 118),
            quality(rssi),
            self.font_medium,
            self.signal_color(rssi),
        )

        self.bar(
            draw,
            (16, 150, 250, 170),
            signal_percent(rssi) / 100,
            self.signal_color(rssi),
        )

        self.panel(
            draw,
            (274, 90, 466, 174),
        )

        info = [
            ("Channel", network["channel"]),
            ("Security", network["security"]),
            (
                "Seen",
                f"{time.time() - network['last_seen']:.1f}s",
            ),
        ]

        y = 104

        for label, value in info:
            self.text(
                draw,
                (286, y),
                label,
                self.font_small,
                self.MUTED,
            )

            self.text(
                draw,
                (376, y),
                str(value),
                self.font_small,
                self.TEXT,
            )

            y += 22

        history = list(network["history"])

        chart = (16, 196, 464, 278)

        draw.rounded_rectangle(
            chart,
            radius=8,
            fill=self.PANEL,
        )

        self.text(
            draw,
            (27, 204),
            "RSSI history",
            self.font_small,
            self.MUTED,
        )

        if len(history) >= 2:
            x0, y0, x1, y1 = chart

            plot_left = x0 + 12
            plot_right = x1 - 12
            plot_top = y0 + 25
            plot_bottom = y1 - 10

            values = [
                item[1]
                for item in history
            ]

            points = []

            for idx, value in enumerate(values):
                x = (
                    plot_left
                    + idx
                    / max(1, len(values) - 1)
                    * (plot_right - plot_left)
                )

                # Fixed RF graph range -100 .. -35 dBm.
                normalized = clamp(
                    (value + 100) / 65,
                    0,
                    1,
                )

                y = (
                    plot_bottom
                    - normalized
                    * (plot_bottom - plot_top)
                )

                points.append((x, y))

            draw.line(
                points,
                fill=self.ACCENT,
                width=3,
            )

            avg = sum(values) / len(values)

            self.text(
                draw,
                (342, 204),
                (
                    f"min {min(values)}  "
                    f"avg {avg:.1f}  "
                    f"max {max(values)}"
                ),
                self.font_small,
                self.MUTED,
            )

    def draw_ble(self, draw, pna):
        devices = pna.sorted_ble()

        self.text(
            draw,
            (12, 48),
            "Passive BLE advertisements",
            self.font_medium,
            self.GREEN,
        )

        self.text(
            draw,
            (352, 50),
            f"{len(devices)} found",
            self.font_small,
            self.MUTED,
        )

        if not devices:
            self.text(
                draw,
                (24, 100),
                "Waiting for BLE advertisements...",
                self.font_medium,
                self.MUTED,
            )
            return

        pna.ble_selection = clamp(
            pna.ble_selection,
            0,
            len(devices) - 1,
        )

        rows = 7

        start = clamp(
            pna.ble_selection - rows // 2,
            0,
            max(0, len(devices) - rows),
        )

        y = 78

        for index in range(
            start,
            min(start + rows, len(devices)),
        ):
            dev = devices[index]
            selected = index == pna.ble_selection

            self.panel(
                draw,
                (12, y, 468, y + 28),
                fill=(
                    self.PANEL_2
                    if selected
                    else self.PANEL
                ),
                outline=(
                    self.GREEN
                    if selected
                    else None
                ),
                radius=6,
            )

            self.text(
                draw,
                (20, y + 14),
                dev["name"][:25],
                self.font_small,
                self.TEXT,
                anchor="lm",
            )

            self.text(
                draw,
                (286, y + 14),
                dev["address"][-8:],
                self.font_small,
                self.MUTED,
                anchor="lm",
            )

            self.text(
                draw,
                (410, y + 14),
                f"{dev['rssi']:>4}dBm",
                self.font_small,
                self.signal_color(dev["rssi"]),
                anchor="lm",
            )

            y += 31

    def draw_ethernet(self, draw, pna):
        pna.update_ethernet()
        e = pna.eth_cache
        self.text(draw, (12, 46), "Ethernet diagnostics", self.font_medium, self.ACCENT)
        if not e:
            self.text(draw, (20, 90), "Collecting Ethernet data...", self.font_body)
            return

        # Upper status strip leaves space for a real encoder-driven target menu.
        self.panel(draw, (12, 72, 468, 151))
        rows = [
            ("Port", e.get("iface", "-"), "Link", e.get("carrier", "-")),
            ("IPv4", e.get("ip", "-"), "Speed", e.get("speed", "-")),
            ("GW", e.get("gateway", "-"), "DNS", e.get("dns", "-")),
        ]
        for i, (l1, v1, l2, v2) in enumerate(rows):
            y = 81 + i * 23
            self.text(draw, (20, y), l1, self.font_small, self.MUTED)
            self.text(draw, (70, y), str(v1)[:22], self.font_small, self.TEXT)
            self.text(draw, (286, y), l2, self.font_small, self.MUTED)
            self.text(draw, (340, y), str(v2)[:16], self.font_small, self.TEXT)

        if pna.eth_detail_target:
            d = pna.eth_detail
            self.text(draw, (15, 161), "DEVICE INSPECTOR  (click R to return)",
                      self.font_small, self.ACCENT)
            fields = [("IP", d.get("ip", "-")),
                      ("Hostname", d.get("hostname", "-")),
                      ("MAC", d.get("mac", "-")),
                      ("Ping", d.get("ping", "-")),
                      ("TCP ports", d.get("ports", "-"))]
            for i, (key, value) in enumerate(fields):
                y = 183 + i * 22
                self.text(draw, (18, y), key, self.font_small, self.MUTED)
                self.text(draw, (125, y), str(value)[:49], self.font_small, self.TEXT)
            self.text(draw, (15, 299), "STATE: " + pna.eth_detail_state,
                      self.font_small, self.ACCENT)
            return

        self.text(draw, (15, 159), "TARGETS  (R: scroll/click ping/hold inspect)",
                  self.font_small, self.ACCENT)
        self.text(draw, (15, 296), f"SCAN: {pna.eth_scan_state[:43]}", self.font_small, self.ACCENT)
        choices = [("SCAN LAN", "SCAN")] + pna.eth_targets
        if not pna.eth_targets:
            self.text(draw, (15, 281), "Click SCAN LAN to find devices", self.font_small,
                      self.MUTED)
        if choices:
            pna.eth_selection = clamp(pna.eth_selection, 0, len(choices) - 1)
            first = clamp(pna.eth_selection - 1, 0, max(0, len(choices) - 3))
            for row, idx in enumerate(range(first, min(first + 3, len(choices)))):
                label, ip = choices[idx]
                y = 181 + row * 30
                selected = idx == pna.eth_selection
                self.panel(draw, (12, y, 468, y + 27),
                           fill=self.PANEL_2 if selected else self.PANEL,
                           outline=self.ACCENT if selected else None, radius=5)
                self.text(draw, (20, y + 13), ">" if selected else " ",
                          self.font_small, self.ACCENT, anchor="lm")
                self.text(draw, (39, y + 13), label[:17], self.font_small,
                          self.TEXT, anchor="lm")
                self.text(draw, (225, y + 13), ip, self.font_small,
                          self.MUTED, anchor="lm")
                if ip == pna.eth_ping_target and ip != "SCAN":
                    result = pna.eth_ping_result
                    color = self.RED if result == "FAIL" else self.GREEN
                    self.text(draw, (367, y + 13), result[:12],
                              self.font_small, color, anchor="lm")

        if not pna.eth_ping_target:
            self.text(draw, (15, 281), "Click a target to run ping", self.font_small,
                      self.MUTED)

    def draw_monitor(self, draw, pna):
        snap = pna.wifi_monitor.snapshot()

        proc = pna.wifi_monitor.proc
        running = (
            proc is not None
            and proc.poll() is None
        )

        avg = snap.get("avg_rssi")
        peak = snap.get("peak_rssi")

        rows = [
            ("Radio", "wlan1 / mt7921u"),
            ("State", "RUNNING" if running else "STOPPED"),
            ("Frames", snap.get("frames", 0)),
            ("APs Seen", snap.get("aps", 0)),
            ("Beacons", snap.get("beacons", 0)),
            ("Management", snap.get("management", 0)),
            ("Data", snap.get("data", 0)),
            ("Control", snap.get("control", 0)),
            ("Avg RSSI", f"{avg} dBm" if avg is not None else "--"),
            ("Peak RSSI", f"{peak} dBm" if peak is not None else "--"),
        ]

        y = 50

        for label, value in rows:
            self.text(
                draw,
                (18, y),
                f"{label:<12} {value}",
                self.font_small,
                self.TEXT,
            )
            y += 22

    def draw_system(self, draw, pna):
        stats = pna.system_stats()

        self.text(
            draw,
            (12, 48),
            "PNA system",
            self.font_medium,
            self.ACCENT,
        )

        self.panel(
            draw,
            (12, 80, 235, 278),
        )

        self.panel(
            draw,
            (245, 80, 468, 278),
        )

        left = [
            ("Core", f"v{CORE_VERSION}"),
            ("ESP FW", pna.esp_version or "unknown"),
            ("UART", SERIAL_PORT),
            ("Messages", str(pna.messages)),
            ("CPU temp", stats["cpu_temp"]),
            ("Throttle", stats["throttle"]),
            ("Memory", stats["memory"]),
        ]

        right = [
            (
                "Display",
                "ONLINE"
                if pna.display is not None
                else "HEADLESS",
            ),
            (
                "Wi-Fi log",
                pna.wifi_log.status,
            ),
            (
                "BLE log",
                pna.ble_log.status,
            ),
            (
                "RF log",
                pna.rf_log.status,
            ),
            ("Disk free", stats["disk_free"]),
            ("Hostname", stats["hostname"]),
            ("Shutdown", "hold LEFT 5s"),
        ]

        y = 96

        for label, value in left:
            self.text(
                draw,
                (24, y),
                label,
                self.font_small,
                self.MUTED,
            )

            self.text(
                draw,
                (92, y),
                value,
                self.font_small,
                self.TEXT,
            )

            y += 25

        y = 96

        for label, value in right:
            self.text(
                draw,
                (257, y),
                label,
                self.font_small,
                self.MUTED,
            )

            self.text(
                draw,
                (330, y),
                value,
                self.font_small,
                self.TEXT,
            )

            y += 25

    def render(self, pna):
        image, draw = self.new_frame()

        page = PAGES[pna.page]

        self.header(draw, pna, page)

        if page == "STATUS":
            self.draw_status(draw, pna)

        elif page == "WIFI":
            self.draw_wifi(draw, pna)

        elif page == "CHANNELS":
            self.draw_channels(draw, pna)

        elif page == "RF":
            self.draw_rf(draw, pna)

        elif page == "SIGNAL":
            self.draw_signal(draw, pna)

        elif page == "BLE":
            self.draw_ble(draw, pna)

        elif page == "ETHERNET":
            self.draw_ethernet(draw, pna)

        elif page == "MONITOR":
            self.draw_monitor(draw, pna)

        elif page == "SYSTEM":
            self.draw_system(draw, pna)

        self.footer(draw, page)

        self.display.show(image)


# ============================================================
# PNA CORE
# ============================================================

class PNA:
    def __init__(
        self,
        enable_display=True,
        enable_logging=True,
        force_terminal=False,
    ):
        self.start_time = time.time()

        self.last_esp = 0
        self.esp_version = ""
        self.messages = 0

        self.networks = {}
        self.ble_devices = {}
        self.rf_channels = {}

        self.scan_state = {
            "wifi": "IDLE",
            "rf": "IDLE",
            "ble": "IDLE",
        }

        self.events = deque()

        self.page = 0

        self.wifi_selection = 0
        self.ble_selection = 0

        self.serial = None
        self.last_uart_attempt = 0
        self.uart_error = ""

        self.shutdown_requested = False

        self.eth_cache = {}
        self.last_eth_update = 0
        self.eth_targets = []
        self.eth_selection = 0
        self.eth_ping_target = ""
        self.eth_ping_result = "N/A"
        self.eth_scan_state = "READY"
        self.eth_scan_count = 0
        self.eth_scan_results = []
        self.eth_scan_lock = threading.Lock()
        self.eth_detail_target = ""
        self.eth_detail = {}
        self.eth_detail_state = "READY"
        self.right_hold_handled = False

        self.wifi_monitor = WiFiMonitor("wlan1")
        self.monitor_error = ""

        self._system_cache = {}
        self._last_system_update = 0

        self.force_terminal = force_terminal
        self.terminal_enabled = (
            force_terminal
            or sys.stdout.isatty()
        )

        self.display = None
        self.ui = None
        self.display_error = ""

        # Resilient logs.
        self.wifi_log = SafeCSVLogger(
            WIFI_LOG_FILE,
            [
                "timestamp",
                "ssid",
                "bssid",
                "rssi",
                "channel",
                "security",
            ],
            enabled=enable_logging,
        )

        self.ble_log = SafeCSVLogger(
            BLE_LOG_FILE,
            [
                "timestamp",
                "name",
                "address",
                "rssi",
            ],
            enabled=enable_logging,
        )

        self.rf_log = SafeCSVLogger(
            RF_LOG_FILE,
            [
                "timestamp",
                "channel",
                "packets",
                "bytes",
                "avg_rssi",
                "peak_rssi",
                "activity",
                "mgmt",
                "data",
                "ctrl",
            ],
            enabled=enable_logging,
        )

        self.setup_encoders()

        if enable_display:
            self.setup_display()

    # ========================================================
    # HARDWARE SETUP
    # ========================================================

    def setup_encoders(self):
        self.left_encoder = RotaryEncoder(
            LEFT_CLK,
            LEFT_DT,
            max_steps=0,
            wrap=False,
        )

        self.left_button = Button(
            LEFT_SW,
            pull_up=True,
            bounce_time=0.05,
            hold_time=5,
            hold_repeat=False,
        )

        self.right_encoder = RotaryEncoder(
            RIGHT_CLK,
            RIGHT_DT,
            max_steps=0,
            wrap=False,
        )

        self.right_button = Button(
            RIGHT_SW,
            pull_up=True,
            bounce_time=0.05,
            hold_time=0.85,
            hold_repeat=False,
        )

        self.left_encoder.when_rotated_clockwise = (
            lambda:
            self.events.append("LEFT_CW")
        )

        self.left_encoder.when_rotated_counter_clockwise = (
            lambda:
            self.events.append("LEFT_CCW")
        )

        self.left_button.when_pressed = (
            lambda:
            self.events.append("LEFT_PRESS")
        )

        self.left_button.when_held = (
            lambda:
            self.events.append("LEFT_HOLD")
        )

        self.right_encoder.when_rotated_clockwise = (
            lambda:
            self.events.append("RIGHT_CW")
        )

        self.right_encoder.when_rotated_counter_clockwise = (
            lambda:
            self.events.append("RIGHT_CCW")
        )

        # A short click is emitted on release so a hold cannot also ping.
        self.right_button.when_pressed = (
            lambda: setattr(self, "right_hold_handled", False)
        )
        self.right_button.when_held = (
            lambda: (setattr(self, "right_hold_handled", True),
                     self.events.append("RIGHT_HOLD"))
        )
        self.right_button.when_released = (
            lambda: self.events.append("RIGHT_PRESS")
            if not self.right_hold_handled else None
        )

    def setup_display(self):
        try:
            self.display = ST7796SDisplay()
            self.ui = TFTUI(self.display)

        except Exception as exc:
            self.display_error = (
                f"{type(exc).__name__}: {exc}"
            )

            self.display = None
            self.ui = None

            if self.terminal_enabled:
                print(
                    "[PNA] Display disabled:",
                    self.display_error,
                )

    # ========================================================
    # UART
    # ========================================================

    def connect_uart(self):
        now = time.time()

        if (
            self.serial is not None
            or now - self.last_uart_attempt < 2
        ):
            return

        self.last_uart_attempt = now

        try:
            self.serial = serial.Serial(
                SERIAL_PORT,
                SERIAL_BAUD,
                timeout=0.05,
            )

            self.serial.reset_input_buffer()
            self.uart_error = ""

        except Exception as exc:
            self.serial = None
            self.uart_error = (
                f"{type(exc).__name__}: {exc}"
            )

    def close_uart(self):
        if self.serial:
            try:
                self.serial.close()
            except Exception:
                pass

        self.serial = None

    def poll_uart(self):
        if self.serial is None:
            self.connect_uart()
            return

        try:
            # Drain a limited burst each loop so UI/GPIO stay responsive.
            for _ in range(64):
                if self.serial.in_waiting <= 0:
                    break

                line = (
                    self.serial.readline()
                    .decode(
                        "utf-8",
                        errors="replace",
                    )
                    .strip()
                )

                if line:
                    self.process_message(line)

        except (
            serial.SerialException,
            OSError,
        ) as exc:
            self.uart_error = (
                f"{type(exc).__name__}: {exc}"
            )
            self.close_uart()

    # ========================================================
    # DATA INGEST
    # ========================================================

    def add_network(
        self,
        ssid,
        bssid,
        rssi,
        channel,
        security,
    ):
        now = time.time()
        key = bssid.lower()

        if key not in self.networks:
            self.networks[key] = {
                "ssid": ssid,
                "bssid": bssid,
                "rssi": rssi,
                "channel": channel,
                "security": security,
                "last_seen": now,
                "history": deque(maxlen=60),
            }

        network = self.networks[key]

        network["ssid"] = ssid
        network["rssi"] = rssi
        network["channel"] = channel
        network["security"] = security
        network["last_seen"] = now
        network["history"].append((now, rssi))

        self.wifi_log.append([
            datetime.now().isoformat(
                timespec="seconds"
            ),
            ssid,
            bssid,
            rssi,
            channel,
            security,
        ])

    def add_ble(
        self,
        name,
        address,
        rssi,
    ):
        now = time.time()
        key = address.lower()

        if key not in self.ble_devices:
            self.ble_devices[key] = {
                "name": name,
                "address": address,
                "rssi": rssi,
                "last_seen": now,
                "history": deque(maxlen=60),
            }

        device = self.ble_devices[key]

        if name != "<unknown>":
            device["name"] = name

        device["rssi"] = rssi
        device["last_seen"] = now
        device["history"].append((now, rssi))

        self.ble_log.append([
            datetime.now().isoformat(
                timespec="seconds"
            ),
            device["name"],
            address,
            rssi,
        ])

    def add_rf(
        self,
        channel,
        packets,
        byte_count,
        avg_rssi,
        peak_rssi,
        activity,
        mgmt,
        data,
        ctrl,
    ):
        now = time.time()

        item = {
            "channel": channel,
            "packets": packets,
            "bytes": byte_count,
            "avg_rssi": avg_rssi,
            "peak_rssi": peak_rssi,
            "activity": clamp(activity, 0, 100),
            "mgmt": mgmt,
            "data": data,
            "ctrl": ctrl,
            "last_seen": now,
        }

        self.rf_channels[channel] = item

        self.rf_log.append([
            datetime.now().isoformat(
                timespec="seconds"
            ),
            channel,
            packets,
            byte_count,
            avg_rssi,
            peak_rssi,
            activity,
            mgmt,
            data,
            ctrl,
        ])

    def process_message(self, message):
        self.messages += 1

        if message == "ESP32_ALIVE":
            self.last_esp = time.time()
            return

        if message.startswith("BOOT|"):
            parts = message.split("|")

            if len(parts) >= 3:
                self.esp_version = parts[2]
                self.last_esp = time.time()

            return

        if message.startswith("SCAN|"):
            parts = message.split("|")

            if len(parts) >= 2:
                stage = parts[1]

                if stage == "WIFI_BEGIN":
                    self.scan_state["wifi"] = "SCANNING"

                elif stage == "WIFI_END":
                    self.scan_state["wifi"] = "DONE"

                elif stage == "RF_BEGIN":
                    self.scan_state["rf"] = "SCANNING"

                elif stage == "RF_END":
                    self.scan_state["rf"] = "DONE"

                elif stage == "BLE_BEGIN":
                    self.scan_state["ble"] = "SCANNING"

                elif stage == "BLE_END":
                    self.scan_state["ble"] = "DONE"

            return

        # WIFI|SSID|BSSID|RSSI|CHANNEL|SECURITY
        if message.startswith("WIFI|"):
            parts = message.split("|")

            if len(parts) >= 6:
                try:
                    self.add_network(
                        parts[1],
                        parts[2],
                        int(parts[3]),
                        int(parts[4]),
                        parts[5],
                    )
                except ValueError:
                    pass

            return

        # BLE|NAME|ADDRESS|RSSI
        if message.startswith("BLE|"):
            parts = message.split("|")

            if len(parts) >= 4:
                try:
                    self.add_ble(
                        parts[1],
                        parts[2],
                        int(parts[3]),
                    )
                except ValueError:
                    pass

            return

        # RF|CHANNEL|PACKETS|BYTES|AVG|PEAK|ACTIVITY|MGMT|DATA|CTRL
        if message.startswith("RF|"):
            parts = message.split("|")

            if len(parts) >= 10:
                try:
                    self.add_rf(
                        int(parts[1]),
                        int(parts[2]),
                        int(parts[3]),
                        int(parts[4]),
                        int(parts[5]),
                        int(parts[6]),
                        int(parts[7]),
                        int(parts[8]),
                        int(parts[9]),
                    )
                except ValueError:
                    pass

            return

        # ERR records are retained as UART error text but do not crash.
        if message.startswith("ERR|"):
            self.uart_error = message
            return

    # ========================================================
    # DATA MAINTENANCE
    # ========================================================

    def esp_online(self):
        return (
            self.last_esp > 0
            and time.time() - self.last_esp
            <= ESP_TIMEOUT
        )

    def remove_old_data(self):
        now = time.time()

        for key in [
            key
            for key, item in self.networks.items()
            if now - item["last_seen"] > WIFI_TIMEOUT
        ]:
            del self.networks[key]

        for key in [
            key
            for key, item in self.ble_devices.items()
            if now - item["last_seen"] > BLE_TIMEOUT
        ]:
            del self.ble_devices[key]

        for key in [
            key
            for key, item in self.rf_channels.items()
            if now - item["last_seen"] > RF_TIMEOUT
        ]:
            del self.rf_channels[key]

        self._clamp_selections()

    def sorted_networks(self):
        return sorted(
            self.networks.values(),
            key=lambda item: item["rssi"],
            reverse=True,
        )

    def sorted_ble(self):
        return sorted(
            self.ble_devices.values(),
            key=lambda item: item["rssi"],
            reverse=True,
        )

    def _clamp_selections(self):
        networks = self.sorted_networks()
        devices = self.sorted_ble()

        if networks:
            self.wifi_selection = clamp(
                self.wifi_selection,
                0,
                len(networks) - 1,
            )
        else:
            self.wifi_selection = 0

        if devices:
            self.ble_selection = clamp(
                self.ble_selection,
                0,
                len(devices) - 1,
            )
        else:
            self.ble_selection = 0

    # ========================================================
    # ENCODER EVENTS
    # ========================================================

    def handle_events(self):
        while self.events:
            event = self.events.popleft()
            page = PAGES[self.page]

            if event == "LEFT_CW":
                self.page = (
                    self.page + 1
                ) % len(PAGES)

            elif event == "LEFT_CCW":
                self.page = (
                    self.page - 1
                ) % len(PAGES)

            elif event == "LEFT_PRESS":
                self.page = PAGES.index("STATUS")

            elif event == "LEFT_HOLD":
                self.shutdown_requested = True

            elif event == "RIGHT_CW":
                if page == "WIFI":
                    networks = self.sorted_networks()

                    if networks:
                        self.wifi_selection = min(
                            self.wifi_selection + 1,
                            len(networks) - 1,
                        )

                elif page == "BLE":
                    devices = self.sorted_ble()

                    if devices:
                        self.ble_selection = min(
                            self.ble_selection + 1,
                            len(devices) - 1,
                        )
                elif page == "ETHERNET" and not self.eth_detail_target:
                    self.eth_selection = min(self.eth_selection + 1,
                                             len(self.eth_targets))

            elif event == "RIGHT_CCW":
                if page == "WIFI":
                    self.wifi_selection = max(
                        0,
                        self.wifi_selection - 1,
                    )

                elif page == "BLE":
                    self.ble_selection = max(
                        0,
                        self.ble_selection - 1,
                    )
                elif page == "ETHERNET" and not self.eth_detail_target:
                    self.eth_selection = max(0, self.eth_selection - 1)

            elif event == "RIGHT_PRESS":
                if page == "WIFI" and self.networks:
                    self.page = PAGES.index("SIGNAL")

                elif page == "SIGNAL":
                    self.page = PAGES.index("WIFI")
                elif page == "ETHERNET":
                    if self.eth_detail_target:
                        self.eth_detail_target = ""
                    elif self.eth_selection == 0:
                        self.start_ethernet_scan()
                    else:
                        self.ping_selected_ethernet_target()

            elif event == "RIGHT_HOLD":
                if page == "ETHERNET" and not self.eth_detail_target:
                    self.inspect_selected_ethernet_target()

    # ========================================================
    # ETHERNET
    # ========================================================

    def ping_selected_ethernet_target(self):
        """Run one bounded ICMP test only on a user-selected target."""
        if not self.eth_targets or self.eth_selection < 1:
            return
        label, target = self.eth_targets[self.eth_selection - 1]
        iface = self.eth_cache.get('iface', '')
        self.eth_ping_target = target
        self.eth_ping_result = 'N/A'
        if self.eth_cache.get('carrier') != 'UP' or not iface:
            return
        try:
            result = subprocess.run(
                ['ping', '-n', '-I', iface, '-c', '1', '-W', '1', target],
                capture_output=True, text=True, timeout=2, check=False)
            self.eth_ping_result = 'FAIL'
            if result.returncode == 0:
                for token in result.stdout.split():
                    if token.startswith('time='):
                        self.eth_ping_result = token.split('=', 1)[1] + ' ms'
                        break
        except (OSError, subprocess.TimeoutExpired):
            self.eth_ping_result = 'FAIL'

    def inspect_selected_ethernet_target(self):
        """Inspect only an explicitly selected IPv4 target on wired Ethernet."""
        if not 1 <= self.eth_selection <= len(self.eth_targets):
            return
        target = self.eth_targets[self.eth_selection - 1][1]
        try:
            addr = ipaddress.ip_address(target)
            iface = self.eth_cache.get("iface", "")
            local = ipaddress.ip_interface(self.eth_cache.get("ip", ""))
            if (addr.version != 4 or addr not in local.network or
                    self.eth_cache.get("carrier") != "UP" or not iface):
                self.eth_detail_state = "TARGET NOT ON WIRED LAN"
                return
        except ValueError:
            self.eth_detail_state = "INVALID TARGET"
            return
        self.eth_detail_target = target
        self.eth_detail = {"ip": target, "hostname": "...", "mac": "...",
                           "ping": "...", "ports": "..."}
        self.eth_detail_state = "INSPECTING"
        threading.Thread(target=self._inspect_ethernet_worker,
                         args=(target, iface), daemon=True).start()

    def _inspect_ethernet_worker(self, target, iface):
        """Bounded inspection; no arbitrary ports or nonlocal targets."""
        details = {"ip": target, "hostname": "N/A", "mac": "N/A",
                   "ping": "FAIL", "ports": "NONE FOUND"}
        try:
            ping = subprocess.run(
                ["ping", "-n", "-I", iface, "-c", "1", "-W", "1", target],
                capture_output=True, text=True, timeout=2, check=False)
            if ping.returncode == 0:
                for token in ping.stdout.split():
                    if token.startswith("time="):
                        details["ping"] = token.split("=", 1)[1] + " ms"
                        break
            neigh = subprocess.run(
                ["ip", "neigh", "show", "to", target, "dev", iface],
                capture_output=True, text=True, timeout=2, check=False)
            words = neigh.stdout.split()
            if "lladdr" in words and words.index("lladdr") + 1 < len(words):
                details["mac"] = words[words.index("lladdr") + 1]
            hostname = subprocess.run(
                ["getent", "hosts", target], capture_output=True,
                text=True, timeout=2, check=False)
            if hostname.returncode == 0:
                first = hostname.stdout.splitlines()[0].split() if hostname.stdout.strip() else []
                if len(first) > 1:
                    details["hostname"] = first[1][:42]
            ports = {22: "SSH", 80: "HTTP", 443: "HTTPS", 445: "SMB",
                     3389: "RDP", 8080: "WEB"}
            open_ports = []
            for port, label in ports.items():
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                        sock.settimeout(0.35)
                        # Bind to the wired interface where permitted.
                        try:
                            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                                            iface.encode() + b"\0")
                        except (OSError, AttributeError):
                            pass
                        if sock.connect_ex((target, port)) == 0:
                            open_ports.append(f"{port}/{label}")
                except OSError:
                    pass
            details["ports"] = ", ".join(open_ports) if open_ports else "NONE FOUND"
            state = "DONE"
        except (OSError, subprocess.TimeoutExpired, IndexError):
            state = "PARTIAL / TIMEOUT"
        if self.eth_detail_target == target:
            self.eth_detail = details
            self.eth_detail_state = state

    def start_ethernet_scan(self):
        """Explicit, bounded IPv4 LAN discovery; never scan Wi-Fi or public ranges."""
        if self.eth_scan_state.startswith("SCANNING"):
            return
        iface = self.eth_cache.get("iface", "")
        address = self.eth_cache.get("ip", "N/A")
        if self.eth_cache.get("carrier") != "UP":
            self.eth_scan_state = "LINK DOWN"
            return
        try:
            network = ipaddress.ip_interface(address).network
            if (not network.is_private or network.prefixlen < 24
                    or network.num_addresses > 256):
                self.eth_scan_state = "SCAN LIMITED TO PRIVATE /24"
                return
        except ValueError:
            self.eth_scan_state = "NO VALID IPv4"
            return
        self.eth_scan_state = "SCANNING 0/254"
        self.eth_scan_count = 0
        threading.Thread(target=self._scan_ethernet_worker,
                         args=(iface, network), daemon=True).start()

    def _scan_ethernet_worker(self, iface, network):
        """Ping sweep in background so display and GPIO event loop stay responsive."""
        hosts = [str(ip) for ip in network.hosts()]
        found = []
        def probe(ip):
            try:
                r = subprocess.run(
                    ["ping", "-n", "-I", iface, "-c", "1", "-W", "1", ip],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=2, check=False)
                return ip if r.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired):
                return None
        try:
            with ThreadPoolExecutor(max_workers=12) as pool:
                futures = [pool.submit(probe, ip) for ip in hosts]
                for future in as_completed(futures):
                    ip = future.result()
                    if ip:
                        found.append(ip)
                    self.eth_scan_count += 1
                    self.eth_scan_state = f"SCANNING {self.eth_scan_count}/{len(hosts)}"
            found.sort(key=lambda ip: int(ipaddress.IPv4Address(ip)))
            with self.eth_scan_lock:
                self.eth_scan_results = found
                self.last_eth_update = 0
                self.eth_scan_state = f"DONE: {len(found)} HOST(S) REPLIED"
        except Exception:
            self.eth_scan_state = "SCAN ERROR"

    def refresh_ethernet_targets(self, iface, ip_addr, gateway, configured):
        """List explicit and previously observed local IPv4 peers; no network sweep."""
        choices = []
        def add(label, ip):
            try:
                socket.inet_pton(socket.AF_INET, ip)
            except (OSError, TypeError):
                return
            if ip not in [entry[1] for entry in choices]:
                choices.append((label, ip))

        add('Configured', configured)
        add('Gateway', gateway)
        try:
            result = subprocess.run(['ip', '-4', 'neigh', 'show', 'dev', iface],
                                    capture_output=True, text=True, timeout=1)
            for line in result.stdout.splitlines():
                parts = line.split()
                if parts and 'FAILED' not in parts and 'INCOMPLETE' not in parts:
                    add('LAN device', parts[0])
        except (OSError, subprocess.TimeoutExpired):
            pass
        # A direct NetworkManager-shared PearBook connection uses 10.42.0.1.
        if ip_addr.startswith('10.42.0.'):
            add('PearBook', '10.42.0.1')
        previous = (self.eth_targets[self.eth_selection - 1][1]
                    if self.eth_targets and 1 <= self.eth_selection <= len(self.eth_targets)
                    else None)
        for ip in self.eth_scan_results:
            add("Discovered", ip)
        self.eth_targets = choices
        if previous:
            self.eth_selection = next((i + 1 for i, item in enumerate(choices)
                                       if item[1] == previous), 0)
        else:
            self.eth_selection = min(self.eth_selection, len(choices))

    def update_ethernet(self):
        """Read wired link independently of the Wi-Fi/default route."""
        now = time.monotonic()
        if now - self.last_eth_update < 8 and self.eth_cache:
            return
        self.last_eth_update = now

        def command(*args, timeout=1.5):
            try:
                return subprocess.run(args, capture_output=True, text=True,
                                      timeout=timeout, check=False).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                return ""

        interfaces = sorted(Path('/sys/class/net').glob('*'))
        wired = [x.name for x in interfaces if x.name.startswith(('eth', 'enx', 'enp'))]
        iface = ('eth0' if 'eth0' in wired else wired[0]) if wired else None
        if not iface:
            self.eth_cache = {'iface': 'N/A', 'carrier': 'NO ADAPTER'}
            return

        base = f'/sys/class/net/{iface}'
        carrier = 'UP' if read_text_file(f'{base}/carrier') == '1' else 'DOWN'
        ip_text = command('ip', '-4', '-o', 'addr', 'show', 'dev', iface)
        ip_addr = ip_text.split()[3] if len(ip_text.split()) >= 4 else 'N/A'
        route_text = command('ip', '-4', 'route', 'show', 'default', 'dev', iface)
        gateway = 'N/A'
        for line in route_text.splitlines():
            parts = line.split()
            if 'via' in parts and parts.index('via') + 1 < len(parts):
                gateway = parts[parts.index('via') + 1]
                break

        def ping(target):
            if not target or target == 'N/A' or carrier != 'UP':
                return 'N/A'
            output = command('ping', '-n', '-I', iface, '-c', '1', '-W', '1', target, timeout=2)
            for token in output.split():
                if token.startswith('time='):
                    return token.split('=', 1)[1] + ' ms'
            return 'FAIL'

        speed_raw = read_text_file(f'{base}/speed')
        speed = f'{speed_raw} Mb/s' if speed_raw.isdigit() and int(speed_raw) > 0 else 'N/A'
        target = os.environ.get('PNA_ETH_TARGET', '').strip()
        # Accept only an IPv4 address, not a hostname or shell input.
        try:
            socket.inet_aton(target)
            if len(target.split('.')) != 4:
                target = ''
        except OSError:
            target = ''
        self.refresh_ethernet_targets(iface, ip_addr, gateway, target)
        dns = 'OK' if command('getent', 'ahostsv4', 'example.com', timeout=2) else 'FAIL'
        self.eth_cache = {
            'iface': iface,
            'carrier': carrier,
            'ip': ip_addr,
            'mac': read_text_file(f'{base}/address', 'N/A'),
            'speed': speed,
            'duplex': read_text_file(f'{base}/duplex', 'N/A'),
            'gateway': gateway,
            'gateway_ping': ping(gateway),
            'internet_ping': ping('1.1.1.1') if gateway != 'N/A' else 'N/A',
            'dns': dns,
            'target': target or 'NOT SET',
            'target_ping': self.eth_ping_result,
            'rx_errors': read_text_file(f'{base}/statistics/rx_errors', 'N/A'),
            'tx_errors': read_text_file(f'{base}/statistics/tx_errors', 'N/A'),
            'rx_dropped': read_text_file(f'{base}/statistics/rx_dropped', 'N/A'),
            'tx_dropped': read_text_file(f'{base}/statistics/tx_dropped', 'N/A'),
        }

    # ========================================================
    # SYSTEM STATS
    # ========================================================

    def system_stats(self):
        now = time.time()

        if (
            self._system_cache
            and now - self._last_system_update < 4
        ):
            return self._system_cache

        self._last_system_update = now

        temp_raw = read_text_file(
            "/sys/class/thermal/thermal_zone0/temp",
            "",
        )

        try:
            cpu_temp = (
                f"{int(temp_raw) / 1000:.1f} C"
            )
        except Exception:
            cpu_temp = "N/A"

        throttle = run_cmd(
            "vcgencmd get_throttled 2>/dev/null",
            timeout=1,
        )

        if throttle.startswith("throttled="):
            throttle = throttle.split("=", 1)[1]
        elif not throttle:
            throttle = "N/A"

        try:
            usage = shutil.disk_usage("/")
            disk_free = (
                f"{usage.free / (1024**3):.1f} GB"
            )
        except Exception:
            disk_free = "N/A"

        memory = "N/A"

        try:
            meminfo = {}

            for line in Path("/proc/meminfo").read_text().splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    meminfo[key] = value.strip()

            total_kb = int(
                meminfo["MemTotal"].split()[0]
            )

            available_kb = int(
                meminfo["MemAvailable"].split()[0]
            )

            used_mb = (
                total_kb - available_kb
            ) / 1024

            total_mb = total_kb / 1024

            memory = (
                f"{used_mb:.0f}/{total_mb:.0f} MB"
            )

        except Exception:
            pass

        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = "pna"

        self._system_cache = {
            "cpu_temp": cpu_temp,
            "throttle": throttle,
            "disk_free": disk_free,
            "memory": memory,
            "hostname": hostname,
        }

        return self._system_cache

    # ========================================================
    # TERMINAL / SSH UI
    # ========================================================

    def render_terminal(self):
        page = PAGES[self.page]

        print("\033[2J\033[H", end="")

        print(
            f"PNA V2 | {page} | "
            f"ESP:{'ON' if self.esp_online() else 'OFF'} | "
            f"W:{len(self.networks)} | "
            f"B:{len(self.ble_devices)} | "
            f"RF:{len(self.rf_channels)}"
        )

        print("=" * 72)
        print()

        if page == "STATUS":
            print("Raspberry Pi : ONLINE")
            print(
                "ESP32        :",
                "ONLINE"
                if self.esp_online()
                else "WAITING",
            )
            print(
                "UART         :",
                "ONLINE"
                if self.serial
                else "RETRY",
            )
            print(
                "Display      :",
                "ONLINE"
                if self.display
                else "HEADLESS",
            )
            print(
                "Uptime       :",
                format_uptime(
                    time.time() - self.start_time
                ),
            )
            print(
                "Messages     :",
                self.messages,
            )
            print(
                "Wi-Fi APs    :",
                len(self.networks),
            )
            print(
                "BLE Devices  :",
                len(self.ble_devices),
            )
            print(
                "RF Channels  :",
                len(self.rf_channels),
            )

        elif page == "WIFI":
            networks = self.sorted_networks()

            if not networks:
                print(
                    "Waiting for Wi-Fi scan..."
                )
            else:
                self._clamp_selections()

                print(
                    "     RSSI  CH  SECURITY   SSID"
                )

                for index, network in enumerate(
                    networks[:18]
                ):
                    marker = (
                        ">"
                        if index == self.wifi_selection
                        else " "
                    )

                    print(
                        f"{marker} "
                        f"{network['rssi']:>4}  "
                        f"{network['channel']:>2}  "
                        f"{network['security']:<10} "
                        f"{network['ssid'][:34]}"
                    )

        elif page == "CHANNELS":
            counts = Counter(
                network["channel"]
                for network in self.networks.values()
                if 1 <= network["channel"] <= 11
            )

            maximum = max(
                list(counts.values()) + [1]
            )

            for ch in range(1, 12):
                count = counts[ch]
                width = int(
                    count / maximum * 28
                )

                print(
                    f"CH {ch:>2} "
                    f"{'#' * width:<28} "
                    f"{count}"
                )

        elif page == "RF":
            if not self.rf_channels:
                print(
                    "Waiting for passive RF sweep..."
                )
            else:
                print(
                    "CH  ACTIVITY  PKTS   AVG   PEAK  MGMT DATA CTRL"
                )

                for ch in range(1, 12):
                    item = self.rf_channels.get(ch)

                    if not item:
                        print(
                            f"{ch:>2}  ---"
                        )
                        continue

                    print(
                        f"{ch:>2}  "
                        f"{item['activity']:>3}%      "
                        f"{item['packets']:>5} "
                        f"{item['avg_rssi']:>5} "
                        f"{item['peak_rssi']:>5} "
                        f"{item['mgmt']:>5} "
                        f"{item['data']:>4} "
                        f"{item['ctrl']:>4}"
                    )

        elif page == "SIGNAL":
            networks = self.sorted_networks()

            if not networks:
                print(
                    "No Wi-Fi network selected."
                )
            else:
                self._clamp_selections()
                net = networks[self.wifi_selection]

                values = [
                    item[1]
                    for item in net["history"]
                ]

                print("SSID    :", net["ssid"])
                print("BSSID   :", net["bssid"])
                print(
                    "RSSI    :",
                    f"{net['rssi']} dBm",
                )
                print(
                    "Signal  :",
                    signal_bar_text(
                        net["rssi"],
                        24,
                    ),
                    quality(net["rssi"]),
                )
                print(
                    "Channel :",
                    net["channel"],
                )
                print(
                    "Security:",
                    net["security"],
                )

                if values:
                    print(
                        "Samples :",
                        len(values),
                    )
                    print(
                        "Min/Avg/Max:",
                        min(values),
                        f"{sum(values)/len(values):.1f}",
                        max(values),
                    )

        elif page == "BLE":
            devices = self.sorted_ble()

            if not devices:
                print(
                    "Waiting for BLE advertisements..."
                )
            else:
                self._clamp_selections()

                print(
                    "     RSSI  NAME                         ADDRESS"
                )

                for index, device in enumerate(
                    devices[:18]
                ):
                    marker = (
                        ">"
                        if index == self.ble_selection
                        else " "
                    )

                    print(
                        f"{marker} "
                        f"{device['rssi']:>4}  "
                        f"{device['name'][:28]:<28} "
                        f"{device['address']}"
                    )

        elif page == "ETHERNET":
            self.update_ethernet()

            if not self.eth_cache:
                print(
                    "Collecting Ethernet data..."
                )
            else:
                for key, value in self.eth_cache.items():
                    print(
                        f"{key:<14}: {value}"
                    )

        elif page == "SYSTEM":
            stats = self.system_stats()

            print(
                "Core version :",
                CORE_VERSION,
            )
            print(
                "ESP version  :",
                self.esp_version or "unknown",
            )
            print(
                "CPU temp     :",
                stats["cpu_temp"],
            )
            print(
                "Throttled    :",
                stats["throttle"],
            )
            print(
                "Memory       :",
                stats["memory"],
            )
            print(
                "Disk free    :",
                stats["disk_free"],
            )
            print(
                "Wi-Fi log    :",
                self.wifi_log.status,
            )
            print(
                "BLE log      :",
                self.ble_log.status,
            )
            print(
                "RF log       :",
                self.rf_log.status,
            )

            if self.display_error:
                print(
                    "Display err  :",
                    self.display_error,
                )

            if self.uart_error:
                print(
                    "UART err     :",
                    self.uart_error,
                )

        print()
        print("-" * 72)
        print(
            "L=Page  LClick=Home  "
            "R=Select  RClick=Open  "
            "Hold L 5s=Shutdown"
        )
        print("Ctrl+C=Exit viewer")

    # ========================================================
    # DISPLAY
    # ========================================================

    def render_display(self):
        if self.ui is None:
            return

        try:
            self.ui.render(self)

        except Exception as exc:
            self.display_error = (
                f"{type(exc).__name__}: {exc}"
            )

            if self.terminal_enabled:
                print(
                    "[PNA] Display error:",
                    self.display_error,
                )

            try:
                self.display.close()
            except Exception:
                pass

            self.display = None
            self.ui = None

    # ========================================================
    # SHUTDOWN
    # ========================================================

    def perform_safe_shutdown(self):
        if self.ui is not None:
            try:
                image, draw = self.ui.new_frame()

                self.ui.text(
                    draw,
                    (240, 116),
                    "SAFE SHUTDOWN",
                    self.ui.font_large,
                    self.ui.YELLOW,
                    anchor="mm",
                )

                self.ui.text(
                    draw,
                    (240, 158),
                    "Saving logs and powering off...",
                    self.ui.font_medium,
                    self.ui.TEXT,
                    anchor="mm",
                )

                self.ui.text(
                    draw,
                    (240, 198),
                    "Wait for Pi activity to stop",
                    self.ui.font_body,
                    self.ui.MUTED,
                    anchor="mm",
                )

                self.display.show(image)

            except Exception:
                pass

        self.wifi_log.flush()
        self.ble_log.flush()
        self.rf_log.flush()

        time.sleep(0.5)

        # The sudoers entry previously configured for the PNA is:
        # prestonp ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff
        subprocess.run(
            [
                "/usr/bin/sudo",
                "/usr/bin/systemctl",
                "poweroff",
            ],
            check=False,
        )

    # ========================================================
    # MAIN LOOP
    # ========================================================

    def run(self):
        if self.terminal_enabled:
            print(
                f"Starting PNA V2 Core v{CORE_VERSION}..."
            )

        self.connect_uart()

        try:
            self.wifi_monitor.start()
        except Exception as exc:
            self.monitor_error = str(exc)

        last_display = 0
        last_terminal = 0
        last_log_flush = 0

        try:
            while True:
                self.poll_uart()
                self.handle_events()
                self.remove_old_data()

                now = time.time()

                if self.shutdown_requested:
                    self.perform_safe_shutdown()
                    break

                if (
                    self.ui is not None
                    and now - last_display
                    >= DISPLAY_REFRESH
                ):
                    self.render_display()
                    last_display = now

                if (
                    self.terminal_enabled
                    and now - last_terminal
                    >= TERMINAL_REFRESH
                ):
                    self.render_terminal()
                    last_terminal = now

                if now - last_log_flush >= 2:
                    self.wifi_log.flush()
                    self.ble_log.flush()
                    self.rf_log.flush()
                    last_log_flush = now

                time.sleep(0.02)

        except KeyboardInterrupt:
            if self.terminal_enabled:
                print()
                print("Stopping PNA viewer...")

        finally:
            self.close()

    def close(self):
        self.close_uart()

        try:
            self.wifi_monitor.stop()
        except Exception:
            pass

        self.wifi_log.close()
        self.ble_log.close()
        self.rf_log.close()

        if self.display is not None:
            try:
                self.display.close()
            except Exception:
                pass

        for device in (
            self.left_encoder,
            self.right_encoder,
            self.left_button,
            self.right_button,
        ):
            try:
                device.close()
            except Exception:
                pass


# ============================================================
# DISPLAY SMOKE TEST
# ============================================================

def display_smoke_test():
    if Image is None:
        raise RuntimeError(
            "Pillow/spidev dependencies are missing"
        )

    display = ST7796SDisplay()

    try:
        tests = [
            ((255, 0, 0), "RED"),
            ((0, 255, 0), "GREEN"),
            ((0, 0, 255), "BLUE"),
            ((0, 0, 0), "PNA V2"),
        ]

        for color, label in tests:
            image = Image.new(
                "RGB",
                (TFT_WIDTH, TFT_HEIGHT),
                color,
            )

            if label == "PNA V2":
                draw = ImageDraw.Draw(image)

                try:
                    font = ImageFont.truetype(
                        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                        42,
                    )
                except Exception:
                    font = ImageFont.load_default()

                draw.text(
                    (TFT_WIDTH // 2, TFT_HEIGHT // 2),
                    "PNA V2",
                    fill=(255, 255, 255),
                    font=font,
                    anchor="mm",
                )

            display.show(image)
            time.sleep(1.0)

    finally:
        display.close()


# ============================================================
# ENTRYPOINT
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="PNA V2 Raspberry Pi core"
    )

    parser.add_argument(
        "--display-test",
        action="store_true",
        help="cycle red/green/blue/PNA V2 on the ST7796S and exit",
    )

    parser.add_argument(
        "--no-display",
        action="store_true",
        help="run headless without opening the TFT",
    )

    parser.add_argument(
        "--no-log",
        action="store_true",
        help="disable CSV logging",
    )

    parser.add_argument(
        "--terminal",
        action="store_true",
        help="force the terminal/SSH UI even when stdout is not a TTY",
    )

    args = parser.parse_args()

    if args.display_test:
        display_smoke_test()
        return

    PNA(
        enable_display=not args.no_display,
        enable_logging=not args.no_log,
        force_terminal=args.terminal,
    ).run()


if __name__ == "__main__":
    main()
