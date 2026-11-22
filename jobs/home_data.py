"""Data for the home page (plan: 2026-11-12 Home page - plan.md, sections 4-5). Read-only; every function is
safe to call from the web server and never writes to the vault.

  open_days()        open proposals, oldest first: day, link, counts, email delivery
  recent_results(n)  the last n answered days
  needs_you(cfg)     held queue items, held links, Nightly "needs you" lines
  mail(cfg)          mail connection (queue\\mail-health.json)
  vault_stats(cfg)   notes by folder and type, Inbox/Clippings, new this week, queue counts, search index
  activity(cfg)      recent saves, filed notes (log.md), new analyses, connector requests
  home_token()       the bookmarkable token (credentials\\home_token.txt; made on first use)
"""
import datetime as dt
import json
import re
import secrets
from pathlib import Path

import common as C


def jload(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


# ---------------------------------------------------------------- token
def token_path(cfg):
    return Path(cfg["local_root"]) / "credentials" / "home_token.txt"


def home_token(cfg, reset=False):
    p = token_path(cfg)
    if reset or not p.exists() or not p.read_text(encoding="utf-8").strip():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(secrets.token_urlsafe(24), encoding="utf-8")
    return p.read_text(encoding="utf-8").strip()


def token_ok(cfg, t):
    return bool(t) and secrets.compare_digest(home_token(cfg), str(t))


# ---------------------------------------------------------------- questions
def counts(prop):
    n = {"filing": len(prop.get("questions") or []),
         "tags": len(prop.get("new_tags") or []) + len(prop.get("new_folders") or []),
         "duplicates": len(prop.get("dups") or []), "queue": len(prop.get("queue") or []),
         "held": len(prop.get("held") or [])}
    n["total"] = sum(n.values())
    return n


def open_days():
    """The latest update's questions while they are open ([] or one day). Earlier days are never listed: each
    morning re-asks whatever is still unanswered, and supersedes the earlier day (2026-11-12)."""
    out = []
    p = latest_day()
    if p and p.get("status") == "sent":
        out.append({"day": p["day"], "link": f"organize/{p['day']}?t={p['web_token']}",
                    "queue_link": f"organize/{p['day']}/queue?t={p['web_token']}",
                    "usage_base": f"organize/{p['day']}/usage?t={p['web_token']}",
                    "token": p["web_token"], "created": p.get("created"), "email": p.get("email") or {},
                    "counts": counts(p), "items": len(p.get("items") or [])})
    return out


def new_since(cfg):
    """What has come in since the latest update and isn't in its questions: unfiled notes in Inbox/Clippings and
    queue or held items waiting for the owner. They are asked in the next morning's update, or now with "Ask now"
    (Organize run, which rebuilds today's questions; not once today's are answered)."""
    import organize as O
    p = latest_day() or {}
    paths = {it.get("path") for it in p.get("items") or []} | set((p.get("pairs") or {}).values())
    for q in p.get("questions") or []:
        paths |= set(q.get("paths") or [])
    asked = {str(q.get("item")) for q in (p.get("queue") or []) + (p.get("held") or []) if q.get("item")}
    vault = Path(cfg["vault_path"])
    notes = [x.relative_to(vault).as_posix() for x in O.unfiled(vault, 500)]
    notes = [x for x in notes if x not in paths]
    waiting = []
    try:
        import vqueue as Q
        for x in Q.Store(cfg).all():
            if x.get("status") == "held" and x["id"] not in asked:
                waiting.append(x.get("title") or x.get("source_name") or x["id"])
    except Exception:  # noqa: BLE001
        pass
    for k, h in jload(C.APP / "queue" / "held.json", {}).items():
        if h.get("status") == "held" and k not in asked and not k.startswith("q-"):
            waiting.append(str(h.get("url") or h.get("subject") or k))
    today = C.now(cfg).strftime("%Y-%m-%d")
    tf = jload(C.APP / "reports" / f"organize-{today}.json", {})
    return {"notes": notes, "waiting": waiting, "since": p.get("created") or p.get("day"),
            "can_ask": tf.get("status") != "applied"}


def latest_day():
    """The newest proposal of any status that has a token (its queue/usage pages work even once answered)."""
    for f in sorted((C.APP / "reports").glob("organize-*.json"), reverse=True)[:10]:
        p = jload(f, {})
        if p.get("web_token") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(p.get("day", ""))):
            return p
    return None


def recent_results(n=5):
    out = []
    for f in sorted((C.APP / "reports").glob("organize-*.json"), reverse=True)[:40]:
        p = jload(f, None)
        if not p or p.get("status") != "applied":
            continue
        ans = p.get("answers") or {}
        out.append({"day": p["day"], "via": p.get("via") or "", "applied": p.get("applied") or "",
                    "filed": int(p.get("filed") or 0), "mode": p.get("mode"), "answered": len(ans),
                    "counts": counts(p), "link": f"organize/{p['day']}?t={p.get('web_token', '')}"})
        if len(out) >= n:
            break
    return out


