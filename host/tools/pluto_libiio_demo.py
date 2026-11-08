#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pluto_libiio_demo.py - 用 Python 的 libiio 绑定（pylibiio）直连本项目的 iiod 服务。

前提：
  1) 桥接已经在跑（GUI 的「SDR# 桥接」模式，或 `bridge/dist/esp_sdr_bridge.exe`）；
  2) 装了 pylibiio 0.25（纯 ctypes，无需编译）：
         pip install "pylibiio==0.25"
  3) 有一个 **64 位** libiio.dll 能被找到。最省事的来源是 MATLAB 的 Pluto 支持包：
         C:\\ProgramData\\MATLAB\\SupportPackages\\R2026a\\3P.instrset\\
         libiio.instrset\\win64\\Windows-MinGW-W64\\
     把该目录加进 PATH，或用环境变量 IIO_DLL_DIR 指过去（本脚本会自动加进 PATH，
     同目录下的 libxml2-2.dll / libusb-1.0.dll 等依赖也一并可见）。

用法：
    python tools/pluto_libiio_demo.py                       # 2412 MHz / 250 kS/s / 读 4096 点
    python tools/pluto_libiio_demo.py --freq 5180 --rate 250000 --samples 16384
    python tools/pluto_libiio_demo.py --uri ip:127.0.0.1 --out cap.iq

输出：调谐/速率/增益读写结果 + 读到的 I/Q 统计；--out 可把 int16 交错样本存盘
（numpy 可用时再顺带估算一下峰值频点）。
"""
import argparse
import os
import struct
import sys

DEFAULT_DLL_DIR = (r"C:\ProgramData\MATLAB\SupportPackages\R2026a\3P.instrset"
                   r"\libiio.instrset\win64\Windows-MinGW-W64")


def _prepare_dll_path(dll_dir):
    d = dll_dir or os.environ.get("IIO_DLL_DIR", "")
    if d and os.path.isdir(d):
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        return d
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", default="ip:127.0.0.1", help="iiod URI（默认 ip:127.0.0.1）")
    ap.add_argument("--freq", type=float, default=2412.0, help="中心频率 MHz")
    ap.add_argument("--rate", type=int, default=250000, help="SDR 侧采样率（会换算到固件档位）")
    ap.add_argument("--gain", type=float, default=40.0, help="硬件增益（0..80，非 dB）")
    ap.add_argument("--samples", type=int, default=4096, help="读取样本数（I/Q 对）")
    ap.add_argument("--out", default=None, help="把 int16 交错 I/Q 存到这里")
    ap.add_argument("--dll-dir", default=DEFAULT_DLL_DIR, help="64 位 libiio.dll 所在目录")
    a = ap.parse_args(argv)

    d = _prepare_dll_path(a.dll_dir)
    if d:
        print("libiio.dll 目录: %s" % d)
    else:
        print("提示: 没找到 --dll-dir/IIO_DLL_DIR 指定的 libiio.dll 目录，"
              "依赖系统 PATH 里已有的 64 位 libiio.dll")
    try:
        import iio
    except Exception as e:
        sys.exit("import iio 失败（%s）：先 pip install pylibiio==0.25，并保证 64 位 "
                 "libiio.dll 可见" % e)

    ctx = iio.Context(a.uri)
    print("context: %s | %s" % (ctx.name, ctx.description))
    for dev in ctx.devices:
        print("  device %-16s channels=%d sample_size=%d"
              % (dev.id, len(dev.channels), dev.sample_size))

    phy = ctx.find_device("ad9361-phy")
    if phy is None:
        sys.exit("找不到 ad9361-phy（桥接没在跑？）")
    rx = phy.find_channel("voltage0")
    lo = phy.find_channel("altvoltage0", True)

    lo.attrs["frequency"].value = str(int(round(a.freq * 1e6)))
    rx.attrs["sampling_frequency"].value = str(int(a.rate))
    rx.attrs["hardwaregain"].value = "%.6f" % a.gain
    print("tune -> %s Hz, rate -> %s Hz, gain -> %s"
          % (lo.attrs["frequency"].value, rx.attrs["sampling_frequency"].value,
             rx.attrs["hardwaregain"].value))

    dma = ctx.find_device("cf-ad9361-lpc")
    for ch in dma.channels:
        if ch.id in ("voltage0", "voltage1"):
            ch.enabled = True
    print("sample_size = %d" % dma.sample_size)

    n = max(64, int(a.samples))
    buf = iio.Buffer(dma, n, False)
    buf.refill()
    raw = bytes(buf.read())
    print("read %d bytes (%d I/Q 对)" % (len(raw), len(raw) // 4))

    if a.out:
        with open(a.out, "wb") as f:
            f.write(raw)
        print("saved:", a.out)

    v = struct.unpack("<%dh" % (len(raw) // 2), raw)
    i, q = v[0::2], v[1::2]
    mi, mq = sum(i) / len(i), sum(q) / len(q)
    peak = max(max(abs(x) for x in i), max(abs(x) for x in q))
    print("mean I=%.1f Q=%.1f   peak=%d (%.0f%% FS)   DC 未去除"
          % (mi, mq, peak, 100.0 * peak / 32768))

    try:
        import numpy as np
        z = np.array(i, dtype=np.float32) + 1j * np.array(q, dtype=np.float32)
        z -= z.mean()
        spec = np.abs(np.fft.fftshift(np.fft.fft(z * np.hanning(len(z)))))
        k = int(np.argmax(spec))
        fs = float(rx.attrs["sampling_frequency"].value)
        print("peak bin %d -> 偏移 %+.1f kHz（fs=%g kS/s）"
              % (k, (k - len(z) / 2) * fs / len(z) / 1e3, fs / 1e3))
    except ImportError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
