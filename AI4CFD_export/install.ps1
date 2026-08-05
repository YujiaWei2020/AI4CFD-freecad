# One-click installer for the AI4CFD FreeCAD workbench.
# Run by double-clicking install.bat, or: powershell -ExecutionPolicy Bypass -File install.ps1
#
# Assumes FreeCAD, the CfdOF workbench, and a native OpenFOAM install are already
# set up on this machine -- this script only handles the AI4CFD-specific pieces:
#   1. Copy the workbench into FreeCAD's per-user Mod folder
#   2. Find (or ask for) a native Python and install its pip dependencies
#   3. Write the AI4CFD preferences (project root, python exe) via FreeCAD itself

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot

Write-Host "AI4CFD installer" -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot`n"

# --- 1. Copy the workbench into FreeCAD's per-user Mod folder ---------------
$src = Join-Path $ProjectRoot "freecad_addon\AI4CFD"
$dst = Join-Path $env:APPDATA "FreeCAD\Mod\AI4CFD"

if (-not (Test-Path $src)) {
    Write-Error "Can't find $src -- run this script from inside the extracted AI4CFD_export folder."
    exit 1
}

New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
Write-Host "Installing workbench to $dst ..."
robocopy $src $dst /MIR /XD __pycache__ .git /XF "*.pyc" "*.pyo" /NP /NFL | Out-Null

# --- 2. Find a native Python (not FreeCAD's bundled one) --------------------
function Find-NativePython {
    try {
        $out = & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($out -and (Test-Path $out.Trim())) { return $out.Trim() }
    } catch {}
    $patterns = @(
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "$env:ProgramFiles\Python3*\python.exe",
        "C:\Python3*\python.exe"
    )
    foreach ($p in $patterns) {
        $found = Get-ChildItem $p -ErrorAction SilentlyContinue |
                 Where-Object { $_.FullName -notmatch "WindowsApps" } |
                 Sort-Object FullName -Descending | Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return $null
}

$pythonExe = Find-NativePython
if (-not $pythonExe) {
    $pythonExe = Read-Host "Couldn't auto-detect a native Python install. Enter the full path to python.exe"
}
Write-Host "Using Python: $pythonExe"

Write-Host "Installing Python dependencies (cadquery, numpy, scipy) ..."
& $pythonExe -m pip install --quiet --upgrade cadquery numpy scipy
if ($LASTEXITCODE -ne 0) {
    Write-Warning "pip install reported errors -- you may need to install cadquery/numpy/scipy manually for $pythonExe"
}

# --- 3. Write AI4CFD preferences via FreeCAD's own API -----------------------
function Find-FreeCADCmd {
    $patterns = @(
        "$env:ProgramFiles\FreeCAD*\bin\FreeCADCmd.exe",
        "${env:ProgramFiles(x86)}\FreeCAD*\bin\FreeCADCmd.exe"
    )
    foreach ($p in $patterns) {
        $found = Get-ChildItem $p -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return $null
}

$freecadCmd = Find-FreeCADCmd
if ($freecadCmd) {
    $prefScript = Join-Path $env:TEMP "ai4cfd_set_prefs.py"
    $pyLines = @(
        "import FreeCAD",
        "g = FreeCAD.ParamGet('User parameter:BaseApp/Preferences/Mod/AI4CFD')",
        "g.SetString('WorkingDir', r'$ProjectRoot')",
        "g.SetString('PythonExe', r'$pythonExe')",
        "print('AI4CFD preferences set.')"
    )
    $pyLines | Set-Content -Path $prefScript -Encoding utf8

    Write-Host "Writing AI4CFD preferences via FreeCAD ..."
    & $freecadCmd $prefScript
    Remove-Item $prefScript -Force
} else {
    Write-Warning "Couldn't find FreeCADCmd.exe automatically."
    Write-Warning "Open FreeCAD -> Edit -> Preferences -> AI4CFD and set:"
    Write-Warning "  Project root      = $ProjectRoot"
    Write-Warning "  Python executable = $pythonExe"
}

Write-Host "`nDone. Start FreeCAD, switch to the AI4CFD workbench, and run 'Geometry Container'." -ForegroundColor Green
Write-Host "(This assumes CfdOF + OpenFOAM are already installed and configured.)"
