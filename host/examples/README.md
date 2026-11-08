# examples/ — GUI 使用说明与示例脚本

| 文件 | 用途 |
| --- | --- |
| `GUI使用说明.md` | **主文档**：四个模式（SDR / WiFi Monitor / BLE Monitor / SDR# 桥接）的功能表、操作步骤、示例、常见错误 |
| `gui_libiio_record.py` | 示例：GUI 开着「SDR# 桥接」时，用 **Python libiio** 连续录 IQ 到 `.iq`（int16 交错） |
| `pcap_summary.py` | 示例：不装 tshark 也能汇总 GUI 存的 WiFi/BLE pcap（帧类型、信道、RSSI、BSSID/AdvA） |
| `wireshark_pipe.bat` | 示例：打开 Wireshark 读 GUI 的命名管道（`wifi` / `ble`） |

## 快速跑一遍

```bat
:: 1) 打开 GUI（或源码），连接 COM10
monitor_gui\dist\esp_sdr_gui.exe

:: 2) SDR 模式录 IQ 的离线路子
python tools\iq_analyze.py capture.iq --format int32 --rate 40000 --center 2412

:: 3) 抓包后汇总（WiFi / BLE 任选）
python examples\pcap_summary.py wifi_monitor.pcap --top 5
python examples\pcap_summary.py ble_monitor.pcap  --top 5

:: 4) Wireshark 实时看管道（先在 GUI 勾「Wireshark 实时管道」）
examples\wireshark_pipe.bat wifi
examples\wireshark_pipe.bat ble

:: 5) GUI 处于「SDR# 桥接」模式时，用 Python libiio 录 IQ
pip install "pylibiio==0.25"
python examples\gui_libiio_record.py --seconds 5 --rate 250000 --out rec.iq
```

> **libiio 的入口就是 GUI 的「SDR# 桥接」模式**：点它之后本进程在 `127.0.0.1:30431`
> 提供 iiod 服务，SDR# / Python `pylibiio` / MATLAB 的 `iio_info|iio_attr|iio_readdev`
> 都连这个地址。不需要（也不要同时）再开 `bridge/dist/esp_sdr_bridge.exe`。

## 依赖

| 示例 | 依赖 |
| --- | --- |
| `gui_libiio_record.py` | `pylibiio==0.25` + 64 位 `libiio.dll`（脚本默认用 MATLAB 支持包里那份） |
| `pcap_summary.py` | 无（纯标准库） |
| `wireshark_pipe.bat` | Wireshark（默认 `D:\Wireshark\Wireshark.exe`） |
| `tools/iq_analyze.py` | `numpy` + `matplotlib` |
