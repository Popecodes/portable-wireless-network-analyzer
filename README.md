# Portable Wireless Network Analyzer (PNA V2)

A handheld, battery-powered network diagnostic instrument built with a **Raspberry Pi 4**, **ESP32-S3**, a dedicated USB Wi-Fi monitoring adapter, a **480 × 320 ST7796S SPI TFT**, and two rotary encoders.

## Current capabilities

### Ethernet diagnostics and discovery
- Detect wired interface status, IPv4 address, link speed and duplex, gateway, DNS resolution, and RX/TX errors.
- Ping the gateway, test external connectivity when routed, and ping a chosen diagnostic target.
- **SCAN LAN**: explicitly initiate a bounded ICMP sweep of the connected private IPv4 /24 subnet, with progress on the TFT. Discovery runs in a background worker to keep the interface responsive.
- Scroll through discovered responsive IP addresses using the right rotary encoder.
- **Device inspector**: view available IP, hostname, MAC address, latency, and results of checks for common TCP services (SSH, HTTP, HTTPS, SMB, RDP, and 8080).

**Discovery limitations:** hosts that block ICMP may not appear. Hostnames, MAC addresses, and port results depend on network configuration and device responses. Only scan networks you own or are authorized to assess.

### Wireless and system
- Wi-Fi access point scanning, signal-strength visualization, and channel statistics.
- BLE scanning and passive 2.4 GHz RF activity observation through the ESP32-S3.
- Dedicated USB Wi-Fi monitor radio for raw 802.11 observations, including supported 2.4 GHz and 5 GHz channels.
- TFT status, signal, Ethernet, monitoring, and system pages.
- Resilient logging and headless/SSH fallback.

## Hardware

| Component | Purpose |
| --- | --- |
| Raspberry Pi 4 | Main Python application, display, Ethernet diagnostics, system monitoring |
| ESP32-S3 | Wi-Fi/BLE/RF coprocessor over UART |
| Netgear USB Wi-Fi adapter | Dedicated monitor-mode 802.11 radio |
| ST7796S 480 × 320 SPI TFT | Local display |
| Left rotary encoder | Navigate pages; click for home; hold 5 seconds for safe shutdown |
| Right rotary encoder | Select entries, scan/ping Ethernet targets, inspect devices |
| Portable 5 V battery supply | Mobile operation |

### Interface connections

- TFT: SPI0 CE0 GPIO8, RESET GPIO24, DC GPIO25, MOSI GPIO10, SCK GPIO11.
- Left encoder: CLK GPIO5, DT GPIO6, SW GPIO13.
- Right encoder: CLK GPIO16, DT GPIO20, SW GPIO21.
- ESP32-S3 to Pi: UART `/dev/serial0`, 115200 baud.

## Firmware

The active Raspberry Pi application is `pna.py`. It uses the companion `wifi_monitor_backend.py` for USB Wi-Fi monitoring and is run by `pna.service` on the Pi.

For the November 2026 device-inspector build, the tested source is named `pna_ethernet_device_inspector.py`. Once uploaded to this repository, it can be copied to `pna.py` on the Pi after backing up the existing file.

### Deploy on the Raspberry Pi

```bash
cd ~/pna
cp -a pna.py "pna_backup_$(date +%Y%m%d_%H%M%S).py"
python3 -m py_compile pna_ethernet_device_inspector.py
cp pna_ethernet_device_inspector.py pna.py
sudo systemctl restart pna.service
sudo systemctl status pna.service --no-pager
```

Configure an optional fixed Ethernet target using the systemd service environment variable `PNA_ETH_TARGET` (IPv4 address).

## PNA V2 Ethernet controls

1. Navigate to **ETHERNET** with the left encoder.
2. Rotate the right encoder to **SCAN LAN** and click to begin discovery.
3. Wait for the scan to finish, then rotate through responsive hosts.
4. Click a selected host to ping it.
5. Hold the right encoder button for approximately one second to open the device inspector; click to return.

## Project evolution

**V1:** ESP32-only handheld Wi-Fi analyzer.

**V2:** Raspberry Pi 4 + ESP32-S3, larger SPI TFT, USB monitor radio, BLE/RF/Wi-Fi pages, Ethernet diagnostics, interactive LAN discovery, and device inspection.

**Future:** custom PCB, Compute Module integration, 3D-printed enclosure, improved power management, and expanded diagnostics.

## Engineering skills demonstrated

Embedded Python, C++, GPIO, SPI, UART, Linux/systemd, Ethernet and IP networking, wireless analysis, concurrent background tasks, hardware integration, and debugging.

## Author

**Preston S. Pope**  
Electrical Engineering Student  
Missouri University of Science and Technology
