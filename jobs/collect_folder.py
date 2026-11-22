"""Folder collector for the intake queue: the OneDrive dropbox `Remember` (queue.dropbox_path) -> items.

Capture only: no model calls, nothing written to the vault. A file stays in the dropbox until
queue_run.finish() confirms its note (and original copy in attachments/Sources) exists.

  <root>\\file                 route auto (Claude picks; costly routes wait for the owner's OK)
  <root>\\Books\\file           route from queue.route_folders (Articles, Books, Recipes, Manuals,
                                Full text, Summarize, Other)
  file.txt next to file.pdf   the owner's note on file.pdf (a "sidecar"), not an item of its own
  .msg / .eml                 exactly like a Remember email (collect_mail.capture), attachments included
  .url, or a .txt holding only a link (+ up to ~30 words)  -> link item
Files still being written or synced (changed in the last queue.settle_minutes) wait for the next run.
Folders starting with "_" or "." and Office/OneDrive temp files are ignored.
"""
import datetime as dt
import email
import email.policy
import hashlib
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect_mail as M  # noqa: E402
import intake as I  # noqa: E402
import vqueue as Q  # noqa: E402
import routes as RT  # noqa: E402

SKIP_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}
TEMP = re.compile(r"(^~\$|^\.~|\.(tmp|partial|crdownload|part|download)$)", re.I)


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def files_in(root):
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part.startswith(("_", ".")) for part in rel.parts[:-1]):
            continue
        if p.is_file() and p.name.lower() not in SKIP_NAMES and not TEMP.search(p.name):
            yield p, rel.as_posix()


def is_sidecar(p):
    if p.suffix.lower() != ".txt":
        return False
    return any(q != p and q.stem == p.stem and q.suffix.lower() != ".txt" for q in p.parent.glob(p.stem + ".*"))


def sidecar_note(p):
    s = p.with_suffix(".txt")
    if p.suffix.lower() != ".txt" and s.exists():
        try:
            import extract as X
            return " ".join(X.read_text(s).split())[:1500]
        except Exception:
            return ""
    return ""


# ---------------------------------------------------------------- .eml / .msg -> Graph-like message
def _addr_list(v):
    out = []
    for a in re.split(r"[;,]\s*", v or ""):
        m = re.search(r"[\w.+'-]+@[\w.-]+", a)
        if m:
            out.append({"emailAddress": {"address": m.group(0)}})
    return out


def _iso(d):
    try:
        if isinstance(d, str):
            d = email.utils.parsedate_to_datetime(d)
        return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_email_file(p, sha, q):
    """-> (msg dict shaped like Graph's, [attachments with 'data' bytes])."""
    limit = int(q["skip_images_under_kb"]) * 1024
    atts = []
    if p.suffix.lower() == ".eml":
        m = email.message_from_bytes(p.read_bytes(), policy=email.policy.default)
        html = m.get_body(preferencelist=("html",))
        plain = m.get_body(preferencelist=("plain",))
        body = {"contentType": "html", "content": html.get_content()} if html else \
            {"contentType": "text", "content": plain.get_content() if plain else ""}
        for i, a in enumerate(m.iter_attachments()):
            data = a.get_payload(decode=True) or b""
            ctype = a.get_content_type()
            inline = (a.get_content_disposition() == "inline") and bool(a.get("Content-ID"))
            if inline or (ctype.startswith("image/") and len(data) < limit):
                continue
            atts.append({"id": f"a{i}", "name": a.get_filename() or f"attachment-{i}", "size": len(data),
                         "content_type": ctype, "is_item": False, "data": data})
        msg = {"subject": str(m.get("subject") or ""), "from": (_addr_list(str(m.get("from") or "")) or [{}])[0],
               "toRecipients": _addr_list(str(m.get("to") or "")), "ccRecipients": _addr_list(str(m.get("cc") or "")),
               "receivedDateTime": _iso(m.get("date") or ""), "internetMessageId": str(m.get("message-id") or ""),
               "body": body}
    else:
        import extract_msg
        m = extract_msg.openMsg(str(p))
        try:
            hb = m.htmlBody
            body = {"contentType": "html", "content": hb.decode("utf-8", "replace") if isinstance(hb, bytes) else hb} \
                if hb else {"contentType": "text", "content": m.body or ""}
            for i, a in enumerate(m.attachments):
                data = getattr(a, "data", None)
                if not isinstance(data, (bytes, bytearray)):
                    continue   # an attached email inside the .msg: not handled yet
                name = a.getFilename() if hasattr(a, "getFilename") else (a.longFilename or a.shortFilename or f"attachment-{i}")
                ctype = (getattr(a, "mimetype", None) or "").lower()
                if getattr(a, "cid", None) and ctype.startswith("image/"):
                    continue
                if (ctype.startswith("image/") or str(name).lower().endswith((".png", ".jpg", ".jpeg", ".gif"))) and len(data) < limit:
                    continue
                atts.append({"id": f"a{i}", "name": name, "size": len(data), "content_type": ctype, "is_item": False,
                             "data": bytes(data)})
            msg = {"subject": m.subject or "", "from": (_addr_list(m.sender or "") or [{}])[0],
                   "toRecipients": _addr_list(m.to or ""), "ccRecipients": _addr_list(m.cc or ""),
                   "receivedDateTime": _iso(m.date) if m.date else _iso(""), "internetMessageId": m.messageId or "",
                   "body": body}
        finally:
            m.close()
    msg.update({"id": f"file-{sha[:24]}", "webLink": None, "hasAttachments": bool(atts)})
    return msg, atts


