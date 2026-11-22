# Installs Vault Librarian on this PC, or repairs an install after the folder was moved. Safe to run again:
# each step checks what is already there and skips it.
#
# Run from your own PowerShell (not as Admin, and not from a Claude session: the Claude app's sandbox redirects
# writes under AppData\Local, so a runtime or credentials made from Claude would be invisible to Task Scheduler):
#   Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process -Force
#   & "<this folder>\install.ps1"
#
# Steps
#   1. Checks Windows, Python 3.11+, the Claude CLI, and optional tools (Tesseract, Calibre).
#   2. Creates config\vault.json from config\vault.example.json by asking a few questions (setup\make_config.py).
#   3. Builds the Python runtime (setup\rebuild_runtime.ps1), with the optional packages for the features you chose.
#   4. Sign-ins and keys for the features that are on: Claude token (always), Gemini key and YouTube sign-in,
#      connector key. Graph mail credentials are checked, not created (see docs\CONFIGURATION.md).
#   5. Offers the first build of the search index.
#   6. Registers the scheduled tasks for the features that are on (setup\register_tasks.ps1 -Enabled).
#
#   -SkipTasks      do everything except registering scheduled tasks
#   -CheckOnly      only step 1, plus a list of what is set up and what is missing
param([switch]$SkipTasks, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$app = Split-Path -Parent $PSScriptRoot
$cfgFile = Join-Path $app 'config\vault.json'

function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "   ok      $t" -ForegroundColor Green }
function Warn($t) { Write-Host "   missing $t" -ForegroundColor Yellow }
function Exp($v)  { [Environment]::ExpandEnvironmentVariables([string]$v) }
function On($block) { -not ($block -and ($block.enabled -eq $false)) }

# ---------------------------------------------------------------- 1. prerequisites
Step '1. Prerequisites'
if ($env:OS -ne 'Windows_NT') { throw 'Vault Librarian runs on Windows (Task Scheduler, DPAPI).' }
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw 'Python not found. Install Python 3.12 from python.org (tick "Add python.exe to PATH") and run this again.' }
$ver = & $py.Source -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$ver -lt [version]'3.11') { throw "Python $ver found; 3.11 or newer is needed." }
Ok "Python $ver"
$claude = Get-Command claude -ErrorAction SilentlyContinue
if ($claude) { Ok 'Claude CLI' } else { Warn 'Claude CLI: install Claude Code (https://claude.com/claude-code); every model call goes through it' }
$tess = Test-Path (Join-Path $env:ProgramFiles 'Tesseract-OCR\tesseract.exe')
if ($tess) { Ok 'Tesseract (OCR for scanned PDFs)' } else { Warn 'Tesseract (optional, scanned PDFs): setup\install_file_tools.ps1' }
$cal = (Test-Path (Join-Path $env:ProgramFiles 'Calibre2\ebook-convert.exe')) -or (Test-Path (Join-Path ${env:ProgramFiles(x86)} 'Calibre2\ebook-convert.exe'))
if ($cal) { Ok 'Calibre (e-books)' } else { Warn 'Calibre (optional, .epub/.mobi): setup\install_file_tools.ps1' }

if ($CheckOnly) {
  Step 'Set up so far'
  if (Test-Path $cfgFile) { Ok 'config\vault.json' } else { Warn 'config\vault.json (step 2)'; return }
  $cfg = Get-Content $cfgFile -Raw -Encoding UTF8 | ConvertFrom-Json
  $cred = Join-Path (Exp $cfg.local_root) 'credentials'
  if (Test-Path (Join-Path (Exp $cfg.runtime) 'Scripts\python.exe')) { Ok 'runtime' } else { Warn 'runtime (step 3)' }
  if (Test-Path "$cred\claude_oauth_token.dpapi") { Ok 'Claude token' } else { Warn 'Claude token (step 4)' }
  if ((On $cfg.mail) -and $cfg.mailbox) {
    if (Test-Path (Exp $cfg.graph_credentials)) { Ok 'Graph mail credentials' } else { Warn "Graph mail credentials at $(Exp $cfg.graph_credentials)" } }
  if ($cfg.remember.enabled -eq $true) {
    if (Test-Path "$cred\gemini_api_key.txt") { Ok 'Gemini key' } else { Warn 'Gemini key' }
    if (Test-Path "$cred\youtube_token.dpapi") { Ok 'YouTube sign-in' } else { Warn 'YouTube sign-in' } }
  if ($cfg.mcp.enabled -eq $true) { if (Test-Path "$cred\mcp_token.txt") { Ok 'connector key' } else { Warn 'connector key' } }
  return
}

