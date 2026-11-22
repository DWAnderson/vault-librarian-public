#!/usr/bin/env python3
"""Claude sign-in health for the the app's jobs (added 2026-10-24, after the 06:00 organize run failed on an
expired Claude Code login: "OAuth session expired and could not be refreshed").

  python claude_auth.py check      21:00 task: one tiny Haiku call; email the owner if the sign-in is broken
  python claude_auth.py recover    (QueueWindow, every 15 min) only while broken: re-test, then catch up
  python claude_auth.py status     print the stored state

How the jobs sign in:
  * Preferred: a long-lived token from `claude setup-token`, saved Windows-encrypted (DPAPI, the owner's login
    only) at %LOCALAPPDATA%\\VaultLibrarian\\credentials\\claude_oauth_token.dpapi. claude_cli passes it to
    claude.exe as CLAUDE_CODE_OAUTH_TOKEN, so the jobs no longer depend on the interactive login, which
    lapsed after about a month (first login ~2026-09-25, failed 2026-10-24).
  * Fallback: the interactive Claude Code login (%USERPROFILE%\\.claude\\.credentials.json).

When a call fails on sign-in (classify() -> "auth"): claude_cli records it here; llm.run treats it like a
usage limit (items wait, not failed); organize records a "missed" run; one clear email goes out (at most
every 12 h while broken). Once a call works again the state flips back, and `recover` re-runs a missed
organize prep for today. State: <app>\\queue\\claude-signin.json (shown by the Vault connector's status).
The token is never logged or written anywhere else.
"""
import ctypes
import datetime as dt
import json
import os
import re
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

STATE = C.APP / "queue" / "claude-signin.json"
TAG = "Vault sign-in"
AUTH_RE = re.compile(r"failed to authenticate|oauth|invalid (?:api key|bearer|token|x-api-key)|authentication_error"
                     r"|not logged in|please run /login|/login|credentials? (?:expired|invalid)|401", re.I)
RENOTIFY_HOURS = 12
TOKEN_WARN_DAYS = 330          # setup-token tokens are long-lived (about a year); warn well before


def token_file(cfg=None):
    root = Path((cfg or {}).get("local_root") or os.path.expandvars(r"%LOCALAPPDATA%\VaultLibrarian"))
    return root / "credentials" / "claude_oauth_token.dpapi"


def _dpapi_decrypt(blob):
    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(blob, len(blob))
    src, dst = BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst)):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def token(cfg=None):
    """The long-lived token, or None. File = hex from PowerShell ConvertFrom-SecureString (UTF-16LE inside)."""
    p = token_file(cfg)
    try:
        raw = _dpapi_decrypt(bytes.fromhex(p.read_text(encoding="ascii").strip()))
        t = raw.decode("utf-16-le").strip()
        return t or None
    except Exception:  # noqa: BLE001  (missing, unreadable, or written by another Windows user)
        return None


def env(cfg=None):
    """Environment for claude.exe: the long-lived token when one is saved."""
    e = dict(os.environ)
    t = token(cfg)
    if t:
        e["CLAUDE_CODE_OAUTH_TOKEN"] = t
    return e


def classify(err):
    return "auth" if err and AUTH_RE.search(str(err)) else None


# ---------------------------------------------------------------- state
def load():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"ok": True}


def _save(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1), encoding="utf-8")
    os.replace(tmp, STATE)


def _cfg(cfg):
    return cfg or C.load_config()


def record_failure(err, job=None, cfg=None, missed=None, notify=True):
    """A Claude call failed on sign-in. missed: e.g. "organize" -> caught up by recover() today."""
    cfg = _cfg(cfg)
    s = load()
    now = C.now(cfg)
    if s.get("ok", True):
        s["since"] = now.isoformat(timespec="seconds")
    s.update(ok=False, error=str(err)[:300], last_fail=now.isoformat(timespec="seconds"),
             using="long-lived token" if token(cfg) else "interactive login")
    if job:
        s["jobs"] = sorted(set(s.get("jobs", [])) | {job})
    if missed:
        s.setdefault("missed", {})[missed] = f"{now:%Y-%m-%d}"
    _save(s)
    if notify:
        maybe_email(cfg, s)
    return s


