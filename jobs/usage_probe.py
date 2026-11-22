#!/usr/bin/env python3
"""Read the owner's Claude plan usage (the numbers on Settings > Usage) for overnight pacing.

  python usage_probe.py          print the current reading and add it to the usage ledger

Source: the usage endpoint Claude Code itself calls (api.anthropic.com/api/oauth/usage), with the
Claude Code sign-in stored on this PC (%USERPROFILE%\\.claude\\.credentials.json). Anthropic doesn't
document it, so it can change; every caller treats a failed read as "unknown" and falls back to the
fixed limits in config (pacing.books_fallback / calls_fallback). The token is never logged.
If the stored token has expired, one tiny `claude -p` call (Haiku) lets Claude Code refresh it.

Reading: {"ok": True, "session_pct", "session_resets", "week_pct", "week_resets", "at"} (pct 0-100,
resets as ISO strings) or {"ok": False, "why": "..."}. Added 2026-09-29 (overnight pacing).
"""
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import claude_cli  # noqa: E402

URL = "https://api.anthropic.com/api/oauth/usage"
CREDS = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".claude" / ".credentials.json"


def _token():
    try:
        o = json.loads(CREDS.read_text(encoding="utf-8")).get("claudeAiOauth") or {}
    except Exception as e:
        return None, None, f"no Claude Code sign-in file ({type(e).__name__})"
    return o.get("accessToken"), o.get("expiresAt"), None


def _refresh():
    """One minimal Claude Code call so it refreshes its own sign-in."""
    if not claude_cli.EXE.exists():
        return
    try:
        p = subprocess.run([str(claude_cli.EXE), "-p", "Reply with OK.", "--model", "haiku", "--output-format", "json",
                            *claude_cli.lean()], input="", capture_output=True, text=True, timeout=90,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        claude_cli.log_raw(p.stdout, "_refresh", model="haiku")   # usage ledger (2026-11-13)
    except Exception:
        pass


def _get(tok):
    req = urllib.request.Request(URL, headers={"Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20",
                                               "Content-Type": "application/json", "User-Agent": "vault-librarian"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _pct(v):
    """The endpoint reports percent used (e.g. 32.0); checked against Settings > Usage on first run."""
    return None if v is None else round(float(v), 1)


def read(cfg=None, allow_refresh=True):
    """Interactive login first; if that is missing or refused, the long-lived setup-token (2026-10-24)."""
    r = _read_login(cfg, allow_refresh)
    if r.get("ok"):
        return r
    try:
        import claude_auth
        lt = claude_auth.token(cfg)
    except Exception:  # noqa: BLE001
        lt = None
    if not lt:
        return r
    try:
        j = _get(lt)
    except urllib.error.HTTPError as e:
        return {"ok": False, "why": f"{r.get('why')}; long-lived token: usage endpoint HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "why": f"{r.get('why')}; long-lived token: {type(e).__name__}"}
    return _parse(j, cfg)


def _parse(j, cfg):
    fh, sd = j.get("five_hour") or {}, j.get("seven_day") or {}
    if fh.get("utilization") is None or sd.get("utilization") is None:
        return {"ok": False, "why": "usage endpoint answered without five_hour/seven_day figures", "keys": sorted(j)}
    return {"ok": True, "at": C.now(cfg).isoformat(timespec="seconds"),
            "session_pct": _pct(fh["utilization"]), "session_resets": fh.get("resets_at"),
            "week_pct": _pct(sd["utilization"]), "week_resets": sd.get("resets_at")}


def _read_login(cfg=None, allow_refresh=True):
    tok, exp, why = _token()
    if not tok:
        return {"ok": False, "why": why or "no access token in the Claude Code sign-in file"}
    if allow_refresh and exp and float(exp) / 1000 < time.time() + 120:
        _refresh()
        tok, exp, why = _token()
    try:
        j = _get(tok)
    except urllib.error.HTTPError as e:
        if e.code == 401 and allow_refresh:
            _refresh()
            return _read_login(cfg, allow_refresh=False)
        return {"ok": False, "why": f"usage endpoint HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "why": f"usage endpoint: {type(e).__name__}: {str(e)[:120]}"}
    fh, sd = j.get("five_hour") or {}, j.get("seven_day") or {}
    if fh.get("utilization") is None or sd.get("utilization") is None:
        return {"ok": False, "why": "usage endpoint answered without five_hour/seven_day figures", "keys": sorted(j)}
    return {"ok": True, "at": C.now(cfg).isoformat(timespec="seconds"),
            "session_pct": _pct(fh["utilization"]), "session_resets": fh.get("resets_at"),
            "week_pct": _pct(sd["utilization"]), "week_resets": sd.get("resets_at")}


def when(iso, cfg=None):
    """ISO reset time -> aware datetime in the vault's time zone (None if missing)."""
    if not iso:
        return None
    d = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    return d.astimezone(C.now(cfg).tzinfo)


def ledger_path(cfg):
    p = Path(cfg["local_root"]) / "usage"
    p.mkdir(parents=True, exist_ok=True)
    return p / "usage-ledger.jsonl"


def note(cfg, rec):
    with open(ledger_path(cfg), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    cfg = C.load_config()
    r = read(cfg)
    if r.get("ok"):
        note(cfg, {"kind": "reading", **r})
        print(f"OK  session {r['session_pct']}% (resets {when(r['session_resets'], cfg):%a %H:%M})   "
              f"week {r['week_pct']}% (resets {when(r['week_resets'], cfg):%a %H:%M})")
    else:
        print("FAILED:", r)
    sys.exit(0 if r.get("ok") else 1)
