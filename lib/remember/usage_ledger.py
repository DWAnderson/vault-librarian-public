"""Gemini usage ledger for the Remember job (Vault Librarian).

COPY, adapted, of the Daily Summary's platform/digest-core/gemini_usage.py, taken 2026-09-26.
The apps keep separate copies on purpose; when you change one, consider the same change in
the other (see README.md "Code kept in step with the Daily Summary").

File: %LOCALAPPDATA%\\VaultLibrarian\\gemini_usage.json (this PC only).
Project: "obsidian_youtube" = Google AI project "Obsidian Youtube Summary"
(gen-lang-client-0779526173), free tier, key %LOCALAPPDATA%\\VaultLibrarian\\credentials\\gemini_api_key.txt.
Remember is its only user, so this ledger is Remember's whole view of that project's day:
video seconds watched (free tier allows 8 h/day), requests and 429s per model.

Quota day = the Pacific calendar date (free-tier quotas reset at midnight Pacific = 2:00 AM
Central). Every public function swallows its own errors.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
CENTRAL = ZoneInfo("America/Chicago")
LOCAL = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "VaultLibrarian"
PATH = LOCAL / "gemini_usage.json"
LOCK = PATH.with_suffix(".json.lock")
KEEP_DAYS = 21
LOCK_WAIT_S = 10.0
LOCK_STALE_S = 60.0

# Free-tier YouTube video allowance per project per day (docs: "8 hours of YouTube video per day").
FREE_VIDEO_SECONDS_PER_DAY = 8 * 3600

PROJECTS = {
    "obsidian_youtube": {"name": "Obsidian Youtube Summary", "id": "gen-lang-client-0779526173", "tier": "free"},
}


# ---------------------------------------------------------------- time
def quota_day(now: datetime | None = None) -> str:
    """The quota day a moment belongs to: its Pacific calendar date, 'YYYY-MM-DD'."""
    now = now or datetime.now(PACIFIC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CENTRAL)
    return now.astimezone(PACIFIC).date().isoformat()


def next_reset(now: datetime | None = None) -> datetime:
    """Next free-tier quota reset (midnight Pacific), returned in Central time."""
    now = (now or datetime.now(PACIFIC)).astimezone(PACIFIC)
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=PACIFIC)
    return midnight.astimezone(CENTRAL)


# ---------------------------------------------------------------- 429 scope
_DAY = re.compile(r"PerDay|per[_ ]day", re.I)
_MIN = re.compile(r"PerMinute|per[_ ]minute", re.I)


def is_quota_error(e: BaseException | str) -> bool:
    m = str(e).lower()
    return "resource_exhausted" in m or "429" in m or ("quota" in m and "exceed" in m)


def quota_scope(e: BaseException | str) -> str | None:
    """For a 429: 'day' if a daily limit was hit, 'minute' if only a per-minute limit was,
    None if the error carries no quota detail (treat as unknown)."""
    s = str(e)
    if not is_quota_error(s):
        return None
    if _DAY.search(s):
        return "day"
    if _MIN.search(s):
        return "minute"
    return None


def retry_delay_seconds(e: BaseException | str, default: float = 30.0) -> float:
    """The server's suggested wait for a per-minute 429 ('retryDelay': '29s' / 'retry in 29.3s')."""
    m = re.search(r"retry(?:Delay'?:\s*'|\s+in\s+)(\d+(?:\.\d+)?)s", str(e), re.I)
    return float(m.group(1)) + 1.0 if m else default


# ---------------------------------------------------------------- file I/O
class _Lock:
    def __enter__(self):
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        while True:
            try:
                fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.ok = True
                return self
            except FileExistsError:
                try:
                    if time.time() - LOCK.stat().st_mtime > LOCK_STALE_S:
                        LOCK.unlink()
                        continue
                except OSError:
                    pass
                if time.time() - t0 > LOCK_WAIT_S:
                    self.ok = False          # give up on the lock; write anyway (best effort)
                    return self
                time.sleep(0.1)

    def __exit__(self, *a):
        if getattr(self, "ok", False):
            try:
                LOCK.unlink()
            except OSError:
                pass


