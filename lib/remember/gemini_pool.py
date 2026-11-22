"""Gemini model rotation for the Remember job (Vault Librarian).

COPY, adapted, of the Daily Summary's adapters/youtube/scripts/summarize.py (ModelPool,
classify_error, short_reason, _make_config), taken 2026-09-26. The two apps keep separate
copies on purpose: when you change one, consider the same change in the other
(see README.md "Code kept in step with the Daily Summary").

Differences from the Daily Summary copy:
  * no dependency on the Daily Summary's _common.py: settings are the constants below;
  * forget_model() and the model-discovery cache use Remember's own files;
  * the usage ledger is Remember's copy (usage_ledger.py, project "obsidian_youtube").
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

from google import genai
from google.genai import types

try:
    from . import usage_ledger as U
except Exception:  # noqa: BLE001
    U = None

SAME_MODEL_RETRIES = 2
MAX_MODELS_PER_REQUEST = 3
RETRY_BACKOFF_SECONDS = 2.0
GEMINI_TIMEOUT_MS = 600_000
MODEL_CACHE: Path | None = None        # set by the job: <local_root>/cache/remember/model_cache.json
MODEL_CACHE_TTL_DAYS = 7
DEFAULT_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]


def make_client(api_key: str):
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS))


def _load(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def flash_models(api_key: str, lite_first: bool = False) -> list[str]:
    """Stable 'gemini-<n.n>-flash' models newest first, then '-flash-lite' ones (cached a week)."""
    cache = _load(MODEL_CACHE) if MODEL_CACHE else {}
    if cache.get("full") is not None and time.time() - cache.get("at", 0) < MODEL_CACHE_TTL_DAYS * 86400:
        full, lite = cache["full"], cache["lite"]
    else:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}&pageSize=200"
            with urllib.request.urlopen(url, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
            f, l = [], []
            for m in data.get("models", []):
                name = m.get("name", "").split("/")[-1]
                if "generateContent" not in m.get("supportedGenerationMethods", []):
                    continue
                mf = re.fullmatch(r"gemini-(\d+)(?:\.(\d+))?-flash", name)
                ml = re.fullmatch(r"gemini-(\d+)(?:\.(\d+))?-flash-lite", name)
                if mf:
                    f.append((int(mf.group(1)), int(mf.group(2) or 0), name))
                elif ml:
                    l.append((int(ml.group(1)), int(ml.group(2) or 0), name))
            full = [n for *_, n in sorted(f, reverse=True)]
            lite = [n for *_, n in sorted(l, reverse=True)]
            if MODEL_CACHE and (full or lite):
                MODEL_CACHE.parent.mkdir(parents=True, exist_ok=True)
                MODEL_CACHE.write_text(json.dumps({"full": full, "lite": lite, "at": time.time()}), encoding="utf-8")
        except Exception:  # noqa: BLE001
            full, lite = [DEFAULT_MODELS[0]], [DEFAULT_MODELS[1]]
    return (lite + full) if lite_first else (full + lite)


def forget_model(model: str) -> None:
    """Drop a retired model from the cached lists so later runs never try it."""
    if not MODEL_CACHE:
        return
    c = _load(MODEL_CACHE)
    changed = False
    for k in ("full", "lite"):
        if model in (c.get(k) or []):
            c[k] = [m for m in c[k] if m != model]
            changed = True
    if changed:
        MODEL_CACHE.write_text(json.dumps(c), encoding="utf-8")


class _QuotaExhausted(Exception):
    """All models are out of free-tier quota for today."""


class _EmptyResponse(Exception):
    """Model returned a response with no text (often a silent safety stop)."""


# --------------------------------------------------------------------------
# Failure classification
#
# Gemini failures fall into four buckets, and the right response differs:
#
#   quota       this model is out of requests for the day  -> permanently drop it
#               and move to the next model (each has its own quota)
#   dead_model  model retired / not available to this key   -> permanently drop it
#   fatal       the REQUEST is the problem (video private, oversized, blocked,
#               unsupported) -> every other model would reject it the same way,
#               so stop now instead of spending quota to be told twice
#   retryable   the SERVICE had a bad moment (503 overload, 500, timeout, socket
#               error) -> try again: same model first, then a different one
#
# Anything unrecognized is treated as retryable. One extra attempt is cheap; a
# silently missing summary is not.
# --------------------------------------------------------------------------

_FATAL_SIGNS = (
    "invalid_argument", "permission_denied", "failed_precondition",
    "unsupported", "not supported", "cannot be processed", "could not be processed",
    "video is private", "video unavailable", "video is unavailable",
    "unavailable in your region", "not available in your", "is not accessible", "is private",
    "prohibited_content", "blocked", "safety", "recitation",
    "too large", "exceeds the maximum", "context length", "token limit",
)

_RETRYABLE_SIGNS = (
    "503", "unavailable", "overloaded", "high demand", "try again later",
    "500", "internal error", "internal server",
    "504", "deadline", "timeout", "timed out",
    "connection", "reset by peer", "broken pipe", "eof occurred", "ssl",
    "aborted", "temporarily",
)


def _is_quota(e: Exception) -> bool:
    m = str(e).lower()
    return "resource_exhausted" in m or "429" in m or ("quota" in m and "exceed" in m)


def classify_error(e: Exception) -> str:
    """Return one of: 'quota', 'dead_model', 'fatal', 'retryable'."""
    if isinstance(e, _EmptyResponse):
        return "retryable"
    m = str(e).lower()
    if _is_quota(e):
        return "quota"
    # Input-fatal is checked BEFORE dead-model: an unusable video and a retired
    # model can both surface as 404 / NOT_FOUND, and rotating eight models over a
    # video that no model can read is pure waste.
    if any(s in m for s in _FATAL_SIGNS):
        return "fatal"
    if "no longer available" in m:
        return "dead_model"
    if "not_found" in m or "404" in m:
        # "models/gemini-x is not found" = retired model; anything else = the video
        return "dead_model" if "model" in m else "fatal"
    if any(s in m for s in _RETRYABLE_SIGNS):
        return "retryable"
    return "retryable"


# Longest / most specific signatures first -- the first hit wins.
_REASON_SIGNS = (
    # the video itself
    ("video unavailable", "video unavailable"),
    ("video is unavailable", "video unavailable"),
    ("unavailable in your region", "video unavailable"),
    ("not available in your", "video unavailable"),
    ("video is private", "video not accessible"),
    ("is private", "video not accessible"),
    ("is not accessible", "video not accessible"),
    ("permission_denied", "video not accessible"),
    ("prohibited_content", "blocked by content filter"),
    ("recitation", "blocked for recitation"),
    ("safety", "blocked by content filter"),
    ("blocked", "blocked by content filter"),
    ("exceeds the maximum", "video too large for the model"),
    ("context length", "video too large for the model"),
    ("token limit", "video too large for the model"),
    ("too large", "video too large for the model"),
    ("unsupported", "video format not supported"),
    ("not supported", "video format not supported"),
    ("invalid_argument", "video rejected by Gemini"),
    # the service
    ("high demand", "Gemini overloaded"),
    ("overloaded", "Gemini overloaded"),
    ("503", "Gemini overloaded"),
    ("deadline", "Gemini timed out"),
    ("timed out", "Gemini timed out"),
    ("timeout", "Gemini timed out"),
    ("504", "Gemini timed out"),
    ("internal", "Gemini internal error"),
    ("500", "Gemini internal error"),
    ("unavailable", "Gemini unavailable"),
    ("reset by peer", "network error"),
    ("connection", "network error"),
    ("ssl", "network error"),
    ("not_found", "video unavailable"),
)


def short_reason(e: Exception) -> str:
    """A few plain words for the report line: 'video summary failed - <this>'."""
    if isinstance(e, _EmptyResponse):
        return "Gemini returned nothing"
    if isinstance(e, _QuotaExhausted) or _is_quota(e):
        return "Gemini daily quota reached"
    if classify_error(e) == "dead_model":
        return "Gemini model retired"
    m = str(e).lower()
    for sign, text in _REASON_SIGNS:
        if sign in m:
            return text
    return type(e).__name__


def _make_config(low_res: bool):
    kw = {"temperature": 0.3}
    if low_res:
        try:
            kw["media_resolution"] = types.MediaResolution.MEDIA_RESOLUTION_LOW
        except Exception:
            pass
    return types.GenerateContentConfig(**kw)


class ModelPool:
    """Rotates across flash models, with two different kinds of rotation.

    PERMANENT -- a model that is out of daily quota or retired is dropped for the
    rest of the run: self.i advances past it and it lands in self.exhausted.

    PER REQUEST -- a model that is merely overloaded *right now* is retried a
    couple of times, and then this ONE request walks ahead to the next models
    WITHOUT moving self.i. An overload clears in seconds (the run that prompted
    this had a video fail on gemini-3.5-flash and the very next video succeed on
    it), so one unlucky video must not cost the whole run its best model.

    A 'fatal' error -- the request itself is unusable -- is raised immediately;
    no other model would answer it any differently.
    """

    def __init__(self, client, models: list[str],
                 same_model_retries: int | None = None,
                 max_models_per_request: int | None = None,
                 project: str | None = None, source: str = "",
                 minute_backoff: bool = False):
        self.client = client
        # Usage ledger (digest-core/gemini_usage.py): which Google AI project this pool's key
        # belongs to ("youtube", "podcast_paid", ...) and who is calling. None = don't record.
        self.project = project
        self.source = source
        # True: a per-MINUTE 429 waits the server's retryDelay and retries the same model
        # instead of retiring it for the run (used by the vault-librarian Remember job).
        self.minute_backoff = minute_backoff
        self.models = models
        self.i = 0
        self.exhausted: set[str] = set()
        self.last_used: str | None = None      # model of the most recent attempt
        self.last_usage = None                 # usage_metadata of the last success
        self.models_tried = 0                  # distinct models used by the last generate()
        self.same_model_retries = (
            SAME_MODEL_RETRIES if same_model_retries is None else same_model_retries)
        self.max_models_per_request = (
            MAX_MODELS_PER_REQUEST if max_models_per_request is None else max_models_per_request)

    def _note(self, model: str, ok: bool = True) -> None:
        if U is not None and self.project:
            U.record(self.project, self.source, model, ok=ok)

    @property
    def current(self):
        return self.models[self.i] if self.i < len(self.models) else None

    def _retire(self, model: str, j: int) -> None:
        """Drop a model for the rest of the run (out of quota, or retired)."""
        self.exhausted.add(model)
        if j == self.i:
            while self.i < len(self.models) and self.models[self.i] in self.exhausted:
                self.i += 1

    def generate(self, contents, config):
        last_err: Exception | None = None
        transient_err: Exception | None = None   # an overload/timeout seen on a live model
        self.models_tried = 0
        j = self.i
        while j < len(self.models) and self.models_tried < self.max_models_per_request:
            model = self.models[j]
            if model in self.exhausted:
                j += 1
                continue
            self.models_tried += 1
            for attempt in range(self.same_model_retries + 1):
                self.last_used = model
                try:
                    resp = self.client.models.generate_content(
                        model=model, contents=contents, config=config)
                    txt = (resp.text or "").strip()
                    if txt:
                        self.last_usage = getattr(resp, "usage_metadata", None)
                        self._note(model)
                        return txt
                    self._note(model, ok=False)
                    last_err = _EmptyResponse(f"empty response from {model}")
                    break                      # no text -- a different model may do better
                except Exception as e:         # noqa: BLE001
                    last_err = e
                    kind = classify_error(e)
                    if kind == "quota" and U is not None:
                        scope = U.quota_scope(e)
                        U.note_429(self.project, model, e, self.source)
                        if scope == "day" and self.project:
                            U.mark_exhausted(self.project, model, self.source)
                        if (self.minute_backoff and scope == "minute"
                                and attempt < self.same_model_retries):
                            time.sleep(U.retry_delay_seconds(e))
                            continue           # per-minute limit only -- same model again
                    elif kind != "dead_model":
                        self._note(model, ok=False)
                    if kind == "fatal":
                        raise                  # every model would reject this same request
                    if kind in ("quota", "dead_model"):
                        self._retire(model, j)
                        if kind == "dead_model":
                            forget_model(model)   # never try it again, any run
                        # An out-of-quota or retired model never actually looked at the
                        # video, so it doesn't use up one of the video's model tries.
                        self.models_tried -= 1
                        break                  # this model is done for the run
                    transient_err = e
                    if attempt < self.same_model_retries:
                        time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                        continue               # transient blip -- same model again
                    break                      # retries spent -- try a different model
            j = max(j + 1, self.i)
        if self.i >= len(self.models):
            raise _QuotaExhausted()
        # A live model was only overloaded: report that (retryable -> end-of-run retry),
        # not the quota/retired error from whichever model happened to be tried last.
        if transient_err is not None and last_err is not None and classify_error(last_err) in ("quota", "dead_model"):
            raise transient_err
        if last_err is not None:
            raise last_err
        raise _QuotaExhausted()

QuotaExhausted = _QuotaExhausted
EmptyResponse = _EmptyResponse
