"""Title check (2026-10-06): catch items named after their file ("CASPEH_Report_62023", "CD8332.531-574",
"sewtha.dvi") before anything is filed or analyzed under that name.

  looks_bad(t)                       -> True for a file-name-like or meaningless title
  check_file(cfg, log, store, item)  -> after extraction: find the real title. If the document states it (the title
                                        the model reads off the opening pages appears in the text itself), apply it at
                                        once (title, author, published into extract.json; the old name stays an alias).
                                        Otherwise hold the item for the owner ("title" question). Returns the hold reason or None.
  check_note(cfg, log, store, item, n, text) -> the same for a filed vault note about to be analyzed; a filed note is
                                        never renamed without the owner, so a bad name is always held with a suggestion.
  apply(cfg, log, store, item, fields) -> the owner's answer (email, queue page): set title/fields, rename the vault note
                                        if there is one (links and analysis fingerprints updated), release the item.
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

PROMPT = """You read the OPENING PAGES of a document from «owner's» files and report its bibliographic details.
Return ONLY one JSON object:
{"title": "the work's real title, as printed (title case as printed; no file names, codes or page numbers)",
 "subtitle": "" or the subtitle, "author": ["names as printed"], "published": "YYYY or YYYY-MM-DD or \\"\\"",
 "publisher": "" or the publisher / institution / journal, "kind": "book|report|article|paper|newsletter|other",
 "evidence": "the exact words (up to 20) from the text where the title appears, or \\"\\" if you had to infer it"}
