"""Shared helpers for the Vault Librarian jobs: config, paths, lock, logging, Graph mail.

Code lives in this project folder (APP below, found from this file's location); runtime, logs, work files and
snapshots live under config local_root (in %LOCALAPPDATA%, this PC only). Secrets are never logged.
"""
import datetime as dt
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent            # ...\Custom Apps\Obsidian
LIB = APP / "lib"
VL = LIB / "vault-librarian" / "scripts"
UV = LIB / "url-to-vault" / "scripts"


def vrel(vault, name):
    """Vault-relative path of a housekeeping folder/file (Archives, attachments, Templates, Topics,
    _claude-tags.md, Vault Review.base): inside `_Obsidian Internal/` if it is there, else the root."""
    if str(VL) not in sys.path:
        sys.path.insert(0, str(VL))
    from vaultlib import vault_rel
    return vault_rel(str(vault), name)


def vpath(vault, name):
    """Path of vrel(vault, name)."""
    return Path(vault) / vrel(vault, name)
try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


def load_config():
    cfg = json.loads((APP / "config" / "vault.json").read_text(encoding="utf-8"))

    def expand(v):
        if isinstance(v, str):
            return os.path.expandvars(v)
        if isinstance(v, dict):
            return {k: expand(x) for k, x in v.items()}
        if isinstance(v, list):
            return [expand(x) for x in v]
        return v
    cfg = expand(cfg)
    cfg["vault_path"] = str(Path(cfg["vault_path"]))
    local = Path(cfg["local_root"])
    for sub in ("logs", "work", "snapshots", "queue"):
        (local / sub).mkdir(parents=True, exist_ok=True)
    o = owner(cfg)   # the lib/ scripts run as subprocesses and read these (vaultlib.owner)
    os.environ["VAULT_OWNER_NAME"], os.environ["VAULT_OWNER_ID"] = o["given"], o["id"]
    os.environ["VAULT_OWNER_NOTE_LABEL"] = o["note_label"]
    _CFG[:] = [cfg]
    return cfg


# ---------------------------------------------------------------- owner and PC names (config "owner", "pc_name")
# Prompts, messages and the notes the app writes name the vault's owner and the PC through these, so nothing
# personal is in the code. Prompt text uses the tokens below; claude_cli.ask fills them in (personalize).
_CFG = []


def _cfg():
    if not _CFG:
        try:
            c = load_config()   # (tests may replace load_config: keep what it returns)
        except Exception:  # noqa: BLE001  no config (first install): generic names
            c = {}
        if not _CFG:
            _CFG[:] = [c if isinstance(c, dict) else {}]
    return _CFG[0]


def owner(cfg=None):
    """name ('Sam' or 'the owner'), possessive, given (the bare configured name, may be ''), id (generated.by of
    the owner's own notes), about (one sentence for prompts, may be ''), note_label (the callout title of the
    owner's note on an item)."""
    o = ((cfg if cfg is not None else _cfg()).get("owner")) or {}
    given = str(o.get("name") or "").strip()
    return {"given": given, "name": given or "the owner", "possessive": f"{given}'s" if given else "the owner's",
            "id": str(o.get("id") or "human:owner"), "about": str(o.get("about") or "").strip(),
            "note_label": str(o.get("note_label") or (f"{given}'s note" if given else "My note"))}


def pc_name(cfg=None):
    """The PC's name for messages (config pc_name), or 'this PC'."""
    return str((cfg if cfg is not None else _cfg()).get("pc_name") or "").strip() or "this PC"


def personalize(text, cfg=None):
    """Fill the owner tokens in prompt or message text: «owner» «owner's» (and «Owner» «Owner's» «OWNER'S» at the
    start of a sentence or in capitals), «owner_about» (the owner.about sentence, or nothing), «note_label», «pc»."""
    if not text or "«" not in text:
        return text
    o = owner(cfg)
    cap = lambda s: s[:1].upper() + s[1:]   # noqa: E731
    for k, v in (("«owner's»", o["possessive"]), ("«Owner's»", cap(o["possessive"])),
                 ("«OWNER'S»", o["possessive"].upper()), ("«owner»", o["name"]), ("«Owner»", cap(o["name"])),
                 ("«note_label»", o["note_label"]), ("«owner_id»", o["id"]), ("«pc»", pc_name(cfg))):
        text = text.replace(k, v)
    return text.replace(" «owner_about»", (" " + o["about"]) if o["about"] else "").replace("«owner_about»", o["about"])


