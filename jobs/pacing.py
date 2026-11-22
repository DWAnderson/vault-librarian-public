"""Overnight pacing for costly queue routes (book, summary) - added 2026-09-29.

The owner's rules (2026-09-29):
  * Spend more overnight as unused weekly usage builds up, aiming to end each week at
    pacing.weekly_goal_pct (90%) of the weekly limit.
  * Never take the 5-hour session past pacing.session_max_pct (80%), leaving room for the
    Daily Summary and the other early-morning jobs.
  * Tonight's weekly allowance = (goal - used now - daytime_pct_per_day x days to the weekly reset)
                                 / nights left before the reset (tonight included).
  * Each item is checked before it starts: its estimated share of the session and the week must fit.
    Estimates start from pacing.default_cost and are replaced by what this PC has measured
    (usage read before and after each item; usage\\usage-ledger.jsonl).
  * If usage can't be read, the old fixed limits apply (books_fallback / calls_fallback).
"""
import datetime as dt
import json
import statistics
from pathlib import Path

import common as C
import usage_probe as U

DEFAULTS = {"enabled": True, "weekly_goal_pct": 90, "session_max_pct": 80, "daytime_pct_per_day": 5,
            "books_max": 20, "books_fallback": 1, "calls_max": 400, "calls_fallback": 60,
            "night_cheap": True, "night_items_max": 80, "check_every": 5,
            "more_factor": 2.0, "light_factor": 0.25, "light_session_max": 50,
            "default_cost": {"book": {"session_per_100k_words": 20, "week_per_100k_words": 3},
                             "summary": {"session_per_100k_words": 20, "week_per_100k_words": 3},
                             "analyze": {"session_per_100k_words": 20, "week_per_100k_words": 3}},
            "min_cost": {"session": 3, "week": 0.5}}


def pcfg(cfg):
    p = json.loads(json.dumps(DEFAULTS))
    p.update(cfg.get("pacing") or {})
    return p


def words_of(item):
    return int((item.get("file") or {}).get("words") or item.get("words") or 100000)


def week_share(cfg, words, route):
    """Estimated % of the WEEKLY limit an item of `words` words uses on `route` (added 2026-10-06 for
    the confirm rule, which is now in % of the week instead of API-equivalent dollars). Uses the
    median measured rate in the usage ledger when there are 2+ measurements, else default_cost.
    No min_cost floor: this is for showing and comparing, not for pacing."""
    p = pcfg(cfg)
    rates = []
    try:
        for ln in U.ledger_path(cfg).read_text(encoding="utf-8").splitlines()[-400:]:
            r = json.loads(ln)
            if r.get("kind") == "item" and r.get("route") == route and r.get("words") and (r.get("d_week") or -1) >= 0:
                rates.append(r["d_week"] / (r["words"] / 100000))
    except (FileNotFoundError, OSError, ValueError):
        pass
    d = p["default_cost"].get(route) or p["default_cost"]["book"]
    rate = statistics.median(rates[-10:]) if len(rates) >= 2 else d["week_per_100k_words"]
    return round(rate * (words or 0) / 100000, 2)


def apply_mode(p, mode, allow, avail):
    """The owner's choice for tonight from the daily email (usage_plan.py, 2026-10-06).
    Returns (allowance, session_max_pct, analyses_at_night)."""
    if mode == "more":
        return min(avail, allow * float(p.get("more_factor", 2.0))), p["session_max_pct"], True
    if mode == "light":
        return allow * float(p.get("light_factor", 0.25)), min(p["session_max_pct"], p.get("light_session_max", 50)), False
    return allow, p["session_max_pct"], bool(p.get("night_cheap", True))


