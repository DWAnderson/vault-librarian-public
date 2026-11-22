"""Claude usage summary from the per-call ledger (logs/llm-YYYY-MM.jsonl under local_root).

    python jobs\\usage_report.py                 last 7 days, by route and model
    python jobs\\usage_report.py --days 30       another span
    python jobs\\usage_report.py --by day        by day instead (also: route, model, caller)

cost_usd is the API-equivalent price Claude Code reports; on the owner's subscription it is a measure of how
much of the 5-hour/weekly allowance a call used, not money spent. Written 2026-11-13.
"""
import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402


def records(cfg, since):
    for p in sorted((Path(cfg["local_root"]) / "logs").glob("llm-*.jsonl")):
        for ln in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r.get("at", "")[:10] >= since:
                yield r


def key(r, by):
    if by == "day":
        return r["at"][:10]
    if by == "model":
        return ",".join(r.get("models") or []) or "(none)"
    if by == "caller":
        return r.get("caller") or "(llm.run)"
    return f'{r.get("route")} | {",".join(r.get("models") or []) or "-"}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--by", choices=["route", "day", "model", "caller"], default="route")
    a = ap.parse_args()
    cfg = C.load_config()
    since = (C.now(cfg) - dt.timedelta(days=a.days)).strftime("%Y-%m-%d")
    agg = defaultdict(lambda: [0, 0, 0, 0, 0, 0.0])   # calls, failed, input, cache write, cache read + output, cost
    for r in records(cfg, since):
        u = r.get("usage") or {}
        g = agg[key(r, a.by)]
        g[0] += 1
        g[1] += 0 if r.get("ok") else 1
        g[2] += u.get("input_tokens") or 0
        g[3] += u.get("cache_creation_input_tokens") or 0
        g[4] += (u.get("cache_read_input_tokens") or 0)
        g[5] += r.get("cost_usd") or 0
        g.append(u.get("output_tokens") or 0)
    rows = sorted(agg.items(), key=lambda kv: -kv[1][5])
    total = sum(v[5] for _, v in rows) or 1
    print(f"Claude usage since {since} (by {a.by})")
    print(f"{'':42} {'calls':>6} {'fail':>5} {'in+cache-write':>15} {'cache-read':>11} {'output':>9} {'$equiv':>9} {'share':>6}")
    for k, v in rows:
        out = sum(v[6:])
        print(f"{k[:42]:42} {v[0]:6} {v[1]:5} {v[2] + v[3]:15,} {v[4]:11,} {out:9,} {v[5]:9.2f} {v[5] / total:6.0%}")
    print(f"{'TOTAL':42} {sum(v[0] for _, v in rows):6} {'':5} {'':15} {'':11} {'':9} {sum(v[5] for _, v in rows):9.2f}")


if __name__ == "__main__":
    main()
