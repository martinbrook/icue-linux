"""Spotify Web API, just enough to save the playing track to Liked Songs and
tell whether it's already there.

MPRIS (playerctl) has no "like", so this uses the Web API with a PKCE login:
no client secret, only a client ID from your own app at developer.spotify.com
(Development Mode apps need the owner to have Premium). `icue-linux
spotify-login CLIENT_ID` stores a refresh token in CONFIG_DIR/spotify.json
(mode 0600); access tokens are refreshed from it as needed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "icue-linux"
AUTH_FILE = CONFIG_DIR / "spotify.json"
ACCOUNTS = "https://accounts.spotify.com"
API = "https://api.spotify.com/v1"
PORT = 8898
REDIRECT = f"http://127.0.0.1:{PORT}/callback"     # register exactly this in the Spotify app
SCOPE = "user-library-modify user-library-read"     # save, and show whether it's saved

_token: dict = {}                                   # {"access": str, "expires": monotonic}
_lock = threading.Lock()                            # liked-checks run on worker threads


def _post_token(form: dict) -> dict:
    req = urllib.request.Request(f"{ACCOUNTS}/api/token", data=urllib.parse.urlencode(form).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def _save_auth(auth: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(auth, f)


def _access_token() -> str:
    with _lock:
        return _refresh() if _token.get("expires", 0) <= time.monotonic() else _token["access"]


def _refresh() -> str:
    auth = json.loads(AUTH_FILE.read_text())
    t = _post_token({"grant_type": "refresh_token", "refresh_token": auth["refresh_token"],
                     "client_id": auth["client_id"]})
    if t.get("refresh_token") and t["refresh_token"] != auth["refresh_token"]:
        _save_auth({**auth, "refresh_token": t["refresh_token"]})      # Spotify may rotate it
    _token.update(access=t["access_token"], expires=time.monotonic() + int(t.get("expires_in", 3600)) - 60)
    return _token["access"]


def track_uri(trackid: str) -> str | None:
    """MPRIS trackid (/com/spotify/track/ID or spotify:track:ID) -> spotify:track:ID.
    None for ads, podcast episodes and local files."""
    for prefix in ("/com/spotify/track/", "spotify:track:"):
        if trackid.startswith(prefix):
            tid = trackid[len(prefix):]
            return f"spotify:track:{tid}" if tid.isalnum() else None
    return None


def save(uri: str) -> tuple[bool, str]:
    """Add uri to the user's library (Liked Songs for a track)."""
    if not AUTH_FILE.exists():
        return False, "run  icue-linux spotify-login CLIENT_ID  first"
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(f"{API}/me/library?" + urllib.parse.urlencode({"uris": uri}),
                                         method="PUT", headers={"Authorization": f"Bearer {_access_token()}"})
            urllib.request.urlopen(req, timeout=5).close()
            return True, ""
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 1:          # token revoked or expired early
                _token.clear()
                continue
            if e.code == 400 and str(e.filename).startswith(ACCOUNTS):
                return False, "Spotify login expired: run  icue-linux spotify-login CLIENT_ID"
            if e.code == 403:
                return False, "Spotify refused (403): the app owner needs Premium and you must be an allowed user"
            return False, f"Spotify error {e.code}"
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            return False, f"Spotify unreachable: {getattr(e, 'reason', e)}"
    return False, "Spotify rejected the login"


def contains(uri: str) -> bool | None:
    """Whether uri is in the user's library; None when unknown (not logged in,
    a login from before read access was asked for, offline)."""
    if not AUTH_FILE.exists():
        return None
    try:
        req = urllib.request.Request(f"{API}/me/library/contains?" + urllib.parse.urlencode({"uris": uri}),
                                     headers={"Authorization": f"Bearer {_access_token()}"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return bool(json.load(r)[0])
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _token.clear()
        return None
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def login(client_id: str) -> None:
    """One-time PKCE login in the browser; stores the refresh token."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = f"{ACCOUNTS}/authorize?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPE,
        "code_challenge_method": "S256", "code_challenge": challenge, "state": state,
        "show_dialog": "true"})                     # show the account, don't silently reuse the browser's
    got: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if urllib.parse.urlparse(self.path).path != "/callback" or q.get("state") != state:
                self.send_error(404)
                return
            got.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"icue-linux: done, you can close this tab.")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Opening Spotify login. If no browser appears, open:\n  {url}")
    webbrowser.open(url)
    while not got:
        srv.handle_request()
    srv.server_close()
    if "code" not in got:
        raise SystemExit(f"Spotify login failed: {got.get('error', 'no code returned')}")
    t = _post_token({"grant_type": "authorization_code", "code": got["code"], "redirect_uri": REDIRECT,
                     "client_id": client_id, "code_verifier": verifier})
    _save_auth({"client_id": client_id, "refresh_token": t["refresh_token"]})
    req = urllib.request.Request(f"{API}/me", headers={"Authorization": f"Bearer {t['access_token']}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            me = json.load(r)
        who = me.get("display_name") or me.get("id")
    except (urllib.error.URLError, OSError, ValueError):
        who = "(unknown account)"
    print(f"Logged in as {who}; likes go to this account's Liked Songs. Token saved to {AUTH_FILE}")
