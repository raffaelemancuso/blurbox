# Create blurbox.lnk next to blurbox.py: a shortcut that
# starts the GUI through uvw.exe (uv's windowless launcher), so no console
# window appears, unlike a .bat. Drag a video or a project .json onto the
# shortcut to open it. Rerun after reinstalling uv or moving this folder,
# since a shortcut stores absolute paths.
#
# Usage:
#   .\blurbox_shortcut.ps1                 # shortcut in this folder
#   .\blurbox_shortcut.ps1 -Desktop        # also one on the Desktop

param([switch]$Desktop)

$ErrorActionPreference = "Stop"

$uvw = (Get-Command uvw.exe -ErrorAction SilentlyContinue).Source
if (-not $uvw) { throw "uvw.exe not found on PATH: install uv first (https://docs.astral.sh/uv/)." }
$script = Join-Path $PSScriptRoot "blurbox.py"
if (-not (Test-Path $script)) { throw "Not found: $script" }
$icon = Join-Path $PSScriptRoot "docs\logo.ico"

$targets = @(Join-Path $PSScriptRoot "blurbox.lnk")
if ($Desktop) {
    $targets += Join-Path ([Environment]::GetFolderPath("Desktop")) "Blurbox.lnk"
}

$shell = New-Object -ComObject WScript.Shell
foreach ($lnk in $targets) {
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = $uvw
    $s.Arguments = "run --gui-script `"$script`""
    $s.WorkingDirectory = $PSScriptRoot
    if (Test-Path $icon) { $s.IconLocation = "$icon,0" }
    $s.Description = "Cover part of a video (black box, blur, pixelate) with ffmpeg"
    $s.Save()
    Write-Host "Created: $lnk"
}
