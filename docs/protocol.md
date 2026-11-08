# Serial protocol

All burst firmware speaks the same line-based protocol over native USB or
UART0 (2 Mbaud, 8N1, no flow control). Send ASCII commands terminated by
`\n`; every command gets exactly one text reply line, and some replies are
followed by binary data. Errors start with `ERR`.

## Session

| Command | Reply | Notes |
| --- | --- | --- |
| `SYNC <nonce>` | `SYNC <nonce>` | Resynchronize after an interrupted transfer. |
| `INFO` | e.g. `S3SDR 6 burst 16380` | Family, protocol version, mode, maximum capture length. |
| `CAPS` | `CAPS <flags...>` | Optional features this firmware supports. |
| `VERSION?` | `VERSION {json}` | Git revision and build time, see [firmware-version.md](firmware-version.md). |
| `TRANSPORT?` | `TRANSPORT USB 0` or `TRANSPORT UART <baud>` | The interface replies and payloads are sent on (follows `OUTPUT`; in AUTO, the port this command arrived on). |
| `OUTPUT?` | `OUTPUT AUTO\|USB\|UART` | Output routing state. |
| `OUTPUT USB\|UART\|AUTO` | `OK` (sent on the current output port) | Pin all replies, payloads and monitor frames to one transport while commands keep arriving on either port. `AUTO` (the boot default) restores "output follows the port that sent the last command". Typical split: type commands on the UART, pin data to USB with `OUTPUT USB`. |
| `RELEASE` | `OK` | Give up the receiver lease. |

Only one client controls the radio at a time. The first command takes the
lease; a client on the other interface gets `ERR busy` until `RELEASE` or
five seconds of silence. Read each reply completely before sending the next
command.

Opening some ports resets the board (UART bridges with DTR/RTS wired to
EN/BOOT, the S2's USB-OTG port, the S31's USB Serial/JTAG). Clients should
send `SYNC` and discard everything up to the echoed nonce before talking to
the firmware.

## Receiver settings

