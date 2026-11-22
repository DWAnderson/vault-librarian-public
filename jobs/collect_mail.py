"""Mail collector for the intake queue: Outlook Inbox\\Remember and its subfolders -> queue items.

Capture only: no model calls, nothing written to the vault, nothing moved in Outlook. The email
stays in Remember until queue_run.finish() confirms its note (and every attachment's) is written.

Per message (plan section 2):
  confidential sender (intake.confidential_domains)  -> email item, held for the owner
  one link and <= ~30 words                           -> link item
  attachments and <= attachment_email_max_words words -> "container" item (no Email note) with one
                                                         child item per attachment; the body text
                                                         becomes the owner's note on them   [files_enabled]
  anything else                                       -> email item; attachments become child items [files_enabled]
A message in a subfolder (Remember/Books ...) passes that folder's route to its attachments; for
an email body it is kept as work.route_hint for phase 2. Classification reuses intake.classify,
so emails and links are judged exactly as the current intake judges them.
"""
import base64
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import intake as I  # noqa: E402
import vqueue as Q  # noqa: E402

SEL = ("id,subject,from,toRecipients,ccRecipients,receivedDateTime,internetMessageId,webLink,body,"
       "hasAttachments,parentFolderId")
WORD = re.compile(r"\w+")


def mail_folders(s, base, name, route_folders):
    """[(folder id, 'Remember' or 'Remember/Books', route or None, total items)]."""
    fid, total = I.remember_folder(s, base, name)
    out = [(fid, name, None, total)]
    r = s.get(f"{base}/mailFolders/{fid}/childFolders",
              params={"$select": "id,displayName,totalItemCount", "$top": 100}, timeout=30)
    if r.ok:
        lower = {k.lower(): v for k, v in route_folders.items()}
        for f in r.json().get("value", []):
            out.append((f["id"], f"{name}/{f['displayName']}", lower.get(f["displayName"].lower(), "auto"),
                        f.get("totalItemCount", 0)))
    return out


def real_attachments(s, base, msg, q):
    """Attachments worth filing: not inline, not small images (signature logos, tracking pixels)."""
    if not msg.get("hasAttachments"):
        return []
    r = s.get(f"{base}/messages/{msg['id']}/attachments",
              params={"$select": "id,name,size,isInline,contentType"}, timeout=30)
    if not r.ok:
        return []
    out = []
    for a in r.json().get("value", []):
        ctype = (a.get("contentType") or "").lower()
        if a.get("isInline"):
            continue
        if ctype.startswith("image/") and (a.get("size") or 0) < int(q["skip_images_under_kb"]) * 1024:
            continue
        out.append({"id": a["id"], "name": a.get("name") or "attachment", "size": a.get("size"),
                    "content_type": ctype, "is_item": "itemAttachment" in (a.get("@odata.type") or "")})
    return out


def own_text(msg, c):
    """Body text without links and without words that only repeat the subject."""
    text = I.URL_RE.sub(" ", c.get("md") or "")
    subj = set(WORD.findall((msg.get("subject") or "").lower()))
    words = [w for w in WORD.findall(text) if w.lower() not in subj]
    return " ".join(" ".join(text.split()).split()[:80]), len(words)


def download_attachment(s, base, msg_id, att, dest_dir):
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "_", att["name"])[:120]
    if att["is_item"]:  # an attached email: keep it as .eml, handled like a dropped .eml
        r = s.get(f"{base}/messages/{msg_id}/attachments/{att['id']}/$value", timeout=120)
        r.raise_for_status()
        p = dest_dir / (Path(safe).stem + ".eml")
        p.write_bytes(r.content)
        return p
    r = s.get(f"{base}/messages/{msg_id}/attachments/{att['id']}", timeout=120)
    r.raise_for_status()
    p = dest_dir / safe
    p.write_bytes(base64.b64decode(r.json().get("contentBytes") or b""))
    return p


