# Builds a public copy of this repository: the last commit, without the paths in .public-exclude, as a new
# repository with a single commit (no history, so nothing removed earlier can be recovered from it). Then it runs
# setup\privacy_check.py on the copy. It never pushes; you review the copy and publish it yourself.
#
#   .\make_public.ps1 -Email 12345+you@users.noreply.github.com
#   .\make_public.ps1 -Email ... -OutDir <folder> -Force      replace an earlier copy
#
#   -Email    the commit's author email: use your GitHub "noreply" address (GitHub > Settings > Emails)
#   -Name     the commit's author name (default: git config user.name)
#   -OutDir   where the copy goes (default: <this folder's name>-public next to this folder's parent, in %TEMP%)
param([Parameter(Mandatory = $true)][string]$Email, [string]$Name, [string]$OutDir, [switch]$Force,
      [string]$Message = 'Public release')
$ErrorActionPreference = 'Stop'
$app = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $env:TEMP ((Split-Path -Leaf $app) + '-public') }
if (-not $Name) { $Name = (git -C $app config user.name) }
if ($Email -notmatch 'noreply') { Write-Host "Note: $Email is not a GitHub noreply address; it will be public in the commit." -ForegroundColor Yellow }

$dirty = git -C $app status --porcelain
if ($dirty) { throw 'Commit or stash your changes first: the public copy is built from the last commit only.' }
if (Test-Path $OutDir) {
  if (-not $Force) { throw "$OutDir exists; pass -Force to replace it." }
  Remove-Item -Recurse -Force $OutDir
}
New-Item -ItemType Directory -Force $OutDir | Out-Null

# the last commit, minus the excluded paths
$exclude = Get-Content (Join-Path $app '.public-exclude') | Where-Object { $_.Trim() -and -not $_.TrimStart().StartsWith('#') } |
           ForEach-Object { ":(exclude)$($_.Trim())" }
$zip = Join-Path $env:TEMP ("public-" + [guid]::NewGuid().ToString('N') + '.zip')
git -C $app archive --format=zip -o $zip HEAD -- . @exclude
if ($LASTEXITCODE) { throw 'git archive failed' }
Expand-Archive -Path $zip -DestinationPath $OutDir
Remove-Item $zip

# a new repository with one commit
git -C $OutDir init -q -b main
git -C $OutDir add -A
git -C $OutDir -c "user.name=$Name" -c "user.email=$Email" commit -q -m $Message
$files = (git -C $OutDir ls-files | Measure-Object).Count
Write-Host "Public copy: $OutDir ($files files, 1 commit)"

# what personal terms are still in it (terms from this app's settings; never printed)
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if ($py) { & $py (Join-Path $PSScriptRoot 'privacy_check.py') --root $OutDir --brief }
Write-Host ''
Write-Host 'Review the copy, then publish it, for example:'
Write-Host "  gh repo create <name> --public --source `"$OutDir`" --push"
