"""One-time YouTube sign-in for the Remember job. Run from YOUR OWN PowerShell on this PC
(not from a Claude session -- the Claude app redirects writes under AppData\\Local):

  & "$env:LOCALAPPDATA\\Programs\\VaultLibrarian\\runtime\\Scripts\\python.exe" "<app folder>\\setup\\remember_auth.py"

A browser opens: sign in with the Google account that owns the Remember playlist and allow
access (the app is unverified: click Advanced -> Go to Vault Librarian Remember). The token is
saved DPAPI-encrypted to %LOCALAPPDATA%\\VaultLibrarian\\credentials\\youtube_token.dpapi.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from remember import youtube as Y  # noqa: E402

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

if not Y.CLIENT_SECRET.exists():
    sys.exit(f"Missing {Y.CLIENT_SECRET} -- download the OAuth client JSON first.")
flow = InstalledAppFlow.from_client_secrets_file(str(Y.CLIENT_SECRET), Y.SCOPES)
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline",
                              authorization_prompt_message="Opening your browser to sign in to YouTube...",
                              success_message="Signed in. You can close this tab and return to PowerShell.")
Y.save_creds(creds)
yt = Y.service(Y.load_creds())
me = yt.channels().list(part="snippet", mine=True).execute().get("items", [{}])[0].get("snippet", {})
pid = Y.find_playlist(yt)
n = len(Y.playlist_items(yt, pid)) if pid else 0
print(f"Saved. Channel: {me.get('title')}; Remember playlist: {'found, ' + str(n) + ' videos' if pid else 'NOT FOUND'}.")
print(f"Refresh token stored: {'yes' if creds.refresh_token else 'NO - run again'}")
