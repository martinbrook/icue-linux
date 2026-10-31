"""Spotify "like" against a fake Spotify (accounts + Web API) and a fake playerctl.
   python tests/spotify_test.py"""
import base64, hashlib, json, os, shutil, stat, sys, threading, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
HERE = Path(__file__).resolve().parent
CFG = HERE / ".cfg-spotify"
shutil.rmtree(CFG, ignore_errors=True); (CFG / "bin").mkdir(parents=True)
os.environ["XDG_CONFIG_HOME"] = str(CFG)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
sys.path.insert(0, str(HERE.parent))
from icuehost import spotify_api as sp

# ---- fake Spotify: behaviour is driven by `fake`, requests are logged in `seen`
fake = {"library": 200, "token": 200, "rotate": False, "challenge": ""}
seen = []


class Fake(BaseHTTPRequestHandler):
    def _reply(self, code, body=b""):
        self.send_response(code); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
        seen.append(("token", form))
        if fake["token"] != 200:
            return self._reply(fake["token"], b'{"error":"invalid_grant"}')
        if form["grant_type"] == "authorization_code":
            ok = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
            if ok != fake["challenge"] or form["code"] != "CODE":
                return self._reply(400, b'{"error":"invalid_grant"}')
        t = {"access_token": f"AT{len(seen)}", "expires_in": 3600}
        if form["grant_type"] == "authorization_code" or fake["rotate"]:
            t["refresh_token"] = f"RT{len(seen)}"
        self._reply(200, json.dumps(t).encode())

    def do_GET(self):
        self._reply(200, b'{"id": "u1", "display_name": "Desk Owner"}') if self.path == "/v1/me" else self._reply(404)

    def do_PUT(self):
        u = urllib.parse.urlparse(self.path)
        seen.append(("put", u.path, dict(urllib.parse.parse_qsl(u.query)), self.headers["Authorization"]))
        code = fake["library"].pop(0) if isinstance(fake["library"], list) else fake["library"]
        self._reply(code)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
sp.ACCOUNTS, sp.API = base, base + "/v1"

fails = 0
def check(c, m):
    global fails; print(("PASS " if c else "FAIL ") + m); fails += (not c)

# ---- track ids from MPRIS
check(sp.track_uri("/com/spotify/track/6bIDoSkbA1MEqfQmhUUyas") == "spotify:track:6bIDoSkbA1MEqfQmhUUyas", "MPRIS object-path track id")
check(sp.track_uri("spotify:track:6bIDoSkbA1MEqfQmhUUyas") == "spotify:track:6bIDoSkbA1MEqfQmhUUyas", "spotify: URI track id")
for bad in ("", "/com/spotify/ad/123", "spotify:episode:abc", "spotify:local:a:b:c:180",
            "/com/spotify/track/", "/com/spotify/track/abc&uris=spotify:album:x"):
    check(sp.track_uri(bad) is None, f"rejected {bad!r}")

# ---- not logged in
ok, msg = sp.save("spotify:track:A")
check(not ok and "spotify-login" in msg, f"not logged in: {msg}")

# ---- login (PKCE); the "browser" checks the URL, tries a forged state, then completes
browser = {}
def fake_browser(url):
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    browser.update(q); fake["challenge"] = q["code_challenge"]
    def go():
        try:
            urllib.request.urlopen(f"{sp.REDIRECT}?code=EVIL&state=forged", timeout=5)
        except urllib.error.HTTPError as e:
            browser["forged"] = e.code
        urllib.request.urlopen(f"{sp.REDIRECT}?code=CODE&state={q['state']}", timeout=5).read()
    threading.Thread(target=go, daemon=True).start()
    return True
sp.webbrowser.open = fake_browser
import contextlib, io
out = io.StringIO()
with contextlib.redirect_stdout(out):
    sp.login("CLIENT")
check(browser["redirect_uri"] == "http://127.0.0.1:8898/callback" and browser["scope"] == "user-library-modify"
      and browser["code_challenge_method"] == "S256" and "client_secret" not in browser, "authorize URL is PKCE, loopback, minimal scope")
