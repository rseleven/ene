#!/usr/bin/env python3
"""
ene.py - control the lighting of Ballistix RGB memory modules from GNU/Linux.

STDLIB ONLY (fcntl/ctypes), root, no external dependencies.

================================================================================
HOW IT WORKS (OVERVIEW)
================================================================================

Ballistix RGB is DDR4 memory with 16 LEDs along the module. The LEDs are
driven by a small ENE controller right on the module; the script talks to
it over the system bus.

Every module has ITS OWN controller at its own address on the SMBus
(a variety of I2C; on GNU/Linux it shows up as /dev/i2c-0). On a machine
with two modules the controllers usually sit at addresses 0x20 and 0x22;
the script finds them by itself (scan) - no need to look the addresses up.

Settings do not survive a reboot: after the PC is powered on, the mode has
to be set again (once, or via a systemd unit, see below).

"Zones": the 16 LEDs are addressed in pairs, giving 8 ZONES per module -
segments along the module that can be colored individually.
The zone mask is 8 bits:
  0x01 = zone 1, 0x02 = zone 2, 0x04 = zone 3 ... 0x80 = zone 8, 0xFF = all.
Different colors on one module - two color calls with different masks.

Synchronization: each controller has its own internal clock, so animations
(rainbow, wave) on several modules gradually drift apart without sync.
The daemon sends the controllers a clock tick every ~110 ms - the daemon
mode does exactly that (see daemon below). A static color and turning the
lights off need no synchronization.

================================================================================
COMMANDS
================================================================================
Without --addr a command applies to ALL found modules at once.
With --addr 0x20 --addr 0x22 (or --addr 0x20,0x22) - only to the listed
ones (e.g. two out of four modules).

One-shot commands (run and exit; the settings stay in the controller until
the PC is powered off):

    sudo ./ene.py scan                    # find modules and show addresses
    sudo ./ene.py off                     # TURN OFF the lighting of all modules
    sudo ./ene.py on 255                  # brightness 0-255 (255 = max, 0 = off)
    sudo ./ene.py color 255 0 0           # color R G B (0-255 each) on all zones
    sudo ./ene.py color 255 0 0 0x55      # same, but only the zones by mask 0x55
    sudo ./ene.py brightness 128          # alias of on
    sudo ./ene.py speed 16                # animation speed (0-255)
    sudo ./ene.py pattern static 255 0 0  # simple modes: static / breathing /
                                          #   flashing (the third argument is the color)
    sudo ./ene.py std-pat wave 0 0 255    # stock effects: shift, gradient,
                                          #   fill, stack, double-stack, breathing,
                                          #   motion, inside-out, color-step,
                                          #   wave, flashing, static (+ color);
                                          #   "rainbow" = alias for shift
                                          #   without a color
    sudo ./ene.py rainbow                 # smooth rainbow (color-cycling wave)
    sudo ./ene.py timing 0xFF 500 200     # on 500 ms / off 200 ms (blinking)
    sudo ./ene.py sync on                 # enable module sync + a clock tick
    sudo ./ene.py info                    # everything known about the module:
                                          #   chip ID, firmware, vendor, zones, color

Daemon mode (setup + sync ticks every ~110 ms; needed only for animations
on SEVERAL modules in phase; for turning the lights off or a static color
the daemon is not needed - use the one-shot off / color):

    sudo ./ene.py daemon                  # no arguments = rainbow + sync
    sudo ./ene.py daemon --rainbow --brightness 128
    sudo ./ene.py daemon --pattern breathing --color 0 0 255
    sudo ./ene.py daemon --color 255 0 0
    Ctrl+C stops it; modules disappearing/reappearing are handled on their own.

================================================================================
START AT BOOT (systemd)
================================================================================
Option 1 - apply a mode once at boot (off/color/brightness; no sync needed,
file ene-off.service):

    sudo cp ene.py /usr/local/bin/ene.py
    sudo cp ene-off.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable ene-off.service

Option 2 - daemon with sync (for animations on all modules,
file ene-syncd.service):

    sudo cp ene.py /usr/local/bin/ene.py
    sudo cp ene-syncd.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now ene-syncd.service

The mode is changed via the ExecStart= line in the unit file. Do not enable
BOTH options at the same time - pick one.

================================================================================
TROUBLESHOOTING
================================================================================
- List all devices on the bus: sudo i2cdetect -y 0
"""
import argparse
import ctypes
import fcntl
import os
import signal
import sys
import time

