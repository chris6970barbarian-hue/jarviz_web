# Native Windows setup for the Jarviz backend (no Docker required).
#
# What this does, in order:
#   1. Ensures `scoop` is installed.
#   2. Installs Python 3.12 (NOT 3.13 — several deps lack 3.13 wheels still).
#   3. Installs ffmpeg (for Edge-TTS audio decoding).
#   4. Creates a fresh .venv pinned to the python312 interpreter and pip-installs
#      requirements.txt. The PyOgg wheel ships libopus.dll bundled, so no
#      separate native lib step is needed.
#   5. Prints next-step commands.
#
# Re-running is safe: scoop installs are idempotent, and we recreate .venv
# only if it's missing or pinned to a different Python version.

$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)

function Write-Step($msg) { Write-Host "[setup] $msg" -ForegroundColor Cyan }
function Write-Skip($msg) { Write-Host "[setup] $msg" -ForegroundColor DarkGray }
function Write-Info($msg) { Write-Host "[setup] $msg" -ForegroundColor Yellow }

# ---------- scoop ----------
$scoop = (Get-Command scoop -ErrorAction SilentlyContinue)
if (-not $scoop) {
    Write-Info "scoop not found, installing..."
    Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force
    Invoke-RestMethod get.scoop.sh | Invoke-Expression
}

# ---------- Python 3.12 (deliberately, not 3.13) ----------
# Some deps (PyAV via faster-whisper, numpy 1.26.x, PyOgg) only ship up to
# cp312 wheels right now. Pinning to 3.12 keeps `pip install` painless.
$py312Dir = "$env:USERPROFILE\scoop\apps\python312\current"
$py312Exe = Join-Path $py312Dir "python.exe"
if (-not (Test-Path $py312Exe)) {
    Write-Info "installing python312 via scoop..."
    & scoop install python312
} else {
    Write-Skip "python312 already installed"
}
if (-not (Test-Path $py312Exe)) {
    Write-Error "Expected $py312Exe after scoop install. Aborting."
}
$py312Version = (& $py312Exe -c "import sys;print('{0}.{1}.{2}'.format(*sys.version_info[:3]))").Trim()
Write-Step "using python $py312Version at $py312Exe"

# ---------- ffmpeg ----------
$installed = (& scoop list 2>$null) -join "`n"
if ($installed -notmatch "(?ms)^\s*ffmpeg\s") {
    Write-Info "installing ffmpeg..."
    & scoop install ffmpeg
} else {
    Write-Skip "ffmpeg already installed"
}

# ---------- .venv ----------
# If an existing .venv was created with a different Python (e.g. the user's
# default 3.13), nuke and recreate it so wheels resolve cleanly.
$venvPyExe = ".\.venv\Scripts\python.exe"
$rebuildVenv = $true
if (Test-Path $venvPyExe) {
    $venvVersion = (& $venvPyExe -c "import sys;print('{0}.{1}'.format(*sys.version_info[:2]))").Trim()
    if ($venvVersion -eq "3.12") {
        Write-Skip ".venv already pinned to 3.12"
        $rebuildVenv = $false
    } else {
        Write-Info ".venv is on $venvVersion; recreating with 3.12..."
    }
}
if ($rebuildVenv) {
    if (Test-Path ".venv") { Remove-Item -Recurse -Force .venv }
    & $py312Exe -m venv .venv
}

. .\.venv\Scripts\Activate.ps1
Write-Info "installing Python dependencies..."
& python -m pip install --upgrade pip | Out-Null
& python -m pip install -r requirements.txt

Write-Host ""
Write-Host "[setup] done." -ForegroundColor Green
Write-Host "Next steps:" -ForegroundColor Green
Write-Host "  1. Edit .env (set DEEPSEEK_API_KEY and JARVIZ_WS_PUBLIC_URL to your LAN IP)."
Write-Host "  2. .\.venv\Scripts\Activate.ps1"
Write-Host "  3. uvicorn server.main:app --host 0.0.0.0 --port 8080"
Write-Host "  4. From another shell: python scripts\simulate_device.py 'remind me to call mom in 30 seconds'"
