"""One tiny claude -p call with the lean flags (phase 3 follow-up, 2026-09-28): prints tokens, cost, model.
  python lean_check.py            lean flags (what claude_cli.py uses)"""
import json, subprocess, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "jobs"))
import claude_cli
cmd = [str(claude_cli.EXE), "-p", "Reply with the single word OK.", "--output-format", "json", *claude_cli.LEAN]
p = subprocess.run(cmd, input="hello", capture_output=True, text=True, encoding="utf-8", errors="replace",
                   timeout=180, cwd=tempfile.gettempdir())
try:
    j = json.loads(p.stdout)
except Exception:
    print("exit", p.returncode, (p.stderr or p.stdout)[-800:]); sys.exit(1)
print(json.dumps({"result": j.get("result"), "is_error": j.get("is_error"), "cost_usd": j.get("total_cost_usd"),
                  "usage": {k: (j.get("usage") or {}).get(k) for k in ("input_tokens", "cache_creation_input_tokens",
                            "cache_read_input_tokens", "output_tokens")}, "models": list((j.get("modelUsage") or {}))}))
