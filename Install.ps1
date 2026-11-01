$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& python -c "import sys, tkinter; assert (3,10) <= sys.version_info[:2] < (3,13), 'Use Python 3.10, 3.11 or 3.12 (64-bit)'; assert sys.maxsize > 2**32, '64-bit Python is required'"
if ($LASTEXITCODE -ne 0) { throw 'Install 64-bit Python 3.10-3.12 with tkinter, venv and pip from python.org.' }
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10-3.12 with tkinter and venv is required. Install 64-bit Python from python.org.' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& '.\.venv\Scripts\python.exe' -c "import tkinter, sounddevice, websockets; print('Omni Local dependencies ready')"
if ($LASTEXITCODE -ne 0) { throw 'Prerequisite verification failed.' }
Write-Host 'Ready. Double-click Run.cmd. No models were downloaded.'
