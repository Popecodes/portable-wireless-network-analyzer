#!/usr/bin/env python3
"""
PNA V2 - TFT SIGNAL PIN FINDER
==============================

Purpose:
    Find which of the five existing Raspberry Pi signal wires is connected
    to which TFT signal when the display-side wires got mixed up.

SAFE SCOPE:
    This script ONLY permutes these five 3.3 V logic signals:
        CS, RESET, DC/RS, MOSI/SDI, SCK

    It NEVER touches:
        VCC, LED/BL, GND, SDO/MISO, or touch pins.

IMPORTANT BEFORE RUNNING:
    1. VCC, LED/BL, and GND must already be correctly wired.
    2. SDO/MISO must be DISCONNECTED for this test.
    3. Touch pins must be disconnected/ignored.
    4. Stop pna.service so it does not own the same GPIOs.

Pi signal wires being tested:
    GPIO8
    GPIO24
    GPIO25
    GPIO10
    GPIO11

The script tries all 120 possible assignments, but orders them so the
normal PNA wiring and simple swaps are tested first.

For each candidate it:
    - resets the panel
    - initializes an ST7796S
    - draws two colored blocks in the top-left corner
    - waits for you to answer y/n

If you see MAGENTA + CYAN blocks, press y.
The working mapping is saved to:
    ~/pna/tft_found_mapping.txt
"""

import itertools
import time
from pathlib import Path

import lgpio


# ------------------------------------------------------------
# FIXED PI GPIO WIRES
# ------------------------------------------------------------

GPIO_WIRES = [8, 24, 25, 10, 11]

FUNCTIONS = ["CS", "RST", "DC", "MOSI", "SCK"]

EXPECTED = {
    "CS": 8,
    "RST": 24,
    "DC": 25,
    "MOSI": 10,
    "SCK": 11,
}

SAVE_PATH = Path.home() / "pna" / "tft_found_mapping.txt"


# ------------------------------------------------------------
# LOW-LEVEL GPIO / SOFTWARE SPI
# ------------------------------------------------------------

class BitBangTFT:
    def __init__(self, chip_handle, mapping):
        self.h = chip_handle
        self.cs = mapping["CS"]
        self.rst = mapping["RST"]
        self.dc = mapping["DC"]
        self.mosi = mapping["MOSI"]
        self.sck = mapping["SCK"]

    def write(self, gpio, value):
        lgpio.gpio_write(self.h, gpio, 1 if value else 0)

    def pulse_clock(self):
        self.write(self.sck, 1)
        self.write(self.sck, 0)

    def send_byte(self, value):
        for bit in range(7, -1, -1):
            self.write(self.mosi, (value >> bit) & 1)
            self.pulse_clock()

    def send_bytes(self, data):
        for value in data:
            self.send_byte(value)

    def command(self, cmd, data=None):
        self.write(self.cs, 0)
        self.write(self.dc, 0)
        self.send_byte(cmd)

        if data:
            self.write(self.dc, 1)
            self.send_bytes(data)

        self.write(self.cs, 1)

    def hardware_reset(self):
        self.write(self.cs, 1)
        self.write(self.rst, 1)
        time.sleep(0.020)

        self.write(self.rst, 0)
        time.sleep(0.120)

        self.write(self.rst, 1)
        time.sleep(0.180)

    def init_st7796s(self):
        self.hardware_reset()

        # Software reset
        self.command(0x01)
        time.sleep(0.120)

        # Sleep out
        self.command(0x11)
        time.sleep(0.120)

        # Unlock manufacturer commands
        self.command(0xF0, [0xC3])
        self.command(0xF0, [0x96])

        # RGB565
        self.command(0x3A, [0x55])

        # Landscape + BGR
        self.command(0x36, [0x28])

        # Normal / inversion off
        self.command(0x20)
        self.command(0x13)

        # Relock
        self.command(0xF0, [0x3C])
        self.command(0xF0, [0x69])

        # Display on
        self.command(0x29)
        time.sleep(0.060)

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

    def fill_rect_rgb565(self, x0, y0, width, height, rgb565):
        x1 = x0 + width - 1
        y1 = y0 + height - 1

        self.set_window(x0, y0, x1, y1)

        self.write(self.cs, 0)
        self.write(self.dc, 0)
        self.send_byte(0x2C)

        self.write(self.dc, 1)

        hi = (rgb565 >> 8) & 0xFF
        lo = rgb565 & 0xFF

        for _ in range(width * height):
            self.send_byte(hi)
            self.send_byte(lo)

        self.write(self.cs, 1)

    def draw_test_pattern(self):
        # Bright MAGENTA block
        self.fill_rect_rgb565(
            12, 12,
            36, 36,
            0xF81F,
        )

        # Bright CYAN block
        self.fill_rect_rgb565(
            56, 12,
            36, 36,
            0x07FF,
        )


