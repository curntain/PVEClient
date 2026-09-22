param([string]$Python = "python", [string]$Version = "1.0.0")
$ErrorActionPreference = "Stop"
$Version = $Version -replace '^v', ''
$env:VERSION = $Version
& $Python -m PyInstaller --noconfirm --clean PVEClient.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
$compiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (!$compiler) { throw "Inno Setup 6 compiler (ISCC.exe) not found" }
& $compiler.Source "installer/windows/PVEClient.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
Write-Host "Windows installer created in dist-windows"
