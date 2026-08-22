$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if ($env:DEEPSEEK_API_KEY) { & powershell -NoProfile -ExecutionPolicy Bypass -File packaging\make_embedded_config.ps1 } else { Write-Host "DEEPSEEK_API_KEY is not set; the EXE will use local OCR only." }
if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
  Write-Host "PyInstaller is not installed. Run: py -m pip install -r requirements-build.txt"
  exit 1
}
if (Test-Path build) { Remove-Item build -Recurse -Force }
if (Test-Path dist) { Remove-Item dist -Recurse -Force }
if (Test-Path icon.png) { py packaging\make_icon.py }
if (Test-Path embedded_config.py) { pyinstaller --clean --noconfirm --onefile --windowed --icon build\本地发票整理.ico --hidden-import embedded_config --name "本地发票整理" desktop_entry.py } else { pyinstaller --clean --noconfirm --onefile --windowed --icon build\本地发票整理.ico --name "本地发票整理" desktop_entry.py }
if (Test-Path embedded_config.py) { Remove-Item embedded_config.py -Force }
Write-Host "Created dist\本地发票整理.exe"