check(browser.get("show_dialog") == "true", "login always shows Spotify's account/consent page")
check("Logged in as Desk Owner" in out.getvalue(), "login says which account likes will go to")
check(browser.get("forged") == 404, "callback with a forged state is refused")
auth = json.loads(sp.AUTH_FILE.read_text())
check(auth == {"client_id": "CLIENT", "refresh_token": "RT1"}, f"refresh token saved {auth}")
check(stat.S_IMODE(sp.AUTH_FILE.stat().st_mode) == 0o600, "token file is private (0600)")

# ---- like
seen.clear()
ok, msg = sp.save("spotify:track:A")
puts = [s for s in seen if s[0] == "put"]
check(ok and puts and puts[0][1] == "/v1/me/library" and puts[0][2] == {"uris": "spotify:track:A"}
      and puts[0][3].startswith("Bearer AT"), f"PUT /me/library?uris=… with bearer token ({ok}, {msg})")
seen.clear(); sp.save("spotify:track:B")
check(not [s for s in seen if s[0] == "token"], "access token reused, not refreshed every press")

sp._token.clear(); fake["rotate"] = True
sp.save("spotify:track:C"); fake["rotate"] = False
check(json.loads(sp.AUTH_FILE.read_text())["refresh_token"] != "RT1", "rotated refresh token is saved")

seen.clear(); fake["library"] = [401, 200]
ok, msg = sp.save("spotify:track:D")
check(ok and len([s for s in seen if s[0] == "token"]) == 1, "401 refreshes the token once and retries")

fake["library"] = 403
ok, msg = sp.save("spotify:track:E")
check(not ok and "Premium" in msg, f"403 explained: {msg}")

fake["library"] = [401, 401]
ok, msg = sp.save("spotify:track:F")
check(not ok, f"repeated 401 gives up: {msg}")

fake["library"] = 200; sp._token.clear(); fake["token"] = 400
ok, msg = sp.save("spotify:track:G")
check(not ok and "expired" in msg, f"revoked login explained: {msg}")
fake["token"] = 200

sp._token.clear(); sp.ACCOUNTS = "http://127.0.0.1:9"     # nothing listens on port 9
ok, msg = sp.save("spotify:track:H")
check(not ok and "unreachable" in msg, f"offline: {msg}")
sp.ACCOUNTS = base

# ---- deck button: playerctl -> track uri -> save
(CFG / "bin/playerctl").write_text('#!/bin/sh\ncat "$FAKE_TRACK" 2>/dev/null\n'); (CFG / "bin/playerctl").chmod(0o755)
os.environ["PATH"] = f"{CFG / 'bin'}:{os.environ['PATH']}"; os.environ["FAKE_TRACK"] = str(CFG / "track")
from icuehost.plugins.deck import DeckPlugin
deck = DeckPlugin(); d = {"settings": {}}
like = {"spotify": "like", "label": "Like", "icon": "heart"}
ok, msg = deck._media(d, like)
check(not ok and "isn't playing" in msg, f"nothing playing: {msg}")
(CFG / "track").write_text("/com/spotify/ad/77\n")
ok, msg = deck._media(d, like)
check(not ok and "ads" in msg, f"ad not liked: {msg}")
(CFG / "track").write_text("/com/spotify/track/6bIDoSkbA1MEqfQmhUUyas\n"); seen.clear()
ok, msg = deck._media(d, like)
check(ok and [s for s in seen if s[0] == "put"][0][2] == {"uris": "spotify:track:6bIDoSkbA1MEqfQmhUUyas"}, "deck button likes the playing track")
ok, msg = deck._media(d, {"media": "like"})
check(not ok and "spotify buttons only" in msg, f"media = \"like\" refused: {msg}")
seen.clear(); ok, msg = deck._media({"settings": {"dry_run": True}}, like)
check(ok and "dry-run" in msg and not seen, "dry run doesn't call Spotify")

srv.shutdown(); shutil.rmtree(CFG, ignore_errors=True)
print("\n" + ("ALL PASSED" if not fails else f"{fails} FAILED")); sys.exit(1 if fails else 0)
