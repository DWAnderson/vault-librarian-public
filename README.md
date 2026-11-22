# Vault Librarian

An always-on helper for an [Obsidian](https://obsidian.md) vault, running on a Windows PC. It files what you
send it into the vault as well-formed notes, keeps the vault tidy overnight, and lets Claude search the vault.
All model work goes through the [Claude Code](https://claude.com/claude-code) CLI on your own subscription.

- **Capture:** files dropped in a folder (PDFs, Office files, e-books, saved emails), links, and optionally
  emails in an Outlook "Remember" folder and videos in a YouTube "Remember" playlist, all become notes with
  summaries, citations and tags.
- **Organize:** every morning it proposes where unfiled notes should go. You answer on a home page in the
  browser, or by replying to an email if mail is on.
- **Maintain:** each night it takes a snapshot, removes strong duplicates, fixes citations, rebuilds the
  indexes and topic hubs, and writes a report note.
- **Understand:** concept analysis links related ideas across notes, and a local search index finds notes by
  keyword and by meaning.
- **Connect:** an optional MCP connector lets Claude on claude.ai search and read the vault.

Notes follow the Open Knowledge Format conventions of the `vault-librarian` skill, which travel with the app
in `lib/`.

## Requirements

| Needed for | What |
|---|---|
| Everything | Windows 10/11, Python 3.11 or newer, an Obsidian vault folder, Claude Code with a Claude subscription |
| Scanned PDFs, e-books | [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki), [Calibre](https://calibre-ebook.com) (optional) |
| Mail features | A Microsoft 365 mailbox and an Entra app registration for Microsoft Graph (optional) |
| YouTube summaries | A Google Cloud project (YouTube Data API) and a Gemini API key (optional) |
| Vault connector | A public HTTPS address for the PC, for example Tailscale Funnel (optional) |

Every optional feature can be left off. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md#feature-switches).

## Install

From your own PowerShell, not as Admin and not from inside a Claude session:

```powershell
git clone <repository URL> "$env:USERPROFILE\Apps\vault-librarian"
cd "$env:USERPROFILE\Apps\vault-librarian\setup"
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process -Force
.\install.ps1
```

The installer:

1. Checks the prerequisites.
2. Asks a few questions (vault folder, time zone, which optional features you want) and writes
   `config/vault.json`.
3. Builds a private Python runtime under `%LOCALAPPDATA%`.
4. Walks you through the sign-ins the features need.
5. Offers to build the search index.
6. Registers the scheduled tasks.

It's safe to run again. `.\install.ps1 -CheckOnly` lists what is set up and what is missing.

New installs start with the nightly maintenance as a **dry run**: it reports what it would change. Once the
reports look right, set `nightly.dry_run` to `false` in `config/vault.json`.

## Using it

- **Home page:** `http://127.0.0.1:8766/vault/home?t=<key>`. The installer prints the full link. It shows
  open questions, what ran overnight, and the settings you can change without editing files.
- **Adding things:** drop files into the dropbox folder you chose. Its subfolders (`Articles`, `Books`,
  `Recipes`, `Manuals`, `Full text`, `Summarize`, `Other`) say how each item is handled.
- **Reports:** every run writes `reports/<date> <job>.md`. A summary note goes into the vault each night.
- **Day-to-day use:** [docs/USER-GUIDE.md](docs/USER-GUIDE.md) covers the vault layout it expects (including the
  approved tag list it needs: start from `config/claude-tags.example.md`), getting things in, the morning
  questions, analyses, the home page, and troubleshooting.
- **Settings and optional features:** [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Scheduled tasks

| Task | When | Does |
|---|---|---|
| Intake | every 30 min, daytime | dropbox, links, connector requests and, with mail on, the Remember mail folder |
| QueueNight / QueueWindow | night / booked windows | books and long summaries, paced by your Claude plan usage |
| Organize | 06:00 | the morning filing proposal and questions |
| Nightly | 02:30 | snapshot, duplicates, citations, indexes, hubs, lint, report |
| Weekly | Sunday 03:30 | citation backfill, full lint, pruning |
| Originals | 02:45 | text of book and PDF originals into the search index (no model calls) |
| SignInCheck | 21:00 | one tiny call to confirm the Claude sign-in still works |
| Server | at logon | home page and save-a-link page on `127.0.0.1:8766` |
| Connector | at logon | the MCP connector on `127.0.0.1:8767` (only when switched on) |
| YouTubeRemember | 02:05 | YouTube playlist summaries (only when switched on) |

Run times are set in `config/vault.json` (`schedule`) or on the home page.

## Where things live

| Location | Holds |
|---|---|
| this folder | code (`jobs/`, `service/`, `lib/`), settings (`config/`), setup scripts (`setup/`), docs |
| `config/vault.json` | your settings; local, never committed (template: `config/vault.example.json`) |
| `queue/`, `reports/` | the work queue and run reports; local, never committed |
| `%LOCALAPPDATA%\<local_root>` | logs, work files, vault snapshots, search index, and credentials (Windows-encrypted) |
| `%LOCALAPPDATA%\Programs\<app>\runtime` | the Python runtime |

## Moving or renaming

- **Moving the folder or the PC:** move or clone the folder, copy `config/vault.json` across (fix `vault_path`
  if the vault moved), then run `setup\install.ps1` again. It rebuilds the runtime if it's missing and
  re-registers the tasks against the new location. Credentials stay in `%LOCALAPPDATA%`, so a new PC needs
  its sign-ins again.
- **Renaming the app:** change `local_root`, `runtime` and `schedule.task_path` in `config/vault.json`, copy
  the old `%LOCALAPPDATA%` data folder to the new name (or redo the sign-ins), unregister the old tasks, and
  run `setup\install.ps1`.

## Development

- `lib/` holds published copies of the `vault-librarian` and `url-to-vault` skill scripts. Change them in the
  skill source and publish (`setup/publish.ps1`); don't edit them here.
- Tests are in `setup/test_*.py` and `setup/queue/test_*.py`. Run them with the runtime's Python. They work on
  temporary folders, not your vault.
- Every Claude call goes through `jobs/claude_cli.py`, which logs it to
  `%LOCALAPPDATA%\<local_root>\logs\llm-YYYY-MM.jsonl`. `jobs/usage_report.py` summarizes the log by route,
  day and model.
- [CHANGELOG.md](CHANGELOG.md) has a short history.
- Before pushing, run `python setup/privacy_check.py` to list any personal details in files git would publish.
  `setup/make_public.ps1` builds a public copy without the paths in `.public-exclude`. See
  [docs/MAKING-PUBLIC.md](docs/MAKING-PUBLIC.md).

## Security

See [SECURITY.md](SECURITY.md). In short: secrets never go into the repository, the local web pages are
bound to `127.0.0.1`, and the connector needs a bearer token and accepts only Anthropic's address range.

## License

MIT. See [LICENSE](LICENSE).
