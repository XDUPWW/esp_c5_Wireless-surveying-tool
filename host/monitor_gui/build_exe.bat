@echo off
rem Build esp_sdr_gui.exe (one file, no console) into monitor_gui\dist.
rem Run from anywhere:  build_exe.bat
rem
rem numpy 现在会被打进去：SDR# 桥接模式用它做主机侧重采样（纯 Python 太慢）。
rem matplotlib 仍然排除（GUI 和桥接都不需要）。
set "HERE=%~dp0"
set "PY=C:\Espressif\tools\python\v6.1\venv\Scripts\python.exe"
cd /d "%HERE%"
"%PY%" -m PyInstaller --onefile --windowed --name esp_sdr_gui ^
    --collect-submodules serial ^
    --hidden-import sdrsharp_bridge --paths . ^
    --exclude-module matplotlib ^
    esp_sdr_gui.py
echo.
echo Result: %HERE%dist\esp_sdr_gui.exe
pause
