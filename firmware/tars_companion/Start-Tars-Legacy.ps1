param([switch]$Setup, [switch]$ResetKey, [switch]$RenewFreeSnapshot, [switch]$ChatOnly, [switch]$Unattended)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$Host.UI.RawUI.WindowTitle = 'TARS - Continuous Voice and Motion'
$settingsPath = Join-Path $PSScriptRoot 'tars-service.local.json'
if ($Setup -or -not (Test-Path -LiteralPath $settingsPath)) {
    if ($Unattended) { throw 'Run setup once before automatic startup.' }
    $free = [int](Read-Host 'Current remaining FREE tokens in the Volcengine console')
    if ($free -lt 100000) { throw 'At least 100000 verified free tokens required; no payment is enabled here.' }
    $motion = (Read-Host 'Enable voice motion? Type YES only when you are beside TARS and can cut servo power') -ceq 'YES'
    $support = $false
    if ($motion) { $support = (Read-Host 'Have support conditions for the tested emotion motions been verified? Type YES or press Enter') -ceq 'YES' }
    @{ free_tokens=$free; motion_enabled=$motion; support_confirmed=$support; vad_threshold=500 } |
        ConvertTo-Json | Set-Content -LiteralPath $settingsPath -Encoding UTF8
}
$settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
$previousKey = $env:DOUBAO_API_KEY
$secureKey = $null
try {
    if (-not $env:DOUBAO_API_KEY -or $ResetKey) {
        $keyPath = Join-Path $PSScriptRoot 'doubao-key.credential.local.xml'
        if ((Test-Path -LiteralPath $keyPath) -and -not $ResetKey) {
            $secureKey = Import-Clixml -LiteralPath $keyPath
        } else {
            if ($Unattended) { throw 'Encrypted local API key missing; run setup once.' }
            $secureKey = Read-Host 'Doubao API Key (hidden, saved encrypted for this Windows account)' -AsSecureString
            if ($secureKey.Length -lt 20) { throw 'Invalid API key length' }
            $secureKey | Export-Clixml -LiteralPath $keyPath
        }
        if ($secureKey -isnot [System.Security.SecureString]) { throw 'Invalid encrypted key file' }
        $env:DOUBAO_API_KEY = [System.Net.NetworkCredential]::new('', $secureKey).Password
    }
    $runtimeArgs = @('-u','-m','tars_companion.voice_service','--live','--free-tokens', [string]$settings.free_tokens,
                     '--vad-threshold', [string]$settings.vad_threshold)
    if ($settings.motion_enabled -and -not $ChatOnly) {
        Write-Host 'Voice motion enabled. Stay beside TARS; /stop or Ctrl+C stops this service.' -ForegroundColor Yellow
        if (-not $Unattended) { Read-Host 'Confirm the floor is clear and you can cut servo power. Press ENTER to start' | Out-Null }
        $runtimeArgs += @('--allow-motion','--motion-confirmation','I_AM_BESIDE_TARS_AND_CAN_CUT_SERVO_POWER')
        if ($settings.support_confirmed) { $runtimeArgs += '--support-confirmed' }
    }
    if ($RenewFreeSnapshot) { $runtimeArgs += '--renew-free-snapshot' }
    if ($Unattended) { $runtimeArgs += '--headless' }
    New-Item -ItemType Directory -Path 'evidence' -Force | Out-Null
    Write-Host 'Wait for LISTENING. Speak Chinese or English. Memory is stored locally.' -ForegroundColor Cyan
    & '.\.venv\Scripts\python.exe' @runtimeArgs 2>&1 | Tee-Object -FilePath 'evidence\tars-service-last.log'
    if ($LASTEXITCODE -ne 0) { Write-Host 'Service stopped. Keep this window open for the diagnostic line.' -ForegroundColor Yellow }
} finally {
    $env:DOUBAO_API_KEY = $previousKey
    if ($secureKey) { $secureKey.Dispose() }
}
