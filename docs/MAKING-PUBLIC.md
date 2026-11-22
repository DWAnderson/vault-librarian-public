# Making the repository public

The repository is meant to stay private as your working copy. A public release is a separate repository: a
cleaned snapshot with a fresh, one-commit history, built by `setup/make_public.ps1`.

## What is already in place

- **Names come from the config.** Claude's instructions, the connector's descriptions, messages and reports name
  the owner and the PC through `owner` and `pc_name` in `config/vault.json`. Comments and docstrings say "the
  owner" and "this PC". See [CONFIGURATION.md](CONFIGURATION.md#the-owner-settings-and-the-skill-scripts).
- **Addresses, host names and paths** are in the local config only. Code defaults are generic.
- **Personal files are listed in `.public-exclude`:** the plans, the developer notes, the user manual (which
  describes one person's vault and devices), the search benchmark questions, and one-time migration scripts.
  The public copy leaves them out.
- **`setup/privacy_check.py`** lists personal terms in any repository without printing them. The terms come from
  your settings plus the git-ignored `config/privacy-terms.local.txt`.

## Steps for a release

1. Commit your work. The copy is built from the last commit only.
2. Build and check the copy:
   ```powershell
   .\setup\make_public.ps1 -Email <id>+<user>@users.noreply.github.com
   ```
   It writes the copy to `%TEMP%\<folder>-public`, makes one commit with that author email, and runs the
   privacy check on it. Use GitHub's noreply address (GitHub → Settings → Emails) so your mail address isn't
   in the commit.
3. Read what the check reports. Expected leftovers are:
   - your name in `LICENSE` (the copyright line)
   - the `doug_note` field name (see CONFIGURATION.md)
   - mentions of "Daily Summary" and "Cowork", the owner's other tools
   - test fixtures
   For anything else, fix it in the private repository, commit, and run step 2 again with `-Force`.
4. The `LICENSE` (MIT) and `docs/USER-GUIDE.md` are already in the repository, so the copy includes them.
5. Publish:
   ```powershell
   gh repo create <name> --public --source "$env:TEMP\<folder>-public" --push
   ```
   Then turn on private vulnerability reporting (repository Settings → Security).

For later releases, build a new copy and push it as a new commit on the public repository. Never push the
private repository's history there.

## Still worth doing before a first release

- **Example search questions** in `tests/` to replace the excluded `search_questions.json`.
