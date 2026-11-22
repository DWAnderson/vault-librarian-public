"""Model backends for the intake queue (plan section 4a).

Routes never call a model directly. They pass a list of requests and get answers back:

    answers = llm.run(cfg, route, [{"id": "summary", "instructions": ..., "material": ...}],
                      cache_dir=store.payload(item_id) / "answers", log=log, item=item_id)
    answers["summary"] -> {"text": "..."}                      done
                       -> {"pending": True, "batch": "<id>"}   submitted, collect on a later run
                       -> {"error": "...", "limit": bool}     failed (limit=True: usage limit, try another night)
                       -> {"skipped": True}                   not run because an earlier request hit a limit

Backends, chosen per route in config queue.backends ({"default": "claude-cli", "book": ...}):
  claude-cli       claude -p on the owner's Pro login (jobs\\claude_cli.py): answers at once.
  anthropic-batch  Message Batches API with its own key: slot reserved, not built yet.
Answers are cached as <cache_dir>\\<request id>.json, so a rerun never pays twice for the same
request. Every model call is logged to %LOCALAPPDATA%\\VaultLibrarian\\logs\\llm-YYYY-MM.jsonl, which
is also what the call budgets count: daytime calls against queue.claude_calls_per_day, night-window
calls against the cap queue_run sets from pacing (2026-10-06).
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import claude_cli  # noqa: E402

LIMIT_RE = re.compile(r"usage limit|rate limit|limit reached|out of (extra )?usage|too many requests|429|quota", re.I)


def backend_for(cfg, route):
    b = ((cfg.get("queue") or {}).get("backends") or {})
    return b.get(route) or b.get("default") or "claude-cli"


def _ledger(cfg):
    return Path(cfg["local_root"]) / "logs" / f"llm-{C.now(cfg):%Y-%m}.jsonl"


def night_window(cfg):
    w = (cfg.get("queue") or {}).get("night_window") or ["23:45", "01:55"]
    return w[0], w[1]


def _in_window(cfg, t):
    a, b = night_window(cfg)
    hm = f"{t:%H:%M}"
    return a <= hm <= b if a <= b else (hm >= a or hm <= b)


def pool_now(cfg):
    """'night' inside queue.night_window, else 'day' (2026-10-06: separate call budgets)."""
    if _in_window(cfg, C.now(cfg)):
        return "night"
    try:   # an extra run window the owner booked counts as night work (2026-10-06)
        import usage_plan
        if usage_plan.active_window(cfg):
            return "night"
    except Exception:  # noqa: BLE001
        pass
    return "day"


def _ledger_lines(cfg, since):
    """Ledger records from `since` on (reads last month's file too when the span crosses it)."""
    files = {_ledger(cfg)}
    files.add(Path(cfg["local_root"]) / "logs" / f"llm-{since:%Y-%m}.jsonl")
    for p in sorted(files):
        if p.exists():
            for ln in p.read_text(encoding="utf-8").splitlines():
                at = ln[8:33] if ln.startswith('{"at": "') else ""
                if at[:19] >= f"{since:%Y-%m-%dT%H:%M:%S}":
                    yield ln, at


def calls_in_pool(cfg, pool=None):
    """Model calls counted against the current budget (2026-10-06).
    day:   calls today outside the night window (cap: queue.claude_calls_per_day)
    night: calls since tonight's window opened (cap: set by queue_run from pacing, else pacing.calls_fallback)
    Before this, one calendar-day count was shared, so daytime analyses used up the 23:45 book run's budget."""
    import datetime as dt
    now = C.now(cfg)
    pool = pool or pool_now(cfg)
    a, _ = night_window(cfg)
    if pool == "night":
        start = now.replace(hour=int(a[:2]), minute=int(a[3:]), second=0, microsecond=0)
        if start > now:
            start -= dt.timedelta(days=1)
        return sum(1 for ln, _ in _ledger_lines(cfg, start) if '"budget": false' not in ln)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    n = 0
    for ln, at in _ledger_lines(cfg, start):
        if '"budget": false' in ln:   # sign-in probes and refresh pings (2026-11-13)
            continue
        try:
            if not _in_window(cfg, dt.datetime.fromisoformat(at[:19])):
                n += 1
        except ValueError:
            n += 1
    return n


def calls_today(cfg):
    """Kept for callers: the count for the current pool."""
    return calls_in_pool(cfg)


def pool_cap(cfg):
    q = cfg.get("queue") or {}
    if pool_now(cfg) == "night":
        if cfg.get("_night_calls_cap"):
            return int(cfg["_night_calls_cap"])
        p = cfg.get("pacing") or {}
        return int(p.get("calls_fallback") or q.get("claude_calls_per_day") or 0)
    return int(q.get("claude_calls_per_day") or 0)


def _log_call(cfg, route, item, rid, backend, info, ok, model=None):
    """One ledger line per call; the format lives in claude_cli.log_call (2026-11-13)."""
    claude_cli.log_call(info, ok, route=route, item=item, request=rid, model=model, backend=backend, cfg=cfg)


def run(cfg, route, requests, cache_dir, log=print, item=None, work_dir=None):
    backend = backend_for(cfg, route)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(work_dir) if work_dir else Path(cfg["local_root"]) / "work" / "llm"
    work_dir.mkdir(parents=True, exist_ok=True)
    out, stop = {}, None
    for r in requests:
        cp = cache_dir / f"{r['id']}.json"
        if cp.exists():
            out[r["id"]] = json.loads(cp.read_text(encoding="utf-8"))
            continue
        if stop:
            out[r["id"]] = {"skipped": True, "error": stop}
            continue
        cap = pool_cap(cfg)
        if cap and calls_in_pool(cfg) >= cap:   # the day or night budget: the rest waits (treated like a usage limit)
            stop = f"{pool_now(cfg)} model budget used ({cap} calls)"
            out[r["id"]] = {"skipped": True, "error": stop}
            log(f"  llm: {stop}; {route}/{r['id']} waits")
            continue
        if backend == "claude-cli":
            # model: the request's own, else the one config pins for its model_key (e.g. "analyze_part"), else for
            # the route, else queue.models.default (2026-11-13)
            model = (r.get("model") or claude_cli.model_for(r.get("model_key"), cfg, fallback=False)
                     or claude_cli.model_for(route, cfg))
            kw = {"model": model} if model else {}
            text, info = claude_cli.ask(r["instructions"], r["material"], work_dir, timeout=r.get("timeout", 300),
                                        ledger=False, system_extra=r.get("system"), **kw)
            _log_call(cfg, r.get("model_key") or route, item, r["id"], backend, info, bool(text), model=model)
            if text:
                ans = {"text": text, "cost_usd": info.get("cost_usd"), "ms": info.get("ms"),
                       "usage": info.get("usage"), "models": info.get("models")}
                cp.write_text(json.dumps(ans, ensure_ascii=False), encoding="utf-8")
                out[r["id"]] = ans
            else:
                err = info.get("error") or "no answer"
                if info.get("auth"):   # Claude sign-in expired (2026-10-24): items wait like a usage limit, never "failed"
                    out[r["id"]] = {"error": f"waiting for Claude sign-in on {C.pc_name()}", "limit": True, "auth": True}
                    stop = f"waiting for Claude sign-in on {C.pc_name()}"
                    log(f"  llm: Claude sign-in failed on {route}/{r['id']}: {err[:120]} - the rest waits")
                    continue
                limit = bool(LIMIT_RE.search(err))
                out[r["id"]] = {"error": err, "limit": limit}
                if limit:
                    stop = "usage limit reached earlier in this run"
                    log(f"  llm: usage limit on {route}/{r['id']}: {err[:120]}")
        elif backend == "anthropic-batch":
            out[r["id"]] = {"error": "anthropic-batch backend is not built yet (plan section 4a)", "limit": False}
        else:
            out[r["id"]] = {"error": f"unknown backend {backend}", "limit": False}
    return out
