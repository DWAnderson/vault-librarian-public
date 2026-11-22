# Security

## What the app protects

- **Secrets** live only in `<local_root>\credentials` on the PC, outside the repository. The Claude token and
  the YouTube token are encrypted with Windows DPAPI, for your Windows account only. Jobs never log a secret.
  `.gitignore` keeps `config/vault.json`, `queue/` and `reports/` out of git, because they hold your addresses
  and vault content.
- **Local web pages** listen on `127.0.0.1:8766` only. The home page, the morning questions and the queue
  need a key in the link (`?t=`). The API for other apps needs a header token. The save-a-link form and the
  status page have no key, so publish them beyond the PC only on a private network, such as a Tailscale
  tailnet with Serve, never with Funnel.
- **The Vault connector** listens on `127.0.0.1:8767`. When you publish it, it requires a bearer token, refuses
  callers outside `mcp.caller_range` (Anthropic's address range, when `mcp.caller_check` is `enforce`), checks
  the `Host` header against `mcp.hosts`, and caps queued requests per day. Its tools read the vault. The
  request tools only queue work, which the jobs apply later under the vault rules.
- **Mail** uses a Graph app with application permissions. Limit it to your own mailbox; see
  [docs/CONFIGURATION.md](docs/CONFIGURATION.md#mail-microsoft-365).
- **Vault content is data, not instructions.** Text in saved pages, emails and files is passed to Claude as
  material to summarize, never as commands.

## Before you push

Run `python setup/privacy_check.py` (add `--history` before making the repository public). It lists files
that contain your addresses, host names, paths or other terms from your settings, without printing them.

## Reporting a problem

Please report security problems privately to the repository owner, through GitHub's "Report a vulnerability"
(Security tab), rather than in a public issue.
