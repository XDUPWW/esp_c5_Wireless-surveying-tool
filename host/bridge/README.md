# bridge/ — ESP32-C5 → SDR# 的 PlutoSDR（libiio/iiod）桥接

> 同一套实现已经内置进 `monitor_gui/dist/esp_sdr_gui.exe` 的「SDR# 桥接」模式
> （进程内启动，不用额外开进程）。本目录的独立 exe 保留给无界面/脚本场景，
> 两者不要同时运行（会抢 TCP 30431 和串口）。

把 ESP32-C5 的连续 I/Q（固件 `IQS` 流）包装成一台"假的 ADALM-PLUTO"，
让 SDR# 用自带的 **PlutoSDR** 插件直接接入，不需要 rtl_tcp、也不需要装驱动。

```
ESP32-C5 (COM10)  ──IQFRAME(int32 LE)──►  pluto_iiod.py  ──iiod(TCP 30431)──►  SDR#
                   IQS ON 80 kS/s          属性→FREQ/IQS/BANDWIDTH/GAIN    PlutoSDR 插件
```

> 本次范围按确认结果：**只做 PlutoSDR(libiio) 路线**，不做 rtl_tcp / SpyServer。
> 取舍与依据见 `差异清单.md`。

## 1. 依赖

- Python 3.9+；`pyserial`、`numpy`（与 `monitor_gui` / `tools/iq_analyze.py` 同一套环境）
- SDR#：`SDR#/sdrsharp-x86-noskin/SDRSharp.exe`（自带 `SDRSharp.PlutoSDR.dll`、`libiio.dll`）

```bash
pip install pyserial numpy      # 只有跑源码才需要；用 dist/esp_sdr_bridge.exe 无需安装
```

仓库里的可执行文件：

| 文件 | 作用 | 日常是否需要 |
| --- | --- | --- |
| `monitor_gui/dist/esp_sdr_gui.exe` | 三模式调试 GUI（SDR/WiFi/BLE） | 需要 |
| `bridge/dist/esp_sdr_bridge.exe` | **SDR# 用的 Pluto 桥接**（本目录） | 需要（和 GUI 二选一占用串口） |
| `bridge/dev/Probe.exe` | libiio 协议自测探针（C#，链接 SDR# 自带 dll） | 不用，仅排障/验收 |

## 2. 启动桥接

两种方式，效果完全一样：

```bat
:: A. 打包好的 exe（和 monitor_gui/dist/esp_sdr_gui.exe 一样的三方依赖都为 0）
bridge\dist\esp_sdr_bridge.exe                 :: 默认 COM10 / 2412 MHz / 80 kS/s
bridge\dist\esp_sdr_bridge.exe --freq 5180 --rate 40000
bridge\dist\esp_sdr_bridge.exe --fake          :: 无硬件：+40 kHz 单音 + 噪声
```

```bash
# B. 直接跑源码（改代码时用）
python bridge/pluto_iiod.py

# 常用参数
python bridge/pluto_iiod.py --port COM10 --freq 5180 --rate 40000
python bridge/pluto_iiod.py --fake          # 无硬件：+40 kHz 单音 + 噪声
python bridge/pluto_iiod.py --listen 0.0.0.0:30431      # 局域网给别的机器用
```

启动成功的标志（日志最后两行）：

```
[..] iiod 监听 127.0.0.1:30431（SDR# 里用 PlutoSDR 源，URI = ip:127.0.0.1）
[..] 就绪：SDR# → Source = PlutoSDR, Device-URI = ip:127.0.0.1:30431
```

## 3. SDR# 配置步骤

1. 打开 `SDR#/sdrsharp-x86-noskin/SDRSharp.exe`（本仓库已预置好下面两项，
   见 `SDRSharp.exe.Config` 的 `iqSource` / `PlutoSDRURI`；原配置备份在
   `SDRSharp.exe.Config.bak`）：
   - 源下拉框选 **PlutoSDR**（配置文件里对应 `iqSource = 12`）
   - 源设置（齿轮图标 → PlutoSDR Properties）里 **Device-URI** 填 `ip:127.0.0.1`
     端口固定 30431，不用写
2. 点工具栏 **▶** 开始。SDR# 会连上来，日志出现：

   ```
   [..] SDR# 客户端连入 ('127.0.0.1', xxxxx)
   [..] OPEN cf-ad9361-lpc 4096
   ```