# ---------------------------------------------------------------- needs you
def needs_you(cfg):
    out = {"queue": [], "held": [], "nightly": [], "failures": []}
    try:
        import vqueue as Q
        for x in Q.Store(cfg).all():
            if x.get("status") == "held":
                out["queue"].append({"title": x.get("title") or x.get("source_name") or x["id"],
                                     "why": x.get("why") or x.get("needs") or "", "route": Q.route_of(x)})
    except Exception as e:  # noqa: BLE001
        out["queue_error"] = f"{type(e).__name__}: {e}"
    for k, h in jload(C.APP / "queue" / "held.json", {}).items():
        if h.get("status") == "held":
            out["held"].append({"what": h.get("url") or h.get("subject") or k, "why": h.get("why") or "",
                                "needs": h.get("needs") or ""})
    nf = sorted((C.APP / "reports").glob("nightly-*.json"))
    if nf:
        n = jload(nf[-1], {})
        out["nightly"] = list(n.get("needs") or [])
        out["failures"] = list(n.get("failures") or [])
        out["nightly_day"] = n.get("day")
    return out


# ---------------------------------------------------------------- mail
def mail(cfg):
    if not C.mail_on(cfg):   # mail turned off (config mail.enabled): never "down"
        return {"down": False, "off": True, "read": {}, "send": {}}
    h = C.mail_health_read()
    r, s = h.get("read") or {}, h.get("send") or {}
    down = int(r.get("failed_in_row") or 0) > 0 or int(s.get("failed_in_row") or 0) > 0
    return {"down": down, "off": False, "read": r, "send": s}


# ---------------------------------------------------------------- stats
def vault_stats(cfg):
    st = {}
    try:
        import vault_search as VS
        con = VS.connect(cfg)
        try:
            top = {}
            for path, n in con.execute("SELECT path, 1 FROM notes WHERE layer != 'skip'"):
                k = path.split("/", 1)[0] if "/" in path else "(root)"
                top[k] = top.get(k, 0) + n
            st["by_folder"] = sorted(top.items(), key=lambda x: -x[1])
            st["by_type"] = list(con.execute("SELECT coalesce(type,'(none)'), count(*) FROM notes WHERE layer != 'skip' "
                                             "GROUP BY 1 ORDER BY 2 DESC LIMIT 12"))
            week = (C.now(cfg) - dt.timedelta(days=7)).timestamp()
            st["changed_week"] = con.execute("SELECT count(*) FROM notes WHERE mtime >= ?", (week,)).fetchone()[0]
            st["analyses_week"] = con.execute("SELECT count(*) FROM notes WHERE mtime >= ? AND type = 'Analysis'",
                                              (week,)).fetchone()[0]
            st["notes"] = sum(n for _, n in top.items())
            st["concepts"] = con.execute("SELECT count(*) FROM notes WHERE type = 'Concept'").fetchone()[0]
        finally:
            con.close()
        s2 = VS.stats(cfg)
        st["search"] = {k: s2.get(k) for k in ("passages", "size_mb", "last_update", "waiting_for_vectors",
                                               "original_passages")}
        st["originals"] = s2.get("originals") or {}
    except Exception as e:  # noqa: BLE001
        st["error"] = f"{type(e).__name__}: {e}"
    vault = Path(cfg["vault_path"])
    for name in ("Inbox", "Clippings"):
        try:
            st[name.lower()] = sum(1 for _ in (vault / name).rglob("*.md"))
        except OSError:
            st[name.lower()] = None
    try:
        import vqueue as Q
        items = Q.Store(cfg).all()
        by = {}
        for x in items:
            if x.get("status") in Q.FINAL:
                continue
            key = (Q.route_of(x) or "?", x.get("status") or "?")
            by[key] = by.get(key, 0) + 1
        st["queue"] = sorted(by.items(), key=lambda kv: (-kv[1], kv[0]))
        st["queue_open"] = sum(by.values())
    except Exception as e:  # noqa: BLE001
        st["queue_error"] = f"{type(e).__name__}: {e}"
    return st


# ---------------------------------------------------------------- activity
def activity(cfg):
    out = {}
    out["saves"] = (jload(Path(cfg["local_root"]) / "queue" / "recent.json", []) or [])[:10]
    lines = []
    try:
        text = (Path(cfg["vault_path"]) / "log.md").read_text(encoding="utf-8")
        parts = re.split(r"(?m)^## (\d{4}-\d{2}-\d{2})\s*$", text)
        for i in range(len(parts) - 2, 0, -2):          # newest heading last in the file
            day, body = parts[i], parts[i + 1]
            for ln in body.splitlines():
                if ln.startswith("- "):
                    lines.append((day, ln[2:].strip()))
            if len(lines) >= 40:
                break
    except OSError:
        pass
    out["log"] = sorted(lines, key=lambda x: x[0], reverse=True)[:25]
    try:
        import connector_requests as CR
        out["connector"] = [r for _p, r in CR.load_all()[-10:]][::-1]
    except Exception:  # noqa: BLE001
        out["connector"] = []
    return out
