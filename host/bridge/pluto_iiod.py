#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pluto_iiod.py - 在 ESP32-C5 前面"冒充"一台 PlutoSDR（libiio/iiod 服务端）。

SDR# 的 PlutoSDR 插件（manawyrm/sdrsharp-plutosdr）通过 libiio 的 *网络* 后端
连接 `ip:<host>`，走 TCP 30431 的 iiod 文本协议。本进程实现该协议的最小子集：

    VERSION / PRINT / TIMEOUT / OPEN / CLOSE / READ / WRITE / READBUF

并伪造两台设备（与 ADALM-PLUTO 同名）：
  - `ad9361-phy`      : 调谐 / 增益 / 带宽 / 采样率 属性
  - `cf-ad9361-lpc`   : I/Q 扫描通道（int16 交错），由 ESP32-C5 的 IQS 流喂数据

命令映射（写属性 → 固件命令）：
    altvoltage0.frequency  → FREQ <MHz>          （C5 只有整 MHz，四舍五入并记日志）
    voltage0.sampling_frequency → IQS ON/OFF     （就近取 10k/20k/25k/40k/50k/80k）
    voltage0.rf_bandwidth  → BANDWIDTH <MHz>     （0=Open，11..48=Filter）
    voltage0.hardwaregain  → GAIN MANUAL <0..80> （C5 增益是 PHY 表项，非 dB）
    voltage0.gain_control_mode → GAIN HARDWARE / GAIN MANUAL

用法：
    python pluto_iiod.py                      # 真板 COM10，监听 127.0.0.1:30431
    python pluto_iiod.py --port COM10 --fake   # 无硬件，合成单音+噪声
    python pluto_iiod.py --listen 0.0.0.0:30431 --freq 2412 --rate 80000