# --- mini-driver for i2c-dev (stdlib only) -----------------------------------
I2C_SLAVE       = 0x0703
I2C_SMBUS       = 0x0720
SMBUS_WRITE     = 0
SMBUS_READ      = 1
SMBUS_BYTE_DATA = 2
SMBUS_WORD_DATA = 3


class _SmbusData(ctypes.Union):
    _fields_ = [("byte", ctypes.c_uint8),
                ("word", ctypes.c_uint16),
                ("block", ctypes.c_uint8 * 34)]


class _SmbusIoctlData(ctypes.Structure):
    _fields_ = [("read_write", ctypes.c_uint8),
                ("command", ctypes.c_uint8),
                ("size", ctypes.c_uint32),
                ("data", ctypes.POINTER(_SmbusData))]


class SMBus:
    """SMBus via ioctl on /dev/i2c-N (minimal dependency-free wrapper)."""

    def __init__(self, n):
        self.fd = os.open(f"/dev/i2c-{n}", os.O_RDWR)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def _xfer(self, addr, rw, cmd, size, wval=None):
        fcntl.ioctl(self.fd, I2C_SLAVE, addr)
        data = _SmbusData()
        if rw == SMBUS_WRITE:
            data.word = (wval or 0) & 0xFFFF
        arg = _SmbusIoctlData(rw, cmd, size, ctypes.pointer(data))
        fcntl.ioctl(self.fd, I2C_SMBUS, arg)
        return data.word if rw == SMBUS_READ else None

    def write_word_data(self, addr, cmd, val):
        self._xfer(addr, SMBUS_WRITE, cmd, SMBUS_WORD_DATA, val & 0xFFFF)

    def write_byte_data(self, addr, cmd, val):
        self._xfer(addr, SMBUS_WRITE, cmd, SMBUS_BYTE_DATA, val & 0xFF)

    def read_byte_data(self, addr, cmd):
        return self._xfer(addr, SMBUS_READ, cmd, SMBUS_BYTE_DATA) & 0xFF

    def read_word_data(self, addr, cmd):
        return self._xfer(addr, SMBUS_READ, cmd, SMBUS_WORD_DATA) & 0xFFFF


# --- ENE / Crucial protocol --------------------------------------------------
PORT_PTR       = 0x00   # word: register pointer (bytes swapped, see _ptr_val)
PORT_DATA      = 0x01   # byte: write the value
PORT_READ      = 0x81   # byte: read the selected register
PORT_READ_WORD = 0x82   # word: read a word (chip ID after selecting R_ID)

R_ID            = 0xF01C  # chip ID (word-read): the ENE Gen2 ID 0x6802,
                          #                      stored big-endian (a raw
                          #                      word-read returns 0x0268)
R_ID_CRUCIAL    = 0x1CF0  # chip ID (word-read): 0x0634 = Ballistix RGB
R_UP_ZONEMASK   = 0x820D  # zone mask for a user pattern
R_UP_PATTERN    = 0x820E  # user pattern code (see USER_PATTERNS)
R_UP_CMD        = 0x820F  # 2 = apply user, 8 = stop standard; or
                          #     standard code | 0x03/0x07 | 0x08 (see set_std_pattern)
