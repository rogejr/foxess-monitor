# build.ps1
# One-click builder for FoxESS Tray Monitor.
#
# How to run:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\build.ps1
#
# Output:
#   dist\FoxessTray.exe                        (portable standalone exe)
#   installer\FoxessTraySetup-1.1.0.exe        (the installer, if Inno Setup found)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "===========================================" -ForegroundColor Cyan
Write-Host "  FoxESS Tray Monitor - Build Script" -ForegroundColor Cyan
Write-Host "===========================================" -ForegroundColor Cyan
Write-Host ""

# ---- 1. Check Python -------------------------------------------------------
Write-Host "[1/4] Checking Python..." -ForegroundColor Yellow
try {
    $pyVersion = & python --version 2>&1
    Write-Host "    Found: $pyVersion"
} catch {
    Write-Host "    ERROR: Python is not installed or not on PATH." -ForegroundColor Red
    Write-Host "    Install it from https://www.python.org/downloads/windows/"
    Write-Host "    Make sure to tick 'Add Python to PATH' during installation."
    exit 1
}

# ---- 2. Install deps -------------------------------------------------------
Write-Host ""
Write-Host "[2/4] Installing Python dependencies..." -ForegroundColor Yellow
& python -m pip install --upgrade pip --quiet
& python -m pip install -r requirements.txt --quiet
& python -m pip install pyinstaller --quiet
Write-Host "    Done."

# ---- 3. Build the .exe -----------------------------------------------------
Write-Host ""
Write-Host "[3/4] Building FoxessTray.exe with PyInstaller..." -ForegroundColor Yellow

# Clean previous builds so we do not ship stale code
if (Test-Path "build")  { Remove-Item "build"  -Recurse -Force }
if (Test-Path "dist")   { Remove-Item "dist"   -Recurse -Force }
if (Test-Path "FoxessTray.spec") { Remove-Item "FoxessTray.spec" -Force }

# Build PyInstaller arguments as an array, then splat. This avoids all line
# continuation and quoting issues with the --add-data "file;dest" syntax.
$pyiArgs = @(
    "-m", "PyInstaller",
    "--onefile",
    "--noconsole",
    "--name", "FoxessTray",
    "--icon", "foxess.ico",
    "--add-data", "foxess.ico;.",
    "--add-data", "foxess.png;.",
    "foxess_tray.py"
)
& python @pyiArgs

if (-not (Test-Path "dist\FoxessTray.exe")) {
    Write-Host "    ERROR: Build failed - dist\FoxessTray.exe not found." -ForegroundColor Red
    exit 1
}
$exeSize = [math]::Round((Get-Item "dist\FoxessTray.exe").Length / 1MB, 1)
Write-Host "    Built dist\FoxessTray.exe ($exeSize MB)."

# ---- 4. Compile the installer (optional) -----------------------------------
Write-Host ""
Write-Host "[4/4] Compiling installer with Inno Setup..." -ForegroundColor Yellow

# Look for Inno Setup in the usual install locations
$innoPaths = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 5\ISCC.exe"
)
$iscc = $innoPaths | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $iscc) {
    Write-Host "    Inno Setup not found - skipping installer step." -ForegroundColor DarkYellow
    Write-Host "    Your standalone exe is ready at:  dist\FoxessTray.exe"
    Write-Host ""
    Write-Host "    To also produce a proper installer, install Inno Setup from"
    Write-Host "    https://jrsoftware.org/isdl.php  and re-run this script."
    exit 0
}

Write-Host "    Found Inno Setup at: $iscc"
& $iscc "installer.iss"

if (Test-Path "installer\FoxessTraySetup-1.1.0.exe") {
    Write-Host ""
    Write-Host "===========================================" -ForegroundColor Green
    Write-Host "  Build complete!" -ForegroundColor Green
    Write-Host "===========================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Installer:  installer\FoxessTraySetup-1.1.0.exe"
    Write-Host "  Portable :  dist\FoxessTray.exe"
    Write-Host ""
} else {
    Write-Host "    ERROR: Installer compile failed." -ForegroundColor Red
    exit 1
}