注意：与 monitor_gui / rtl_tcp 桥接一样，串口租约同一时刻只能有一个持有者。
"""
import argparse
import math
import os
import socket
import struct
import sys
import threading
import time

import numpy as np

IIOD_PORT = 30431
LF = bytes([10])
C5_IQS_RATES = (10000, 20000, 25000, 40000, 50000, 80000)
BYTES_PER_SAMPLE = 4          # int16 I + int16 Q

XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<context name="pluto" description="@DESC@">
  <context-attribute name="hw_model" value="ESP32-C5 SDR" />
  <context-attribute name="hw_model_variant" value="0" />
  <context-attribute name="hw_serial" value="esp32c5-sdr-0001" />
  <context-attribute name="fw_version" value="esp-sdr" />
  <device id="ad9361-phy" name="ad9361-phy">
    <channel id="altvoltage0" name="RX_LO" type="output">
      <attribute name="frequency" filename="out_altvoltage0_frequency" />
      <attribute name="frequency_available" filename="out_altvoltage0_frequency_available" />
      <attribute name="phase_inversion" filename="out_altvoltage0_phase_inversion" />
    </channel>
    <channel id="altvoltage1" name="TX_LO" type="output">
      <attribute name="frequency" filename="out_altvoltage1_frequency" />
      <attribute name="frequency_available" filename="out_altvoltage1_frequency_available" />
      <attribute name="phase_inversion" filename="out_altvoltage1_phase_inversion" />
    </channel>
    <channel id="voltage0" name="voltage0" type="input">
      <attribute name="gain_control_mode" filename="in_voltage0_gain_control_mode" />
      <attribute name="gain_control_mode_available" filename="in_voltage0_gain_control_mode_available" />
      <attribute name="hardwaregain" filename="in_voltage0_hardwaregain" />
      <attribute name="hardwaregain_available" filename="in_voltage0_hardwaregain_available" />
      <attribute name="rf_bandwidth" filename="in_voltage0_rf_bandwidth" />
      <attribute name="rf_bandwidth_available" filename="in_voltage0_rf_bandwidth_available" />
      <attribute name="sampling_frequency" filename="in_voltage0_sampling_frequency" />
      <attribute name="sampling_frequency_available" filename="in_voltage0_sampling_frequency_available" />
      <attribute name="rf_port_select" filename="in_voltage0_rf_port_select" />
      <attribute name="rf_port_select_available" filename="in_voltage0_rf_port_select_available" />
      <attribute name="phase_inversion" filename="in_voltage0_phase_inversion" />
      <attribute name="bb_dc_offset_tracking_en" filename="in_voltage0_bb_dc_offset_tracking_en" />
      <attribute name="quadrature_tracking_en" filename="in_voltage0_quadrature_tracking_en" />
      <attribute name="rf_dc_offset_tracking_en" filename="in_voltage0_rf_dc_offset_tracking_en" />
      <attribute name="lo_leakage_tracking_en" filename="in_voltage0_lo_leakage_tracking_en" />
    </channel>
    <channel id="voltage1" name="voltage1" type="input">
      <attribute name="gain_control_mode" filename="in_voltage1_gain_control_mode" />
      <attribute name="gain_control_mode_available" filename="in_voltage1_gain_control_mode_available" />
      <attribute name="hardwaregain" filename="in_voltage1_hardwaregain" />
      <attribute name="hardwaregain_available" filename="in_voltage1_hardwaregain_available" />
      <attribute name="rf_bandwidth" filename="in_voltage1_rf_bandwidth" />
      <attribute name="rf_bandwidth_available" filename="in_voltage1_rf_bandwidth_available" />
      <attribute name="sampling_frequency" filename="in_voltage1_sampling_frequency" />
      <attribute name="sampling_frequency_available" filename="in_voltage1_sampling_frequency_available" />
      <attribute name="rf_port_select" filename="in_voltage1_rf_port_select" />
      <attribute name="rf_port_select_available" filename="in_voltage1_rf_port_select_available" />
      <attribute name="phase_inversion" filename="in_voltage1_phase_inversion" />
      <attribute name="bb_dc_offset_tracking_en" filename="in_voltage1_bb_dc_offset_tracking_en" />
      <attribute name="quadrature_tracking_en" filename="in_voltage1_quadrature_tracking_en" />
      <attribute name="rf_dc_offset_tracking_en" filename="in_voltage1_rf_dc_offset_tracking_en" />
      <attribute name="lo_leakage_tracking_en" filename="in_voltage1_lo_leakage_tracking_en" />
    </channel>
    <channel id="voltage2" name="voltage2" type="input">
      <attribute name="gain_control_mode" filename="in_voltage2_gain_control_mode" />
      <attribute name="gain_control_mode_available" filename="in_voltage2_gain_control_mode_available" />
      <attribute name="hardwaregain" filename="in_voltage2_hardwaregain" />
      <attribute name="hardwaregain_available" filename="in_voltage2_hardwaregain_available" />
      <attribute name="rf_bandwidth" filename="in_voltage2_rf_bandwidth" />
      <attribute name="rf_bandwidth_available" filename="in_voltage2_rf_bandwidth_available" />
      <attribute name="sampling_frequency" filename="in_voltage2_sampling_frequency" />
      <attribute name="sampling_frequency_available" filename="in_voltage2_sampling_frequency_available" />
      <attribute name="rf_port_select" filename="in_voltage2_rf_port_select" />
      <attribute name="rf_port_select_available" filename="in_voltage2_rf_port_select_available" />
      <attribute name="phase_inversion" filename="in_voltage2_phase_inversion" />
      <attribute name="bb_dc_offset_tracking_en" filename="in_voltage2_bb_dc_offset_tracking_en" />
      <attribute name="quadrature_tracking_en" filename="in_voltage2_quadrature_tracking_en" />
      <attribute name="rf_dc_offset_tracking_en" filename="in_voltage2_rf_dc_offset_tracking_en" />
      <attribute name="lo_leakage_tracking_en" filename="in_voltage2_lo_leakage_tracking_en" />
    </channel>
    <channel id="voltage3" name="voltage3" type="input">
      <attribute name="gain_control_mode" filename="in_voltage3_gain_control_mode" />
      <attribute name="gain_control_mode_available" filename="in_voltage3_gain_control_mode_available" />
      <attribute name="hardwaregain" filename="in_voltage3_hardwaregain" />
      <attribute name="hardwaregain_available" filename="in_voltage3_hardwaregain_available" />
      <attribute name="rf_bandwidth" filename="in_voltage3_rf_bandwidth" />
      <attribute name="rf_bandwidth_available" filename="in_voltage3_rf_bandwidth_available" />
      <attribute name="sampling_frequency" filename="in_voltage3_sampling_frequency" />
      <attribute name="sampling_frequency_available" filename="in_voltage3_sampling_frequency_available" />
      <attribute name="rf_port_select" filename="in_voltage3_rf_port_select" />
      <attribute name="rf_port_select_available" filename="in_voltage3_rf_port_select_available" />
      <attribute name="phase_inversion" filename="in_voltage3_phase_inversion" />
      <attribute name="bb_dc_offset_tracking_en" filename="in_voltage3_bb_dc_offset_tracking_en" />
      <attribute name="quadrature_tracking_en" filename="in_voltage3_quadrature_tracking_en" />
      <attribute name="rf_dc_offset_tracking_en" filename="in_voltage3_rf_dc_offset_tracking_en" />
      <attribute name="lo_leakage_tracking_en" filename="in_voltage3_lo_leakage_tracking_en" />
    </channel>
      <attribute name="filter_fir_en" filename="filter_fir_en" />
      <attribute name="ensm_mode" filename="ensm_mode" />
      <attribute name="ensm_mode_available" filename="ensm_mode_available" />
      <attribute name="calib_mode" filename="calib_mode" />
      <attribute name="calib_mode_available" filename="calib_mode_available" />
      <attribute name="trx_rate_governor" filename="trx_rate_governor" />
      <attribute name="trx_rate_governor_available" filename="trx_rate_governor_available" />
      <attribute name="xo_correction" filename="xo_correction" />
      <attribute name="dcxo_tune_coarse" filename="dcxo_tune_coarse" />
      <attribute name="dcxo_tune_fine" filename="dcxo_tune_fine" />
      <attribute name="phase_inversion" filename="phase_inversion" />
  </device>
  <device id="cf-ad9361-dds" name="cf-ad9361-dds">
    <channel id="voltage0" name="voltage0" type="output">
      <attribute name="phase_inversion" filename="out_voltage0_phase_inversion" />
      <attribute name="raw" filename="out_voltage0_raw" />
      <attribute name="scale" filename="out_voltage0_scale" />
    </channel>
    <channel id="voltage1" name="voltage1" type="output">
      <attribute name="phase_inversion" filename="out_voltage1_phase_inversion" />
      <attribute name="raw" filename="out_voltage1_raw" />
      <attribute name="scale" filename="out_voltage1_scale" />
    </channel>
    <channel id="voltage2" name="voltage2" type="output">
      <attribute name="phase_inversion" filename="out_voltage2_phase_inversion" />
    </channel>
    <channel id="voltage3" name="voltage3" type="output">
      <attribute name="phase_inversion" filename="out_voltage3_phase_inversion" />
    </channel>
    <attribute name="sampling_frequency" filename="sampling_frequency" />
  </device>
  <device id="cf-ad9361-lpc" name="cf-ad9361-lpc">
    <channel id="voltage0" name="voltage0" type="input">
      <scan-element index="0" format="le:S16/16>>0" />
      <attribute name="phase_inversion" filename="in_voltage0_phase_inversion" />
    </channel>
    <channel id="voltage1" name="voltage1" type="input">
      <scan-element index="1" format="le:S16/16>>0" />
      <attribute name="phase_inversion" filename="in_voltage1_phase_inversion" />
    </channel>
    <channel id="voltage2" name="voltage2" type="input" />
    <channel id="voltage3" name="voltage3" type="input" />
      <attribute name="sampling_frequency" filename="sampling_frequency" />
  </device>
</context>"""


