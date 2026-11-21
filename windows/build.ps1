# Build "New Niche.exe" from windows/launcher.py (run in Windows PowerShell, from the project
# inside WSL):
#   powershell -ExecutionPolicy Bypass -File \\wsl.localhost\<distro>\home\<user>\new-niche-overall\windows\build.ps1
#
# Uses its own venv in %LOCALAPPDATA%\New Niche\build (your Python installs aren't touched),
# puts the exe in %LOCALAPPDATA%\Programs\New Niche and a "New Niche" shortcut on the Desktop.
# The exe only starts the app in WSL and shows it, so it doesn't need rebuilding when the app
# changes; rebuild only after changing launcher.py.
#
# Where the project is (distro, user, folder) is worked out from this script's own path and
# saved in %LOCALAPPDATA%\New Niche\launcher.json for the exe, so the repo holds no local names.

$ErrorActionPreference = 'Stop'
$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$work    = Join-Path $env:LOCALAPPDATA 'New Niche\build'
$venv    = Join-Path $work 'venv'
$install = Join-Path $env:LOCALAPPDATA 'Programs\New Niche'
New-Item -ItemType Directory -Force $work, $install | Out-Null

# \\wsl.localhost\<distro>\home\<user>\<project>\windows  (or \\wsl$\...)
if ($here -notmatch '^\\\\wsl(?:\.localhost|\$)\\([^\\]+)\\(.+)\\windows\\?$') {
    throw "Run this from the project inside WSL (\\wsl.localhost\<distro>\...\windows\build.ps1), not from $here"
}
$distro  = $Matches[1]
$project = '/' + ($Matches[2] -replace '\\', '/')
$user    = if ($project -match '^/home/([^/]+)/') { $Matches[1] } else { 'root' }
$config  = Join-Path $env:LOCALAPPDATA 'New Niche\launcher.json'
$json    = @{ distro = $distro; user = $user; project = $project } | ConvertTo-Json
# UTF-8 without a byte-order mark (Set-Content -Encoding UTF8 adds one in Windows PowerShell).
[System.IO.File]::WriteAllText($config, $json, (New-Object System.Text.UTF8Encoding $false))
Write-Host "Project location saved for the exe: $config"

if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    Write-Host 'Creating the build venv...'
    py -3.13 -m venv $venv
    if ($LASTEXITCODE) { py -3 -m venv $venv }
}
$py = Join-Path $venv 'Scripts\python.exe'
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet 'pywebview>=5.3' 'pyinstaller>=6.10' 'pillow>=10'
if ($LASTEXITCODE) { throw 'pip install failed' }

# PyInstaller works from a local folder (not \\wsl.localhost).
Copy-Item (Join-Path $here 'launcher.py') (Join-Path $work 'launcher.py') -Force

# The app's icon: the red "N" from the site header.
$icon = Join-Path $work 'new-niche.ico'
& $py -c @"
from PIL import Image, ImageDraw, ImageFont
img = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((8, 8, 248, 248), radius=56, fill=(200, 50, 43, 255))
try:
    font = ImageFont.truetype('georgia.ttf', 170)
except OSError:
    font = ImageFont.load_default()
d.text((128, 136), 'N', fill='white', font=font, anchor='mm')
img.save(r'$icon', sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
"@

Push-Location $work
try {
    & $py -m PyInstaller --noconfirm --clean --onefile --windowed --name 'New Niche' `
        --icon $icon --distpath $install --workpath (Join-Path $work 'pyi') `
        --specpath $work launcher.py
    if ($LASTEXITCODE) { throw 'PyInstaller failed' }
} finally { Pop-Location }

$exe = Join-Path $install 'New Niche.exe'
$desktop = [Environment]::GetFolderPath('Desktop')
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path $desktop 'New Niche.lnk'))
$lnk.TargetPath = $exe
$lnk.WorkingDirectory = $install
$lnk.IconLocation = "$exe,0"
$lnk.Description = 'New Niche (runs the app from WSL)'
$lnk.Save()

Write-Host "Built: $exe"
Write-Host "Shortcut: $(Join-Path $desktop 'New Niche.lnk')"