3. 在 SDR# 里改中心频率 / 采样率 / RF 增益 / 带宽，桥接日志会同步出现
   `tune ... -> FREQ ...`、`firmware IQS ON ...`、`gain -> GAIN MANUAL ...`、
   `bandwidth -> BANDWIDTH ...`。
4. 关掉 SDR#（或点 ■）后 1 s 内：`no client: IQS OFF + RELEASE, serial port released`，
   串口被释放，`monitor_gui` 可立即接管。

## 4. 命令与单位换算

| SDR# 写入（libiio 属性） | 桥接动作 | 说明 |
| --- | --- | --- |
| `altvoltage0.frequency`（Hz） | `FREQ <MHz>`（四舍五入） | **C5 只有整 MHz**（`RANGE 100 6000 1`，无 FOFS），Δ≤±0.5 MHz 会打印在日志里 |
| `voltage0.sampling_frequency`（Hz） | `IQS ON <最近可用档>` | 固件只支持 10000/20000/25000/40000/50000/80000；其它值回退并记日志，主机侧再线性插值到 SDR# 请求的速率 |
| `voltage0.rf_bandwidth`（Hz） | `BANDWIDTH <MHz>` | 0=Open（最宽），11–48=模拟滤波器；越界钳到 11–48 |
| `voltage0.hardwaregain`（dB 字符串） | `GAIN MANUAL <0..80>` | C5 增益是 PHY 表项、**不是 dB**：`idx = round(dB/100*80)` |
| `voltage0.gain_control_mode`（`manual`/`slow_attack`…） | `GAIN MANUAL 40` / `GAIN HARDWARE` | 非 manual 一律走硬件 AGC |
| `filter_fir_en`、`ensm_mode`、`calib_mode` 等 | 接受并返回固定值 | SDR# 用来确认设备形态，不映射到硬件 |

改频率/带宽期间会短暂停流（固件在 IQS 流式期间拒绝 `FREQ`）：
`IQS OFF → FREQ → IQS ON`，日志有记录，SDR# 上表现为一次很短的断点。

## 5. 采样率、带宽与真实带宽

- 固件侧速率上限 **80 kS/s**（USB-Serial-JTAG 实测约 640 kB/s）。
- SDR# 的采样率下拉最低是 0.25 MSPS，因此桥接做**主机侧线性插值**：
  串口侧仍按 80 kS/s 采集，输出按 SDR# 请求的速率（如 250 kS/s）给样本。
  **真实信息只占频谱中心 ±40 kHz**，两侧是插值出来的，属正常现象，不是信号。
- 频谱显示的宽度 = SDR# 请求的速率；要"看得见全带宽"，只能把 SDR# 采样率调到你
  实际需要的值（≥ 80 kS/s 都能对上）。

## 6. 幅度标定声明（重要）

- C5 的 `IQFRAME` 载荷是基带 boxcar 累加值（相对 ADC 码值缩放 2^-6），
  **没有公开的满量程定义**。
- 桥接默认 `--full-scale auto`：取启动后第一块 `|x|` 的 99 分位 ×4 作为参考，
  再把该参考映射到 ±0.6 FS（留 4 dB 余量），并做逐块去直流。
- 因此：**输出幅度未校准**，只能用于相对观察（频谱形状、SNR、调制识别），
  不能当功率计用。需要绝对值时用 `--full-scale <数值>` 手工指定，但该数值
  仍需自行用信号源标定。

## 7. 与 monitor_gui 的关系（串口租约）

- 桥接和 `monitor_gui` 是**并列的串口消费者**，同一时刻只能有一个打开 COM10。
- 桥接在**没有 SDR# 客户端**时会主动 `IQS OFF` + `RELEASE` 并**关闭串口**，
  此时 `monitor_gui` 可以直接连接。
- 若串口已被 `monitor_gui`（或别的 SDR/WiFi/BLE 模式）占用，桥接启动即报错：

  ```
  RuntimeError: serial port busy with another mode: 'MON WIFI:off BLE:off IQ:on ...'
  ```

  先停掉占用方再启动桥接。

## 8. 已知限制

| 限制 | 原因 |
| --- | --- |
| 频率只能整 MHz | C5 固件 `FREQ` 语义（无 FOFS）；要 kHz 级需新增固件命令 |
| 真实带宽 ≤ 80 kHz | 固件 IQS 上限；插值只改变"显示宽度" |
| 改频率有 ~0.5–1 s 断点 | 固件在流式期间拒绝 FREQ，必须停流再开 |
| 增益不是 dB | C5 增益是 PHY 表项索引 0..80 |
| 单客户端 | iiod 实现只服务一个 SDR#；第二个连接会被拒绝并记日志 |
| 不做 DDC/抽取 | C5 单核无 DDC；抽取只发生在固件 IQS 内部 |

