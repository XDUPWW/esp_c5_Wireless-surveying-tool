# ESP-SDR 调试 GUI 使用说明（四模式）

适用程序：`monitor_gui/dist/esp_sdr_gui.exe`（或源码 `python monitor_gui/esp_sdr_gui.py`）
适用硬件：ESP32-C5（本仓库固件，USB-Serial-JTAG 单口通信，UART0 已关闭）

---

## 0. 界面总览

```
┌ 串口 ─────────────────────────────────────────────────────────────┐
│ 设备端口: [COM10 USB-Serial-JTAG]  [刷新/自动识别] [连接]          │
└───────────────────────────────────────────────────────────────────┘
┌ 模式（切换前必须先点 STOP） ───────────────────────────────────────┐
│ [SDR 频谱] [WiFi Monitor] [BLE Monitor] [SDR# 桥接]  [■ STOP]      │
└───────────────────────────────────────────────────────────────────┘
┌ 标签页： SDR │ WiFi Monitor │ BLE Monitor │ SDR# 桥接 ─────────────┐
│ （各模式的面板，见下文）                                           │
└───────────────────────────────────────────────────────────────────┘
┌ 日志 ─────────────────────────────────────────────────────────────┐
│ 时间戳 + 每条命令/事件；排错先看这里                               │
└───────────────────────────────────────────────────────────────────┘
```

**三条铁律**

1. 先 `刷新/自动识别` → 选 `COM10 (USB-Serial-JTAG)` → 点 `连接`（握手 `SYNC`，日志出现 `INFO: C5SDR 6 burst 16380`）。
2. **任何模式切换前必须先点 `■ STOP`**，否则会提示“请先点击 STOP 再切换模式！”。
3. 串口同一时刻只能有一个消费者：GUI 与 `esp_sdr_bridge.exe`、MATLAB/其它工具不能同时占用 COM10。
   桥接模式停止后（或 GUI 断开）串口才空闲。

---

## 1. SDR 模式（芯片级 SDR / 频谱）

### 1.1 功能

| 控件 | 作用 |
| --- | --- |
| 频率 MHz (100-6000) | 调谐；点 `设置频率` 下发 `FREQ <MHz>`（整 MHz）。抓包运行中会自动 停止→改频→按原方式重启 |
| 采样率 | 80/40/20/10/8/4 MS/s（快照/片上 FFT 用），对应固件 `rate` 码 |
| FFT | 片上频谱点数 256 / 512 / 1024 / 2048 |
| 模拟带宽 | `Open (全带宽)` 或 `Filter (设定 MHz)`（11–48）→ `BANDWIDTH 0` / `BANDWIDTH <MHz>` |
| 抓包模式 | **片上频谱 (On-chip FFT)** 与 **IQ 传回电脑 (I/Q Streaming)** 二选一 |
| IQ 方式（仅 IQ 模式） | `连续 (无间隙)` → `IQS ON <rate>`（10k/20k/25k/40k/50k/80k）；`突发 (快照连拍)` → 循环 `CAP16 <n> <rate>` |
| 连续速率 S/s | 连续 IQ 的固件速率（须整除 240 MHz 与 80 MHz） |
| 突发样本数 | 单次快照样本数 256–16380（默认 4096） |
| 开始抓包 / ■ 停止抓包 | 按当前选择启动/停止；**运行中所有选择器锁定** |
| 保存 IQ 原始样本到文件 | 仅 IQ 模式可用；连续＝int32 交错，突发＝int8 交错 |
| ▶ 显示 / ▼ 显示 | 展开/收起“瀑布电平”面板（Floor / Range / 自动电平） |
| 瀑布右侧色标 | 功率码 `code = 20·log10|X| dB`（相对值 0–255）的颜色映射 |

### 1.2 示例 A：看 2.4 GHz 频谱/瀑布

1. `连接` → 点 `SDR 频谱`
2. 频率填 `2412`，采样率 `80 MS/s`，FFT `256`，模拟带宽 `Open (全带宽)`
3. 抓包模式选 `片上频谱 (On-chip FFT)` → 点 `开始抓包`
4. 日志出现 `SPEC: SPEC 256 80000000 256 2412`；上方频谱、下方瀑布开始刷新（约 430 帧/s）
5. 想调色阶：`▶ 显示` → 点 `自动电平`，或拖 Floor / Range；标题会显示 `色阶 x → y dB（相对）`

