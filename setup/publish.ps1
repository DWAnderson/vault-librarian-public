# Run on the LAPTOP after testing a skill change: copies the skill scripts from the laptop's
# source folders into this app's lib\ (which this PC runs), and writes lib\VERSION.txt.
#   source: %USERPROFILE%\OneDrive\Documents\Claude Cowork\Obsidian\{vault-librarian,url-to-vault}\scripts
#   target: <this app folder>\lib\<skill>\scripts   (on the laptop: ...\OneDrive\<laptop's synced copy>\Custom Apps\Obsidian)
# Safety: a lib file that differs from the source AND is newer (fixed on this PC) is NOT overwritten;
# it is listed so it can be back-ported first. -BackPort copies those this PC fixes into the source;
# -Force overwrites anyway. Output: reports\publish-<stamp>.log
param([switch]$BackPort, [switch]$Force,
      [string]$Source = (Join-Path $env:USERPROFILE 'OneDrive\Documents\Claude Cowork\Obsidian'))
$ErrorActionPreference = 'Stop'
$app = Split-Path -Parent $PSScriptRoot
$stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$log = Join-Path $app "reports\publish-$stamp.log"
function Say($m) { Write-Host $m; Add-Content -Path $log -Value $m }
$held = @(); $copied = 0; $back = 0
foreach ($skill in 'vault-librarian','url-to-vault') {
  $src = Join-Path $Source "$skill\scripts"; $dst = Join-Path $app "lib\$skill\scripts"
  if (-not (Test-Path $src)) { Say "missing source $src"; continue }
  New-Item -ItemType Directory -Force $dst | Out-Null
  foreach ($f in Get-ChildItem $src -Filter *.py -File) {
    $t = Join-Path $dst $f.Name
    if ((Test-Path $t) -and ((Get-FileHash $t).Hash -eq (Get-FileHash $f.FullName).Hash)) { continue }
    if ((Test-Path $t) -and ((Get-Item $t).LastWriteTime -gt $f.LastWriteTime) -and -not $Force) {
      if ($BackPort) { Copy-Item $t $f.FullName -Force; $back++; Say "back-ported app fix -> source: $skill\$($f.Name)" }
      else { $held += "$skill\$($f.Name)" }
      continue
    }
    Copy-Item $f.FullName $t -Force; $copied++; Say "published $skill\$($f.Name)"
  }
  # scripts that exist only in lib (written on this PC, e.g. file_to_note.py 2026-09-28): -BackPort copies them to the source
  foreach ($g in Get-ChildItem $dst -Filter *.py -File) {
    if (-not (Test-Path (Join-Path $src $g.Name))) {
      if ($BackPort) { Copy-Item $g.FullName (Join-Path $src $g.Name); $back++; Say "back-ported new app script -> source: $skill\$($g.Name)" }
      else { $held += "$skill\$($g.Name) (only in the app)" }
    }
  }
}
$lines = @("Vault Librarian lib, published $(Get-Date -Format s) from the skill source folders", '')   # no local path: this file is committed
Get-ChildItem (Join-Path $app 'lib') -Recurse -Filter *.py | Sort-Object FullName | ForEach-Object { $lines += "$((Get-FileHash $_.FullName -Algorithm MD5).Hash.ToLower())  $($_.FullName.Substring($app.Length + 5))" }
Set-Content -Path (Join-Path $app 'lib\VERSION.txt') -Value $lines -Encoding UTF8
Say "published $copied file(s); back-ported $back"
if ($held) { Say ("NOT published (the app's copy is newer; run with -BackPort first): " + ($held -join ', ')) }
