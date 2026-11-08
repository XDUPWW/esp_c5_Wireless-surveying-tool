#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pcap_summary.py - 汇总 GUI 存下来的 WiFi / BLE pcap（纯标准库，不需要 tshark）。

支持本工具写出的两种 linktype：
  - 127  LINKTYPE_IEEE802_11_RADIO : Radiotap + 完整 802.11 帧
  - 256  LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR : 10 B 伪头 + AA + 广播 PDU + CRC

用法：
    python examples/pcap_summary.py wifi_monitor.pcap
    python examples/pcap_summary.py ble_monitor.pcap --top 10
"""
import argparse
import collections
import struct
import sys

LINKTYPE_WIFI = 127
LINKTYPE_BLE = 256


def read_pcap(path):
    data = open(path, "rb").read()
    if len(data) < 24:
        sys.exit("不是 pcap：%s" % path)
    magic = data[:4]
    if magic == b"\xd4\xc3\xb2\xa1":
        endian = "<"
    elif magic == b"\xa1\xb2\xc3\xd4":
        endian = ">"
    else:
        sys.exit("不支持的 pcap magic: %r" % magic)
    # global header: magic(4) vmaj(2) vmin(2) thiszone(4) sigfigs(4) snaplen(4) network(4)
    net = struct.unpack(endian + "I", data[20:24])[0]
    off = 24
    recs = []
    while off + 16 <= len(data):
        ts, tus, incl, _orig = struct.unpack(endian + "IIII", data[off:off + 16])
        off += 16
        recs.append((ts + tus / 1e6, data[off:off + incl]))
        off += incl
    return net, recs


def wifi_summary(recs, top):
    types = collections.Counter()
    chans = collections.Counter()
    rssi = []
    bssids = collections.Counter()
    for _, pkt in recs:
        if len(pkt) < 4:
            continue
        rt_len = struct.unpack("<H", pkt[2:4])[0]
        if len(pkt) < rt_len + 2:
            continue
        fc = pkt[rt_len] | (pkt[rt_len + 1] << 8)
        ftype = (fc >> 2) & 3
        subtype = (fc >> 4) & 0xF
        types["%s/%d" % ({0: "mgmt", 1: "ctrl", 2: "data"}.get(ftype, "?"), subtype)] += 1
        # radiotap: 扫描 present 字段里的 Channel / dBm
        # radiotap present 位：1=Flags 2=Rate 3=Channel 5=dBm antenna signal
        bits = struct.unpack("<I", pkt[4:8])[0] if len(pkt) >= 8 else 0
        has_flags = bits & (1 << 1)
        has_rate = bits & (1 << 2)
        has_chan = bits & (1 << 3)
        has_dbm = bits & (1 << 5)
        p = 8
        if has_flags:
            p += 1
        if has_rate:
            p += 1
        if has_chan:
            if len(pkt) >= p + 4:
                freq = struct.unpack("<H", pkt[p:p + 2])[0]
                ch = 0
                if freq == 2484:
                    ch = 14
                elif 2412 <= freq < 2484:
                    ch = (freq - 2407) // 5
                elif freq >= 5000:
                    ch = (freq - 5000) // 5
                chans[ch or freq] += 1
            p += 4
        if has_dbm and len(pkt) > p:
            rssi.append(struct.unpack("b", pkt[p:p + 1])[0])
        if ftype == 0 and len(pkt) >= rt_len + 22:
            bssids[pkt[rt_len + 16:rt_len + 22].hex(":")] += 1
    print("帧数: %d" % len(recs))
    print("类型分布(前 8):", dict(types.most_common(8)))
    print("信道分布:", dict(chans.most_common(8)))
    if rssi:
        print("RSSI: 平均 %.1f dBm, 范围 %d..%d dBm" % (sum(rssi) / len(rssi), min(rssi), max(rssi)))
    print("BSSID 前 %d:" % top)
    for mac, n in bssids.most_common(top):
        print("  %s  %d 帧" % (mac, n))


def ble_summary(recs, top):
    pdu = collections.Counter()
    adva = collections.Counter()
    rssi = []
    chans = collections.Counter()
    for _, pkt in recs:
        if len(pkt) < 10 + 4 + 2:
            continue
        ch, rssi_v = pkt[0], struct.unpack("b", pkt[2:3])[0]
        chans[ch] += 1
        rssi.append(rssi_v)
        p = 10 + 4                      # 10 B 伪头 + 4 B Access Address
        hdr = pkt[p] | (pkt[p + 1] << 8)
        ptype = hdr & 0x0F
        length = (hdr >> 8) & 0xFF
        pdu[ptype] += 1
        if ptype in (0, 2, 4) and len(pkt) >= p + 2 + 6:      # ADV_IND / NONCONN / SCAN_RSP
            adva[pkt[p + 2:p + 8].hex(":")] += 1
        _ = length
    print("帧数: %d" % len(recs))
    print("PDU 类型:", dict(pdu.most_common(8)))
    print("抓包信道字段:", dict(chans))
    if rssi:
        print("RSSI: 平均 %.1f dBm, 范围 %d..%d" % (sum(rssi) / len(rssi), min(rssi), max(rssi)))
    print("AdvA 前 %d:" % top)
    for mac, n in adva.most_common(top):
        print("  %s  %d 帧" % (mac, n))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--top", type=int, default=5)
    a = ap.parse_args(argv)
    net, recs = read_pcap(a.path)
    print("文件: %s  linktype=%d  记录=%d" % (a.path, net, len(recs)))
    if net == LINKTYPE_WIFI:
        wifi_summary(recs, a.top)
    elif net == LINKTYPE_BLE:
        ble_summary(recs, a.top)
    else:
        sys.exit("未知 linktype %d" % net)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
