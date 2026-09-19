/*
  ============================================================
  PNA V2 - ESP32-S3 Radio Coprocessor
  Firmware v1.0

  Purpose
  -------
  Passive radio measurement engine for the PNA V2.

  Features
  --------
  - Wi-Fi AP discovery: SSID/BSSID/RSSI/channel/security
  - Passive 2.4 GHz RF channel sweep on channels 1-11
  - Per-channel packet count, bytes, average RSSI, peak RSSI
  - Approximate channel activity percentage
  - Passive BLE advertisement discovery
  - Heartbeat/status records to Raspberry Pi
  - UART streaming to Raspberry Pi

  This firmware intentionally contains passive measurement
  features only. It does not transmit deauth/injection/spoofing
  attack frames or collect credentials.

  Hardware
  --------
  ESP32-S3 UART1 TX GPIO44 -> Raspberry Pi GPIO15 / RX
  Common GND required.
  UART direction is intentionally ESP32 -> Pi only.

  Arduino IDE
  -----------
  Board: ESP32S3 Dev Module
  Serial monitor: 115200
  Partition scheme: Huge APP recommended

  UART protocol
  -------------
  BOOT|PNA_ESP32|1.0
  ESP32_ALIVE
  SCAN|WIFI_BEGIN
  WIFI|SSID|BSSID|RSSI|CHANNEL|SECURITY
  SCAN|WIFI_END|COUNT
  SCAN|RF_BEGIN
  RF|CHANNEL|PACKETS|BYTES|AVG_RSSI|PEAK_RSSI|ACTIVITY|MGMT|DATA|CTRL
  SCAN|RF_END
  SCAN|BLE_BEGIN
  BLE|NAME|ADDRESS|RSSI
  SCAN|BLE_END|COUNT
  ERR|SUBSYSTEM|CODE
  ============================================================
*/

#include <Arduino.h>
#include <WiFi.h>
#include <esp_wifi.h>

#include <BLEDevice.h>
#include <BLEUtils.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>


// ============================================================
// CONFIG
// ============================================================

static const char *FW_VERSION = "1.0";

HardwareSerial PiSerial(1);

constexpr int PI_UART_TX = 44;
constexpr uint32_t PI_UART_BAUD = 115200;

constexpr uint32_t HEARTBEAT_INTERVAL_MS = 1000;
constexpr uint32_t RADIO_CYCLE_GAP_MS = 2500;

constexpr uint8_t RF_FIRST_CHANNEL = 1;
constexpr uint8_t RF_LAST_CHANNEL = 11;
constexpr uint16_t RF_DWELL_MS = 300;

constexpr uint32_t BLE_SCAN_SECONDS = 4;

BLEScan *pBLEScan = nullptr;

unsigned long lastHeartbeat = 0;


// ============================================================
// RF COUNTERS
// ============================================================

static volatile uint32_t rfPackets = 0;
static volatile uint32_t rfBytes = 0;
static volatile int32_t rfRssiSum = 0;
static volatile int8_t rfPeakRssi = -127;

static volatile uint32_t rfMgmt = 0;
static volatile uint32_t rfData = 0;
static volatile uint32_t rfCtrl = 0;


// ============================================================
// HELPERS
// ============================================================

String cleanField(String value) {
  value.replace("|", "_");
  value.replace("\r", " ");
  value.replace("\n", " ");

  if (value.length() == 0) {
    value = "<unknown>";
  }

  return value;
}

const char *securityName(wifi_auth_mode_t auth) {
  switch (auth) {
    case WIFI_AUTH_OPEN:
      return "OPEN";

    case WIFI_AUTH_WEP:
      return "WEP";

    case WIFI_AUTH_WPA_PSK:
      return "WPA";

    case WIFI_AUTH_WPA2_PSK:
      return "WPA2";

    case WIFI_AUTH_WPA_WPA2_PSK:
      return "WPA/WPA2";

    case WIFI_AUTH_WPA2_ENTERPRISE:
      return "WPA2-ENT";

    case WIFI_AUTH_WPA3_PSK:
      return "WPA3";

    case WIFI_AUTH_WPA2_WPA3_PSK:
      return "WPA2/WPA3";

    default:
      return "UNKNOWN";
  }
}


void sendHeartbeat() {
  PiSerial.println("ESP32_ALIVE");
  lastHeartbeat = millis();
}


void serviceHeartbeat() {
  unsigned long now = millis();

  if (now - lastHeartbeat >= HEARTBEAT_INTERVAL_MS) {
    sendHeartbeat();
  }
}


// ============================================================
// PASSIVE PROMISCUOUS RX CALLBACK
// ============================================================

