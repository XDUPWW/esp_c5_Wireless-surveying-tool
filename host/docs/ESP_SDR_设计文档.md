# ESP-SDR 三模式（SDR / Wi-Fi Monitor / BLE Monitor）设计文档

> 适用对象：`espsdr/esp-sdr`（ESP32-C5 固件）+ `monitor_gui/esp_sdr_gui.py`（主机调试工具）
> 硬件：ESP32-C5-WROOM-1-N16R8（16 MB quad-SPI Flash / 8 MB quad-SPI PSRAM，单核 RISC-V 240 MHz）
> 工具链：ESP-IDF v6.1，`esp-dsp` 子模块；固件仅通过 USB-Serial-JTAG 收发（UART0 已关闭）

---

## 1. 需求分析

### 1.1 功能需求

| 编号 | 需求 | 验收判据 |
| --- | --- | --- |
| FR-1 | 三模式互斥共存：SDR / Wi-Fi Monitor / BLE Monitor，同一时刻只有一个"主人"占用射频 | 未停止当前模式时切换必须被拒绝；停止后可切换 |
| FR-2 | SDR 支持 2.4 GHz 与 **5 GHz**（ESP32-C5 特有）任意 100–6000 MHz 调谐 | `FREQ <MHz>` 生效；5 GHz 实测 5180 MHz 帧可达 |
| FR-3 | SDR 抓包两种模式：**片上频谱 (On-chip FFT)** 与 **IQ 传回电脑 (I/Q Streaming)**；后者再分**连续（无间隙）**与**突发（快照连拍）** | GUI 单按钮启停；运行中锁定模式选择器；三种通路均出图 |
| FR-4 | 连续 IQ 支持 10k–80k S/s，速率须同时整除 240 MHz 与 80 MHz | 80 kS/s 实测 625 帧/s、636 kB/s 达标 |
| FR-5 | 突发 IQ 支持 256–16380 样本、多档采样率（80/40/20/10/8/4 MS/s） | `CAP16 <n> <rate>` 单快照 `DATA` 载荷完整、CRC 正确 |
| FR-6 | 模拟带宽可选 Open（全带宽）或 Filter（11–48 MHz） | `BANDWIDTH 0` / `BANDWIDTH <MHz>`；越界回 `ERR` |
| FR-7 | Wi-Fi Monitor：固定信道（2.4G 1–13 / 5G 36–177）或全信道轮巡（驻留默认 110 ms 可配） | `MON WIFI ON <ch> [hop_ms]`；`MON?` 的 `CH:` 随轮巡变化 |
| FR-8 | Wi-Fi Monitor 回传 **Radiotap + 完整 802.11 帧**，可直接落 pcap（linktype 127） | `tshark -r` 能解析频率/信号强度/帧类型 |
| FR-9 | BLE Monitor：被动广播扫描，不发 SCAN_REQ，回传**完整 BLE PDU**（header+payload） | `BLEFRAME` 长度自洽；pcap（linktype 256）可解析 adv 地址 |
| FR-10 | 主机侧 GUI：自动识别/手动选择端口、模式切换需先 STOP、pcap 落盘与 Wireshark 命名管道、SDR 频谱/瀑布/色标 | 对应 GUI 用例全过 |
| FR-11 | 默认静默：上电/停止后串口无自发数据 | `MON?` 前后无帧输出 |

### 1.2 非功能需求

| 编号 | 需求 | 指标 |
| --- | --- | --- |
| NFR-1 | 吞吐：连续 IQ 80 kS/s 端到端 | ≥ 600 kB/s，丢帧如实计数 |
| NFR-2 | 稳定性：长时间流式 | 30 s 连续 IQ 零失步；60 s×2 长稳无重启 |
| NFR-3 | 背压安全：主机停止读取 | 只丢帧，不得阻塞命令通道（必须在 1 s 内仍可应答） |
| NFR-4 | 内存：不得侵占射频 dump bank | 内部 BSS `_bss_end ≤ 0x40820000`（链接期断言） |
| NFR-5 | 可测试性：命令与二进制帧可在无 GUI 情况下被脚本驱动 | 提供 `SYNC/RELEASE/CAPS/MON?/IQS?/IQDBG` 等测试钩子 |
| NFR-6 | 协议向后兼容 | 新增能力只追加（`CAPS` 标志、写新文件），不改 upstream 命令语义 |

### 1.3 约束与假设

- ESP32-C5 单核，无第二个 CPU 做 DSP；重活必须在中断/任务与主机之间分工。
- 官方未公开 dump engine / IQ estimator 寄存器，配置依据 libphy 反汇编与社区（cyber-murmel/esp-sdr-zephyr）结论。
- BLE 控制器固定同时扫描 37/38/39，**无法**固定单信道或设置驻留时间 → 抓包信道字段统一填 37。
- 5 GHz 仅 UNII 子集（36–177）可调；信道 99 之类非法信道返回 `ERR channel`。
- 主机端不允许依赖 numpy/matplotlib（要求 stdlib + pyserial），故 FFT 为纯 Python。

---

## 2. 项目背景

### 2.1 什么是 ESP-SDR

