#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sdrsharp_bridge.py - 在 monitor_gui 进程内把 ESP32-C5 冒充成 ADALM-PLUTO。

SDR# 的 PlutoSDR 插件（manawyrm/sdrsharp-plutosdr）用 libiio 的**网络**后端
连 `ip:<host>`，走 TCP 30431 的 iiod 文本协议。本模块实现该协议的最小子集：

    VERSION / PRINT / TIMEOUT / OPEN / CLOSE / READ / WRITE / READBUF / EXIT

并伪装两台设备（与真 Pluto 同名）：
  - `ad9361-phy`    : altvoltage0.frequency / voltage0.{sampling_frequency,
                      rf_bandwidth, hardwaregain, gain_control_mode} 等属性
  - `cf-ad9361-lpc` : I/Q 扫描通道（int16 交错），由 GUI 的 IQFRAME 流喂入

硬件动作通过回调交给调用方（GUI 用它的串口 Link 发 FREQ / IQS / GAIN / BANDWIDTH），
所以本模块不直接碰串口，也不依赖 numpy（有 numpy 时用向量化重采样，否则纯 Python）。
"""
import os
import socket
import struct
import threading
import time

try:                                   # 可选加速
    import numpy as _np
except Exception:                      # pragma: no cover
    _np = None

IIOD_PORT = 30431
BYTES_PER_PAIR = 4                     # int16 I + int16 Q



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


def _log_sink_default(msg):
    print(msg, flush=True)


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


# 样本缓冲直接复用独立桥接的实现（bridge/pluto_iiod.py 的 Stream），避免两份代码漂移
_sys_path_added = False


def _import_base_stream():
    global _sys_path_added
    import importlib
    import os as _os
    import sys as _sys
    here = _os.path.dirname(_os.path.abspath(__file__))
    bridge_dir = _os.path.join(_os.path.dirname(here), "bridge")
    if bridge_dir not in _sys.path:
        _sys.path.insert(0, bridge_dir)
        _sys_path_added = True
    return importlib.import_module("pluto_iiod").Stream


_BASE_STREAM = _import_base_stream()


class SampleStream(_BASE_STREAM):
    """GUI 版封装：多一个 bytes 入口，其余（重采样/背压/清空）复用同一实现。"""

    def push_bytes(self, payload_i16):
        if not payload_i16:
            return
        if _np is not None:
            self.push(_np.frombuffer(payload_i16, dtype="<i2"))
        else:                                   # 极端情况：没有 numpy 时退化为乱序丢弃
            pass

    def _pairs(self):
        return (self.buf.size // 2) if hasattr(self.buf, "size") else (len(self.buf) // 2)


class IiodServer:
    """iiod 协议子集；所有硬件动作通过回调交回给 GUI。"""

    def __init__(self, stream, on_freq=None, on_rate=None, on_gain=None,
                 on_bandwidth=None, host="127.0.0.1", port=IIOD_PORT,
                 freq_hz=2412_000_000, rate_hz=80000, log=_log_sink_default,
                 on_client_change=None):
        self.stream = stream
        self.on_freq = on_freq
        self.on_rate = on_rate
        self.on_gain = on_gain
        self.on_bandwidth = on_bandwidth
        self.log = log
        self.on_client_change = on_client_change
        self.freq_hz = int(freq_hz)
        self.rate_hz = int(rate_hz)
        self.bw_hz = 20_000_000
        self.gain_db = 40.0
        self.gain_mode = "manual"
        self.clients = 0
        self.mask_every_read = True
        self.lock = threading.Lock()
        self._stop = False
        self.srv = socket.socket()
        # Windows 上 SO_REUSEADDR 会让第二个实例也能绑同一端口（两个监听、客户端随机连），
        # 所以只在非 Windows 打开它。
        if os.name != "nt":
            self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((host, port))
        self.srv.listen(4)
        self.srv.settimeout(0.5)
        self.addr = "%s:%d" % (host, port)
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()
        self.log("桥接已监听 %s（SDR# 里 PlutoSDR 源，URI = ip:%s）"
                 % (self.addr, host if host != "0.0.0.0" else "127.0.0.1"))

    # ---- 属性 ----
    def _xml(self):
        return (XML_TEMPLATE
                .replace("@DESC@", "ESP32-C5 SDR (monitor_gui bridge)")
                )

    def _read_attr(self, dev, chan, attr):
        if dev == "ad9361-phy":
            if attr == "frequency":
                if chan == "altvoltage1":
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
        if dev == "cf-ad9361-lpc" and attr == "sampling_frequency":
            return str(int(self.rate_hz))
        return CONST_ATTRS.get(attr)

    def _write_attr(self, dev, chan, attr, value):
        value = (value or "").replace(chr(0), "").strip()
        try:
            if dev == "ad9361-phy":
                if chan == "altvoltage0" and attr == "frequency":
                    hz = int(float(value))
                    self.freq_hz = hz
                    if self.on_freq:
                        self.on_freq(hz)
                    return 0
                if chan == "voltage0":
                    if attr == "sampling_frequency":
                        hz = int(float(value))
                        self.rate_hz = hz
                        self.stream.set_rate(hz)
                        if self.on_rate:
                            self.on_rate(hz)
                        return 0
                    if attr == "rf_bandwidth":
                        hz = int(float(value))
                        self.bw_hz = hz
                        if self.on_bandwidth:
                            self.on_bandwidth(hz)
                        return 0
                    if attr == "gain_control_mode":
                        self.gain_mode = value
                        if self.on_gain:
                            self.on_gain(-1 if value != "manual"
                                         else int(round(self.gain_db / 100.0 * 80)))
                        return 0
                    if attr == "hardwaregain":
                        self.gain_db = float(value)
                        if self.on_gain:
                            self.on_gain(int(round(max(0.0, self.gain_db) / 100.0 * 80)))
                        return 0
                if attr == "filter_fir_en":
                    return 0
        except (TypeError, ValueError):
            return -22
        self.log("桥接：未处理的属性写入 %s %s.%s" % (dev, chan or "-", attr))
        return 0

    # ---- 连接 ----
    def _accept_loop(self):
        while not self._stop:
            try:
                c, addr = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self.lock:
                self.clients += 1
                n = self.clients
            threading.Thread(target=self._handle, args=(c, addr), daemon=True).start()
            if self.on_client_change:
                try:
                    self.on_client_change(n)
                except Exception as e:
                    self.log("bridge: client callback failed: %s" % e)

    def _handle(self, c, addr):
        self.log("桥接：SDR# 连入 %s" % (addr,))
        first_read = True
        rfile = c.makefile("rb")
        try:
            while True:
                line = rfile.readline()
                if not line:
                    break
                cmd = line.decode("ascii", "replace").strip()
                if not cmd:
                    continue
                parts = cmd.split()
                op = parts[0].upper()
                if op == "VERSION":
                    c.sendall(b"0.21.monitor-gui\n")
                elif op == "PRINT":
                    xml = self._xml().encode()
                    c.sendall(b"%d\n" % len(xml))
                    c.sendall(xml + b"\n")
                elif op == "TIMEOUT":
                    c.sendall(b"0\n")
                elif op in ("SET", "GET"):
                    if op == "SET":
                        self.mask_every_read = True
                    c.sendall(b"0" + bytes([10]))
                elif op == "OPEN":
                    first_read = True
                    c.sendall(b"0\n")
                    self.log("桥接：OPEN %s" % " ".join(parts[1:3]))
                elif op == "CLOSE":
                    c.sendall(b"0\n")
                elif op == "EXIT":
                    c.sendall(b"0\n")
                    break
                elif op == "READ":
                    dev = parts[1]
                    if len(parts) >= 4 and parts[2] in ("INPUT", "OUTPUT"):
                        chan, attr = parts[3], (parts[4] if len(parts) > 4 else None)
                    elif len(parts) >= 3 and parts[2] in ("DEBUG", "BUFFER"):
                        chan, attr = None, (parts[3] if len(parts) > 3 else None)
                    else:
                        chan, attr = None, (parts[2] if len(parts) > 2 else None)
                    val = self._read_attr(dev, chan, attr)
                    if val is None:
                        c.sendall(b"-22\n")
                    else:
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
                    c.sendall(b"%d\n" % self._write_attr(
                        dev, chan, attr, body.decode("ascii", "replace")))
                elif op == "READBUF":
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
                    c.sendall(b"-22\n")
        except OSError as e:
            self.log("桥接：连接结束（%s）" % e)
        finally:
            try:
                c.close()
            except OSError:
                pass
            with self.lock:
                self.clients -= 1
                left = self.clients
            if self.on_client_change:
                try:
                    self.on_client_change(left)
                except Exception as e:
                    self.log("bridge: client callback failed: %s" % e)
            self.log("桥接：SDR# 断开 %s（剩余 %d）" % (addr, left))

    def close(self):
        self._stop = True
        try:
            self.srv.close()
        except OSError:
            pass