def save_att(att, dest_dir):
    dest_dir.mkdir(parents=True, exist_ok=True)
    p = dest_dir / re.sub(r'[\\/:*?"<>|]', "_", att["name"])[:120]
    p.write_bytes(att["data"])
    return p


# ---------------------------------------------------------------- collect
def link_in(p):
    """A .url file, or a .txt that is just a link (+ a few words): -> (url, note) else None."""
    import extract as X
    t = X.read_text(p)
    if p.suffix.lower() == ".url":
        m = re.search(r"(?mi)^URL=(\S+)", t)
        return (m.group(1), "") if m else None
    urls = X.URL_RE.findall(t)
    rest = X.URL_RE.sub(" ", t)
    if len(set(urls)) == 1 and X.words(rest) <= 30:
        return urls[0].rstrip(".,;"), " ".join(rest.split())
    return None


def collect(cfg, log, store, dry):
    """Returns (rows, new items)."""
    q = Q.qcfg(cfg)
    dp = (q.get("dropbox_path") or "").strip()
    root = Path(dp) if dp else None
    # Never fall back to the current folder: an empty or relative path must mean "no dropbox" (2026-09-28 incident)
    if not q["files_enabled"] or root is None or not root.is_absolute() or not root.is_dir():
        return [], []
    app = Path(__file__).resolve().parent.parent
    if root.resolve() in (app, *app.parents) or app in root.resolve().parents or Path(cfg["vault_path"]).resolve() in (root.resolve(), *root.resolve().parents):
        return [("(dropbox)", "-", f"refused: {root} is, contains or is inside the app or vault folder")], []
    lower = {k.lower(): v for k, v in q["route_folders"].items()}
    settle = float(q.get("settle_minutes", 3)) * 60
    known = {}
    for it in store.all():
        if it.get("source") == "folder" and not it.get("parent"):
            known.setdefault((it.get("source_ref") or {}).get("path"), []).append(it)
    rows, new, present = [], [], set()
    for p, rel in files_in(root):
        present.add(rel)
        if is_sidecar(p):
            continue
        st = p.stat()
        mine = known.get(rel) or []
        same = [x for x in mine if (x.get("file") or {}).get("mtime") == st.st_mtime and
                (x.get("file") or {}).get("bytes") == st.st_size]
        if same or any(x["status"] in Q.OPEN for x in mine):
            continue
        if time.time() - st.st_mtime < settle:
            continue   # still arriving; next run
        top = rel.split("/")[0] if "/" in rel else None
        froute = lower.get(top.lower(), "auto") if top else None
        folder = f"Remember/{top}" if top else "Remember"
        sha = sha256(p)
        ref = {"path": rel, "sha256": sha, "folder": folder}
        fileinfo = {"name": p.name, "ext": p.suffix.lower(), "bytes": st.st_size, "mtime": st.st_mtime, "sha256": sha}
        note = sidecar_note(p)
        try:
            if p.suffix.lower() in (".msg", ".eml"):
                msg, atts = read_email_file(p, sha, q)
                item, kids, row = M.capture(cfg, store, None, None, msg, folder, froute, True, source="folder",
                                            ref_extra=ref, atts=atts, fetch=save_att)
                item["file"] = fileinfo
                if not dry:   # capture() was told dry=True so the file info could be added first
                    import json
                    RT.file_into_folder(cfg, store, item, "auto")   # top-level email file -> Other (2026-09-28)
                    (store.payload(item["id"]) / "message.json").write_text(json.dumps(msg, ensure_ascii=False), encoding="utf-8")
                    for ch in kids:
                        a = next(x for x in atts if x["id"] == ch["source_ref"]["attachment_id"])
                        ch["file"]["payload"] = save_att(a, store.payload(ch["id"])).name
                        store.save(ch)
                    store.save(item)
                new += [item] + kids
                rows.append((p.name, folder, "email file: " + row))
                continue
            lk = link_in(p) if p.suffix.lower() in (".url", ".txt") else None
            if lk:
                item = store.create(source="folder", source_ref=ref, kind="link", route="link", route_from="classifier",
                                    title=p.stem, url=lk[0], doug_note=(note or lk[1]), file=fileinfo)
                Q.set_status(item, "ready", cfg=cfg)
                row = f"link: {lk[0][:80]}"
            else:
                dup = next((x for x in store.all() if (x.get("file") or {}).get("sha256") == sha
                            and x["status"] in ("done", "duplicate") and (x.get("result") or {}).get("notes")), None)
                item = store.create(source="folder", source_ref=ref, kind="file", title=p.stem,
                                    route=froute or "auto", route_from="folder" if froute else None,
                                    doug_note=note, file=fileinfo)
                if dup:
                    item["result"]["notes"] = list(dup["result"]["notes"])
                    Q.set_status(item, "duplicate", "same file already filed: " + ", ".join(dup["result"]["notes"]), cfg=cfg)
                    row = "duplicate of " + ", ".join(dup["result"]["notes"])
                else:
                    Q.set_status(item, "new", cfg=cfg)
                    row = f"file ({froute or 'auto'})"
            if not dry:
                if item.get("kind") == "link":
                    RT.file_into_folder(cfg, store, item, "auto")   # top-level link file -> Other
                store.save(item)
            new.append(item)
            rows.append((p.name, folder, row))
        except Exception as e:
            rows.append((p.name, folder, f"capture error: {type(e).__name__}: {e}"))
    if not dry:
        for rel, its in known.items():
            for x in its:
                if rel not in present and x["status"] in Q.OPEN:
                    Q.set_status(x, "cancelled", "the file is no longer in the dropbox", cfg=cfg)
                    store.save(x)
                    rows.append((x.get("title") or rel, "-", "cancelled: file no longer in the dropbox"))
    return rows, new