### 1.3 示例 B：把 IQ 录成文件（后续可离线分析）

1. 抓包模式选 `IQ 传回电脑 (I/Q Streaming)`，IQ 方式选 `连续 (无间隙)`，速率 `40000`
2. 勾 `保存 IQ 原始样本到文件`，路径填 `D:\cap40k.iq`
3. `开始抓包`（日志：`连续 IQ 流启动 (40000 S/s, 无间隙...)`），运行几秒后 `■ 停止抓包`
4. 离线分析：
   ```bash
   python tools/iq_analyze.py D:\cap40k.iq --format int32 --rate 40000 --center 2412
   ```
   会生成三联图（时域 / 功率谱 / 时频热力图）与 `cap40k.png`

### 1.4 示例 C：突发快照（宽带宽瞬间）

- 抓包模式 `IQ 传回电脑`，IQ 方式 `突发 (快照连拍)`，突发样本数 `4096`，采样率 `80 MS/s`
- `开始抓包` → 约 20 帧/s，每帧 4096 点（19.5 kHz/bin），快照之间有 RF 间隙（固件 burst 特性）

### 1.5 常见提示

| 现象 | 说明 |
| --- | --- |
| `ERR mode_busy` | 有其它模式（WiFi/BLE/IQS）没停：先 `■ STOP` |
| 连续 IQ 帧率不达标 | 80 kS/s 理论 625 帧/s；USB 带宽约 640 kB/s，主机读得慢时固件丢帧（`IQS?` 的 dropped 会涨） |
| 频谱很平/全黑 | 频点没信号，或 Floor/Range 不合适 → 点 `自动电平` |

---

## 2. WiFi Monitor 模式（802.11 混杂监听）

### 2.1 功能

| 控件 | 作用 |
| --- | --- |
| 全信道轮巡 | 勾选后按频段逐信道轮巡；不勾则固定在 `固定信道` |
| 固定信道 | 2.4 GHz 1–13 / 5 GHz 36–40-44-48-52…165（UNII） |
| 全信道频段 | `2.4 GHz (1-13)` 或 `5 GHz (UNII)` |
| 驻留 ms | 轮巡时每信道停留时间，默认 **110 ms** |
| 写 pcap | 默认勾选；输出 linktype 127（Radiotap + 完整 802.11 帧） |
| Wireshark 实时管道 | 勾选后建命名管道，`wireshark -k -i \\.\pipe\esp_sdr_wifi` 实时看 |
| 设备表 | MAC(BSSID) / 最强 RSSI / 帧数 / 信道 / 最近出现 |

固件侧等价命令：`MON WIFI ON <ch> [hop_ms]`、`MON WIFI OFF`。

### 2.2 示例：抓 5 GHz 的 ch36，边抓边看 Wireshark

1. `连接` → `WiFi Monitor` 标签页
2. 取消 `全信道轮巡`，`固定信道` 选 `36`
3. 勾 `写 pcap`（路径默认 `wifi_monitor.pcap`），勾 `Wireshark 实时管道`
4. 点 `WiFi Monitor` 按钮启动（日志：`MON WIFI ON 36` → `OK`）
5. 接 Wireshark，两种方式任选：
   - **命令行（最省事）**：`"D:\Wireshark\Wireshark.exe" -k -i \\.\pipe\esp_sdr_wifi`
     （或直接跑 `examples/wireshark_pipe.bat wifi`）
   - **图形界面**：Wireshark → 捕获 → 选项 → **管道** 标签 → 点左下 **+** → 填 `esp_sdr_wifi`
     （或完整路径 `\\.\pipe\esp_sdr_wifi`）→ 确定 → 回到接口列表选 “Named pipe: esp_sdr_wifi” → 开始。
     注意：这个版本的 Wireshark **不保存**管道设置，每次都要重新加一行；而且必须先让 GUI 把管道建出来
     （日志出现“等待 Wireshark 连接 \\.\pipe\esp_sdr_wifi ...”）再去连，顺序反了会提示管道不存在。
