"""Claude usage forecast and tonight's mode for the daily vault email and web page (2026-10-06).

  forecast(cfg)            -> dict for the "Claude usage" block: week/session %, reset, tonight's allowance,
                              what tonight would run (books with estimated weekly share, analyses), the week's
                              projected finish against pacing.weekly_goal_pct, and warnings.
  set_mode(cfg, mode, via) -> the owner's choice for the NEXT night window: "normal", "more" or "light".
  mode_for(cfg, night)     -> the mode stored for that night (pacing.Night reads it).

Modes (applied by pacing.Night):
  more   tonight may spend up to pacing.more_factor x the usual allowance (default 2x; never past the goal
         minus the daytime reserve for the days left), books_max/session limit unchanged.
  light  tonight's allowance x pacing.light_factor (default 0.25), session kept under pacing.light_session_max
         (default 50%), and no analyses at night - for a day when the owner expects to need Claude himself.
The mode lasts one night; it is kept in queue\\usage-mode.json (synced, so the laptop can see it).
"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

MODES = ("normal", "more", "light")
LABEL = {"normal": "Normal", "more": "Use more tonight", "light": "Go light tonight"}


def _path():
    return C.APP / "queue" / "usage-mode.json"


def next_night(cfg, now=None):
    """Key (YYYY-MM-DD of the evening it starts) of the night window now running, or the next one."""
    now = now or C.now(cfg)
    a, b = ((cfg.get("queue") or {}).get("night_window") or ["23:45", "01:55"])
    hm = f"{now:%H:%M}"
    if a > b and hm <= b:                      # after midnight, inside (or before the end of) last evening's window
        return f"{now - dt.timedelta(days=1):%Y-%m-%d}"
    return f"{now:%Y-%m-%d}"


def mode_for(cfg, night):
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "normal"
    return d.get("mode", "normal") if d.get("night") == night and d.get("mode") in MODES else "normal"


def set_mode(cfg, mode, via="web"):
    if mode not in MODES:
        raise ValueError(mode)
    night = next_night(cfg)
    rec = {"night": night, "mode": mode, "set_at": C.now(cfg).isoformat(timespec="seconds"), "via": via}
    p = _path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    tmp.replace(p)
    return rec


# ---------------------------------------------------------------- extra run windows (2026-10-06)
# Besides the night run, the owner can book a window (e.g. tomorrow 2-7 AM) in which this PC works through the queue until
# the 5-hour session reaches his percentage (and never past the week's goal). The QueueWindow task (every 15 min)
# runs queue_run.py --window, which exits at once unless a window is active. Kept in queue/usage-windows.json.
DAILY_SUMMARY = ("04:45", "05:30")     # the Daily Summary app's morning run (approximate); config schedule.daily_summary_slot wins


def ds_slot():
    try:
        s = (C.load_config().get("schedule") or {}).get("daily_summary_slot")
        return tuple(s) if s and len(s) == 2 else DAILY_SUMMARY
    except Exception:  # noqa: BLE001
        return DAILY_SUMMARY


def _wpath():
    return C.APP / "queue" / "usage-windows.json"


def windows(cfg, include_past=False):
    try:
        ws = json.loads(_wpath().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ws = []
    now = C.now(cfg)
    out = []
    for w in ws:
        try:
            if include_past or dt.datetime.fromisoformat(w["end"]) > now:
                out.append(w)
        except (KeyError, ValueError):
            continue
    return sorted(out, key=lambda w: w["start"])


def _save_windows(ws):
    p = _wpath()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(ws, indent=1), encoding="utf-8")
    tmp.replace(p)


def next_at(cfg, hhmm):
    """The next time-of-day hhmm (within 24 h)."""
    now = C.now(cfg)
    h, m = map(int, hhmm.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t > now else t + dt.timedelta(days=1)


def add_window(cfg, start, hours, pct, via="web"):
    hours = max(0.5, min(5.0, float(hours)))
    pct = max(10, min(95, int(pct)))
    end = start + dt.timedelta(hours=hours)
    ws = [w for w in windows(cfg) if not (w["start"] < end.isoformat() and w["end"] > start.isoformat())]   # replace overlaps
    w = {"start": start.isoformat(timespec="minutes"), "end": end.isoformat(timespec="minutes"), "session_pct": pct,
         "set_at": C.now(cfg).isoformat(timespec="seconds"), "via": via}
    ws.append(w)
    _save_windows(sorted(ws, key=lambda x: x["start"]))
    return w


def cancel_window(cfg, start_iso):
    ws = [w for w in windows(cfg) if w["start"] != start_iso]
    _save_windows(ws)


def active_window(cfg, now=None):
    now = now or C.now(cfg)
    for w in windows(cfg):
        if dt.datetime.fromisoformat(w["start"]) <= now < dt.datetime.fromisoformat(w["end"]):
            return w
    return None


def window_label(w):
    a, b = dt.datetime.fromisoformat(w["start"]), dt.datetime.fromisoformat(w["end"])
    day = a.strftime("%a")
    fmt = lambda d: d.strftime("%I:%M %p").lstrip("0").replace(":00", "")
    return f"{day} {fmt(a)}–{fmt(b)}, up to {w['session_pct']}% of the session"


def window_warning(w):
    a, b = dt.datetime.fromisoformat(w["start"]), dt.datetime.fromisoformat(w["end"])
    s0 = ds_slot()[0]
    ds = a.replace(hour=int(s0[:2]), minute=int(s0[3:]))
    for d in (ds, ds + dt.timedelta(days=1)):
        if a <= d < b and w["session_pct"] > 80:
            return ("The Daily Summary runs around 5 AM, inside this window; at "
                    f"{w['session_pct']}% it may be short of room. 80% leaves it space.")
    return ""


def write_snapshot(cfg, fc=None):
    """queue\\usage-forecast.json: what the laptop's "process the vault queue" shows (it can't read usage itself)."""
    try:
        fc = fc or forecast(cfg)
        fc["snapshot_at"] = C.now(cfg).isoformat(timespec="minutes")
        p = C.APP / "queue" / "usage-forecast.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(fc, indent=1, default=str), encoding="utf-8")
        tmp.replace(p)
    except Exception:  # noqa: BLE001
        pass


def _book_share(cfg, item, route):
    import pacing
    return pacing.week_share(cfg, pacing.words_of(item), route)


def forecast(cfg, usage=None):
    """Everything the usage block shows. Never raises; {'ok': False, 'why': ...} when usage can't be read."""
    import pacing
    import usage_probe as U
    import vqueue as Q
    p = pacing.pcfg(cfg)
    u = usage or U.read(cfg, allow_refresh=False)
    out = {"ok": bool(u.get("ok")), "why": u.get("why"), "mode": mode_for(cfg, next_night(cfg)),
           "night": next_night(cfg), "goal": p["weekly_goal_pct"], "warnings": []}
    try:
        store = Q.Store(cfg)
        items = store.all()
    except Exception as e:  # noqa: BLE001
        items = []
        out["warnings"].append(f"queue could not be read ({type(e).__name__})")
    ready = Q.ordered(items, cfg, statuses=("ready", "failed", "working")) if items else []
    books = [x for x in ready if Q.route_of(x) in Q.COSTLY]
    cheap = [x for x in ready if Q.route_of(x) not in Q.COSTLY]
    confirm = [x for x in items if x.get("status") == "held" and x.get("needs") == "confirm"]
    long_held = [x for x in items if x.get("status") == "held" and "too long for one call" in str(x.get("why"))]
    out["books"] = [(x.get("title") or "?", _book_share(cfg, x, Q.route_of(x))) for x in books]
    out["n_cheap"] = len(cheap)
    out["cheap_share"] = round(sum(((x.get("estimate") or {}).get("week_pct") or pacing.week_share(cfg, 10000, "analyze"))
                                   for x in cheap), 1)
    out["n_confirm"], out["n_long_held"] = len(confirm), len(long_held)
    if not u.get("ok"):
        return out
    now = C.now(cfg)
    reset = U.when(u.get("week_resets"), cfg) or now + dt.timedelta(days=7)
    days = max(0.0, (reset - now).total_seconds() / 86400)
    nights = pacing.nights_left(cfg, (cfg.get("queue") or {}).get("night_window", ["23:45"])[0], reset)
    avail = max(0.0, p["weekly_goal_pct"] - u["week_pct"] - p["daytime_pct_per_day"] * days)
    allow = avail / max(1, nights)
    allow = pacing.apply_mode(p, out["mode"], allow, avail)[0]
    # what tonight would run: books in queue order within the allowance and books_max, then analyses
    plan, used = [], 0.0
    for t, s in out["books"]:
        if len(plan) >= int(p["books_max"]) or used + s > allow:
            break
        plan.append((t, s))
        used += s
    cheap_tonight = 0
    if p.get("night_cheap", True) and out["mode"] != "light":
        per = (out["cheap_share"] / len(cheap)) if cheap else 0
        while cheap_tonight < len(cheap) and cheap_tonight < int(p.get("night_items_max", 80)) and used + per <= allow:
            cheap_tonight += 1
            used += per
    work_total = sum(s for _, s in out["books"]) + out["cheap_share"]
    proj = u["week_pct"] + p["daytime_pct_per_day"] * days + min(avail, work_total)
    out.update(week_pct=u["week_pct"], session_pct=u["session_pct"], reset=reset.isoformat(timespec="minutes"),
               days_left=round(days, 1), nights_left=nights, allowance=round(allow, 1), tonight_books=plan,
               tonight_cheap=cheap_tonight, tonight_share=round(used, 1), projected=round(min(100.0, proj), 0),
               work_total=round(work_total, 1))
    out["windows"] = [dict(w, label=window_label(w), warning=window_warning(w)) for w in windows(cfg)]
    if work_total + 0.5 < avail:
        out["warnings"].append(
            f"The queue holds about {work_total:.0f}% of a week's work, less than the {avail:.0f}% left to use before the "
            f"reset: add items to Remember, answer the held questions, or approve long documents.")
    if confirm:
        out["warnings"].append(f"{len(confirm)} item(s) wait for your OK below before they can use any of it.")
    if long_held:
        out["warnings"].append(f"{len(long_held)} long document(s) are held for the long-document analysis step.")
    try:
        last = json.loads((C.APP / "reports" / f"pacing-{(now - dt.timedelta(days=1)):%Y-%m-%d}.json").read_text(encoding="utf-8"))
        if last.get("stopped"):
            out["warnings"].append(f"Last night stopped early: {last['stopped']}.")
    except (OSError, ValueError):
        pass
    return out


if __name__ == "__main__":   # laptop / this PC command line: mode more|light|normal ; window HH:MM HOURS PCT ; cancel START_ISO ; show
    import sys as _s
    cfg = C.load_config()
    a = _s.argv[1:]
    if a[:1] == ["mode"]:
        print(set_mode(cfg, a[1], "command line"))
    elif a[:1] == ["window"]:
        print(add_window(cfg, next_at(cfg, a[1]), a[2], a[3], "command line"))
    elif a[:1] == ["cancel"]:
        cancel_window(cfg, a[1]); print("cancelled")
    else:
        print(json.dumps({"mode": mode_for(cfg, next_night(cfg)), "windows": windows(cfg)}, indent=1))