def nights_left(cfg, night_start, week_reset):
    """Night windows starting before the weekly reset, counting tonight."""
    hh, mm = map(int, night_start.split(":"))
    now = C.now(cfg)
    night = (now - dt.timedelta(hours=12)).date()           # 00:30-01:55 belongs to the evening before
    n = 0
    for d in range(0, 9):
        s = dt.datetime.combine(night + dt.timedelta(days=d), dt.time(hh, mm), tzinfo=now.tzinfo)
        if d > 0 and s >= week_reset:
            break
        n += 1
    return max(1, n)


class Night:
    def __init__(self, cfg, log, night_start, window=None):
        self.cfg, self.log, self.p = cfg, log, pcfg(cfg)
        self.window = window
        self.start = U.read(cfg) if self.p["enabled"] else {"ok": False, "why": "pacing.enabled is false"}
        self.ok = bool(self.start.get("ok"))
        self.done, self.stop_why, self.last = [], None, self.start
        if not self.ok:
            log(f"  pacing: usage unknown ({self.start.get('why')}) - fixed limits: "
                f"{self.p['books_fallback']} book(s), {self.p['calls_fallback']} model calls")
            return
        U.note(cfg, {"kind": "reading", "why": "night start", **self.start})
        now = C.now(cfg)
        self.week_reset = U.when(self.start["week_resets"], cfg) or now + dt.timedelta(days=7)
        days = max(0.0, (self.week_reset - now).total_seconds() / 86400)
        self.nights = nights_left(cfg, night_start, self.week_reset)
        avail = self.p["weekly_goal_pct"] - self.start["week_pct"] - self.p["daytime_pct_per_day"] * days
        self.allow = max(0.0, avail) / self.nights
        import usage_plan
        if window:   # The owner's extra window: up to his share of the session, within what's left of the week's goal
            self.mode, self.night_cheap = "window", True
            self.allow = max(0.0, avail)
            self.p["session_max_pct"] = int(window["session_pct"])
            log(f"  pacing: extra window {usage_plan.window_label(window)} -> up to {self.allow:.1f}% of the week")
            return
        self.mode = usage_plan.mode_for(cfg, usage_plan.next_night(cfg))
        self.allow, self.p["session_max_pct"], self.night_cheap = apply_mode(self.p, self.mode, self.allow, max(0.0, avail))
        if self.mode != "normal":
            log(f"  pacing: you chose '{usage_plan.LABEL[self.mode]}' for tonight")
        log(f"  pacing: session {self.start['session_pct']}%, week {self.start['week_pct']}% "
            f"(weekly reset {self.week_reset:%a %H:%M}, {self.nights} night(s) left) -> tonight up to "
            f"{self.allow:.1f}% of the week, session kept under {self.p['session_max_pct']}%")

    # -------------------------------------------------------------- estimates
    def estimate(self, item, route):
        w = words_of(item) / 100000
        rates = {"session": [], "week": []}
        try:
            for ln in U.ledger_path(self.cfg).read_text(encoding="utf-8").splitlines()[-400:]:
                r = json.loads(ln)
                if r.get("kind") == "item" and r.get("route") == route and r.get("words"):
                    for k in rates:
                        if r.get(f"d_{k}") is not None and r[f"d_{k}"] >= 0:
                            rates[k].append(r[f"d_{k}"] / (r["words"] / 100000))
        except FileNotFoundError:
            pass
        d = self.p["default_cost"].get(route) or self.p["default_cost"]["book"]
        out = {}
        for k in rates:
            rate = statistics.median(rates[k][-10:]) if len(rates[k]) >= 2 else d[f"{k}_per_100k_words"]
            out[k] = max(self.p["min_cost"][k], rate * w)
        out["measured"] = len(rates["session"])
        return out

    # -------------------------------------------------------------- per item
    def may_start(self, item, route):
        """(True, None) or (False, reason). Reads usage again so the check is current."""
        if self.stop_why:
            return False, self.stop_why
        if len(self.done) >= int(self.p["books_max"]):
            return False, f"pacing: books_max ({self.p['books_max']}) reached"
        now = U.read(self.cfg)
        if not now.get("ok"):
            self.stop_why = f"pacing: usage could not be read ({now.get('why')})"
            return False, self.stop_why
        self.last = now
        est = self.estimate(item, route)
        used_tonight = max(0.0, now["week_pct"] - self.start["week_pct"])
        if now["session_pct"] + est["session"] > self.p["session_max_pct"]:
            self.stop_why = (f"pacing: session at {now['session_pct']}%, this item ~{est['session']:.0f}% "
                             f"would pass {self.p['session_max_pct']}%")
            return False, self.stop_why
        if used_tonight + est["week"] > self.allow:
            self.stop_why = (f"pacing: tonight's weekly allowance ({self.allow:.1f}%) would be passed "
                             f"(used {used_tonight:.1f}%, this item ~{est['week']:.1f}%)")
            return False, self.stop_why
        item.setdefault("work", {})["usage_before"] = {k: now[k] for k in ("session_pct", "week_pct", "session_resets")}
        return True, None

    def may_continue(self):
        """For cheap items in the night run (2026-10-06): stop once tonight's weekly allowance or the
        session limit is reached. Reads usage every pacing.check_every items (a cheap item is small)."""
        if self.stop_why:
            return False, self.stop_why
        if not getattr(self, "night_cheap", True):
            return False, "pacing: tonight is set to 'Go light' (no analyses at night)"
        self._cheap_n = getattr(self, "_cheap_n", 0) + 1
        if (self._cheap_n - 1) % max(1, int(self.p.get("check_every", 5))):
            return True, None
        now = U.read(self.cfg)
        if not now.get("ok"):
            self.stop_why = f"pacing: usage could not be read ({now.get('why')})"
            return False, self.stop_why
        self.last = now
        used = max(0.0, now["week_pct"] - self.start["week_pct"])
        if now["session_pct"] >= self.p["session_max_pct"]:
            self.stop_why = f"pacing: session at {now['session_pct']}% (limit {self.p['session_max_pct']}%)"
        elif used >= self.allow:
            self.stop_why = f"pacing: tonight's weekly allowance ({self.allow:.1f}%) used"
        return (False, self.stop_why) if self.stop_why else (True, None)

    def finished(self, item, route):
        b = (item.get("work") or {}).pop("usage_before", None)
        a = U.read(self.cfg)
        self.done.append(item.get("title") or "?")
        if not (a.get("ok") and b):
            return
        self.last = a
        rec = {"kind": "item", "at": a["at"], "route": route, "title": item.get("title"), "words": words_of(item),
               "d_week": round(a["week_pct"] - b["week_pct"], 2)}
        if a.get("session_resets") == b.get("session_resets"):   # a session reset mid-item spoils the delta
            rec["d_session"] = round(a["session_pct"] - b["session_pct"], 2)
        U.note(self.cfg, rec)

    def summary(self):
        if not self.ok:
            return {"ok": False, "why": self.start.get("why"), "done": self.done}
        return {"ok": True, "start": {k: self.start[k] for k in ("session_pct", "week_pct")},
                "end": {k: self.last[k] for k in ("session_pct", "week_pct")},
                "week_reset": self.week_reset.isoformat(timespec="minutes"), "nights_left": self.nights,
                "allowance": round(self.allow, 1), "mode": getattr(self, "mode", "normal"),
                "done": self.done, "stopped": self.stop_why}


def session_busy(cfg):
    """For daytime (cheap) runs: reason string if the 5-hour session is already past the limit, else None.
    Never refreshes the sign-in (no extra model call); unknown usage never blocks."""
    p = pcfg(cfg)
    if not p["enabled"]:
        return None
    r = U.read(cfg, allow_refresh=False)
    if r.get("ok") and r["session_pct"] >= p["session_max_pct"]:
        return f"session at {r['session_pct']}% (over {p['session_max_pct']}%)"
    return None
