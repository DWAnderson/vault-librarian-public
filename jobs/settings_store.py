"""Settings the owner can change from the home page (plan: 2026-11-12 Home page - plan.md, sections 6-7).

  SPEC                      every editable setting: where it lives in config\\vault.json, its kind and bounds,
                            and the scheduled task whose trigger follows it
  current(cfg)              {id: value}
  check(values)             list of problems with a full set of values (times, night order, bounds)
  change(cfg, new, via)     check, write config, update task triggers, log; rolls back if a trigger fails
  undo(cfg, via)            puts back the values of the last change that isn't undone yet
  changes(n)                the change log, newest first (queue\\settings-changes.jsonl)
  run_now(cfg, what, via)   start a task now (Intake, QueueNow, Nightly), request Originals, resend the update
  tasks_info(cfg)           state, last run, result and next run of every task (cached 60 s)

config\\vault.json is read and written raw (no %VAR% expansion), through a temp file. The first change made from
the page keeps a copy of the file as it was in setup\\_backup-<date>-home\\. Task triggers are set by PowerShell
(Set-ScheduledTask; the tasks run as the owner at the Limited level, so no admin rights are needed) with plain local
times, like setup\\register_tasks.ps1 (daylight-saving fix, 2026-11-12).
"""
import datetime as dt
import json
import re
import secrets
import subprocess
import threading
import time

import common as C

CONFIG = C.APP / "config" / "vault.json"
CHANGES = C.APP / "queue" / "settings-changes.jsonl"
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DAY_FULL = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday", "Fri": "Friday",
            "Sat": "Saturday", "Sun": "Sunday"}
_lock = threading.Lock()


def task_path(cfg):
    return C.task_path(cfg)


