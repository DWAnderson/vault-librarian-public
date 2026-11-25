# Changelog

A short history. The private repository also keeps detailed design notes for each change.

## 2026-11-23: concept questions and answer retries
- A concept is asked about once two different items use it: a book's chapters count as one item (before, two
  chapters of one book were enough). Concepts used by one book go to the overnight review instead.
- Concept questions say how many items each concept is linked to (a book's chapters count once), in the
  heading and the line below it.
- Concept questions are asked most-used first, and "Not now" brings one back after 7 days instead of the next
  morning (`analysis.later_days`).
- New `jobs/concept_review.py`: each night Claude (route `concept_review`, Opus) reviews up to 20 concepts that
  only one item has used for 30+ days on one question: is it an interesting idea, or an interesting take on
  one, that is not common knowledge? It suggests keep or drop, with a reason. The morning update asks up to 10
  "Keep this concept?" questions a day, outside the 15-question cap (`analysis.concept_review`).
- Concept merge: meaning threshold 0.77 (was 0.83), up to 150 pairs a run in calls of 50, confirmed by Opus
  (was Sonnet), and every 3 days while it still finds overlaps (`every_days_backlog`), else every 7.
- Answers to the daily update are saved in the proposal before they are applied. An apply that stops partway is
  retried from the saved answers by Intake and the 06:00 run (up to 3 tries; then one email), instead of the
  questions being asked again.

## 2026-11-22 (later): names from the config, public copy
- New `owner` and `pc_name` settings. Claude's instructions, the connector's descriptions, messages, reports and
  the notes the app writes (`generated.by`, the owner's note callout) take the owner's name and the PC's name
  from them. The code has placeholders (`common.personalize`), filled in by the single Claude wrapper.
- The `lib/` skill scripts read the owner from `VAULT_OWNER_*` environment variables, which the app sets.
- Comments and docstrings say "the owner" and "this PC".
- `setup/make_public.ps1` builds a one-commit public copy without the paths in `.public-exclude` and checks it.
  `privacy_check.py --root` scans any repository.

## 2026-11-22: ready for GitHub
- Installer `setup/install.ps1` and config wizard `setup/make_config.py`. They take you from a fresh clone to
  running tasks, and can be run again to repair an install.
- `config/vault.example.json` is the committed template. `config/vault.json` is now local and git-ignored.
- Master mail switch `mail.enabled`: the app runs without Microsoft 365. Scheduled tasks for features that
  are off (connector, YouTube) are not registered.
- `requirements.txt` and `requirements-optional.txt`, so only the packages for features that are on get installed.
- The runtime and Task Scheduler folder are read from the config, not written into the scripts. The vault
  path-length check no longer assumes one vault location.
- `setup/privacy_check.py` lists personal details in files git would publish.
- New README, `docs/CONFIGURATION.md`, `SECURITY.md`, `docs/MAKING-PUBLIC.md`. Plans moved to `docs/plans/`.

## 2026-11-19
- Memory check in the nightly report.

## 2026-11-12 to 2026-11-16
- Home page: open questions, status and settings in the browser, so morning questions survive a mail outage.
- Re-clip matching, task times that don't drift with daylight saving, Podwise episodes, podcast audio links.

## 2026-10-24
- Long-lived Claude token, and a nightly sign-in check with recovery of missed runs.

## 2026-10-08 to 2026-10-12
- Vault search: a local hybrid index (keyword and meaning), the Vault connector (MCP) for claude.ai, and
  originals of books and PDFs in the index.

## 2026-10-01 to 2026-10-10
- More file types, unreadable files reported instead of summarized, note-name rules, your own corrections
  applied overnight.

## 2026-09-28 and 2026-09-29
- Intake queue for files and email, with usage pacing.
- Concept analysis: key ideas, arguments and links between items.

## 2026-09-25 and 2026-09-26
- First version: nightly and weekly maintenance, Remember-folder email intake, the daily filing email, and
  YouTube Remember summaries.
