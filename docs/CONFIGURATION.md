# Configuration

All settings are in `config/vault.json`, which `setup/install.ps1` creates from `config/vault.example.json`.
The real file is git-ignored: it holds your addresses and decisions. Values can use `%LOCALAPPDATA%`-style
variables. Keys starting with `_` are comments. Many settings can also be changed on the home page's Settings
tab, which writes this file and keeps an undo history.

After changing a feature switch, run `setup\install.ps1` again. It installs the packages the feature needs,
asks for any missing sign-in, and registers or skips the feature's scheduled task.

## Main settings

| Key | Meaning |
|---|---|
| `vault_path` | the vault folder (the one that contains `.obsidian`) |
| `owner.name` | your first name, as Claude's instructions and the app's notes name you (empty: "the owner") |
| `owner.id` | `generated.by` of your own notes, such as `human:sam`. Notes with it count as yours in search and analysis |
| `owner.about` | one optional sentence for Claude, such as "Sam is a historian." |
| `owner.note_label` | title of the callout holding your note on an item (default "Sam's note", or "My note") |
| `pc_name` | the PC's name in messages and reports (empty: "this PC") |
| `local_root` | data folder on this PC: logs, work files, snapshots, search index, credentials |
| `runtime` | the app's Python venv |
| `timezone` | IANA time zone, used for run times and report dates |
| `queue.dropbox_path` | folder you drop files into; its subfolders choose the handling (`queue.route_folders`) |
| `schedule` | run times of the scheduled tasks, and `task_path` (the Task Scheduler folder) |
| `nightly.dry_run` | `true`: Nightly and Weekly only report what they would change |
| `queue.models` | the Claude model for each kind of call (route); nothing falls back to the CLI's default |
| `pacing` | how much of your Claude plan the overnight work may use |

### The owner settings and the skill scripts

Prompt texts in the code use placeholders such as `«owner's»` and `«pc»`. The wrapper every Claude call goes
through, `jobs/claude_cli.py`, fills them in from these settings (`common.personalize`). The connector does the
same for its instructions and tool descriptions.

The skill scripts in `lib/` are also run by the `vault-librarian` skill on other PCs, outside this app. They read
the owner from environment variables: `VAULT_OWNER_NAME`, `VAULT_OWNER_ID` and `VAULT_OWNER_NOTE_LABEL`. The app
sets these from `owner` on every run. On another PC that uses the skill, set them once as user variables, or new
notes there will say "My note" and `human:owner`:

```powershell
setx VAULT_OWNER_NAME "Sam"
setx VAULT_OWNER_ID "human:sam"
setx VAULT_OWNER_NOTE_LABEL "Sam's note"
```

The field that holds your note on a queue item is still called `doug_note` in the code and in saved queue
files. Renaming it would mean migrating every queue item and the `held.json` that the laptop also writes.

## Feature switches

