#!/usr/bin/env python3
"""Keep documents in this app folder mirrored into the vault: one way, the app-folder file is the master.

OFF since 2026-10-12 12:17 (config doc_sync.enabled false): The owner wants all Claude documents to reach the vault through
the laptop task "Vault Cowork sync", which copies the master into Claude Cowork\\Obsidian (copy_out.py --sync) and
syncs it from there. Kept for reference and in case that changes.

2026-10-12: "keep Vault Librarian - User Manual.md sync'd to Obsidian". The manual is edited here (the app
folder) by laptop sessions; its vault copy is `Obsidian/Vault Librarian - User Manual.md`. Same rules as the
vault-librarian Cowork sync (sync_notes.py), whose helpers this uses, but for named files only, so it is quick:
  * the vault copy keeps its name, folder and frontmatter; only the body is replaced (local Markdown links become
    plain text); `verified` is dropped and `generated.at` refreshed when the text changes;
  * the prior body is saved under <vault>/.sync-backups/<stamp>/; each change is logged in log.md;
  * if the vault copy was edited in Obsidian since the last sync, it is NOT overwritten (reported as a conflict;
    `--force` overwrites);
  * tracking keys: synced_from = "the app/<file>", synced_hash, synced_body_hash, synced_at.
Run by Nightly (under the vault lock) for config `doc_sync.files`; by hand:
  python doc_sync.py [--force] [--adopt "<file>=<vault path>"] [--dry-run]
"""
import argparse
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

DEFAULT_FILES = {"Vault Librarian - User Manual.md": "Obsidian/Vault Librarian - User Manual.md"}
# A files entry is "<vault path>" or {"vault": "<vault path>", "type", "title", "description", "tags"}: with type and
# title, a missing vault copy is created (Inbox-free: straight at its vault path) with that OKF frontmatter.
PREFIX = f"{C.pc_name()} app/"


def files_of(cfg):
    return (cfg.get("doc_sync") or {}).get("files") or DEFAULT_FILES


def sync(cfg, log=print, force=False, dry=False, adopt=None):
    """Returns (lines for the report, needs). adopt: {file: vault_rel} to take over an earlier copy."""
    if not (cfg.get("doc_sync") or {}).get("enabled", True) and not adopt and not force:
        return (["off: documents reach the vault through the laptop task \"Vault Cowork sync\" (2026-10-12)"], [])
    if str(C.VL) not in sys.path:
        sys.path.insert(0, str(C.VL))
    import sync_notes as SN
    vault = Path(cfg["vault_path"])
    lines, needs, log_lines = [], [], []
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    now = SN.now_iso()
    for name, spec in files_of(cfg).items():
        spec = spec if isinstance(spec, dict) else {"vault": spec}
        vrel = spec["vault"]
        src = C.APP / name
        if not src.is_file():
            lines.append(f"{name}: source missing")
            continue
        vrel = (adopt or {}).get(name, vrel)
        dest = vault / vrel
        if not dest.is_file() and spec.get("type") and spec.get("title") and not dry:
            body0 = SN.body_of(SN.read(str(src)))
            fm0 = {k: spec[k] for k in ("type", "title", "description", "tags") if spec.get(k) is not None}
            fm0["generated"] = {"by": spec.get("generated_by", "claude/claude-opus-5-5"), "at": now}
            fm0.update({"synced_from": PREFIX + name, "synced_hash": "", "synced_body_hash": ""})
            SN.write(str(dest), SN.set_keys(body0.lstrip("\n"), fm0))
            log_lines.append(f"- Created `{vrel}` as a synced copy of the {C.pc_name()} app folder's `{name}`")
            lines.append(f"{name}: created {vrel}")
        if not dest.is_file():
            lines.append(f"{name}: vault copy {vrel} not found (moved? set doc_sync.files)")
            needs.append(f"the synced copy of {name} wasn't found at {vrel}")
            continue
        src_text, old = SN.read(str(src)), SN.read(str(dest))
        src_body = SN.body_of(src_text)
        fm, err, _ = SN.split_frontmatter(old)
        fm = fm if isinstance(fm, dict) else {}
        mine = str(fm.get("synced_from") or "") == PREFIX + name
        if not mine and not (adopt and name in adopt):
            lines.append(f"{name}: {vrel} is synced from {fm.get('synced_from')!r}, not from here; adopt it first")
            continue
        if mine and fm.get("synced_hash") == "":   # just created: write the tracked body below
            pass
        elif mine and fm.get("synced_hash") == SN.digest(src_body):
            lines.append(f"{name}: unchanged")
            continue
        elif mine and not force and fm.get("synced_body_hash") != SN.digest(SN.body_of(old)):
            lines.append(f"{name}: CONFLICT: {vrel} was edited in Obsidian since the last sync; not overwritten")
            needs.append(f"{vrel} was edited in Obsidian; its master is the app-folder file (run doc_sync.py --force "
                         f"to overwrite, or copy your edit into the app-folder file)")
            continue
        body, _w, _p = SN.convert_links(src_body, str(src), str(C.APP), {})
        m = SN.FM_RE.match(old)
        head = old[:m.end()] if m else ""
        new_text = head + ("\n" if head and not body.startswith("\n") else "") + body
        upd = {"synced_from": PREFIX + name, "synced_hash": SN.digest(src_body), "synced_body_hash": SN.digest(body),
               "synced_at": now}
        gen = fm.get("generated")
        if isinstance(gen, dict) and gen.get("by"):
            mtime = dt.datetime.fromtimestamp(os.path.getmtime(src)).astimezone().isoformat(timespec="seconds")
            upd["generated"] = {"by": gen["by"], "at": mtime}
        unset = [k for k in ("verified", SN.MARK) if k in fm]
        new_text = SN.set_keys(new_text, upd, unset)
        if dry:
            lines.append(f"{name}: would update {vrel}")
            continue
        SN.backup(str(vault), vrel, old, stamp)
        SN.write(str(dest), new_text)
        lines.append(f"{name}: updated {vrel}")
        log_lines.append(f"- Updated synced note `{vrel}` from the {C.pc_name()} app folder `{name}`; prior version in "
                         f"`.sync-backups/{stamp}/`")
    if log_lines:
        lp = vault / "log.md"
        existing = SN.read(str(lp)) if lp.exists() else "# Vault Update Log\n"
        SN.write(str(lp), existing.rstrip() + f"\n\n## {dt.date.today().isoformat()}\n\n**Document sync ({C.pc_name()} app "
                 f"folder)**\n\n" + "\n".join(log_lines) + "\n")
    for ln in lines:
        log("doc sync: " + ln)
    return lines, needs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--adopt", nargs="*", default=[])
    ap.add_argument("--vault")
    a = ap.parse_args(argv)
    import json
    cfg = json.loads((C.APP / "config" / "vault.json").read_text(encoding="utf-8"))
    if a.vault:
        cfg["vault_path"] = a.vault
    adopt = dict(x.split("=", 1) for x in a.adopt)
    lines, needs = sync(cfg, print, a.force, a.dry_run, adopt)
    for n in needs:
        print("NEEDS: " + n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