DEBUG_IIOD = os.environ.get("IIOD_DEBUG", "") not in ("", "0")


def _setup_console():
    """Windows 控制台默认 GBK，中文日志会变乱码：切到 UTF-8 并重配 stdout。"""
    if os.name == "nt":
        try:
            os.system("chcp 65001 >nul")
        except Exception:
            pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass



# 固定值的 IIO 属性（MATLAB 的 Pluto 驱动会读它们做能力探测；读失败会直接报错）
CONST_ATTRS = {
    "phase_inversion": "0",
    "bb_dc_offset_tracking_en": "1",
    "quadrature_tracking_en": "1",
    "rf_dc_offset_tracking_en": "1",
    "lo_leakage_tracking_en": "0",
    "gain_control_mode_available": "manual fast_attack slow_attack hybrid",
    "hardwaregain_available": "[-3.000000 0.500000 71.000000]",
    "sampling_frequency_available": "[10000 1 80000]",
    "rf_bandwidth_available": "[11000000 1 48000000]",
    "rf_port_select": "A_BALANCED",
    "rf_port_select_available": ("A_BALANCED B_BALANCED C_BALANCED A_N A_P B_N B_P "
                                 "C_N C_P TX_MONITOR1 TX_MONITOR2 TX_MONITOR1_2"),
    "ensm_mode": "fdd",
    "ensm_mode_available": "sleep wait alert fdd fdd_alert rx tx",
    "calib_mode": "auto",
    "calib_mode_available": "auto manual manual_tx_quad rssi tx_quad rf_dc_offs",
    "trx_rate_governor": "nominal",
    "trx_rate_governor_available": "nominal highest_2msps",
    "xo_correction": "0",
    "dcxo_tune_coarse": "0",
    "dcxo_tune_fine": "0",
    "frequency_available": "[100000000 1 6000000000]",
    "filter_fir_en": "0",
}

def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def nearest_rate(hz):
    return min(C5_IQS_RATES, key=lambda r: abs(r - hz))