def task_path(cfg=None):
    """Task Scheduler folder of the app's tasks (config schedule.task_path; default: named after local_root)."""
    cfg = cfg or load_config()
    return (cfg.get("schedule") or {}).get("task_path") or "\\" + Path(cfg["local_root"]).name + "\\"


def now(cfg=None):
    tz = ZoneInfo((cfg or {}).get("timezone", "America/Chicago")) if ZoneInfo else None
    return dt.datetime.now(tz) if tz else dt.datetime.now().astimezone()


class Log:
    def __init__(self, cfg, job):
        self.path = Path(cfg["local_root"]) / "logs" / f"{job}-{now(cfg):%Y-%m-%d}.log"
        self.cfg = cfg

    def __call__(self, msg):
        line = f"{now(self.cfg):%H:%M:%S} {msg}"
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)


class VaultLock:
    """Shared vault lock: `.vault-librarian.lock` in the vault root, the same file the laptop's
    "Vault Cowork sync" task uses. Format (ASCII JSON):
        {"owner": "<job name>", "created": "<ISO 8601 with offset>", "host": "<pc_name>"}
    Rules (identical on both machines), applied by acquire():
      1. A lock owned by a DIFFERENT job and under 2 hours old (age from "created", else the
         file's modified time) -> don't change the vault: acquire() returns (False, "skipped: lock
         held by <owner> since <created>"); the job reports that and exits cleanly.
      2. Otherwise (no lock, a lock over 2 hours old, or a leftover lock from this same job) ->
         write our lock; the returned note says when a stale or leftover lock was replaced.
      3. release() deletes the lock (only if it is still ours); call it in a finally block.
    Usage:
        lock = C.VaultLock(cfg, "<pc> nightly")
        ok, note = lock.acquire()
        if not ok: report(note); return 0
        try: ... change the vault ...
        finally: lock.release()
    """

    def __init__(self, cfg, owner):
        self.path = Path(cfg["vault_path"]) / ".vault-librarian.lock"
        self.owner = owner
        self.host = cfg.get("lock_host") or pc_name(cfg)
        self.stale = dt.timedelta(minutes=int(cfg.get("lock_stale_minutes", 120)))
        self.cfg = cfg
        self.held = False
        self.created = None

    def _read(self):
        """Return (owner, created_text, age) of the current lock file; unreadable parts are None."""
        owner = created = None
        try:
            info = json.loads(self.path.read_text(encoding="utf-8"))
            owner, created = info.get("owner"), info.get("created")
        except Exception:
            pass
        when = None
        if created:
            try:
                when = dt.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.astimezone()
            except ValueError:
                when = None
        if when is None:
            try:
                when = dt.datetime.fromtimestamp(self.path.stat().st_mtime).astimezone()
                created = created or when.isoformat(timespec="seconds") + " (file time)"
            except OSError:
                return owner, created, None
        return owner, created, now(self.cfg) - when

    def acquire(self):
        note = None
        if self.path.exists():
            owner, created, age = self._read()
            if owner != self.owner and (age is None or age < self.stale):
                return False, f"skipped: lock held by {owner or 'unknown'} since {created}"
            if owner == self.owner:
                note = f"replaced a leftover lock from this job ({owner}, since {created})"
            else:
                hrs = age.total_seconds() / 3600 if age else 0
                note = f"replaced a stale lock held by {owner or 'unknown'} since {created} ({hrs:.1f} h old)"
        self.created = now(self.cfg).isoformat(timespec="seconds")
        body = json.dumps({"owner": self.owner, "created": self.created, "host": self.host}, ensure_ascii=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(body, encoding="ascii")
        os.replace(tmp, self.path)
        self.held = True
        return True, note

    def release(self):
        """Delete the lock if it is still ours (another job may have replaced it as stale)."""
        if not self.held:
            return
        self.held = False
        try:
            info = json.loads(self.path.read_text(encoding="utf-8"))
            if info.get("owner") == self.owner and info.get("created") == self.created:
                self.path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


def run(log, script, *args, timeout=1800):
    """Run a lib script with this venv's Python; return (exit code, stdout+stderr text)."""
    cmd = [sys.executable, str(script), *[str(a) for a in args]]
    log("run " + " ".join(Path(c).name if i < 2 else c for i, c in enumerate(cmd)))
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
        log(f"  exit {p.returncode}")
        return p.returncode, out
    except subprocess.TimeoutExpired:
        log("  TIMEOUT")
        return 124, "timed out"


# ---------------------------------------------------------------- Microsoft Graph (app token)
class MailOff(RuntimeError):
    """Raised instead of calling Graph when mail is turned off (config mail.enabled = false)."""


def mail_on(cfg):
    """True unless mail is turned off: config mail.enabled false, or no mailbox set. Without mail (no Microsoft
    365 / Entra app), intake still works the OneDrive dropbox, the connector and the queue; questions and the
    daily update are answered on the home page instead of by email."""
    return (cfg.get("mail") or {}).get("enabled", True) is not False and bool(cfg.get("mailbox"))


def graph_token(cfg):
    if not mail_on(cfg):
        raise MailOff("mail is turned off (config mail.enabled)")
    import requests
    c = json.loads(Path(cfg["graph_credentials"]).read_text(encoding="utf-8"))
    r = requests.post(f"https://login.microsoftonline.com/{c['tenantId']}/oauth2/v2.0/token",
                      data={"client_id": c["clientId"], "client_secret": c["clientSecret"],
                            "scope": "https://graph.microsoft.com/.default",
                            "grant_type": "client_credentials"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def send_mail(cfg, subject, html, to=None):
    """Send one email through Graph. Returns the HTTP status (202 = accepted), or None when mail is off."""
    if not mail_on(cfg):
        return None
    import requests
    try:
        tok = graph_token(cfg)
        body = {"message": {"subject": subject, "body": {"contentType": "HTML", "content": html},
                            "toRecipients": [{"emailAddress": {"address": to or cfg["notify_to"]}}]},
                "saveToSentItems": False}
        r = requests.post(f"https://graph.microsoft.com/v1.0/users/{cfg['mailbox']}/sendMail",
                          headers={"Authorization": f"Bearer {tok}"}, json=body, timeout=30)
    except Exception as e:
        mail_health(cfg, "send", False, f"{type(e).__name__}: {e}")
        raise
    mail_health(cfg, "send", r.status_code == 202, None if r.status_code == 202 else f"HTTP {r.status_code}")
    return r.status_code  # 202 = accepted


def paused(cfg):
    """The 'Pause until' date from the home page (config pause.until, inclusive) while it lasts, else None.
    While paused: no intake queue, analyses, books or YouTube Remember (home page plan, 2026-11-12)."""
    until = (cfg.get("pause") or {}).get("until")
    if until and now(cfg).date().isoformat() <= str(until):
        return str(until)
    return None


# ---------------------------------------------------------------- mail health (home page plan, 2026-11-12)
# queue\mail-health.json: when Graph last worked for reading and sending, the last error and how many tries in a
# row failed. Written by send_mail, intake and queue_run; read by the home page and the catch-up resend.
def mail_health_path():
    return APP / "queue" / "mail-health.json"


def mail_health(cfg, kind, ok, error=None):
    """Record one Graph read or send (kind 'read' | 'send'). Never raises. Nothing is recorded while mail is off,
    so the home page doesn't report mail as down."""
    if not mail_on(cfg):
        return
    try:
        p = mail_health_path()
        try:
            h = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            h = {}
        k = h.setdefault(kind, {})
        t = now(cfg).isoformat(timespec="seconds")
        if ok:
            k.update(last_ok=t, failed_in_row=0)
            k.pop("down_since", None)
        else:
            k.update(last_fail=t, last_error=str(error or "")[:300], failed_in_row=int(k.get("failed_in_row") or 0) + 1)
            if k["failed_in_row"] == 1:
                k["down_since"] = t
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(h, indent=1), encoding="utf-8")
        tmp.replace(p)
    except Exception:  # noqa: BLE001  bookkeeping only
        pass


def mail_health_read():
    try:
        return json.loads(mail_health_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
