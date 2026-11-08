# ESP32-C5 无线勘察工具

> 固件基于 [ESP-SDR](https://github.com/ESPARGOS/esp-sdr)：50 块不到的板子，一边看频谱，一边抓 Wi-Fi/BLE 报文，再把它接进 SDR#、Python 和 MATLAB。

这个仓库是基于 [ESPARGOS/esp-sdr](https://github.com/ESPARGOS/esp-sdr) 的 **ESP32-C5** 分支，
不是另一个 SDR 硬件，而是在 ESP-SDR 的原始 I/Q 通路之外，把 ESP32 自带的协议栈接收能力也用起来：

- **协议栈抓包**：Wi-Fi 混杂监听（Radiotap + 完整 802.11 帧）、BLE 被动扫描（完整广播 PDU），
  直接存成 Wireshark 能开的 pcap，也可以走命名管道实时看。
- **连续 I/Q**：C5 的 SRAM bank 不能乒乓，所以用调制解调器的 I/Q estimator 做无间隙采样
  （10k–80k S/s），思路移植自 [cyber-murmel/esp-sdr-zephyr](https://github.com/cyber-murmel/esp-sdr-zephyr)。
- **接进桌面 SDR 生态**：主机侧模拟一台 PlutoSDR（libiio/iiod），SDR# 的 PlutoSDR 源、
  Python `pylibiio`、MATLAB 自带的 `iio_readdev` 都能直接控制它、读它的样本。

配一个单文件 GUI（`monitor_gui/esp_sdr_gui.exe`，零安装）就能把上面三件事点出来。

## 三种用途，一台射频

射频前端只有一个：SDR 要让 PHY 进 RX 测试模式，监听要把 PHY 交给协议栈，两种状态没法共存。
所以固件里有一个 `mode_manager` 负责"谁在用射频"，**同一时刻只跑一种，切换前必须 STOP**。

| 模式 | 数据从哪来 | 主机拿到什么 | 实测 |
| --- | --- | --- | --- |
| SDR · 片上频谱 | dump engine → 固件做 FFT | 256–2048 个功率 bin（`SPC1`） | ~430 帧/s @80 MS/s |
| SDR · 连续 I/Q | IQ estimator 累加器 | int32 交错 I/Q，10k–80k S/s | 80 kS/s ≈ 636 kB/s |
| SDR · 突发 I/Q | dump engine 的 SRAM bank | 单次快照 256–16380 点 | ~20 帧/s |
| Wi-Fi Monitor | 802.11 协议栈混杂回调 | Radiotap + 完整 802.11 帧 | ~400 帧/s @繁忙 2.4G |
| BLE Monitor | NimBLE 被动扫描 | 完整广播 PDU（37/38/39） | ~40 帧/s @实验室 |
| SDR# 桥接 | 主机侧 libiio/iiod | int16 I/Q（由连续 I/Q 插值） | 客户端请求速率 |

## 快速开始

### 1. 构建固件

需要 ESP-IDF（本分支在 **v6.1** 上验证；上游固定的旧 IDF 缺少 C5 的 dump-engine 符号）：

```bash
idf.py set-target esp32c5
idf.py build
```

Windows 下本仓库带了一键脚本 `build_esp32c5.bat`（会自己设好 IDF 环境变量）。

> 改过 `sdkconfig.defaults.esp32c5` 之后，记得**先删 `sdkconfig` 再构建**，否则旧值会静默生效。

### 2. 烧录（N16R8）

```bash
python -m esptool --chip esp32c5 -p COM10 -b 460800 \
  --before default-reset --after hard-reset write-flash \
  --flash-mode dio --flash-freq 80m --flash-size 16MB \
  0x2000 build/bootloader/bootloader.bin \
  0x8000 build/partition_table/partition-table.bin \
  0x10000 build/esp_sdr.bin
```

### 3. 接上位机

- GUI（Windows）：`host/monitor_gui/` 下 `build_exe.bat` 打成单文件 exe，或用 `python host/monitor_gui/esp_sdr_gui.py` 直接跑（只需 pyserial）
  —— 连 COM10，选模式，点开始。SDR 模式出频谱/瀑布；WiFi/BLE 模式出设备表并落 pcap。
- 命令行也行：串口协议是文本行 + CRC32 保护的二进制帧，见 [`docs/protocol.md`](docs/protocol.md)
  （`MON WIFI ON <ch> [hop_ms]`、`MON BLE ON`、`IQS ON <rate>`、`SPEC …`、`CAP16 …`）。

## 仓库结构（本分支新增）

```
main/common/mode_manager.{c,h}   射频归属、帧池与发送任务、MON*/IQS* 命令
main/common/wifi_monitor.{c,h}   混杂模式、信道/轮巡、Radiotap 组装
main/common/ble_monitor.{c,h}    NimBLE 被动扫描与广播回调
main/common/iq_stream.{c,h}      IQ estimator + 20 kHz ISR + PSRAM 环形缓冲
main/common/burst_serial.{c,h}   命令/租约 + 有界发送（背压只丢帧）
main/families/c5_c6_c61/         C5/C6/C61 共享的 receiver（本分支接入上述模式）
sdkconfig.defaults.esp32c5       N16R8 的 16 MB flash / 8 MB PSRAM、USB-only、NimBLE observer

host/monitor_gui/                单文件 GUI（源码；build_exe.bat 可自行打包）
host/bridge/                     Pluto/iiod 桥接（源码，可无界面运行）
host/tools/                      离线 I/Q 分析、Python libiio 示例
host/examples/                   GUI 使用说明、pcap 汇总、Wireshark 管道脚本
host/tests/                      固件命令桩、GUI mock、自测报告
host/docs/                       设计文档、三模式说明
docs/post/                       项目介绍文章（含配图）
docs/upstream-README.md          上游 ESPARGOS README 原文（保留以便对照）
```

上位机只放了源码，避免把几十 MB 的打包 exe 塞进仓库；需要现成 exe 就按
`host/monitor_gui/README.md` 里的 `build_exe.bat` 打一个（PyInstaller，一条命令）。

## 文档

- 项目来龙去脉（为什么先做抓包、再移植连续采样、最后接 SDR#）：
  [`docs/post/ESP-SDR-项目介绍.md`](docs/post/ESP-SDR-项目介绍.md)
- GUI 四个模式的操作步骤与示例：`host/examples/GUI使用说明.md`
- 固件协议与帧格式：`docs/protocol.md`；片上频谱：`docs/spectrum.md`；原理：`docs/how-it-works.md`
- 上游 README（ESPARGOS 原版，含全芯片支持矩阵）：[`docs/upstream-README.md`](docs/upstream-README.md)

## 已知限制

- 真实 I/Q 带宽受传输限制：USB-Serial-JTAG 约 640 kB/s，因此连续 I/Q 上限 **80 kS/s**
  （SDR# 里显示的更大带宽是主机侧插值，只有中心一段是真数据）。
- C5 调谐步进 **1 MHz**（固件 `FREQ` 只到整 MHz）；100–6000 MHz 都接受，但 PLL 只在
  2.4 GHz 与 5 GHz 附近稳定锁定。
- 增益与幅度**未校准**，只适合相对观察，不作计量。
- BLE 只能被动扫描，控制器固定扫 37/38/39，抓包信道字段统一记为 37。
- 串口同一时刻只能有一个消费者：GUI、独立桥接、其它工具互斥。

## 许可与致谢

本仓库与上游一样使用 **GPL-3.0-or-later**（见 `LICENSE`）：分发修改版时需要一并提供源码。
新增的监听/连续采样/桥接代码同样按 GPL-3.0-or-later 发布。

- 原始 ESP-SDR 固件与 I/Q 采集机制：ESPARGOS 项目（Florian Euchner、Marc Gauger 等）
- 连续采样思路：`cyber-murmel/esp-sdr-zephyr`（Murmel）
- 主机侧包装成标准 SDR 的思路：`z2labs/esp-sdr-bridge`（Zoltan Doczi）
- 片上 FFT 使用 ESP-DSP（Apache-2.0）
