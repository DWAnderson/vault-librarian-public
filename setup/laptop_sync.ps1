# Run on a PC that uses the vault-librarian skill (the laptop), from its synced copy of this app folder:
#   1. sets the VAULT_OWNER_* user environment variables from config\vault.json "owner", so the skill
#      scripts there write the same owner id and note title as the app;
#   2. back-ports the lib\ changes made in the app into the skill source (publish.ps1 -BackPort), then
#      publishes, which should leave nothing held.
# It shows what it would do and asks before changing anything. The skill source is backed up first.
#   .\laptop_sync.ps1                 (from your own PowerShell, not as Admin)
#   .\laptop_sync.ps1 -Source <skill source folder>   if it isn't in the default place
param([string]$Source = (Join-Path $env:USERPROFILE 'OneDrive\Documents\Claude Cowork\Obsidian'))
$ErrorActionPreference = 'Stop'
$app = Split-Path -Parent $PSScriptRoot

# ---------------------------------------------------------------- 0. is the synced copy up to date?
$cfgFile = Join-Path $app 'config\vault.json'
if (-not (Test-Path $cfgFile)) { throw "config\vault.json not found in $app (has OneDrive synced it?)" }
$vl = Join-Path $app 'lib\vault-librarian\scripts\vaultlib.py'
if (-not (Select-String -Path $vl -Pattern 'def owner\(' -Quiet)) {
  throw 'This copy of the app folder is older than the owner-settings change: wait for OneDrive to sync, then run this again.' }
if (-not (Test-Path $Source)) { throw "Skill source not found: $Source (pass -Source)" }
$cfg = Get-Content $cfgFile -Raw -Encoding UTF8 | ConvertFrom-Json
$o = $cfg.owner
if (-not $o -or -not $o.name) { throw 'config\vault.json has no owner.name; set the owner block first.' }
$want = [ordered]@{ VAULT_OWNER_NAME = [string]$o.name; VAULT_OWNER_ID = [string]$o.id; VAULT_OWNER_NOTE_LABEL = [string]$o.note_label }
if (-not $want.VAULT_OWNER_ID) { $want.VAULT_OWNER_ID = 'human:' + ($o.name.ToLower() -replace '[^a-z0-9]', '') }
if (-not $want.VAULT_OWNER_NOTE_LABEL) { $want.VAULT_OWNER_NOTE_LABEL = "$($o.name)'s note" }

# ---------------------------------------------------------------- preview
Write-Host "`n== 1. Environment variables (user level)" -ForegroundColor Cyan
foreach ($k in $want.Keys) {
  $now = [Environment]::GetEnvironmentVariable($k, 'User')
  Write-Host ("   {0,-24} {1}" -f $k, $(if ($now -eq $want[$k]) { "already $now" } else { "'$now' -> '$($want[$k])'" }))
}
Write-Host "`n== 2. Skill scripts: $Source" -ForegroundColor Cyan
$back = @(); $pub = @(); $risky = @()
foreach ($skill in 'vault-librarian', 'url-to-vault') {
  $src = Join-Path $Source "$skill\scripts"; $dst = Join-Path $app "lib\$skill\scripts"
  foreach ($f in Get-ChildItem $dst -Filter *.py -File) {
    $s = Join-Path $src $f.Name
    if (-not (Test-Path $s)) { $back += "$skill\$($f.Name) (new)"; continue }
    if ((Get-FileHash $s).Hash -eq (Get-FileHash $f.FullName).Hash) { continue }
    if ($f.LastWriteTime -gt (Get-Item $s).LastWriteTime) { $back += "$skill\$($f.Name)" }
    else {
      $pub += "$skill\$($f.Name)"
      # publishing would replace the app's copy; flag it when the app's copy has the owner change
      if (Select-String -Path $f.FullName -Pattern 'VAULT_OWNER|owner\(\)' -Quiet) { $risky += "$skill\$($f.Name)" }
    }
  }
  foreach ($s in Get-ChildItem $src -Filter *.py -File) {
    if (-not (Test-Path (Join-Path $dst $s.Name))) { $pub += "$skill\$($s.Name) (new in source)" } }
}
Write-Host "   back-port (app -> skill source): $($back.Count)"; $back | ForEach-Object { Write-Host "     $_" }
Write-Host "   publish (skill source -> app):   $($pub.Count)"; $pub | ForEach-Object { Write-Host "     $_" }
if ($risky) {
  Write-Host "`nSTOP: these skill-source files are newer than the app's copies, which have the owner change:" -ForegroundColor Red
  $risky | ForEach-Object { Write-Host "     $_" -ForegroundColor Red }
  Write-Host 'Publishing would undo that change. Merge them by hand (or ask Claude), then run this again.'
  return
}
if ((Read-Host "`nGo ahead? (y/n)") -notlike 'y*') { Write-Host 'Nothing changed.'; return }

# ---------------------------------------------------------------- 1. environment variables
foreach ($k in $want.Keys) {
  [Environment]::SetEnvironmentVariable($k, $want[$k], 'User')
  Set-Item -Path "Env:$k" -Value $want[$k]   # this window too
}
Write-Host '   variables set' -ForegroundColor Green

# ---------------------------------------------------------------- 2. back-up, back-port, publish
$bk = Join-Path $Source ('_backup-' + (Get-Date -Format 'yyyyMMdd-HHmm'))
foreach ($skill in 'vault-librarian', 'url-to-vault') {
  $src = Join-Path $Source "$skill\scripts"
  New-Item -ItemType Directory -Force (Join-Path $bk $skill) | Out-Null
  Copy-Item (Join-Path $src '*.py') (Join-Path $bk $skill)
}
Write-Host "   skill source backed up to $bk" -ForegroundColor Green
& (Join-Path $PSScriptRoot 'publish.ps1') -BackPort -Source $Source
Write-Host "`n== publish again (should hold nothing)" -ForegroundColor Cyan
& (Join-Path $PSScriptRoot 'publish.ps1') -Source $Source

Write-Host "`nDone. Restart the Claude desktop app and any open terminals so they see the new variables." -ForegroundColor Cyan
Write-Host 'publish.ps1 rewrote lib\VERSION.txt; commit that on the PC that holds the repository.'
