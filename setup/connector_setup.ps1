# Vault search plan, phase 3 (2026-10-11): turn on the Vault connector and move the private pages to port 8443.
# Run from your own PowerShell on this PC (not Admin), in the daytime (it refuses 23:30-06:30, when the overnight
# jobs run):
#   cd '<app folder>\setup'
#   Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process -Force
#   Unblock-File .\connector_setup.ps1
#   .\connector_setup.ps1
# What it does:
#   1. Checks the runtime, the MCP library and the connector token (mcp_token.txt; never shown).
#   2. Registers and starts the \VaultLibrarian\Connector task (service\vault_mcp.py on 127.0.0.1:8767, at logon,
#      restarts itself if it stops) and checks it answers locally with all 7 tools.
#   3. Saves the current Tailscale setup to reports\, then: private pages ("/" Daily Summary, "/vault") on
#      tailnet-only port 8443; public Funnel on 443 serving /mcp only.
#   4. Points the morning email / page links and the Daily Summary links at https://<your-host>.ts.net:8443
#      (backups in setup\_backup-<date>-connector\). Links in emails sent before today stop working.
#   5. Restarts the Server task (new status-page line and "Answered via Claude" on the answer page) and checks
#      the private pages answer on 8443.
#   6. Puts "Bearer <token>" on the clipboard for claude.ai.
# If Tailscale says "Access denied", run this script again from an Admin PowerShell.
# Undo the port move (back to the old layout):
#   & 'C:\Program Files\Tailscale\tailscale.exe' serve reset
#   & 'C:\Program Files\Tailscale\tailscale.exe' serve --bg --https=443 http://127.0.0.1:8765
#   & 'C:\Program Files\Tailscale\tailscale.exe' serve --bg --https=443 --set-path=/vault http://127.0.0.1:8766
#   and copy vault.json.before-links / detail_server.json.before-links back from setup\_backup-<date>-connector\.
#   -BaseUrl   your Tailscale Funnel address; default https://<first config mcp.hosts entry>
param([string]$Runtime, [string]$BaseUrl)
$ErrorActionPreference = 'Stop'
$app  = Split-Path -Parent $PSScriptRoot
$cfg  = Get-Content (Join-Path $app 'config\vault.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $Runtime) { $Runtime = [Environment]::ExpandEnvironmentVariables([string]$cfg.runtime) }
$vpy  = Join-Path $Runtime 'Scripts\python.exe'
$base = if ($BaseUrl) { $BaseUrl.TrimEnd('/') } elseif (@($cfg.mcp.hosts).Count) { 'https://' + @($cfg.mcp.hosts)[0] } else {
  Write-Host 'Set config mcp.hosts to your Tailscale host name (e.g. pc-name.tailnet-name.ts.net), or pass -BaseUrl.'; exit 1 }
$env:PYTHONIOENCODING = 'utf-8'
$now = Get-Date; $mins = $now.Hour * 60 + $now.Minute
if ($mins -ge 1410 -or $mins -lt 390) {
    Write-Host "It's overnight-job time (23:30-06:30). Run this in the daytime."; exit 1
}
if (-not (Test-Path $vpy)) { Write-Host "Runtime not found at $Runtime"; exit 1 }
$ts = 'C:\Program Files\Tailscale\tailscale.exe'
if (-not (Test-Path $ts)) { $c = Get-Command tailscale -ErrorAction SilentlyContinue; if ($c) { $ts = $c.Source } else { Write-Host "tailscale.exe not found"; exit 1 } }
$stamp = Get-Date -Format 'yyyy-MM-dd-HHmm'
$rep = Join-Path $app "reports\connector-setup-$stamp.md"
$log = New-Object System.Collections.Generic.List[string]
$log.Add("# Vault connector setup $stamp"); $log.Add("")
function Say($t) { Write-Host $t; $log.Add("- $t") }
function Save-Report { $log | Set-Content -Path $rep -Encoding UTF8 }

# ---- 1. checks
& $vpy -c "import importlib.metadata as m; print('mcp', m.version('mcp'))"
if ($LASTEXITCODE -ne 0) { Say "The MCP library is missing from the runtime (it was installed in phase 0). Stopping."; Save-Report; exit 1 }
$tf = Join-Path $env:LOCALAPPDATA 'VaultLibrarian\credentials\mcp_token.txt'
if (-not (Test-Path $tf)) { Say "No connector token at $tf (made in phase 0). Stopping."; Save-Report; exit 1 }
Say "Runtime, MCP library and token found."

# ---- 2. Connector task
& (Join-Path $PSScriptRoot 'register_tasks.ps1') -Only Connector
Stop-ScheduledTask -TaskPath '\VaultLibrarian\' -TaskName Connector -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskPath '\VaultLibrarian\' -TaskName Connector
Start-Sleep -Seconds 8
$probe = & $vpy (Join-Path $PSScriptRoot 'connector_setup.py') probe 2>&1 | Out-String
Say ("Local connector check: " + $probe.Trim())
if ($probe -notmatch '^OK') {
    Say "The connector didn't answer locally, so the ports were NOT changed. Tell Claude; logs: reports\connector-log-*.md."
    Save-Report; exit 1
}

# ---- 3. ports
$before = Join-Path $app "reports\tailscale-before-connector-$stamp.json"
& $ts serve status --json | Set-Content -Path $before -Encoding UTF8
Say "Saved the current Tailscale setup to reports\$(Split-Path $before -Leaf)."
& $ts serve reset
if ($LASTEXITCODE -ne 0) { Say "tailscale serve reset failed (try an Admin PowerShell). Nothing else was changed."; Save-Report; exit 1 }
$ok = $true
& $ts serve --bg --https=8443 http://127.0.0.1:8765;                   if ($LASTEXITCODE -ne 0) { $ok = $false }
& $ts serve --bg --https=8443 --set-path=/vault http://127.0.0.1:8766; if ($LASTEXITCODE -ne 0) { $ok = $false }
& $ts funnel --bg --https=443 --set-path=/mcp http://127.0.0.1:8767/mcp; if ($LASTEXITCODE -ne 0) { $ok = $false }
$st = (& $ts serve status 2>&1 | Out-String).Trim()
$log.Add(""); $log.Add('```'); $log.Add($st); $log.Add('```'); $log.Add("")
Write-Host $st
if (-not $ok) { Say "A Tailscale step failed (see above). Undo commands are at the top of this script. Tell Claude."; Save-Report; exit 1 }
Say "Ports: private pages on $($base):8443 (tailnet only); $base/mcp public (Funnel)."

# ---- 4. links
$lk = & $vpy (Join-Path $PSScriptRoot 'connector_setup.py') links "$($base):8443" 2>&1 | Out-String
Say ("Links: " + $lk.Trim())

# ---- 5. restart the web pages and check
Stop-ScheduledTask -TaskPath '\VaultLibrarian\' -TaskName Server -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskPath '\VaultLibrarian\' -TaskName Server
Start-Sleep -Seconds 8
foreach ($u in @("$($base):8443/vault/status", "$base/mcp")) {
    try {
        $r = Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 20
        Say "$u -> HTTP $($r.StatusCode)"
    } catch {
        $code = $null; try { $code = [int]$_.Exception.Response.StatusCode } catch {}
        if ($code) { Say "$u -> HTTP $code" } else { Say "$u -> no answer ($($_.Exception.Message))" }
    }
}
Say "Expected: /vault/status 200; /mcp 401 (no token sent, so refused: that is correct)."

# ---- 6. claude.ai
$tok = (Get-Content $tf -Raw).Trim()
Set-Clipboard -Value ("Bearer " + $tok)
Save-Report
Write-Host ""
Write-Host "Done. Report: reports\$(Split-Path $rep -Leaf)"
Write-Host "'Bearer <token>' is on the clipboard. In claude.ai: Settings > Connectors > Add custom connector"
Write-Host "   Name:  Vault"
Write-Host "   URL:   $base/mcp"
Write-Host "   Authentication: none (No sign-in); Request header:  authorization  =  (paste)"
Write-Host "Then set vault_search, vault_get and vault_status to 'Allow always'; leave the other four on 'Ask'."
Write-Host "Keep the old 'Vault ping' entry until the new one works; then remove it."
