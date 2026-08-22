@echo off
setlocal
cd /d "%~dp0"
if "%DEEPSEEK_API_KEY%"=="" echo DEEPSEEK_API_KEY is not set; the EXE will use local OCR only.
if not "%DEEPSEEK_API_KEY%"=="" powershell -NoProfile -ExecutionPolicy Bypass -File packaging\make_embedded_config.ps1
where pyinstaller >nul 2>nul
if errorlevel 1 (
  echo PyInstaller is not installed. Run: py -m pip install -r requirements-build.txt
  exit /b 1
)
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist icon.png py packaging\make_icon.py
if exist embedded_config.py (
  pyinstaller --clean --noconfirm --onefile --windowed --icon build\本地发票整理.ico --hidden-import embedded_config --name "本地发票整理" desktop_entry.py
) else (
  pyinstaller --clean --noconfirm --onefile --windowed --icon build\本地发票整理.ico --name "本地发票整理" desktop_entry.py
)
if exist embedded_config.py del /q embedded_config.py
if errorlevel 1 exit /b 1
echo Created dist\本地发票整理.exe
endlocal