R_STD_READ      = 0x8210  # current standard pattern (not used by the script)
R_ZONE_MASK     = 0x82E9  # zone mask for the color
# 0x82ED/EE/EF are "multi-purpose": in set_color they carry the color, in
# brightness/speed - the zone and the value, in set_timing - the zone and
# on/off (ms / 10).
R_RED           = 0x82ED
R_GREEN         = 0x82EE
R_BLUE          = 0x82EF
R_ZONE          = R_GREEN  # alias: the zone for set_brightness/set_speed
R_VALUE         = R_BLUE   # alias: the value for set_brightness/set_speed
R_ON_OFF        = R_GREEN  # alias: on (ms/10) in set_timing; off goes via R_VALUE
R_CMD           = 0x82F0  # command: 1=color 3=brightness 4=speed 5=timing 8=zones;
                          #          |0x80 = apply now; 0x8A = host sync
R_COLOR_R       = 0x82F1
R_COLOR_G       = 0x82F2
R_COLOR_B       = 0x82F3
R_SYNC_STATUS   = 0x8086  # != 0 - a Sync Clock tick is not required
R_SYNC_CMD      = 0x83DF  # writing 1 = one Sync Clock tick
R_NUM_ZONES     = 0x8500  # zone count: reads 0 until the host configures
                          # it (the info output then shows the verified 8)

CMD_APPLY_COLOR      = 0x01
CMD_APPLY_BRIGHT     = 0x03
CMD_APPLY_BRIGHT_NOW = 0x83
CMD_APPLY_SPEED      = 0x04
CMD_APPLY_SPEED_NOW  = 0x84
CMD_APPLY_TIMING     = 0x05
CMD_CONFIG_ZONES     = 0x08
CMD_HOST_SYNC        = 0x8A

USER_PATTERNS = {"static": 199, "breathing": 103, "flashing": 183}
STD_PATTERNS = {                     # base codes; written as code | 0x07 | 0x08
    "shift": 0x10, "gradient": 0x20, "fill": 0x30, "stack": 0x40,
    "double-stack": 0x50, "breathing": 0x60, "motion": 0x70,
    "inside-out": 0x80, "color-step": 0x90, "wave": 0xA0,
    "flashing": 0xB0, "static": 0xC0,
}
ALL_ZONES   = 0xFF   # mask for "all 8 zones"
DEVICE_POOL = [0x20, 0x21, 0x22, 0x23, 0x39, 0x3A, 0x3B, 0x3C,
               0x27, 0x40] + list(range(0x72, 0x7F, 2))  # addresses to probe


