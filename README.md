# ene.py — control the lighting of Crucial Ballistix RGB from GNU/Linux

English | [Русский](README.ru.md)

Control the lighting of **Crucial Ballistix RGB (DDR4)** memory modules
straight from userspace via SMBus (`/dev/i2c-*`) - no OpenRGB, no X server,
no Qt. A single pure-Python script (standard library only) that runs as
root: one-shot commands, a sync daemon, and systemd units for autostart.

> The utility and this documentation were written by an **AI assistant**
> (OpenCode, the GLM-5.3-Flash model by Z.ai) and verified on real hardware.

---

## Tested on

| Component | Value |
|---|---|
| Motherboard | ASRock Z490M-ITX/ac (Intel i801 SMBus controller, bus `i2c-0`) |
| Memory | Crucial Ballistix RGB DDR4 `BL16G30C15U4WL.M16FE`, 2 modules at addresses `0x20` and `0x22` |
| Lighting controller | ENE, chip ID `0x0634` (chips at addresses `0x20`/`0x22`) |
| Controller firmware | `v.20.19.05.22.00`, build `May 22 2019` (read via the `info` command) |
| OS | Debian GNU/Linux 13 (trixie) |
| Architecture | amd64 |
| Python | 3.13.5 (standard library only, no external dependencies) |

Verified on this system: module detection, turning the lighting off,
reading module information (firmware version, vendor), and graceful
daemon termination on signals.

---

## How it works (overview)

- The LEDs (16 of them, 8 zones of two LEDs each) are driven by an ENE
  controller right on the module, over the system bus.
- Every module has its own address on the SMBus: usually `0x20` and `0x22`.
  The script finds the modules itself (`scan`) - no need to specify
  addresses manually.
- Settings **do not survive a reboot** - after the PC is powered on, the
  mode has to be set again (once, or via a systemd unit, see below).

---

## Install

```bash
sudo cp ene.py /usr/local/bin/ene.py
sudo chmod +x /usr/local/bin/ene.py
```

## Commands

Without `--addr` a command applies to **all found modules** at once;
`--addr 0x20` - to one, `--addr 0x20 --addr 0x22` (or `--addr 0x20,0x22`) -
to the listed ones (e.g. two out of four modules).

```bash
sudo ene.py scan                    # find modules and show addresses
sudo ene.py off                     # TURN OFF the lighting
sudo ene.py on 255                  # brightness 0-255 (255 = max)
sudo ene.py color 255 0 0           # color R G B on all zones
sudo ene.py color 255 0 0 0x55      # color only on the zones by mask 0x55
sudo ene.py speed 16                # animation speed (0-255)
sudo ene.py pattern static 255 0 0  # user modes: static/breathing/flashing
sudo ene.py std-pat wave 0 0 255    # stock effects: shift, gradient, fill,
                                    #   stack, double-stack, breathing, motion,
                                    #   inside-out, color-step, wave, flashing,
                                    #   static (+ color); "rainbow" is an alias
                                    #   for shift without a color
sudo ene.py rainbow                 # smooth rainbow
sudo ene.py timing 0xFF 500 200     # on 500 ms / off 200 ms (blinking)
sudo ene.py sync on                 # enable module sync + a clock tick
sudo ene.py info                    # everything known about the module:
                                    #   chip ID, firmware, vendor, zones, color
```

**Zone mask** - 8 bits, one bit per zone: `0x01` - zone 1, `0x80` - zone 8,
`0xFF` - all. Different colors on one module - two `color` calls with
different masks (e.g. a checkerboard: `color 255 0 0 0x55`, then
`color 0 0 255 0xAA`).

## Daemon (animations on several modules in phase)

Each controller has its own internal clock, so animations on two modules
gradually drift apart without sync. The daemon applies the mode and sends
the controllers a sync tick every ~110 ms. Modules that disappear are
rescanned (a rejoined module gets its mode applied again), a lost bus does
not crash the process, SIGTERM/SIGINT - graceful exit.

The daemon logs module events (found / lost / rejoined, the list of modules
it works with) with timestamps - under systemd they land in the journal:
`journalctl -u ene-syncd.service`.

```bash
sudo ene.py daemon                  # no arguments = rainbow + sync
sudo ene.py daemon --brightness 128 # rainbow with a brightness level
sudo ene.py daemon --pattern breathing --color 0 0 255
sudo ene.py daemon --color 255 0 0
```

For turning the lights off or a static color the daemon is not needed -
use the one-shot `off` / `color` / `pattern`.

## Start at boot (systemd)

Option 1 - apply a mode once at boot (`ene-off.service`, oneshot;
no sync needed):

```bash
sudo cp ene.py /usr/local/bin/ene.py
sudo cp ene-off.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ene-off.service
```

Option 2 - daemon with sync for animations (`ene-syncd.service`;
rainbow by default):

```bash
sudo cp ene.py /usr/local/bin/ene.py
sudo cp ene-syncd.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ene-syncd.service
```

The mode is set via the `ExecStart=` line in the unit file. **Do not enable
both units at the same time - pick one.** Before manual commands or OpenRGB,
stop the daemon: `sudo systemctl stop ene-syncd.service`.

## OpenRGB compatibility

On the verified firmware the modules are detected by the standard OpenRGB
detector and are visible to a stock build. The GUI offers modes and painting
each zone with the mouse; there are no brightness/speed sliders, no "pure"
rainbow, and no module sync in OpenRGB - for that, `ene.py` remains the tool
(the `daemon` covers sync). Do not run both tools at the same time: they
control the same lighting, and the last writer wins.

---

## Troubleshooting

- List all devices on the bus: `sudo i2cdetect -y 0`
- Full module information (chip ID, firmware, vendor, zones, color):
  `sudo ene.py info`

## Safety notes

- The script only talks to the lighting controllers; the module's data
  (SPD EEPROM) and other devices on the bus are not touched.
- Do not change settings whose purpose you do not know.
- On unknown devices - read-only operations only (`scan`, `info`).
