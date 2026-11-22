"""YouTube Data API for the Remember job: its own OAuth sign-in and playlist calls.

Sign-in: OAuth client "Remember job" in Google Cloud project "Obsidian Youtube Summary",
scope youtube.force-ssl (needed to add/delete playlist items). One-time consent:
setup\\remember_auth.py. Files (this PC only):
    %LOCALAPPDATA%\\VaultLibrarian\\credentials\\youtube_client_secret.json
    %LOCALAPPDATA%\\VaultLibrarian\\credentials\\youtube_token.dpapi   (DPAPI-encrypted JSON)
API quota (per Cloud project, 10,000 units/day): list = 1 unit/page, insert/delete = 50.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import dpapi

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
CRED = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "VaultLibrarian" / "credentials"
CLIENT_SECRET = CRED / "youtube_client_secret.json"
TOKEN = CRED / "youtube_token.dpapi"
PLAYLIST_TITLE = "Remember"


class AuthError(Exception):
    pass


def save_creds(creds) -> None:
    CRED.mkdir(parents=True, exist_ok=True)
    TOKEN.write_bytes(dpapi.protect(creds.to_json().encode("utf-8")))


def load_creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    if not TOKEN.exists():
        raise AuthError(f"No YouTube sign-in yet ({TOKEN.name}); run setup\\remember_auth.py once.")
    info = json.loads(dpapi.unprotect(TOKEN.read_bytes()).decode("utf-8"))
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise AuthError("YouTube sign-in has no refresh token; run setup\\remember_auth.py again.")
        try:
            creds.refresh(Request())
        except Exception as e:  # noqa: BLE001
            raise AuthError(f"YouTube sign-in could not be refreshed ({e}); run setup\\remember_auth.py again.")
        save_creds(creds)
    return creds


def service(creds=None):
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=creds or load_creds(), cache_discovery=False)


def find_playlist(yt, title: str = PLAYLIST_TITLE) -> str | None:
    tok = None
    while True:
        r = yt.playlists().list(part="snippet", mine=True, maxResults=50, pageToken=tok).execute()
        for p in r.get("items", []):
            if p["snippet"]["title"].strip().lower() == title.lower():
                return p["id"]
        tok = r.get("nextPageToken")
        if not tok:
            return None


def playlist_items(yt, playlist_id: str) -> list[dict]:
    """[{item_id, video_id, added, title, position}] in playlist order."""
    out, tok = [], None
    while True:
        r = yt.playlistItems().list(part="snippet,contentDetails", playlistId=playlist_id,
                                    maxResults=50, pageToken=tok).execute()
        for it in r.get("items", []):
            sn = it.get("snippet", {})
            out.append({"item_id": it["id"], "video_id": it["contentDetails"]["videoId"],
                        "added": sn.get("publishedAt"), "title": sn.get("title"),
                        "position": sn.get("position")})
        tok = r.get("nextPageToken")
        if not tok:
            return out


def add_to_playlist(yt, playlist_id: str, video_id: str) -> str:
    r = yt.playlistItems().insert(part="snippet", body={"snippet": {
        "playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}).execute()
    return r["id"]


def delete_item(yt, item_id: str) -> None:
    yt.playlistItems().delete(id=item_id).execute()


_DUR = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def iso_seconds(iso: str | None) -> int:
    m = _DUR.fullmatch(iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(g or 0) for g in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def video_meta(yt, ids: list[str]) -> dict[str, dict]:
    """videos.list for up to 50 ids at a time -> {id: meta}. Missing ids = private/removed."""
    out = {}
    for i in range(0, len(ids), 50):
        r = yt.videos().list(part="snippet,contentDetails,status", id=",".join(ids[i:i + 50])).execute()
        for v in r.get("items", []):
            sn, cd, st = v.get("snippet", {}), v.get("contentDetails", {}), v.get("status", {})
            out[v["id"]] = {
                "video_id": v["id"], "title": sn.get("title"), "channel": sn.get("channelTitle"),
                "channel_id": sn.get("channelId"), "published": sn.get("publishedAt"),
                "description": sn.get("description") or "", "tags": sn.get("tags") or [],
                "language": sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
                "duration_sec": iso_seconds(cd.get("duration")), "caption": cd.get("caption") == "true",
                "privacy": st.get("privacyStatus"), "embeddable": st.get("embeddable"),
                "live": sn.get("liveBroadcastContent"),
            }
    return out


_CHAPTER = re.compile(r"^\s*[\(\[]?((?:\d{1,2}:)?\d{1,2}:\d{2})[\)\]]?\s*[-–—:|]?\s*(.+?)\s*$")


def chapters(description: str) -> list[dict]:
    """YouTube chapters from the description: needs >= 3 timestamps starting at 0:00."""
    out = []
    for line in (description or "").splitlines():
        m = _CHAPTER.match(line)
        if m:
            secs = 0
            for p in m.group(1).split(":"):
                secs = secs * 60 + int(p)
            out.append({"start": secs, "title": m.group(2)})
    if len(out) < 3 or out[0]["start"] != 0:
        return []
    return out


def description_links(description: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"https?://[^\s)>\]]+", description or "")))
