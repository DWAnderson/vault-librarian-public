# User guide

How to live with Vault Librarian once it is installed (see the [README](../README.md) for installing and
[CONFIGURATION.md](CONFIGURATION.md) for settings). In short: you capture things the way you always do, the
app turns them into tidy notes, and every morning it asks you a few questions. **It proposes first and acts
only on your answers.** The one exception is strong duplicates (the same web address, or nearly identical
text): it moves those to the archive by itself, and never deletes them.

## 1. Your vault

The app expects an Obsidian vault laid out roughly like this. Create the folders you don't have yet. The
filing step only proposes folders that already exist, plus new ones you approve.

| Folder or file | What goes there |
|---|---|
| `Inbox/` | New notes the app made, waiting to be filed |
| `Clippings/` | Pages saved with the Obsidian Web Clipper, waiting to be filed |
| `Personal Notes/` | Notes you wrote yourself. The app never files incoming items here |
| `Subjects/<subject>/` | Incoming material on a subject: articles, videos, podcasts, recipes, emails |
| `Manuals/<subfolder>/` | Product manuals, quick-start guides, warranties and spec sheets |
| `Projects/`, `Areas/`, `Resources/` | Things with an outcome, ongoing responsibilities, reference material |
| `Concepts/` | One short note per idea, made by the analyses (section 5) |
| `_Obsidian Internal/` | Housekeeping: `Archives/`, `attachments/`, `Templates/`, `Topics/` (generated tag hubs) and `_claude-tags.md` |
| `_Obsidian Internal/_claude-tags.md` | **Your approved tag list.** The app uses only these tags, and asks before adding one. **The morning step needs this file.** Start from `config/claude-tags.example.md` |
| `index.md` in each folder, `log.md` | A generated table of contents per folder, and a dated record of every change the app makes |

Notes get Open Knowledge Format properties (`type`, `title`, `description`, `tags`, `generated`), so any AI
tool can read the vault. Source items also get two properties for you: `my_note` (a one-line take) and
`see_also` (links you think belong with it).

## 2. Getting things in

| Way | How | Result |
|---|---|---|
| **Dropbox folder** | Drop a file into the folder you chose at install (`queue.dropbox_path`) | A note in `Inbox/`, made within 30 minutes during the day; books and long documents at night |
| **Web Clipper** | Clip a page into `Clippings/` as usual | Citation details filled in overnight; filed by the morning questions |
| **Save a link** | Open `http://127.0.0.1:8766/vault/` and paste a link, or ask Claude through the connector | Full text for an article, a one-paragraph summary for other pages, the picture for an image |
| **Mail** (optional) | Move an email into Outlook's `Remember` folder (or one of its subfolders) | The email or its attachments become notes |
| **YouTube** (optional) | Add a video to your `Remember` playlist | A detailed Video Summary and a Transcript note, overnight |

**The dropbox subfolder decides the treatment.** The same subfolders work in the Outlook `Remember` folder:

| Subfolder | You get | Note type |
|---|---|---|
| top level, or `Other` | The app picks one of the treatments below; a book-length file waits for your OK | — |
| `Articles` | The full text, with author, publication, date and a citation | Article |
| `Full text` | The full text of any other document | Reference |
| `Recipes` | Ingredients, steps, servings and times, copied exactly | Recipe |
| `Manuals` | Brand, model, what it covers, key sections with page numbers | Manual |
| `Books` | A detailed chapter-by-chapter summary, written at night | Book Summary |
| `Summarize` | A detailed summary of a long document, written at night | Reference |

