param([int]$FreeTokens = 0, [switch]$ResetKey,
      [ValidateRange(1,10)][int]$Turns = 1,
      [ValidateRange(30,900)][int]$Seconds = 60,
      [switch]$Daily, [switch]$AllowMotion,
      [string]$ArmConfirmation = '',
      [string]$MemoryPath = 'data\tars-voice.sqlite3')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$Host.UI.RawUI.WindowTitle = "TARS - $Turns TURNS - PRESS ENTER TO START"
Clear-Host
Write-Host '============================================================' -ForegroundColor Cyan
if ($Daily) {
    if (-not $PSBoundParameters.ContainsKey('Turns')) { $Turns = 10 }
    if (-not $PSBoundParameters.ContainsKey('Seconds')) { $Seconds = 900 }
    $Host.UI.RawUI.WindowTitle = 'TARS DAILY - PRESS ENTER TO START'
    Write-Host 'Daily mode: text memory enabled, original Tars voice, /quit to exit.'
    Write-Host 'Session cap: 10 turns / 15 minutes / 20000 reported tokens.'
    if ($FreeTokens -eq 0) {
        $FreeTokens = [int](Read-Host 'Enter the CURRENT remaining FREE token count from the console')
    }
}
Write-Host ' TARS ROBOT VOICE TEST - this is the test window' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
if ($FreeTokens -lt 100000) {
    Write-Host 'First confirm at least 100000 FREE tokens in the current project.'
    Write-Host 'Then run: .\Start-Tars-Seeduplex.ps1 -FreeTokens REMAINING_COUNT'
    exit 1
}
$previousKey = $env:DOUBAO_API_KEY
$keyPath = Join-Path $PSScriptRoot 'doubao-key.credential.local.xml'
try {
    if (-not $env:DOUBAO_API_KEY) {
        if ((Test-Path -LiteralPath $keyPath) -and -not $ResetKey) {
            $secureKey = Import-Clixml -LiteralPath $keyPath
            if ($secureKey -isnot [System.Security.SecureString]) { throw 'Invalid local credential file' }
            Write-Host 'Loaded encrypted local API Key.'
        } else {
            $secureKey = Read-Host 'Enter Speech API Key once (hidden; saved encrypted for this Windows user)' -AsSecureString
            if ($secureKey.Length -lt 20) { throw 'API Key is too short; not saved' }
            $secureKey | Export-Clixml -LiteralPath $keyPath
            Write-Host 'Saved encrypted local API Key; future launches need no paste.'
        }
        $env:DOUBAO_API_KEY = [System.Net.NetworkCredential]::new('', $secureKey).Password
    }
    New-Item -ItemType Directory -Path 'evidence' -Force | Out-Null
    Write-Host "$Turns conversation turn(s), maximum $Seconds seconds. Ctrl+C stops."
    if (-not $AllowMotion) { Write-Host 'Motion remains disabled.' }
    Read-Host 'When you are beside the robot, press ENTER to begin' | Out-Null
    $Host.UI.RawUI.WindowTitle = "TARS - ROBOT VOICE TEST - $Turns TURNS"
    $runtimeArgs = @('-u', '-m', 'tars_companion.seeduplex', '--persona', 'tars-seeduplex.json', '--live', '--free-tokens', $FreeTokens, '--turns', $Turns, '--seconds', $Seconds)
    if ($Daily) { $runtimeArgs += @('--daily', '--memory', $MemoryPath, '--actions') }
    if ($AllowMotion) {
        if ($ArmConfirmation -ne 'I_HAVE_LIFTED_TARS_AND_CAN_CUT_SERVO_POWER') {
            throw 'Motion requires the existing explicit physical preparation confirmation.'
        }
        $runtimeArgs += @('--actions', '--allow-motion', '--arm-confirmation', $ArmConfirmation)
        Write-Host 'Explicit motion mode requested. Reconnect will NOT restore Arm.' -ForegroundColor Yellow
    }
    & '.\.venv\Scripts\python.exe' @runtimeArgs 2>&1 | Tee-Object -FilePath 'evidence\seeduplex-live-last.log'
    Write-Host 'Test finished. You can copy the last lines from THIS window.' -ForegroundColor Cyan
} finally {
    $env:DOUBAO_API_KEY = $previousKey
    if ($secureKey) { $secureKey.Dispose() }
}
