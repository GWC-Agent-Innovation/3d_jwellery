$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path '.venv/Scripts/python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11 or newer, then run this script again.' }
}
& .venv/Scripts/python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
if (-not (Test-Path '.env')) { Copy-Item .env.example .env }
& .venv/Scripts/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8017