# ------------------------------------------------------------
# CANDIDATE ORDER
# ------------------------------------------------------------

def candidate_mappings():
    expected_tuple = tuple(EXPECTED[name] for name in FUNCTIONS)

    permutations = list(itertools.permutations(GPIO_WIRES))

    # Test normal mapping first, then simple swaps, then increasingly
    # different assignments.
    permutations.sort(
        key=lambda perm: (
            sum(
                1
                for a, b
                in zip(perm, expected_tuple)
                if a != b
            ),
            perm,
        )
    )

    for perm in permutations:
        yield dict(zip(FUNCTIONS, perm))


def mapping_text(mapping):
    return (
        f"CS=GPIO{mapping['CS']}  "
        f"RST=GPIO{mapping['RST']}  "
        f"DC=GPIO{mapping['DC']}  "
        f"MOSI=GPIO{mapping['MOSI']}  "
        f"SCK=GPIO{mapping['SCK']}"
    )


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

def main():
    print()
    print("=" * 68)
    print(" PNA V2 - TFT SIGNAL PIN FINDER")
    print("=" * 68)
    print()
    print("This only tests the FIVE 3.3 V signal wires.")
    print()
    print("DO NOT run this if VCC/GND/LED are mixed with signal wires.")
    print("SDO/MISO must be disconnected.")
    print()
    print("Expected normal mapping:")
    print(" ", mapping_text(EXPECTED))
    print()
    input("Press ENTER when power wiring is verified and PNA service is stopped...")

    h = lgpio.gpiochip_open(0)

    try:
        # Claim the five candidate GPIOs once.
        for gpio in GPIO_WIRES:
            lgpio.gpio_claim_output(h, gpio, 1)

        candidates = list(candidate_mappings())

        for number, mapping in enumerate(candidates, start=1):
            print()
            print("-" * 68)
            print(f"TEST {number:03d}/{len(candidates)}")
            print(mapping_text(mapping))
            print("-" * 68)

            # Stable idle state before each attempt.
            for gpio in GPIO_WIRES:
                lgpio.gpio_write(h, gpio, 1)

            time.sleep(0.050)

            try:
                tft = BitBangTFT(h, mapping)
                tft.init_st7796s()
                tft.draw_test_pattern()

            except Exception as exc:
                print("Test error:", exc)

            print()
            print("LOOK AT THE DISPLAY.")
            print("Working mapping should show two blocks near top-left:")
            print("  MAGENTA  +  CYAN")
            print()

            answer = input(
                "[y] works   [n/Enter] next   [q] quit : "
            ).strip().lower()

            if answer == "y":
                SAVE_PATH.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                result = (
                    "PNA V2 TFT WORKING SIGNAL MAPPING\n"
                    "=================================\n"
                    f"{mapping_text(mapping)}\n\n"
                    "Display labels:\n"
                    f"CS       -> Pi GPIO{mapping['CS']}\n"
                    f"RESET    -> Pi GPIO{mapping['RST']}\n"
                    f"DC/RS    -> Pi GPIO{mapping['DC']}\n"
                    f"SDI/MOSI -> Pi GPIO{mapping['MOSI']}\n"
                    f"SCK      -> Pi GPIO{mapping['SCK']}\n"
                    "SDO/MISO -> disconnected for normal drawing\n"
                )

                SAVE_PATH.write_text(
                    result,
                    encoding="utf-8",
                )

                print()
                print("FOUND IT.")
                print(result)
                print(f"Saved to: {SAVE_PATH}")
                return

            if answer == "q":
                print("Stopped by user.")
                return

        print()
        print("All 120 signal permutations were tested.")
        print()
        print("If none worked, the issue is probably NOT signal order.")
        print("Next suspects:")
        print("  - incorrect VCC / LED / GND")
        print("  - wrong controller/init sequence")
        print("  - bad connection / solder joint")
        print("  - display module variant")
        print("  - panel orientation/init parameters")

    finally:
        for gpio in GPIO_WIRES:
            try:
                lgpio.gpio_free(h, gpio)
            except Exception:
                pass

        lgpio.gpiochip_close(h)


if __name__ == "__main__":
    main()
