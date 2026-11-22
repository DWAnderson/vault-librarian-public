"""Vault search plan, phase 3 (2026-10-11): helper for setup\\connector_setup.ps1. Run by that script, not by hand.

  python connector_setup.py probe            initialize call to the local connector (127.0.0.1:8767) with the token;
                                             prints OK and the tool count, or the error
  python connector_setup.py links <base>     point the email/page links at <base> (e.g. https://<your-host>.ts.net:8443):
                                             config\\vault.json organize.web_base = <base>/vault, and the Daily Summary's
                                             platform\\adapters\\youtube\\detail_server.json url_base = <base>.
                                             Backups go to setup\\_backup-<date>-connector\\ first.
Never prints the token.
"""
import datetime as dt
import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
CFG = json.loads((APP / "config" / "vault.json").read_text(encoding="utf-8"))
LOCAL = Path(os.path.expandvars(CFG["local_root"]))
# Optional: a companion Daily Summary app whose page links are pointed at the same address (config
# connector_daily_summary = its detail_server.json; default: the sibling "Daily Summary - <pc_name>" folder)
DS = Path(os.path.expandvars(CFG.get("connector_daily_summary") or str(
    APP.parent / f"Daily Summary - {CFG.get('pc_name') or ''}" / "platform" / "adapters" / "youtube" / "detail_server.json")))


def probe():
    tok = (LOCAL / "credentials" / "mcp_token.txt").read_text(
        encoding="utf-8").strip()
    h = {"content-type": "application/json", "accept": "application/json, text/event-stream",
         "authorization": "Bearer " + tok}
    out = []
    for i, (method, params) in enumerate([("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                           "clientInfo": {"name": "setup", "version": "1"}}),
                                          ("tools/list", {})], 1):
        body = json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}).encode()
        for attempt in range(20):          # the connector may take a few seconds to load its libraries
            try:
                r = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8767/mcp", body, h), timeout=30)
                out.append(json.loads(r.read()))
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 19:
                    print(f"FAILED: {method}: {type(e).__name__}: {e}")
                    return 1
                time.sleep(2)
    tools = [t["name"] for t in out[1]["result"]["tools"]]
    print(f"OK: {out[0]['result']['serverInfo']['name']}, {len(tools)} tools ({', '.join(tools)})")
    return 0 if len(tools) >= 7 else 1


def links(base):
    base = base.rstrip("/")
    bk = APP / "setup" / f"_backup-{dt.date.today():%Y%m%d}-connector"
    bk.mkdir(parents=True, exist_ok=True)
    vj = APP / "config" / "vault.json"
    shutil.copy2(vj, bk / "vault.json.before-links")
    cfg = json.loads(vj.read_text(encoding="utf-8"))
    old = (cfg.get("organize") or {}).get("web_base")
    text = vj.read_text(encoding="utf-8")
    # edit in place so the file's layout and comments stay as they are
    if old:
        text = text.replace(json.dumps(old), json.dumps(base + "/vault"), 1)
    else:
        text = text.replace('"organize": {', '"organize": {\n    "web_base": ' + json.dumps(base + "/vault") + ",", 1)
    json.loads(text)
    vj.write_text(text, encoding="utf-8", newline="")
    print(f"config\\vault.json organize.web_base: {old or '(default)'} -> {base}/vault")
    if DS.exists():
        shutil.copy2(DS, bk / "detail_server.json.before-links")
        d = json.loads(DS.read_text(encoding="utf-8"))
        was = d.get("url_base")
        d["url_base"] = base
        DS.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8", newline="")
        print(f"Daily Summary detail_server.json url_base: {was} -> {base}")
    else:
        print(f"WARNING: {DS} not found; the Daily Summary links were not changed")
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "probe":
        sys.exit(probe())
    if len(sys.argv) >= 3 and sys.argv[1] == "links":
        sys.exit(links(sys.argv[2]))
    print(__doc__)
    sys.exit(2)
