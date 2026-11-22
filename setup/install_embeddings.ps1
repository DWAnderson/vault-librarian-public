# Phase B (2026-10-06): installs the local embedding model used for "related" suggestions.
# Run from your own PowerShell on this PC (package installs into the app runtime can't be done from a Claude session).
param([string]$Runtime = (Join-Path $env:LOCALAPPDATA 'Programs\VaultLibrarian\runtime'))
$ErrorActionPreference = 'Stop'
$app = Split-Path -Parent $PSScriptRoot
$vpy = Join-Path $Runtime 'Scripts\python.exe'
if (-not (Test-Path $vpy)) { Write-Host "Runtime not found at $Runtime - run setup\rebuild_runtime.ps1 first"; exit 1 }
& $vpy -m pip install --quiet fastembed numpy
& $vpy -c "import fastembed, numpy; print('fastembed', fastembed.__version__, 'OK')"
# First index: downloads the model (~130 MB, once) and embeds every source note (a few minutes).
& $vpy (Join-Path $app 'jobs\related.py')
