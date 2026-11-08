#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""iq_analyze.py - offline analysis plots for ESP-SDR I/Q captures.

Reads I/Q recorded from an ESP32-C5 (or any esp-sdr target) and draws the
three-panel figure used for signal inspection:

    1. time domain      (I/Q vs time, envelope-decimated for long captures)
    2. power spectrum   (Welch-averaged PSD, dBFS vs MHz offset from center)
    3. spectrogram      (STFT heat map, dB colour bar on top)

Supported capture formats (as produced by this project):

    int32  continuous IQS stream saved from `IQFRAME` payloads
           (interleaved little-endian int32 I/Q pairs, 128 pairs per frame)
    int8   burst `CAP16` payload (signed 8-bit I/Q pairs, ADC rate)
    int16  raw 16-bit I/Q (other esp-sdr targets / future formats)

Examples
--------
    # continuous capture at 80 kS/s, 2.4 GHz center
    python iq_analyze.py capture_80k.iq --format int32 --rate 80000 --center 2412

    # burst snapshot at 80 MS/s, show only +/-20 MHz, save PNG
    python iq_analyze.py burst.iq --format int8 --rate 80e6 --span 20 --out burst.png

    # grab 2 s straight from the board (continuous IQS) and plot it
    python iq_analyze.py --live COM10 --live-rate 80000 --live-seconds 2 --save cap.iq