## 9. 自测与验收

`dev/` 里有一个用**和 SDR# 相同的 libiio C# 绑定**写的探针，可在不启动 SDR# 的情况下
验证整条协议链（context / 属性读写 / OPEN / READBUF）：

```bash
cd bridge/dev
./Probe.exe ip:127.0.0.1
```

预期输出（真板 80 kS/s）：

```
ctx '127.0.0.1 ESP32-C5 SDR (iiod emulation)' lib=0.14 backend=0.21
LO freq before = 2412000000
LO freq after  = 2418000000
rate=80000 bw=20000000 gmode=manual gain=40.000000
rate after = 250000
gmode after = manual
gain after = 60.000000
sample_size = 4
read 8192 bytes
mean=... peak=...
OK-ALL
```

对应关系：

| 验收点 | 验证方式 |
| --- | --- |
| SDR# 识别设备并出频谱 | 启动 SDR# → 点 ▶ → 桥接日志出现"客户端连入 / OPEN"；SDR# 界面出频谱 |
| 改中心频率 → 固件收到 FREQ | 日志 `tune X MHz -> FREQ Y MHz`；`Probe.exe` 写 `frequency` 也会触发 |
| 连续刷新 ≥30 s | 桥接日志无反压错误；`IQS?` 的 dropped 只来自串口背压（正常） |
| 10k/40k/80k 三档 | `--rate` 或 SDR# 改采样率后日志 `firmware IQS ON <rate>` |
| 关闭 SDR# → 1 s 内停流 + 释放租约 | 日志 `no client: IQS OFF + RELEASE, serial port released`，随后 monitor_gui 可直接连 |
| 无硬件协议自测 | `--fake` + `Probe.exe ip:127.0.0.1` |

> SDR# 不提供命令行指定源/地址的入口，也不自动开始流，所以"出频谱"这一步需要一次
> 人工点击 ▶；其余（头部/属性/命令映射/流格式/断开释放）都由 `Probe.exe` 自动断言。

## 10. 其它客户端

| 客户端 | 状态 | 说明 |
| --- | --- | --- |
| **SDR# (PlutoSDR 源)** | ✅ 已验证 | 本仓库的主线用法，见上文 |
| **Python libiio (`pylibiio`)** | ✅ 已验证 | `pip install "pylibiio==0.25"` + 一个 **64 位** `libiio.dll`（可用 MATLAB 自带的，见下）；示例脚本 `tools/pluto_libiio_demo.py`（改频率/速率/增益、读 I/Q、算峰值） |
| **MATLAB 自带 libiio CLI**（`iio_info` / `iio_attr` / `iio_readdev`） | ✅ 已验证 | 在 `C:\ProgramData\MATLAB\SupportPackages\R2026a\3P.instrset\libiio.instrset\win64\Windows-MinGW-W64\`；`iio_readdev -u ip:127.0.0.1 -b 16384 -s 32768 cf-ad9361-lpc` 可直接出数 |
| **MATLAB `sdrrx('Pluto')`**（通信工具箱 Pluto 支持包） | ❌ 暂不支持 | 能连上（能看到 `PRINT` / `SET BUFFERS_COUNT`），但它在自己的初始化里要求完整的 Pluto 属性/驱动语义，目前报 `Failed to write phase inversion attribute` 且**不发出任何属性写**；同一属性用它的 `iio_attr` 写是成功的，属 MathWorks 驱动侧限制 |
| MATLAB 想用数据 | ✅ 建议走文件 | GUI 勾「保存 IQ 原始样本」或用 `--live` 录 `.iq`，MATLAB `fread(fid,[2 inf],'int32')` 直接读 |

Python 示例（真板或 `--fake` 均可）：

```bash
pip install "pylibiio==0.25"
python tools/pluto_libiio_demo.py                 # 2412 MHz / 250 kS/s / 4096 点
python tools/pluto_libiio_demo.py --freq 5180 --rate 250000 --out cap.iq
```

`--fake` 模式下单音在 `fs/8`（80 kS/s → 10 kHz），所以 `peak` 应显示 **+10.0 kHz**，可用来判断
链路是否正常。注意：**串口侧仍是 10k–80k**，SDR#/libiio 请求的 250 kS/s 是主机插值出来的，
真实信息只占中心 ±40 kHz。
