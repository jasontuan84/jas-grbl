# Dev install for jasGrbl (Windows). Links src/ into the Inkscape user
# extensions dir so code changes are picked up by re-running the extension.
# A directory Junction needs no admin rights.
#
#   powershell -ExecutionPolicy Bypass -File tools\dev_install.ps1

$src = (Resolve-Path "$PSScriptRoot\..").Path
$dst = Join-Path $env:APPDATA "inkscape\extensions\jasGrbl"

if (Test-Path $dst) {
    Write-Host "Removing existing link/dir: $dst"
    cmd /c rmdir "$dst" 2>$null
    if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
}

New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
New-Item -ItemType Junction -Path $dst -Target $src | Out-Null

Write-Host "Linked:"
Write-Host "  $dst  ->  $src"
Write-Host ""
Write-Host "Restart Inkscape, then open: Extensions > jas GRBL"
Write-Host "After editing .py files just re-run the extension; after editing .inx, restart Inkscape."
