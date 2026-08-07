# Rebuild AI4CFD_export/ from the dev workspace, then zip it for distribution.
# Run from PowerShell: .\build_export.ps1
#
# Two source locations feed the export package:
#   1. freecad_addon/AI4CFD/       -- mirrored in full (it's a 1:1 copy of the workbench)
#   2. config.py + utilities/*.py  -- updated in place, but ONLY for files the export
#      already carries. The export deliberately excludes training/notebook-only
#      utilities (e.g. pytorch_dataset_from_VTK.py, visualization_helpers.py) that
#      live in the workspace -- this script must not pull those in.
#
# Output: AI4CFD_export.zip next to this script, ready to hand off or attach
# to a GitHub release.

$ErrorActionPreference = "Stop"
$RepoRoot  = $PSScriptRoot
$Workspace = "E:\AI\3d_DL_prediction\AI4CFD_core"
$ExportDir = Join-Path $RepoRoot "AI4CFD_export"
$ZipPath   = Join-Path $RepoRoot "AI4CFD_export.zip"

if (-not (Test-Path $Workspace)) {
    Write-Error "Workspace not found: $Workspace"
    exit 1
}
if (-not (Test-Path $ExportDir)) {
    Write-Error "Export folder not found: $ExportDir"
    exit 1
}

Write-Host "AI4CFD export builder" -ForegroundColor Cyan
Write-Host "Workspace: $Workspace"
Write-Host "Export:    $ExportDir`n"

# --- 1. Mirror the workbench itself ------------------------------------------
$src = Join-Path $Workspace "freecad_addon\AI4CFD"
$dst = Join-Path $ExportDir "freecad_addon\AI4CFD"
Write-Host "Syncing workbench -> $dst"
robocopy $src $dst /MIR /XD __pycache__ .git /XF "*.pyc" "*.pyo" /NP /NFL | Out-Null

# --- 2. Update config.py + the utilities/ files the export already carries ---
Write-Host "Updating config.py ..."
Copy-Item (Join-Path $Workspace "config.py") (Join-Path $ExportDir "config.py") -Force

$exportUtils = Join-Path $ExportDir "utilities"
Get-ChildItem $exportUtils -Filter "*.py" | ForEach-Object {
    $srcFile = Join-Path $Workspace "utilities\$($_.Name)"
    if (Test-Path $srcFile) {
        Copy-Item $srcFile $_.FullName -Force
        Write-Host "  updated utilities\$($_.Name)"
    } else {
        Write-Warning "  $($_.Name) exists in export but not in workspace -- left as-is"
    }
}

# Clean any __pycache__ that crept in
Get-ChildItem $ExportDir -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force

# --- 3. Zip it -----------------------------------------------------------------
if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
Compress-Archive -Path "$ExportDir\*" -DestinationPath $ZipPath
Write-Host "`nBuilt $ZipPath" -ForegroundColor Green

# --- 4. Reminder to commit -----------------------------------------------------
Push-Location $RepoRoot
$changed = git status --porcelain -- AI4CFD_export
Pop-Location
if ($changed) {
    Write-Host "`nAI4CFD_export/ has changes relative to the last commit -- review with" -ForegroundColor Yellow
    Write-Host "'git diff -- AI4CFD_export' and commit before sharing the zip." -ForegroundColor Yellow
}
