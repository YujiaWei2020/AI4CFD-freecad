# Sync AI4CFD addon files from the project folder to FreeCAD's Mod directory.
# Run from PowerShell: .\sync_to_appdata.ps1

$src = "E:\AI\3d_DL_prediction\AI4CFD_core\freecad_addon\AI4CFD"
$dst = "C:\Users\yujia\AppData\Roaming\FreeCAD\Mod\AI4CFD"

robocopy $src $dst /MIR /XD __pycache__ .git /XF "*.pyc" "*.pyo" /NP /NFL
Write-Host "`nSync complete."
