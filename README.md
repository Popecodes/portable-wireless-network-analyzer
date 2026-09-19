# Portable Wireless Network Analyzer

A handheld wireless diagnostic system built with a Raspberry Pi 4, ESP32-S3, and dedicated USB Wi-Fi monitor radio.

## Overview

This project is a portable wireless network analyzer designed to provide real-time Wi-Fi, Bluetooth Low Energy, RF, Ethernet, and signal diagnostics through a handheld interface.

The project began as an ESP32-only prototype and evolved into a Raspberry Pi + ESP32 architecture with a larger display, dedicated wireless monitoring hardware, and expanded network analysis capabilities.

## Features

- Wi-Fi access point scanning
- Bluetooth Low Energy device scanning
- Passive 2.4 GHz RF channel analysis
- 5 GHz monitor-mode wireless analysis
- RSSI and signal-strength visualization
- Ethernet diagnostics
- Real-time packet statistics
- Rotary encoder navigation
- 4-inch SPI TFT interface
- Data logging
- Portable battery-powered operation

## Hardware

- Raspberry Pi 4
- ESP32-S3
- Netgear USB Wi-Fi adapter
- 4-inch ST7796S 480x320 TFT display
- Dual rotary encoders
- External Wi-Fi antenna
- Portable battery supply

## System Architecture

### Raspberry Pi 4
Handles:

- User interface
- TFT display
- Data processing
- Ethernet diagnostics
- System monitoring
- Logging
- USB Wi-Fi monitor-mode analysis

### ESP32-S3
Handles:

- Wi-Fi scanning
- BLE scanning
- Passive 2.4 GHz RF activity measurement
- Wireless data collection

### Netgear USB Wi-Fi Radio
Handles:

- Dedicated monitor mode
- Raw 802.11 frame observation
- 2.4 GHz / 5 GHz wireless analysis
- Packet and channel statistics

## Communication

The ESP32-S3 communicates with the Raspberry Pi using UART.

The Pi combines data from the ESP32, Ethernet interface, and dedicated USB Wi-Fi radio into one real-time interface.

## Project Evolution

### V1
ESP32-only handheld Wi-Fi analyzer with a small display and rotary encoder controls.

### V2
Raspberry Pi 4 + ESP32-S3 architecture with:

- Larger TFT display
- Wi-Fi scanning
- BLE scanning
- RF analysis
- Ethernet diagnostics
- Dedicated USB Wi-Fi monitor radio
- Real-time system interface

### Future Development

- Custom PCB
- Raspberry Pi Compute Module integration
- ESP32-S3 module integration
- 3D-printed enclosure
- Improved power management
- Expanded wireless diagnostics

## Skills Demonstrated

- Electrical engineering
- Embedded systems
- Raspberry Pi
- ESP32
- Python
- C++
- Linux
- UART
- SPI
- GPIO
- Wireless networking
- PCB design
- Hardware debugging
- System integration

## Author

**Preston S. Pope**  
Electrical Engineering Student  
Missouri University of Science and Technology