# id: (group, label, kind, config path, bounds or None, task or None, help)
# kinds: time "HH:MM", weekly "Sun 03:30", hour int 0-24, int, float, bool, date ("" = none)
SPEC = {
    "organize":       ("times", "Morning questions and email (Organize)", "time", ("schedule", "Organize"), None, "Organize",
                       "Builds the day's questions and sends the update email. Uses Claude."),
    "intake_first":   ("times", "Intake: first run (hour)", "hour", ("intake", "first_hour"), (0, 23), "Intake",
                       "Intake runs every 30 minutes from this hour."),
    "intake_last":    ("times", "Intake: last run before (hour)", "hour", ("intake", "last_hour"), (1, 24), "Intake",
                       "No Intake run starts at or after this hour."),
    "signin":         ("times", "Claude sign-in check", "time", ("schedule", "SignInCheck"), None, "SignInCheck",
                       "One tiny Claude call before the night jobs; emails you if sign-in is broken."),
    "night_start":    ("times", "Night queue starts (books, long summaries)", "time", ("queue", "night_window", 0), None,
                       "QueueNight", "Start of the night window for costly Claude work."),
    "night_end":      ("times", "Night queue ends", "time", ("queue", "night_window", 1), None, None,
                       "No new costly item starts after this."),
    "remember":       ("times", "YouTube Remember starts", "time", ("schedule", "YouTubeRemember"), None, "YouTubeRemember",
                       "Remember playlist videos to notes (Gemini, no Claude)."),
    "remember_stop":  ("times", "YouTube Remember stops by", "time", ("remember", "stop_by"), None, None, ""),
    "nightly":        ("times", "Nightly maintenance", "time", ("schedule", "Nightly"), None, "Nightly",
                       "Snapshot, clean-ups, indexes, lint, report. Holds the vault lock."),
    "originals":      ("times", "Originals into search starts", "time", ("schedule", "Originals"), None, "Originals",
                       "Text of books and PDFs into the search index (no Claude)."),
    "originals_stop": ("times", "Originals stops by", "time", ("originals", "stop_by"), None, None, ""),
    "weekly":         ("times", "Weekly maintenance", "weekly", ("schedule", "Weekly"), None, "Weekly",
                       "Citation backfill, full lint, search check, pruning."),
    # Claude models and concepts (2026-11-13). kind "choice": bounds = the allowed values.
    "model_analysis": ("claude", "Model for whole-item analyses and the book-level synthesis", "choice",
                       ("queue", "models", "analyze"), ("opus", "sonnet", "haiku"), None,
                       "Where the deeper reasoning matters most. Opus uses roughly twice Sonnet's share of your plan per call."),
    "model_parts":    ("claude", "Model for chapter-by-chapter part analyses", "choice",
                       ("queue", "models", "analyze_part"), ("opus", "sonnet", "haiku"), None,
                       "Most calls for long items and books (one per chapter); the synthesis above pulls them together."),
    "model_books":    ("claude", "Model for book summaries", "choice", ("queue", "models", "book"),
                       ("opus", "sonnet", "haiku"), None, ""),
    "register_detail": ("claude", "Concepts described with each analysis", "int", ("analysis", "register_detail"),
                        (0, 300), None,
                        "All concept names are always sent (and cached). This many of the concepts most like the item "
                        "also get their descriptions. More = better reuse of existing concepts, more usage."),
    "register_pending": ("claude", "Also show Claude concepts still awaiting your OK", "bool",
                         ("analysis", "register_pending"), None, None,
                         "Off = only approved concepts (fewer tokens); new ones are then combined by the concept merge."),
    "concept_merge_days": ("claude", "Combine overlapping concepts every (days)", "int",
                           ("analysis", "concept_merge", "every_days"), (0, 90), None,
                           "Runs with Nightly. One Sonnet call checks likely pairs. 0 = off."),
    "concept_merge_auto": ("claude", "Combine new (pending) concepts without asking", "bool",
                           ("analysis", "concept_merge", "auto_merge_pending"), None, None,
                           "Off = pending pairs are only listed in the Nightly report. Pairs involving an approved "
                           "concept are always asked about in the morning email."),
    "weekly_goal_pct":     ("pacing", "Week's goal (% of the weekly Claude limit)", "int", ("pacing", "weekly_goal_pct"), (30, 100), None,
                            f"{C.pc_name()} paces the night work to end the week near this."),
    "session_max_pct":     ("pacing", "Never take the 5-hour session past (%)", "int", ("pacing", "session_max_pct"), (20, 95), None,
                            "Leaves room for the Daily Summary and your own use."),
    "daytime_pct_per_day": ("pacing", "Daytime work per day (% of the week)", "float", ("pacing", "daytime_pct_per_day"), (0, 30), None, ""),
    "books_max":           ("pacing", "Most books / long summaries a night", "int", ("pacing", "books_max"), (0, 50), None, ""),
    "night_items_max":     ("pacing", "Most other items (analyses) a night", "int", ("pacing", "night_items_max"), (0, 300), None, ""),
    "calls_max":           ("pacing", "Most Claude calls a day", "int", ("pacing", "calls_max"), (0, 2000), None, ""),
    "more_factor":         ("pacing", "\"Use more tonight\" multiplies tonight's share by", "float", ("pacing", "more_factor"), (1, 5), None, ""),
    "light_factor":        ("pacing", "\"Go light tonight\" multiplies it by", "float", ("pacing", "light_factor"), (0, 1), None, ""),
    "intake_on":     ("switches", "Intake live (off = dry runs only, nothing changes)", "bool", ("intake", "enabled"), None, None,
                      "Remember emails, the dropbox and the queue."),
    "files_on":      ("switches", "Files from the Remember dropbox", "bool", ("queue", "files_enabled"), None, None, ""),
    "analysis_on":   ("switches", "Automatic analyses of new items", "bool", ("analysis", "auto"), None, None, ""),
    "email_on":      ("switches", "Morning update email", "bool", ("organize", "email_enabled"), None, None,
                      "Off: no email; the questions are still on this page."),
    "remember_on":   ("switches", "YouTube Remember", "bool", ("remember", "enabled"), None, None, ""),
    "originals_on":  ("switches", "Originals into search", "bool", ("originals", "enabled"), None, None, ""),
    "pause_until":   ("switches", "Pause until (vacation)", "date", ("pause", "until"), None, None,
                      "Through this date: no intake, queue, analyses, books or YouTube Remember. Empty = not paused."),
}
DEFAULTS = {"organize": "06:00", "signin": "21:00", "remember": "02:05", "nightly": "02:30", "originals": "02:45",
            "weekly": "Sun 03:30", "more_factor": 2.0, "light_factor": 0.25, "email_on": True, "remember_on": True,
            "pause_until": "", "night_start": "23:45", "night_end": "01:55",
            "model_analysis": "opus", "model_parts": "sonnet", "model_books": "sonnet", "register_detail": 60,
            "register_pending": False, "concept_merge_days": 7, "concept_merge_auto": True}