- **File types:** PDF (scans are OCR'd), Word, text, saved web pages (`.html`, `.mhtml`), photos and scans,
  e-books (`.epub`, `.mobi`, `.azw3`), and saved emails (`.msg`, `.eml`).
- **Your own note on a file:** put a `.txt` with the same name next to it (`Dishwasher.pdf` + `Dishwasher.txt`).
  It appears at the top of the note under your note label. For a manual, this is where a serial number or
  purchase date goes; those are never guessed.
- **The original file is kept** under `_Obsidian Internal/attachments/Sources/`. The file leaves the dropbox
  only after its note is written.
- **Held for you instead of filed:** anything that looks confidential (markers such as "Privileged &
  Confidential", plus the domains and words in `intake.confidential_domains` and `queue.confidential_markers`),
  DRM-protected e-books, damaged or password-protected PDFs, and file types it can't read.
- **Before spending a lot** on a book or long summary, the app checks whether the vault already has it, and asks.

## 3. The morning questions

At 06:00 the app gathers everything that needs a decision into one update:
- where to file each new note, with a suggested folder, type, tags and sometimes a clearer name;
- duplicates;
- names that look like file names;
- large items to confirm;
- new concepts;
- items it couldn't save.

**Where to find it:** the home page (section 7) always has it. With mail on, it also arrives as an email.

**How to answer:**
- **On the web page:** the suggested answers are already selected. Change any you disagree with ("Leave in
  the Inbox for now" is always an option), then press **Apply these answers**. A progress page shows each step.
- **By email reply** (mail on): `ok` takes every suggestion, `skip` leaves everything, and letters such as
  `1a 3b` answer single questions. `ok but 2c` approves everything except question 2. Replies are applied by
  the next Intake run.
- **Through Claude** (connector on): "what are this morning's vault questions?", then "answer 1a, 3b". Claude's
  answers are held until you say "apply them" or submit the page. Questions that delete or merge something
  can't be answered this way.

Whichever answer arrives first counts. Anything you leave unanswered is asked again the next morning.

**What the choices do, briefly:**
- **File a note:** moves it, adds the folder's tag, keeps an old name as an alias, and rebuilds the indexes. A
  summary and its transcript move together.
- **New tag or new folder:** created only on yes. A "no" is remembered.
- **Duplicates:** an archived copy can be deleted, merged into the kept note, or restored. A suspected pair can
  be resolved by keeping one, or marked "not duplicates" so it's never asked again.
- **Name check:** use the suggested "Title (Author year)", type your own, or keep the current name.
- **Couldn't be saved:** try again, stop asking, or delete. For pages that need your sign-in, clip them yourself
  with the Web Clipper; the app finds your clip and clears the card.

Every item on the web page opens read-only. Duplicates and "already in the vault?" questions have a **compare
side by side** view that marks the paragraphs found in only one of the two.

## 4. Night work and your Claude plan

Books, long summaries and long analyses run at night (23:45–01:55 by default), paced against your Claude
plan. The app reads your usage before and after each item. It aims to end the week near `pacing.weekly_goal_pct`
(90%) and never pushes the 5-hour session past `pacing.session_max_pct` (80%). Early in the week it does
little; unused allowance builds up toward the weekend.

From the morning update or the home page you can choose **Use more tonight**, **Go light tonight** or
**Normal**. You can also book an extra run window in the next 24 hours, for example 2–7 AM up to 90%. The
**queue page** lists what's waiting. Each item can be moved first, held, released, skipped or routed
differently, and its name and details edited before it runs.

Every Claude call is logged with its tokens and API-equivalent cost. `jobs/usage_report.py` summarizes the
log by day, route and model.

## 5. Analyses, concepts and your own views

Each filed article, clipping, summary or reference note gets a companion **`<item> (Analysis)`** note, a draft
by Claude that you may edit. It contains:
- **In brief**, and the **argument** if the item argues a case: thesis, reasons, objections, conclusion;
- **key concepts**, each linked to a note in `Concepts/`;
- **notable claims**, with quotations that a script checks against the text (a quote it can't find is marked
  *(unverified)*);
- **related notes**, each labeled agrees, disagrees, extends, applies, same subject or background.

Concept notes list every item that uses the idea and where your sources disagree. A new idea becomes a concept
note only once two items use it, and only after you say yes in the morning questions.

**Make your views count.** Each of these reaches Claude as *your* view, kept apart from the item's text:
- `my_note`: a one-line take;
- `see_also`: links to notes you think go with this one (they always appear among the related notes);
- a `## My notes` heading in the item's note ("My thoughts", "My comments" or "<your name>'s notes" also work);
- a separate note of your own that links to the item;
- the `.txt` beside a dropped file.

When you change your notes, the analysis is redone after an hour. An analysis you've edited isn't overwritten;
you're asked instead.

**Ask for more** (through Claude, or `jobs/vqueue.py analyze …`): "analyze <note>", "re-analyze everything
tagged economics", "refresh the related links for <note>". Long items and books are analyzed chapter by
chapter at night, with one Part Analysis note per chapter.

## 6. Fixing a name or a filing yourself

- **Just do it in Obsidian.** Rename the note or drag it to another folder. That night the app notices, adds
  the new folder's tag, keeps the old name as an alias, and learns from it for future suggestions. When you
  make the same kind of move twice, it suggests a standing rule.
- **Or add a `fix` property** describing what you want: `file under Subjects/Economics`,
  `title should be The Road to Serfdom (Hayek 1944)`, `add tag economics`. That night Claude makes the change
  and removes `fix`. If it can't do it safely, `fix` stays with a reason, and the morning update asks you.

## 7. The home page

`http://127.0.0.1:8766/vault/home?t=<key>`. The installer prints the full link, and the key in it is the
password, so bookmark it. To use it from your phone, publish the pages on a private network (for example
Tailscale Serve) and set `organize.web_base` so emailed links point there.

- **Today:** open questions, what's new since the last update, recent answers, tonight's usage plan, items
  that need you, and a "save a link" box.
- **Status:** every scheduled job with its last result and next run, mail health, Claude usage, vault and
  queue counts, and recent activity.
- **Settings:**
  - run times (checked so the night jobs keep their order);
  - pacing;
  - the Claude model for analyses and summaries;
  - switches for intake, dropbox files, automatic analyses, the morning email, YouTube and Originals;
  - **Pause until** a date (vacation);
  - **Run now** buttons and an undoable change history;
  - **Make a new link** if the link leaked.

## 8. Asking Claude about your vault

With the connector on (CONFIGURATION.md, Vault connector), any Claude app can search and read your vault:
"Using my vault, what did I save about heat pumps?", "What's my vault status?". Claude searches by keyword and
meaning, reads only the sections it needs, and cites them as `[[Note#Section]]`. Each fact is labeled with
what kind of note it came from: the source itself, your own notes, a summary, or an analysis. It can also
queue a few requests (save a link, analyze a note, add your `my_note`, save a draft it wrote) after asking
you. It can't move, rename, merge, delete or edit notes.

## 9. Safety

- **Propose, then act.** Nothing is filed, renamed, merged or deleted without your answer. Strong duplicates
  are archived, not deleted.
- **One writer at a time.** A job changing the vault holds `.vault-librarian.lock` at the vault root. Other
  jobs wait or skip, and a lock older than two hours is treated as stale.
- **Backups.** Nightly takes a zip snapshot of the vault: 7 daily and 4 weekly are kept under
  `%LOCALAPPDATA%\<local_root>\snapshots`.
- **Deletes are recoverable.** Deleted duplicates go to the vault's `.trash`, and every change is listed in
  `log.md`.
- **Your text stays yours.** The body of your own notes is never rewritten, and analyses and concept notes are
  marked as Claude's drafts.

## 10. When something looks wrong

| Symptom | What to do |
|---|---|
| No morning update | Check the home page's Status tab and the Vault Maintenance Report in `Claude/Other/`. |
| "Couldn't read your reply" | Reply to the original update again (`ok`, `skip`, `1a 2b`), or use the web page. |
| The home page says mail is failing | Answer on the home page. The email is sent again once mail works. If it lasts, check the Graph client secret. |
| "Claude … needs signing in again" | From your own PowerShell, delete `credentials\claude_oauth_token.dpapi` under your `local_root`, then run `setup\install.ps1` again. It signs you in and saves a new token, and missed work catches up by itself. |
| Model work stops, or "claude.exe not found" | Reinstall Claude Code and sign in again. |
| Books or long items wait for days | The night window may be too short for your plan, or usage was used up during the day. Check the queue page and the pacing settings. |
| A setting change says the scheduled task couldn't be updated | Nothing changed. Run `setup\register_tasks.ps1 -Only <task>` from your own PowerShell. |
| The web page says "vault busy" | Another job holds the lock. Try again in a few minutes. |
| An "overnight maintenance failed" email | Open the report it names. In a Claude session on the PC, ask Claude to check last night's maintenance. |
| Search can't find something new | The index updates within minutes. To force it: `python jobs\vault_search.py update` with the runtime's Python. |
| Anything else | `setup\install.ps1 -CheckOnly` lists what's missing. Each run's report is in `reports\`. |