void IRAM_ATTR rfSnifferCallback(
  void *buf,
  wifi_promiscuous_pkt_type_t type
) {
  if (!buf) {
    return;
  }

  const wifi_promiscuous_pkt_t *pkt =
    reinterpret_cast<const wifi_promiscuous_pkt_t *>(buf);

  const int8_t rssi = pkt->rx_ctrl.rssi;
  const uint16_t length = pkt->rx_ctrl.sig_len;

  rfPackets++;
  rfBytes += length;
  rfRssiSum += rssi;

  if (rssi > rfPeakRssi) {
    rfPeakRssi = rssi;
  }

  switch (type) {
    case WIFI_PKT_MGMT:
      rfMgmt++;
      break;

    case WIFI_PKT_DATA:
      rfData++;
      break;

    case WIFI_PKT_CTRL:
      rfCtrl++;
      break;

    default:
      break;
  }
}


// ============================================================
// BLE CALLBACK
// ============================================================

class PNAAdvertisedDeviceCallbacks
  : public BLEAdvertisedDeviceCallbacks {

  void onResult(
    BLEAdvertisedDevice advertisedDevice
  ) override {

    String name = "<unknown>";

    if (advertisedDevice.haveName()) {
      name = advertisedDevice.getName().c_str();
    }

    String address =
      advertisedDevice.getAddress().toString().c_str();

    name = cleanField(name);
    address = cleanField(address);

    const int rssi = advertisedDevice.getRSSI();

    PiSerial.print("BLE|");
    PiSerial.print(name);
    PiSerial.print("|");
    PiSerial.print(address);
    PiSerial.print("|");
    PiSerial.println(rssi);
  }
};


// ============================================================
// WI-FI AP SCAN
// ============================================================

void scanWiFi() {
  PiSerial.println("SCAN|WIFI_BEGIN");

  Serial.println();
  Serial.println("[PNA] Wi-Fi AP scan");

  const int count = WiFi.scanNetworks();

  if (count < 0) {
    PiSerial.print("ERR|WIFI_SCAN|");
    PiSerial.println(count);
    PiSerial.println("SCAN|WIFI_END|0");
    return;
  }

  for (int i = 0; i < count; i++) {
    String ssid = WiFi.SSID(i);

    if (ssid.length() == 0) {
      ssid = "<hidden>";
    }

    ssid = cleanField(ssid);

    const String bssid =
      cleanField(WiFi.BSSIDstr(i));

    const int rssi = WiFi.RSSI(i);
    const int channel = WiFi.channel(i);

    const wifi_auth_mode_t auth =
      WiFi.encryptionType(i);

    PiSerial.print("WIFI|");
    PiSerial.print(ssid);
    PiSerial.print("|");
    PiSerial.print(bssid);
    PiSerial.print("|");
    PiSerial.print(rssi);
    PiSerial.print("|");
    PiSerial.print(channel);
    PiSerial.print("|");
    PiSerial.println(securityName(auth));
  }

  WiFi.scanDelete();

  PiSerial.print("SCAN|WIFI_END|");
  PiSerial.println(count);

  Serial.print("[PNA] Wi-Fi APs: ");
  Serial.println(count);
}


// ============================================================
// PASSIVE RF CHANNEL SWEEP
// ============================================================

