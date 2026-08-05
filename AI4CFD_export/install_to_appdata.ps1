# Installs the AI4CFD FreeCAD workbench into this user's FreeCAD Mod folder.
# Run from PowerShell, from inside the extracted AI4CFD_export folder:
#   .\install_to_appdata.ps1

$src = Join-Path $PSScriptRoot "freecad_addon\AI4CFD"
$dst = Join-Path $env:APPDATA "FreeCAD\Mod\AI4CFD"

if (-not (Test-Path $src)) {
    Write-Error "Can't find $src -- run this script from inside the extracted AI4CFD_export folder."
    exit 1
}

New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
robocopy $src $dst /MIR /XD __pycache__ .git /XF "*.pyc" "*.pyo" /NP /NFL

Write-Host "`nAI4CFD workbench installed to: $dst"
Write-Host "This computer's project root (config.py, utilities/) is at: $PSScriptRoot"
Write-Host "Next steps:"
Write-Host "  1. Start FreeCAD, open Edit -> Preferences -> AI4CFD"
Write-Host "  2. Set 'Project root' to: $PSScriptRoot"
Write-Host "  3. Set 'Python executable' to a native Python (not FreeCAD's own) with cadquery, numpy, scipy installed"
Write-Host "  4. Make sure the CfdOF workbench and a native OpenFOAM install are set up (Edit -> Preferences -> CfdOF)"