| Feature | Switch | Default | Needs |
|---|---|---|---|
| Mail: Remember folder intake, emailed daily update, email replies, notices | `mail.enabled` | off | Microsoft 365 mailbox, Entra app (see [Mail](#mail-microsoft-365)) |
| Daily update by email (when mail is on) | `organize.email_enabled` | on | mail |
| YouTube Remember playlist | `remember.enabled`, `remember.dry_run` | off | Google Cloud project, Gemini key (see [YouTube](#youtube-remember)) |
| Vault connector for claude.ai | `mcp.enabled` | off | public HTTPS address (see [Connector](#vault-connector)) |
| Local search index | `search.enabled` | on | about 130 MB model download (`fastembed`) |
| Related-note suggestions | `related.enabled` | on | same model as search |
| Concept analysis | `analysis.auto` | on | Claude usage (Opus by default) |
| Originals into the search index | `originals.enabled` | on | none (no model calls) |
| File intake queue | `queue.enabled`, `queue.files_enabled` | on | Tesseract and Calibre for scans and e-books (optional) |
| Usage pacing | `pacing.enabled` | on | none |
| Podcast links | `podcasts.vault_episode` | empty (off) | a separate podcast pipeline script |
| Cowork / document sync | `cowork_sync.enabled`, `doc_sync.enabled` | off | a Claude Cowork folder on this PC |

**With mail off,** everything else keeps working: the dropbox, links, the queue, the connector and the
nightly jobs. Morning questions are answered on the home page instead of by email reply. No mail errors are
recorded, so the home page doesn't show mail as down. In code, `jobs/common.py` `mail_on(cfg)` is the
single test. `send_mail` returns `None` and `graph_token` raises `MailOff` while mail is off.

## Credentials

All credentials are under `<local_root>\credentials`. They are never in the repository, never logged, and
never pasted into a Claude chat. The `.dpapi` files are encrypted with Windows DPAPI, so only your Windows
account on this PC can read them.

| File | For | Made by |
|---|---|---|
| `claude_oauth_token.dpapi` | all model calls (`claude setup-token`, valid about a year) | `install.ps1` |
| `graph-credentials.json` (path: `graph_credentials`) | mail: `{"tenantId", "clientId", "clientSecret"}` | you (see below) |
| `gemini_api_key.txt` | YouTube summaries | `install.ps1` |
| `youtube_client_secret.json` | YouTube sign-in: your Google Cloud desktop client | you (Google Cloud console) |
| `youtube_token.dpapi` | YouTube playlist access | `setup/remember_auth.py`, run by `install.ps1` |
| `mcp_token.txt` | the connector's bearer token | `install.ps1` |
| `api_token.txt`, `home_token.txt` | the local web pages' keys | made on first use |

## Mail (Microsoft 365)

1. In the Entra admin center, register an app (single tenant) and create a client secret.
2. Grant the **application** permissions `Mail.ReadWrite` and `Mail.Send`. Limit them to your own mailbox:
   tenant-wide application permissions can read and send as *every* mailbox. In Exchange Online PowerShell,
   use RBAC for Applications:
   ```powershell
   New-ManagementScope -Name "Vault mailbox only" -RecipientRestrictionFilter "PrimarySmtpAddress -eq 'you@example.com'"
   New-ManagementRoleAssignment -App <client id> -Role "Application Mail.ReadWrite" -CustomResourceScope "Vault mailbox only"
   New-ManagementRoleAssignment -App <client id> -Role "Application Mail.Send"      -CustomResourceScope "Vault mailbox only"
   ```
   If you use RBAC for Applications, take away the tenant-wide consent for the two permissions in Entra.
   Check the result with `Test-ServicePrincipalAuthorization`.
3. Save the tenant ID, client ID and secret as JSON at the `graph_credentials` path.
4. In Outlook, make an Inbox subfolder named as `intake.remember_folder` (default `Remember`). Optionally add
   subfolders named like the dropbox ones.
5. Set `mail.enabled` to `true` and set `mailbox` and `notify_to`.

`intake.confidential_domains` and `queue.confidential_markers` hold back mail that looks confidential (for
example from your employer's domain) instead of filing it.

## YouTube Remember

1. At aistudio.google.com, create a Gemini API key in a new project, on the free tier.
2. In the same project's Google Cloud console:
   - Enable the **YouTube Data API v3**.
   - Set up the consent screen as External, and publish it. Left in Testing, the sign-in expires every 7 days.
   - Create a **Desktop app** OAuth client, and save its JSON as `<local_root>\credentials\youtube_client_secret.json`.
3. Make a playlist named as `remember.playlist` (default `Remember`).
4. Set `remember.enabled` to `true` and run `install.ps1`. It asks for the key and opens the Google sign-in.
   Keep `remember.dry_run` `true` for a first night to check the report.

## Vault connector

The connector (`service/vault_mcp.py`) is an MCP server on `127.0.0.1:8767`. To use it from claude.ai, it
must be reachable at a public HTTPS address. The setup script assumes Tailscale Funnel:

1. Install Tailscale, and enable HTTPS and Funnel for your tailnet.
2. Set `mcp.enabled` to `true` and `mcp.hosts` to your host name (for example `pc-name.tailnet.ts.net`). Run
   `install.ps1` (it makes the token), then `setup\connector_setup.ps1` (it publishes `/mcp` and puts
   `Bearer <token>` on the clipboard).
3. In claude.ai, add a custom connector with the URL `https://<host>/mcp` and that bearer token.

Callers outside `mcp.caller_range` (Anthropic's published range) are refused while `mcp.caller_check` is
`enforce`. `mcp.daily_requests` caps the requests it queues each day.