void scanRFChannels() {
  PiSerial.println("SCAN|RF_BEGIN");

  Serial.println();
  Serial.println("[PNA] Passive 2.4 GHz RF sweep");

  wifi_promiscuous_filter_t filter = {};
  filter.filter_mask = WIFI_PROMIS_FILTER_MASK_ALL;

  esp_wifi_set_promiscuous_filter(&filter);
  esp_wifi_set_promiscuous_rx_cb(rfSnifferCallback);

  for (
    uint8_t channel = RF_FIRST_CHANNEL;
    channel <= RF_LAST_CHANNEL;
    channel++
  ) {
    esp_wifi_set_promiscuous(false);

    esp_err_t chResult =
      esp_wifi_set_channel(
        channel,
        WIFI_SECOND_CHAN_NONE
      );

    if (chResult != ESP_OK) {
      PiSerial.print("ERR|RF_CHANNEL|");
      PiSerial.println(static_cast<int>(chResult));
      continue;
    }

    // Let the PLL/channel settle before counting.
    delay(15);

    rfPackets = 0;
    rfBytes = 0;
    rfRssiSum = 0;
    rfPeakRssi = -127;
    rfMgmt = 0;
    rfData = 0;
    rfCtrl = 0;

    esp_wifi_set_promiscuous(true);

    const unsigned long started = millis();

    while (millis() - started < RF_DWELL_MS) {
      serviceHeartbeat();
      delay(10);
    }

    esp_wifi_set_promiscuous(false);

    const uint32_t packets = rfPackets;
    const uint32_t bytes = rfBytes;
    const int32_t rssiSum = rfRssiSum;
    const int8_t peak = rfPeakRssi;

    const uint32_t mgmt = rfMgmt;
    const uint32_t data = rfData;
    const uint32_t ctrl = rfCtrl;

    int avgRssi = -127;

    if (packets > 0) {
      avgRssi =
        static_cast<int>(
          rssiSum / static_cast<int32_t>(packets)
        );
    }

    /*
      Approximate activity metric inspired by a passive
      channel analyzer approach:
      - payload bits estimated at a conservative 6 Mbps
      - fixed per-frame overhead approximation
      This is a relative activity estimate, not calibrated
      spectrum-analyzer airtime.
    */
    const uint32_t airtimeUs =
      (bytes * 8UL) / 6UL
      + packets * 60UL;

    const uint32_t dwellUs =
      static_cast<uint32_t>(RF_DWELL_MS) * 1000UL;

    uint32_t activity =
      dwellUs
      ? (airtimeUs * 100UL) / dwellUs
      : 0;

    if (activity > 100) {
      activity = 100;
    }

    PiSerial.print("RF|");
    PiSerial.print(channel);
    PiSerial.print("|");
    PiSerial.print(packets);
    PiSerial.print("|");
    PiSerial.print(bytes);
    PiSerial.print("|");
    PiSerial.print(avgRssi);
    PiSerial.print("|");
    PiSerial.print(peak);
    PiSerial.print("|");
    PiSerial.print(activity);
    PiSerial.print("|");
    PiSerial.print(mgmt);
    PiSerial.print("|");
    PiSerial.print(data);
    PiSerial.print("|");
    PiSerial.println(ctrl);
  }

  esp_wifi_set_promiscuous(false);
  esp_wifi_set_promiscuous_rx_cb(nullptr);

  PiSerial.println("SCAN|RF_END");

  Serial.println("[PNA] RF sweep complete");
}


// ============================================================
// PASSIVE BLE SCAN
// ============================================================

void scanBLE() {
  PiSerial.println("SCAN|BLE_BEGIN");

  Serial.println();
  Serial.println("[PNA] Passive BLE scan");

  pBLEScan->setActiveScan(false);

  BLEScanResults *results =
    pBLEScan->start(
      BLE_SCAN_SECONDS,
      false
    );

  int count = 0;

  if (results != nullptr) {
    count = results->getCount();
  }

  pBLEScan->clearResults();

  PiSerial.print("SCAN|BLE_END|");
  PiSerial.println(count);

  Serial.print("[PNA] BLE advertisements: ");
  Serial.println(count);
}


// ============================================================
// SETUP
// ============================================================

void setup() {
  Serial.begin(115200);

  delay(500);

  // Proven one-way UART path:
  // ESP32 GPIO44 -> Raspberry Pi GPIO15 RX.
  PiSerial.begin(
    PI_UART_BAUD,
    SERIAL_8N1,
    -1,
    PI_UART_TX
  );

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();

  BLEDevice::init("PNA-V2");

  pBLEScan = BLEDevice::getScan();

  pBLEScan->setAdvertisedDeviceCallbacks(
    new PNAAdvertisedDeviceCallbacks()
  );

  // Passive BLE advertisements only.
  pBLEScan->setActiveScan(false);
  pBLEScan->setInterval(100);
  pBLEScan->setWindow(80);

  PiSerial.print("BOOT|PNA_ESP32|");
  PiSerial.println(FW_VERSION);

  sendHeartbeat();

  Serial.println();
  Serial.println("==================================");
  Serial.println(" PNA V2 ESP32 RADIO CORE v1.0");
  Serial.println("==================================");
  Serial.println("UART TX   : GPIO44");
  Serial.println("Wi-Fi     : passive discovery");
  Serial.println("RF sweep  : passive channels 1-11");
  Serial.println("BLE       : passive advertisements");
}


// ============================================================
// MAIN LOOP
// ============================================================

void loop() {
  serviceHeartbeat();

  scanWiFi();
  sendHeartbeat();

  delay(150);

  scanRFChannels();
  sendHeartbeat();

  delay(150);

  scanBLE();
  sendHeartbeat();

  const unsigned long pauseStarted = millis();

  while (
    millis() - pauseStarted
    < RADIO_CYCLE_GAP_MS
  ) {
    serviceHeartbeat();
    delay(25);
  }
}
