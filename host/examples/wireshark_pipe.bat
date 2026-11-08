@echo off
rem 打开 Wireshark 读 GUI 建的命名管道（先在 GUI 里勾「Wireshark 实时管道」并启动对应模式）
rem 参数: wifi | ble  （默认 wifi）
set "WS=D:\Wireshark\Wireshark.exe"
if not exist "%WS%" set "WS=wireshark.exe"
if /I "%~1"=="ble" (
    echo 连接管道 \\.\pipe\esp_sdr_ble ...
    start "" "%WS%" -k -i \\.\pipe\esp_sdr_ble
) else (
    echo 连接管道 \\.\pipe\esp_sdr_wifi ...
    start "" "%WS%" -k -i \\.\pipe\esp_sdr_wifi
)
