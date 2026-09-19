# PNA V2 Mode System v0.1

The optional mode selector provides:

- **PNA Analyzer** — launches the main `pna.py` application
- **Bruce Safe** — passive wireless-security monitoring
- **Security Lab** — local, simulation-only detection demonstrations
- **Shutdown** — safely powers off the Raspberry Pi

If untouched, the selector launches PNA Analyzer after 10 seconds.

## Hardware configuration

- ST7796S 480×320 display, MADCTL `0xE8`
- Left encoder: CLK GPIO5, DT GPIO6, SW GPIO13
- Right encoder: CLK GPIO16, DT GPIO20, SW GPIO21
- Encoder VCC disconnected; inputs use Raspberry Pi pull-ups

## Layout

The launcher resolves paths relative to this repository. It expects the main
analyzer at `src/raspberry-pi/pna.py`.

## Service template

`pna-launcher@.service` is a systemd template. If the repository is installed
at `~/pna`, install and enable it for the target Linux account:

```sh
sudo cp pna-launcher@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now "pna-launcher@$USER.service"
```

The target account must already have access to SPI, GPIO, UART, and the narrowly
scoped power-off command used by the device.

## v0.1 limitation

Bruce Safe and Security Lab return to the selector when the right encoder is
held for three seconds. The current main `pna.py` does not implement the same
right-hold return behavior, so leaving PNA Analyzer still requires restarting
the launcher service or rebooting.