# ---------------------------------------------------------------- 2. settings
Step '2. Settings (config\vault.json)'
if (Test-Path $cfgFile) { Ok 'config\vault.json exists; kept as it is' }
else { & $py.Source (Join-Path $PSScriptRoot 'make_config.py'); if ($LASTEXITCODE) { throw 'settings not written' } }
$cfg = Get-Content $cfgFile -Raw -Encoding UTF8 | ConvertFrom-Json
$runtime = Exp $cfg.runtime
$cred = Join-Path (Exp $cfg.local_root) 'credentials'
$vpy = Join-Path $runtime 'Scripts\python.exe'

# ---------------------------------------------------------------- 3. runtime
Step "3. Python runtime ($runtime)"
& (Join-Path $PSScriptRoot 'rebuild_runtime.ps1')
if (-not (Test-Path $vpy)) { throw "runtime was not built at $runtime" }
New-Item -ItemType Directory -Force $cred | Out-Null

# ---------------------------------------------------------------- 4. sign-ins and keys (never shown, never logged)
Step '4. Sign-ins and keys'
if (Test-Path "$cred\claude_oauth_token.dpapi") { Ok 'Claude token' }
elseif ($claude) {
  Write-Host '   A browser opens for the Claude sign-in; then paste the token it prints (it starts sk-ant-oat01-).'
  & claude setup-token
  Read-Host '   Paste the token' -AsSecureString | ConvertFrom-SecureString | Set-Content -Encoding ascii "$cred\claude_oauth_token.dpapi"
  & $vpy (Join-Path $app 'jobs\claude_auth.py') status
} else { Warn 'Claude token: install the Claude CLI, then run this again' }

if ((On $cfg.mail) -and $cfg.mailbox) {
  if (Test-Path (Exp $cfg.graph_credentials)) { Ok 'Graph mail credentials' }
  else { Warn "Graph mail credentials: save tenantId, clientId and clientSecret as JSON at $(Exp $cfg.graph_credentials) (docs\CONFIGURATION.md, Mail)" }
}
if ($cfg.remember.enabled -eq $true) {
  if (Test-Path "$cred\gemini_api_key.txt") { Ok 'Gemini key' }
  else {
    $s = Read-Host '   Paste your Gemini API key (aistudio.google.com/api-keys)' -AsSecureString
    $k = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
    [IO.File]::WriteAllText("$cred\gemini_api_key.txt", $k, (New-Object Text.UTF8Encoding $false)); Remove-Variable k, s
    Ok 'Gemini key saved'
  }
  if (Test-Path "$cred\youtube_token.dpapi") { Ok 'YouTube sign-in' }
  elseif (Test-Path "$cred\youtube_client_secret.json") { & $vpy (Join-Path $app 'setup\remember_auth.py') }
  else { Warn "YouTube: save your Google Cloud desktop client as $cred\youtube_client_secret.json, then run this again" }
}
if ($cfg.mcp.enabled -eq $true) {
  if (Test-Path "$cred\mcp_token.txt") { Ok 'connector key' }
  else { & $vpy -c "import secrets,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(secrets.token_urlsafe(32))" "$cred\mcp_token.txt"; Ok 'connector key made' }
}

# ---------------------------------------------------------------- 5. search index
if ((On $cfg.search) -and -not (Test-Path (Join-Path (Exp $cfg.local_root) 'index\search.db'))) {
  Step '5. Search index'
  if ((Read-Host '   Build the search index now? It reads the whole vault at low priority and can take an hour (y/n)') -like 'y*') {
    & (Join-Path $PSScriptRoot 'build_search_index.ps1')
  } else { Write-Host '   Later: setup\build_search_index.ps1 (Nightly also catches up a little each night).' }
}

# ---------------------------------------------------------------- 6. scheduled tasks
if ($SkipTasks) { Step '6. Scheduled tasks skipped (-SkipTasks)' }
else {
  Step '6. Scheduled tasks'
  & (Join-Path $PSScriptRoot 'register_tasks.ps1') -Enabled
  Start-ScheduledTask -TaskPath $(if ($cfg.schedule.task_path) { $cfg.schedule.task_path } else { '\' + (Split-Path -Leaf (Exp $cfg.local_root)) + '\' }) -TaskName Server
}

Step 'Done'
$tok = & $vpy -c "import sys; sys.path.insert(0, sys.argv[1]); import common as C, home_data as H; print(H.home_token(C.load_config()))" (Join-Path $app 'jobs')
Write-Host "   Home page (bookmark it; the link is your key): http://127.0.0.1:8766/vault/home?t=$tok"
Write-Host '   Check what is set up at any time: setup\install.ps1 -CheckOnly'
if ($cfg.nightly.dry_run -ne $false) { Write-Host '   Nightly is a dry run until nightly.dry_run is false in config\vault.json.' }