class EneDimm:
    """One Ballistix RGB module: low level, modes, diagnostics."""

    def __init__(self, busno, dev):
        self.busno = busno
        self.dev = dev
        self.bus = SMBus(busno)
        # Pointer byte order: swapped (((reg<<8)&0xFF00)|((reg>>8)&0xFF));
        # verified by reading 0xA5 (== 0x05, because registers 0xA0-0xAF hold
        # 00..0F). Swapped is the default.
        self.swap = True
        for s in (True, False):
            try:
                if self.read_reg(0xA5, swap=s) == 0x05:
                    self.swap = s
                    break
            except OSError:
                pass

    def close(self):
        try:
            self.bus.close()
        except OSError:
            pass

    # low level --------------------------------------------
    @staticmethod
    def _ptr_val(reg, swap):
        if swap:
            return ((reg << 8) & 0xFF00) | ((reg >> 8) & 0xFF)
        return reg & 0xFFFF

    def _select(self, reg, swap=None):
        if swap is None:
            swap = self.swap
        self.bus.write_word_data(self.dev, PORT_PTR, self._ptr_val(reg, swap))

    def write_reg(self, reg, value):
        self._select(reg)
        self.bus.write_byte_data(self.dev, PORT_DATA, value & 0xFF)

    def read_reg(self, reg, swap=None):
        self._select(reg, swap)
        return self.bus.read_byte_data(self.dev, PORT_READ) & 0xFF

    # module discovery -------------------------------------
    @staticmethod
    def scan(busno, candidates=None):
        """Probe addresses: read the chip ID with both pointer byte orders."""
        if candidates is None:
            candidates = DEVICE_POOL
        found = []
        with SMBus(busno) as b:
            for addr in candidates:
                try:
                    val = None
                    for swap in (True, False):
                        b.write_word_data(addr, PORT_PTR,
                                          EneDimm._ptr_val(R_ID, swap))
                        time.sleep(0.002)
                        v = b.read_word_data(addr, PORT_READ_WORD) & 0xFFFF
                        if v in (0x6802, 0x0634):
                            val = v
                            break
                        if val is None:
                            val = v
                    tag = ""
                    if val == 0x6802:
                        tag = "  <- ENE Gen2"
                    elif val == 0x0634:
                        tag = "  <- Crucial ENE (Ballistix RGB)"
                    print(f"  0x{addr:02X}: ID=0x{val:04X}{tag}")
                    if val in (0x6802, 0x0634):
                        found.append(addr)
                except OSError as e:
                    print(f"  0x{addr:02X}: {e.strerror or e}")
                    continue
        return found

    # high-level operations --------------------------------
    def set_color(self, r, g, b, zone=ALL_ZONES):
        self.write_reg(R_ZONE_MASK, zone)
        self.write_reg(0x82EA, 0)
        self.write_reg(0x82EB, 0)
        self.write_reg(0x82EC, 0)
        self.write_reg(R_RED, r)
        self.write_reg(R_GREEN, g)
        self.write_reg(R_BLUE, b)
        self.write_reg(R_CMD, CMD_APPLY_COLOR)
        time.sleep(0.02)

    def get_color(self):
        return (self.read_reg(R_COLOR_R),
                self.read_reg(R_COLOR_G),
                self.read_reg(R_COLOR_B))

    def set_brightness(self, val, zone=ALL_ZONES, now=True):
        self.write_reg(R_ZONE, zone)
        self.write_reg(R_VALUE, val)
        self.write_reg(R_CMD, CMD_APPLY_BRIGHT_NOW if now else CMD_APPLY_BRIGHT)
        time.sleep(0.02)

    def set_speed(self, val, zone=ALL_ZONES, now=True):
        self.write_reg(R_ZONE, zone)
        self.write_reg(R_VALUE, val)
        self.write_reg(R_CMD, CMD_APPLY_SPEED_NOW if now else CMD_APPLY_SPEED)
        time.sleep(0.02)

    def set_timing(self, zone, on_ms, off_ms):
        self.write_reg(R_RED, zone)
        self.write_reg(R_ON_OFF, max(0, on_ms // 10))
        self.write_reg(R_VALUE, max(0, off_ms // 10))
        self.write_reg(R_CMD, CMD_APPLY_TIMING)
        time.sleep(0.02)

    def set_user_pattern(self, code, zone=ALL_ZONES):
        """User pattern: zone mask + code + the "apply" command (see USER_PATTERNS)."""
        self.write_reg(R_UP_ZONEMASK, zone)
        self.write_reg(R_UP_PATTERN, code)
        self.write_reg(R_UP_CMD, 2)
        time.sleep(0.02)

    def set_std_pattern(self, name, r=None, g=None, b=None):
        code = STD_PATTERNS[name]
        self.write_reg(R_UP_CMD, 8)          # stop the standard pattern
        time.sleep(0.02)
        self.write_reg(R_BLUE, 0)            # reset the zone selection
        self.write_reg(R_CMD, CMD_CONFIG_ZONES)
        time.sleep(0.02)
        if r is None:
            self.write_reg(R_UP_CMD, code | 0x03 | 0x08)  # rainbow
        else:
            self.set_color(r, g, b)
            self.write_reg(R_UP_CMD, code | 0x07 | 0x08)
        time.sleep(0.02)

    def set_host_sync(self, enabled):
        """Host sync: R_VALUE = the flag, R_CMD = 0x8A."""
        self.write_reg(R_VALUE, 1 if enabled else 0)
        self.write_reg(R_CMD, CMD_HOST_SYNC)
        time.sleep(0.02)

    def sync_clock(self):
        """One Sync Clock tick: if R_SYNC_STATUS == 0, send R_SYNC_CMD = 1."""
        if self.read_reg(R_SYNC_STATUS) != 0:
            return False
        self.write_reg(R_SYNC_CMD, 1)
        return True

    # diagnostics ------------------------------------------
    @staticmethod
    def _strings(bs):
        """bytes -> list of printable NUL-separated strings (FW date/version).

        A non-printable byte terminates the current string: the version
        block has no NUL after the text - binary data follows it."""
        out, cur = [], []
        for b in bs:
            if b == 0 or not 32 <= b < 127:
                if cur:
                    out.append("".join(cur))
                    cur = []
            else:
                cur.append(chr(b))
        if cur:
            out.append("".join(cur))
        return out

    def chip_id(self):
        """Chip ID: 0x0634 = Ballistix RGB, 0x6802 = ENE Gen2.

        The controller exposes the ID in two registers: 0x1CF0 holds the
        Crucial ID 0x0634, 0xF01C holds the Gen2 ID 0x6802 big-endian
        (a raw word-read there returns 0x0268 - observed on real
        hardware). Both are read and the Crucial ID wins if seen, so the
        result does not depend on the pointer byte order. Each word read
        gets a short delay after the pointer write (as in scan())."""
        raw = last_gen2 = 0
        for reg in (R_ID_CRUCIAL, R_ID):
            self._select(reg)
            time.sleep(0.002)
            raw = self.bus.read_word_data(self.dev, PORT_READ_WORD) & 0xFFFF
            if raw == 0x0268:
                raw = 0x6802          # 0xF01C stores the ID big-endian
            if raw == 0x0634:
                return raw
            if raw == 0x6802:
                last_gen2 = raw
        return last_gen2 if last_gen2 else raw

    def vendor(self):
        """DRAM vendor: the "Micron\\0" string at 0x1025/0x1030, or None."""
        for reg in (0x1025, 0x1030):
            bs = bytes(self.read_block(reg, 7))
            if bs[:6] == b"Micron" and bs[6] == 0:
                return "Micron"
        return None

    def read_block(self, reg, n):
        return [self.read_reg(reg + i) for i in range(n)]

    def num_zones(self):
        return self.read_reg(R_NUM_ZONES)

    def info(self):
        """Multi-line summary: everything known about the module."""
        r, g, b = self.get_color()
        strs = self._strings(self.read_block(0x1000, 32))
        ver = self._strings(self.read_block(0x1030, 32))
        if ver:
            strs.append(ver[0])   # the version block: binary data follows
                                  # the text - only the first string is one
        mic = self.vendor()
        zones = self.num_zones()
        cid = self.chip_id()
        lines = [
            f"Module 0x{self.dev:02X}:",
            f"  Chip ID:      0x{cid:04X}",
            "  FW strings:   " + (" | ".join(strs) if strs else "-"),
            f"  DRAM Manuf.:  {mic if mic else 'not detected'}",
            # the zone-count register reads 0 until the host configures it
            f"  zones:        {zones if zones else '8 (register reads 0)'}",
            f"  RGB color:    R={r} G={g} B={b}",
        ]
        return "\n".join(lines)


# --- shared helpers ----------------------------------------------------------
def find_dimms(busno, addr=None):
    """[EneDimm]: modules found by the 0xA0-0xAF == 00..0F signature,
    or the ones listed in --addr (a list of addresses)."""
    if addr is not None:
        return [EneDimm(busno, a) for a in addr]
    out = []
    with SMBus(busno) as bus:
        for dev in DEVICE_POOL:
            try:
                if [bus.read_byte_data(dev, i) for i in range(0xA0, 0xB0)] == list(range(16)):
                    out.append(EneDimm(busno, dev))
            except OSError:
                continue
    return out


def for_each(args, fn):
    """Apply fn(dimm) to every found (or --addr) module; True if any succeeded."""
    try:
        dimms = find_dimms(args.bus, args.addr)
    except OSError as e:
        print(f"cannot open i2c bus {args.bus}: {e}")
        return False
    if not dimms:
        print("No Ballistix RGB modules found (try: ene.py scan)")
        return False
    ok = False
    for d in dimms:
        try:
            fn(d)
            ok = True
        except OSError as e:
            print(f"0x{d.dev:02X}: error: {e.strerror or e}")
        finally:
            d.close()
    return ok


def daemon(busno, opts, ticks=None):
    """Set up the modules and keep sending sync ticks every ~110 ms.

    Module events (found / lost / rejoined, the list it works with) are
    logged to stdout/stderr with timestamps - under systemd they land in
    the journal (journalctl -u ene-syncd).

    Modules that disappear are rescanned every ~5 s (a rejoined module
    gets its mode applied again), a lost bus does not crash the process;
    SIGTERM/SIGINT/SIGHUP/SIGQUIT - graceful exit."""
    TICK_SEC, RESCAN_SEC = 0.110, 5.0  # ~110 ms tick, ~5 s rescan
    running = [True]

    def log(msg):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    def stop(signum, _frame):
        # on a signal Python retries the syscalls (ioctl/read) itself
        # (PEP 475), so the current I2C transaction finishes whole; we
        # exit right after it.
        running[0] = False
        print(f"\nsignal {signum} received, exiting...", file=sys.stderr, flush=True)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        try:
            signal.signal(sig, stop)
        except (ValueError, OSError):  # not available in every environment
            pass

    def setup():
        out = []
        for d in find_dimms(busno, opts.addr):
            try:
                # host sync first: it writes R_VALUE - a register shared
                # with brightness
                d.set_host_sync(True)
                d.sync_clock()
                apply_opts(d, opts)
                log(f"0x{d.dev:02X}: mode applied")
                out.append(d)
            except OSError:
                d.close()
        if out:
            log(f"working with {len(out)} module(s): "
                + ", ".join(f"0x{d.dev:02X}" for d in out))
        return out

    dimms = None
    try:
        dimms = setup()
    except OSError as e:
        sys.exit(f"cannot open i2c bus {busno}: {e}")
    if not dimms:
        sys.exit("No Ballistix RGB modules found")

    n = 0
    try:
        while running[0] and (ticks is None or n < ticks):
            time.sleep(TICK_SEC)
            alive = []
            for d in dimms:
                try:
                    d.sync_clock()
                    alive.append(d)
                except OSError:
                    d.close()
                    log(f"0x{d.dev:02X}: module lost, removed from sync")
            dimms = alive
            n += 1
            if not dimms or n % int(RESCAN_SEC / TICK_SEC) == 0:
                try:
                    have = {d.dev for d in dimms}
                    for d in find_dimms(busno, opts.addr):
                        if d.dev in have:
                            d.close()  # already tracked - close the probe handle
                            continue
                        try:
                            # a rejoined module starts with factory
                            # settings - apply the mode again
                            d.set_host_sync(True)
                            d.sync_clock()
                            apply_opts(d, opts)
                            log(f"0x{d.dev:02X}: module found, mode applied")
                            dimms.append(d)
                        except OSError:
                            d.close()
                except OSError:
                    pass  # the bus is gone - retry later
                if not dimms:
                    log("no modules found, waiting...")
    finally:
        for d in dimms:
            try:
                d.close()
            except OSError:
                pass
    log("stopped")


def apply_opts(d, o):
    """Apply a Namespace (off/color/pattern/rainbow/brightness/speed).

    Brightness and speed are applied only when set (>0 / not None) -
    zero brightness without an explicit off means "leave it as is"."""
    if o.off or (o.brightness == 0 and not o.color and not o.pattern and not o.rainbow):
        d.set_brightness(0, ALL_ZONES, now=True)
        return
    if o.pattern:
        name, rgb = o.pattern.lower(), o.color
        code = USER_PATTERNS.get(name)
        if code:
            d.set_user_pattern(code)
            if rgb:
                d.set_color(*rgb)
        else:
            (d.set_std_pattern(name, *rgb) if rgb else d.set_std_pattern(name))
    elif o.rainbow:
        d.set_std_pattern("shift", None)     # Water Wave without color = rainbow
    elif o.color:
        d.set_color(*o.color)
    if o.brightness and o.brightness > 0:
        d.set_brightness(o.brightness, ALL_ZONES, now=True)
    if o.speed is not None:
        d.set_speed(o.speed, ALL_ZONES, now=True)


# --- CLI ---------------------------------------------------------------------
def byte_arg(s):
    """Integer 0-255 (dec/hex); garbage gives a clear error, not a traceback."""
    try:
        v = int(s, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number 0-255, got: {s!r}")
    if not 0 <= v <= 255:
        raise argparse.ArgumentTypeError(f"value {v} out of range 0-255")
    return v


def ms_arg(s):
    """Milliseconds 0-2550 (the register holds ms/10 in a single byte)."""
    try:
        v = int(s, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected milliseconds 0-2550, got: {s!r}")
    if not 0 <= v <= 2550:
        raise argparse.ArgumentTypeError(f"{v} ms out of range 0-2550 (register limit)")
    return v


PATTERN_NAMES = sorted(set(USER_PATTERNS) | set(STD_PATTERNS))


def addr_list(s):
    """One address or a comma-separated list: '0x20' / '0x20,0x22' (dec/hex)."""
    return [byte_arg(x.strip()) for x in s.split(",")]


def bus_arg(s):
    """i2c bus number: an integer >= 0; garbage gives a clear error."""
    try:
        v = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"the bus number must be an integer, got: {s!r}")
    if v < 0:
        raise argparse.ArgumentTypeError(f"the bus number cannot be negative: {v}")
    return v


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bus", type=bus_arg, default=0,
                   help="i2c bus number (default 0)")
    p.add_argument("--addr", type=addr_list, action="append", default=None,
                   metavar="ADDR",
                   help="module address(es): 0x20 or 0x20,0x22 (the flag may "
                        "be repeated); default - all found")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    for name in ("off", "info", "rainbow", "sync", "daemon"):
        sp = sub.add_parser(name)
        if name == "sync":
            sp.add_argument("state", nargs="?", choices=("on", "off", "1", "0",
                                                         "yes", "no"),
                            default="on")
        if name == "daemon":
            sp.description = ("apply a mode and keep the sync ticks going "
                              "every ~110 ms (only needed for animations on "
                              "several modules); no arguments = rainbow; "
                              "for off/static color use the one-shot off/color")
            sp.add_argument("--off", action="store_true")
            sp.add_argument("--color", type=byte_arg, nargs=3, metavar=("R", "G", "B"))
            sp.add_argument("--pattern", choices=PATTERN_NAMES)
            sp.add_argument("--rainbow", action="store_true")
            sp.add_argument("--brightness", type=byte_arg, default=0)
            sp.add_argument("--speed", type=byte_arg, default=None)
            sp.add_argument("--ticks", type=int, default=None,
                            help=argparse.SUPPRESS)  # for tests: N ticks and exit
    sp = sub.add_parser("on");        sp.add_argument("value", type=byte_arg)
    sp = sub.add_parser("brightness"); sp.add_argument("value", type=byte_arg)
    sp = sub.add_parser("speed");     sp.add_argument("value", type=byte_arg)
    sp = sub.add_parser("color")
    sp.add_argument("rgb", nargs=3, type=byte_arg, metavar=("R", "G", "B"))
    sp.add_argument("mask", nargs="?", type=byte_arg, default=ALL_ZONES)
    sp = sub.add_parser("pattern")
    sp.add_argument("name", choices=sorted(USER_PATTERNS))
    sp.add_argument("rgb", nargs="*", type=byte_arg)
    sp = sub.add_parser("std-pat")
    sp.add_argument("name", choices=sorted(STD_PATTERNS) + ["rainbow"])
    sp.add_argument("rgb", nargs="*", type=byte_arg)
    sp = sub.add_parser("timing")
    sp.add_argument("zone", type=byte_arg)
    sp.add_argument("on_ms", type=ms_arg); sp.add_argument("off_ms", type=ms_arg)

    a = p.parse_args()

    # --addr is collected as a list of lists (the flag repeats) - flatten it
    if a.addr is not None:
        a.addr = [x for lst in a.addr for x in lst]

    # the color arguments of pattern/std-pat: either none or exactly R G B -
    # otherwise the values would silently spill into zone/None in the
    # method signatures
    if a.cmd in ("pattern", "std-pat") and a.rgb and len(a.rgb) != 3:
        p.error(f"{a.cmd}: expected 0 or 3 numbers (R G B), got {len(a.rgb)}")

    # std-pat rainbow = the shift pattern without a fixed color; a color
    # would turn it back into a plain wave
    if a.cmd == "std-pat" and a.name == "rainbow" and a.rgb:
        p.error("std-pat rainbow takes no color - the hues are cycled by the "
                "controller itself (use: std-pat shift R G B for a fixed color)")

    # daemon with no MODE arguments (off/color/pattern/rainbow) = rainbow;
    # brightness/speed are adjustments applied on top of the mode
    if a.cmd == "daemon" and not (a.off or a.color or a.pattern or a.rainbow):
        a.rainbow = True

    if a.cmd == "color" and a.mask == 0:
        p.error("color: the zone mask is 0 - no zones selected")
    if a.cmd == "timing" and a.zone == 0:
        p.error("timing: the zone mask is 0 - no zones selected")

    if a.cmd == "scan":
        try:
            found = EneDimm.scan(a.bus)
        except OSError as e:
            sys.exit(f"cannot open i2c bus {a.bus}: {e}")
        print("ENE found (Ballistix RGB):", [hex(x) for x in found])
        return
    if a.cmd == "daemon":
        daemon(a.bus, a, a.ticks)
        return
    if a.cmd == "off":
        ok = for_each(a, lambda d: d.set_brightness(0, ALL_ZONES, now=True))
    elif a.cmd == "on" or a.cmd == "brightness":
        ok = for_each(a, lambda d: d.set_brightness(a.value, ALL_ZONES, now=True))
    elif a.cmd == "color":
        ok = for_each(a, lambda d: d.set_color(*a.rgb, a.mask))
    elif a.cmd == "speed":
        ok = for_each(a, lambda d: d.set_speed(a.value, ALL_ZONES, now=True))
    elif a.cmd == "pattern":
        ok = for_each(a, lambda d: (d.set_user_pattern(USER_PATTERNS[a.name]),
                                    d.set_color(*a.rgb) if a.rgb else None))
    elif a.cmd == "std-pat":
        if a.name == "rainbow":              # alias: shift without a color
            ok = for_each(a, lambda d: d.set_std_pattern("shift", None))
        else:
            ok = for_each(a, lambda d: (d.set_std_pattern(a.name, *a.rgb) if a.rgb
                                        else d.set_std_pattern(a.name)))
    elif a.cmd == "rainbow":
        ok = for_each(a, lambda d: d.set_std_pattern("shift", None))
    elif a.cmd == "timing":
        ok = for_each(a, lambda d: d.set_timing(a.zone, a.on_ms, a.off_ms))
    elif a.cmd == "sync":
        en = a.state.lower() not in ("off", "0", "no")
        def fn(d):
            d.set_host_sync(en)
            if en:
                print(f"  0x{d.dev:02X}: sync-clock:",
                      "tick sent" if d.sync_clock() else "0x8086 != 0 - not required")
            else:
                print(f"  0x{d.dev:02X}: host sync disabled")
        ok = for_each(a, fn)
    elif a.cmd == "info":
        ok = for_each(a, lambda d: print(d.info()))
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