6. 停止：点 `■ STOP`（日志 `MON WIFI OFF`）

### 2.3 示例：2.4 GHz 全信道巡检

- 勾 `全信道轮巡`，频段 `2.4 GHz (1-13)`，驻留 `110` → 启动
- `MON?` 的 `CH:` 会随轮巡变化；设备表按信道汇总（约 400 帧/s，繁忙环境下 10+ 个 BSS）

### 2.4 pcap 校验

```bash
"D:\Wireshark\tshark.exe" -r wifi_monitor.pcap -c 5 \
    -T fields -e wlan.fc.type_subtype -e wlan_radio.frequency -e wlan_radio.signal_dbm
```
应能看到 802.11 帧类型、真实频点（如 5180）与信号强度。

---

## 3. BLE Monitor 模式（被动广播扫描）

### 3.1 功能与硬件限制

- 纯**被动扫描**（不发 SCAN_REQ / 不建立连接），只听 BLE 广播信道 **37/38/39**。
- ESP32-C5 的 BLE 控制器**固定同时扫三个广播信道**：GUI **不提供**固定信道、也**不提供**驻留时间。
- 抓包里的信道字段**统一标记为 37**（硬件限制，Wireshark 里会这样显示）。
- 回传帧 = **完整广播 PDU**（2 B 头 + AdvA + AD 数据），pcap linktype 256。
- 设备表：Adva（MAC）/ RSSI / 帧数 / AD 数据（hex）。

固件侧等价命令：`MON BLE ON` / `MON BLE OFF`。

### 3.2 示例

1. `连接` → `BLE Monitor` 标签页（页面顶部就是硬件限制说明）
2. 勾 `写 pcap`（默认 `ble_monitor.pcap`），可选 `Wireshark 实时管道`
3. 点 `BLE Monitor` 启动 → 设备表出现周边广播设备（实验室环境约 40 帧/s）
4. 校验：
   ```bash
   "D:\Wireshark\tshark.exe" -r ble_monitor.pcap -c 5 \
       -T fields -e btle.advertising_header.pdu_type -e btle.advertising_address
   ```
5. 停止：`■ STOP`

---

## 4. SDR# 桥接 模式（把 C5 变成一台 PlutoSDR）

> **这一模式就是 libiio 的入口**：启动后本进程在 `127.0.0.1:30431` 提供标准 iiod 服务，
> SDR# 的 PlutoSDR 插件、Python `pylibiio`、MATLAB 自带的 `iio_info/iio_attr/iio_readdev`
> 都连同一个地址；**不需要再单独运行 `bridge/esp_sdr_bridge.exe`**（两者不要同时开）。

### 4.1 功能

| 控件 | 作用 |
| --- | --- |
| IQS 速率 | 固件侧速率 10k/20k/25k/40k/50k/80k（SDR#/libiio 请求其它速率时主机侧线性插值） |
| 监听 | 显示 `127.0.0.1:30431` |
| 状态 | 客户端数 / 已收帧数与字节 / 缓冲深度 / 输出速率 |

属性 → 固件命令映射：

| libiio 写入 | 固件动作 |
| --- | --- |
| `altvoltage0.frequency`（Hz） | `IQS OFF` → `FREQ <MHz 四舍五入>` → `IQS ON`（C5 只有整 MHz） |
| `voltage0.sampling_frequency` | 就近取档 `IQS ON <rate>`，主机侧插值到请求速率 |
| `voltage0.hardwaregain` / `gain_control_mode` | `GAIN MANUAL 0..80` / `GAIN HARDWARE` |
| `voltage0.rf_bandwidth` | `BANDWIDTH <MHz>`（0=Open，11–48） |

### 4.2 示例 A：SDR# 出频谱

1. `连接` → `SDR# 桥接` 按钮（日志：`SDR# 桥接已启动：IQS 80000 S/s，监听 127.0.0.1:30431`）
2. 打开 `SDR#/sdrsharp-x86-noskin/SDRSharp.exe`（配置里已预置 `iqSource=12`＝PlutoSDR、`PlutoSDRURI=ip:127.0.0.1`）
3. SDR# 里源应为 **PlutoSDR**；点工具栏 **▶**
4. GUI 日志出现 `桥接：SDR# 连入` / `OPEN cf-ad9361-lpc`；状态栏帧数上涨；SDR# 出频谱
5. 在 SDR# 改中心频率 → 日志 `SDR# 调谐 2400.000 MHz -> FREQ 2400 MHz (Δ=...)`
6. 用完点 `■ STOP`（会 `IQS OFF` 并释放串口）

