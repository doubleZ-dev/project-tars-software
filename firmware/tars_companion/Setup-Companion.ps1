$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & py -3.13 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.13 for Windows, then run this setup again.' }
}
& '.\.venv\Scripts\python.exe' -m pip install '.[daily]'
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
if (-not (Test-Path -LiteralPath 'wifi-audio.local.json')) {
    Write-Host 'Restore your existing wifi-audio.local.json pairing file before starting TARS.'
    Write-Host 'Do not share that file or the encrypted API credential in public packages.'
}
Write-Host 'Companion installed. The existing encrypted Doubao key is reused on this Windows account.'