def capture(cfg, store, s, base, msg, folder, froute, dry, source="mail", ref_extra=None, atts=None, fetch=None):
    """Build (and unless dry, save) the queue item(s) for one message. Returns (item, children, row).
    Also used for a .msg/.eml dropped in the OneDrive dropbox (collect_folder): source="folder",
    ref_extra = {path, sha256, ...}, atts = its attachments, fetch(att, dest_dir) -> saved path."""
    q = Q.qcfg(cfg)
    subj = msg.get("subject") or "(no subject)"
    c = I.classify(msg, cfg)
    if c["kind"] == "link" and q["files_enabled"]:   # an attached file is the thing to save, not a stray link (2026-11-03)
        atts = real_attachments(s, base, msg, q) if atts is None else atts
        if atts:
            c = {"kind": "email", "md": c.get("md") or "", "words": c.get("words", 0)}
    ref = {"message_id": msg["id"], "internet_message_id": msg.get("internetMessageId"), "folder": folder,
           "web_link": msg.get("webLink"), "received": msg.get("receivedDateTime")}
    ref.update(ref_extra or {})
    common = dict(source=source, source_ref=ref, title=subj)
    children = []
    if c["kind"] == "held":
        item = store.create(kind="email", route="email", route_from="classifier", **common)
        item["work"]["hold_kind"] = "confidential"
        Q.set_status(item, "held", c["reason"], "you", cfg)
        row = "held: " + c["reason"]
    elif c["kind"] == "link":
        item = store.create(kind="link", route="link", route_from="classifier", url=c["url"],
                            doug_note=c.get("doug_note", ""), **common)
        Q.set_status(item, "ready", cfg=cfg)
        row = f"link: {c['url'][:80]}"
    else:
        if not q["files_enabled"]:
            atts = []
        elif atts is None:
            atts = real_attachments(s, base, msg, q)
        note, n_own = own_text(msg, c)
        if atts and n_own <= int(q["attachment_email_max_words"]):
            item = store.create(kind="email", route="container", route_from="classifier",
                                doug_note=note if n_own >= 3 else "", **common)
            row = f"attachment email: {len(atts)} file(s)"
        else:
            item = store.create(kind="email", route="email", route_from="classifier", **common)
            item["work"]["route_hint"] = froute
            row = f"email: {c['words']} words" + (" (summary)" if c.get("long") else "") + \
                  (f" + {len(atts)} attachment(s)" if atts else "")
        item["work"]["attachment_names"] = [a["name"] for a in atts]
        for a in atts:
            ext = ".eml" if a["is_item"] else Path(a["name"]).suffix.lower()
            ch = store.create(source=source, source_ref=dict(ref, attachment_id=a["id"]), parent=item["id"],
                              kind="file", title=a["name"], route=froute or "auto",
                              route_from="folder" if froute else None,
                              doug_note=item.get("doug_note", ""),
                              file={"name": a["name"], "ext": ext, "bytes": a.get("size"),
                                    "content_type": a.get("content_type")})
            children.append((ch, a))
        item["children"] = [ch["id"] for ch, _ in children]
        Q.set_status(item, "waiting" if item["route"] == "container" else "ready", cfg=cfg)
    if not dry:
        (store.payload(item["id"]) / "message.json").write_text(json.dumps(msg, ensure_ascii=False), encoding="utf-8")
        for ch, a in children:
            try:
                p = (fetch or (lambda att, d: download_attachment(s, base, msg["id"], att, d)))(a, store.payload(ch["id"]))
                ch["file"]["payload"] = p.name
            except Exception as e:  # keep the item; it will be held when processed
                ch["work"]["download_error"] = f"{type(e).__name__}: {e}"
            store.save(ch)
        store.save(item)
    return item, [ch for ch, _ in children], row


def collect(cfg, log, store, s, base, dry, skip=()):
    """Capture every new message in Remember and its subfolders. Returns (rows, new items, present):
    present = the message ids now in Remember, or None when a folder had more than one batch.
    skip: message ids not to capture (entries held.json still owns; held_bridge adopts them).
    Also cancels open mail items whose message is gone (the owner deleted or moved it)."""
    q, it = Q.qcfg(cfg), cfg["intake"]
    batch = int(it.get("batch", 20))
    rows, new, present, complete = [], [], set(), True
    for fid, folder, froute, total in mail_folders(s, base, it.get("remember_folder", "Remember"), q["route_folders"]):
        if not total:
            continue
        r = s.get(f"{base}/mailFolders/{fid}/messages", timeout=60,
                  params={"$top": batch, "$orderby": "receivedDateTime asc", "$select": SEL})
        r.raise_for_status()
        msgs = r.json().get("value", [])
        complete = complete and len(msgs) >= total
        for msg in msgs:
            present.add(msg["id"])
            if msg["id"] in skip or store.find(message_id=msg["id"]):
                continue
            try:
                item, kids, row = capture(cfg, store, s, base, msg, folder, froute, dry)
                new += [item] + kids
                rows.append((msg.get("subject") or "(no subject)", folder, row))
            except Exception as e:
                rows.append((msg.get("subject") or "(no subject)", folder, f"capture error: {type(e).__name__}: {e}"))
    if complete and not dry:
        for x in store.all():
            if (x.get("source") == "mail" and not x.get("parent") and x["status"] in Q.OPEN
                    and (x.get("source_ref") or {}).get("message_id") not in present):
                Q.set_status(x, "cancelled", "the email is no longer in Remember", cfg=cfg)
                store.save(x)
                rows.append((x.get("title") or "?", "-", "cancelled: email no longer in Remember"))
    return rows, new, (present if complete else None)