Use only what the text shows; leave a field empty rather than guess."""


def looks_bad(t):
    s = re.sub(r"\s+", " ", str(t or "")).strip()
    if not s:
        return True
    try:
        sys.path.insert(0, str(C.VL))
        from vaultlib import junk_title
        if junk_title(s):
            return True
    except Exception:  # noqa: BLE001
        pass
    if "_" in s or re.search(r"\.\d{1,3}$|\.(dvi|pdf|docx?)$", s, re.I):
        return True
    if re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+){2,}", s):                     # populism-and-the-fed (a slug)
        return True
    if re.match(r"^[\d\W]{3,}", s) or re.search(r"\b[A-Z]{1,4}\d{3,}", s):  # 00-9389-2 FM, CD8332...
        return True
    if re.search(r"\b[A-Za-z]*[a-z][A-Z][a-z]+[A-Z][a-z]+[A-Z][A-Za-z]*\b", s) and any(len(w) >= 18 for w in s.split()):
        return True                                                         # MistrustBasedDigitalRightsManagement
    letters = re.sub(r"[^A-Za-z]", "", s)
    if len(letters) >= 12 and letters.isupper() and " " not in s.strip():
        return True
    return False


# ---------------------------------------------------------------- display names (the owner's rules, 2026-10-06)
# (i) title case, not all lower or all upper; (ii) no hyphens between every word; (iii) no "-webPDF"; (iv) no
# underscores; (v) no leading numbers unless the text shows them as part of the title; and, when it can be identified
# with decent confidence, the author (a person's surname, or an institution / publication) in parentheses.
SMALL = {"a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into", "nor", "of", "on", "or", "over",
         "the", "to", "vs", "via", "with", "per"}
ORG = re.compile(r"\b(institute|institution|university|center|centre|foundation|council|bank|press|inc|llc|agency|"
                 r"department|office|association|society|commission|committee|bureau|board|school|college|"
                 r"group|corporation|company|trust|fund|federal|national|ministry|government|times|journal|review)\b", re.I)


def title_case(t):
    words = t.split()
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        if 0 < i < len(words) - 1 and lw in SMALL and not out[-1].endswith(":"):
            out.append(lw)
        elif re.fullmatch(r"[ivxlc]+", lw) and len(lw) <= 4 and i > 0:
            out.append(lw.upper())                                    # roman numerals
        else:
            out.append(lw[:1].upper() + lw[1:] if lw else w)
    return " ".join(out)


def clean_title(t, keep_numbers=False):
    s = re.sub(r"\s+", " ", str(t or "")).strip()
    s = re.sub(r"(?i)\.(pdf|epub|mobi|azw3|docx?|dvi)$", "", s)
    s = re.sub(r"(?i)[-_ ]*web[-_ ]?pdf$", "", s)
    s = s.replace("_", " ")
    if not keep_numbers:
        s = re.sub(r"^\d{1,3}[\s.\-]+(?=\D)", "", s)
    if " " not in s.strip() and s.count("-") >= 1 and re.fullmatch(r"[A-Za-z0-9'\-]+", s):
        s = s.replace("-", " ")                                      # heroes-of-progress
    s = re.sub(r"\s+", " ", s).strip(" -")
    letters = re.sub(r"[^A-Za-z]", "", s)
    if letters and (letters.islower() or letters.isupper()):
        s = title_case(s)
    elif s[:1].islower():
        s = s[:1].upper() + s[1:]
    return s


def source_label(j):
    """'Bastiat', 'Cato Institute', 'The Atlantic' - or '' when nothing is known with decent confidence."""
    au = [a for a in (j.get("author") or []) if a and len(a) > 2]
    if au:
        a = au[0].strip()
        if ORG.search(a):
            return a
        last = re.sub(r",.*$", "", a).split()[-1]
        return last + (" et al." if len(au) > 2 else (" & " + re.sub(r",.*$", "", au[1]).split()[-1] if len(au) == 2 else ""))
    return (j.get("publisher") or "").strip()


def display_name(title, j=None, head=""):
    """'Title (Author year)' (2026-10-06): the year only when it has a good source - the document's own date
    (published) and, when the opening pages are given, that year appears in them."""
    j = j or {}
    lab = source_label(j)
    t = clean_title(title)
    yr = str(j.get("published") or "")[:4]
    if not re.fullmatch(r"(1[5-9]|20)\d\d", yr) or (head and yr not in head):
        yr = ""
    tail = " ".join(x for x in (lab if lab and lab.lower() not in t.lower() else "", yr) if x)
    return f"{t} ({tail})" if tail else t


def retitle_pending(cfg, log, store, limit=15):
    """Queue items waiting to be filed (files: ready/held/new) get a clean display name with their source, once each.
    The title is taken from the opening pages when the text states it (one small call), else the cleaned file name.
    Writes item["title"] (what the queue page and the email show) and fills extract.json title/author when stated.
    Returns report rows."""
    import vqueue as Q
    rows = []
    for it in store.all():
        if len(rows) >= limit:
            break
        w = it.get("work") or {}
        if it.get("kind") != "file" or w.get("retitled") or it.get("status") not in ("ready", "held", "new", "failed") \
                or not w.get("extracted") or (it.get("manual") or {}).get("title_ok"):
            continue
        pay = store.payload(it["id"])
        try:
            meta = json.loads((pay / "extract.json").read_text(encoding="utf-8"))
            head = (pay / "text.md").read_text(encoding="utf-8")[:12000]
        except OSError:
            continue
        old = it.get("title") or ""
        j = {}
        try:
            j = ask_model(cfg, log, store, it, head) or {}
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "LimitHit":
                break
        if j.get("title") and stated(j["title"], head):
            title = clean_title(j["title"], keep_numbers=True)
            if not meta.get("title") or looks_bad(meta.get("title")):
                meta["title"] = title + (f": {j['subtitle']}" if j.get("subtitle") else "")
            for k in ("author", "published", "publisher"):
                if j.get(k) and not meta.get(k):
                    meta[k] = j[k]
            (pay / "extract.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            src = j if (j.get("author") or j.get("publisher")) and stated(source_label(j).split(" &")[0].split(" et al")[0], head) else {}
        else:
            title, src = clean_title(old), {}
        if j.get("published") and not src and j.get("title") and stated(j["title"], head):
            src = {"published": j["published"]}
        new = display_name(title, src, head)
        it["title"] = new
        w["retitled"] = True
        it["work"] = w
        if new != old:
            Q.event(it, f"display name: '{old}' -> '{new}'", cfg)
            rows.append((new, Q.route_of(it), f"renamed from '{old}'"))
        store.save(it)
    return rows


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def stated(title, text):
    t = _norm(title)
    return bool(t) and len(t) >= 6 and t in _norm(text[:60000])


def ask_model(cfg, log, store, item, head):
    import llm
    import routes as RT
    rid = "title-" + hashlib.sha1(head[:5000].encode("utf-8")).hexdigest()[:10]
    ans = llm.run(cfg, "title", [{"id": rid, "instructions": PROMPT, "material": "OPENING PAGES:\n\n" + head[:9000],
                                  "timeout": 180}], store.payload(item["id"]) / "answers", log, item["id"])
    j = RT.as_json(RT.text_of(ans, rid) or "")
    return j if isinstance(j, dict) and j.get("title") else None


def _hold(cfg, item, old, sug, why_more=""):
    import vqueue as Q
    item.setdefault("work", {})["hold_kind"] = "title"
    item["title_suggest"] = sug
    Q.set_status(item, "held", f"title looks like a file name ('{old}'); suggested: "
                 + (f"'{sug.get('name') or sug.get('title')}'" if sug and sug.get("title") else "none found") + why_more, "confirm", cfg)
    return item["why"]


def check_file(cfg, log, store, item):
    """Called by routes.extract_item after extraction. Returns a hold reason, or None to go on."""
    if (item.get("manual") or {}).get("title_ok"):
        return None
    pay = store.payload(item["id"])
    meta = json.loads((pay / "extract.json").read_text(encoding="utf-8"))
    import routes as RT
    current = RT.title_of(item, meta)
    if not looks_bad(current):
        return None
    head = (pay / "text.md").read_text(encoding="utf-8")[:12000]
    j = ask_model(cfg, log, store, item, head)
    if not j:
        return _hold(cfg, item, current, None)
    full = j["title"] + (f": {j['subtitle']}" if j.get("subtitle") else "")
    if stated(j["title"], head) and not looks_bad(j["title"]):
        meta["title"] = full
        for k in ("author", "published", "publisher"):
            if j.get(k) and not meta.get(k):
                meta[k] = j[k]
        meta.setdefault("aliases", []).append(current)
        (pay / "extract.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        old = item.get("title")
        item["title"] = display_name(j["title"], j if stated(source_label(j).split(" &")[0], head) else
                                     {"published": j.get("published")}, head)
        item.setdefault("work", {})["retitled"] = True
        import vqueue as Q
        Q.event(item, f"title check: '{old}' -> '{full}' (stated in the document)", cfg)
        return None
    return _hold(cfg, item, current, dict(j, title=full), " (Claude inferred it; please confirm)")


def suggest_name(j, fm=None):
    """A vault note name in the owner's style: 'Short title (Author year)'."""
    t = re.split(r"[:—–]", j.get("title") or "")[0].strip()
    t = t[:1].upper() + t[1:].lower() if t.isupper() else t
    who = (j.get("author") or [""])[0].split()[-1] if j.get("author") else (j.get("publisher") or "")
    yr = str(j.get("published") or "")[:4]
    tail = " ".join(x for x in (who, yr) if x)
    return f"{t} ({tail})" if tail else t