ESP-SDR（上游仓库 [ESPARGOS/esp-sdr](https://github.com/ESPARGOS/esp-sdr)）把乐鑫 Wi-Fi SoC 变成宽带软件无线电：利用 Wi-Fi 基带里**未文档化的 dump engine**，在 PHY 进入 RX test 模式后把基带 I/Q 直接写进一块预留 SRAM，CPU 直接读走，**绕过 Wi-Fi/BLE 调制解调器**。覆盖 ESP32/S2/S3/C2/C3/C5/C6/C61/H2 等家族。

### 2.2 为什么能拿到 I/Q（原理）

1. **dump engine**：`phy_pbus_workmode`/`phy_stop_tx_tone` 等函数把 PHY 置为"接收直通"，基带 ADC 输出被写入指定 SRAM bank（C5 上该 bank 由链接期预留 128 KiB，`sram_guard.ld` 保证静态区不越界）。CPU 读该 bank 即得到原始 I/Q，采样率由 ADC 时钟决定（80/40/20/10/8/4 MS/s）。
2. **IQ estimator（连续路径）**：C5 没有可乒乓的循环 dump 通路，连续采样改用调制解调器的 IQ estimator —— 硬件对 80 MHz 基带做累加（窗口上限 32768 输入样本 ≈ 409 µs），CPU 周期性读取累加寄存器并对差得到 boxcar 抽取后的 I/Q。本项目用 **esp_timer ISR（50 µs，20 kHz）** 读取/重配累加器，按"欠采样累加"合成任意整数速率（10k–80k），写入 PSRAM 无锁环形缓冲。
3. 因此"能拿到 I/Q"不是靠协议栈，而是靠**测试模式 + 未文档化寄存器**；协议栈（Wi-Fi/BLE）在 SDR 模式下不参与。

### 2.3 本项目在上游之上新增了什么

| 能力 | 说明 |
| --- | --- |
| 三模式框架 | `mode_manager` 统一管理 SDR / Wi-Fi Monitor / BLE Monitor 的射频归属与互斥 |
| Wi-Fi 混杂监听 | `wifi_monitor` 以 Radiotap + 完整 802.11 帧上送，支持 2.4G/5G 与自动轮巡 |
| BLE 被动监听 | `ble_monitor` 以完整广播 PDU 上送（NimBLE，不发 SCAN_REQ） |
| 连续 IQ | `iq_stream` 从 Zephyr 版 esp-sdr 移植并改造为 ISR 架构（原轮询在 IDF 下会饿死 IDLE 触发看门狗） |
| 传输加固 | 有界发送 + FIFO 等待让出，杜绝"主机不读 → 命令通道被饿死" |
| 主机调试工具 | 单文件 tkinter GUI：端口识别、模式机、SDR 双抓包模式、电平/色标、pcap + Wireshark 命名管道 |
| 测试资产 | 固件命令桩、GUI mock、端到端脚本与自测报告 |

---

## 3. 可行性分析

### 3.1 技术可行性

| 关注点 | 结论 | 证据 |
| --- | --- | --- |
| 原始 I/Q 获取 | 可行 | 上游 + 本项目在 C5 实测：`CAP16` 单快照 CRC 全对；80 MS/s 满速 |
| 连续无间隙采样 | 可行（受传输带宽约束） | 10k/40k/80k 三档速率达成率 86%/102%/100%（80k 需背压丢帧） |
| 5 GHz 调谐 | 可行 | 实测 5180 MHz 帧带 radiotap freq=5180 |
| Wi-Fi 混杂 | 可行 | 2.4G 繁忙信道 ~400 帧/s；5G 固定/轮巡均出帧 |
| BLE 被动扫描 | 可行 | 被动广播 ~40 帧/s（实验室环境），pcap 可解析 |
| USB-Serial-JTAG 带宽 | 上限约 640 kB/s（实测） | 80 kS/s 连续 IQ = 636 kB/s 恰在上限附近，故必须丢帧策略 |

### 3.2 资源可行性

- **内存**：监视帧池 8×1600 B、IQ 环 4096 对 int32（32 KB）均在 PSRAM（`EXT_RAM_BSS_ATTR`）；内部 BSS 受 `sram_guard.ld` 断言保护（`_bss_end ≤ 0x40820000`）。
- **CPU**：单核 240 MHz。20 kHz ISR（约 2–3 µs/次）+ mon_tx/iq_stream 两个 prio-8 任务 + 命令循环，实测 80 kS/s 下 UI/命令仍可响应。
- **Flash**：app 分区 4 MB，当前固件 0xfe830（约 1 MB，75% 空闲），16 MB Flash 余量充足。

### 3.3 风险与对策

| 风险 | 影响 | 对策（已实现） |
| --- | --- | --- |
| 主机不读串口 | 发送线程忙等 → 命令循环被饿死，整机"假死" | 发送核心"忙等 200 µs 后让出"；监视帧 150 ms 有界发送，超时丢帧；`mon_tx` 曾等待则主动 `vTaskDelay(1)` |
| 主机侧解析跟不上（625 帧/s） | 串口缓冲溢出 → 字节流失步 → 显示错乱 | GUI 显示按 tick 取帧、512 样本一次 FFT、瀑布 C 级渲染、重绘 50 ms |
| 优先级反转 | 文本应答抢不到 TX 互斥锁 | 发送任务竞争后让出；文本应答走长超时路径 |
| 单核看门狗 | 高优先级紧循环饿死 IDLE | 不采用轮询架构，ISR + 4 ms 节拍排空 |
| 5G/BLE 硬件限制 | 用户误以为可固定 BLE 信道 | GUI 明确标注限制；非法参数拒绝 |

### 3.4 结论

技术、资源、进度均可行；主要代价集中在**主机与设备之间的流控**，已在固件与 GUI 两侧分别加固并通过长稳验证。

---

## 4. 概要设计

### 4.1 总体框架

#### 4.1.1 启动链路（上电 → app_main）

```
上电复位
  └─ ROM 一级引导：读 strapping 引脚决定启动介质
       ├─ UART/USB 下载模式 → 串口烧录
       └─ SPI Flash 启动 → 从 0x2000 读 bootloader 头
  └─ 二级引导 bootloader（0x2000）
       ├─ 读 0x8000 分区表
       ├─ 配置 flash cache / MMU 映射、PSRAM
       └─ 校验并加载 app（0x10000）到 IRAM/DRAM（.text/.data 直载，其余按需 cache）
  └─ 应用启动
       ├─ 端口/驱动：nvs_flash_init → usb_serial_jtag_driver_install
       ├─ 系统服务：event loop → esp_wifi_init/start（STA 空模式）
       ├─ 硬件初始化：esp_wifi_set_promiscuous(true) → set_channel(1) → prepare_rx()（dump engine）
       ├─ 组件初始化：burst_serial_init() → mode_manager_init()（帧池/worker）
       └─ 创建 main_task（app_main 本身）→ 进入 burst_serial_serve(handle_command) 命令循环
  └─ 用户 app_main 之后：主机侧 GUI 通过 SYNC 握手建立会话
```

> 深度睡眠唤醒走 ROM 的 deep-sleep 恢复路径（不重跑 bootloader），本项目未使用深睡，故不作特殊处理。

#### 4.1.2 固件分层

```
┌──────────────────────────────────────────────────────────────┐
│ 传输层  burst_serial：行命令解析 / 租约 / 有界发送 / OUTPUT 路由 │
├──────────────────────────────────────────────────────────────┤
│ 模式层  mode_manager：射频归属、帧池、worker、MON?/IQS 命令      │
├───────────────┬───────────────┬──────────────────────────────┤
│ wifi_monitor  │ ble_monitor   │ iq_stream / spectrum / dump   │
│ (混杂回调)     │ (NimBLE 回调)  │ (dump engine / IQ estimator)  │
├───────────────┴───────────────┴──────────────────────────────┤
│ 平台层  esp_wifi(NimBLE) / esp_timer / PSRAM / esp-dsp(FFT)   │
└──────────────────────────────────────────────────────────────┘
```

#### 4.1.3 主机（GUI）分层

```
tkinter UI（App：模式机 / 面板 / 绘图）
   │ 事件队列（线程安全）
Link（串口读线程：逐帧解析 → 事件）           IQStreamer（突发快照线程）
   │                                          │
PcapStream（linktype 127/256）→ 文件 / NamedPipeSink（Wireshark）
```

### 4.2 各模块功能划分

| 模块 | 文件 | 职责 |
| --- | --- | --- |
| 传输层 | `main/common/burst_serial.{c,h}` | 双端口命令解析、5 s 租约、`OUTPUT` 路由、互斥发送、有界发送 |
| 模式管理 | `main/common/mode_manager.{c,h}` | 射频互斥、帧池/worker、`MON*` `IQS*` `MON?` 命令、丢弃统计 |
| Wi-Fi 监听 | `main/common/wifi_monitor.{c,h}` | 混杂模式开启、信道/轮巡、Radiotap 组装、回调上送 |
| BLE 监听 | `main/common/ble_monitor.{c,h}` | NimBLE 被动扫描、广播回调、PDU 上送 |
| 连续 IQ | `main/common/iq_stream.{c,h}` | estimator 配置、20 kHz ISR 定速、PSRAM 环、块上送 |
| 片上频谱 | `main/common/spectrum.c`, `spectrum_fft.h`, `spectrum_dc.h` | 采样 + 片上 FFT + `SPC1` 帧 |
| 快照捕获 | `main/common/burst_capture.h`, `ring_capture.c` | `CAP16/CAP20/CAP/RXRUN` 原始 I/Q |
| 射频控制 | `main/common/{rx_tuning,rx_bandwidth,rx_recalibration,burst_gain}.c` | 调谐、模拟带宽、重校准、增益 |
| 目标常量 | `main/targets/esp32c5/{chip.h,tuning.h,sram_guard.ld}` | dump 寄存器/缓冲地址、链路断言 |
| 命令聚合 | `main/families/c5_c6_c61/receiver.c` | `app_main`、命令分派、模式忙时拒绝快照 |
| GUI | `monitor_gui/esp_sdr_gui.py` | 端口、模式机、SDR 三种抓包、监控表、pcap/管道、绘图与色标 |

### 4.3 数据结构设计

#### 4.3.1 固件

```c
/* 监视帧池（PSRAM） */
#define MON_FRAME_MAX  1600u      /* 单帧载荷上限（Wi-Fi 帧按单位截断）*/
#define MON_POOL_SIZE  8u         /* 静态槽位，Wi-Fi/BLE/IQ 共用 */
typedef struct {
    uint8_t type;                 /* MON_TYPE_WIFI/BLE/IQ */
    int8_t  rssi;
    uint8_t channel;
    uint16_t len;
    uint8_t data[MON_FRAME_MAX];
} mon_frame_t;

/* 连续 IQ 环形缓冲（PSRAM，单生产者 ISR / 单消费者任务，无锁） */
#define IQ_BLOCK       128u       /* 每块 128 对 int32 = 1024 B */
#define IQ_RING_PAIRS  4096u      /* 2 的幂；≥ 2 块 */
static EXT_RAM_BSS_ATTR int32_t ring[IQ_RING_PAIRS * 2];  /* I/Q 交错 */
static volatile unsigned ring_head;   /* 写指针（ISR）*/
static unsigned ring_tail;            /* 读指针（任务）*/

/* 片上频谱帧头（28 B，magic "SPC1"）*/
typedef struct { uint32_t magic, frames; uint64_t index;
                 uint32_t nbins; uint16_t merged; uint8_t flags, gain;
                 uint16_t zero; uint8_t log2n, type; } spec_header_t;  /* 之后 nbins 字节功率码 + CRC32 */
```

#### 4.3.2 主机 GUI

```python
class Link:            # 串口读线程 + 命令/应答同步 + 逐帧解析
    buf: bytearray; data_wire_fn: 可选；spec_active: SPEC 流开关
class IQStreamer(Thread):   # 突发快照线程：循环 CAP16 → 本地 FFT → 事件
class PcapStream:      # classic pcap 写文件 + 多路管道 sink
class NamedPipeSink:   # Windows 命名管道，无客户端时丢弃、有界缓冲
class App(tk.Tk):      # 全局状态机 + 三个面板
    mode: None|'SDR'|'WIFI'|'BLE'
    cap_mode/iq_mode/iq_rate/iq_n：SDR 抓包选择
    wf_rows: list[bytes]; wf_floor/wf_range/_wf_lut：瀑布电平与色表
```

`wf_rows` 保存**整行**功率码（`bytes`，0–255），按显示宽度用列→bin 映射表插值；`_wf_lut` 是 256 项 `(floor,range)` 拉伸后的 RGB 表。

### 4.4 各模块功能设计

#### 4.4.1 burst_serial（传输层）

- 两个端口各一套行解析器；`\n` 结束，超长回 `ERR command_length`。
- **租约**：首个命令的端口获得所有权，5 s 无命令自动释放；另一端回 `ERR busy`；`RELEASE` 主动释放。
- `OUTPUT USB|UART|AUTO`：把应答/数据钉到指定口（默认 AUTO = 跟随最近命令端口）。
- **发送核心**（关键）：`send_nolock_deadline()` 持 `tx_mutex`，USB FIFO 不可写时忙等 ≤200 µs，仍不可写则 `vTaskDelay(1)` 让出；`burst_serial_send()` 用长超时，`burst_serial_send_timeout()` 用于高码率帧（有界）。

#### 4.4.2 mode_manager

- `mon_frame_push()`：从池取槽 → 填头 → 入 `work_q`；池空则计数丢弃。
- `worker_main()`：取帧 → 计算 CRC32 → 组 `WIFIFRAME/BLEFRAME/IQFRAME` 头 → **150 ms 有界发送**（失败计 `tx_fail`）→ 归还槽位；若本次发送等待过总线则 `vTaskDelay(1)`。
- 命令：`MON?`、`MON WIFI ON/OFF`、`MON BLE ON/OFF`、`IQS ON/OFF/?`、`IQDBG`。
- `mode_manager_busy()`：任一模式运行中，`CAP*`/`RXRUN`/`SPEC` 回 `ERR mode_busy`。

#### 4.4.3 wifi_monitor

- 开启 `esp_wifi_set_promiscuous(true)` + `esp_wifi_set_promiscuous_rx_cb(cb)`，过滤器按需选管理/数据/控制帧。
- 回调仅：取 rssi/channel、组装 15 B Radiotap（Flags/Rate/Channel/dBm/FCS 标志） + 完整 802.11 帧（含 FCS）→ `mon_frame_push`。**回调内不做重处理**。
- 轮巡：每 `hop_ms` 调 `esp_wifi_set_channel()`，2.4G 表 1–13，5G 走 UNII 表 36…165。

#### 4.4.4 ble_monitor

- NimBLE 初始化后启用 **被动扫描**（不发 SCAN_REQ/SCAN_RSP）：`ble_gap_disc(BLE_OWN_ADDR_PUBLIC, BLE_HS_FOREVER, &params, cb, ...)`，`params.passive=1`，`itvl/window` 默认 0x50/0x30（0.625 ms 单位）。
- 回调 `GAP_EVENT_DISC`：取 `event->disc.addr.val`、`event->disc.rssi`、`event->disc.data/len` → 完整 PDU（2 B 头 + AdvA + AD）→ `mon_frame_push`（channel 填 37）。

#### 4.4.5 iq_stream

- `iq_configure()`：配置 estimator 控制寄存器（0x600a044c/450/464/468），源 = I/Q、计数上限 32768。
- ISR（50 µs）：读累加和并差分得 `di/dq`（每 7 次 fire 重新 armed 一次算子窗口）→ 用 `iq_owed_acc += rate_hz; k = acc/20000` 合成任意速率 → 写环（空间不足则丢）。
- `iq_task`：每 4 ms 排空环，128 对一块送 `mon_frame_push(MON_TYPE_IQ)`。
- 统计：`IQS?` 输出 `rate samples late events dropped`；`IQDBG` 额外给 `txfail/iqdrop`。

#### 4.4.6 spectrum

- 从 dump 缓冲取块 → 窗 + 片上 FFT（`esp-dsp`）→ 功率码（`20·log10|X|`，0–255）→ `SPC1` 帧；支持 256/512/1024/2048 点与 mean/max-hold。

#### 4.4.7 GUI

- `Link`：独立读线程，逐帧解析 `SPC1/DATA/WIFIFRAME/BLEFRAME/IQFRAME`；命令用"置命令→等完成事件"同步，避免与读线程抢串口。
- `App` 模式机：`start_mode/stop_mode`，未 STOP 切换被拒绝；SDR 内再选抓包模式。
- SDR 显示：`spec_shifted` 供频谱折线与瀑布共用；`_wf_lut` 由 Floor/Range 生成；瀑布按 `redraw_ms=50` 重绘，画不过来时只统计不画。
- 输出：`PcapStream` 支持同时写文件 + 命名管道（`\\.\pipe\esp_sdr_wifi|ble`）。

### 4.5 各模块接口设计

#### 4.5.1 串口命令（文本，`\n` 结尾）

| 命令 | 应答 | 说明 |
| --- | --- | --- |
| `SYNC <nonce>` | `SYNC <nonce>` | 失步重同步（GUI 握手） |
| `INFO` | `C5SDR 6 burst 16380` | 家族/协议版本/最大快照长度 |
| `CAPS` | `CAPS … MON WIFIMON BLEMON IQ8 …` | 能力位（C5 才含 MON/WIFIMON/BLEMON） |
| `FREQ <MHz>` | `OK` | 100–6000 |
| `BANDWIDTH <MHz>` | `OK` | 0=Open，11–48=Filter |
| `CAP16 <n> <rate>` | `DATA <n> <crc> <us>` + 载荷 | 8-bit I/Q |
| `CAP20 <n> <rate>` | 同上 | 打包 10-bit |
| `SPEC …` | `SPEC …` + `SPC1` 流 | 片上频谱 |
| `IQS ON <rate>` / `OFF` / `?` | `OK` / `OK` / `IQS <rate> <samples> <late> <events> <dropped>` | 连续 IQ |
| `IQDBG` | `IQDBG <si> <sq> <sc> txfail=<n> iqdrop=<n>` | 调试计数 |
| `MON WIFI ON <ch> [hop_ms]` / `OFF` | `OK` / `ERR …` | 混杂监听（5G 36–177） |
| `MON BLE ON [itvl] [win]` / `OFF` | `OK` / `ERR …` | 被动扫描 |
| `MON?` | `MON WIFI:a BLE:b IQ:c CH:n DROPS:w/b` | 状态与丢弃计数 |
| `OUTPUT USB\|UART\|AUTO` | `OK` | 输出路由 |
| `RELEASE` | `OK` | 释放租约 |

#### 4.5.2 二进制帧

```
WIFIFRAME <len> <rssi> <ch> <crc32>\n + <len> B   // Radiotap(15) + 802.11(含 FCS)，pcap linktype 127
BLEFRAME  <len> <rssi> <37> <crc32>\n + <len> B   // 完整广播 PDU，pcap linktype 256（需 on-air 伪头）
IQFRAME   <len> <rate> <crc32>\n      + <len> B   // int32 LE I/Q 对，128 对 = 1024 B
SPC1 …（28 B 头 + nbins 功率码 + CRC32）
```

#### 4.5.3 模块间 C 接口

```c
void mode_manager_init(void);
bool mode_manager_command(const char *line);
bool mode_manager_busy(void);
bool mon_frame_push(mon_type_t type, const void *data, uint16_t len, int8_t rssi, uint8_t channel);

bool burst_serial_send(const void *data, size_t size);
bool burst_serial_send_timeout(const void *data, size_t size, unsigned ms);

bool iq_stream_start(unsigned hz, char *err, size_t err_size);
void iq_stream_stop(void);
bool iq_stream_running(void);
void iq_stream_stats(char *buf, size_t size);

bool wifi_monitor_start(unsigned ch, unsigned hop_ms, char *err, size_t n);
void wifi_monitor_stop(void); unsigned wifi_monitor_channel(void);
bool ble_monitor_start(unsigned itvl, unsigned win, char *err, size_t n);
void ble_monitor_stop(void);
```

#### 4.5.4 GUI 内部接口

```python
Link(port, on_event, log, baudrate)     # on_event(kind, *args)
Link.command(cmd, timeout=5) -> str     # 同步命令
Link.close()
PcapStream(linktype).add_file(path|fobj); .add_pipe_sink(sink); .record(payload); .close()
App._handle_event(kind, *args)          # 'spec' | 'data' | 'iqframe' | 'iqcodes' | 'wififrame' | 'bleframe' | 'line' | 'link_error'
```

### 4.6 进程设计（任务/中断）

| 执行体 | 类型 | 优先级 | 栈 | 周期/触发 | 职责 |
| --- | --- | --- | --- | --- | --- |
| `app_main`（serve 循环） | 任务 | IDF main_task 默认 | 默认 | 事件驱动 | 读命令、模式操作、同步应答 |
| `mon_tx` | 任务 | 8 | 4096 | 队列触发 | 组帧→有界发送→归还池 |
| `iq_stream` | 任务 | 8 | 4096 | 4 ms | 排空 IQ 环→128 对/块上送 |
| `iq_pace`（esp_timer ISR） | 中断 | - | - | 50 µs | 读/重配 estimator、写环、统计 |
| Wi-Fi RX 回调 | 协议栈上下文 | - | - | 每帧 | 组 Radiotap→`mon_frame_push` |
| NimBLE host | 任务 | IDF 默认 | IDF 默认 | 事件驱动 | 广播回调→`mon_frame_push` |

时序（一次连续 IQ 采集）：

```
GUI: IQS ON 80000 ──► serve: iq_stream_start → iq_configure
                       └─ 创建 iq_stream 任务
      每 50 µs: ISR ──► ring(head++)
      每 4 ms:  iq_task ──► mon_frame_push(IQ, 128 对)
      mon_tx ──► "IQFRAME …\n" + 1024 B ──► USB ──► GUI Link 解析 ──► 事件队列
GUI: 每 50 ms 取最新帧 → 512 样本 FFT → 频谱/瀑布（Floor/Range 查表）
GUI: IQS OFF ──► serve: iq_stream_stop → OK；随后 IQS? 输出统计
```

### 4.7 数据管理 / 存储设计

| 数据 | 位置 | 生命周期 | 说明 |
| --- | --- | --- | --- |
| dump 缓冲（128 KiB SRAM bank） | 内部 SRAM 预留区 | 常驻 | 由 dump engine 独占；读取前必须确认引擎让出 |
| 监视帧池 8×1600 B | PSRAM | 常驻 | 池空即丢帧并计数 |
| IQ 环 4096 对 int32 | PSRAM | 采集期间 | 单生产者/单消费者无锁 |
| SPEC FFT 工作区 | PSRAM（`c2_spectrum`/EXT_RAM） | 常驻 | 避免内部 BSS 越界 |
| 配置 | NVS + `sdkconfig` | 常驻 | 仅 Wi-Fi 初始化参数；模式状态不持久化 |
| pcap | 主机文件 | 会话 | linktype 127 / 256，Wireshark 可直接打开 |
| IQ 原始样本 | 主机文件 | 会话 | 连续：int32 交错；突发：8-bit 交错（GUI 以追加方式写） |
| Flash 布局 | 0x2000 bootloader / 0x8000 分区表 / 0x10000 app | - | app 分区 4 MB，16 MB Flash 剩余可再扩 |

### 4.8 单元测试设计

| 层 | 桩/夹具 | 覆盖策略 |
| --- | --- | --- |
| 固件命令层 | `tests/fw_harness.py`（真实串口 + 事件解析） | 逐命令正常/越界/非法/忙态；二进制帧 CRC 与长度；长稳与背压 |
| 固件协议解析 | 同上（构造粘包/半包/超长行） | 失步后 `SYNC` 自恢复 |
| GUI 链路层 | `tests/gui_tests/mock_fw.py`（`FakeSerial`+`FirmwareModel`+`MockLink`） | 正常帧、截断帧、CRC 错、乱序、粘包/半包、超时、断连 |
| GUI 应用层 | `MockLink` 注入 `App` | 模式机、抓包模式、电平、pcap/管道、队列背压 |
| 端到端 | `tests/e2e_smoke.py`（真板 + 真 GUI 组件） | 三模式联调冒烟 |

### 4.9 功能测试接口设计（可测试性钩子）

| 钩子 | 用途 |
| --- | --- |
| `SYNC <nonce>` / `RELEASE` | 任意时刻重建会话、清理租约（自动化前置） |
| `CAPS` | 断言能力集合（WIFIMON/BLEMON/IQ8），判断固件版本 |
| `MON?` | 状态位 + `DROPS` 计数（背压可见） |
| `IQS?` | `rate/samples/late/events/dropped` 数值化断言 |
| `IQDBG` | 发送层 `txfail`、池丢弃 `iqdrop`（定位"是背压还是故障"） |
| `INFO` / `VERSION?` | 固件身份与构建时间 |
| GUI `MockLink` / `FakeSerial` | 注入任意异常返回，无需硬件 |
| 命名管道 | `tshark -i \\.\pipe\...` 直接验证实时抓包 |

---

## 5. 功能测试

### 5.1 环境

- 板卡：ESP32-C5-WROOM-1-N16R8，USB-Serial-JTAG = COM10（UART0 关闭，CH340/COM8 未接）。
- 主机：Windows + Python 3.13（venv）、pyserial、`D:\Wireshark\tshark.exe` 4.6.2。
- 固件：IDF v6.1 构建，app 0x10000，烧录命令见 `AGENTS.md`。

### 5.2 Mode × 操作覆盖矩阵（摘要）

| 操作 \ 模式 | SDR（SPEC） | SDR（IQ 连续） | SDR（IQ 突发） | Wi-Fi Monitor | BLE Monitor |
| --- | --- | --- | --- | --- | --- |
| 启动 / 停止 / 重复停止 | ✓ | ✓ | ✓ | ✓ | ✓ |
| 未 STOP 切换被拒 | ✓ | ✓ | ✓ | ✓ | ✓ |
| 参数边界/非法 | ✓ FFT/带宽 | ✓ 速率 | ✓ 样本数 | ✓ 信道/驻留 | ✓ 参数忽略 |
| 原始报文/样本回传 | `SPC1` | `IQFRAME` | `DATA` | `WIFIFRAME` | `BLEFRAME` |
| pcap / 管道 | -（频谱） | IQ 文件 | IQ 文件 | ✓ linktype 127 | ✓ linktype 256 |
| 默认静默 | ✓ | ✓ | ✓ | ✓ | ✓ |

### 5.3 关键实测结果

| 场景 | 结果 |
| --- | --- |
| 片上频谱 80 MS/s、256 bin | 约 430–440 帧/s；`F-17` 4393 帧 CRC 全对 |
| 连续 IQ 10k / 40k / 80k | 86% / 102% / 100% 速率达成；80k = 636 kB/s、625 帧/s |
| 连续 IQ 30 s（GUI 全流程） | 19008 帧、0 条失步事件、队列 ≤28、最长 UI 更新 28 ms |
| 突发 CAP16 4096 样本 | ≈20 帧/s，4096 bin、span 80 MHz |
| 5 GHz 固定信道 | ch36 帧带 radiotap freq=5180，设备表按频点显示 |
| Wi-Fi 全信道轮巡 | `CH:` 在 1–13 / UNII 表轮转，驻留 ≈110 ms |
| Wi-Fi 监听帧率（繁忙 2.4G） | ≈400 帧/s，13 个 BSS |
| BLE 被动扫描 | ≈40 帧/s，16 个 adv 地址，信道字段统一 37 |
| pcap 解析 | Wi-Fi：`wlan_radio.frequency/signal_dbm/fc` 20/20；BLE：`pdu_type/advertising_address` 20/20 |
| Wireshark 命名管道 | `tshark -i \\.\pipe\...` 实时解析 10/10 |
| 背压（主机停读 6 s 后发 `IQS OFF`） | 写在 0.00 s 返回、`OK` 正常回；`txfail/iqdrop` 如实计数 |

---

## 6. 单元测试报告

### 6.1 执行方式

```bash
# 固件命令桩（真板，COM10）
python tests/fw_harness.py func      # 20 项功能用例（另有 robust / pcap 组）
# GUI（mock 固件，无需硬件）
python tests/gui_tests/test_gui.py a b c   # 22 项（链路/应用/鲁棒）
# 端到端
python tests/e2e_smoke.py            # 9 项
```

### 6.2 固件命令桩（`tests/fw_harness.py`）

| 分组 | 用例 | 覆盖 |
| --- | --- | --- |
| func | F-01…F-20 | 握手/CAPS/模式启停/信道边界/快照各格式/SPEC/IQS/双口/静默 |
| robust | R-xx | 未知命令、超长行、粘包/半包、重复/乱序、长稳 60 s×2、丢帧语义 |
| pcap | P-xx | WiFi/BLE pcap 结构与 tshark 解析、管道实时性 |

**最新结果：19/20 PASS。** 唯一非 PASS 为 `F-19`（双口用例需要 COM8/UART；本构建已关闭 UART 且 CH340 未接入）→ 环境限制，非缺陷。顺带修正 3 处**测试期望漂移**（协议未改）：`MON?` 新增 `IQ:` 字段、本构建 `CAPS` 不含 `DUALSERIAL`、非法信道 99 应答为 `ERR channel`。

### 6.3 GUI mock（`tests/gui_tests/test_gui.py`）

| 分组 | 用例 | 说明 |
| --- | --- | --- |
| A（链路） | G-A01…A10 | 握手/INFO/CAPS、Wi-Fi/BLE 帧解析、SPEC 流、截断与重同步、超时 |
| B（应用） | G-B01…B12 | 模式机、改频重启、5G 命令序列、带宽下发、抓包模式与锁定、burst 显示回归 |
| C（鲁棒） | G-C01…C02 | 队列背压 5000 事件、管道无客户端 1 MB 写入 |

**最新结果：22/22 PASS。**

### 6.4 端到端

`tests/e2e_smoke.py`：真板 + 真 GUI 组件，三模式启停、帧接收、pcap 落盘共 9 项，**9/9 PASS**。

### 6.5 缺陷清单与回归（累计 13 项）

| ID | 级别 | 摘要 | 状态 |
| --- | --- | --- | --- |
| D1 | 高 | GUI `DATA` 载荷长度硬编码 `n*2`，CAP20/RXRUN 错位 | 已修（`data_wire_fn`） |
| D2 | 高 | 大载荷用 0.15 s idle 截止，>16 KB 传输残缺 | 已修（独立截止） |
| D3 | 中 | 填充超时残留半包，流失步 | 已修（清缓冲重同步） |
| D4 | 高 | BLE pcap 缺 Access Address/CRC | 已修（on-air 布局） |
| D5 | 中 | pyserial 非线程安全导致崩溃 | 已修（事件驱动 + 打开重试） |
| D6 | 低 | 探测后未 RELEASE，租约残留 | 已修 |
| D7 | 低 | `DROPS` 断言过严（寿命累计计数） | 已修（前缀匹配） |
| D8 | 高 | 突发谱线走 `iqframe` 分支被丢弃 → 突发视图永久空白 | 已修（独立 `iqcodes` 事件）+ G-B12 回归 |
| D9 | 高 | 80 kS/s 下 USB 发送忙等饿死命令循环，整机"假死" | 已修（200 µs 忙等 + 让出；有界发送） |
| D10 | 中 | 突发样本数被 `DISABLED` Spinbox 静默固定为 256 | 已修（`StringVar` 默认 4096） |
| D11 | 中 | 瀑布行截断到 512 bin，与频谱不一致 | 已修（整行存储） |
| D12 | 高 | GUI 每帧 FFT + 全像素重绘饿死读线程 → 串口溢出失步（"卡住"） | 已修（按 tick 取帧、512 样本 FFT、C 级渲染、50 ms） |
| D13 | 中 | 瀑布缺功率色标/电平标定 | 已修（常驻色标 + Floor/Range + 自动电平 + 折叠"显示"面板） |

### 6.6 未覆盖项与原因

| 未覆盖 | 原因 | 替代证据 |
| --- | --- | --- |
| USB 物理拔出瞬间板端行为 | 拔出即失去调试与供电通道 | 宿主侧断开/重连用例 |
| 固件内存/句柄泄漏定量 | 协议未暴露堆查询 | 60 s×2 长稳 + 40 轮模式切换无退化 |
| 磁盘满 | 构造真实满盘风险高 | pcap 打开失败路径已有异常处理 |
| 5 GHz 外部信号源标定 | 无信号源 | 调谐与帧上 radiotap 频点已验证 |

---

## 附录 A 构建与烧录

```bash
cmd //c "D:\esp_32_project\esp_sdr\build_esp32c5.bat"     # 构建（IDF v6.1）
python -m esptool --chip esp32c5 -p COM10 -b 460800 \
  --before default-reset --after hard-reset write-flash \
  --flash-mode dio --flash-freq 80m --flash-size 16MB \
  0x2000 build/bootloader/bootloader.bin \
  0x8000 build/partition_table/partition-table.bin \
  0x10000 build/esp_sdr.bin
```

> 修改 `sdkconfig.defaults.esp32c5` 后必须先删 `sdkconfig` 再构建，否则旧值静默生效。

## 附录 B 相关文档

- 原理与三模式说明：`esp_sdr_三模式监控.md`
- 固件协议：`espsdr/esp-sdr/docs/protocol.md`、`docs/spectrum.md`、`docs/how-it-works.md`
- GUI 使用：`monitor_gui/README.md`
- 自测明细（含逐条前后对照）：`tests/自测报告.md`

## 附录 C 离线分析工具（tools/iq_analyze.py）

把采集到的原始 I/Q 画成三联图：时域（I/Q 包络）→ 功率谱（Welch，dBFS vs 频偏）→
时频热力图（STFT，dBFS 色标在上方）。

```bash
pip install numpy matplotlib              # 一次性依赖

# 1) 分析 GUI“保存 IQ 原始样本”得到的文件（连续 = int32，突发 = int8）
python tools/iq_analyze.py capture.iq --format int32 --rate 80000 --center 2412
python tools/iq_analyze.py burst.iq   --format int8  --rate 80e6  --center 2412 --span 20

# 2) 直接从板子抓一段再分析（连续 IQS），同时存原始流
python tools/iq_analyze.py --live COM10 --live-rate 40000 --live-seconds 2 \
        --save cap40k.iq --out cap40k.png

# 3) 只看某一段 / 去掉直流 / 指定 0 dBFS 参考
python tools/iq_analyze.py cap.iq --format int32 --rate 80000 \
        --start 0.5 --length 1.0 --remove-dc --full-scale 32768
```

要点：

- `--format` 与 GUI 保存的文件一致：连续 IQ（`IQFRAME`，int32 交错）→ `int32`；
  突发快照（`CAP16`，int8 交错）→ `int8`；另有 `int16` 供其它 esp-sdr 目标。
- 0 dBFS 参考：`int8` 固定为 127（真实 8 bit ADC）；`int32` 连续流没有公开满量程，
  默认取该文件的 `|x|` 的 99 分位（可用 `--full-scale` 覆盖），因此谱峰接近 0 dBFS。
- 突发快照是多次 `CAP16` 拼接而成，热力图的时间轴是"合成时间"（快照间有 RF 间隙），
  连续 IQ 的时间轴才是真实时间。
- 输出为 PNG（默认 `<文件名>.png`），加 `--show` 可交互查看；脚本会打印峰值频点与电平。