BIG = {"intake_on", "email_on", "pause_until"}      # asked twice when switched off / set (plan section 7)
DS_SLOT = ("04:45", "05:30")                         # fallback; the real one is schedule.daily_summary_slot


# ---------------------------------------------------------------- reading
def raw():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def _get(d, path):
    for k in path:
        if isinstance(k, int):
            d = d[k] if isinstance(d, list) and len(d) > k else None
        else:
            d = d.get(k) if isinstance(d, dict) else None
        if d is None:
            return None
    return d


def _set(d, path, v):
    for k in path[:-1]:
        d = d.setdefault(k, {})
    last = path[-1]
    if isinstance(last, int):
        raise TypeError("list paths are set by their parent")
    d[last] = v


def current(conf=None):
    conf = conf if conf is not None else raw()
    out = {}
    for sid, (_, _, kind, path, _, _, _) in SPEC.items():
        v = _get(conf, path)
        if v is None:
            v = DEFAULTS.get(sid)
        out[sid] = v
    return out


def ds_slot(cfg):
    s = (cfg.get("schedule") or {}).get("daily_summary_slot")
    return tuple(s) if s and len(s) == 2 else DS_SLOT


# ---------------------------------------------------------------- checking
def parse(sid, text):
    """A form value -> typed value, or raises ValueError with a plain message."""
    kind, label, bounds = SPEC[sid][2], SPEC[sid][1], SPEC[sid][4]
    t = str(text if text is not None else "").strip()
    if kind == "bool":
        return t.lower() in ("1", "true", "on", "yes")
    if kind == "choice":
        if t.lower() not in bounds:
            raise ValueError(f"{label}: '{t}' isn't one of {', '.join(bounds)}")
        return t.lower()
    if kind == "time":
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
        if not m or int(m[1]) > 23 or int(m[2]) > 59:
            raise ValueError(f"{label}: '{t}' isn't a time like 06:00")
        return f"{int(m[1]):02d}:{m[2]}"
    if kind == "weekly":
        m = re.fullmatch(r"([A-Za-z]{3})\w*\s+(\d{1,2}):(\d{2})", t)
        if not m or m[1].title() not in DAYS or int(m[2]) > 23 or int(m[3]) > 59:
            raise ValueError(f"{label}: '{t}' isn't a day and time like Sun 03:30")
        return f"{m[1].title()} {int(m[2]):02d}:{m[3]}"
    if kind == "date":
        if not t:
            return ""
        try:
            return dt.date.fromisoformat(t).isoformat()
        except ValueError:
            raise ValueError(f"{label}: '{t}' isn't a date") from None
    try:
        v = int(t) if kind in ("int", "hour") else round(float(t), 2)
    except ValueError:
        raise ValueError(f"{label}: '{t}' isn't a number") from None
    if bounds and not (bounds[0] <= v <= bounds[1]):
        raise ValueError(f"{label}: {v} is outside {bounds[0]}-{bounds[1]}")
    return v


def _m(hhmm):
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def _night(hhmm):
    """Minutes on a clock that starts at noon, so 23:45 comes before 02:30 (the night runs in order on it)."""
    return (_m(hhmm) - 720) % 1440


