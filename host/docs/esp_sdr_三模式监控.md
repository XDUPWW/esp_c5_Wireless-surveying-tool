# ESP-SDR 三模式监控方案

> 本文档基于 `espsdr/esp-sdr`（ESPARGOS/esp-sdr 克隆，本地扩展）与 ESP32-C5-WROOM-1-N16R8 开发板（16 MB Quad Flash + 8 MB Quad PSRAM），构建环境 ESP-IDF v6.1。
> 配套调试工具：`monitor_gui/esp_sdr_gui.py`（Python + tkinter GUI）。

---

## 1. ESP-SDR 项目背景：什么是 ESP-SDR

[ESP-SDR](https://github.com/ESPARGOS/esp-sdr) 是 ESPARGOS 团队开源的固件项目，它把乐鑫 ESP32 全系列芯片（ESP32、S2/S3/S31、C2/C3/C5/C6/C61、H2）变成**宽带软件无线电（SDR）接收机**：调谐范围 100–6000 MHz，采样率最高 80 MS/s，I/Q 精度 8/10 bit，配合浏览器页面或主机工具实时看频谱、抓 I/Q。

与常规 SDR 不同，ESP-SDR **不加任何外围射频器件**——它利用的是芯片内部 Wi-Fi 射频前端本就具备的接收链路（天线 → LNA → 混频 → 模拟滤波 → ADC），只不过把 ADC 输出引向了芯片里一个**未文档化的调试模块**（项目称之为 **dump engine**，厂家出厂 RF 测试用），而不是 Wi-Fi/BLE 解调器。因此一块几美元的 Wi-Fi 开发板就能覆盖 2.4 GHz（C5 还能收 **5 GHz**）的原始基带。

本工程在 ESP-SDR 基础上扩展了 **Wi-Fi 混杂监听（WiFi Monitor）** 与 **BLE 被动扫描监听（BLE Monitor）** 两种协议级监控模式，与原生 SDR 模式共存于同一固件，通过串口命令切换，形成"同一射频、三种视角"的三模式工具。

## 2. ESP-SDR 项目原理：为什么能拿到 I/Q 数据

### 2.1 层次位置

```
天线 → LNA/混频器 → 模拟滤波(BBTOP 电容阵列) → ADC ──┬─→ Wi-Fi 基带/MAC（正常路径，协议栈）
                                                    ├─→ BLE 链路层（正常路径）
                                                    └─→ dump engine ─→ SRAM bank ─→ CPU（SDR 旁路）
```

每颗芯片的 Wi-Fi 基带里都有一个调试采样块（dump engine），乐鑫在工厂测试（`librftest.a` 的 `adctrig()`）时用它把接收到的基带样本搬进一块普通 SRAM。ESP-SDR 通过逆向 `librftest.a`、翻阅寄存器发现：

- 采样从源头看就是 **ADC 之后、解调之前** 的原始 I/Q——没有经过 Wi-Fi 滤波、同步或解调，所以能看到通带内任意信号，不限于 Wi-Fi；
- 每个 32 位采样字 = I（10 bit 有符号） | Q（10 bit 有符号） | 当时的 AGC 增益索引（8 bit）；
- 引擎把样本写入 SRAM 前先要通过 **SRAM owner 寄存器** 把目标 bank 所有权从 CPU 切给引擎（ESP32-C5：寄存器 `0x60095004`，bit 1 交出整块 128 KB bank `0x40820000–0x40840000`，样本落在 `0x40830000`）；
- 控制寄存器（C5: `0x600a9004`）bit31 使能、bit0–16 样本数、bit19 触发脉冲、bit22 完成标志；**lane 选择寄存器**（C5: `0x600a9018`）选通内部调试信号 0–3 = 接收 I/Q；速率档位（C5: 80/40/20/10/8/4 MS/s）与原始信号源（源 15 = 原始 I/Q）由模式寄存器（`0x600a9008`）控制。

### 2.2 一次快照的完整流程（C5）

1. **保留内存**：`SOC_RESERVE_MEMORY_REGION(0x40820000, 0x40840000)` + 链接脚本 `sram_guard.ld` 断言静态段不越界，防止 heap/栈进入该 bank；
2. **PHY 进入 RX 测试态**：`prepare_rx()` → `phy_set_chanfreq(mhz, mode)`（C5 直接接受任意 MHz，含 5 GHz）→ `phy_pbus_workmode()` → 关 TX（`phy_pbus_xpd_tx_off()`）→ 开 RX（`phy_pbus_xpd_rx_on()`）→ 开采样时钟 → 按需重测 DC（`rx_recalibrate()`，不跑 TX loopback）；
3. **发起捕获**：`adctrig(n-1, 0, 0, divider*2, 0,0,0,0,0)` 把上述寄存器操作一步完成（厂商函数），样本落 SRAM；
4. **哨兵校验**：捕获前填充 `0xa5a0055a` 哨兵，事后任何未改写=截断、尾部 canary 变=溢出；
5. **读出与输出**：CPU 读回 I/Q，打包（CAP16=int8 I/Q 对 / CAP20=压缩 10bit）经串口发给主机。

由于引擎是"快照"工作方式（捕获→搬运→再捕获），帧间有 RF 间隙；C6/C61/S3/S31 另有环形连续模式，C5 当前为快照模式。顺带一提，Zephyr 移植版（cyber-murmel/esp-sdr-zephyr）在 C5/C6 上用另一条路实现**连续采样**：利用基带的 **IQ estimator 硬件累加器**（把 80 MHz 样本硬件累加，CPU 差分得到盒式抽取），CPU 定步轮询、免 DMA，可达 ~4.7 MS/s 无间隙。

## 3. 本项目的新增内容、原理与串口交互协议

### 3.1 新增了什么

| 模块 | 文件 | 说明 |
| --- | --- | --- |
| 模式管理器 | `main/common/mode_manager.{c,h}` | 单一 RF 所有者状态机；监控帧统一队列（8 槽静态池）+ `mon_tx` 输出任务（prio 8）；`MON …` 命令族 |
| WiFi Monitor | `main/common/wifi_monitor.{c,h}` | 混杂嗅探 + 信道轮巡 + Radiotap 封装 |
| BLE Monitor | `main/common/ble_monitor.{c,h}` | NimBLE 被动扫描（不发 SCAN_REQ）+ 完整 BLE PDU 重组 |
| 输出路由 | `main/common/burst_serial.c` | `OUTPUT USB\|UART\|AUTO`：命令口与数据口可分离（UART 进命令、USB 出数据） |
| 构建配置 | `main/CMakeLists.txt`、`sdkconfig.defaults.esp32c5` | C5 使能三模式；N16R8 的 16 MB Flash + 8 MB Quad PSRAM；FFT/帧池放 PSRAM（`EXT_RAM_BSS_ATTR`）保住内部 SRAM 的 dump-bank 链接断言 |

### 3.2 三模式原理与截流点

- **SDR**：如第 2 节，ADC 之后直接取原始 I/Q（旁路整个协议栈）；独占 PHY。
- **WiFi Monitor**：走正常 Wi-Fi 协议栈但在 MAC 层截流——`WIFI_MODE_NULL + promiscuous`（IDF 嗅探器配置，无需连接 AP），`esp_wifi_set_promiscuous_rx_cb()` 回调位于 Wi-Fi 任务上下文，只拷贝入队；帧体带 **Radiotap 头**（Flags/Rate/Channel/dBm 信号，FCS 标志）+ **完整 802.11 帧（含 FCS）**，主机侧可直接写 linktype 127 的 pcap 给 Wireshark。固定信道或按驻留时间轮巡 1–13 信道。
- **BLE Monitor**：NimBLE `ble_gap_disc()` **被动扫描**（`passive=1`，不发 SCAN_REQ），回调重组 **完整广播信道 PDU**（2 字节头 + AdvA + ≤31 B adv data）。C5 控制器并发扫描 37/38/39 三个广播信道，宿主栈不可见信道跳变，故信道字段统一标记 37（硬件限制）。
- **共存规则**：SDR 独占射频（PHY 测试态与协议栈互斥）；WiFi/BLE 两个监控可由 coex 分时共存；监控运行期间 `CAP/SPEC/RXRUN` 一律 `ERR mode_busy`；全关后 `sdr_rx_invalidate()` 强制下次捕获重建 PHY 状态。

### 3.3 串口交互协议（burst protocol 扩展）

物理层：USB-Serial-JTAG 虚拟串口（默认）或 UART0（GPIO11/12，2 Mbaud 8N1），同一套协议。行式文本命令，一条一行应答；二进制块紧跟其头部行，不跨帧穿插（发送互斥锁保证）。

**会话与路由**

| 命令 | 应答 | 说明 |
| --- | --- | --- |
| `SYNC <nonce>` | `SYNC <nonce>` | 上电/乱流后对齐 |
| `INFO` / `CAPS` / `VERSION?` | 固件信息/能力位 | `CAPS` 含 `MON WIFIMON BLEMON DUALSERIAL` |
| `RELEASE` | `OK` | 放弃会话租约（5 秒静默自动过期） |
| `OUTPUT?` / `OUTPUT USB\|UART\|AUTO` | 状态/`OK` | 钉死数据出口；`AUTO`=输出跟随最后命令口（默认） |

**模式切换**：`MON WIFI ON <ch> [hop_ms]` / `MON WIFI OFF`、`MON BLE ON [itvl] [window]` / `MON BLE OFF`、`MON?` 查询（`MON WIFI:on BLE:off CH:n DROPS:w/b`）。无任何监控即 SDR 模式。

**SDR 数据**：`FREQ <MHz>`（100–6000）、`CAP16 <n> <rate>` / `CAP20` → `DATA <n> <crc32> <us>` + 定长二进制；`SPEC <ms> <stride> <upf> <det> <rate> <bins>` → 先回 `SPEC <bins> <rate_hz> <pairs> <MHz>`，随后流式 `SPC1` 二进制帧（28 字节头 + bins 字节功率码 + CRC32，码值 = 20·log10(power)）。

**监控帧**（异步推送，发往当前输出口）：

- `WIFIFRAME <len> <rssi> <ch> <crc32>\n` + Radiotap + 802.11 帧；
- `BLEFRAME <len> <rssi> <ch> <crc32>\n` + 完整 BLE PDU（ch 恒为 37）。

**主机侧 pcap 规则**：WiFi → linktype 127（IEEE802_11_RADIO），载荷原样入包；BLE → linktype 256（BLUETOOTH_LE_LL_WITH_PHDR），加 10 字节伪头（RF channel、RSSI、广播 Access Address `0x8E89BED6`、CRC 有效标志）后入包。两种 pcap 均可被 Wireshark 直接打开，也支持写入命名管道实时分析。

### 3.4 内存与硬件适配（N16R8）

WiFi+BLE 协议栈的静态内存使内部 BSS 一度越出 dump bank 链接断言 37.9 KB；启用 PSRAM 后把 spectrum FFT 工作区（26.6 KB）与监控帧池（14.5 KB）迁入 `.ext_ram.bss`，NimBLE 动态内存走 PSRAM（`BT_NIMBLE_MEM_ALLOC_MODE_EXTERNAL`），内部 SRAM 留足控制器/静态需求。USB 大载荷传输需主机把串口接收缓冲设为 64 KB（Windows usbser 默认 4 KB 会截断 16 KB 级载荷）。
