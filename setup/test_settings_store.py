"""Test: home page settings (jobs/settings_store.py; plan 2026-11-12 Home page, sections 6-7).

Works on a copy of config\\vault.json in a temp folder and a throwaway scheduled task (\\VaultLibrarianTest\\Probe,
removed at the end); the real config and the real tasks are never touched.
  - the current values check clean; bad times and wrong night order are refused
  - a change writes the config, logs it, and can be undone
  - a failed trigger update rolls the config back
  - set_trigger really changes a task's trigger (plain local time, daily / weekly / Intake repetition)
Run: python setup\\test_settings_store.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jobs"))
import common as C  # noqa: E402
import settings_store as SS  # noqa: E402

fails = 0


def check(cond, what):
    global fails
    print(("ok   " if cond else "FAIL ") + what)
    fails += 0 if cond else 1


real = SS.CONFIG
tmp = Path(tempfile.mkdtemp())
SS.CONFIG = tmp / "vault.json"
shutil.copy(real, SS.CONFIG)
SS.CHANGES = tmp / "changes.jsonl"
C.APP = tmp                      # backups go to tmp/setup
cfg = json.loads(SS.CONFIG.read_text(encoding="utf-8"))

cur = SS.current()
check(SS.check(cur, cfg) == [], f"current values pass the checks ({SS.check(cur, cfg)})")
for sid, val, why in (("nightly", "23:00", "Nightly before the night queue ends"),
                      ("organize", "05:00", "Organize inside the Daily Summary slot"),
                      ("night_end", "05:00", "night queue overlapping the Daily Summary"),
                      ("intake_last", 24, "Intake running into the night queue")):
    check(SS.check(dict(cur, **{sid: val}), cfg) != [], f"refused: {why}")
for sid, text in (("organize", "6am"), ("weekly", "Funday 03:00"), ("weekly_goal_pct", "150")):
    try:
        SS.parse(sid, text)
        check(False, f"parse refuses {sid}={text}")
    except ValueError:
        check(True, f"parse refuses {sid}={text}")
check(SS.parse("weekly", "sunday 4:15") == "Sun 04:15", "parse 'sunday 4:15'")

calls = []
SS.set_trigger = lambda cfg_, task, v: (calls.append((task, dict(v))) or True, f"{task}: next run x")
ok, msgs = SS.change(cfg, {"organize": "06:30", "weekly_goal_pct": 85}, "test")
conf = json.loads(SS.CONFIG.read_text(encoding="utf-8"))
check(ok and conf["schedule"]["Organize"] == "06:30" and conf["pacing"]["weekly_goal_pct"] == 85, "change written")
check([c[0] for c in calls] == ["Organize"], "only Organize's trigger updated")
check((tmp / "setup").exists() and any((tmp / "setup").rglob("vault.json")), "config backed up before the first change")
ok, msgs = SS.change(cfg, {"night_start": "23:30"}, "test")
conf = json.loads(SS.CONFIG.read_text(encoding="utf-8"))
check(ok and conf["queue"]["night_window"] == ["23:30", "01:55"], "night window start changed in its list")
ok, msgs = SS.undo(cfg, "test")
conf = json.loads(SS.CONFIG.read_text(encoding="utf-8"))
check(ok and conf["queue"]["night_window"][0] == "23:45", "undo puts the night start back")
ok, msgs = SS.undo(cfg, "test")
conf = json.loads(SS.CONFIG.read_text(encoding="utf-8"))
check(ok and conf["schedule"]["Organize"] == "06:00" and conf["pacing"]["weekly_goal_pct"] == cur["weekly_goal_pct"],
      "second undo puts the first change back")
ok, msgs = SS.undo(cfg, "test")
check(not ok, "nothing left to undo")
ok, msgs = SS.change(cfg, {"pause_until": "2099-01-01"}, "test")
conf = json.loads(SS.CONFIG.read_text(encoding="utf-8"))
check(ok and conf["pause"]["until"] == "2099-01-01" and C.paused(conf) == "2099-01-01", "pause set and seen by C.paused")
SS.change(cfg, {"pause_until": ""}, "test")
check("pause" not in json.loads(SS.CONFIG.read_text(encoding="utf-8")), "pause cleared")

before = SS.CONFIG.read_text(encoding="utf-8")
SS.set_trigger = lambda cfg_, task, v: (False, f"{task}: access denied")
ok, msgs = SS.change(cfg, {"nightly": "02:40"}, "test")
check(not ok and SS.CONFIG.read_text(encoding="utf-8") == before, "failed trigger: config rolled back")

# a real trigger on a throwaway task
import importlib  # noqa: E402
importlib.reload(SS)
tcfg = {"schedule": {"task_path": "\\VaultLibrarianTest\\"}}
ps = lambda s: subprocess.run(["powershell", "-NoProfile", "-Command", s], capture_output=True, text=True)  # noqa: E731
reg = ps("$a = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c exit 0'; "
         "Register-ScheduledTask -TaskPath '\\VaultLibrarianTest\\' -TaskName Probe -Action $a -Force | Out-Null; 'ok'")
if "ok" not in reg.stdout:
    check(False, f"couldn't make the throwaway task: {reg.stderr[:200]}")
else:
    try:
        v = dict(cur, organize="06:10", weekly="Sat 04:20", intake_first=8, intake_last=22)
        for task, expect in (("Organize", "T06:10:00"), ("Weekly", "T04:20:00"), ("Intake", "T08:00:00")):
            SS._trigger_ps.__defaults__  # noqa: B018
            orig = SS._trigger_ps
            SS._trigger_ps = lambda t, vv, _task=task: orig(_task, vv)
            ok, msg = SS.set_trigger(tcfg, "Probe", v)
            SS._trigger_ps = orig
            out = ps("$t = (Get-ScheduledTask -TaskPath '\\VaultLibrarianTest\\' -TaskName Probe).Triggers[0]; "
                     "$t.StartBoundary + ' ' + $t.Repetition.Interval + ' ' + $t.Repetition.Duration + ' ' + $t.DaysOfWeek").stdout.strip()
            good = ok and expect in out and "+" not in out.split()[0][19:] and "Z" not in out.split()[0]
            if task == "Intake":
                good = good and "PT30M" in out and "PT14H" in out
            if task == "Weekly":
                good = good and out.endswith(" 64")
            check(good, f"real trigger like {task}: {out} ({msg})")
    finally:
        ps("Unregister-ScheduledTask -TaskPath '\\VaultLibrarianTest\\' -TaskName Probe -Confirm:$false; "
           "$s = New-Object -ComObject Schedule.Service; $s.Connect(); $s.GetFolder('\\').DeleteFolder('VaultLibrarianTest', 0)")
        gone = "Probe" not in ps("Get-ScheduledTask -TaskPath '\\VaultLibrarianTest\\' -ErrorAction SilentlyContinue | % TaskName").stdout
        check(gone, "throwaway task removed")

info = SS.tasks_info({"schedule": {}}, fresh=True)
check(any(r["name"] == "Nightly" for r in info), f"tasks_info reads the real tasks ({len(info)} found)")
shutil.rmtree(tmp, ignore_errors=True)
print("\nall passed" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
