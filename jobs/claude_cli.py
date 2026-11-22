"""Headless Claude (claude -p) for the Vault Librarian jobs: text in, text out, no tools.

The prompt's instructions go on the command line; the material (email or page text) goes in on
stdin, so nothing is echoed into logs. Signs in with the long-lived token from `claude setup-token`
when one is saved (claude_auth; 2026-10-24), else the owner's interactive Pro login on this PC.
A sign-in failure comes back as info["auth"] = True and is recorded by claude_auth (state + one email).

Usage ledger (2026-11-13): EVERY call made through ask() appends one line to
logs\\llm-YYYY-MM.jsonl under config local_root: route, caller, model asked for,
models used, tokens, API-equivalent cost, duration, ok/error. Before this only llm.run() logged, so the 06:00
organize proposal, intake/linkjob summaries and sign-in probes were invisible. Pass route= so the line says what
the call was for; budget=False for housekeeping pings that should not count against the day/night call caps.
Never call claude.exe without going through ask() or log_raw().
"""
import datetime as dt
import inspect
import json
import os
import subprocess
from pathlib import Path

EXE = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
NO_TOOLS = ["Bash", "Edit", "Write", "Read", "Glob", "Grep", "WebFetch", "WebSearch", "NotebookEdit", "Task"]
# Lean mode (2026-09-28, after the pilot book): Claude Code's own system prompt, tool, skill, MCP and settings
# context was ~40,000 tokens per call, ~75% of a book's input. These flags leave only our instructions.
SYSTEM = ("You process documents for «owner's» Obsidian notes. Follow the instructions exactly, work only from the "
          "text on stdin, never invent facts, and output only what is asked for.")
LEAN_REST = ["--tools", "", "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", ""]
LEAN = ["--system-prompt", SYSTEM, *LEAN_REST]   # with the owner tokens; lean() fills them in


def lean():
    """LEAN with the owner's name filled in (common.personalize)."""
    import common as C
    return ["--system-prompt", C.personalize(SYSTEM), *LEAN_REST]


def model_for(route, cfg=None, fallback=True):
    """The model pinned for a route in config queue.models (route, else "default"), or None (2026-11-13:
    nothing should inherit the Claude Code default, which moved from Opus to Sonnet on its own on 2026-10-25)."""
    if not route:
        return None
    try:
        if cfg is None:
            import common as C
            cfg = C.load_config()
        m = (cfg.get("queue") or {}).get("models") or {}
        return m.get(route) or (m.get("default") if fallback else None)
    except Exception:  # noqa: BLE001
        return None


def _thinking_off(model):
    """True for models listed in config queue.thinking_off (default ["haiku"]). claude -p thinks by default: a
    one-word Haiku answer used ~4,900 output tokens with thinking and 7 without (test, 2026-11-13). Opus and
    Sonnet keep thinking for the analyses, where the reasoning is the point."""
    try:
        import common as C
        off = (C.load_config().get("queue") or {}).get("thinking_off", ["haiku"])
    except Exception:  # noqa: BLE001
        off = ["haiku"]
    m = str(model).lower()
    return any(x.lower() in m for x in off)


def _system_file(cwd, extra):
    """SYSTEM + extra written once per distinct text (the name is its hash), for --system-prompt-file.
    Text that is the same from call to call (the concept register) goes here so that Claude's prompt cache can
    reuse it: calls after the first in a run read it at about a tenth of the price (2026-11-13)."""
    import hashlib
    import common as C
    text = C.personalize(SYSTEM + "\n\n" + extra)
    f = Path(cwd) / f"system-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:12]}.txt"
    if not f.exists():
        f.write_text(text, encoding="utf-8")
    return f


def _env():
    try:
        import claude_auth
        return claude_auth.env()
    except Exception:  # noqa: BLE001
        return None


def _note(ok, err, record):
    """Keep claude_auth's sign-in state current. Never raises."""
    if not record:
        return
    try:
        import claude_auth
        if ok:
            claude_auth.record_ok()
        elif claude_auth.classify(err) == "auth":
            claude_auth.record_failure(err)
    except Exception:  # noqa: BLE001
        pass


def _fail(err, record):
    info = {"error": err}
    try:
        import claude_auth
        if claude_auth.classify(err) == "auth":
            info["auth"] = True
    except Exception:  # noqa: BLE001
        pass
    _note(False, err, record)
    return None, info