| Command | Notes |
| --- | --- |
| `FREQ <MHz>` | Whole MHz, 100–6000 (`RANGE?`). Accepting a frequency does not guarantee PLL lock. |
| `LIMITS?` | `LIMITS {"gain":[min,max,step],"bandwidth":[min,max,step,default]\|null,"rates":[...],"bits":[...]}` |
| `GAIN HARDWARE` / `GAIN MANUAL <index>` / `GAIN?` | Hardware AGC is the default. Indices are PHY table entries, not dB. |
| `BANDWIDTH <MHz>` | Approximate analog bandwidth; `0` selects the widest setting. |
| `GPIO?` / `GPIO <pin> <Z\|0\|1>` | Spare GPIO outputs, e.g. for antenna switches. See [rx-controls.md](rx-controls.md#gpio-outputs). |

Details on gain, bandwidth, calibration and extended tuning are in
[rx-controls.md](rx-controls.md).

## Captures

`CAP16 <samples> <rate>` and `CAP20 <samples> <rate>` return samples as
`(I, -Q)` pairs. Both components are signed two's complement integers.

**Q is negated in the transmitted samples.** Negate the second component
when decoding to obtain standard `(I, Q)` samples.

| Format | Bits per sample | I | -Q |
| --- | --- | --- | --- |
| `CAP16` | 16 | First byte, signed 8-bit | Second byte, signed 8-bit |
| `CAP20` | 20 | Bits 0–9, signed 10-bit | Bits 10–19, signed 10-bit |

`CAP20` samples are packed consecutively, LSB first, with no padding between
samples. The reply is

```text
DATA <samples> <crc32-hex> <capture-microseconds>
```

followed by exactly `ceil(samples * bits * 2 / 8)` bytes. Check the CRC32
(zlib polynomial) before using the data. Only use rates listed in `LIMITS?`:

| Rate code | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| MS/s | 80 | 40 | 20 | 10 | 8 | 4 | 16 | 32 | 10.667 | 6.4 |

A capture is a single burst; there are gaps between captures.

The [S3 continuous stream](iq-stream.md) also sends `(I, -Q)` pairs.
The separate [S31 streaming firmware](s31-streaming.md#iq-packets) sends
`(I, Q)` pairs.

## Streaming modes

- On-chip spectra (`SPEC`, all chips): [spectrum.md](spectrum.md)
- Continuous decimated I/Q on the S3 (`IQS`): [iq-stream.md](iq-stream.md)
- Continuous I/Q over Ethernet/USB on the S31 (separate firmware): [s31-streaming.md](s31-streaming.md)

## Monitor mode (ESP32-C5)

ESP32-C5 firmware also advertises `MON WIFIMON BLEMON`: Wi-Fi promiscuous
sniffing and BLE passive scanning (no SCAN_REQ), shipped on the active
transport as asynchronous binary frames. Send `MON?` on the port that should
own the monitor stream before starting. While any monitor runs, snapshot and
spectrum commands are rejected with `ERR mode_busy` — the SDR dump path owns
the PHY only after every monitor is off. `MON WIFI ON` and `MON BLE ON` may
run together; the Wi-Fi/BLE coexistence scheduler time-slices the RF.

| Command | Reply | Notes |
| --- | --- | --- |
| `MON?` | `MON WIFI:on\|off BLE:on\|off CH:<n> DROPS:<wifi>/<ble>` | Status and dropped-frame counters. |
| `MON WIFI ON <ch> [hop_ms]` | `OK` / `ERR …` | Promiscuous RX. `<ch>` = 2.4 GHz 1–13 or 5 GHz 36–177 (UNII). `hop_ms` cycles the matching band's hop set (2.4 GHz 1–13; 5 GHz 36…165 UNII list). |
| `MON WIFI OFF` | `OK` | Restores the boot-time promiscuous steady state. |
| `MON BLE ON [itvl] [window]` | `OK` / `ERR …` | Passive scan; interval/window in 0.625 ms units, defaults 80/48. |
| `MON BLE OFF` | `OK` | Stops discovery; controller and host stay up. |
| `IQS ON <rate_hz>` | `OK` / `ERR …` | Continuous gapless complex baseband (IQ estimator). `rate_hz` must divide 240 MHz and 80 MHz, 10000–80000; the timer floor is 20 kfires/s, so higher rates emit per-fire window averages. Sustainable rate is transport-bound (≈37 kS/s of int16 over USB-Serial-JTAG). |
| `IQS OFF` | `OK` | |
| `IQS?` | `IQS <rate> <samples> <late> <events> <dropped>` | Stream statistics. |

Monitor frames are binary and never split by other replies:

- `WIFIFRAME <len> <rssi> <ch> <crc32-hex>\n`, followed by `len` bytes: a
  Radiotap header (v0, Flags/Rate/Channel/dBm-Antenna-Signal, FCS flag set)
  then the complete 802.11 frame **with FCS**, truncated as a unit to 1600
  bytes. The payload drops directly into a pcap with
  linktype `LINKTYPE_IEEE802_11_RADIO` (127).
- `BLEFRAME <len> <rssi> <ch> <crc32-hex>\n`, followed by `len` bytes: a
  complete advertising-channel BLE PDU — 2-byte header (PDU type, TxAdd,
  length), advertiser address, advertising data. The channel field is always
  `37`: the C5 controller scans advertising channels 37/38/39 concurrently
  and the hop is invisible to the host stack. Feed it to Wireshark with
  linktype `LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR` (256) and a pseudo-header
  carrying the channel and RSSI.
- `IQFRAME <len> <rate_hz> <crc32-hex>\n`, followed by `len` bytes of
  interleaved little-endian **int32** I/Q pairs (128 samples = 1024 bytes)
  from the continuous IQ-estimator stream: values are boxcar sums of the
  80 MHz baseband, scaled by 2^-6 relative to the ADC codes. Gapless up to
  the transport limit; `IQS?` reports late fires, re-arm events and
  pool-dropped samples.

Check the CRC32 (zlib polynomial) before using a payload. Arriving frames
are dropped while the frame pool is exhausted; `MON?` reports the counts.
A frame whose transmission cannot complete within 150 ms (host stopped
draining the port) is also dropped — possibly truncated, so resynchronise on
the next ASCII header. Backpressure therefore costs frames, never the
command channel: text replies always get through.
