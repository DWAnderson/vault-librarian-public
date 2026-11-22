# Builds the app's Python runtime (per-user program, Windows convention). Locations come from config\vault.json:
#   runtime (program):  config "runtime"     (e.g. %LOCALAPPDATA%\Programs\<app>\runtime)
#   data:               config "local_root"  (e.g. %LOCALAPPDATA%\<app>\{logs,work,snapshots,queue})
# Installs requirements.txt, plus the optional groups in requirements-optional.txt whose feature is on
# (or every group with -AllOptional). Safe to run again.
# Run from your own Terminal, not from a Claude session: the Claude app's sandbox redirects
# writes under AppData\Local, so a runtime built from Claude would be invisible to Task Scheduler.
param([string]$Runtime, [string]$Data, [switch]$AllOptional)
$ErrorActionPreference = 'Continue'  # python writes harmless warnings to stderr
$app = Split-Path -Parent $PSScriptRoot
$cfgFile = Join-Path $app 'config\vault.json'
if (-not (Test-Path $cfgFile)) { throw "config\vault.json not found: run setup\install.ps1 first (it creates it from config\vault.example.json)" }
$cfg = Get-Content $cfgFile -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $Runtime) { $Runtime = [Environment]::ExpandEnvironmentVariables([string]$cfg.runtime) }
if (-not $Data)    { $Data    = [Environment]::ExpandEnvironmentVariables([string]$cfg.local_root) }

$py = (Get-Command python -ErrorAction Stop).Source
New-Item -ItemType Directory -Force -Path (Split-Path $Runtime) | Out-Null
foreach ($d in 'logs','work','snapshots','queue','credentials') { New-Item -ItemType Directory -Force -Path (Join-Path $Data $d) | Out-Null }
if (-not (Test-Path (Join-Path $Runtime 'Scripts\python.exe'))) { & $py -m venv $Runtime }
$vpy = Join-Path $Runtime 'Scripts\python.exe'
& $vpy -m pip install --upgrade pip --quiet
& $vpy -m pip install --quiet -r (Join-Path $app 'requirements.txt')

# Optional groups: "# [name]" sections of requirements-optional.txt
$groups = @{}; $cur = $null
foreach ($line in Get-Content (Join-Path $app 'requirements-optional.txt')) {
  if ($line -match '^#\s*\[(\w+)\]') { $cur = $Matches[1]; $groups[$cur] = @() }
  elseif ($cur -and $line.Trim() -and -not $line.Trim().StartsWith('#')) { $groups[$cur] += $line.Trim() }
}
function On($block) { -not ($block -and ($block.enabled -eq $false)) }
$want = @{
  search    = (On $cfg.search) -or (On $cfg.related)
  connector = [bool]($cfg.mcp -and $cfg.mcp.enabled -eq $true)
  youtube   = [bool]($cfg.remember -and $cfg.remember.enabled -eq $true)
  browser   = $false
}
foreach ($g in $groups.Keys) {
  if ($AllOptional -or $want[$g]) {
    Write-Host "optional packages: $g"
    & $vpy -m pip install --quiet @($groups[$g])
    if ($g -eq 'browser') { & $vpy -m playwright install chromium }
  }
}
& $vpy -c "import yaml, requests, lxml, trafilatura, extruct, pypdf, tzdata, html2text, flask, waitress; print('runtime OK')"
