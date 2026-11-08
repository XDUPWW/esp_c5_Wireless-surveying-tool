#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gui_libiio_record.py - 通过 GUI 的「SDR# 桥接」模式连续录 IQ（Python libiio）。

前提：GUI 已连接板子并点了「SDR# 桥接」（监听 127.0.0.1:30431）。
依赖：pip install "pylibiio==0.25" + 一个 64 位 libiio.dll（默认取 MATLAB 支持包那份）。

用法：
    python examples/gui_libiio_record.py --seconds 5 --out rec.iq
    python examples/gui_libiio_record.py --freq 5180 --rate 250000 --seconds 3

输出 .iq 是 **int16 交错 I/Q**（I0,Q0,I1,Q1,...），可直接给
`tools/iq_analyze.py --format int16 --rate <SDR 侧速率>` 或 MATLAB `fread(fid,[2 inf],'int16')`。
"""
import argparse
import os
import sys
import time

DEFAULT_DLL_DIR = (r"C:\ProgramData\MATLAB\SupportPackages\R2026a\3P.instrset"
                   r"\libiio.instrset\win64\Windows-MinGW-W64")


def prepare_libiio(dll_dir):
    d = dll_dir or os.environ.get("IIO_DLL_DIR", "")
    if d and os.path.isdir(d):
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    try:
        import iio
    except Exception as e:
        sys.exit("import iio 失败（%s）：pip install \"pylibiio==0.25\"，并让 64 位 "
                 "libiio.dll 可见（--dll-dir / IIO_DLL_DIR / PATH）" % e)
    return iio


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", default="ip:127.0.0.1")
    ap.add_argument("--freq", type=float, default=2412.0, help="中心频率 MHz")
    ap.add_argument("--rate", type=int, default=250000, help="SDR 侧采样率")
    ap.add_argument("--gain", type=float, default=40.0, help="硬件增益 0..80（非 dB）")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--chunk", type=int, default=65536, help="每次读多少样本（I/Q 对）")
    ap.add_argument("--out", required=True, help="输出 int16 交错 .iq 文件")
    ap.add_argument("--dll-dir", default=DEFAULT_DLL_DIR)
    a = ap.parse_args(argv)

    iio = prepare_libiio(a.dll_dir)
    ctx = iio.Context(a.uri)
    phy = ctx.find_device("ad9361-phy")
    dma = ctx.find_device("cf-ad9361-lpc")
    if phy is None or dma is None:
        sys.exit("找不到 ad9361-phy / cf-ad9361-lpc —— GUI 的「SDR# 桥接」没启动？")

    rx = phy.find_channel("voltage0")
    lo = phy.find_channel("altvoltage0", True)
    lo.attrs["frequency"].value = str(int(round(a.freq * 1e6)))
    rx.attrs["sampling_frequency"].value = str(int(a.rate))
    rx.attrs["hardwaregain"].value = "%.6f" % a.gain
    for ch in dma.channels:
        if ch.id in ("voltage0", "voltage1"):
            ch.enabled = True

    fs = float(a.rate)
    want = int(fs * a.seconds)
    got = 0
    peak = 0
    t0 = time.time()
    print("recording %.1f s @ %.0f kS/s -> %s" % (a.seconds, fs / 1e3, a.out))
    # 缓冲区只建一次（libiio 的 Buffer 生命周期与设备绑定，反复新建容易失败）
    buf = iio.Buffer(dma, a.chunk, False)
    with open(a.out, "wb") as f:
        while got < want:
            n = min(a.chunk, want - got)
            buf.refill()
            raw = bytes(buf.read())[:n * 4]
            f.write(raw)
            got += len(raw) // 4
            # 粗看幅度（int16 交错，逐 4 字节取 I）
            v = memoryview(raw)
            for k in range(0, len(v) - 3, 4 * 97):        # 抽样，别每样本都算
                x = int.from_bytes(v[k:k + 2], "little", signed=True)
                if abs(x) > peak:
                    peak = abs(x)
    dt = time.time() - t0
    print("done: %d 对, %.1f MB, %.1f s -> 速率 %.0f kS/s, 峰值≈%.0f%% FS"
          % (got, os.path.getsize(a.out) / 1e6, dt, got / max(dt, 1e-9) / 1e3,
             100.0 * peak / 32768))
    print("提示：这是 SDR 侧速率（含主机插值）；固件真实带宽 ≤ 80 kHz，"
          "离线分析请用 --format int16 --rate %d" % a.rate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