def record_ok(cfg=None):
    s = load()
    if s.get("ok", True):
        return s
    cfg = _cfg(cfg)
    s.update(ok=True, restored=C.now(cfg).isoformat(timespec="seconds"))
    _save(s)
    return s


def maybe_email(cfg, s, force=False):
    now = C.now(cfg)
    last = s.get("emailed")
    if not force and last:
        try:
            if now - dt.datetime.fromisoformat(last) < dt.timedelta(hours=RENOTIFY_HOURS):
                return False
        except ValueError:
            pass
    has_tok = bool(token(cfg))
    if has_tok:
        fix = ("<p>The jobs use the long-lived token from <code>claude setup-token</code>, and Claude refused it "
               f"(it may have expired or been revoked). In your own PowerShell on {C.pc_name()}, make a new one and save it:</p>"
               "<pre>claude setup-token\n"
               "$d = \"$env:LOCALAPPDATA\\VaultLibrarian\\credentials\"\n"
               "Read-Host \"Paste the token\" -AsSecureString | ConvertFrom-SecureString | "
               "Set-Content -Encoding ascii \"$d\\claude_oauth_token.dpapi\"</pre>")
    else:
        fix = (f"<p>In your own PowerShell on {C.pc_name()} run <code>claude</code>, type <code>/login</code>, finish the "
               "browser sign-in, then <code>/exit</code>. Better still, set up the long-lived token "
               "(<code>claude setup-token</code>; see the User Manual, \"Claude sign-in\") so this stops recurring.</p>")
    jobs = ", ".join(s.get("jobs", [])) or "the vault jobs"
    try:
        since = dt.datetime.fromisoformat(s.get("since")).strftime("%a %b %d, %I:%M %p").replace(" 0", " ")
    except (TypeError, ValueError):
        since = s.get("since", "?")
    missed = s.get("missed") or {}
    catch = ("<p>Missed today and re-run automatically once Claude answers again: "
             + ", ".join(missed) + ".</p>") if missed else ""
    html = (f"<p><b>Claude on {C.pc_name()} isn't signed in</b>, so {jobs} can't run. "
            "Nothing in the vault was changed: queue items wait (they aren't marked failed) and resume by themselves.</p>"
            + catch + fix +
            f"<p style='color:#666'>Claude said: {s.get('error', '')}<br>First failed: {since}.</p>")
    try:
        C.send_mail(cfg, f"[{TAG} {now:%Y-%m-%d}] Claude on {C.pc_name()} needs signing in again", html)
        s["emailed"] = now.isoformat(timespec="seconds")
        _save(s)
        return True
    except Exception:  # noqa: BLE001  (mail is best effort)
        return False


# ---------------------------------------------------------------- checks
def probe(cfg):
    """One tiny Haiku call. -> (ok, error)."""
    import claude_cli
    out, info = claude_cli.ask("Reply with OK.", "", Path(cfg["local_root"]) / "work", timeout=90, model="haiku",
                               record=False, route="_probe", budget=False)
    return bool(out), info.get("error")


def token_age_days(cfg):
    p = token_file(cfg)
    if not p.exists():
        return None
    return (dt.datetime.now() - dt.datetime.fromtimestamp(p.stat().st_mtime)).days