Dependencies: numpy + matplotlib (pip install numpy matplotlib).
"""
import argparse
import os
import sys
import time

try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.exit("numpy is required:  pip install numpy matplotlib")
try:
    import matplotlib
    if "--show" not in sys.argv:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import Normalize
except ImportError:  # pragma: no cover
    sys.exit("matplotlib is required:  pip install numpy matplotlib")

# Full-scale divisors: files are normalized to roughly [-1, 1) before any dB
# maths, so a full-scale tone sits near 0 dBFS. Continuous IQS samples are
# boxcar sums scaled by 2^-6 relative to the ADC codes (see docs/protocol.md).
FULL_SCALE = {"int32": 32768.0, "int16": 32768.0, "int8": 127.0}
BYTES_PER_PAIR = {"int32": 8, "int16": 4, "int8": 2}
DTYPES = {"int32": "<i4", "int16": "<i2", "int8": "<i1"}


# ---------------------------------------------------------------------------
# capture (live) - continuous IQS over USB-Serial-JTAG
# ---------------------------------------------------------------------------
def capture_live(port, rate_hz, seconds, baud=115200, log=print):
    """Grab `seconds` of continuous IQ (IQS) straight from the firmware.

    Returns (raw_payload_bytes, rate_hz). The payload is the concatenation of
    IQFRAME bodies, i.e. the int32 interleaved stream iq_analyze understands.
    """
    try:
        import serial
    except ImportError:
        sys.exit("pyserial is required for --live:  pip install pyserial")
    ser = serial.Serial(port, baud, timeout=0.2, write_timeout=5)

    def send(cmd, wait=0.5):
        ser.write(cmd.encode() + b"\n")
        end = time.time() + wait
        buf = b""
        while time.time() < end:
            buf += ser.read(65536)
        return buf

    try:
        ser.write(b"RELEASE\n")
        time.sleep(0.3)
        ser.reset_input_buffer()
        send("SYNC 4711")
        reply = send("IQS ON %d" % rate_hz, 0.6)
        if b"OK" not in reply:
            sys.exit("IQS ON rejected: %r" % reply[-120:])
        log("capturing %.1f s at %d S/s from %s ..." % (seconds, rate_hz, port))
        buf = b""
        payloads = []
        deadline = time.time() + seconds
        while time.time() < deadline:
            buf += ser.read(65536)
            while True:
                i = buf.find(b"IQFRAME ")
                if i < 0:
                    break
                nl = buf.find(b"\n", i)
                if nl < 0:
                    break
                parts = buf[i:nl].split()
                try:
                    length = int(parts[1])
                except (IndexError, ValueError):
                    buf = buf[nl + 1:]
                    continue
                need = nl + 1 + length
                if len(buf) < need:
                    break
                payloads.append(bytes(buf[nl + 1:need]))
                buf = buf[need:]
        ser.write(b"IQS OFF\n")
        time.sleep(0.5)
        log("received %d IQFRAME blocks (%.1f kB)" %
            (len(payloads), sum(len(p) for p in payloads) / 1000.0))
        return b"".join(payloads), rate_hz
    finally:
        try:
            ser.write(b"RELEASE\n")
        except Exception:
            pass
        ser.close()


# ---------------------------------------------------------------------------
# loading / preprocessing
# ---------------------------------------------------------------------------
def guess_format(path, rate):
    size = os.path.getsize(path)
    if rate and rate <= 2_000_000:
        return "int32"
    if rate:
        return "int8"
    # no rate given: prefer the format that yields a plausible sample count
    return "int8" if size % 8 else "int32"


def auto_full_scale(iq, pct=99.0):
    """Reference amplitude for dBFS. Continuous IQS values are boxcar sums with
    no documented ADC full scale, so the default is a high percentile of the
    capture (a tone then sits near 0 dBFS). int8 CAP16 keeps its true 127."""
    mag = np.abs(iq)
    ref = float(np.percentile(mag, pct)) if mag.size else 1.0
    return ref if ref > 0 else 1.0


def load_iq(path, fmt, rate, start=0.0, length=None, remove_dc=False,
            full_scale=None):
    fmt = fmt.lower()
    if fmt not in BYTES_PER_PAIR:
        sys.exit("unknown format %r (use int32/int16/int8)" % fmt)
    with open(path, "rb") as f:
        raw = f.read()
    step = BYTES_PER_PAIR[fmt]
    usable = len(raw) - len(raw) % step
    if usable != len(raw):
        print("warning: ignoring %d trailing bytes (not a whole I/Q pair)"
              % (len(raw) - usable))
    data = np.frombuffer(raw[:usable], dtype=DTYPES[fmt])
    if data.size == 0:
        sys.exit("file is empty: %s" % path)
    x = data.reshape(-1, 2)
    iq = (x[:, 0].astype(np.float32) + 1j * x[:, 1].astype(np.float32))
    fs = float(rate)
    if start > 0:
        iq = iq[int(start * fs):]
    if length:
        iq = iq[:int(length * fs)]
    if iq.size == 0:
        sys.exit("selected window is empty (start=%.3f s, length=%s)" % (start, length))
    if remove_dc:
        iq = iq - iq.mean()
    if full_scale is None:
        full_scale = FULL_SCALE[fmt] if fmt == "int8" else auto_full_scale(iq)
    iq = iq / np.float32(full_scale)
    return iq, fs, float(full_scale)


# ---------------------------------------------------------------------------
# DSP
# ---------------------------------------------------------------------------
def welch_psd(iq, fs, nperseg):
    """Welch PSD in dBFS/bin (Hann window, 50 % overlap, mean power)."""
    n = iq.size
    nperseg = int(min(nperseg, n))
    if nperseg < 16:
        nperseg = int(n)
    if nperseg & (nperseg - 1):                      # round down to power of two
        nperseg = 1 << (nperseg.bit_length() - 1)
    hop = max(1, nperseg // 2)
    win = np.hanning(nperseg).astype(np.float32)
    # Coherent gain normalization: |X|^2 / (sum w)^2 makes a complex tone of
    # amplitude A read A^2, i.e. a full-scale I/Q tone sits at 0 dBFS.
    wsum = float(np.sum(win.astype(np.float64)))
    wnorm = wsum * wsum
    acc = None
    segs = 0
    for off in range(0, n - nperseg + 1, hop):
        seg = iq[off:off + nperseg] * win
        spec = np.fft.fftshift(np.fft.fft(seg))
        p = (spec.real.astype(np.float64) ** 2 + spec.imag.astype(np.float64) ** 2)
        acc = p if acc is None else acc + p
        segs += 1
    if acc is None:                                  # capture shorter than one segment
        seg = np.zeros(nperseg, dtype=np.complex64)
        seg[:n] = iq
        spec = np.fft.fftshift(np.fft.fft(seg * win))
        acc = (spec.real.astype(np.float64) ** 2 + spec.imag.astype(np.float64) ** 2)
        segs = 1
    psd = acc / (segs * wnorm)                       # tone amplitude^2 (0 dBFS = FS I/Q)
    db = 10.0 * np.log10(psd + 1e-20)
    freqs = np.fft.fftshift(np.fft.fftfreq(nperseg, d=1.0 / fs))
    return freqs, db


def stft_db(iq, fs, nperseg):
    """STFT magnitude in dB for the heat map: returns (db[t, f], freqs, times)."""
    n = iq.size
    nperseg = int(min(nperseg, n))
    if nperseg < 32:
        nperseg = int(n)
    if nperseg & (nperseg - 1):
        nperseg = 1 << (nperseg.bit_length() - 1)
    hop = max(1, nperseg // 4)
    win = np.hanning(nperseg).astype(np.float32)
    wnorm = float(np.sum(win.astype(np.float64))) ** 2
    frames = []
    times = []
    for off in range(0, max(1, n - nperseg) + 1, hop):
        seg = iq[off:off + nperseg]
        if seg.size < nperseg:
            seg = np.concatenate([seg, np.zeros(nperseg - seg.size, np.complex64)])
        spec = np.fft.fftshift(np.fft.fft(seg * win))
        p = (spec.real.astype(np.float64) ** 2 + spec.imag.astype(np.float64) ** 2)
        frames.append(p)
        times.append((off + nperseg / 2.0) / fs)
    if not frames:
        return np.zeros((1, nperseg)), np.zeros(nperseg), np.zeros(1)
    db = 10.0 * np.log10(np.array(frames) / wnorm + 1e-20)
    freqs = np.fft.fftshift(np.fft.fftfreq(nperseg, d=1.0 / fs))
    return db, freqs, np.array(times)


def envelope(iq, bins):
    """Min/max envelope of I and Q for plotting long captures cheaply."""
    n = iq.size
    bins = int(min(bins, n))
    edges = np.linspace(0, n, bins + 1).astype(int)
    t = np.empty(bins)
    i_hi, i_lo = np.empty(bins), np.empty(bins)
    q_hi, q_lo = np.empty(bins), np.empty(bins)
    for k in range(bins):
        a, b = edges[k], max(edges[k] + 1, edges[k + 1])
        seg = iq[a:b]
        t[k] = ((a + b) / 2.0)
        ri, rq = seg.real, seg.imag
        i_hi[k], i_lo[k] = ri.max(), ri.min()
        q_hi[k], q_lo[k] = rq.max(), rq.min()
    return t, i_lo, i_hi, q_lo, q_hi


# ---------------------------------------------------------------------------
# plotting
# ---------------------------------------------------------------------------
def plot(iq, fs, center_mhz, nperseg, span_mhz, out_path, title=None, show=False):
    n = iq.size
    dur_ms = n / fs * 1000.0
    freqs, psd_db = welch_psd(iq, fs, nperseg)
    db, sfreqs, stimes = stft_db(iq, fs, nperseg)
    f_mhz = freqs / 1e6
    keep = np.abs(f_mhz) <= (span_mhz if span_mhz else fs / 1e6 / 2 + 1e-9)
    if not keep.any():
        keep = np.ones_like(f_mhz, dtype=bool)

    fig, axes = plt.subplots(3, 1, figsize=(14, 9.5),
                             gridspec_kw={"height_ratios": [1.0, 1.15, 1.35]})
    fig.subplots_adjust(left=0.075, right=0.985, top=0.93, bottom=0.07, hspace=0.42)

    # ---- 1. time domain ---------------------------------------------------
    ax = axes[0]
    t_ms = np.arange(n) / fs * 1000.0
    if n > 30000:
        tb, i_lo, i_hi, q_lo, q_hi = envelope(iq, 4000)
        tb_ms = tb / fs * 1000.0
        ax.fill_between(tb_ms, i_lo, i_hi, color="#1f77b4", linewidth=0, alpha=0.85,
                        label="I")
        ax.fill_between(tb_ms, q_lo, q_hi, color="#7fc4f0", linewidth=0, alpha=0.75,
                        label="Q")
    else:
        ax.plot(t_ms, iq.real, color="#1f77b4", linewidth=0.6, label="I")
        ax.plot(t_ms, iq.imag, color="#7fc4f0", linewidth=0.6, label="Q")
    ax.set_xlim(0, dur_ms)
    # Robust vertical range: a few glitch samples would otherwise flatten the
    # trace (IQ estimator output has isolated spikes).
    mag = np.abs(iq)
    hi = float(np.percentile(mag, 99.9)) if mag.size else 1.0
    lo = float(np.percentile(mag, 0.1)) if mag.size else -1.0
    span = max(hi, -lo, 1e-6)
    ax.set_ylim(-span * 1.15, span * 1.15)
    ax.set_xlabel("Time (ms)")
    ax.set_ylabel("Amplitude (norm.)")
    ax.grid(True, color="#dddddd", linewidth=0.6)
    ax.set_axisbelow(True)
    ax.legend(loc="upper left", frameon=False, ncol=2, fontsize=9)
    ax.set_title("I/Q time domain", fontsize=10)

    # ---- 2. power spectrum ------------------------------------------------
    ax = axes[1]
    ax.plot(f_mhz[keep], psd_db[keep], color="#1f77b4", linewidth=0.9)
    ax.fill_between(f_mhz[keep], psd_db[keep], np.floor(psd_db[keep].min()),
                    color="#1f77b4", alpha=0.18, linewidth=0)
    ax.set_xlabel("Frequency offset from %.3f MHz (MHz)" % center_mhz)
    ax.set_ylabel("Power spectrum (dBFS)")
    ax.grid(True, color="#dddddd", linewidth=0.6)
    ax.set_axisbelow(True)
    ax.set_xlim(f_mhz[keep].min(), f_mhz[keep].max())
    pk = int(np.argmax(psd_db[keep]))
    peak_f = f_mhz[keep][pk]
    peak_db = psd_db[keep][pk]
    ax.annotate("peak %.3f MHz / %.1f dBFS" % (center_mhz + peak_f, peak_db),
                xy=(peak_f, peak_db), xytext=(0.02, 0.06), textcoords="axes fraction",
                fontsize=9, color="#333333")
    ax.set_title("Power spectrum  (Welch, NFFT=%d, ~%d frames)" %
                 (freqs.size, max(1, n // max(16, nperseg // 2))), fontsize=10)

    # ---- 3. spectrogram ---------------------------------------------------
    ax = axes[2]
    sk = np.abs(sfreqs / 1e6) <= (span_mhz if span_mhz else fs / 1e6 / 2 + 1e-9)
    if not sk.any():
        sk = np.ones(sfreqs.size, dtype=bool)
    dbv = db[:, sk].T
    fv = sfreqs[sk] / 1e6
    tv = stimes * 1000.0
    vmin = float(np.percentile(dbv, 2))
    vmax = float(np.percentile(dbv, 99.9))
    if vmax - vmin < 6:
        vmin, vmax = vmax - 40.0, vmax
    extent = [tv[0], tv[-1] if tv.size > 1 else dur_ms, fv[0], fv[-1]]
    im = ax.imshow(dbv, aspect="auto", origin="lower", extent=extent,
                   cmap="turbo", norm=Normalize(vmin=vmin, vmax=vmax),
                   interpolation="nearest")
    ax.set_xlabel("Time (ms)")
    ax.set_ylabel("Frequency (MHz)")
    ax.set_title("")
    cbar = fig.colorbar(im, ax=ax, location="top", pad=0.02, fraction=0.055,
                        aspect=45)
    cbar.ax.tick_params(labelsize=8, pad=1)
    cbar.set_label("Power (dBFS)   —   spectrogram: STFT %d, hop %d"
                   % (sfreqs.size, max(1, sfreqs.size // 4)),
                   fontsize=8, labelpad=1)
    cbar.ax.xaxis.set_ticks_position("top")
    cbar.ax.xaxis.set_label_position("top")

    fig.suptitle("ESP-SDR capture  ·  fs = %s S/s  ·  center = %.3f MHz  ·  "
                 "%d samples  ·  %.1f ms%s"
                 % (_fmt_rate(fs), center_mhz, n, dur_ms,
                    "" if not title else "  ·  " + title), fontsize=11)

    if out_path:
        fig.savefig(out_path, dpi=110)
        print("figure written: %s" % out_path)
    if show:
        plt.show()
    plt.close(fig)
    return {"peak_offset_mhz": peak_f, "peak_center_mhz": center_mhz + peak_f,
            "peak_dbfs": peak_db, "samples": n, "duration_ms": dur_ms}


def _fmt_rate(fs):
    if fs >= 1e6:
        return "%.3g M" % (fs / 1e6)
    if fs >= 1e3:
        return "%.3g k" % (fs / 1e3)
    return "%.0f" % fs


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", help="I/Q file (raw, no header)")
    ap.add_argument("--format", choices=("int32", "int16", "int8"), default=None,
                    help="sample layout; default: guessed from --rate/file size")
    ap.add_argument("--rate", type=float, default=None, help="sample rate in S/s")
    ap.add_argument("--center", type=float, default=2412.0, help="center freq in MHz")
    ap.add_argument("--start", type=float, default=0.0, help="start offset in seconds")
    ap.add_argument("--length", type=float, default=None, help="analyse this many seconds")
    ap.add_argument("--nfft", type=int, default=1024, help="FFT size for PSD/STFT")
    ap.add_argument("--span", type=float, default=None,
                    help="show only +/- this many MHz (default: full band)")
    ap.add_argument("--remove-dc", action="store_true", help="subtract the mean (DC) first")
    ap.add_argument("--full-scale", default="auto",
                    help="amplitude that maps to 0 dBFS: 'auto' (default) or a number")
    ap.add_argument("--out", default=None, help="output PNG (default: <file>.png)")
    ap.add_argument("--show", action="store_true", help="open an interactive window")
    ap.add_argument("--live", metavar="PORT", default=None,
                    help="capture from the board first (e.g. COM10, continuous IQS)")
    ap.add_argument("--live-rate", type=float, default=80000.0,
                    help="IQS rate for --live (10000..80000)")
    ap.add_argument("--live-seconds", type=float, default=2.0)
    ap.add_argument("--save", default=None, help="also save the captured raw stream")
    args = ap.parse_args(argv)

    if args.live:
        raw, rate = capture_live(args.live, int(args.live_rate), args.live_seconds)
        if not raw:
            sys.exit("no IQ frames received (is the board idle and COM port correct?)")
        if args.save:
            with open(args.save, "wb") as f:
                f.write(raw)
            print("raw I/Q saved: %s (%d bytes)" % (args.save, len(raw)))
        fmt = "int32"
        data = np.frombuffer(raw, dtype=DTYPES[fmt])
        iq = (data.reshape(-1, 2)[:, 0].astype(np.float32)
              + 1j * data.reshape(-1, 2)[:, 1].astype(np.float32))
        iq = iq / np.float32(auto_full_scale(iq))
        fs = float(rate)
        out = args.out or "live_%s.png" % time.strftime("%H%M%S")
        plot(iq, fs, args.center, args.nfft, args.span, out, show=args.show,
             title="%s%s" % (args.live, " @ %d S/s" % rate))
        return 0

    if not args.path:
        ap.error("give an I/Q file, or use --live PORT")
    if not os.path.exists(args.path):
        sys.exit("no such file: %s" % args.path)
    fmt = args.format or guess_format(args.path, args.rate)
    if args.rate:
        rate = args.rate
    elif fmt == "int32":
        rate = 80000.0
        print("note: --rate not given, assuming 80000 S/s for an int32 stream")
    else:
        sys.exit("--rate is required for %s captures (e.g. 80e6 for a CAP16 burst)" % fmt)
    fs_arg = None if str(args.full_scale).lower() == "auto" else float(args.full_scale)
    iq, fs, ref = load_iq(args.path, fmt, rate, args.start, args.length,
                          args.remove_dc, fs_arg)
    out = args.out or os.path.splitext(args.path)[0] + ".png"
    info = plot(iq, fs, args.center, args.nfft, args.span, out, show=args.show,
                title=os.path.basename(args.path))
    print("format=%s  fs=%s S/s  samples=%d  duration=%.2f ms  0 dBFS ref=%g"
          % (fmt, _fmt_rate(fs), info["samples"], info["duration_ms"], ref))
    print("peak: %.3f MHz (offset %+.3f MHz) at %.1f dBFS"
          % (info["peak_center_mhz"], info["peak_offset_mhz"], info["peak_dbfs"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
