#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mock_fw.py - Mock firmware transport for GUI testing.

FakeSerial: pyserial-compatible in-memory serial port.
FirmwareModel: threaded fake esp-sdr firmware speaking the burst protocol,
with fault injection (drop/bad-crc/truncate/sticky/slow/dead).
"""
import struct
import threading
import time
import zlib


def _crc(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def make_wifi_frame(seq, rssi=-65, ch=1, payload_extra=b""):
    """Radiotap(15) + beacon-ish 802.11 frame."""
    fc = 0x0080
    frame = struct.pack("<HH", fc, 0) + b"\xff" * 6 + b"\x02\x00\x00\x00\x00\x01" + \
        b"\x02\x00\x00\x00\x00\x02" + struct.pack("<H", seq & 0xFFFF) + payload_extra
    rt = bytes([0, 0, 15, 0, 0x2E, 0, 0, 0, 0x02, 2, 0x6C, 0x09, 0xC0, 0x00,
                rssi & 0xFF])
    return rt + frame


def make_ble_frame(seq, rssi=-70, adva=b"\xcf\x82\x3c\x51\x7b\x5f", ad=None):
    if ad is None:
        ad = bytes([2, 1, 0x06, 3, 0x19, 0xC0, 0x03, seq & 0xFF])
    pdu = bytes([0x00, 6 + len(ad)]) + adva + ad
    return pdu


def spc1_frame(seq, nbins=256):
    bins = bytes(((seq * 7 + i * 3) % 120 + (i % 11) for i in range(nbins)))
    hdr = b"SPC1" + struct.pack("<I", 1) + struct.pack("<Q", seq) + \
        struct.pack("<I", nbins * 2) + struct.pack("<H", 2) + bytes([8, 26]) + \
        b"\x00\x00" + bytes([8, 2])
    body = hdr + bins
    return body + struct.pack("<I", _crc(body))


class FakeSerial:
    """In-memory serial port. Model thread consumes written bytes and queues
    response bytes for read()."""

    def __init__(self, model=None):
        self.model = model
        self.is_open = False
        self.timeout = 0.1
        self._rx = bytearray()
        self._tx = bytearray()
        self._cond = threading.Condition()

    # -- pyserial API ------------------------------------------------------
    def open(self):
        self.is_open = True
        if self.model:
            self.model.attach(self)

    def close(self):
        self.is_open = False
        with self._cond:
            self._cond.notify_all()
        if self.model:
            self.model.detach()

    def read(self, n=1):
        deadline = time.time() + (self.timeout or 0.1)
        with self._cond:
            while not self._rx:
                if time.time() > deadline or not self.is_open:
                    return b""
                self._cond.wait(timeout=max(0.0, min(0.02, deadline - time.time())))
            out = bytes(self._rx[:n])
            del self._rx[:n]
            return out

    def write(self, data):
        if not self.is_open:
            raise OSError("port not open")
        with self._cond:
            self._tx += bytes(data)
            self._cond.notify_all()
        return len(data)

    def setDTR(self, v):
        pass

    def setRTS(self, v):
        pass

    def set_buffer_size(self, **kw):
        pass

    def reset_input_buffer(self):
        with self._cond:
            self._rx.clear()

    # -- model helpers -----------------------------------------------------
    def feed(self, data):
        """Model pushes bytes toward the host (the Link's read side)."""
        with self._cond:
            self._rx += data
            self._cond.notify_all()

    def pull_line(self, timeout=0.05):
        """Model fetches one host command line (without trailing \n)."""
        deadline = time.time() + timeout
        with self._cond:
            while b"\n" not in self._tx:
                if time.time() > deadline or not self.is_open:
                    return None
                self._cond.wait(timeout=0.01)
            line, _, rest = bytes(self._tx).partition(b"\n")
            self._tx = bytearray(rest)
            return line.decode(errors="replace").strip()

    def pending_bytes(self):
        with self._cond:
            n = len(self._tx)
        return n


class FirmwareModel:
    """Fake three-mode firmware. fault: None|'drop'|'badcrc'|'truncate'|
    'sticky'|'slow'|'dead'."""

    CAPS = ("CAPS VERSION GPIO SPEC SPECN SPECCAPS SPECSTAT DCT UARTBAUD RXLIMITS "
            "GAIN HWAGC IQ8 SERIALLEASE TUNEEXT MON WIFIMON BLEMON LPF LPF12 ALPF DUALSERIAL")

    def __init__(self):
        self.ser = None
        self.alive = True
        self.fault = None
        self.fault_every = 5
        self._n = 0
        self.wifi_on = False
        self.ble_on = False
        self.spec_on = False
        self.spec_stop = False
        self.reply_delay = 0.0
        self._rx_count = 0
        self.emitter = None

    def attach(self, ser):
        self.ser = ser
        self.alive = True
        self.emitter = threading.Thread(target=self._run, daemon=True)
        self.emitter.start()

    def detach(self):
        self.alive = False
        self.ser = None

    def _send(self, text):
        if self.fault == "drop" and self._n % self.fault_every == 0:
            self._n += 1
            return
        if self.fault == "slow":
            time.sleep(0.8)
        self.ser.feed(text.encode() + b"\n")

    def _send_binary(self, header, payload):
        if self.fault == "badcrc" and self._n % self.fault_every == 0:
            payload = payload[:-1] + bytes([payload[-1] ^ 0xFF])
        if self.fault == "truncate" and self._n % self.fault_every == 0:
            self.ser.feed(header.encode() + b"\n")
            self.ser.feed(payload[:max(0, len(payload) - 3)])
            self._n += 1
            return
        self.ser.feed(header.encode() + b"\n")
        self.ser.feed(payload)
        if self.fault == "sticky":
            pass
        self._n += 1

    def _run(self):
        seq = 0
        while self.alive and self.ser is not None:
            line = self.ser.pull_line(0.05)
            if line is None:
                continue
            if line == "":
                # A bare newline is the SPEC stop byte (the real firmware
                # drains it in its stream-stop check, it is not a command).
                if self.spec_on:
                    self.spec_on = False
                continue
            self._rx_count += 1
            if self.fault == "dead":
                continue
            self._handle(line, seq)
            seq += 1

    def _handle(self, line, seq):
        if line.startswith("SYNC "):
            self._send(line)
        elif line == "INFO":
            self._send("C5SDR 6 burst 16380")
        elif line == "CAPS":
            self._send(self.CAPS)
        elif line == "RELEASE" or line == "OK":
            self._send("OK")
        elif line.startswith("OUTPUT"):
            if line == "OUTPUT?":
                self._send("OUTPUT AUTO")
            else:
                self._send("OK")
        elif line == "MON?":
            self._send("MON WIFI:%s BLE:%s CH:1 DROPS:0/0" %
                       ("on" if self.wifi_on else "off", "on" if self.ble_on else "off"))
        elif line.startswith("MON WIFI ON"):
            parts = line.split()
            ch = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
            if ch > 13 and not (36 <= ch <= 177):
                self._send("ERR channel_range")
            else:
                self.wifi_on = True
                self._send("OK")
        elif line == "MON WIFI OFF":
            self.wifi_on = False
            self._send("OK")
        elif line.startswith("MON BLE ON"):
            parts = line.split()
            if len(parts) > 5:
                self._send("ERR mon_args")
            else:
                self.ble_on = True
                self._send("OK")
        elif line == "MON BLE OFF":
            self.ble_on = False
            self._send("OK")
        elif line.startswith("FREQ "):
            try:
                f = int(line.split()[1])
                self._send("OK" if 100 <= f <= 6000 else "ERR command")
            except Exception:
                self._send("ERR command")
        elif line.startswith("CAP16 ") or line.startswith("CAP20 "):
            fmt = line.split()[0]
            n, rate = int(line.split()[1]), int(line.split()[2])
            if n < 256 or n > 16380 or rate > 5:
                self._send("ERR command")
                return
            if self.wifi_on or self.ble_on:
                self._send("ERR mode_busy")
                return
            payload = bytes(((i * 37 + n) & 0xFF for i in range(n * 2)))
            self._send_binary("DATA %d %08x 100" % (n, _crc(payload)), payload)
        elif line.startswith("RXRUN"):
            self._send("END")
        elif line.startswith("SPEC"):
            parts = line.split()
            if len(parts) >= 7 and parts[6].isdigit():
                bins = int(parts[6])
                self._send("SPEC %d 80000000 256 2412" % bins)
                self.spec_on = True
                self.spec_stop = False
                threading.Thread(target=self._spec_stream, args=(bins,), daemon=True).start()
            else:
                self._send("ERR command")
        else:
            self._send("ERR command")
        if self.wifi_on and line.startswith("SPEC") is False:
            pass

    def _spec_stream(self, bins):
        seq = 0
        # monitor frames also flow while SPEC runs in real firmware they
        # can't (mode exclusivity); here SPEC only.
        while self.spec_on and not self.spec_stop and self.alive and self.ser is not None:
            self.ser.feed(spc1_frame(seq, bins))
            seq += 1
            if self.spec_stop:
                break
            # honor stop byte: any pending host line stops the stream
            if self.ser.pending_bytes() > 0:
                line = self.ser.pull_line(0.01)
                if line is not None:
                    self.spec_on = False
                    break
            time.sleep(0.01)

    def emit_wifi_burst(self, count, interval=0.02):
        for i in range(count):
            if not self.wifi_on or self.ser is None:
                return
            pl = make_wifi_frame(i, rssi=-60 - (i % 30))
            self.ser.feed(b"WIFIFRAME %d %d 1 %08x\n" % (len(pl), -60 - (i % 30), _crc(pl)))
            self.ser.feed(pl)
            time.sleep(interval)

    def emit_ble_burst(self, count, interval=0.03):
        for i in range(count):
            if not self.ble_on or self.ser is None:
                return
            pl = make_ble_frame(i, rssi=-70 - (i % 20))
            self.ser.feed(b"BLEFRAME %d %d 37 %08x\n" % (len(pl), -70 - (i % 20), _crc(pl)))
            self.ser.feed(pl)
            time.sleep(interval)


class MockLink:
    """Scriptable Link stand-in injected into the App for mode-logic tests."""

    def __init__(self, script=None, fail=None):
        self.script = script or {}
        self.fail = fail
        self.spec_active = False
        self.calls = []
        self.wifi_on = False
        self.ble_on = False
        self._ser = FakeSerial()
        self._ser.is_open = True  # writes from IQStreamer need an open port

    @property
    def ser(self):
        return self._ser

    def command(self, cmd, timeout=5):
        self.calls.append(cmd)
        if self.fail == "timeout":
            raise TimeoutError("mock timeout for %r" % cmd)
        if self.fail == "err_busy" and cmd.startswith("MON"):
            return "ERR busy"
        if cmd in self.script:
            v = self.script[cmd]
            return v() if callable(v) else v
        if cmd.startswith("MON WIFI ON"):
            self.wifi_on = True
            return "OK"
        if cmd == "MON WIFI OFF":
            self.wifi_on = False
            return "OK"
        if cmd.startswith("MON BLE ON"):
            self.ble_on = True
            return "OK"
        if cmd == "MON BLE OFF":
            self.ble_on = False
            return "OK"
        if cmd.startswith("SYNC"):
            return cmd
        if cmd == "INFO":
            return "C5SDR 6 burst 16380"
        if cmd == "MON?":
            return "MON WIFI:%s BLE:%s CH:1 DROPS:0/0" % (
                "on" if self.wifi_on else "off", "on" if self.ble_on else "off")
        if cmd.startswith("OUTPUT"):
            return "OK"
        if cmd.startswith("FREQ"):
            return "OK"
        if cmd.startswith("SPEC"):
            self.spec_active = True
            return "SPEC 256 80000000 256 2412"
        return "OK"

    def activate_data_port(self):
        pass

    def close(self):
        self.spec_active = False
