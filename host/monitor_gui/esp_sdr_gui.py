#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
esp_sdr_gui.py - ESP-SDR debug tool for the three-mode firmware
(SDR / WiFi Monitor / BLE Monitor) on ESP32-C5.

Stdlib + pyserial only (no numpy/matplotlib needed: the SDR spectrum is the
firmware's on-chip FFT streamed via SPEC; waterfalls render through tkinter
PhotoImage).

Run:  python esp_sdr_gui.py
"""
import ctypes
import os
import queue
import struct
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import sdrsharp_bridge as SDRSHARP_BRIDGE
except Exception:            # 缺文件时只影响桥接模式
    SDRSHARP_BRIDGE = None

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("pyserial is required:  pip install pyserial")
    raise

# --------------------------------------------------------------------------
# Protocol constants
# --------------------------------------------------------------------------
MAGIC_SPC1 = b"SPC1"
SPC1_HEADER = 28  # magic4 frames4 index8 nbins4 merged2 flags1 gain1 zero2 log2n1 type1
LINKTYPE_WIFI = 127        # LINKTYPE_IEEE802_11_RADIO (Radiotap included in payload)
LINKTYPE_BLE = 256         # LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR
BLE_ADV_AA = 0x8E89BED6
F_DEWHITENED = 0x0001
F_REF_AA_VALID = 0x0010
F_AA_OFFENSES_VALID = 0x0020
F_CRC_CHECKED = 0x0400
F_CRC_VALID = 0x0800

CANDIDATE_VIDS = {0x303A: "Espressif USB-Serial-JTAG", 0x10C4: "CP210x UART",
                  0x1A86: "CH340/CH341 UART", 0x0403: "FTDI UART"}
USB_SJTAG_VID = 0x303A

BINARY_HEADERS = ("WIFIFRAME ", "BLEFRAME ", "DATA ", "IQFRAME ")

WIFI_2G_CHANNELS = list(range(1, 14))
WIFI_5G_CHANNELS = [36, 40, 44, 48, 52, 56, 60, 64,
                    100, 104, 108, 112, 116, 120, 124, 128,
                    132, 136, 140, 144, 149, 153, 157, 161, 165]

# --------------------------------------------------------------------------
# pcap sinks: classic pcap stream to a file and/or a Windows named pipe
# --------------------------------------------------------------------------
class PcapStream:
    """Writes one classic-pcap stream to every attached sink (file/pipe)."""

    def __init__(self, linktype):
        self.linktype = linktype
        self.sinks = []
        self._lock = threading.Lock()

    def add_file(self, path):
        f = open(path, "wb")
        f.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, self.linktype))
        f.flush()
        with self._lock:
            self.sinks.append(f)
        return f

    def add_pipe_sink(self, pipe_sink):
        pipe_sink.write_pcap_header(self.linktype)
        with self._lock:
            self.sinks.append(pipe_sink)

    def record(self, payload):
        sec = int(time.time())
        pkt = struct.pack("<IIII", sec, 0, len(payload), len(payload)) + payload
        with self._lock:
            dead = []
            for s in self.sinks:
                try:
                    s.write(pkt)
                    s.flush()
                except Exception:
                    dead.append(s)
            for s in dead:
                self.sinks.remove(s)

    def close(self):
        with self._lock:
            for s in self.sinks:
                try:
                    s.close()
                except Exception:
                    pass
            self.sinks.clear()


class NamedPipeSink:
    """Blocking Windows named-pipe server; Wireshark reads the pcap stream
    with:  wireshark -k -i \\\\.\\pipe\\<name>"""

    def __init__(self, name, log):
        self.name = name
        self.log = log
        self.handle = None
        self._write_lock = threading.Lock()
        self._pending = bytearray()
        self.alive = True
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        k32 = ctypes.windll.kernel32
        path = r"\\.\pipe\%s" % self.name
        while self.alive:
            h = k32.CreateNamedPipeW(path, 0x00000002, 0x00000004, 1, 65536, 0, 0, None)
            if h == -1 or h is None:
                self.log("管道创建失败: %s" % path)
                return
            self.log("等待 Wireshark 连接 %s ..." % path)
            ok = k32.ConnectNamedPipe(h, None)
            if not ok and ctypes.GetLastError() == 535:  # ERROR_PIPE_CONNECTED
                ok = True
            if not ok:
                ctypes.windll.kernel32.CloseHandle(h)
                continue
            self.log("Wireshark 已连接 %s" % path)
            self.handle = h
            with self._write_lock:
                if self._pending:
                    self._raw_write(bytes(self._pending))
                    self._pending.clear()
            # Wait until the pipe breaks or we are closed.
            while self.alive and self.handle is h:
                time.sleep(0.5)
            try:
                k32.FlushFileBuffers(h)
                k32.DisconnectNamedPipe(h)
                k32.CloseHandle(h)
            except Exception:
                pass
            self.handle = None

    def _raw_write(self, data):
        if self.handle is None:
            return
        written = ctypes.c_ulong(0)
        buf = ctypes.create_string_buffer(data, len(data))
        ok = ctypes.windll.kernel32.WriteFile(self.handle, buf, len(data),
                                              ctypes.byref(written), None)
        if not ok:
            self.handle = None

    def write_pcap_header(self, linktype):
        self.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, linktype))

    def write(self, data):
        with self._write_lock:
            if self.handle is None:
                self._pending.extend(data)
                if len(self._pending) > 1 << 20:
                    del self._pending[:1 << 19]
                return
            self._raw_write(data)

    def flush(self):
        pass

    def close(self):
        self.alive = False
        self.handle = None


# --------------------------------------------------------------------------
# Serial link with a single reader thread: synchronous commands + async frames
# --------------------------------------------------------------------------
class Link(threading.Thread):
    def __init__(self, port, on_event, log, baudrate=115200, data_port=None, data_baudrate=None):
        super().__init__(daemon=True)
        self.port = port
        self.baudrate = baudrate
        self.data_port = data_port or port
        self.data_baudrate = data_baudrate or baudrate
        self.on_event = on_event   # callable(kind, *args)
        self.log = log
        self.ser = None            # command (TX) side
        self.ser_rx = None         # data (RX) side
        self.alive = True
        self.buf = bytearray()
        self.spec_active = False
        self.data_wire_fn = None   # set from the outstanding CAP format
        self._cmd_lock = threading.Lock()
        self._cmd_pending = None
        self._cmd_done = threading.Event()
        self._cmd_reply = None

    def _open_one(self, port, baud):
        s = serial.Serial()
        s.port = port
        s.baudrate = baud
        s.timeout = 0.1
        s.dtr = False
        s.rts = False
        s.open()
        s.setDTR(False)
        s.setRTS(False)
        try:
            s.set_buffer_size(rx_size=65536, tx_size=4096)
        except Exception:
            pass
        time.sleep(0.2)
        s.reset_input_buffer()
        return s

    def open(self):
        last = None
        for attempt in range(4):
            try:
                self.ser = self._open_one(self.port, self.baudrate)     # command TX
                break
            except Exception as e:
                last = e
                time.sleep(1.0 + attempt)
        else:
            raise last
        self.ser_cmd = self.ser
        if self.data_port != self.port:
            self.ser_data = self._open_one(self.data_port, self.data_baudrate)
        else:
            self.ser_data = self.ser
        # Replies follow the command port until OUTPUT pins the data port.
        self.ser_rx = self.ser_cmd

    def activate_data_port(self):
        """Called once the OUTPUT pin has been ACKed: from here on every reply
        and frame arrives on the data port."""
        self.buf = bytearray()
        self.ser_rx = self.ser_data

    def close(self):
        self.alive = False
        seen = set()
        for s in (self.ser, self.ser_data):
            try:
                if s is not None and id(s) not in seen:
                    seen.add(id(s))
                    s.close()
            except Exception:
                pass

    def command(self, cmd, timeout=5.0):
        """Send a text command, return its one-line reply (async frames are
        dispatched to on_event while waiting)."""
        with self._cmd_lock:
            self._cmd_pending = cmd
            self._cmd_done.clear()
        if not self._cmd_done.wait(timeout):
            with self._cmd_lock:
                self._cmd_pending = None
            raise TimeoutError("timeout waiting reply for %r" % cmd)
        return self._cmd_reply

    def run(self):
        while self.alive:
            with self._cmd_lock:
                cmd = self._cmd_pending
            if cmd is not None:
                try:
                    self.ser.write(cmd.encode() + b"\n")
                    reply = self._wait_reply()
                except Exception as e:
                    reply = "ERR link: %s" % e
                with self._cmd_lock:
                    self._cmd_pending = None
                    self._cmd_reply = reply
                    self._cmd_done.set()
            else:
                try:
                    ev = self._read_event(0.15)
                except Exception as e:
                    if self.alive:
                        self.on_event("link_error", str(e))
                    time.sleep(0.5)
                    continue
                if ev:
                    self.on_event(*ev)

    def _fill(self, want, deadline):
        while len(self.buf) < want:
            if time.time() > deadline:
                return False
            chunk = self.ser_rx.read(max(64, want - len(self.buf)))
            if chunk:
                self.buf += chunk
        return True

    def _wait_reply(self):
        deadline = time.time() + 8.0
        while time.time() < deadline:
            ev = self._read_event(0.2, deadline)
            if ev is None:
                continue
            kind = ev[0]
            if kind in ("line",):
                return ev[1]
            self.on_event(*ev)
        return "ERR timeout"

    def _read_event(self, idle_timeout, deadline=None):
        """Parse one event out of the buffered stream."""
        if deadline is None:
            deadline = time.time() + idle_timeout
        while True:
            if self.spec_active:
                if len(self.buf) >= 4 and self.buf[:4] == MAGIC_SPC1:
                    if not self._fill(SPC1_HEADER + 4, deadline):
                        return None
                    # Field at offset 16 is n*merged (informational); the wire
                    # always carries exactly n = 1<<log2n power bytes plus CRC.
                    log2n = self.buf[26]
                    nbins = 1 << log2n
                    total = SPC1_HEADER + nbins + 4
                    fill_deadline = time.time() + 2.0
                    if not self._fill(total, fill_deadline):
                        self.buf.clear()
                        return None
                    frame = bytes(self.buf[:total])
                    del self.buf[:total]
                    det = frame[22] & 1
                    gain = frame[23]
                    bins = frame[SPC1_HEADER:SPC1_HEADER + nbins]
                    crc_ok = (zlib_crc32(frame[:-4]) == struct.unpack_from("<I", frame, total - 4)[0])
                    return ("spec", det, gain, log2n, bins, crc_ok)
                # SPEC stream should be all SPC1; fall through to line parse
                # (e.g. trailing text right after we sent the stop byte).
            nl = self.buf.find(b"\n")
            if nl >= 0:
                line = bytes(self.buf[:nl]).decode(errors="replace").strip()
                del self.buf[:nl + 1]
                if not line:
                    continue
                for head in BINARY_HEADERS:
                    if line.startswith(head):
                        parts = line.split()
                        n = int(parts[1])
                        # DATA wire size depends on the capture format the
                        # host requested (CAP16=2B/sample, CAP20=packed 10-bit,
                        # CAP=raw 32-bit words). The header carries only the
                        # sample count, so the format comes from data_wire_fn
                        # (set from the outstanding capture command); monitor
                        # frames carry an explicit byte length.
                        if parts[0] == "DATA":
                            length = self.data_wire_fn(n) if self.data_wire_fn else n * 2
                        else:
                            length = n
                        # Payloads may take far longer than the idle tick to
                        # arrive (host-driven pacing); give the fill its own
                        # generous deadline or large transfers misframe.
                        fill_deadline = time.time() + 1.0 + length / 8000.0
                        if not self._fill(length, fill_deadline):
                            # Truncated transfer (link drop): discard the
                            # partial payload so the stream can re-align on
                            # the next ASCII header instead of staying
                            # misframed forever.
                            self.buf.clear()
                            return None
                        payload = bytes(self.buf[:length])
                        del self.buf[:length]
                        return (parts[0].lower(), parts, payload)
                return ("line", line)
            if time.time() > deadline:
                return None
            chunk = self.ser_rx.read(4096)
            if chunk:
                self.buf += chunk
            else:
                if self.buf and time.time() + 0.05 > deadline:
                    return None


def zlib_crc32(data):
    import zlib
    return zlib.crc32(data) & 0xFFFFFFFF


def ble_crc24(data, init=0x555555):
    """BLE CRC-24 (poly 0x00065B), as transmitted on an advertising channel
    (pre-whitening). Needed because the LINKTYPE 256 record carries the
    on-air layout: pseudo-header + access address + PDU + CRC."""
    crc = init
    for b in data:
        crc ^= b << 16
        for _ in range(8):
            crc = ((crc << 1) ^ 0x00065B) & 0xFFFFFF if crc & 0x800000 \
                else (crc << 1) & 0xFFFFFF
    return crc


# --------------------------------------------------------------------------
# Port auto-detection
# --------------------------------------------------------------------------
def probe_ports(log):
    """Return [(port, label), ...] for ports that answer the burst protocol.
    Each port is probed in a daemon thread with a hard deadline: opening a
    stale COM device can block forever, and the GUI must not freeze."""
    found = []
    lock = threading.Lock()

    def probe_one(info):
        vid, pid = (info.vid or 0), (info.pid or 0)
        kind = CANDIDATE_VIDS.get(vid)
        if kind is None and not info.description:
            return
        label = kind or info.description
        try:
            ok = False
            # Espressif's USB-Serial-JTAG speaks any baud (CDC); UART bridges
            # carry the 2 Mbaud burst transport, so try their native rate
            # first and give CH340-class chips a moment between reopens.
            bauds = (115200,) if vid == USB_SJTAG_VID else (2000000, 115200)
            s = None
            for baud in bauds:
                for attempt in range(2):
                    try:
                        s = serial.Serial()
                        s.port = info.device
                        s.baudrate = baud
                        s.timeout = 0.15
                        s.write_timeout = 1.0
                        s.dtr = False
                        s.rts = False
                        s.open()
                        break
                    except Exception:
                        time.sleep(0.4)
                        s = None
                if s is None:
                    continue
                s.setDTR(False)
                s.setRTS(False)
                try:
                    s.set_buffer_size(rx_size=65536, tx_size=4096)
                except Exception:
                    pass
                time.sleep(0.15)
                s.reset_input_buffer()
                # Clear any five-second session lease left by a previous
                # probe; then retry SYNC long enough to outlive a lease
                # owned by the other port.
                s.write(b"RELEASE\n")
                time.sleep(0.1)
                s.reset_input_buffer()
                deadline = time.time() + 6.0
                buf = b""
                ok = False
                while time.time() < deadline and not ok:
                    s.write(b"SYNC 4755\n")
                    sub = time.time() + 0.9
                    while time.time() < sub:
                        buf += s.read(256)
                        if buf.rstrip().endswith(b"SYNC 4755"):
                            ok = True
                            break
                if ok:
                    break
                s.close()
                s = None
                time.sleep(0.3)
            if ok:
                s.write(b"TRANSPORT?\n")
                deadline = time.time() + 0.8
                tr = b""
                while time.time() < deadline and b"\n" not in tr:
                    tr += s.read(256)
                t = tr.decode(errors="replace").strip()
                tag = "USB-Serial-JTAG" if "USB" in t else "UART"
                if vid == USB_SJTAG_VID or "USB" in t:
                    tag = "USB-Serial-JTAG"
                with lock:
                    found.append((info.device, "%s (%s) [%s]" % (info.device, tag, label)))
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
        except Exception:
            return

    threads = []
    for info in list_ports.comports():
        th = threading.Thread(target=probe_one, args=(info,), daemon=True)
        th.start()
        threads.append(th)
    # A port can refuse SYNC while the firmware's five-second lease belongs
    # to the other port, so the worst-case probe waits it out.
    deadline = time.time() + 7.5
    for th in threads:
        th.join(max(0.1, deadline - time.time()))
    return found


# --------------------------------------------------------------------------
# Color map for the waterfall (code 0..255 -> RGB)
# --------------------------------------------------------------------------
def build_colormap():
    stops = [(0, (8, 8, 26)), (60, (24, 40, 110)), (110, (20, 120, 180)),
             (150, (30, 180, 120)), (190, (230, 200, 40)), (225, (240, 90, 30)),
             (255, (250, 250, 250))]
    table = []
    for i in range(256):
        for j in range(len(stops) - 1):
            c0, v0 = stops[j]
            c1, v1 = stops[j + 1]
            if c0 <= i <= c1:
                f = (i - c0) / max(1, c1 - c0)
                table.append(tuple(int(v0[k] + (v1[k] - v0[k]) * f) for k in range(3)))
                break
    return table


COLORMAP = build_colormap()
# 3-byte flavour for the waterfall: rows are joined with b"".join(map(...)),
# which is C-level per element instead of a Python loop per pixel.
COLORMAP_B = [bytes(c) for c in COLORMAP]
WF_BG = b"\x08\x08\x1a"
RATES = [("80 MS/s", 0), ("40 MS/s", 1), ("20 MS/s", 2), ("10 MS/s", 3),
         ("8 MS/s", 4), ("4 MS/s", 5)]
BIN_COUNTS = [256, 512, 1024, 2048]

# SDR capture modes: the RF front end either runs the firmware's on-chip FFT
# (SPEC) or ships raw I/Q back to the PC (IQS continuous / CAP16 bursts).
CAP_SPEC = "片上频谱 (On-chip FFT)"
CAP_IQ = "IQ 传回电脑 (I/Q Streaming)"
IQM_CONT = "连续 (无间隙)"
IQM_BURST = "突发 (快照连拍)"


def bit_reverse(j, bits):
    r = 0
    for _ in range(bits):
        r = (r << 1) | (j & 1)
        j >>= 1
    return r


# --------------------------------------------------------------------------
# Pure-python FFT for the I/Q streaming view (no numpy dependency).
# --------------------------------------------------------------------------
_FFT_PLANS = {}


def _fft_plan(n):
    plan = _FFT_PLANS.get(n)
    if plan is None:
        bits = n.bit_length() - 1
        rev = [bit_reverse(j, bits) for j in range(n)]
        import cmath
        tw = [cmath.exp(-2j * 3.141592653589793 * k / n) for k in range(n // 2)]
        plan = (rev, tw)
        _FFT_PLANS[n] = plan
    return plan


def iq_spectrum_codes(iq, nfft):
    """int8 I/Q pairs -> power codes 0..255 in NATURAL frequency order
    (same convention as the on-chip SPEC frames: code = 2*10*log10(|X|),
    i.e. 20*log10(power))."""
    n = nfft
    step = len(iq) // 2 // n or 1
    buf = [0j] * n
    import cmath
    two_pi = 6.283185307179586
    for k in range(n):
        i = iq[2 * k * step]
        q = iq[2 * k * step + 1]
        if i > 127:
            i -= 256
        if q > 127:
            q -= 256
        w = 0.5 - 0.5 * cmath.cos(two_pi * k / (n - 1))
        buf[k] = complex(i, q) * w
    return _fft_codes(buf, n)


def iq32_spectrum_codes(payload, nfft):
    """int32 LE I/Q pairs (continuous IQFRAME payloads, 128-sample blocks of
    1024 bytes) -> power codes."""
    import cmath
    n = nfft
    step = (len(payload) // 8) // n or 1
    buf = [0j] * n
    two_pi = 6.283185307179586
    for k in range(n):
        off = 8 * k * step
        i = struct.unpack_from("<i", payload, off)[0]
        q = struct.unpack_from("<i", payload, off + 4)[0]
        w = 0.5 - 0.5 * cmath.cos(two_pi * k / (n - 1))
        buf[k] = complex(i >> 6, q >> 6) * w  # >>6: back to ~baseband amplitude
    return _fft_codes(buf, n)


def _fft_codes(buf, n):
    rev, tw = _fft_plan(n)
    import cmath
    out = [0j] * n
    for k in range(n):
        out[rev[k]] = buf[k]
    size = 2
    while size <= n:
        half = size // 2
        for start in range(0, n, size):
            for k in range(half):
                t = out[start + k + half] * tw[k * (n // size)]
                u = out[start + k]
                out[start + k] = u + t
                out[start + k + half] = u - t
        size *= 2
    import math
    codes = []
    for k in range(n):
        p = out[k].real * out[k].real + out[k].imag * out[k].imag
        v = int(10.0 * math.log10(p + 1e-12)) * 2 - 20  # ~SPEC convention
        codes.append(0 if v < 0 else (255 if v > 255 else v))
    return codes


class IQStreamer(threading.Thread):
    """Repeated CAP16 snapshots (the firmware's burst capture) with local FFT,
    giving an espargos-style I/Q streaming view. Snapshot gaps are inherent."""

    def __init__(self, app, n, rate_code):
        super().__init__(daemon=True)
        self.app = app
        self.n = n
        self.rate_code = rate_code
        self.queue = queue.Queue(maxsize=4)
        self.alive = True
        self.out = None

    def feed(self, parts, payload):
        try:
            self.queue.put_nowait((parts, payload))
        except queue.Full:
            pass  # drop on backpressure; the view is live-only

    def attach_file(self, path):
        self.out = open(path, "ab")

    def run(self):
        link = self.app.link
        link.data_wire_fn = lambda nn: nn * 2
        nfft = 1
        while nfft * 2 <= min(self.n, 4096):
            nfft *= 2
        try:
            while self.alive:
                try:
                    link.ser.write(b"CAP16 %d %d\n" % (self.n, self.rate_code))
                except Exception:
                    break
                try:
                    parts, payload = self.queue.get(timeout=6)
                except queue.Empty:
                    continue
                if self.out is not None:
                    try:
                        self.out.write(payload)
                        self.out.flush()
                    except Exception:
                        self.out = None
                codes = iq_spectrum_codes(payload, nfft)
                # Route through the App's event queue: tkinter calls must
                # happen on the GUI thread. "iqcodes" is the burst path (the
                # spectrum is already computed here); the continuous path
                # arrives as "iqframe" with raw int32 I/Q and is transformed
                # in the GUI thread.
                self.app.q.put(("iqcodes", codes, len(payload)))
        finally:
            link.data_wire_fn = None
            if self.out is not None:
                try:
                    self.out.close()
                except Exception:
                    pass

    def stop(self):
        self.alive = False


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ESP-SDR 调试工具 (ESP32-C5)")
        self.geometry("1080x760")
        self.link = None
        self.mode = None           # None | 'SDR' | 'WIFI' | 'BLE'
        self.q = queue.Queue()
        self.wifi_pcap = PcapStream(LINKTYPE_WIFI)
        self.ble_pcap = PcapStream(LINKTYPE_BLE)
        self.wifi_pipe = None
        self.ble_pipe = None
        self.dev_wifi = {}         # mac -> [rssi_best, count, last]
        self.dev_ble = {}
        self.spec_running = False
        self.spec_bins = 256
        self.spec_shifted = []
        self.spec_center_mhz = 2412.0
        self.spec_span_mhz = 80.0
        self.last_freq_mhz = 2412
        self.last_rate_msps = 80.0
        self.iq_running = False
        self.iq_stats = {"captures": 0, "bytes": 0, "started": 0.0}
        self._iq_buf = bytearray()
        self.bridge_server = None
        self.bridge_stream = None
        self.bridge_rate = 80000
        self.bridge_frames = 0
        self.bridge_bytes = 0
        self.bridge_clients = 0
        self.wf_rows = []
        self.redraw_ms = 50   # display tick: never redraw faster than this
        self._wf_xmap_cache = {}
        self.wf_x = 512

        self._build_port_bar()
        self._build_mode_bar()
        self._build_panels()
        self._build_log()
        self._poll_queue()
        self._redraw_job = None

    # ------------------------------------------------------------------ UI
    def _build_port_bar(self):
        bar = ttk.LabelFrame(self, text="串口")
        bar.pack(side=tk.TOP, fill=tk.X, padx=8, pady=4)
        ttk.Label(bar, text="设备端口:").grid(row=0, column=0, padx=4, pady=4)
        self.cmd_port = ttk.Combobox(bar, width=40, state="readonly")
        self.cmd_port.grid(row=0, column=1, padx=4)
        ttk.Button(bar, text="刷新/自动识别", command=self.refresh_ports).grid(row=0, column=2, padx=4)
        self.conn_btn = ttk.Button(bar, text="连接", command=self.toggle_connect)
        self.conn_btn.grid(row=0, column=3, padx=4)
        ttk.Label(bar, text="(USB-Serial-JTAG；命令与数据共用此口)").grid(
            row=0, column=4, padx=6, sticky=tk.W)

    def _build_mode_bar(self):
        bar = ttk.LabelFrame(self, text="模式（切换前必须先点 STOP）")
        bar.pack(side=tk.TOP, fill=tk.X, padx=8, pady=4)
        self.btn_sdr = ttk.Button(bar, text="SDR 频谱", command=lambda: self.start_mode("SDR"))
        self.btn_sdr.grid(row=0, column=0, padx=6, pady=4)
        self.btn_wifi = ttk.Button(bar, text="WiFi Monitor", command=lambda: self.start_mode("WIFI"))
        self.btn_wifi.grid(row=0, column=1, padx=6)
        self.btn_ble = ttk.Button(bar, text="BLE Monitor", command=lambda: self.start_mode("BLE"))
        self.btn_ble.grid(row=0, column=2, padx=6)
        self.btn_sdrsharp = ttk.Button(bar, text="SDR# 桥接",
                                       command=lambda: self.start_mode("SDRSHARP"))
        self.btn_sdrsharp.grid(row=0, column=3, padx=6)
        self.btn_stop = ttk.Button(bar, text="■ STOP", command=self.stop_mode, state=tk.DISABLED)
        self.btn_stop.grid(row=0, column=4, padx=6)
        self.mode_var = tk.StringVar(value="当前模式: 未连接")
        ttk.Label(bar, textvariable=self.mode_var, font=("", 10, "bold")).grid(
            row=0, column=5, padx=12, sticky=tk.W)

    def _build_panels(self):
        self.panels = ttk.Notebook(self)
        self.panels.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=4)
        self._build_sdr_panel()
        self._build_wifi_panel()
        self._build_ble_panel()
        self._build_bridge_panel()

    def _build_sdr_panel(self):
        f = ttk.Frame(self.panels)
        self.panels.add(f, text=" SDR ")
        top = ttk.Frame(f)
        top.pack(side=tk.TOP, fill=tk.X, pady=4)
        ttk.Label(top, text="频率 MHz (100-6000):").pack(side=tk.LEFT, padx=4)
        self.freq_var = tk.StringVar(value="2412")
        ttk.Entry(top, textvariable=self.freq_var, width=8).pack(side=tk.LEFT)
        ttk.Label(top, text="  采样率:").pack(side=tk.LEFT, padx=4)
        self.rate_var = tk.StringVar(value="80 MS/s")
        ttk.Combobox(top, textvariable=self.rate_var, values=[r[0] for r in RATES],
                     width=9, state="readonly").pack(side=tk.LEFT)
        ttk.Label(top, text="  FFT:").pack(side=tk.LEFT, padx=4)
        self.bins_var = tk.StringVar(value="256")
        ttk.Combobox(top, textvariable=self.bins_var, values=[str(b) for b in BIN_COUNTS],
                     width=6, state="readonly").pack(side=tk.LEFT)
        ttk.Label(top, text="  模拟带宽:").pack(side=tk.LEFT, padx=4)
        self.bw_var = tk.StringVar(value="Open (全带宽)")
        ttk.Combobox(top, textvariable=self.bw_var, width=16, state="readonly",
                     values=("Open (全带宽)", "Filter (设定 MHz)")).pack(side=tk.LEFT)
        self.bw_mhz = tk.Spinbox(top, from_=11, to=48, width=5)
        self.bw_mhz.delete(0, tk.END)
        self.bw_mhz.insert(0, "20")
        self.bw_mhz.pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="设置带宽", command=self.apply_bandwidth).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="设置频率", command=self.apply_freq, state=tk.NORMAL).pack(side=tk.LEFT)

        capbar = ttk.Frame(f)
        capbar.pack(side=tk.TOP, fill=tk.X, pady=2)
        ttk.Label(capbar, text="抓包模式:").pack(side=tk.LEFT, padx=4)
        self.cap_mode = tk.StringVar(value=CAP_SPEC)
        self.cap_mode_box = ttk.Combobox(capbar, textvariable=self.cap_mode, width=26,
                                         state="readonly", values=(CAP_SPEC, CAP_IQ))
        self.cap_mode_box.pack(side=tk.LEFT)
        self.cap_mode_box.bind("<<ComboboxSelected>>",
                               lambda e: self._sdr_mode_changed())
        ttk.Label(capbar, text="  IQ 方式:").pack(side=tk.LEFT, padx=4)
        self.iq_mode = tk.StringVar(value=IQM_CONT)
        self.iq_mode_box = ttk.Combobox(capbar, textvariable=self.iq_mode, width=18,
                                        state="disabled", values=(IQM_CONT, IQM_BURST))
        self.iq_mode_box.pack(side=tk.LEFT)
        self.iq_mode_box.bind("<<ComboboxSelected>>",
                              lambda e: self._sdr_mode_changed())
        ttk.Label(capbar, text="  连续速率 S/s:").pack(side=tk.LEFT)
        self.iq_rate = tk.StringVar(value="40000")
        self.iq_rate_box = ttk.Combobox(capbar, textvariable=self.iq_rate, width=8,
                                        state="disabled",
                                        values=("10000", "20000", "25000", "40000", "50000", "80000"))
        self.iq_rate_box.pack(side=tk.LEFT)
        ttk.Label(capbar, text="  突发样本数:").pack(side=tk.LEFT)
        # textvariable, not delete/insert: a DISABLED Spinbox ignores text edits,
        # which silently left the burst at `from_` (256 samples) and made the
        # burst spectrum far coarser than the 4096-sample default.
        self.iq_n_var = tk.StringVar(value="4096")
        self.iq_n = tk.Spinbox(capbar, from_=256, to=16380, width=7,
                               textvariable=self.iq_n_var, state=tk.DISABLED)
        self.iq_n.pack(side=tk.LEFT, padx=2)
        self.btn_cap = ttk.Button(capbar, text="开始抓包", command=self.toggle_capture,
                                  state=tk.DISABLED)
        self.btn_cap.pack(side=tk.LEFT, padx=8)
        self.iq_stats_var = tk.StringVar(value="")
        ttk.Label(capbar, textvariable=self.iq_stats_var, foreground="#006000").pack(
            side=tk.LEFT, padx=8)

        iqbar = ttk.Frame(f)
        iqbar.pack(side=tk.TOP, fill=tk.X, pady=2)
        self.iq_save_var = tk.BooleanVar(value=False)
        self.iq_save_chk = ttk.Checkbutton(iqbar, text="保存 IQ 原始样本到文件",
                                           variable=self.iq_save_var,
                                           command=self._iq_save_toggle,
                                           state=tk.DISABLED)
        self.iq_save_chk.pack(side=tk.LEFT, padx=4)
        self.iq_path = tk.StringVar(value=os.path.abspath("capture.iq"))
        self.iq_path_entry = ttk.Entry(iqbar, textvariable=self.iq_path, width=44)
        self.iq_path_entry.pack(side=tk.LEFT)
        ttk.Button(iqbar, text="浏览...",
                   command=lambda: self._browse(self.iq_path)).pack(side=tk.LEFT, padx=2)
        ttk.Label(iqbar, text="(仅 IQ 传回电脑模式；片上频谱不产生 I/Q 样本)").pack(
            side=tk.LEFT, padx=6)
        self._iq_save_toggle()

        # 显示选项收进可折叠面板：默认收起，只留一行摘要，避免占掉绘图高度
        self.disp_open = False
        dispbar = ttk.Frame(f)
        dispbar.pack(side=tk.TOP, fill=tk.X, pady=(2, 0))
        self.btn_disp = ttk.Button(dispbar, text="▶ 显示", width=9,
                                   command=self._toggle_display_panel)
        self.btn_disp.pack(side=tk.LEFT, padx=(2, 6))
        self.disp_summary = ttk.Label(dispbar, text="", foreground="#777777")
        self.disp_summary.pack(side=tk.LEFT)

        self.disp_panel = ttk.Frame(f, relief=tk.GROOVE, borderwidth=1)
        self.wf_floor = tk.IntVar(value=0)
        self.wf_range = tk.IntVar(value=255)
        self.wf_floor_txt = tk.StringVar(value="0")
        self.wf_range_txt = tk.StringVar(value="255")
        ttk.Label(self.disp_panel, text="瀑布电平").pack(side=tk.LEFT, padx=(6, 8))
        ttk.Label(self.disp_panel, text="Floor").pack(side=tk.LEFT)
        ttk.Scale(self.disp_panel, from_=0, to=255, orient=tk.HORIZONTAL, length=170,
                  variable=self.wf_floor,
                  command=lambda _v: self._wf_level_changed()).pack(side=tk.LEFT, padx=4)
        ttk.Label(self.disp_panel, textvariable=self.wf_floor_txt, width=4).pack(side=tk.LEFT)
        ttk.Label(self.disp_panel, text="Range").pack(side=tk.LEFT, padx=(10, 0))
        ttk.Scale(self.disp_panel, from_=10, to=255, orient=tk.HORIZONTAL, length=170,
                  variable=self.wf_range,
                  command=lambda _v: self._wf_level_changed()).pack(side=tk.LEFT, padx=4)
        ttk.Label(self.disp_panel, textvariable=self.wf_range_txt, width=4).pack(side=tk.LEFT)
        ttk.Button(self.disp_panel, text="自动电平", width=9,
                   command=self._wf_auto_level).pack(side=tk.LEFT, padx=10)
        ttk.Label(self.disp_panel, text="code = 20·log10|X| dB（相对值，0–255）",
                  foreground="#777777").pack(side=tk.LEFT, padx=6)

        mid = ttk.Frame(f)
        mid.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.mid_frame = mid
        self.spec_canvas = tk.Canvas(mid, bg="#06060f", height=200, highlightthickness=0)
        self.spec_canvas.pack(side=tk.TOP, fill=tk.X, padx=6)
        self.wf_height = 220
        self.wf_photo = tk.PhotoImage(width=512, height=self.wf_height)
        self.wf_title = ttk.Label(mid, text="瀑布（顶部为最新一行）", foreground="#777777")
        self.wf_title.pack(side=tk.TOP, anchor=tk.W, padx=8)
        wfrow = ttk.Frame(mid)
        wfrow.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=4)
        self.wf_legend = tk.Canvas(wfrow, width=70, height=self.wf_height,
                                   highlightthickness=0, bg="#06060f")
        self.wf_legend.pack(side=tk.RIGHT, padx=(6, 0))
        self.wf_label = ttk.Label(wfrow, image=self.wf_photo, anchor=tk.NW)
        self.wf_label.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._wf_level_changed()

    def _build_wifi_panel(self):
        f = ttk.Frame(self.panels)
        self.panels.add(f, text=" WiFi Monitor ")
        cfg = ttk.LabelFrame(f, text="配置")
        cfg.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)
        self.wifi_full = tk.BooleanVar(value=False)
        ttk.Checkbutton(cfg, text="全信道轮巡", variable=self.wifi_full,
                        command=self._wifi_toggle).grid(row=0, column=0, padx=6, pady=4, sticky=tk.W)
        ttk.Label(cfg, text="固定信道:").grid(row=0, column=1)
        self.wifi_ch = ttk.Combobox(cfg, width=7, state="readonly",
                                    values=[str(c) for c in WIFI_2G_CHANNELS + WIFI_5G_CHANNELS])
        self.wifi_ch.set("1")
        self.wifi_ch.grid(row=0, column=2, padx=2)
        ttk.Label(cfg, text="全信道频段:").grid(row=0, column=3)
        self.wifi_band = ttk.Combobox(cfg, width=13, state="disabled", values=(
            "2.4 GHz (1-13)", "5 GHz (UNII)"))
        self.wifi_band.set("2.4 GHz (1-13)")
        self.wifi_band.grid(row=0, column=4, padx=2)
        ttk.Label(cfg, text="驻留 ms:").grid(row=0, column=5)
        self.wifi_dwell = tk.Spinbox(cfg, from_=20, to=1000, width=6)
        self.wifi_dwell.delete(0, tk.END)
        self.wifi_dwell.insert(0, "110")
        self.wifi_dwell.grid(row=0, column=6, padx=2)
        ttk.Label(cfg, text="(默认 110ms)").grid(row=0, column=7, sticky=tk.W)
        self.wifi_pcap_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(cfg, text="写 pcap", variable=self.wifi_pcap_var,
                        command=self._wifi_pcap_toggle).grid(row=1, column=0, sticky=tk.W, padx=6)
        self.wifi_pcap_path = tk.StringVar(value=os.path.abspath("wifi_monitor.pcap"))
        ttk.Entry(cfg, textvariable=self.wifi_pcap_path, width=44).grid(row=1, column=1, columnspan=4, sticky=tk.W)
        ttk.Button(cfg, text="浏览...", command=lambda: self._browse(self.wifi_pcap_path)).grid(row=1, column=5)
        self.wifi_pipe_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(cfg, text="Wireshark 实时管道", variable=self.wifi_pipe_var,
                        command=self._wifi_pipe_toggle).grid(row=2, column=0, sticky=tk.W, padx=6)
        ttk.Label(cfg, text=r"wireshark -k -i \\.\pipe\esp_sdr_wifi").grid(row=2, column=1, columnspan=4, sticky=tk.W)

        self.wifi_tree = self._device_tree(f, ("mac", "rssi", "frames", "ch", "最近出现"))
        self.wifi_stats = tk.StringVar(value="帧数 0")
        ttk.Label(f, textvariable=self.wifi_stats).pack(anchor=tk.W, padx=8)

    def _build_ble_panel(self):
        f = ttk.Frame(self.panels)
        self.panels.add(f, text=" BLE Monitor ")
        note = ("ESP32-C5 的 BLE 控制器固定同时扫描 37/38/39 三个广播信道，无法固定到单一信道，"
                "也不支持按信道设置驻留时间（被动扫描，仅监听广播包，不建立连接、不跟随跳频）。\n"
                "BLE 抓包信道字段统一标记为 37，为硬件限制。")
        ttk.Label(f, text=note, foreground="#a06000", wraplength=1000,
                  justify=tk.LEFT).pack(anchor=tk.W, padx=8, pady=4)
        cfg = ttk.LabelFrame(f, text="配置")
        cfg.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)
        ttk.Label(cfg, text="扫描 interval/window 使用固件默认值 (0x50/0x30)。").grid(
            row=0, column=0, columnspan=3, sticky=tk.W, padx=6)
        self.ble_pcap_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(cfg, text="写 pcap", variable=self.ble_pcap_var,
                        command=self._ble_pcap_toggle).grid(row=1, column=0, sticky=tk.W, padx=6)
        self.ble_pcap_path = tk.StringVar(value=os.path.abspath("ble_monitor.pcap"))
        ttk.Entry(cfg, textvariable=self.ble_pcap_path, width=44).grid(row=1, column=1, sticky=tk.W)
        ttk.Button(cfg, text="浏览...", command=lambda: self._browse(self.ble_pcap_path)).grid(row=1, column=2)
        self.ble_pipe_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(cfg, text="Wireshark 实时管道", variable=self.ble_pipe_var,
                        command=self._ble_pipe_toggle).grid(row=2, column=0, sticky=tk.W, padx=6)
        ttk.Label(cfg, text=r"wireshark -k -i \\.\pipe\esp_sdr_ble").grid(row=2, column=1, sticky=tk.W)

        self.ble_tree = self._device_tree(f, ("adva", "rssi", "frames", "ch", "AD 数据 (hex)"))
        self.ble_stats = tk.StringVar(value="帧数 0")
        ttk.Label(f, textvariable=self.ble_stats).pack(anchor=tk.W, padx=8)

    def _device_tree(self, parent, columns):
        frame = ttk.Frame(parent)
        frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=4)
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=14)
        for c in columns:
            tree.heading(c, text=c)
            tree.column(c, width=150 if c in ("mac", "adva") else 80, anchor=tk.W)
        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        return tree

    def _build_log(self):
        frame = ttk.LabelFrame(self, text="日志")
        frame.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=4)
        self.log_text = tk.Text(frame, height=6, wrap=tk.WORD)
        self.log_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

    # ------------------------------------------------------------- helpers
    def log(self, msg):
        def append():
            self.log_text.insert(tk.END, time.strftime("%H:%M:%S ") + msg + "\n")
            self.log_text.see(tk.END)
        self.after_idle(append)

    def _browse(self, var):
        path = filedialog.asksaveasfilename(defaultextension=".pcap")
        if path:
            var.set(path)

    def _wifi_toggle(self):
        full = self.wifi_full.get()
        self.wifi_ch.configure(state=tk.DISABLED if full else "readonly")
        self.wifi_band.configure(state="readonly" if full else tk.DISABLED)

    def _wifi_pcap_toggle(self):
        pass

    def _wifi_pipe_toggle(self):
        pass

    def _ble_pcap_toggle(self):
        pass

    def _ble_pipe_toggle(self):
        pass

    def refresh_ports(self):
        self.log("扫描串口（最多约 8 秒，需等待固件 5 秒租约过期）...")
        self.conn_btn.configure(state=tk.DISABLED)

        def work():
            found = probe_ports(self.log)
            all_ports = [p.device for p in list_ports.comports()]
            labels = [lbl for _, lbl in found]
            for p in all_ports:
                if not any(p == f[0] for f in found):
                    labels.append("%s (未识别)" % p)

            def apply():
                self.conn_btn.configure(state=tk.NORMAL)
                self.cmd_port["values"] = labels
                if labels:
                    usb = next((l for l in labels if "USB-Serial-JTAG" in l), labels[0])
                    if not self.cmd_port.get():
                        self.cmd_port.set(usb)
                self.log("发现 %d 个串口: %s" % (len(labels), ", ".join(labels) or "无"))

            self.after_idle(apply)

        threading.Thread(target=work, daemon=True).start()

    def _port_of(self, label):
        return label.split(" ")[0]

    def toggle_connect(self):
        if self.link:
            self.stop_mode()
            self.link.close()
            self.link = None
            self.conn_btn.configure(text="连接")
            self.mode_var.set("当前模式: 未连接")
            self.btn_cap.configure(state=tk.DISABLED)
            self._sdr_mode_changed()
            self.log("已断开")
            return
        cmdp = self._port_of(self.cmd_port.get()) if self.cmd_port.get() else ""
        if not cmdp:
            messagebox.showwarning("提示", "请选择设备端口")
            return
        try:
            baud = 115200  # USB-Serial-JTAG is a CDC device; baud is ignored.
            link = Link(cmdp, self._on_event, self.log, baudrate=baud)
            link.open()
        except Exception as e:
            messagebox.showerror("打开串口失败", str(e))
            return
        self.link = link
        # Release/drain on the quiet port BEFORE the reader thread starts
        # (pyserial on Windows is not thread-safe; PurgeComm concurrent with
        # a blocked ReadFile can crash the process).
        try:
            link.ser.write(b"RELEASE\n")
            time.sleep(0.2)
            link.ser.reset_input_buffer()
        except Exception:
            pass
        link.start()
        try:
            r = link.command("SYNC 4711")
            self.log("握手: %s" % r)
            r = link.command("INFO")
            self.log("INFO: %s" % r)
        except Exception as e:
            self.log("握手失败: %s" % e)
        self.conn_btn.configure(text="断开")
        self.mode_var.set("当前模式: 空闲 (SDR)")

    # ------------------------------------------------------- mode framework
    def start_mode(self, mode):
        if not self.link:
            self.log("请先连接串口")
            return
        if self.mode is not None:
            self.log("请先点击 STOP 再切换模式！")
            return
        try:
            if mode == "SDR":
                self.mode = "SDR"
                self.btn_cap.configure(state=tk.NORMAL)
                self._sdr_mode_changed()
                self.panels.select(0)
            elif mode == "WIFI":
                if self.wifi_full.get():
                    dwell = int(self.wifi_dwell.get())
                    first = 36 if "5 GHz" in self.wifi_band.get() else 1
                    r = self.link.command("MON WIFI ON %d %d" % (first, dwell))
                else:
                    ch = int(self.wifi_ch.get())
                    r = self.link.command("MON WIFI ON %d" % ch)
                if not r.startswith("OK"):
                    self.log("WiFi Monitor 启动失败: %s" % r)
                    return
                self._wifi_start_outputs()
                self.mode = "WIFI"
                self.panels.select(1)
            elif mode == "BLE":
                r = self.link.command("MON BLE ON")
                if not r.startswith("OK"):
                    self.log("BLE Monitor 启动失败: %s" % r)
                    return
                self._ble_start_outputs()
                self.mode = "BLE"
                self.panels.select(2)
            elif mode == "SDRSHARP":
                if SDRSHARP_BRIDGE is None:
                    self.log("缺少 monitor_gui/sdrsharp_bridge.py，桥接不可用")
                    return
                if not self._bridge_start():
                    return
                self.mode = "SDRSHARP"
                self.panels.select(3)
        except Exception as e:
            self.log("模式启动异常: %s" % e)
            self.mode = None
            return
        self.mode_var.set("当前模式: %s (点击 STOP 停止后才能切换)" % mode)
        self.btn_stop.configure(state=tk.NORMAL)
        self._mode_buttons(tk.DISABLED)

    def stop_mode(self):
        if not self.link:
            return
        try:
            if self.mode == "SDR":
                self._stop_spec()
                self._stop_iq()
            elif self.mode == "WIFI":
                self.link.command("MON WIFI OFF")
                self._wifi_stop_outputs()
            elif self.mode == "BLE":
                self.link.command("MON BLE OFF")
                self._ble_stop_outputs()
            elif self.mode == "SDRSHARP":
                self._bridge_stop()
        except Exception as e:
            self.log("停止异常: %s" % e)
        self.mode = None
        self.btn_stop.configure(state=tk.DISABLED)
        self.btn_cap.configure(state=tk.DISABLED)
        self._sdr_mode_changed()
        self._mode_buttons(tk.NORMAL)
        self.mode_var.set("当前模式: 空闲 (SDR)")
        self.log("模式已停止")

    def _mode_buttons(self, state):
        self.btn_sdr.configure(state=state)
        self.btn_wifi.configure(state=state)
        self.btn_ble.configure(state=state)
        self.btn_sdrsharp.configure(state=state)

    # -------------------------------------------------------------- events
    def _on_event(self, kind, *args):
        self.q.put((kind,) + args)

    def _poll_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                self._handle_event(*item)
        except queue.Empty:
            pass
        self.after(40, self._poll_queue)

    def _handle_event(self, kind, *args):
        if kind == "line":
            pass  # replies are consumed by command(); stray lines land here
        elif kind == "data":
            if self.iq_running and not getattr(self, "iq_continuous", False) and \
                    getattr(self, "iq", None):
                self.iq.feed(args[0], args[1])
        elif kind == "iqcodes":
            # Burst (CAP16) path: the streamer thread already produced the
            # spectrum codes from the snapshot.
            if self.iq_running and not getattr(self, "iq_continuous", False):
                self._on_iq_frame(args[0], args[1])
        elif kind == "iqframe":
            if self.mode == "SDRSHARP" and self.bridge_stream is not None:
                self._bridge_feed(args[1])
                return
            if self.iq_running and getattr(self, "iq_continuous", False):
                parts, payload = args[0], args[1]
                self._iq_account(len(payload))
                if self.iq_save_var.get():
                    try:
                        if not getattr(self, "iq_fout", None):
                            self.iq_fout = open(self.iq_path.get(), "ab")
                        self.iq_fout.write(payload)
                    except Exception as e:
                        self.log("IQ 写入失败: %s" % e)
                        self.iq_fout = None
                # Draw at most one frame per display tick, and FFT a 512-sample
                # window instead of a single 128-sample frame: the per-frame
                # pure-Python FFT at 80 kS/s cost more than one tick and starved
                # the serial reader thread (RX overflow -> stream desync), while
                # one frame alone gives a 128-bin row that looks much coarser
                # than the on-chip SPEC rows next to it.
                self._iq_buf += payload
                if len(self._iq_buf) > 16384:
                    del self._iq_buf[:-8192]
                if self._redraw_job is None and len(self._iq_buf) >= 4096:
                    codes = iq32_spectrum_codes(bytes(self._iq_buf[-4096:]), 512)
                    self._on_iq_frame(codes, len(payload), account=False)
        elif kind == "wififrame":
            self._on_wifi_frame(*args)
        elif kind == "bleframe":
            self._on_ble_frame(*args)
        elif kind == "spec":
            self._on_spec(*args)
        elif kind == "bridge_log":
            self.log("[桥接] %s" % args[0])
        elif kind == "bridge_clients":
            self.bridge_clients = args[0]
            self.bridge_stat_var.set("客户端 %d" % self.bridge_clients)
        elif kind == "link_error":
            self.log("链路错误: %s" % args[0])

    def _build_bridge_panel(self):
        f = ttk.Frame(self.panels)
        self.panels.add(f, text=" SDR# 桥接 ")
        head = ("把本机伪装成一台 PlutoSDR（libiio/iiod，TCP 30431），"
                "SDR# 用自带的 PlutoSDR 源接入。" + os.linesep +
                "SDR# 端：源 = PlutoSDR，Device-URI = ip:127.0.0.1（端口固定 30431），"
                "然后点 ▶ 开始。" + os.linesep +
                "注意：桥接期间独占串口；点 STOP 会 IQS OFF + 释放租约，"
                "之后 WiFi/BLE/SDR 才能再启动。")
        ttk.Label(f, text=head, justify=tk.LEFT, wraplength=1000,
                  foreground="#a06000").pack(anchor=tk.W, padx=8, pady=6)

        cfg = ttk.LabelFrame(f, text="配置")
        cfg.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)
        ttk.Label(cfg, text="IQS 速率:").grid(row=0, column=0, padx=6, pady=4, sticky=tk.W)
        self.bridge_rate_var = tk.StringVar(value="80000")
        ttk.Combobox(cfg, textvariable=self.bridge_rate_var, width=10, state="readonly",
                     values=("10000", "20000", "25000", "40000", "50000", "80000")
                     ).grid(row=0, column=1, sticky=tk.W)
        ttk.Label(cfg, text="（SDR# 请求其它速率时主机侧插值，串口侧就近取档）",
                  foreground="#777777").grid(row=0, column=2, padx=6, sticky=tk.W)
        ttk.Label(cfg, text="监听:").grid(row=1, column=0, padx=6, sticky=tk.W)
        self.bridge_addr_var = tk.StringVar(value="未启动")
        ttk.Label(cfg, textvariable=self.bridge_addr_var).grid(
            row=1, column=1, columnspan=2, sticky=tk.W)
        ttk.Label(cfg, text="说明：C5 只有整 MHz 调谐；改频率时桥接会先停流再 FREQ。",
                  foreground="#777777").grid(row=2, column=0, columnspan=3,
                                             padx=6, pady=(0, 4), sticky=tk.W)

        st = ttk.LabelFrame(f, text="状态")
        st.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)
        self.bridge_stat_var = tk.StringVar(value="未启动")
        ttk.Label(st, textvariable=self.bridge_stat_var).pack(anchor=tk.W, padx=6, pady=4)

        ttk.Label(f, text="提示：SDR# 关闭后桥接仍保持监听，可再次连接；点 STOP 才释放串口。",
                  foreground="#777777").pack(anchor=tk.W, padx=8, pady=6)

    # --------------------------------------------------------- SDR# bridge
    def _bridge_note(self, msg):
        """iiod 线程调用：只入队，由 GUI 线程打印。"""
        self.q.put(("bridge_log", msg))

    def _bridge_start(self):
        mon = self.link.command("MON?", 4)
        if "IQ:on" in mon:
            # 上一次会话（桥接/连续 IQ）没干净退出：先停流再启动
            self.log("桥接：固件里还有 IQS 流在跑，先 IQS OFF")
            self.link.command("IQS OFF", 5)
            mon = self.link.command("MON?", 4)
        if "IQ:off" not in mon or "WIFI:off" not in mon or "BLE:off" not in mon:
            self.log("桥接启动失败：串口上还有其它模式在跑（%s）" % mon.strip()[:120])
            return False
        rate = int(self.bridge_rate_var.get())
        r = self.link.command("IQS ON %d" % rate, 6)
        if not r.startswith("OK"):
            self.log("桥接启动失败：IQS ON %d -> %s" % (rate, r))
            return False
        self.bridge_rate = rate
        self.bridge_stream = SDRSHARP_BRIDGE.SampleStream(rate)
        self.bridge_frames = 0
        self.bridge_bytes = 0
        self.bridge_clients = 0
        self._bridge_scale = None
        self._bridge_dc = [0.0, 0.0]
        self._bridge_peak = 0.0
        self._bridge_blocks = 0
        try:
            self.bridge_server = SDRSHARP_BRIDGE.IiodServer(
                self.bridge_stream,
                on_freq=self._bridge_on_freq,
                on_rate=self._bridge_on_rate,
                on_gain=self._bridge_on_gain,
                on_bandwidth=self._bridge_on_bw,
                freq_hz=int(self.freq_var.get() or 2412) * 1_000_000,
                rate_hz=rate,
                log=self._bridge_note,
                on_client_change=self._bridge_on_clients)
        except OSError as e:
            self.log("桥接启动失败：端口 30431 被占用？%s" % e)
            self.link.command("IQS OFF", 4)
            self.bridge_stream = None
            return False
        self.bridge_addr_var.set("127.0.0.1:%d（SDR# → PlutoSDR, ip:127.0.0.1）"
                                 % SDRSHARP_BRIDGE.IIOD_PORT)
        self.bridge_stat_var.set("已启动，等待 SDR# 连接…")
        self.log("SDR# 桥接已启动：IQS %d S/s，监听 127.0.0.1:%d；"
                 "SDR# 源选 PlutoSDR、URI 填 ip:127.0.0.1 后点 ▶"
                 % (rate, SDRSHARP_BRIDGE.IIOD_PORT))
        self.after(500, self._bridge_tick)
        return True

    def _bridge_stop(self):
        try:
            if self.bridge_server is not None:
                self.bridge_server.close()
        except Exception as e:
            self.log("桥接关闭异常: %s" % e)
        self.bridge_server = None
        self.bridge_stream = None
        try:
            self.link.command("IQS OFF", 4)
        except Exception:
            pass
        self.bridge_addr_var.set("未启动")
        self.bridge_stat_var.set("已停止")
        self.log("SDR# 桥接已停止（IQS OFF）")

    def _bridge_on_clients(self, n):
        self.q.put(("bridge_clients", n))

    def _bridge_on_freq(self, hz):
        mhz = max(100, min(6000, int(round(hz / 1e6))))
        err = hz / 1e6 - mhz
        rate = self.bridge_rate
        if self.bridge_stream is not None:
            self.bridge_stream.clear()           # 丢掉旧频点的样本（重启后再清会清掉新数据）
        self.link.command("IQS OFF", 4)          # 流式期间固件拒绝 FREQ
        r = self.link.command("FREQ %d" % mhz, 10)
        self.link.command("IQS ON %d" % rate, 6)
        self._bridge_note("SDR# 调谐 %.3f MHz -> FREQ %d MHz (Δ=%+.3f) %s"
                          % (hz / 1e6, mhz, err,
                             "OK" if r.startswith("OK") else r.strip()[:40]))

    def _bridge_on_rate(self, hz):
        want = min((10000, 20000, 25000, 40000, 50000, 80000),
                   key=lambda x: abs(x - hz))
        if want != self.bridge_rate:
            self.link.command("IQS OFF", 4)
            r = self.link.command("IQS ON %d" % want, 6)
            if r.startswith("OK"):
                self.bridge_rate = want
            self._bridge_note("SDR# 请求 %d S/s -> 固件 %d S/s（主机插值到 %d）"
                              % (hz, self.bridge_rate, hz))
        if self.bridge_stream is not None:
            self.bridge_stream.set_rate(hz)

    def _bridge_on_gain(self, idx):
        if idx is None or idx < 0:
            self.link.command("GAIN HARDWARE", 6)
            self._bridge_note("SDR# 增益模式 -> GAIN HARDWARE")
        else:
            idx = max(0, min(80, int(idx)))
            self.link.command("GAIN MANUAL %d" % idx, 6)
            self._bridge_note("SDR# 增益 -> GAIN MANUAL %d" % idx)

    def _bridge_on_bw(self, hz):
        mhz = int(round(hz / 1e6)) if hz else 0
        if mhz:
            mhz = max(11, min(48, mhz))
        self.link.command("BANDWIDTH %d" % mhz, 8)
        self._bridge_note("SDR# 带宽 %.1f MHz -> BANDWIDTH %d MHz" % (hz / 1e6, mhz))

    def _bridge_feed(self, payload):
        """IQFRAME 载荷（int32 LE 交错）-> int16 交错，去直流后进桥接缓冲。"""
        n = len(payload) // 8
        if n <= 0:
            return
        vals = struct.unpack("<%di" % (n * 2), payload[:n * 8])
        i = vals[0::2]
        q = vals[1::2]
        di = sum(i) / float(n)
        dq = sum(q) / float(n)
        self._bridge_dc[0] = 0.9 * self._bridge_dc[0] + 0.1 * di
        self._bridge_dc[1] = 0.9 * self._bridge_dc[1] + 0.1 * dq
        dci, dcq = self._bridge_dc
        self._bridge_blocks += 1
        if self._bridge_scale is None:
            peak = max(max(abs(v) for v in i), max(abs(v) for v in q))
            self._bridge_peak = max(self._bridge_peak, peak)
            if self._bridge_blocks >= 20:              # 前 20 块估满量程
                self._bridge_scale = max(1.0, self._bridge_peak * 2.0)
                self._bridge_note("满量程参考 = %.0f（幅度未校准）" % self._bridge_scale)
        k = (32767.0 * 0.6) / (self._bridge_scale or 1.0)
        dcs = [dci, dcq] * n
        out = struct.pack("<%dh" % (n * 2),
                          *[int(max(-32768, min(32767, (v - dc) * k)))
                            for v, dc in zip(vals, dcs)])
        self.bridge_frames += 1
        self.bridge_bytes += len(out)
        self.bridge_stream.push_bytes(out)

    def _bridge_tick(self):
        if self.mode != "SDRSHARP" or self.bridge_stream is None:
            return
        st = self.bridge_stream
        pairs = (st.buf.size // 2) if hasattr(st.buf, "size") else (len(st.buf) // 2)
        self.bridge_stat_var.set(
            "客户端 %d · 已收 %d 帧 / %.1f MB · 缓冲 %d 对 · 输出 %.0f kS/s"
            % (self.bridge_clients, self.bridge_frames, self.bridge_bytes / 1e6,
               pairs, st.rate_out / 1000.0))
        self.after(500, self._bridge_tick)

    # ------------------------------------------------------------ wifi/ble
    def _wifi_start_outputs(self):
        self.dev_wifi.clear()
        for i in self.wifi_tree.get_children():
            self.wifi_tree.delete(i)
        self.wifi_frame_count = 0
        if self.wifi_pcap_var.get():
            try:
                self.wifi_pcap.add_file(self.wifi_pcap_path.get())
                self.log("WiFi pcap -> %s" % self.wifi_pcap_path.get())
            except Exception as e:
                self.log("pcap 文件打开失败: %s" % e)
        if self.wifi_pipe_var.get() and self.wifi_pipe is None:
            self.wifi_pipe = NamedPipeSink("esp_sdr_wifi", self.log)
            self.wifi_pcap.add_pipe_sink(self.wifi_pipe)

    def _wifi_stop_outputs(self):
        self.wifi_pcap.close()
        if self.wifi_pipe:
            self.wifi_pipe.close()
            self.wifi_pipe = None

    def _ble_start_outputs(self):
        self.dev_ble.clear()
        for i in self.ble_tree.get_children():
            self.ble_tree.delete(i)
        self.ble_frame_count = 0
        if self.ble_pcap_var.get():
            try:
                self.ble_pcap.add_file(self.ble_pcap_path.get())
                self.log("BLE pcap -> %s" % self.ble_pcap_path.get())
            except Exception as e:
                self.log("pcap 文件打开失败: %s" % e)
        if self.ble_pipe_var.get() and self.ble_pipe is None:
            self.ble_pipe = NamedPipeSink("esp_sdr_ble", self.log)
            self.ble_pcap.add_pipe_sink(self.ble_pipe)

    def _ble_stop_outputs(self):
        self.ble_pcap.close()
        if self.ble_pipe:
            self.ble_pipe.close()
            self.ble_pipe = None

    def _on_wifi_frame(self, parts, payload):
        try:
            self.wifi_frame_count += 1
        except AttributeError:
            self.wifi_frame_count = 1
        self.wifi_pcap.record(payload)
        if len(payload) < 15 + 24:
            return
        rt_len = payload[2]
        fc = payload[rt_len] | (payload[rt_len + 1] << 8)
        if ((fc >> 2) & 3) != 0:  # only management frames carry addr3 position we assume
            return
        mac = payload[rt_len + 16:rt_len + 22].hex(":")  # addr3 (BSSID) for mgmt
        rssi = int(parts[2])
        ch = parts[3]
        dev = self.dev_wifi.get(mac)
        if dev is None:
            self.dev_wifi[mac] = [rssi, 1, time.time()]
            self.wifi_tree.insert("", tk.END, iid=mac, values=(mac, rssi, 1, ch, time.strftime("%H:%M:%S")))
        else:
            dev[0] = max(dev[0], rssi)
            dev[1] += 1
            dev[2] = time.time()
            self.wifi_tree.item(mac, values=(mac, dev[0], dev[1], ch, time.strftime("%H:%M:%S", time.localtime(dev[2]))))
        self.wifi_stats.set("帧数 %d，设备 %d" % (self.wifi_frame_count, len(self.dev_wifi)))

    def _on_ble_frame(self, parts, payload):
        try:
            self.ble_frame_count += 1
        except AttributeError:
            self.ble_frame_count = 1
        # Build the LINKTYPE 256 record in the on-air layout: 10-byte
        # pseudo-header, advertising access address, PDU, CRC24.
        rssi = int(parts[2])
        flags = F_DEWHITENED | F_REF_AA_VALID | F_AA_OFFENSES_VALID | F_CRC_CHECKED | F_CRC_VALID
        phdr = struct.pack("<BbbBIH", 37, 0, rssi, 0, BLE_ADV_AA, flags)
        air = struct.pack("<I", BLE_ADV_AA) + payload + struct.pack(
            ">I", ble_crc24(payload))[1:]
        self.ble_pcap.record(phdr + air)
        if len(payload) < 8:
            return
        adva = payload[2:8].hex(":")
        rssi_i = int(parts[2])
        ad = payload[8:].hex()
        ch = parts[3]
        dev = self.dev_ble.get(adva)
        if dev is None:
            self.dev_ble[adva] = [rssi_i, 1, time.time()]
            self.ble_tree.insert("", tk.END, iid=adva, values=(adva, rssi_i, 1, ch, ad[:40]))
        else:
            dev[0] = max(dev[0], rssi_i)
            dev[1] += 1
            dev[2] = time.time()
            self.ble_tree.item(adva, values=(adva, dev[0], dev[1], ch, ad[:40]))
        self.ble_stats.set("帧数 %d，设备 %d" % (self.ble_frame_count, len(self.dev_ble)))

    # ---------------------------------------------------------------- SDR
    def _sdr_mode_changed(self):
        """抓包模式（片上频谱 / IQ 传回电脑）与 IQ 子方式（连续 / 突发）。
        抓包运行期间锁定选择器，必须先 STOP 才能切换。"""
        running = self.spec_running or self.iq_running
        streaming = self.cap_mode.get() == CAP_IQ
        cont = streaming and self.iq_mode.get() == IQM_CONT
        self.cap_mode_box.configure(state=tk.DISABLED if running else "readonly")
        self.iq_mode_box.configure(
            state=("readonly" if streaming and not running else tk.DISABLED))
        self.iq_rate_box.configure(
            state=("readonly" if cont and not running else tk.DISABLED))
        self.iq_n.configure(
            state=tk.NORMAL if (streaming and not cont and not running) else tk.DISABLED)
        self.btn_cap.configure(text="■ 停止抓包" if running else "开始抓包")
        self._iq_save_toggle()

    def toggle_capture(self):
        if not self.link:
            self.log("请先连接串口")
            return
        if self.spec_running or self.iq_running:
            self.stop_capture()
            return
        if self.cap_mode.get() == CAP_IQ:
            self.toggle_iq()
        else:
            self.toggle_spec()

    def stop_capture(self):
        if self.spec_running:
            self._stop_spec()
        if self.iq_running:
            self._stop_iq()
        self._sdr_mode_changed()

    def toggle_spec(self):
        if self.spec_running:
            self._stop_spec()
        else:
            try:
                bins = int(self.bins_var.get())
                rate = dict(RATES)[self.rate_var.get()]
                self.last_rate_msps = [80, 40, 20, 10, 8, 4][rate]
                self.last_freq_mhz = int(self.freq_var.get())
                self._stop_iq()
                self.apply_bandwidth(silent=True)
                self.link.spec_active = False
                r = self.link.command("SPEC 0 1 1 0 %d %d" % (rate, bins), timeout=8)
                if not r.startswith("SPEC"):
                    self.log("SPEC 启动失败: %s" % r)
                    return
                self.log("SPEC: %s" % r)
                self.link.spec_active = True
                self.spec_running = True
                self.spec_bins = bins
                self.wf_rows = []
                self._sdr_mode_changed()
            except Exception as e:
                self.log("SPEC 异常: %s" % e)

    def _stop_spec(self):
        if not self.spec_running:
            return
        try:
            self.link.spec_active = False
            self.link.ser.write(b"\n")  # stop byte
            time.sleep(0.1)
        except Exception as e:
            self.log("停止 SPEC: %s" % e)
        self.spec_running = False
        self._sdr_mode_changed()

    # ------------------------------------------------------------ IQ stream
    def _iq_save_toggle(self):
        streaming = self.cap_mode.get() == CAP_IQ
        running = self.spec_running or self.iq_running
        editable = streaming and self.iq_save_var.get()
        if getattr(self, "iq_save_chk", None) is not None:
            self.iq_save_chk.configure(
                state=tk.NORMAL if (streaming and not running) else tk.DISABLED)
        self.iq_path_entry.configure(state=tk.NORMAL if editable else tk.DISABLED)

    def toggle_iq(self):
        if self.iq_running:
            self._stop_iq()
            return
        continuous = "连续" in self.iq_mode.get()
        self.last_freq_mhz = int(self.freq_var.get()) if self.freq_var.get().isdigit() else 2412
        try:
            if continuous:
                rate = int(self.iq_rate.get())
                if rate < 10000:
                    raise ValueError("rate < 10000")
            else:
                n = int(self.iq_n.get())
                if not (256 <= n <= 16380):
                    raise ValueError("n out of range")
                rate = dict(RATES)[self.rate_var.get()]
        except ValueError as e:
            self.log("IQ 参数无效: %s" % e)
            return
        self._stop_spec()
        self.apply_bandwidth(silent=True)
        if continuous:
            r = self.link.command("IQS ON %d" % rate)
            if not r.startswith("OK"):
                self.log("连续 IQ 启动失败: %s" % r)
                return
            self.iq_continuous = True
            self.last_rate_msps = rate / 1e6
            self.iq_stats = {"captures": 0, "bytes": 0, "started": time.time()}
            self._iq_buf = bytearray()
            self.iq_running = True
            self._sdr_mode_changed()
            self.log("连续 IQ 流启动 (%d S/s, 无间隙; 速率须同时整除 240MHz 与 80MHz)" % rate)
            return
        self.iq_continuous = False
        self.last_rate_msps = [80, 40, 20, 10, 8, 4][rate]
        self.iq = IQStreamer(self, n, rate)
        if self.iq_save_var.get():
            try:
                self.iq.attach_file(self.iq_path.get())
                self.log("IQ 保存 -> %s" % self.iq_path.get())
            except Exception as e:
                self.log("IQ 文件打开失败: %s" % e)
        self.iq_stats = {"captures": 0, "bytes": 0, "started": time.time()}
        self.iq.start()
        self.iq_running = True
        self._sdr_mode_changed()
        self.log("IQ 流启动 (n=%d rate=%d, 快照间有 RF 间隙)" % (n, rate))

    def _stop_iq(self):
        if not self.iq_running:
            return
        self.iq_running = False
        if getattr(self, "iq_continuous", False):
            try:
                self.link.command("IQS OFF", 5)
                self.log(self.link.command("IQS?", 4))
            except Exception as e:
                self.log("停止连续 IQ: %s" % e)
            self._iq_close_file()
        elif getattr(self, "iq", None):
            self.iq.stop()
            self.iq = None
        self._sdr_mode_changed()

    def _iq_account(self, nbytes):
        st = self.iq_stats
        st["captures"] += 1
        st["bytes"] += nbytes
        dt = max(0.1, time.time() - st["started"])
        self.iq_stats_var.set("IQ %d 帧 %.0f B/s" % (st["captures"], st["bytes"] / dt))

    def _on_iq_frame(self, codes, nbytes, account=True):
        """Called (GUI thread) with one local-FFT spectrum row."""
        n = len(codes)
        self.spec_shifted = list(codes[n // 2:]) + list(codes[:n // 2])
        self.spec_center_mhz = self.last_freq_mhz
        self.spec_span_mhz = self.last_rate_msps * 1.0
        self.wf_rows.insert(0, bytes(self.spec_shifted))
        del self.wf_rows[self.wf_height:]
        if account:
            self._iq_account(nbytes)
        if self._redraw_job is None:
            self._redraw_job = self.after(self.redraw_ms, self._draw_pending)

    def _iq_close_file(self):
        if getattr(self, "iq_fout", None):
            try:
                self.iq_fout.close()
            except Exception:
                pass
            self.iq_fout = None

    def apply_freq(self):
        if not self.link:
            return
        was_spec = self.spec_running
        was_iq = self.iq_running
        if was_spec:
            self._stop_spec()
        if was_iq:
            self._stop_iq()
        try:
            self.last_freq_mhz = int(self.freq_var.get())
            r = self.link.command("FREQ %d" % self.last_freq_mhz)
            self.log("FREQ -> %s" % r)
        except Exception as e:
            self.log("FREQ 异常: %s" % e)
        if self.mode == "SDR":
            if was_spec:
                self.toggle_spec()
            elif was_iq:
                self.toggle_iq()

    def apply_bandwidth(self, silent=False):
        """BANDWIDTH 0 = open (widest), BANDWIDTH <MHz> = analog filter.
        Only safe in SDR ownership; skipped while a monitor owns the RF."""
        if not self.link or self.mode not in (None, "SDR"):
            if not silent:
                self.log("带宽设置仅在 SDR 模式可用")
            return
        try:
            if "Open" in self.bw_var.get():
                cmd = "BANDWIDTH 0"
            else:
                mhz = int(self.bw_mhz.get())
                if not (11 <= mhz <= 48):
                    raise ValueError("MHz 需在 11-48")
                cmd = "BANDWIDTH %d" % mhz
            r = self.link.command(cmd)
            if not silent:
                self.log("%s -> %s" % (cmd, r))
        except ValueError as e:
            self.log("带宽参数无效: %s" % e)
        except Exception as e:
            self.log("BANDWIDTH 异常: %s" % e)

    def _on_spec(self, det, gain, log2n, bins, crc_ok):
        n = len(bins)
        # The wire carries power codes in NATURAL FFT order (docs/spectrum.md:
        # "Power codes in natural FFT order"); only the fftshift moves DC to
        # the center for display.
        self.spec_shifted = list(bins[n // 2:]) + list(bins[:n // 2])
        self.spec_center_mhz = self.last_freq_mhz
        self.spec_span_mhz = self.last_rate_msps * 1.0
        self.wf_rows.insert(0, bytes(self.spec_shifted))
        del self.wf_rows[self.wf_height:]
        if self._redraw_job is None:
            self._redraw_job = self.after(self.redraw_ms, self._draw_pending)

    def _toggle_display_panel(self):
        if self.disp_open:
            self.disp_panel.pack_forget()
            self.btn_disp.configure(text="▶ 显示")
        else:
            self.disp_panel.pack(side=tk.TOP, fill=tk.X, pady=2,
                                 before=self.mid_frame)
            self.btn_disp.configure(text="▼ 显示")
        self.disp_open = not self.disp_open

    def _wf_level_changed(self):
        """Floor/Range stretch the 0-255 power codes onto the colour ramp and
        repaint the legend, so the waterfall shows which colour is which level
        (codes are 20*log10|X| dB, relative)."""
        floor = max(0, min(255, int(self.wf_floor.get())))
        rng = max(1, min(255, int(self.wf_range.get())))
        top = min(255, floor + rng)
        if getattr(self, "wf_floor_txt", None) is not None:
            self.wf_floor_txt.set(str(floor))
            self.wf_range_txt.set(str(rng))
        if getattr(self, "disp_summary", None) is not None:
            self.disp_summary.configure(
                text="色阶 %d → %d dB（相对）· Floor %d / Range %d"
                     % (floor, top, floor, rng))
        self._wf_lut = [COLORMAP_B[min(255, max(0, (i - floor) * 255 // rng))]
                        for i in range(256)]
        if getattr(self, "wf_title", None) is not None:
            self.wf_title.configure(
                text="瀑布（顶部为最新一行） 色阶 %d → %d dB（相对）"
                     % (floor, top))
        if getattr(self, "wf_legend", None) is not None:
            self._draw_wf_legend()

    def _draw_wf_legend(self):
        c = self.wf_legend
        c.delete("all")
        h = self.wf_height
        w = 18
        floor = int(self.wf_floor.get())
        rng = max(1, int(self.wf_range.get()))
        lut = getattr(self, "_wf_lut", COLORMAP_B)
        for y in range(h):
            code = min(255, max(0, floor + rng * (h - 1 - y) // max(1, h - 1)))
            c.create_line(0, y, w, y, fill="#%02x%02x%02x" % tuple(lut[code]))
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            val = min(255, floor + int(rng * frac))
            y = int((h - 1) * (1 - frac))
            c.create_line(w, y, w + 5, y, fill="#c8c8c8")
            c.create_text(w + 7, y, text=str(val), anchor=tk.W, fill="#c8c8c8",
                          font=("", 8))
        c.create_text(w + 7, h - 9, text="dB", anchor=tk.W, fill="#888888",
                      font=("", 8))

    def _wf_auto_level(self):
        vals = []
        for r in self.wf_rows:
            vals.extend(r)
        if not vals:
            return
        vals.sort()
        n = len(vals)
        lo = vals[int(0.02 * (n - 1))]
        hi = vals[int(0.99 * (n - 1))]
        self.wf_floor.set(max(0, min(255, lo - 5)))
        self.wf_range.set(max(20, min(255, hi - lo + 10)))
        self._wf_level_changed()

    def _draw_pending(self):
        self._redraw_job = None
        self._draw_waterfall()
        self._draw_spectrum()

    def _draw_waterfall(self):
        # Follow the actual widget width so the image always fills the panel.
        w = self.wf_label.winfo_width()
        if w > 100:
            # Follow the panel: a capped width left a dead band between the
            # image and the colour legend on wide/high-DPI screens.
            self.wf_x = min(w, 2600)
        h = self.wf_height
        rows = self.wf_rows
        wf = self.wf_x
        ppm = bytearray(b"P6\n%d %d\n255\n" % (wf, h))
        get = getattr(self, "_wf_lut", COLORMAP_B).__getitem__
        for y in range(h):
            row = rows[y] if y < len(rows) else None
            if not row:
                ppm += WF_BG * wf
                continue
            last = len(row) - 1
            key = (wf, last)
            xmap = self._wf_xmap_cache.get(key)
            if xmap is None:
                # display column -> bin index, cached per (width, row length)
                xmap = [x * last // max(1, wf - 1) for x in range(wf)]
                self._wf_xmap_cache[key] = xmap
            ppm += b"".join(map(get, map(row.__getitem__, xmap)))
        self.wf_photo = tk.PhotoImage(data=bytes(ppm), format="PPM")
        self.wf_label.configure(image=self.wf_photo)

    def _draw_spectrum(self):
        c = self.spec_canvas
        c.delete("all")
        w = max(c.winfo_width(), 200)
        hgt = max(c.winfo_height() - 18, 100)
        data = self.spec_shifted
        if not data:
            return
        n = len(data)
        points = min(1024, max(256, n))
        floor = int(self.wf_floor.get())
        rng = max(1, int(self.wf_range.get()))
        pts = []
        for x in range(points):
            idx = x * (n - 1) // max(1, points - 1)
            v = min(255, max(0, data[idx]))
            y = hgt - max(0.0, min(1.0, (v - floor) / rng)) * (hgt - 8)
            pts.extend((x * (w - 1) / max(1, points - 1), y))
        c.create_line(*pts, fill="#39d353", width=1)
        # Frequency axis: bins span +/- span/2 around the center frequency.
        center = self.spec_center_mhz
        half = self.spec_span_mhz / 2.0
        top_code = min(255, floor + rng)
        c.create_text(6, 6, anchor=tk.NW, fill="#888888",
                      text="20·log10|X| dB (相对)")
        # 左侧功率刻度，与瀑布色标同一映射
        c.create_text(4, 22, anchor=tk.NW, fill="#666666", font=("", 8),
                      text=str(top_code))
        c.create_text(4, hgt - 2, anchor=tk.SW, fill="#666666", font=("", 8),
                      text=str(floor))
        c.create_text(w - 6, 6, anchor=tk.NE, fill="#888888",
                      text="FFT %d · span %.3f MHz" % (n, self.spec_span_mhz))
        c.create_text(6, hgt + 8, anchor=tk.W, fill="#aaaaaa",
                      text="%.1f MHz" % (center - half))
        c.create_text(w / 2, hgt + 8, anchor=tk.CENTER, fill="#aaaaaa",
                      text="%.1f MHz" % center)
        c.create_text(w - 6, hgt + 8, anchor=tk.E, fill="#aaaaaa",
                      text="%.1f MHz" % (center + half))
        c.create_line(w / 2, 0, w / 2, hgt, fill="#333355")


def main():
    app = App()
    app.refresh_ports()
    app.mainloop()


if __name__ == "__main__":
    main()