def check(v, cfg=None):
    """Problems with a full set of values (empty list = fine). Rules: plan section 7."""
    bad = []
    a, b = ds_slot(cfg or {})
    ds = (_night(a), _night(b))

    def in_ds(x):
        return ds[0] <= _night(x) < ds[1]
    n = {k: _night(v[k]) for k in ("organize", "signin", "night_start", "night_end", "remember", "remember_stop",
                                   "nightly", "originals", "originals_stop")}
    if not n["night_start"] < n["night_end"]:
        bad.append(f"The night queue must end after it starts ({v['night_start']} to {v['night_end']}).")
    if not n["night_end"] <= n["nightly"]:
        bad.append(f"The night queue ({v['night_end']}) must end before Nightly starts ({v['nightly']}): both need the vault.")
    if not n["nightly"] + 60 <= n["organize"]:
        bad.append(f"The morning questions ({v['organize']}) must come at least an hour after Nightly ({v['nightly']}).")
    if not n["originals_stop"] <= n["organize"]:
        bad.append(f"Originals must stop ({v['originals_stop']}) before the morning questions ({v['organize']}).")
    if not n["originals"] < n["originals_stop"]:
        bad.append(f"Originals must start ({v['originals']}) before it stops ({v['originals_stop']}).")
    if not n["remember"] < n["remember_stop"]:
        bad.append(f"YouTube Remember must start ({v['remember']}) before it stops ({v['remember_stop']}).")
    if not n["signin"] < n["night_start"]:
        bad.append(f"The sign-in check ({v['signin']}) must come before the night queue ({v['night_start']}).")
    if in_ds(v["organize"]):
        bad.append(f"The morning questions ({v['organize']}) use Claude: keep them out of the Daily Summary's {a}-{b} run.")
    if n["night_start"] < ds[1] and ds[0] < n["night_end"]:
        bad.append(f"The night queue ({v['night_start']}-{v['night_end']}) overlaps the Daily Summary's {a}-{b} run.")
    if not int(v["intake_first"]) < int(v["intake_last"]):
        bad.append("Intake's first hour must come before its last hour.")
    if _m(v["night_start"]) >= 720 and int(v["intake_last"]) * 60 > _m(v["night_start"]):
        bad.append(f"Intake (until {v['intake_last']}:00) must stop before the night queue starts ({v['night_start']}).")
    if v["pause_until"] and v["pause_until"] < C.now(cfg).date().isoformat():
        bad.append("The pause date is in the past.")
    return bad


# ---------------------------------------------------------------- task triggers
def _ps(script, timeout=60):
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                       text=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def _trigger_ps(task, v):
    """PowerShell lines that build $t for a task from the values (validated: times and day names only)."""
    if task == "Intake":
        first, last = int(v["intake_first"]), int(v["intake_last"])
        at = f"{first:02d}:00"
        return (f"$t = New-ScheduledTaskTrigger -Daily -At '{at}'; "
                f"$t.Repetition = (New-ScheduledTaskTrigger -Once -At '{at}' -RepetitionInterval (New-TimeSpan -Minutes 30) "
                f"-RepetitionDuration (New-TimeSpan -Hours {last - first})).Repetition; ")
    if task == "Weekly":
        d, hm = v["weekly"].split()
        return f"$t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek {DAY_FULL[d]} -At '{hm}'; "
    sid = {"Organize": "organize", "SignInCheck": "signin", "QueueNight": "night_start", "YouTubeRemember": "remember",
           "Nightly": "nightly", "Originals": "originals"}[task]
    return f"$t = New-ScheduledTaskTrigger -Daily -At '{v[sid]}'; "


