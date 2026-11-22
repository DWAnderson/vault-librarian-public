# Registers the Vault Librarian scheduled tasks (folder: config schedule.task_path) for the logged-on user
# ("Run only when user is logged on": no stored password). Re-run safely; existing tasks are replaced.
#   .\register_tasks.ps1 -Only Nightly,Weekly     the named tasks
#   .\register_tasks.ps1 -Enabled                 every task whose feature is on in config\vault.json (used by install.ps1)
param([string[]]$Only = @('Nightly'), [switch]$Enabled)
$ErrorActionPreference = 'Stop'
$app  = Split-Path -Parent $PSScriptRoot
$user = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
# Run times come from config\vault.json (schedule block, intake hours, queue.night_window; home page plan 2026-11-12)
$cfg = Get-Content (Join-Path $app 'config\vault.json') -Raw -Encoding UTF8 | ConvertFrom-Json
# The runtime and the Task Scheduler folder come from the config too (runtime, schedule.task_path): no app name here
$runtime = [Environment]::ExpandEnvironmentVariables([string]$cfg.runtime)
$pyw  = Join-Path $runtime 'Scripts\pythonw.exe'
if (-not (Test-Path $pyw)) { throw "runtime not found at $pyw (run setup\install.ps1 or setup\rebuild_runtime.ps1 from your own Terminal)" }
$taskPath = if ($cfg.schedule.task_path) { [string]$cfg.schedule.task_path } else {
  '\' + (Split-Path -Leaf ([Environment]::ExpandEnvironmentVariables([string]$cfg.local_root))) + '\' }
$sch = $cfg.schedule
function At([string]$v, [string]$fallback) { if ($v) { $v } else { $fallback } }
$wk = (At $sch.Weekly 'Sun 03:30').Split(' ')
$wkDay = @{Mon='Monday';Tue='Tuesday';Wed='Wednesday';Thu='Thursday';Fri='Friday';Sat='Saturday';Sun='Sunday'}[$wk[0]]
$inFirst = [int](At $cfg.intake.first_hour 7); $inLast = [int](At $cfg.intake.last_hour 23)
$inAt = '{0:D2}:00' -f $inFirst
$nightAt = At $cfg.queue.night_window[0] '23:45'
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew `
             -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$defs = @{
  'Nightly' = @{ script = 'jobs\nightly.py'; trigger = (New-ScheduledTaskTrigger -Daily -At (At $sch.Nightly '02:30'));
                 desc = 'Vault Librarian: snapshot, citation clean-up, duplicates, indexes/hubs, OKF lint, report' }
  'Weekly'  = @{ script = 'jobs\weekly.py'; trigger = (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $wkDay -At $wk[1]);
                 desc = 'Vault Librarian: weekly citation backfill (40), archived-duplicate list, full lint, snapshot check, pruning' }
  'Organize'= @{ script = 'jobs\organize.py'; args = '--prep'; trigger = (New-ScheduledTaskTrigger -Daily -At (At $sch.Organize '06:00'));
                 desc = 'Vault Librarian: 06:00 filing proposal for unfiled Inbox/Clippings notes, emailed; applied when you reply' }
  'Server'  = @{ script = 'service\vault_server.py'; trigger = (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"); server = $true;
                 desc = 'Vault Librarian service on 127.0.0.1:8766 (save links from the phone, status); Tailscale Serve publishes /vault' }
  'Connector' = @{ script = 'service\vault_mcp.py'; trigger = (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"); server = $true;
                 desc = 'Vault connector (MCP) on 127.0.0.1:8767 for claude.ai; Tailscale Funnel publishes https://<your host>/mcp on 443 (plan: 2026-10-08 Vault search, phase 3)' }
  'Originals' = @{ script = 'jobs\originals.py'; args = 'run'; trigger = (New-ScheduledTaskTrigger -Daily -At (At $sch.Originals '02:45')); hours = 3;
                 desc = 'Vault Librarian: text of embedded originals (books, then PDF articles, then manuals) into the search index, in leftover night time after the queue, or on request; no Claude usage' }
  'YouTubeRemember' = @{ script = 'jobs\remember.py'; trigger = (New-ScheduledTaskTrigger -Daily -At (At $sch.YouTubeRemember '02:05')); hours = 4;
                 desc = 'Vault Librarian: YouTube Remember playlist -> detailed Video Summary + Transcript notes (Gemini project Obsidian Youtube Summary); dry run until remember.dry_run = false' }
  'Intake'  = @{ script = 'jobs\intake.py'; trigger = $(
                   $t = New-ScheduledTaskTrigger -Daily -At $inAt
                   $t.Repetition = (New-ScheduledTaskTrigger -Once -At $inAt -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Hours ($inLast - $inFirst))).Repetition
                   $t);
                 desc = 'Vault Librarian: Remember-folder intake every 30 min, 07:00-23:00 (dry run until intake.enabled = true)' }
  'QueueNight' = @{ script = 'jobs\queue_run.py'; args = '--live --routes costly --no-collect'; trigger = (New-ScheduledTaskTrigger -Daily -At $nightAt);
                 desc = 'Vault Librarian: books and long summaries from the intake queue, 23:45-01:55, paced by Claude plan usage (config pacing; 2026-09-29)' }
  'QueueWindow' = @{ script = 'jobs\queue_run.py'; args = '--live --window --no-collect'; hours = 5; trigger = $(
                   $t = New-ScheduledTaskTrigger -Daily -At 12:00am
                   $t.Repetition = (New-ScheduledTaskTrigger -Once -At 12:00am -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Hours 24)).Repetition
                   $t);
                 desc = 'Vault Librarian: every 15 min; works the queue only inside an extra run window you booked (usage_plan; 2026-10-06), else exits at once' }
  'SignInCheck' = @{ script = 'jobs\claude_auth.py'; args = 'check'; trigger = (New-ScheduledTaskTrigger -Daily -At (At $sch.SignInCheck '21:00'));
                 desc = 'Vault Librarian: 21:00 one tiny Haiku call to test the Claude sign-in before the night jobs; emails you the fix if it is broken (2026-10-24)' }
  'QueueNow'   = @{ script = 'jobs\queue_run.py'; args = '--live --routes costly --no-collect --force --max 1'; trigger = $null; hours = 3;
                 desc = 'Vault Librarian: on demand only - one book or long summary now, outside the night window (still counts toward books_per_night)' }
}
function On($block) { -not ($block -and ($block.enabled -eq $false)) }   # block missing, or enabled not false
if ($Enabled) {
  $Only = @('Nightly', 'Weekly', 'Organize', 'Server', 'Intake', 'SignInCheck', 'QueueNow')
  if (On $cfg.queue)     { $Only += 'QueueNight', 'QueueWindow' }
  if (On $cfg.originals) { $Only += 'Originals' }
  if ($cfg.mcp -and $cfg.mcp.enabled -eq $true)           { $Only += 'Connector' }         # off unless switched on
  if ($cfg.remember -and $cfg.remember.enabled -eq $true) { $Only += 'YouTubeRemember' }   # off unless switched on
}
foreach ($name in $Only) {
  $d = $defs[$name]; if (-not $d) { throw "unknown task $name" }
  $argline = '"' + (Join-Path $app $d.script) + '"' + $(if ($d.args) { ' ' + $d.args } else { '' })
  $action = New-ScheduledTaskAction -Execute $pyw -Argument $argline -WorkingDirectory (Split-Path (Join-Path $app $d.script))
  $set = $settings
  if ($d.hours) {
    $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew `
           -ExecutionTimeLimit (New-TimeSpan -Hours $d.hours) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  }
  if ($d.server) {
    $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
           -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  }
  $reg = @{ TaskPath = $taskPath; TaskName = $name; Action = $action; Principal = $principal; Settings = $set
            Description = $d.desc; Force = $true }
  if ($d.trigger) {   # QueueNow has no trigger: started by hand
    # New-ScheduledTaskTrigger stores the time with today's UTC offset, so the task drifts an hour when daylight
    # saving changes (2026-11-01: QueueNight ran at 22:45, outside its window). Plain local time instead.
    foreach ($t in @($d.trigger)) { if ($t.StartBoundary) { $t.StartBoundary = ([DateTimeOffset]$t.StartBoundary).LocalDateTime.ToString('s') } }
    $reg.Trigger = $d.trigger
  }
  Register-ScheduledTask @reg | Out-Null
  $t = Get-ScheduledTask -TaskPath $taskPath -TaskName $name
  "$name registered: state=$($t.State) next=$((Get-ScheduledTaskInfo $t).NextRunTime)"
}
