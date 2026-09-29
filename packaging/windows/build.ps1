# Build de FacturadorE para Windows: ícono, age, PyInstaller y zip.
#
# Mismo script en CI (.github/workflows/windows.yml) y a mano:
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -Version dev
# Deja packaging\windows\dist\FacturadorE\ (carpeta portable) y
# packaging\windows\dist\FacturadorE-windows-<Version>.zip.
param([string]$Version = "dev")

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# age para Windows, fijado por versión y checksum (binario oficial del
# release de GitHub). Cambiar ambos juntos al actualizar.
$AgeVersion = "v1.2.1"
$AgeSha256 = "46db0db71f146061f7c8fffa912cb806aef1bc8450e27a3abc1744f0591b89fb"

$Here = $PSScriptRoot
$Root = (Resolve-Path (Join-Path $Here "..\..")).Path
$Build = Join-Path $Here "build"
$Dist = Join-Path $Here "dist"

if (Test-Path $Build) { Remove-Item -Recurse -Force $Build }
if (Test-Path $Dist) { Remove-Item -Recurse -Force $Dist }
New-Item -ItemType Directory -Force (Join-Path $Build "bin") | Out-Null

Push-Location $Root
try {
    uv sync --frozen --group packaging
    if ($LASTEXITCODE -ne 0) { throw "uv sync falló" }

    uv run python packaging/windows/make_icon.py (Join-Path $Build "facturadore.ico")
    if ($LASTEXITCODE -ne 0) { throw "No se pudo generar el ícono" }

    $AgeZip = Join-Path $Build "age.zip"
    $AgeUrl = "https://github.com/FiloSottile/age/releases/download/$AgeVersion/age-$AgeVersion-windows-amd64.zip"
    Invoke-WebRequest $AgeUrl -OutFile $AgeZip
    $Hash = (Get-FileHash $AgeZip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Hash -ne $AgeSha256) { throw "age: checksum inesperado ($Hash)" }
    Expand-Archive $AgeZip -DestinationPath (Join-Path $Build "age")
    $AgeDir = Join-Path $Build "age\age"
    Copy-Item (Join-Path $AgeDir "age.exe") (Join-Path $Build "bin")
    Copy-Item (Join-Path $AgeDir "age-keygen.exe") (Join-Path $Build "bin")
    Copy-Item (Join-Path $AgeDir "LICENSE") (Join-Path $Build "bin\age-LICENSE.txt")

    uv run pyinstaller --noconfirm --clean `
        --distpath $Dist `
        --workpath (Join-Path $Build "pyinstaller") `
        (Join-Path $Here "FacturadorE.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller falló" }

    $Zip = Join-Path $Dist "FacturadorE-windows-$Version.zip"
    Compress-Archive -Path (Join-Path $Dist "FacturadorE") -DestinationPath $Zip -Force
    Write-Host "Listo: $Zip"
}
finally {
    Pop-Location
}