def _load() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    days = sorted(k for k in d if re.fullmatch(r"\d{4}-\d{2}-\d{2}", k))
    for old in days[:-KEEP_DAYS]:
        d.pop(old, None)
    tmp = PATH.with_suffix(f".json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(d, indent=1, sort_keys=True), encoding="utf-8")
    for _ in range(20):
        try:
            os.replace(tmp, PATH)
            return
        except PermissionError:          # a reader has it open (Windows) -- retry briefly
            time.sleep(0.1)
    os.replace(tmp, PATH)


def _entry(d: dict, day: str, project: str) -> dict:
    e = d.setdefault(day, {}).setdefault(project, {})
    e.setdefault("video_seconds", 0)
    for k in ("requests", "errors", "exhausted", "sources", "limit_429"):
        e.setdefault(k, {})
    return e


def _update(project: str, fn) -> None:
    try:
        with _Lock():
            d = _load()
            e = _entry(d, quota_day(), project)
            fn(e)
            e["updated"] = datetime.now(CENTRAL).isoformat(timespec="seconds")
            _save(d)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- writers
def record(project: str | None, source: str, model: str | None = None, *,
           requests: int = 1, video_seconds: int = 0, ok: bool = True) -> None:
    """Record calls that reached a model and/or video watched. No-op if project is None."""
    if not project:
        return

    def fn(e):
        src = e["sources"].setdefault(source, {"requests": 0, "video_seconds": 0})
        if model and requests:
            e["requests"][model] = e["requests"].get(model, 0) + requests
            if not ok:
                e["errors"][model] = e["errors"].get(model, 0) + requests
            src["requests"] += requests
        if video_seconds:
            e["video_seconds"] += int(video_seconds)
            src["video_seconds"] += int(video_seconds)
    _update(project, fn)


def mark_exhausted(project: str | None, model: str, source: str = "") -> None:
    """A DAILY-quota 429 was seen for this model: it is out until the next reset."""
    if not project or not model:
        return

    def fn(e):
        e["exhausted"].setdefault(model, datetime.now(CENTRAL).isoformat(timespec="seconds")
                                  + (f" ({source})" if source else ""))
    _update(project, fn)


_QID = re.compile(r"quotaId'?\"?:\s*'?\"?([A-Za-z0-9_-]+)")


def note_429(project: str | None, model: str, e: BaseException | str, source: str = "") -> None:
    """Count a 429 by the limit that caused it (day / minute / unknown), keeping the quota IDs."""
    if not project or not model:
        return
    scope = quota_scope(e) or "unknown"
    ids = sorted(set(_QID.findall(str(e))))

    def fn(ent):
        m = ent["limit_429"].setdefault(model, {"day": 0, "minute": 0, "unknown": 0, "quota_ids": []})
        m[scope] = m.get(scope, 0) + 1
        m["quota_ids"] = sorted(set(m.get("quota_ids", [])) | set(ids))
        if source:
            m.setdefault("sources", {})
            m["sources"][source] = m["sources"].get(source, 0) + 1
    _update(project, fn)


# ---------------------------------------------------------------- readers
def day_entry(project: str, day: str | None = None) -> dict:
    try:
        return (_load().get(day or quota_day()) or {}).get(project) or {}
    except Exception:  # noqa: BLE001
        return {}


def video_seconds(project: str, day: str | None = None) -> int | None:
    """Video seconds recorded today for a project; None if the ledger can't be read."""
    try:
        d = _load() if PATH.exists() else {}
        return int(((d.get(day or quota_day()) or {}).get(project) or {}).get("video_seconds", 0))
    except Exception:  # noqa: BLE001
        return None


def exhausted_models(project: str, day: str | None = None) -> set[str]:
    return set((day_entry(project, day).get("exhausted") or {}).keys())


def requests_today(project: str, model: str, day: str | None = None) -> int:
    return int((day_entry(project, day).get("requests") or {}).get(model, 0))


if __name__ == "__main__":   # quick look: python gemini_usage.py
    print(f"quota day {quota_day()}  next reset {next_reset():%Y-%m-%d %H:%M %Z}")
    print(json.dumps(_load().get(quota_day(), {}), indent=1))
