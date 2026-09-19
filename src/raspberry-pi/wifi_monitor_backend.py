#!/usr/bin/env python3
"""Passive 802.11 frame statistics for a monitor-mode Wi-Fi interface."""

import os
import re
import subprocess
import threading
import time
from collections import deque


DEFAULT_INTERFACE = os.environ.get("PNA_MONITOR_INTERFACE", "wlan1")


class WiFiMonitor:
    """Collect high-level frame and signal statistics from tcpdump output."""

    def __init__(self, iface=DEFAULT_INTERFACE):
        self.iface = iface

        self.total = 0
        self.beacons = 0
        self.data = 0
        self.control = 0
        self.management = 0

        self.rssi = deque(maxlen=100)
        self.aps = set()

        self.running = False
        self.proc = None
        self.thread = None

    def start(self):
        if self.running:
            return

        self.running = True

        try:
            self.proc = subprocess.Popen(
                [
                    "tcpdump",
                    "-i",
                    self.iface,
                    "-e",
                    "-n",
                    "-l",
                    "-s",
                    "192",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except Exception:
            self.running = False
            raise

        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _reader(self):
        while self.running and self.proc and self.proc.stdout:
            line = self.proc.stdout.readline()

            if not line:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.01)
                continue

            self.total += 1

            match = re.search(r"(-?\d+)dBm signal", line)
            if match:
                try:
                    self.rssi.append(int(match.group(1)))
                except ValueError:
                    pass

            match = re.search(r"BSSID:([0-9a-fA-F:]{17})", line)
            if match:
                self.aps.add(match.group(1).lower())

            if "Beacon (" in line:
                self.beacons += 1
                self.management += 1
            elif any(
                frame_type in line
                for frame_type in (
                    "Probe Request",
                    "Probe Response",
                    "Association Request",
                    "Association Response",
                    "Authentication",
                )
            ):
                self.management += 1
            elif any(
                frame_type in line
                for frame_type in (
                    "Acknowledgment",
                    "Request-To-Send",
                    "Clear-To-Send",
                    "Power Save-Poll",
                )
            ):
                self.control += 1
            else:
                self.data += 1

        self.running = False

    def snapshot(self):
        avg = None
        peak = None

        if self.rssi:
            avg = round(sum(self.rssi) / len(self.rssi))
            peak = max(self.rssi)

        return {
            "iface": self.iface,
            "frames": self.total,
            "beacons": self.beacons,
            "management": self.management,
            "data": self.data,
            "control": self.control,
            "aps": len(self.aps),
            "avg_rssi": avg,
            "peak_rssi": peak,
        }

    def stop(self):
        self.running = False

        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass

        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1)

        self.proc = None
        self.thread = None


if __name__ == "__main__":
    monitor = WiFiMonitor()
    monitor.start()

    try:
        while True:
            print(monitor.snapshot())
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()
