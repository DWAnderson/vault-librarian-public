# Vault search plan, phase 1 (2026-10-11): first full build of the search index.
# Run from your own PowerShell on this PC (not Admin):
#   cd '<app folder>\setup'
#   Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process -Force
#   Unblock-File .\build_search_index.ps1
#   .\build_search_index.ps1
# Reads the vault only; writes only %LOCALAPPDATA%\VaultLibrarian\index\search.db. About 50-55 minutes at
# below-normal priority. Safe to stop (Ctrl+C) and run again: it resumes where it stopped.
# Will not start while another job holds the vault lock. Writes reports\search-build-<date-time>.md.
param([string]$Runtime = (Join-Path $env:LOCALAPPDATA 'Programs\VaultLibrarian\runtime'))
$ErrorActionPreference = 'Continue'
$app = Split-Path -Parent $PSScriptRoot
$vpy = Join-Path $Runtime 'Scripts\python.exe'
if (-not (Test-Path $vpy)) { Write-Host "Runtime not found at $Runtime"; exit 1 }
$env:PYTHONIOENCODING = 'utf-8'

Write-Host "Building the vault search index at below-normal priority (about 50-55 minutes)."
Write-Host "Progress is shown below; you can keep using the PC meanwhile."
$p = Start-Process -FilePath $vpy -NoNewWindow -PassThru `
     -ArgumentList ('"' + (Join-Path $app 'jobs\vault_search.py') + '"'), 'build'
$null = $p.Handle   # makes ExitCode available after WaitForExit
try { $p.PriorityClass = 'BelowNormal' } catch {}
$p.WaitForExit()
Write-Host ""
if ($p.ExitCode -eq 2) {
    Write-Host "Not started: another Vault Librarian job is using the vault. Try again in a while."
    exit 2
}
if ($p.ExitCode -ne 0) {
    Write-Host "The build stopped with an error (exit $($p.ExitCode)). Run this script again to resume, and tell Claude."
    exit $p.ExitCode
}
$report = Get-ChildItem (Join-Path $app 'reports') -Filter 'search-build-*.md' |
          Sort-Object LastWriteTime -Descending | Select-Object -First 1
Write-Host "Done. Report: $($report.FullName)"
Write-Host "Tell Claude it's ready (it's in the synced reports folder)."