def set_trigger(cfg, task, v):
    """Set one task's trigger from the values. Returns (ok, message)."""
    tp = task_path(cfg).replace("'", "")
    script = (_trigger_ps(task, v)
              + "$t.StartBoundary = ([DateTimeOffset]$t.StartBoundary).LocalDateTime.ToString('s'); "
              + f"Set-ScheduledTask -TaskPath '{tp}' -TaskName '{task}' -Trigger $t -ErrorAction Stop | Out-Null; "
              + f"(Get-ScheduledTaskInfo -TaskPath '{tp}' -TaskName '{task}').NextRunTime.ToString('yyyy-MM-dd HH:mm')")
    try:
        code, out, err = _ps(script)
    except Exception as e:  # noqa: BLE001
        return False, f"{task}: {type(e).__name__}: {e}"
    if code != 0:
        return False, f"{task}: {(err or out).splitlines()[0] if (err or out) else 'failed'}"
    _TASKS["at"] = 0
    return True, f"{task}: next run {out}"


# ---------------------------------------------------------------- writing
def _write(conf):
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(conf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(CONFIG)


def _backup_once(cfg):
    d = C.APP / "setup" / f"_backup-{C.now(cfg):%Y%m%d}-home"
    if not (d / "vault.json").exists():
        d.mkdir(parents=True, exist_ok=True)
        (d / "vault.json").write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")


def _apply_values(conf, new):
    for sid, val in new.items():
        path = SPEC[sid][3]
        if isinstance(path[-1], int):          # queue.night_window[0|1]
            lst = list(_get(conf, path[:-1]) or [DEFAULTS["night_start"], DEFAULTS["night_end"]])
            lst[path[-1]] = val
            _set(conf, path[:-1], lst)
        elif sid == "pause_until":
            if val:
                conf["pause"] = {"until": val, "set_at": C.now().isoformat(timespec="seconds")}
            else:
                conf.pop("pause", None)
        else:
            _set(conf, path, val)


def _log(entry):
    with open(CHANGES, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def changes(n=20):
    try:
        rows = [json.loads(ln) for ln in CHANGES.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except (OSError, ValueError):
        rows = []
    return list(reversed(rows))[:n]


def change(cfg, new, via="home page", undo_of=None):
    """new: {id: typed value}. Returns (ok, messages). Only ids whose value differs are changed."""
    with _lock:
        conf = raw()
        old_all = current(conf)
        diff = {k: v for k, v in new.items() if k in SPEC and v != old_all.get(k)}
        if not diff:
            return True, ["Nothing changed."]
        merged = dict(old_all, **diff)
        problems = check(merged, cfg)
        if problems:
            return False, problems
        _backup_once(cfg)
        before = CONFIG.read_text(encoding="utf-8")
        _apply_values(conf, diff)
        _write(conf)
        msgs, done = [], []
        for task in sorted({SPEC[k][5] for k in diff if SPEC[k][5]}):
            ok, msg = set_trigger(cfg, task, merged)
            if not ok:   # roll back: config as it was, and the triggers already changed
                CONFIG.write_text(before, encoding="utf-8")
                for t in done:
                    set_trigger(cfg, t, old_all)
                return False, [f"Couldn't update the scheduled task, so nothing was changed. {msg}"]
            done.append(task)
            msgs.append(msg)
        entry = {"id": secrets.token_hex(4), "at": C.now(cfg).isoformat(timespec="seconds"), "via": via,
                 "changes": [{"id": k, "label": SPEC[k][1], "old": old_all.get(k), "new": v} for k, v in diff.items()],
                 "tasks": done}
        if undo_of:
            entry["undo_of"] = undo_of
        _log(entry)
        return True, [f"{SPEC[k][1]}: {old_all.get(k)} → {v}" for k, v in diff.items()] + msgs


def last_undoable():
    rows = changes(200)
    undone = {r.get("undo_of") for r in rows if r.get("undo_of")}
    for r in rows:
        if r.get("changes") and not r.get("undo_of") and r.get("id") not in undone:
            return r
    return None


def undo(cfg, via="home page"):
    r = last_undoable()
    if not r:
        return False, ["There is no change to undo."]
    return change(cfg, {c["id"]: c["old"] for c in r["changes"] if c["id"] in SPEC}, via + " (undo)", undo_of=r["id"])


# ---------------------------------------------------------------- run now
RUNNABLE = {"Organize": "Ask the morning questions now (rebuilds today's; same link)",
            "Intake": "Intake now (Remember email, dropbox, queue, connector requests)",
            "QueueNow": "One book or long summary now",
            "Nightly": "Nightly maintenance now",
            "Originals": "Originals into search (on Intake's next run)",
            "Resend": "Send today's update email again"}


def run_now(cfg, what, via="home page"):
    if what not in RUNNABLE:
        return False, "unknown action"
    at = C.now(cfg).isoformat(timespec="seconds")
    if what == "Originals":
        import originals
        msg = originals.request(cfg, "all")
        _log({"at": at, "via": via, "run": what})
        return True, msg
    if what == "Resend":
        import organize
        day = C.now(cfg).strftime("%Y-%m-%d")
        if not (C.APP / "reports" / f"organize-{day}.json").exists():
            return False, "There is no update for today yet."
        threading.Thread(target=organize.resend, args=(cfg, C.Log(cfg, "organize"), day, False), daemon=True).start()
        _log({"at": at, "via": via, "run": what})
        return True, "Sending today's update again; the Mail line on Status shows whether it went out."
    if what == "Organize":
        day = C.now(cfg).strftime("%Y-%m-%d")
        try:
            st = json.loads((C.APP / "reports" / f"organize-{day}.json").read_text(encoding="utf-8")).get("status")
        except (OSError, ValueError):
            st = None
        if st == "applied":
            return False, "Today's questions are already answered; anything new is asked tomorrow morning."
    info = {t["name"]: t for t in tasks_info(cfg, fresh=True)}
    if (info.get(what) or {}).get("state") == "Running":
        return False, f"{what} is already running."
    if what in ("QueueNow", "Nightly") and (info.get("QueueNight") or {}).get("state") == "Running":
        return False, "The night queue is running; try again after it ends."
    tp = task_path(cfg).replace("'", "")
    code, out, err = _ps(f"Start-ScheduledTask -TaskPath '{tp}' -TaskName '{what}' -ErrorAction Stop")
    if code != 0:
        return False, f"Couldn't start {what}: {(err or out)[:200]}"
    _TASKS["at"] = 0
    _log({"at": at, "via": via, "run": what})
    if what == "Organize":
        return True, "Building the questions now; they appear on the Today tab in a few minutes (reload it)."
    return True, f"{what} started."


# ---------------------------------------------------------------- task info (Status tab)
_TASKS = {"at": 0, "rows": []}
RESULTS = {0: "ok", 267009: "running", 267011: "not run yet", 267014: "stopped", 2147750687: "already running",
           2147946720: "not run yet"}


def tasks_info(cfg, fresh=False):
    """[{name, state, last, result, result_text, next}] for the tasks under task_path; cached 60 s."""
    if not fresh and time.time() - _TASKS["at"] < 60:
        return _TASKS["rows"]
    tp = task_path(cfg).replace("'", "")
    script = ("$r = foreach ($t in Get-ScheduledTask -TaskPath '" + tp + "') { $i = $t | Get-ScheduledTaskInfo; "
              "[pscustomobject]@{name=$t.TaskName; state=\"$($t.State)\"; "
              "last=$(if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) { $i.LastRunTime.ToString('yyyy-MM-dd HH:mm') } else { '' }); "
              "result=[int64]$i.LastTaskResult; "
              "next=$(if ($i.NextRunTime) { $i.NextRunTime.ToString('yyyy-MM-dd HH:mm') } else { '' })} }; "
              "ConvertTo-Json -InputObject @($r) -Compress")
    try:
        code, out, err = _ps(script, timeout=45)
        rows = json.loads(out) if code == 0 and out else []
    except Exception:  # noqa: BLE001
        rows = []
    for r in rows:
        res = int(r.get("result") or 0) & 0xFFFFFFFF
        r["result_text"] = RESULTS.get(res, f"error 0x{res:08X}")
        r["ok"] = res in (0, 267009, 267011, 2147946720)
    _TASKS.update(at=time.time(), rows=rows)
    return rows
