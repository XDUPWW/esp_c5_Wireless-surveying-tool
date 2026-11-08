# ESP-SDR 调试工具 (esp_sdr_gui.py)

ESP32-C5 三模式固件（SDR / WiFi Monitor / BLE Monitor）的 Python + tkinter 图形调试工具。
仅依赖标准库 + pyserial（无需 numpy/matplotlib；SDR 频谱使用固件片上 FFT 的 SPEC 流）。

## 运行

```bat
:: 任一 Python 3.9+（需带 tkinter 与 pyserial）
pip install pyserial
python esp_sdr_gui.py
```

## 功能

1. **串口识别**：点击“刷新/自动识别”自动探测 ESP32-C5 的 USB-Serial-JTAG 口
   （VID 0x303A）与 UART 转接口（CP210x/CH340/FTDI），用 `SYNC/TRANSPORT?` 握手确认
   后标注类型；下拉框里也列出全部串口，可手动选择。
   - 只有一个“设备端口”（USB-Serial-JTAG），命令与数据共用；固件已关闭 UART 输出。

2. **模式切换**：SDR / WiFi Monitor / BLE Monitor / **SDR# 桥接** 四个按钮，**任何时刻
   必须点 ■ STOP 后才能切换**（未 STOP 时切换会被拒绝并提示）。

3. **SDR 抓包**：频率（100–6000 MHz，含 5 GHz）、采样率（80/40/20/10/8/4 MS/s）、
   模拟带宽（Open 全带宽 / Filter 11–48 MHz）可调。SDR 页的“抓包模式”二选一，
   **同一时刻只有一种子在跑，切换必须先点“■ 停止抓包”**（运行期间选择器被锁定）：
   - **片上频谱 (On-chip FFT)**：`SPEC 0 1 1 0 <rate> <bins>` 拉取固件片上 FFT 流，
     FFT 点数 256–2048 可调；上方实时频谱（带 Start/Center/Stop 频率刻度）、下方
     满幅瀑布图；改频率自动重启流。
   - **瀑布功率标定**（三种抓包模式共用）：右侧常驻色标（Floor+Range 刻度，单位是
     固件功率码 `code = 20·log10|X| dB` 的相对值 0–255）；“瀑布电平”两个滑条
     Floor / Range 把色阶映射到感兴趣的区间，“自动电平”按当前数据 2%/99% 分位
     自动设好。频谱折线与瀑布共用同一映射，两者高度与颜色一一对应。
   - **IQ 传回电脑 (I/Q Streaming)**：原始 I/Q 样本回传，本地纯 Python FFT 出图
     （espargos 式视图），子方式两选一：
     - **连续 (无间隙)**：`IQS ON <rate>`，固件内部连续采样（10k–80k S/s，
       须同时整除 240 MHz 与 80 MHz），`IQFRAME` 每块 128 对 int32 I/Q；
       频谱按最近 **512 样本** 做 FFT（80 kS/s 下 156 Hz/bin），显示按 50 ms
       节流（画不上的帧只统计不画，避免把串口读线程喂爆）；
     - **突发 (快照连拍)**：循环 `CAP16 <n> <rate>`，单次快照往返约 50–100 ms，
       快照间有 RF 间隙（固件 burst 特性），样本数 256–16380 可调（默认 4096
       → 4096 bin / 19.5 kHz 每格）。
     两种子方式都可勾选“保存 IQ 原始样本到文件”，统计实时帧率/字节率。

4. **WiFi Monitor**：
   - 固定信道：2.4 GHz 1–13 或 **5 GHz UNII 36–165**（下拉选择）。
   - 全信道轮巡：可选 2.4 GHz (1–13) 或 5 GHz (UNII 列表) 频段，驻留时间默认
     **110 ms** 可配。
   - 设备表（BSSID、最强 RSSI、帧数、信道、最近出现），统计计数；
     Radiotap 信道字段对 5G 显示真实信道号与频率（如 36 → 5180 MHz）。
5. **BLE Monitor**（被动扫描，仅监听 37/38/39 广播信道）：
   - 按硬件限制**不提供**固定信道/驻留配置，页面注明信道字段统一标记 37。
   - 设备表（AdvA、RSSI、帧数、AD 数据）。

6. **pcap 与 Wireshark 实时管道**（两种监控模式均支持，可多选）：
   - 默认勾选“写 pcap”：WiFi → `wifi_monitor.pcap`（linktype 127，Radiotap+完整
     802.11 帧）；BLE → `ble_monitor.pcap`（linktype 256，含 10 字节伪头 +
     完整 BLE PDU）。直接用 Wireshark 打开。
   - 勾选「Wireshark 实时管道」：创建 Windows 命名管道并在日志里提示
     `等待 Wireshark 连接 \\.\pipe\esp_sdr_wifi ...`（**先启动模式，再连 Wireshark**）。
     - 命令行：`wireshark -k -i \\.\pipe\esp_sdr_wifi`（BLE 用 `\\.\pipe\esp_sdr_ble`），
       或直接跑 `examples\wireshark_pipe.bat wifi|ble`。
     - 图形界面：Wireshark → 捕获 → 选项 → **管道** 标签 → 点 **+** → 填 `esp_sdr_wifi`
       （或完整路径 `\\.\pipe\esp_sdr_wifi`）→ 确定 → 选 `Named pipe: esp_sdr_wifi` → 开始；
       该版本不保存管道设置，每次都要重新加。
     - 管道同时只接受 1 个客户端：换窗口前先关旧的（服务端会自动重建管道）。

## SDR# 桥接模式（第 4 个模式，进程内）

点「SDR# 桥接」后，本进程会把 C5 伪装成一台 **PlutoSDR**（libiio/iiod，TCP 30431），
SDR# 用自带的 **PlutoSDR** 源连 `ip:127.0.0.1` 即可，**不需要再单独跑 bridge 进程**。

- 启动时先发 `MON?` 校验（别的模式在跑会拒绝），再 `IQS ON <速率>`（页面上可选 10k–80k）；
- SDR# 写属性时映射到固件命令：调谐 → `IQS OFF → FREQ → IQS ON`、采样率 →
  `IQS ON`（就近取档 + 主机插值）、增益 → `GAIN MANUAL/GAIN HARDWARE`、带宽 → `BANDWIDTH`；
- 状态栏实时显示：客户端数、已收帧数/字节、缓冲深度、输出速率；
- 点 ■ STOP 会关掉监听并 `IQS OFF`，串口租约释放，可以再切回 SDR/WiFi/BLE；
- 插件要求、单位换算、幅度未校准等细节与 `bridge/README.md` 相同；不想开 GUI 时仍可用
  `bridge/dist/esp_sdr_bridge.exe`（同一实现，便于无界面/脚本场景）。

## 打包成 exe（Windows）

```bat
monitor_gui\build_exe.bat
```

使用 PyInstaller 生成单文件无窗口 exe：`monitor_gui\dist\esp_sdr_gui.exe`
（约 11 MB，内置 Python/tkinter/pyserial，目标机器无需安装 Python）。
已在本机验证可启动（任务列表可见进程、无即崩溃）。如杀毒软件误报，
加白名单或改用 `--onedir` 目录分发（去掉 `--onefile`）。

## 已知注意事项

- Windows 主机侧串口接收缓冲已自动设为 64 KB（usbser 默认 4 KB 会截断大载荷）。
- USB-Serial-JTAG 口被本工具打开时若板子复位属正常现象（DTR/RTS 复位电路），
  工具会重新握手。
- 两个监控模式可同时启动（固件内 coexistence 分时），但 GUI 为清晰起见仍要求
  STOP 后切换。
