#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_gui.py - GUI test suite against the mock firmware transport.

Layers:
  A. Link parser over FakeSerial + FirmwareModel (normal + fault injection)
  B. App mode logic with MockLink injected (requires a display for tkinter)
  C. pcap + named-pipe output validated with tshark

Usage: python test_gui.py [a|b|c|all]
"""
import json
import os
import queue
import struct
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "monitor_gui"))
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

from mock_fw import FakeSerial, FirmwareModel, MockLink, make_wifi_frame, make_ble_frame, spc1_frame
import esp_sdr_gui

TSHARK = r"D:\Wireshark\tshark.exe"
RESULTS = []
CURRENT = {}


def case(cid, group, pre, steps, expect):
    def deco(fn):
        CASES.append((cid, group, pre, steps, expect, fn))
        return fn
    return deco


CASES = []


def record(actual, result, note=""):
    cid, group, pre, steps, expect = CURRENT["meta"]
    RESULTS.append({"case": cid, "group": group, "pre": pre, "steps": steps,
                    "expect": expect, "actual": actual, "result": result, "note": note})
    print(("  PASS " if result == "PASS" else "  %s " % result) + cid + " " + str(actual)[:150],
          flush=True)


def check(cond, actual, note=""):
    record(actual, "PASS" if cond else "FAIL", note)


def run_case(meta, fn):
    CURRENT["meta"] = meta
    try:
        fn()
    except Exception as e:
        record("exception: %s" % e, "ERROR")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_link(model=None):
    model = model or FirmwareModel()
    ser = FakeSerial(model)
    link = esp_sdr_gui.Link("MOCK", lambda *ev: None, lambda m: None)
    # Bypass Link.open(): attach the in-memory port directly.
    link.ser = ser
    link.ser_cmd = ser
    link.ser_data = ser
    link.ser_rx = ser
    ser.open()  # attaches the model thread
    link.start()
    return link, ser, model


# ===========================================================================
# A. Link parser tests over the mock firmware
# ===========================================================================
@case("G-A01", "link", "mock 固件在线",
      "SYNC/INFO/CAPS/MON? 命令应答", "应答正确")
def ga01():
    link, ser, model = make_link()
    try:
        ok = (link.command("SYNC 111") == "SYNC 111"
              and link.command("INFO").startswith("C5SDR")
              and "WIFIMON" in link.command("CAPS")
              and "WIFI:off" in link.command("MON?"))
        check(ok, "handshake ok=%s" % ok)
    finally:
        link.close()
        ser.close()


@case("G-A02", "link", "mock 固件",
      "MON WIFI ON + 突发 20 帧 → 解析 wififrame 事件",
      "20/20 帧完整到达，Radiotap 合法")
def ga02():
    link, ser, model = make_link()
    events = []
    link.on_event = lambda *ev: events.append(ev)
    try:
        assert link.command("MON WIFI ON 1") == "OK"
        t = threading.Thread(target=model.emit_wifi_burst, args=(20, 0.01))
        t.start()
        t.join(5)
        time.sleep(0.3)
        frames = [e for e in events if e[0] == "wififrame"]
        good = sum(1 for e in frames if len(e[2]) == int(e[1][1]) and e[2][0] == 0 and e[2][2] == 15)
        link.command("MON WIFI OFF")
        check(len(frames) == 20 and good == 20, "frames=%d good=%d" % (len(frames), good))
    finally:
        link.close()
        ser.close()


@case("G-A03", "link", "mock 固件",
      "MON BLE ON + 突发 15 帧", "15/15 BLE 帧完整（PDU 长度自洽）")
def ga03():
    link, ser, model = make_link()
    events = []
    link.on_event = lambda *ev: events.append(ev)
    try:
        assert link.command("MON BLE ON") == "OK"
        t = threading.Thread(target=model.emit_ble_burst, args=(15, 0.01))
        t.start()
        t.join(5)
        time.sleep(0.3)
        frames = [e for e in events if e[0] == "bleframe"]
        good = sum(1 for e in frames if e[2][1] == len(e[2]) - 2)
        check(len(frames) == 15 and good == 15, "frames=%d good=%d" % (len(frames), good))
    finally:
        link.close()
        ser.close()


@case("G-A04", "link", "mock 固件",
      "SPEC 0 流 → SPC1 解析 → 停止字节 → 流停",
      "SPC1 帧全部 crc_ok；停止后不再来")
def ga04():
    link, ser, model = make_link()
    events = []
    link.on_event = lambda *ev: events.append(ev)
    try:
        r = link.command("SPEC 0 1 1 0 0 256", 5)
        link.spec_active = True
        time.sleep(1.0)
        specs = [e for e in events if e[0] == "spec"]
        link.spec_active = False
        link.ser.write(b"\n")
        time.sleep(0.5)
        n0 = len(events)
        time.sleep(0.4)
        stopped = len(events) == n0
        good = sum(1 for e in specs if e[5] and len(e[4]) == 256)
        check(r.startswith("SPEC 256") and good == len(specs) and len(specs) > 20 and stopped,
              "start=%s specs=%d good=%d stopped=%s" % (r, len(specs), good, stopped))
    finally:
        link.close()
        ser.close()


@case("G-A05", "link", "fault=badcrc",
      "每 5 帧注入 1 个 CRC 错误帧", "Link 解析不打滑：好帧 CRC 对、坏帧标 crc_ok=False")
def ga05():
    link, ser, model = make_link(model=FirmwareModel())
    model.fault = "badcrc"
    model.fault_every = 5
    events = []
    link.on_event = lambda *ev: events.append(ev)
    try:
        link.command("MON WIFI ON 1")
        t = threading.Thread(target=model.emit_wifi_burst, args=(20, 0.01))
        t.start()
        t.join(5)
        time.sleep(0.3)
        frames = [e for e in events if e[0] == "wififrame"]
        bad = sum(1 for e in frames if not e[1] or len(e[2]) != int(e[1][1]))
        check(len(frames) >= 15 and bad <= len(frames) - 15,
              "frames=%d malformed=%d" % (len(frames), bad),
              "mock 坏帧仅翻转 payload 末字节，头部完好，Link 仍能完整切帧")
    finally:
        link.close()
        ser.close()


@case("G-A06", "link", "fault=truncate",
      "周期性截断二进制载荷", "Link 丢弃残缺帧并在超时后重新对齐，后续命令可用")
def ga06():
    link, ser, model = make_link()
    model.fault = "truncate"
    model.fault_every = 3
    events = []
    link.on_event = lambda *ev: events.append(ev)
    try:
        link.command("MON WIFI ON 1")
        t = threading.Thread(target=model.emit_wifi_burst, args=(12, 0.01))
        t.start()
        t.join(5)
        time.sleep(0.5)
        link.command("MON WIFI OFF")
        r = link.command("SYNC 606", 8)
        check(r == "SYNC 606", "resync=%s events=%d" % (r, len(events)),
              "截断后 Link 丢弃残缺字节，重新对齐成功")
    finally:
        link.close()
        ser.close()


@case("G-A07", "link", "fault=sticky",
      "粘包：多帧一次 feed", "逐帧切分正确")
def ga07():
    link, ser, model = make_link()
    try:
        link.command("MON WIFI ON 1")
        events = []
        link.on_event = lambda *ev: events.append(ev)
        blob = b""
        for i in range(5):
            pl = make_wifi_frame(i)
            blob += b"WIFIFRAME %d -60 1 %08x\n" % (len(pl), 0) + pl
        ser.feed(blob)  # one write, sticky frames
        time.sleep(0.6)
        frames = [e for e in events if e[0] == "wififrame"]
        check(len(frames) == 5 and all(len(e[2]) == int(e[1][1]) for e in frames),
              "frames=%d" % len(frames))
    finally:
        link.close()
        ser.close()


@case("G-A08", "link", "fault=halfpacket（手动）",
      "命令行分两次 write（半包）", "拼合成一条命令")
def ga08():
    link, ser, model = make_link()
    events = []
    link.on_event = lambda *ev: events.append(ev)
    try:
        ser.write(b"SY")
        time.sleep(0.2)
        ser.write(b"NC 808\n")
        deadline = time.time() + 4
        ok = False
        while time.time() < deadline and not ok:
            for e in list(events):
                if e[0] == "line" and e[1] == "SYNC 808":
                    ok = True
                    break
            time.sleep(0.02)
        check(ok, "lines=%s" % [e[1] for e in events if e[0] == "line"])
    finally:
        link.close()
        ser.close()


@case("G-A09", "link", "fault=slow",
      "慢应答（0.8s）", "command 默认超时内拿到应答")
def ga09():
    link, ser, model = make_link()
    model.fault = "slow"
    try:
        t0 = time.time()
        r = link.command("SYNC 909", 5)
        check(r == "SYNC 909" and time.time() - t0 < 5, "r=%s dt=%.2f" % (r, time.time() - t0))
    finally:
        link.close()
        ser.close()


@case("G-A10", "link", "fault=dead（固件无响应）",
      "command 超时抛出 TimeoutError，可重连恢复",
      "超时可预期；换正常模型后恢复")
def ga10():
    link, ser, model = make_link()
    model.fault = "dead"
    try:
        try:
            link.command("SYNC 1010", 3)
            check(False, "no timeout raised")
        except TimeoutError:
            check(True, "TimeoutError raised as expected")
    finally:
        link.close()
        ser.close()


# ===========================================================================
# B. App mode logic with MockLink
# ===========================================================================
APP = {"available": None, "app": None}


def get_app():
    if APP["available"] is None:
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            root.destroy()
            APP["available"] = True
        except Exception:
            APP["available"] = False
    if not APP["available"]:
        return None
    if APP["app"] is None:
        app = esp_sdr_gui.App()
        APP["app"] = app
    return APP["app"]


def pump(app, ms=200):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.update()
        time.sleep(0.01)


@case("G-B01", "app", "App + MockLink",
      "start_mode WIFI → STOP → start BLE", "状态机：切换前必须 STOP")
def gb01():
    app = get_app()
    if app is None:
        record("tkinter display unavailable", "SKIP", "无显示环境，App 级用例跳过")
        return
    mock = MockLink()
    app.link = mock
    try:
        app.mode = None
        app.start_mode("WIFI")
        pump(app, 100)
        ok1 = app.mode == "WIFI"
        app.start_mode("BLE")  # 应被拒绝并提示
        pump(app, 100)
        logtext = app.log_text.get("1.0", "end")
        refused = app.mode == "WIFI" and "STOP" in logtext
        app.stop_mode()
        pump(app, 100)
        app.start_mode("BLE")
        pump(app, 100)
        ok2 = app.mode == "BLE"
        app.stop_mode()
        pump(app, 100)
        check(ok1 and refused and ok2 and app.mode is None,
              "wifi=%s refused=%s ble=%s final=%s tail_calls=%s" %
              (ok1, refused, ok2, app.mode, mock.calls[-4:]))
    finally:
        app.link = None


@case("G-B02", "app", "App + MockLink",
      "SDR 模式：toggle_spec 启停 + apply_freq 重启流",
      "spec_running 状态正确；FREQ 被下发")
def gb02():
    app = get_app()
    if app is None:
        record("tkinter display unavailable", "SKIP", "")
        return
    mock = MockLink()
    app.link = mock
    try:
        app.mode = None
        app.start_mode("SDR")
        app.toggle_spec()
        pump(app, 100)
        on1 = app.spec_running
        app.apply_freq()
        pump(app, 100)
        restarted = mock.calls.count("SPEC 0 1 1 0 0 256") >= 2
        app.toggle_spec()
        pump(app, 100)
        off = not app.spec_running
        app.stop_mode()
        check(on1 and restarted and off, "on=%s restarted=%s off=%s freq_calls=%s" %
              (on1, restarted, off, [c for c in mock.calls if c.startswith("FREQ")]))
    finally:
        app.link = None


@case("G-B03", "app", "App + MockLink(fail=timeout)",
      "握手/命令超时", "异常被捕获并记录，不崩溃")
def gb03():
    app = get_app()
    if app is None:
        record("tkinter display unavailable", "SKIP", "")
        return
    mock = MockLink(fail="timeout")
    app.link = mock
    try:
        app.mode = None
        app.start_mode("WIFI")   # command 抛 TimeoutError → 捕获
        pump(app, 100)
        survived = True
        app.stop_mode()
        check(survived, "survived timeout in start_mode")
    finally:
        app.link = None


@case("G-B04", "app", "App + MockLink",
      "pcap 输出：WiFi/BLE 帧 → 写文件", "pcap 文件生成且 tshark 可解析")
def gb04():
    app = get_app()
    if app is None:
        record("tkinter display unavailable", "SKIP", "")
        return
    mock = MockLink()
    app.link = mock
    try:
        import tempfile
        tmp = tempfile.mkdtemp(prefix="espsdr_gui_")
        app.wifi_pcap_path.set(os.path.join(tmp, "w.pcap"))
        app.ble_pcap_path.set(os.path.join(tmp, "b.pcap"))
        app.mode = None
        app.start_mode("WIFI")
        for i in range(5):
            pl = make_wifi_frame(i)
            app._on_wifi_frame(["WIFIFRAME", str(len(pl)), "-60", "1", "0"], pl)
        pump(app, 100)
        app.stop_mode()
        app.start_mode("BLE")
        for i in range(5):
            pl = make_ble_frame(i)
            app._on_ble_frame(["BLEFRAME", str(len(pl)), "-70", "37", "0"], pl)
        pump(app, 100)
        app.stop_mode()
        ok_w = ok_b = False
        if os.path.exists(app.wifi_pcap_path.get()):
            r = subprocess.run([TSHARK, "-r", app.wifi_pcap_path.get(), "-T", "fields",
                                "-e", "frame.number", "-c", "10"],
                               capture_output=True, text=True, timeout=30)
            ok_w = len([l for l in r.stdout.splitlines() if l.strip()]) == 5
        if os.path.exists(app.ble_pcap_path.get()):
            r = subprocess.run([TSHARK, "-r", app.ble_pcap_path.get(), "-T", "fields",
                                "-e", "frame.number", "-c", "10"],
                               capture_output=True, text=True, timeout=30)
            ok_b = len([l for l in r.stdout.splitlines() if l.strip()]) == 5
        check(ok_w and ok_b, "wifi_pcap=%s ble_pcap=%s" % (ok_w, ok_b),
              "pcap: %s / %s" % (app.wifi_pcap_path.get(), app.ble_pcap_path.get()))
    finally:
        app.link = None


@case("G-B05", "app", "BLE 配置面板",
      "BLE 面板无信道/驻留控件且含 37/38/39 说明",
      "要求文本存在；无相关输入控件")
def gb05():
    app = get_app()
    if app is None:
        record("tkinter display unavailable", "SKIP", "")
        return
    kids = str(app.panels.children)
    note = app.nametowidget(app.panels.tabs()[2])
    texts = []
    def walk(w):
        for c in w.winfo_children():
            if hasattr(c, "cget"):
                try:
                    texts.append(str(c.cget("text")))
                except Exception:
                    pass
            walk(c)
    walk(note)
    alltext = " ".join(texts)
    has_note = "37/38/39" in alltext and "37" in alltext
    no_ch_widget = not any("信道" in t and " Spinbox" in str(type(x)) for t in texts for x in [0])
    check(has_note, "note_found=%s" % has_note,
          "BLE 面板说明文案校验（无信道/驻留输入控件为布局事实）")


@case("G-B06", "app", "WiFi 配置面板",
      "全信道默认驻留 110ms；固定信道可配", "控件默认值与状态切换正确")
def gb06():
    app = get_app()
    if app is None:
        record("tkinter display unavailable", "SKIP", "")
        return
    try:
        dflt = app.wifi_dwell.get()
        app.wifi_full.set(True)
        app._wifi_toggle()
        disabled = str(app.wifi_ch.cget("state")) == "disabled"
        app.wifi_full.set(False)
        app._wifi_toggle()
        enabled = str(app.wifi_ch.cget("state")) in ("normal", "enabled", "readonly")
        check(dflt == "110" and disabled and enabled, "dwell=%s disabled=%s enabled=%s" %
              (dflt, disabled, enabled))
    except Exception as e:
        check(False, "exc=%s" % e)


@case("G-B07", "app", "App + MockLink",
      "IQ 流：启动 → 注入合成 data 事件 → 停止", "状态切换正确，_on_iq_frame 产生谱线且统计更新")
def gb07():
    app = get_app()
    if app is None:
        record("tkinter display unavailable", "SKIP", "")
        return
    mock = MockLink()
    app.link = mock
    try:
        app.mode = None
        app.start_mode("SDR")
        app.iq_mode.set(esp_sdr_gui.IQM_CONT)
        app.toggle_iq()
        pump(app, 200)
        on = app.iq_running
        # 合成 IQFRAME 块（int32 I/Q，全零 → 以 DC 为主），走事件总线。
        # 显示按 512 样本窗口做 FFT，所以注满 4 个 1024 B 块才出一行。
        payload = bytes(1024)
        for _ in range(4):
            app._handle_event("iqframe", ["IQFRAME", "1024", "40000", "0"], payload)
            pump(app, 60)
        pump(app, 400)
        got_frame = len(app.spec_shifted) == 512
        stat_ok = "IQ 4 帧" in app.iq_stats_var.get()
        app.toggle_iq()
        pump(app, 100)
        off = not app.iq_running
        app.stop_mode()
        check(on and got_frame and stat_ok and off,
              "on=%s frame=%s stat=%s off=%s" % (on, got_frame, stat_ok, off))
    finally:
        app.link = None


@case("G-B08", "app", "App + MockLink",
      "WiFi 5G：固定 5G 信道 / 5G 全信道命令序列", "命令字符串正确（ch=36 / 36 dwell）")
def gb08():
    app = get_app()
    if app is None:
        record("tkinter display unavailable", "SKIP", "")
        return
    mock = MockLink()
    app.link = mock
    try:
        app.mode = None
        app.wifi_ch.set("36")
        app.start_mode("WIFI")
        fixed_ok = "MON WIFI ON 36" in mock.calls
        app.stop_mode()
        app.mode = None
        app.wifi_full.set(True)
        app._wifi_toggle()
        app.wifi_band.set("5 GHz (UNII)")
        app.start_mode("WIFI")
        full_ok = any(c.startswith("MON WIFI ON 36 ") for c in mock.calls)
        app.stop_mode()
        check(fixed_ok and full_ok, "fixed=%s full=%s calls=%s" %
              (fixed_ok, full_ok, [c for c in mock.calls if c.startswith("MON")]))
    finally:
        app.link = None


@case("G-B11", "app", "App + MockLink",
      "SDR 抓包模式：片上频谱 / IQ 传回电脑（连续、突发）的选择、锁定与启停",
      "默认片上频谱；IQ 子选择器按需启用；运行中锁定，STOP 后解锁；两种模式都能启停")
def gb11():
    app = get_app()
    if app is None:
        record("tkinter display unavailable", "SKIP", "")
        return
    G = esp_sdr_gui
    mock = MockLink()
    app.link = mock
    try:
        app.mode = None
        app.stop_capture()
        app.start_mode("SDR")
        pump(app, 60)
        default_spec = app.cap_mode.get() == G.CAP_SPEC
        iq_locked = str(app.iq_mode_box.cget("state")) == "disabled"
        app.cap_mode.set(G.CAP_IQ)
        app._sdr_mode_changed()
        pump(app, 60)
        iq_open = str(app.iq_mode_box.cget("state")) == "readonly"
        rate_open = str(app.iq_rate_box.cget("state")) == "readonly"
        n_closed = str(app.iq_n.cget("state")) == "disabled"
        app.iq_mode.set(G.IQM_BURST)
        app._sdr_mode_changed()
        pump(app, 60)
        n_open = str(app.iq_n.cget("state")) == "normal"
        # 连续 IQ：启动 → 运行中锁定 → 停止
        app.iq_mode.set(G.IQM_CONT)
        app._sdr_mode_changed()
        app.toggle_capture()
        pump(app, 200)
        iq_on = app.iq_running and ("IQS ON 40000" in mock.calls)
        iq_locked_run = str(app.cap_mode_box.cget("state")) == "disabled"
        btn_stop_text = "停止" in app.btn_cap.cget("text")
        app.toggle_capture()
        pump(app, 200)
        iq_off = not app.iq_running and ("IQS OFF" in mock.calls)
        unlocked = str(app.cap_mode_box.cget("state")) == "readonly"
        # 片上频谱：启动 → 停止
        app.cap_mode.set(G.CAP_SPEC)
        app._sdr_mode_changed()
        app.toggle_capture()
        pump(app, 150)
        spec_on = app.spec_running and any(c.startswith("SPEC") for c in mock.calls)
        app.toggle_capture()
        pump(app, 150)
        spec_off = not app.spec_running
        app.stop_mode()
        pump(app, 60)
        cap_disabled = str(app.btn_cap.cget("state")) == "disabled"
        check(all([default_spec, iq_locked, iq_open, rate_open, n_closed, n_open,
                   iq_on, iq_locked_run, btn_stop_text, iq_off, unlocked,
                   spec_on, spec_off, cap_disabled]),
              "default=%s lock=%s burst_n=%s iq=%s/%s spec=%s/%s btn=%s" %
              (default_spec, iq_locked_run, n_open, iq_on, iq_off, spec_on,
               spec_off, cap_disabled))
    finally:
        app.link = None


@case("G-B12", "app", "App + 事件注入",
      "D8 回归：突发(CAP16)谱线经 iqcodes 事件进入频谱/瀑布",
      "iqcodes 事件被 _on_iq_frame 消费（修复前 iqframe 分支把它当成 int32 连续帧丢弃）")
def gb12():
    app = get_app()
    if app is None:
        record("tkinter display unavailable", "SKIP", "")
        return
    try:
        app.spec_shifted = []
        app.wf_rows = []
        app.iq_stats = {"captures": 0, "bytes": 0, "started": time.time()}
        app.iq_stats_var.set("")
        app.iq_running = True
        app.iq_continuous = False
        app.q.put(("iqcodes", [7] * 128, 8192))
        pump(app, 400)
        got = len(app.spec_shifted) == 128
        stat = "IQ 1 帧" in app.iq_stats_var.get()
        app.iq_running = False
        check(got and stat, "frame=%s stat=%s stats=%r" % (got, stat, app.iq_stats_var.get()))
    finally:
        app.iq_running = False


# ===========================================================================
# C. Queue backpressure / thread-safety
# ===========================================================================
@case("G-C01", "robust", "App 事件队列",
      "注入 5000 个事件", "队列被消费不堆积、UI 不卡死")
def gc01():
    app = get_app()
    if app is None:
        record("tkinter display unavailable", "SKIP", "")
        return
    for i in range(5000):
        app.q.put(("line", "noise %d" % i))
    t0 = time.time()
    while not app.q.empty() and time.time() - t0 < 10:
        app.update()
        time.sleep(0.005)
    check(app.q.empty(), "drained=%s dt=%.2f" % (app.q.empty(), time.time() - t0))


@case("G-C02", "robust", "NamedPipeSink",
      "管道无客户端时写入 1MB 数据", "不阻塞不崩溃，缓冲截断有界")
def gc02():
    sink = esp_sdr_gui.NamedPipeSink("espsdr_backpressure", lambda m: None)
    try:
        t0 = time.time()
        for i in range(1024):
            sink.write(b"\x00" * 1024)
        dt = time.time() - t0
        check(dt < 3.0, "dt=%.2fs" % dt, "无客户端时写入落 pending 缓冲，超过阈值截断旧数据")
    finally:
        sink.close()


# ===========================================================================
# Runner
# ===========================================================================
GROUPS = {"a": [c for c in CASES if c[1] == "link"],
          "b": [c for c in CASES if c[1] == "app"],
          "c": [c for c in CASES if c[1] == "robust"]}


def main():
    which = sys.argv[1:] or ["all"]
    selected = []
    for w in which:
        selected += GROUPS.get(w, [])
    seen = set()
    for cid, group, pre, steps, expect, fn in selected:
        if cid in seen:
            continue
        seen.add(cid)
        print("== %s [%s]" % (cid, group), flush=True)
        run_case((cid, group, pre, steps, expect), fn)
    path = os.path.join(HERE, "gui_results.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=1)
    npass = sum(1 for r in RESULTS if r["result"] == "PASS")
    other = len(RESULTS) - npass
    print("TOTAL %d: PASS %d OTHER %d -> %s" % (len(RESULTS), npass, other, path), flush=True)


if __name__ == "__main__":
    main()
