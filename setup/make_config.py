#!/usr/bin/env python3
"""Creates config/vault.json from config/vault.example.json by asking the main questions. Standard library only,
so it runs with the system Python before the app's runtime exists. Called by setup/install.ps1.

  python make_config.py                       ask, then write config/vault.json (refuses to overwrite one)
  python make_config.py --out <file>          write somewhere else (tests)
  python make_config.py --answers <json>      take the answers from a JSON file instead of asking (tests, scripted)

Answer keys: vault_path, timezone, owner_name, owner_about, pc_name, dropbox_path, mail (bool), mailbox, notify_to, youtube (bool),
connector (bool), search (bool). Everything else keeps the template's value; see docs/CONFIGURATION.md.
"""
import argparse
import json
import os
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
TEMPLATE = APP / "config" / "vault.example.json"
TARGET = APP / "config" / "vault.json"


def ask(prompt, default=""):
    shown = f" [{default}]" if default else ""
    v = input(f"{prompt}{shown}: ").strip()
    return v or default


def yes(prompt, default=False):
    v = ask(prompt + " (y/n)", "y" if default else "n").lower()
    return v.startswith("y")


def interview(cfg):
    print("\nAnswer a few questions; press Enter to keep the value in [brackets].\n")
    a = {}
    while True:
        a["vault_path"] = ask("Folder of your Obsidian vault", cfg["vault_path"])
        if Path(os.path.expandvars(a["vault_path"])).is_dir():
            break
        print("  That folder doesn't exist. Give the folder that contains your vault's .obsidian folder.")
    a["timezone"] = ask("Time zone (IANA name, e.g. America/New_York, Europe/London)", cfg["timezone"])
    a["owner_name"] = ask("Your first name, as Claude should call you (Enter: 'the owner')", "")
    if a["owner_name"]:
        a["owner_about"] = ask("One sentence about you for Claude, e.g. 'Sam is a historian.' (optional)", "")
    a["dropbox_path"] = ask("Dropbox folder for files to add (created if missing)", cfg["queue"]["dropbox_path"])
    print("\nOptional features. Each can be switched on later in config/vault.json (docs/CONFIGURATION.md).")
    a["mail"] = yes("Microsoft 365 mail (needs an Entra app registration for Graph)?", False)
    if a["mail"]:
        a["mailbox"] = ask("  Mailbox the jobs read and send from", "")
        a["notify_to"] = ask("  Address for notices and the daily update", a["mailbox"])
    a["youtube"] = yes("YouTube 'Remember' playlist summaries (needs a Google Cloud project and a Gemini key)?", False)
    a["connector"] = yes("Vault connector for claude.ai (needs a public HTTPS address, e.g. Tailscale Funnel)?", False)
    a["search"] = yes("Local search index and related-note suggestions (downloads a small model, about 130 MB)?", True)
    return a


def apply(cfg, a):
    cfg["vault_path"] = a.get("vault_path", cfg["vault_path"])
    cfg["timezone"] = a.get("timezone", cfg["timezone"])
    cfg["queue"]["dropbox_path"] = a.get("dropbox_path", cfg["queue"]["dropbox_path"])
    name = str(a.get("owner_name") or "").strip()
    if name:
        o = cfg.setdefault("owner", {})
        o.update(name=name, id="human:" + "".join(ch for ch in name.lower() if ch.isalnum()),
                 note_label=f"{name}'s note", about=str(a.get("owner_about") or "").strip())
    import socket
    cfg["pc_name"] = a.get("pc_name") or socket.gethostname()
    mail = bool(a.get("mail"))
    cfg.setdefault("mail", {})["enabled"] = mail
    if mail:
        cfg["mailbox"] = a.get("mailbox") or cfg["mailbox"]
        cfg["notify_to"] = a.get("notify_to") or cfg["mailbox"]
    else:
        cfg["mailbox"] = cfg["notify_to"] = ""
        cfg.setdefault("organize", {})["email_enabled"] = False   # questions on the home page instead
    yt = bool(a.get("youtube"))
    cfg.setdefault("remember", {})["enabled"] = yt
    cfg["remember"]["dry_run"] = not yt
    cfg.setdefault("mcp", {})["enabled"] = bool(a.get("connector"))
    s = bool(a.get("search", True))
    cfg.setdefault("search", {})["enabled"] = s
    cfg.setdefault("related", {})["enabled"] = s
    return cfg


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(TARGET))
    ap.add_argument("--answers")
    a = ap.parse_args(argv)
    out = Path(a.out)
    if out.exists():
        print(f"{out} already exists; leaving it as it is. Edit it, or delete it and run this again.")
        return 0
    cfg = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    answers = json.loads(Path(a.answers).read_text(encoding="utf-8")) if a.answers else interview(cfg)
    cfg = apply(cfg, answers)
    Path(os.path.expandvars(cfg["queue"]["dropbox_path"])).mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {out}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