def usage_heads_up(cfg, log, s):
    """The usage figures (overnight pacing) need the interactive login: Anthropic's usage endpoint answers the
    setup-token with HTTP 429 (tested 2026-10-24). When that login lapses, pacing falls back to the fixed limits in
    config; tell the owner gently, at most once a week. Model work is unaffected."""
    try:
        import usage_probe as U
        r = U._read_login(cfg)
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "why": f"{type(e).__name__}: {e}"}
    if r.get("ok"):
        log("sign-in check: usage figures readable (interactive login ok)")
        return
    log(f"sign-in check: usage figures unavailable: {r.get('why')}")
    if not re.search(r"no access token|no claude code sign-in|HTTP 401", str(r.get("why")), re.I):
        return
    last = s.get("usage_warned")
    if last and (C.now(cfg) - dt.datetime.fromisoformat(last)).days < 7:
        return
    C.send_mail(cfg, f"[{TAG} {C.now(cfg):%Y-%m-%d}] Low priority: Claude usage figures unavailable on {C.pc_name()}",
                "<p>The vault jobs are fine: they sign in with the long-lived token. But the interactive Claude Code "
                f"login on {C.pc_name()} has lapsed, and only that login can read your plan's usage figures, so tonight's "
                "pacing uses the fixed limits in config instead of your actual usage.</p>"
                f"<p>When convenient, in your own PowerShell on {C.pc_name()}: <code>claude</code>, then <code>/login</code>, "
                "then <code>/exit</code>.</p>")
    s["usage_warned"] = C.now(cfg).isoformat(timespec="seconds")
    _save(s)


def check(cfg, log):
    """21:00: test the sign-in before the night jobs; email if broken; warn when the token is getting old."""
    ok, err = probe(cfg)
    if ok:
        log("sign-in check: ok (" + ("long-lived token" if token(cfg) else "interactive login") + ")")
        s = record_ok(cfg)
        s.update(last_check=C.now(cfg).isoformat(timespec="seconds"),
                 using="long-lived token" if token(cfg) else "interactive login")
        _save(s)
        age = token_age_days(cfg)
        if age is not None and age >= TOKEN_WARN_DAYS and s.get("age_warned") != f"{C.now(cfg):%Y-%m}":
            C.send_mail(cfg, f"[{TAG}] Claude token for {C.pc_name()} is {age} days old",
                        f"<p>The long-lived Claude token the {C.pc_name()} jobs use is nearly a year old. When convenient, "
                        f"run <code>claude setup-token</code> in your own PowerShell on {C.pc_name()} and save the new token "
                        "(User Manual, \"Claude sign-in\").</p>")
            s["age_warned"] = f"{C.now(cfg):%Y-%m}"
            _save(s)
        if not token(cfg):
            log("sign-in check: no long-lived token saved; relying on the interactive login (lapses ~monthly)")
        usage_heads_up(cfg, log, s)
        return 0
    if classify(err) == "auth":
        log(f"sign-in check: BROKEN - {err}")
        s = record_failure(err, "sign-in check", cfg, notify=False)
        recent = False   # the evening check always reminds, unless an email went out in the last 3 hours
        try:
            recent = C.now(cfg) - dt.datetime.fromisoformat(s.get("emailed")) < dt.timedelta(hours=3)
        except (TypeError, ValueError):
            pass
        if not recent:
            maybe_email(cfg, s, force=True)
        return 1
    log(f"sign-in check: Claude call failed, not a sign-in problem: {err}")
    return 0


def recover(cfg, log=None):
    """Only while the state says broken: re-test; once it works, re-run what was missed today."""
    s = load()
    if s.get("ok", True) and not s.get("missed"):
        return 0
    log = log or C.Log(cfg, "signin")
    if not s.get("ok", True):
        ok, err = probe(cfg)
        if not ok:
            return 1          # still broken; the email already went out
        log("sign-in restored")
        s = record_ok(cfg)
    today = f"{C.now(cfg):%Y-%m-%d}"
    missed = s.get("missed") or {}
    for job, day in list(missed.items()):
        if day == today and job == "organize":
            pyw = Path(sys.executable).with_name("pythonw.exe")
            exe = pyw if pyw.exists() else Path(sys.executable)
            subprocess.Popen([str(exe), str(Path(__file__).with_name("organize.py")), "--prep"],
                             cwd=str(Path(__file__).parent), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            log("catch-up: started organize --prep")
        missed.pop(job, None)
    s["missed"] = missed
    s.pop("jobs", None)
    _save(s)
    return 0


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "status"
    cfg = C.load_config()
    if cmd == "check":
        return check(cfg, C.Log(cfg, "signin"))
    if cmd == "recover":
        return recover(cfg)
    s = load()
    s["token_saved"] = bool(token(cfg))
    s["token_age_days"] = token_age_days(cfg)
    print(json.dumps(s, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