def check_note(cfg, log, store, item, n, text):
    """Called by analyze.do_analyze before any analysis of a filed note. Returns a hold reason, or None."""
    if (item.get("manual") or {}).get("title_ok") or not looks_bad(n["name"]):
        return None
    if not looks_bad(n.get("title") or ""):          # its own title property is fine: suggest that as the name
        sug = {"title": n["title"], "name": n["title"]}
    else:
        j = ask_model(cfg, log, store, item, text[:12000]) or {}
        sug = dict(j, name=suggest_name(j)) if j.get("title") else None
    return _hold(cfg, item, n["name"], sug)


# ---------------------------------------------------------------- applying the owner's choice
def _h(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def rename_note(cfg, log, rel, new_name, fields):
    """Rename a filed note (the owner approved), set fields, update [[links]] and analysis fingerprints. Returns new rel."""
    import yaml
    sys.path.insert(0, str(C.VL))
    from vaultlib import split_frontmatter
    vault = Path(cfg["vault_path"])
    old = Path(rel).stem
    work = Path(os.path.expandvars(cfg["local_root"])) / "work"
    work.mkdir(parents=True, exist_ok=True)
    item = {"path": rel, "set": {k: v for k, v in fields.items() if v}}
    if new_name and new_name != old:
        item.update(rename_to=new_name, rename_approved=True)
    pj = work / "title-plan.json"
    pj.write_text(json.dumps({"actor": C.owner(cfg)["id"], "log": "Title check (approved)", "items": [item]},
                             ensure_ascii=False), encoding="utf-8")
    rc, out = C.run(log, C.VL / "apply_plan.py", vault, pj)
    if rc:
        raise RuntimeError("apply_plan failed: " + out[-300:])
    if not new_name or new_name == old:
        return rel
    pat = re.compile(r"\[\[((?:[^\]|]*/)?)" + re.escape(old) + r"(\|[^\]]*)?\]\]")
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if not f.endswith(".md") or f == "log.md":
                continue
            p = Path(root) / f
            t = p.read_text(encoding="utf-8")
            t2 = pat.sub(lambda m: "[[" + m.group(1) + new_name + (m.group(2) or "") + "]]", t)
            if t2 == t:
                continue
            fm, _, body = split_frontmatter(t2)
            if isinstance(fm, dict) and fm.get("analysis_hash"):
                _, _, obody = split_frontmatter(t)
                if fm["analysis_hash"] == _h(obody.strip()):           # unedited: keep it recognisable as unedited
                    fm["analysis_hash"] = _h(body.strip())
                    t2 = "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000) + "---\n" + body
            p.write_text(t2, encoding="utf-8", newline="\n")
    return str(Path(rel).with_name(new_name + ".md")).replace("\\", "/")


def apply(cfg, log, store, item, fields, keep=False):
    """fields: title, name (vault note name), author (list), published, publisher, description, tags, my_note.
    keep=True: keep the current title; just let the item go on."""
    import vqueue as Q
    ref = item.get("source_ref") or {}
    if not keep:
        if item.get("kind") == "file" and (item.get("work") or {}).get("extracted"):
            pay = store.payload(item["id"])
            meta = json.loads((pay / "extract.json").read_text(encoding="utf-8"))
            if fields.get("title") and fields["title"] != meta.get("title"):
                meta.setdefault("aliases", []).append(item.get("title") or "")
                meta["title"] = fields["title"]
            for k in ("author", "published", "publisher", "description"):
                if fields.get(k):
                    meta[k] = fields[k]
            (pay / "extract.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            item["title"] = fields.get("title") or item.get("title")
            if fields.get("my_note"):
                item["my_note"] = fields["my_note"]
        elif ref.get("path"):
            setf = {k: fields[k] for k in ("title", "author", "published", "publisher", "description", "tags", "my_note")
                    if fields.get(k)}
            new_rel = rename_note(cfg, log, ref["path"], (fields.get("name") or "").strip(), setf)
            ref.update(path=new_rel, name=Path(new_rel).stem)
            item["title"] = fields.get("name") or fields.get("title") or item.get("title")
        else:   # an email or link not extracted yet: the title goes with the item into intake
            item["title"] = fields.get("title") or item.get("title")
    m = item.setdefault("manual", {})
    m["title_ok"] = True
    item.pop("title_suggest", None)
    if item.get("status") == "held" and (item.get("work") or {}).get("hold_kind") == "title":
        m["hold"] = False
        nxt = "ready" if item.get("kind") in ("email", "link", "note") or (item.get("work") or {}).get("extracted") else "new"
        Q.set_status(item, nxt, "title " + ("kept" if keep else "set") + " by you", cfg=cfg)
    store.save(item)
    return f"{item.get('title')}: title {'kept' if keep else 'set'}"
