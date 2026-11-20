# Builds Harvester.exe and hv.exe and zips them into dist\.
param([string]$Version = "dev", [string]$Python = "")

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$Version = $Version -replace '^v', ''

function Invoke-Python {
    # Windows PowerShell treats native stderr logging as errors; rely on the exit code.
    $ErrorActionPreference = "Continue"
    # setup-python sets pythonLocation in CI; locally the py launcher avoids the Store alias.
    if ($Python) { & $Python @args }
    elseif ($env:pythonLocation) { & (Join-Path $env:pythonLocation "python.exe") @args }
    else { & py -3 @args }
    if ($LASTEXITCODE -ne 0) { throw "python $args failed with exit code $LASTEXITCODE" }
}

Invoke-Python -m pip install --disable-pip-version-check --quiet -r requirements-build.txt
[IO.File]::WriteAllText("$PSScriptRoot\_version.py", "VERSION = `"$Version`"`n")

Remove-Item build, dist -Recurse -Force -ErrorAction SilentlyContinue
$common = @(
    "--noconfirm", "--clean", "--onefile", "--log-level", "WARN",
    "--icon", "$PSScriptRoot\assets\harvester.ico",
    # Windows has no system time zone database; reset times need tzdata.
    "--collect-data", "tzdata",
    "--distpath", "$PSScriptRoot\build\exe", "--workpath", "$PSScriptRoot\build\work",
    "--specpath", "$PSScriptRoot\build"
)
Invoke-Python -m PyInstaller @common --windowed --name Harvester "$PSScriptRoot\app.pyw"
Invoke-Python -m PyInstaller @common --console --name hv "$PSScriptRoot\hv.py"

$stage = "$PSScriptRoot\build\UsageHarvester"
New-Item -ItemType Directory -Force $stage, "$PSScriptRoot\dist" | Out-Null
Copy-Item build\exe\Harvester.exe, build\exe\hv.exe, README.md, LICENSE $stage
Copy-Item skills $stage -Recurse
$zip = "$PSScriptRoot\dist\UsageHarvester-$Version-windows-x64.zip"
Compress-Archive -Path "$stage\*" -DestinationPath $zip -Force
Write-Host "built $zip"