# ---------------------------------------------------------------------------
# 样本源
# ---------------------------------------------------------------------------
class Stream:
    """单客户端样本缓冲：设备线程写入 int16 交错块，网络线程按需取出。

    `rate_in` 是固件实际速率；使用方通过 set_rate() 声明它想要的速率，
    读样本时按线性插值重采样（C5 无 DDC，只有 10k..80k 这几档）。
    """

    def __init__(self, rate_in):
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.buf = np.zeros(0, dtype=np.int16)     # 交错 I,Q
        self.rate_in = float(rate_in)
        self.rate_out = float(rate_in)
        self.pos = 0.0                              # 重采样相位（输入样本）
        self.dropped = 0

    def push(self, iq_i16):
        with self.lock:
            self.buf = np.concatenate((self.buf, iq_i16))
            # 保留 ~2 s
            keep = int(self.rate_in * 2) * 2
            if self.buf.size > keep:
                cut = (self.buf.size - keep) & ~1
                self.buf = self.buf[cut:]
                self.pos = max(0.0, self.pos - cut / 2.0)
            self.cond.notify_all()

    def clear(self):
        """丢弃缓冲（换频/换速率后旧样本已无意义）。"""
        with self.lock:
            self.buf = np.zeros(0, dtype=np.int16)
            self.pos = 0.0

    def set_rate(self, rate_out):
        if rate_out and rate_out > 0:
            self.rate_out = float(rate_out)

    def read(self, n_bytes):
        """返回 n_bytes 的 int16 交错数据。

        刚调谐/换速率时缓冲可能是空的：最多等 1.5 s 让数据到位，避免把一帧零发给客户端。
        """
        n_out = max(1, int(n_bytes) // BYTES_PER_SAMPLE)
        need = min(int((n_out - 1) * self.rate_in / max(1e-9, self.rate_out)) + 8, 8192)
        deadline = time.time() + 1.5
        with self.cond:
            while self.buf.size // 2 < need and time.time() < deadline:
                self.cond.wait(0.05)
        with self.lock:
            buf = self.buf
            n_in = buf.size // 2
            if n_in < 4:
                return np.zeros(n_out * 2, dtype=np.int16).tobytes()
            ratio = self.rate_in / self.rate_out   # 每个输出样本跨多少输入样本
            pos = min(self.pos, float(n_in - 2))
            idx = pos + np.arange(n_out, dtype=np.float64) * ratio
            last = n_in - 1
            if idx[-1] > last:                       # 欠采样：钳到最后一个样本
                idx = np.clip(idx, 0.0, float(last))
            i0 = np.floor(idx).astype(np.int64)
            i1 = np.minimum(i0 + 1, last)
            frac = (idx - i0)[:, None]
            a = np.stack([buf[2 * i0], buf[2 * i0 + 1]], axis=1).astype(np.float32)
            b = np.stack([buf[2 * i1], buf[2 * i1 + 1]], axis=1).astype(np.float32)
            out = np.clip(np.rint(a + (b - a) * frac), -32768, 32767).astype("<i2")
            # 丢弃已消费的输入，保留一个样本给下一次插值
            consumed = int(max(0, min(i0[-1], n_in - 2)))
            if consumed > 0:
                self.buf = buf[consumed * 2:]
                self.pos = pos + n_out * ratio - consumed
                if self.pos < 0.0:
                    self.pos = 0.0
            else:
                self.pos = pos + n_out * ratio
            return out.tobytes()


class FakeDevice:
    """--fake：fs/8 单音 + 噪声，不需要硬件。"""

    def __init__(self, stream, rate):
        self.stream = stream
        self.rate = rate
        self.freq_hz = 2412_000_000
        self.gain_idx = 40
        self.alive = True
        self.n = 0
        rng = np.random.default_rng(1234)
        self.rng = rng
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while self.alive:
            m = max(1, int(self.rate / 50))
            t = (self.n + np.arange(m)) / float(self.rate)
            self.n += m
            # 单音放在 fs/8（40 kHz 恰好是 80 kS/s 的奈奎斯特点，插值后很难看）
            ton = self.rate / 8.0
            z = (4000 * np.exp(2j * np.pi * ton * t)
                 + 20 * (self.rng.standard_normal(m) + 1j * self.rng.standard_normal(m)))
            iq = np.empty(m * 2, dtype=np.int16)
            iq[0::2] = np.clip(np.rint(z.real), -32768, 32767)
            iq[1::2] = np.clip(np.rint(z.imag), -32768, 32767)
            self.stream.push(iq)
            time.sleep(0.02)

    def pause(self):
        """没有客户端了：停流、放租约、**关掉串口**，让 monitor_gui 能接管。"""
        if self._closed:
            return
        try:
            if self.streaming:
                self.cmd("IQS OFF", b"OK", 3.0)
            self.streaming = False
            self.cmd("RELEASE", b"OK", 1.5)
        except Exception:
            pass
        self.alive = False
        time.sleep(0.4)
        try:
            self.s.close()
        except Exception:
            pass
        self._closed = True
        log("no client: IQS OFF + RELEASE, serial port released")

    def resume(self):
        if not self._closed:
            if not self.streaming:
                self._iqs_on(self.rate)
            return
        self.s = self._open_serial()
        self._closed = False
        self.alive = True
        self._text = b""
        self._pending = None
        self._handshake()
        threading.Thread(target=self.run, daemon=True).start()
        self._iqs_on(self.rate)
        log("client connected: serial port reopened")

    def set_freq(self, hz):
        self.freq_hz = hz

    def set_rate(self, hz):
        self.stream.set_rate(hz)

    def set_gain(self, idx):
        self.gain_idx = idx

    def set_bandwidth(self, hz):
        pass

    def pause(self):
        pass

    def resume(self):
        pass

    def close(self):
        self.alive = False


class C5Device:
    """ESP32-C5 连续 IQ（IQS）串口客户端。

    单线程模型：`run()` 是唯一读串口的地方；命令经 `_pending` 插槽由该线程发送并
    收集应答（Windows 上两个线程同时读同一个 CDC 端口会直接 ERROR_BAD_COMMAND）。
    帧与文本严格分流：帧进 Stream，其余字节进 `self._text`，所以 "OK" 不会被帧
    头部扫描吞掉。
    """

    def __init__(self, port, stream, full_scale=None, baud=115200):
        import serial
        self.stream = stream
        self.port = port
        self.baud = baud
        self._closed = False
        self.s = self._open_serial()
        self.lock = threading.Lock()
        self.alive = True
        self.rate = int(stream.rate_in)
        self.freq_hz = 2412_000_000
        self.gain_idx = 40
        self.bw_mhz = 0
        self.full_scale = full_scale
        self._dc = (0.0, 0.0)
        self.streaming = False
        self.stats = dict(frames=0, crc=0, short=0, resync=0, bytes=0)
        self._pending = None
        self._reply = b""
        self._text = b""
        self._reply_ev = threading.Event()
        self._handshake()
        threading.Thread(target=self.run, daemon=True).start()

    def _open_serial(self):
        """打开串口：先压低 DTR/RTS，避免开/关端口把板子复位。"""
        import serial
        s = serial.Serial()
        s.port = self.port
        s.baudrate = self.baud
        s.timeout = 0.2
        s.write_timeout = 5
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

    # ---- 握手（在读线程启动前完成） -------------------------------------
    def _read_text(self, t):
        end = time.time() + t
        raw = b""
        while time.time() < end:
            raw += self.s.read(65536)
        out = []
        for ln in raw.split(LF):
            txt = bytes(x for x in ln if 9 <= x < 127).decode("ascii", "replace").strip()
            if txt:
                out.append(txt)
        return chr(10).join(out)

    def _drain_until_quiet(self, max_t=2.0, quiet=0.2):
        end = time.time() + max_t
        last = time.time()
        while time.time() < end:
            d = self.s.read(65536)
            if d:
                last = time.time()
            elif time.time() - last > quiet:
                break

    def _handshake(self):
        # 上次异常退出可能把固件留在 IQS 流里：先停流、清租约
        for pre in (b"IQS OFF", b"RELEASE"):
            self.s.write(pre + LF)
            time.sleep(0.3)
        self.s.reset_input_buffer()
        self._drain_until_quiet()
        self.s.reset_input_buffer()
        self.s.write(b"SYNC %d" % (int(time.time()) % 10000) + LF)
        time.sleep(0.3)
        self.s.reset_input_buffer()
        text = ""
        for _ in range(3):
            self.s.write(b"MON?" + LF)
            time.sleep(0.4)
            text = self._read_text(0.5)
            if "MON WIFI" in text:
                break
        if "IQ:on" in text:
            for _ in range(3):
                self.s.write(b"IQS OFF" + LF)
                time.sleep(0.6)
                self._drain_until_quiet()
                self.s.write(b"MON?" + LF)
                time.sleep(0.4)
                text = self._read_text(0.5)
                if "IQ:off" in text:
                    break
        self.s.reset_input_buffer()
        if "WIFI:off" not in text or "BLE:off" not in text or "IQ:off" not in text:
            raise RuntimeError("serial port busy with another mode: %r" % text[-160:])
        log("handshake OK: %s" % text.splitlines()[-1][:80])

    # ---- 命令（经读线程串行化） -----------------------------------------
    def cmd(self, text, expect=b"OK", t=1.0):
        if not self.alive:
            return b""
        self._reply_ev.clear()
        self._pending = (text, expect, time.time() + t)
        if not self._reply_ev.wait(t + 1.5):
            self._pending = None
            log("command timeout: %s" % text)
            return b""
        if DEBUG_IIOD:
            log("cmd %r -> %r" % (text, self._reply[:60]))
        return self._reply

    # ---- 帧/文本分流 -----------------------------------------------------
    def _take_frame(self, buf):
        """buf 以 'IQFRAME ' 开头；整帧可用则推进并返回剩余，否则返回 None。"""
        nl = buf.find(LF)
        if nl < 0:
            return None
        head = buf[:nl].decode("ascii", "replace").split()
        try:
            n = int(head[1])
        except (IndexError, ValueError):
            self.stats["resync"] += 1
            return buf[nl + 1:]
        if len(buf) < nl + 1 + n:
            return None
        payload = buf[nl + 1:nl + 1 + n]
        self.stats["frames"] += 1
        self.stats["bytes"] += n
        if n != 1024:
            self.stats["short"] += 1
        try:                                   # CRC32(zlib) 只覆盖载荷
            crc_field = int(head[3], 16) if len(head) > 3 else None
        except ValueError:
            crc_field = None
        if crc_field is not None:
            import zlib
            if (zlib.crc32(payload) & 0xFFFFFFFF) != crc_field:
                self.stats["crc"] += 1
        z = np.frombuffer(payload, dtype="<i4").astype(np.float32) / 64.0
        if z.size % 2:
            z = z[:-1]
        if self.full_scale is None:
            self.full_scale = max(1.0, float(np.percentile(np.abs(z), 99.0))) * 4.0
            log("full-scale reference (auto) = %.0f; output amplitude is uncalibrated"
                % self.full_scale)
        # 残余直流：每块减均值（EMA 平滑，避免块间跳变）
        i_dc = z[0::2].mean() if z.size else 0.0
        q_dc = z[1::2].mean() if z.size else 0.0
        self._dc = (0.9 * self._dc[0] + 0.1 * i_dc, 0.9 * self._dc[1] + 0.1 * q_dc)
        z[0::2] -= self._dc[0]
        z[1::2] -= self._dc[1]
        # full_scale 对应 ±0.6 FS，留 4 dB 余量（幅度未校准，见 README）
        z = z / self.full_scale * (32767.0 * 0.6)
        self.stream.push(np.clip(np.rint(z), -32768, 32767).astype(np.int16))
        return buf[nl + 1 + n:]

    def _pump(self, buf):
        """帧进流；其余字节进 self._text（命令应答）。返回残余缓冲。"""
        while True:
            i = buf.find(b"IQFRAME ")
            if i < 0:
                keep = 0
                for k in range(1, min(8, len(buf)) + 1):
                    if b"IQFRAME "[:k] == buf[-k:]:
                        keep = k
                if keep:
                    self._text = (self._text + buf[:-keep])[-8192:]
                    return buf[-keep:]
                self._text = (self._text + buf)[-8192:]
                return b""
            self._text = (self._text + buf[:i])[-8192:]
            rest = self._take_frame(buf[i:])
            if rest is None:
                return buf[i:]
            buf = rest

    def run(self):
        buf = b""
        while self.alive:
            pend = self._pending
            if pend is not None:
                self._pending = None
                text, expect, deadline = pend
                try:
                    self.s.write(text.encode() + LF)
                except Exception as e:
                    log("serial write failed: %s" % e)
                    self._reply = b""
                    self._reply_ev.set()
                    continue
                mark = len(self._text)
                while time.time() < deadline:
                    try:
                        d = self.s.read(4096)
                    except Exception as e:
                        log("serial read failed: %s" % e)
                        self.alive = False
                        break
                    if d:
                        buf = self._pump(buf + d)
                    if expect and expect in self._text[mark:]:
                        break
                self._reply = self._text[mark:][:256]
                self._reply_ev.set()
                continue
            try:
                d = self.s.read(65536)
            except Exception as e:
                log("serial read failed: %s" % e)
                self.alive = False
                break
            if d:
                buf = self._pump(buf + d)

    # ---- 主机侧控制 ------------------------------------------------------
    def start(self, rate, freq_hz):
        """启动顺序：先调谐（未开流时 FREQ 才不会被 PHY 占用拒绝），再开 IQS。"""
        mhz = max(100, min(6000, int(round(freq_hz / 1e6))))
        self.freq_hz = mhz * 1_000_000
        r = self.cmd("FREQ %d" % mhz, b"OK", 8.0)
        log("initial tune FREQ %d MHz %s" % (mhz, "OK" if b"OK" in r else "ERR"))
        self.cmd("BANDWIDTH 0", b"OK", 6.0)
        self._iqs_on(rate)

    def _stop_for_phy(self):
        """IQS 流期间固件拒绝 FREQ（PHY 被 RX 测试模式占用），改参数前必须停流。"""
        rate = self.rate
        self.cmd("IQS OFF", b"OK", 2.0)
        return rate

    def _iqs_on(self, rate):
        self.cmd("IQS OFF", b"OK", 2.0)
        self.stream.clear()          # 丢掉上一次流的残留
        r = self.cmd("IQS ON %d" % rate, b"OK", 4.0)
        if b"OK" not in r:
            raise RuntimeError("IQS ON %d rejected: %r" % (rate, r[-80:]))
        self.rate = rate
        self.stream.rate_in = float(rate)
        self.streaming = True
        log("firmware IQS ON %d S/s" % rate)

    def pause(self):
        """没有客户端了：停流、放租约、**关掉串口**，让 monitor_gui 能接管。"""
        if self._closed:
            return
        try:
            if self.streaming:
                self.cmd("IQS OFF", b"OK", 3.0)
            self.streaming = False
            self.cmd("RELEASE", b"OK", 1.5)
        except Exception:
            pass
        self.alive = False
        time.sleep(0.4)
        try:
            self.s.close()
        except Exception:
            pass
        self._closed = True
        log("no client: IQS OFF + RELEASE, serial port released")

    def resume(self):
        if not self._closed:
            if not self.streaming:
                self._iqs_on(self.rate)
            return
        self.s = self._open_serial()
        self._closed = False
        self.alive = True
        self._text = b""
        self._pending = None
        self._handshake()
        threading.Thread(target=self.run, daemon=True).start()
        self._iqs_on(self.rate)
        log("client connected: serial port reopened")

    def set_freq(self, hz):
        mhz = max(100, min(6000, int(round(hz / 1e6))))
        err = hz / 1e6 - mhz
        with self.lock:
            rate = self._stop_for_phy()
            self.freq_hz = mhz * 1_000_000
            r = self.cmd("FREQ %d" % mhz, b"OK", 8.0)
            self._iqs_on(rate)
        log("tune %.3f MHz -> FREQ %d MHz (delta=%+.3f MHz) %s"
            % (hz / 1e6, mhz, err, "OK" if b"OK" in r else "ERR"))

    def set_rate(self, hz):
        want = nearest_rate(int(hz))
        if int(hz) != want:
            log("requested rate %d unsupported; firmware stays at %d S/s "
                "(host interpolates to %d)" % (int(hz), want, int(hz)))
        if want != self.rate:
            with self.lock:
                self._iqs_on(want)
        self.stream.set_rate(hz)

    def set_gain(self, idx):
        idx = max(0, min(80, int(idx)))
        with self.lock:
            self.gain_idx = idx
            r = self.cmd("GAIN MANUAL %d" % idx, b"OK", 6.0)
        log("gain -> GAIN MANUAL %d %s" % (idx, "OK" if b"OK" in r else "ERR"))

    def set_bandwidth(self, hz):
        mhz = int(round(hz / 1e6)) if hz else 0
        if mhz and not (11 <= mhz <= 48):
            mhz = max(11, min(48, mhz))
        if mhz == self.bw_mhz:
            return
        with self.lock:
            self.bw_mhz = mhz
            r = self.cmd("BANDWIDTH %d" % mhz, b"OK", 8.0)
        log("bandwidth -> BANDWIDTH %d MHz %s" % (mhz, "OK" if b"OK" in r else "ERR"))

    def close(self):
        self.alive = False
        time.sleep(0.3)
        try:
            self.s.write(b"IQS OFF" + LF)
            time.sleep(0.4)
            self.s.write(b"RELEASE" + LF)
            time.sleep(0.3)
        except Exception:
            pass
        try:
            self.s.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# iiod 服务端
# ---------------------------------------------------------------------------
class Iiod:
    """libiio v0.21 网络协议的最小子集（SDR# Pluto 插件实际用到的部分）。"""

    def __init__(self, dev, stream, host, port, freq_hz, rate_hz):
        self.dev = dev
        self.stream = stream
        self.freq_hz = freq_hz
        self.rate_hz = rate_hz
        self.bw_hz = 20_000_000
        self.gain_db = 40.0
        self.gain_mode = "manual"
        self.clients = 0
        self.mask_every_read = True     # 实测 libiio 0.14/0.25 每次 READBUF 都带掩码
        self.lock = threading.Lock()
        self.srv = socket.socket()
        # Windows 上 SO_REUSEADDR 会让第二个实例也能绑同一端口（两个监听、客户端随机连），
        # 所以只在非 Windows 打开它。
        if os.name != "nt":
            self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((host, port))
        self.srv.listen(2)
        log("iiod 监听 %s:%d（SDR# 里用 PlutoSDR 源，URI = ip:%s）"
            % (host, port, host if host != "0.0.0.0" else "127.0.0.1"))

    # -- 属性存取 ---------------------------------------------------------
    def attrs_xml(self):
        return (XML_TEMPLATE
                .replace("@FREQ_HZ@", str(int(self.freq_hz)))
                .replace("@RATE_HZ@", str(int(self.rate_hz)))
                .replace("@BW_HZ@", str(int(self.bw_hz)))
                .replace("@GAIN_DB@", "%.6f" % self.gain_db)
                .replace("@GAIN_MODE@", self.gain_mode))

    def read_attr(self, dev_id, chan, attr):
        if dev_id == "ad9361-phy":
            if attr == "frequency":
                if chan == "altvoltage1":          # TX LO，占位值
                    return "2400000000"
                return str(int(self.freq_hz))
            if attr == "sampling_frequency":
                return str(int(self.rate_hz))
            if attr == "rf_bandwidth":
                return str(int(self.bw_hz))
            if attr == "gain_control_mode":
                return self.gain_mode
            if attr == "hardwaregain":
                return "%.6f" % self.gain_db
        if dev_id == "cf-ad9361-lpc" and attr == "sampling_frequency":
            return str(int(self.rate_hz))
        return CONST_ATTRS.get(attr)

    def write_attr(self, dev_id, chan, attr, value):
        # 属性写入是 NUL 结尾的字符串（libiio 的 write 走 strncpy 语义）
        value = (value or "").replace(chr(0), "").strip()
        if DEBUG_IIOD:
            log("write_attr(%r, %r, %r, %r)" % (dev_id, chan, attr, value))
        try:
            if dev_id == "ad9361-phy":
                if chan == "altvoltage0" and attr == "frequency":
                    hz = int(float(value))
                    self.freq_hz = hz
                    self.dev.set_freq(hz)
                    return 0
                if chan == "voltage0":
                    if attr == "sampling_frequency":
                        hz = int(float(value))
                        self.rate_hz = hz
                        self.dev.set_rate(hz)
                        return 0
                    if attr == "rf_bandwidth":
                        hz = int(float(value))
                        self.bw_hz = hz
                        self.dev.set_bandwidth(hz)
                        return 0
                    if attr == "gain_control_mode":
                        self.gain_mode = value
                        if value == "manual":
                            self.dev.set_gain(int(round(self.gain_db / 100.0 * 80)))
                        else:
                            self.dev.set_gain(40)
                        return 0
                    if attr == "hardwaregain":
                        db = float(value)
                        self.gain_db = db
                        self.dev.set_gain(int(round(max(0.0, db) / 100.0 * 80)))
                        return 0
                if attr == "filter_fir_en":
                    return 0
        except (TypeError, ValueError):
            return -22
        log("未处理的属性写入 %s %s.%s = %r" % (dev_id, chan or "-", attr, value))
        return 0

    # -- 连接处理 ---------------------------------------------------------
    def serve_forever(self):
        while True:
            c, addr = self.srv.accept()
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # libiio 使用两条连接：一条控制（PRINT/READ/WRITE），每个设备再各开
            # 一条数据连接（OPEN/READBUF），因此这里必须允许多连接。
            with self.lock:
                self.clients += 1
            threading.Thread(target=self.handle, args=(c, addr), daemon=True).start()

    def handle(self, c, addr):
        log("SDR# 客户端连入 %s" % (addr,))
        first_read = True
        rfile = c.makefile("rb")
        opened = False
        try:
            while True:
                line = rfile.readline()
                if not line:
                    break
                cmd = line.decode("ascii", "replace").strip()
                if not cmd:
                    continue
                if DEBUG_IIOD:
                    log("<< %s" % cmd)
                parts = cmd.split()
                op = parts[0].upper()
                if op == "VERSION":
                    c.sendall(b"0.21.esp-sdr\n")
                elif op == "PRINT":
                    xml = self.attrs_xml().encode()
                    c.sendall(b"%d\n" % len(xml))
                    c.sendall(xml)
                    c.sendall(b"\n")
                elif op == "TIMEOUT":
                    c.sendall(b"0\n")
                elif op in ("SET", "GET"):
                    # SET <dev> BUFFERS_COUNT <n> / SET <dev> TRIGGER <name>
                    # GET <dev> TRIGGER  -> 0 = no trigger
                    if op == "SET":
                        # 新版 libiio（>=0.20，Python/MATLAB）会先发 SET：它每次
                        # READBUF 都期望长度后跟通道掩码；老版（SDR# 0.14）只第一次要。
                        self.mask_every_read = True
                    c.sendall(b"0" + bytes([10]))
                elif op == "OPEN":
                    if self.dev is not None:
                        self.dev.resume()
                    opened = True
                    first_read = True
                    c.sendall(b"0\n")
                    log("OPEN %s" % (" ".join(parts[1:3])))
                elif op == "EXIT":
                    c.sendall(b"0\n")
                    break
                elif op == "CLOSE":
                    opened = False
                    c.sendall(b"0\n")
                elif op == "READ":
                    dev = parts[1]
                    if len(parts) >= 4 and parts[2] in ("INPUT", "OUTPUT"):
                        chan, attr = parts[3], (parts[4] if len(parts) > 4 else None)
                    elif len(parts) >= 3 and parts[2] in ("DEBUG", "BUFFER"):
                        chan, attr = None, (parts[3] if len(parts) > 3 else None)
                    else:
                        chan, attr = None, (parts[2] if len(parts) > 2 else None)
                    val = self.read_attr(dev, chan, attr)
                    if val is None:
                        c.sendall(b"-22\n")
                        continue
                    data = val.encode()
                    c.sendall(b"%d\n" % len(data))
                    c.sendall(data + b"\n")
                elif op == "WRITE":
                    dev = parts[1]
                    idx = 2
                    chan = None
                    if parts[idx] in ("INPUT", "OUTPUT"):
                        chan = parts[idx + 1]
                        idx += 2
                    elif parts[idx] in ("DEBUG", "BUFFER"):
                        idx += 1
                    attr = parts[idx]
                    try:
                        nbytes = int(parts[idx + 1])
                    except (IndexError, ValueError):
                        c.sendall(b"-22\n")
                        continue
                    body = rfile.read(nbytes)
                    ret = self.write_attr(dev, chan, attr, body.decode("ascii", "replace"))
                    c.sendall(b"%d\n" % ret)
                elif op == "READBUF":
                    dev = parts[1] if len(parts) > 1 else "cf-ad9361-lpc"
                    try:
                        nbytes = int(parts[2])
                    except (IndexError, ValueError):
                        nbytes = 0
                    if not nbytes:
                        c.sendall(b"0\n")
                        continue
                    data = self.stream.read(nbytes)
                    # 老 libiio（SDR# 0.14）只在首次读要掩码；libiio >=0.20 每次都要
                    if first_read or self.mask_every_read:
                        c.sendall(b"%d\n%08x\n" % (len(data), 0x3))
                        first_read = False
                    else:
                        c.sendall(b"%d\n" % len(data))
                    c.sendall(data)
                else:
                    log("未实现命令: %r" % cmd)
                    c.sendall(b"-22\n")
        except OSError as e:
            log("连接结束: %s" % e)
        finally:
            try:
                c.close()
            except OSError:
                pass
            with self.lock:
                self.clients -= 1
                left = self.clients
            log("SDR# 客户端断开 %s（剩余 %d）" % (addr, left))
            if left <= 0 and self.dev is not None:
                self.dev.pause()


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="ESP32-C5 → PlutoSDR(libiio/iiod) 桥")
    ap.add_argument("--port", default="COM10", help="ESP32-C5 串口")
    ap.add_argument("--listen", default="127.0.0.1:30431", help="iiod 监听地址")
    ap.add_argument("--freq", type=float, default=2412.0, help="初始中心频率 MHz")
    ap.add_argument("--rate", type=int, default=80000, help="初始 IQS 速率（10k..80k）")
    ap.add_argument("--full-scale", default="auto", help="int32→int16 的满量程参考")
    ap.add_argument("--fake", action="store_true", help="无硬件：合成单音 + 噪声")
    a = ap.parse_args(argv)
    _setup_console()

    host, port = a.listen.rsplit(":", 1)
    rate = nearest_rate(a.rate)
    stream = Stream(rate)
    fs = None if str(a.full_scale).lower() == "auto" else float(a.full_scale)
    if a.fake:
        dev = FakeDevice(stream, rate)
        log("--fake：%d S/s 合成单音（+40 kHz）+ 噪声" % rate)
    else:
        dev = C5Device(a.port, stream, fs)
        dev.start(rate, a.freq * 1e6)
        log("真板 %s：%d S/s，中心 %.3f MHz" % (a.port, rate, a.freq))
    srv = Iiod(dev, stream, host, int(port), a.freq * 1e6, rate)
    log("就绪：SDR# → Source = PlutoSDR, Device-URI = ip:%s:%s"
        % (host if host != "0.0.0.0" else "127.0.0.1", port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        dev.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
