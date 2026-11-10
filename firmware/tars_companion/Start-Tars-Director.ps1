param([switch]$Worker, [switch]$Mock, [switch]$Setup, [switch]$NoOpen, [switch]$ResetKey)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$urlFile = Join-Path $PSScriptRoot 'data\director\open-url.local.txt'
if ($Mock) { $urlFile = Join-Path $PSScriptRoot 'data\director\mock-url.local.txt' }
$keyPath = Join-Path $PSScriptRoot 'doubao-key.credential.local.xml'
$configPath = Join-Path $PSScriptRoot 'director-tts.local.json'
if ($Setup) {
    Write-Host 'Tars text-to-speech setup. Leave blank to keep the current value.'
    if ($ResetKey -or -not (Test-Path -LiteralPath $keyPath)) {
        $secret = Read-Host 'Volcengine API Key (hidden, encrypted for this Windows account)' -AsSecureString
        if ($secret.Length -gt 0) { $secret | Export-Clixml -LiteralPath $keyPath }
        $secret.Dispose()
    }
    $speaker = Read-Host 'Speaker ID [S_vXjewQyk2]'
    $resource = Read-Host 'Voice resource from console [seed-icl-2.0]'
    $model = Read-Host 'Synthesis model from console [seed-tts-2.0-standard]'
    if (-not $speaker) { $speaker='S_vXjewQyk2' }
    if (-not $resource) { $resource='seed-icl-2.0' }
    if (-not $model) { $model='seed-tts-2.0-standard' }
    @{speaker=$speaker;resource=$resource;model=$model} | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding UTF8
}
if ($Worker) {
    $previous = $env:DOUBAO_API_KEY
    $secureKey = $null
    try {
        if (Test-Path -LiteralPath $keyPath) {
            $secureKey = Import-Clixml -LiteralPath $keyPath
            $env:DOUBAO_API_KEY = [System.Net.NetworkCredential]::new('', $secureKey).Password
        }
        if (Test-Path -LiteralPath $configPath) {
            $cfg = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
            $env:TARS_TTS_SPEAKER=$cfg.speaker
            $env:TARS_TTS_RESOURCE=$cfg.resource
            $env:TARS_TTS_MODEL=$cfg.model
        }
        $env:PYTHONUTF8='1'
        $runtimeArgs=@('-u','-m','tars_companion.director')
        if ($Mock) { $runtimeArgs+='--mock' }
        & $python @runtimeArgs
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    } finally {
        $env:DOUBAO_API_KEY=$previous
        if ($secureKey) { $secureKey.Dispose() }
    }
    exit
}
function Test-Director {
    try { return (Invoke-WebRequest -Uri 'http://127.0.0.1:8870/' -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 }
    catch { return $false }
}
if ($Setup -and (Test-Director)) {
    Set-Content -LiteralPath 'data\stop.request' -Value 'reload_settings' -Encoding ASCII
    for ($i=0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        if (-not (Test-Director)) { break }
    }
}
if (-not (Test-Director)) {
    # Ask the established voice service to release its connections cleanly.
    New-Item -ItemType Directory -Path 'data\director' -Force | Out-Null
    Set-Content -LiteralPath 'data\stop.request' -Value 'director_takeover' -Encoding ASCII
    Start-Sleep -Seconds 4
    $argsList='-NoProfile -ExecutionPolicy Bypass -File "'+$PSCommandPath+'" -Worker'
    if ($Mock) { $argsList+=' -Mock' }
    Start-Process -FilePath 'powershell.exe' -ArgumentList $argsList -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput 'data\director\service.out.log' -RedirectStandardError 'data\director\service.err.log' | Out-Null
    $ready=$false
    for ($i=0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        if ((Test-Director) -and (Test-Path -LiteralPath $urlFile)) { $ready=$true; break }
    }
    if (-not $ready) { throw 'Director did not start. See data\director\service.err.log; close other robot services and retry.' }
}
$url = (Get-Content -LiteralPath $urlFile -Raw).Trim()
if ($url -notmatch '^http://127\.0\.0\.1:8870/#[a-f0-9]{64}$') { throw 'Invalid local director URL' }
if (-not $NoOpen) { Start-Process $url }
