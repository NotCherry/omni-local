$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path '.venv\Scripts\python.exe')) { & '.\Install.ps1' }
& '.\.venv\Scripts\python.exe' -m pip install pyinstaller==6.16.0
if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed.' }
& '.\.venv\Scripts\python.exe' -m PyInstaller --noconfirm --clean --onefile --windowed --name OmniLocal --collect-all sounddevice --collect-all _sounddevice_data app.py
if ($LASTEXITCODE -ne 0) { throw 'Executable build failed.' }
Write-Host 'Built dist\OmniLocal.exe. No model is bundled.'
