"""Phase B (concept analysis plan, 2026-10-06): meaning-based candidates for an analysis's Related list.

A small local embedding model (fastembed, default BAAI/bge-small-en-v1.5: ONNX on the CPU, no API key, ~130 MB
downloaded once into %LOCALAPPDATA%\\VaultLibrarian\\models) embeds every source note's title, description, tags
and opening words. The index (index\\embeddings.npz in local_root) is updated each night by nightly.py and on demand;
only new or changed notes are embedded. analyze.candidates() merges these neighbours with its word-overlap ranking;
if fastembed isn't installed (setup\\install_embeddings.ps1) everything falls back to word overlap.
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

DEFAULTS = {"enabled": True, "model": "BAAI/bge-small-en-v1.5", "words": 400, "batch": 16}
SKIP_TOP = {"Claude", "Archives", "Templates", "attachments", "_Obsidian Internal", "Concepts", "Daily Notes"}
SKIP_TYPES = {"Analysis", "Part Analysis", "Concept", "Transcript", "Podcast Transcript", "Topic Map", "Tag Vocabulary",
              "Agent Instructions", "Daily Note"}
_model = None


def rcfg(cfg):
    d = dict(DEFAULTS)
    d.update(cfg.get("related") or {})
    return d


def available(cfg):
    if not rcfg(cfg)["enabled"]:
        return False
    try:
        import fastembed  # noqa: F401
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False


def _paths(cfg):
    root = Path(os.path.expandvars(cfg["local_root"]))
    (root / "index").mkdir(parents=True, exist_ok=True)
    (root / "models").mkdir(parents=True, exist_ok=True)
    return root / "index" / "embeddings.npz", root / "models"


def model(cfg):
    global _model
    if _model is None:
        _, mdir = _paths(cfg)
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")      # Windows: plain copies are fine
        if any(mdir.glob("models--*")):                                    # downloaded once: no network check each night
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from fastembed import TextEmbedding
        _model = TextEmbedding(model_name=rcfg(cfg)["model"], cache_dir=str(mdir))
    return _model


def embed(cfg, texts):
    import numpy as np
    vecs = np.array(list(model(cfg).embed(texts, batch_size=int(rcfg(cfg).get("batch", 16)))), dtype="float32")
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / np.maximum(norms, 1e-9)


def note_text(title, description, tags, body, words=400):
    body = re.sub(r"(?s)^---.*?\n---\n", "", body)
    body = re.sub(r"!\[\[[^\]]*\]\]|\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", body)
    return f"{title}. {description} Tags: {', '.join(tags)}. " + " ".join(body.split()[:words])


def _scan(cfg):
    sys.path.insert(0, str(C.VL))
    from vaultlib import split_frontmatter
    vault = Path(cfg["vault_path"])
    out = {}
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        rel_root = Path(root).relative_to(vault)
        if rel_root.parts and rel_root.parts[0] in SKIP_TOP:
            dirs[:] = []
            continue
        for f in files:
            if not f.endswith(".md") or f in ("index.md", "log.md", "CLAUDE.md"):
                continue
            p = Path(root) / f
            try:
                fm, _, body = split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                continue
            fm = fm if isinstance(fm, dict) else {}
            if str(fm.get("type")) in SKIP_TYPES or f[:-3].endswith((" - Transcript", " (Transcript)")):
                continue
            tags = [str(t) for t in (fm.get("tags") or []) if t] if isinstance(fm.get("tags"), list) else []
            txt = note_text(str(fm.get("title") or f[:-3]), str(fm.get("description") or ""), tags, body, rcfg(cfg)["words"])
            out[f[:-3]] = (hashlib.sha1(txt.encode("utf-8")).hexdigest()[:16], txt)
    return out


def load(cfg):
    import numpy as np
    ip, _ = _paths(cfg)
    if not ip.exists():
        return [], [], None
    z = np.load(ip, allow_pickle=False)
    return [str(x) for x in z["names"]], [str(x) for x in z["hashes"]], z["vecs"]


def update_index(cfg, log=print):
    """Embed new/changed notes, drop removed ones. Returns a one-line summary."""
    import numpy as np
    if not available(cfg):
        return "skipped (fastembed not installed: run setup\\install_embeddings.ps1)"
    notes = _scan(cfg)
    names, hashes, vecs = load(cfg)
    keep = {n: i for i, n in enumerate(names) if n in notes and notes[n][0] == hashes[i]}
    todo = [n for n in notes if n not in keep]
    dim = vecs.shape[1] if vecs is not None else 384
    chunks = [embed(cfg, [notes[n][1] for n in todo[i:i + 64]]) for i in range(0, len(todo), 64)]
    new_vecs = np.vstack(chunks) if chunks else np.zeros((0, dim), "float32")
    kept = [n for n in names if n in keep]
    allv = np.vstack([vecs[[keep[n] for n in kept]] if kept else np.zeros((0, new_vecs.shape[1]), "float32"), new_vecs])
    alln = kept + todo
    ip, _ = _paths(cfg)
    tmp = ip.with_name("embeddings.tmp.npz")
    np.savez(tmp, names=np.array(alln), hashes=np.array([notes[n][0] for n in alln]), vecs=allv)
    tmp.replace(ip)
    msg = f"{len(alln)} notes indexed ({len(todo)} embedded now, {len(names) - len(kept)} dropped)"
    log(f"related index: {msg}")
    return msg


def neighbours(cfg, name, text, k=20):
    """Names of the k notes closest in meaning to `text` (the note itself excluded). [] when unavailable."""
    if not available(cfg):
        return []
    try:
        import numpy as np
        names, _, vecs = load(cfg)
        if vecs is None or not len(names):
            return []
        q = embed(cfg, [text])[0]
        sims = vecs @ q
        order = np.argsort(-sims)
        return [str(names[i]) for i in order if str(names[i]) != name][:k]
    except Exception:  # noqa: BLE001  a convenience: never stop an analysis
        return []


if __name__ == "__main__":
    c = C.load_config()
    print(update_index(c))