### 4.3 示例 B：Python libiio 读 I/Q（同一桥接）

```bash
pip install "pylibiio==0.25"        # 纯 ctypes 绑定
# 需要 64 位 libiio.dll（可用 MATLAB 支持包里那份，脚本会自动加进 PATH）
python tools/pluto_libiio_demo.py --samples 4096 --out cap.iq
```
预期：`tune -> 2412000000 Hz, rate -> 250000 Hz, gain -> 40.000000` →
`read 16384 bytes (4096 I/Q 对)` → 峰值统计（`--fake` 下应为 `+10.0 kHz`）。

### 4.4 示例 C：MATLAB

- **可用**：MATLAB 自带 libiio CLI（在 `C:\ProgramData\MATLAB\SupportPackages\R2026a\3P.instrset\libiio.instrset\win64\Windows-MinGW-W64\`）
  ```bat
  iio_info.exe -u ip:127.0.0.1
  iio_readdev.exe -u ip:127.0.0.1 -b 16384 -s 32768 cf-ad9361-lpc > cap.bin
  ```
- **不可用**：`sdrrx('Pluto','RadioID','ip:127.0.0.1')` —— MathWorks 驱动在自身初始化阶段就中止
  （报 `Failed to write phase inversion attribute`），临时请改用文件方式：
  用 GUI 的“保存 IQ 原始样本”录 `.iq`，MATLAB `fread(fid,[2 inf],'int32')` 直接读。

### 4.5 注意

- 桥接期间 GUI 独占串口；**`■ STOP` 后**才能切回 SDR/WiFi/BLE 或让别的工具连 COM10。
- C5 调谐步进是 **1 MHz**，SDR# 里拖动小于 500 kHz 可能不生效（日志会打印取整误差）。
- 真实带宽 ≤ 80 kHz；SDR# 显示的 250 kS/s 宽度里只有中心 ±40 kHz 是真数据，两侧是插值。
- 幅度**未校准**（相对值），不要当功率计用。
- 客户端支持：SDR# ✅、Python `pylibiio` ✅、MATLAB 自带 `iio_info/iio_attr/iio_readdev` ✅、
  MATLAB `sdrrx('Pluto')` ❌（见 §4.4）。矩阵与细节见 `bridge/README.md` §10。

---

## 5. 故障排查

| 日志/现象 | 原因与处理 |
| --- | --- |
| `握手失败: timeout waiting reply for 'SYNC 4711'` | 端口被占用（另一 GUI/桥接/串口工具）或板子没跑本仓库固件 |
| `ERR busy` | 另一个客户端持有租约：停掉另一处，或等 5 s |
| `串口已被其它模式占用`（桥接启动时） | 先 `■ STOP` 当前模式；若是上次异常退出留下的 IQS 流，桥接会自动先 `IQS OFF` |
| `端口 30431 被占用` | 已有桥接在跑（`esp_sdr_bridge.exe` 或另一 GUI）：只能开一个 |
| WiFi 模式设备表空 | 信道没信号 / 5G 频段选错；先试 2.4 GHz 全信道 |
| SDR# 连上但没数据 | 确认 SDR# 点了 ▶；看 GUI 状态栏“帧数”是否增长 |
| 关闭 SDR# 后串口没释放 | 桥接是“模式”，要回 GUI 点 `■ STOP` |

## 6. 相关文档

- 三模式设计/原理：`esp_sdr_三模式监控.md`
- 完整设计文档：`ESP_SDR_设计文档.md`
- 桥接细节与客户端支持矩阵：`bridge/README.md`、`bridge/差异清单.md`
- 离线分析脚本：`tools/iq_analyze.py`（见 `ESP_SDR_设计文档.md` 附录 C）
- 自测报告：`tests/自测报告.md`