def _ledger_path(cfg=None):
    """logs/llm-YYYY-MM.jsonl under config local_root. None if the config can't be read (tests, odd setups)."""
    try:
        import common as C
        cfg = cfg or C.load_config()
        return Path(cfg["local_root"]) / "logs" / f"llm-{C.now(cfg):%Y-%m}.jsonl", C.now(cfg)
    except Exception:  # noqa: BLE001
        return None, dt.datetime.now().astimezone()


def _caller():
    """'module.function' of the first frame outside this file and llm.py."""
    for f in inspect.stack()[2:]:
        mod = Path(f.filename).stem
        if mod not in ("claude_cli", "llm"):
            return f"{mod}.{f.function}"
    return None


def log_call(info, ok, route=None, item=None, request=None, model=None, backend="claude-cli", budget=True, cfg=None):
    """Append one ledger line. Never raises: a logging failure must not lose the answer.
    cfg: the caller's loaded config (llm.run passes it); else the app config is read."""
    try:
        path, now = _ledger_path(cfg)
        if path is None:
            return
        rec = {"at": now.isoformat(timespec="seconds"), "route": route or "unrouted", "item": item, "request": request,
               "backend": backend, "ok": ok, "ms": info.get("ms"), "cost_usd": info.get("cost_usd"),
               "usage": info.get("usage"), "models": info.get("models"), "model_asked": model,
               "turns": info.get("turns"), "caller": _caller(),
               "error": None if ok else str(info.get("error"))[:200]}
        if not budget:
            rec["budget"] = False
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _parse(stdout):
    """claude --output-format json -> (text, info, is_error) or raises ValueError."""
    j = json.loads(stdout)
    u = j.get("usage") or {}
    usage = {k: u.get(k) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")}
    info = {"ms": j.get("duration_ms"), "cost_usd": j.get("total_cost_usd"), "usage": usage,
            "models": list((j.get("modelUsage") or {}).keys()), "turns": j.get("num_turns")}
    return (j.get("result") or "").strip(), info, bool(j.get("is_error"))


def log_raw(stdout, route, model=None, budget=False):
    """Log a claude.exe call made outside ask() (usage_probe's sign-in refresh). Never raises."""
    try:
        text, info, err = _parse(stdout)
        if err:
            info["error"] = text[:300]
        log_call(info, bool(text) and not err, route=route, model=model, budget=budget)
    except Exception as e:  # noqa: BLE001
        log_call({"error": f"no JSON from claude: {type(e).__name__}"}, False, route=route, model=model, budget=budget)


def ask(instructions, material, cwd, timeout=300, model=None, record=True, route=None, item=None, request=None,
        budget=True, ledger=True, system_extra=None):
    """Return (text or None, info dict). Never raises. Logs the call to the usage ledger unless ledger=False
    (only llm.run passes that, because it logs the same call itself with its cache/budget context).
    model: None -> the model config pins for `route`. system_extra: stable text appended to the system prompt
    (cached between calls)."""
    model = model or model_for(route)
    text, info = _ask(instructions, material, cwd, timeout, model, record, system_extra)
    if ledger:
        log_call(info, bool(text), route=route, item=item, request=request, model=model, budget=budget)
    return text, info


def _ask(instructions, material, cwd, timeout, model, record, system_extra=None):
    if not EXE.exists():
        return None, {"error": f"claude.exe not found at {EXE}"}
    if system_extra:
        try:
            sysargs = ["--system-prompt-file", str(_system_file(cwd, system_extra)), *LEAN_REST]
        except OSError as e:
            return None, {"error": f"could not write the system prompt file: {e}"}
    else:
        sysargs = lean()
    import common as C
    cmd = [str(EXE), "-p", C.personalize(instructions), "--output-format", "json", *sysargs]
    if model:
        cmd += ["--model", model]
    env = _env()
    if model and _thinking_off(model):
        env = dict(env or os.environ)
        env["MAX_THINKING_TOKENS"] = "0"
    t0 = dt.datetime.now()
    try:
        p = subprocess.run(cmd, input=material, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, cwd=str(cwd), env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        # a timed-out call may still have used tokens; record how long it ran so the ledger shows it
        return None, {"error": "timed out", "ms": int((dt.datetime.now() - t0).total_seconds() * 1000)}
    try:
        text, info, is_err = _parse(p.stdout)
    except Exception:
        return _fail(f"no JSON from claude (exit {p.returncode}): {(p.stderr or p.stdout)[-300:]}", record)
    if is_err:
        t, i = _fail(text[:300], record)
        i.update({k: info[k] for k in ("ms", "cost_usd", "usage", "models", "turns")})   # errors can cost tokens too
        return t, i
    _note(True, None, record)
    return text or None, info
