param(
    [Parameter(Mandatory=$true)][string]$Server,
    [Parameter(Mandatory=$true)][string]$Model,
    [Parameter(Mandatory=$true)][string]$Mmproj,
    [ValidateRange(0,999)][int]$GpuLayers = 99,
    [ValidateRange(512,131072)][int]$Context = 4096,
    [ValidateRange(1024,65535)][int]$Port = 8080,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
foreach ($file in @($Server, $Model, $Mmproj)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Local file not found: $file" }
}
$serverPath = (Resolve-Path -LiteralPath $Server).Path
$modelPath = (Resolve-Path -LiteralPath $Model).Path
$projectorPath = (Resolve-Path -LiteralPath $Mmproj).Path
$serverArgs = @('-m', $modelPath, '--mmproj', $projectorPath, '--host', '127.0.0.1', '--port', "$Port", '--reasoning', 'off', '-ngl', "$GpuLayers", '-c', "$Context")
Write-Host 'Starting only existing local files; no -hf or download options are used.'
if ($CheckOnly) {
    [pscustomobject]@{ executable=$serverPath; arguments=$serverArgs } | ConvertTo-Json -Depth 3
    exit 0
}
& $serverPath @serverArgs
if ($LASTEXITCODE -ne 0) { throw "llama-server exited with code $LASTEXITCODE. Review model/sidecar compatibility and available GPU memory." }
