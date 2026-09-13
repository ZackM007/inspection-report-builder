# Fernwater Inspection Report Builder — first-time setup
# Run from the project root:  .\scripts\setup.ps1

$ErrorActionPreference = 'Stop'

Write-Host "Fernwater Inspection Report Builder — setup" -ForegroundColor Cyan
Write-Host "--------------------------------------"

function Find-Python {
    foreach ($cmd in @('py', 'python', 'python3')) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if (-not $found) { continue }
        try {
            $v = & $cmd --version 2>&1
            if ($LASTEXITCODE -eq 0 -and $v -match 'Python 3\.(1[1-9]|[2-9][0-9])') {
                return $cmd
            }
        } catch {}
    }
    return $null
}

$pythonCmd = Find-Python
if (-not $pythonCmd) {
    Write-Host ""
    Write-Host "Python 3.11 or newer is not installed (or not on PATH)." -ForegroundColor Red
    Write-Host ""
    Write-Host "Install it from: https://www.python.org/downloads/windows/"
    Write-Host "  - During the installer, check 'Add python.exe to PATH'"
    Write-Host "  - Do NOT use the Microsoft Store version"
    Write-Host ""
    Write-Host "After installing, open a NEW PowerShell window and run this script again."
    exit 1
}

Write-Host "Found Python: $(& $pythonCmd --version)" -ForegroundColor Green

if (-not (Test-Path .venv)) {
    Write-Host "Creating virtual environment in .venv ..."
    & $pythonCmd -m venv .venv
}

$activate = Join-Path .venv 'Scripts\Activate.ps1'
if (-not (Test-Path $activate)) {
    Write-Host "Virtual environment seems broken — delete .venv and rerun this script." -ForegroundColor Red
    exit 1
}
. $activate

Write-Host "Upgrading pip ..."
python -m pip install --upgrade pip --quiet

Write-Host "Installing dependencies (this may take a few minutes the first time) ..."
pip install -e . --quiet

Write-Host "Downloading Playwright's bundled Chromium (one-time, ~150 MB) ..."
python -m playwright install chromium

foreach ($d in @('assets', 'assets\fonts', 'assets\logos', 'inputs', 'outputs')) {
    if (-not (Test-Path $d)) {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
    }
}

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Created .env — leave ANTHROPIC_API_KEY empty; no key is needed (Claude writes narratives in-session)."
}

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host ""
Write-Host "Brand assets (logo, fonts, inspector certs) ship in the repo under assets\ — nothing to drop in."
Write-Host ""
Write-Host "Easiest way to build a report: open Claude Code in this folder and say 'build the report'."
Write-Host ""
Write-Host "Manual CLI alternative (every new PowerShell session):"
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "  inspection-report build `"sample-data\Prop 4820 - Inspection Report.xlsx`""
