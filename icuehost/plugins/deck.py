"""icuelinux.deck:Deck:1.0 — a Stream Deck-style button grid for the Edge.

Buttons are defined in a TOML file on this machine, by default
~/.config/icue-linux/deck.toml (created from the bundled example on first
use; other profiles live alongside as <name>.toml). The widget can only say
"button N on page P was pressed"; what that does is decided here, from the
file, so nothing shown on the display can make the host run arbitrary
commands.

Button actions (the first matching key wins):
  app     = "firefox"                 launch a desktop app (gtk-launch / .desktop id) or binary
  command = ["code", "~/src"]         run a program, no shell
  shell   = "notify-send hi && ..."   run through /bin/sh -c (explicit opt-in)
  url     = "https://..."             open with xdg-open (also files and folders)
  keys    = "ctrl+alt+t"              send a key chord; a list sends several in turn
  text    = "hello"                   type text
  spotify = "play-pause"              play-pause | play | pause | next | previous | stop |
                                      volume-up | volume-down | shuffle | repeat | now-playing
  media   = "play-pause"              same verbs, whichever MPRIS player is active
  page    = "Media"                   switch the deck to another page

Keystrokes are sent with xdotool (X11), wtype (wlroots/KDE Wayland) or
ydotool (any Wayland, needs ydotoold), chosen automatically or by
[deck] key_backend = "xdotool" | "wtype" | "ydotool".
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:              # Python 3.10
    import tomli as tomllib

from PySide6.QtCore import QFileSystemWatcher, QTimer, Signal, Slot

from .base import PluginBase

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "icue-linux"
EXAMPLE = Path(__file__).resolve().parent.parent / "deck.example.toml"
SPOTIFY_VERBS = {"play-pause", "play", "pause", "next", "previous", "stop", "volume-up",
                 "volume-down", "shuffle", "repeat", "now-playing"}

# ---------------------------------------------------------------- key names
ALIASES = {  # friendly name -> X keysym (xdotool / wtype)
    "ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt", "super": "super",
    "win": "super", "meta": "super", "cmd": "super", "altgr": "ISO_Level3_Shift",
    "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape", "tab": "Tab",
    "space": "space", "backspace": "BackSpace", "delete": "Delete", "del": "Delete",
    "insert": "Insert", "home": "Home", "end": "End", "pageup": "Prior", "pagedown": "Next",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right", "print": "Print",
    "printscreen": "Print", "menu": "Menu", "capslock": "Caps_Lock",
    "volumeup": "XF86AudioRaiseVolume", "volumedown": "XF86AudioLowerVolume",
    "mute": "XF86AudioMute", "micmute": "XF86AudioMicMute", "playpause": "XF86AudioPlay",
    "next": "XF86AudioNext", "prev": "XF86AudioPrev", "previous": "XF86AudioPrev",
    "brightnessup": "XF86MonBrightnessUp", "brightnessdown": "XF86MonBrightnessDown",
    "minus": "minus", "plus": "plus", "equal": "equal", "comma": "comma", "period": "period",
    "slash": "slash", "backslash": "backslash", "semicolon": "semicolon", "grave": "grave",
}
MODIFIERS = {"ctrl", "shift", "alt", "super", "ISO_Level3_Shift"}
# Linux input-event codes for ydotool >= 1.0
EVDEV = {"ctrl": 29, "shift": 42, "alt": 56, "super": 125, "ISO_Level3_Shift": 100,
         "Return": 28, "Escape": 1, "Tab": 15, "space": 57, "BackSpace": 14, "Delete": 111,
         "Insert": 110, "Home": 102, "End": 107, "Prior": 104, "Next": 109, "Up": 103,
         "Down": 108, "Left": 105, "Right": 106, "Print": 99, "Menu": 139, "Caps_Lock": 58,
         "XF86AudioRaiseVolume": 115, "XF86AudioLowerVolume": 114, "XF86AudioMute": 113,
         "XF86AudioMicMute": 248, "XF86AudioPlay": 164, "XF86AudioNext": 163,
         "XF86AudioPrev": 165, "XF86MonBrightnessUp": 225, "XF86MonBrightnessDown": 224,
         "minus": 12, "equal": 13, "plus": 13, "comma": 51, "period": 52, "slash": 53,
         "backslash": 43, "semicolon": 39, "grave": 41}
for i, c in enumerate("1234567890"):
    EVDEV[c] = 2 + i
for row, start in (("qwertyuiop", 16), ("asdfghjkl", 30), ("zxcvbnm", 44)):
    for i, c in enumerate(row):
        EVDEV[c] = start + i
for n in range(1, 13):
    EVDEV[f"F{n}"] = 58 + n if n <= 10 else (87 if n == 11 else 88)
for n in range(13, 25):
    EVDEV[f"F{n}"] = 183 + (n - 13)


def parse_chord(chord: str) -> list[str]:
    """'Ctrl+Shift+T' -> ['ctrl', 'shift', 't']; 'alt+F4' -> ['alt', 'F4']."""
    out = []
    for part in [p.strip() for p in chord.replace(" ", "").split("+") if p.strip()]:
        low = part.lower()
        if low in ALIASES:
            out.append(ALIASES[low])
        elif len(part) == 1:
            out.append(part.lower())
        elif low[0] == "f" and low[1:].isdigit():
            out.append("F" + low[1:])
        else:
            out.append(part)                     # raw keysym, e.g. XF86Calculator
    return out


def detect_backend() -> str:
    wayland = bool(os.environ.get("WAYLAND_DISPLAY")) or os.environ.get("XDG_SESSION_TYPE") == "wayland"
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if not wayland and shutil.which("xdotool"):
        return "xdotool"
    if wayland and "gnome" not in desktop and shutil.which("wtype"):
        return "wtype"                           # GNOME lacks the virtual-keyboard protocol
    if shutil.which("ydotool"):
        return "ydotool"
    if shutil.which("xdotool"):
        return "xdotool"                         # XWayland apps only
    return ""


def key_commands(backend: str, chord: list[str]) -> list[list[str]]:
    """Commands to send one chord. ydotool gets a legacy-syntax fallback."""
    mods = [k for k in chord if k in MODIFIERS]
    keys = [k for k in chord if k not in MODIFIERS]
    if backend == "xdotool":
        return [["xdotool", "key", "--clearmodifiers", "+".join(chord)]]
    if backend == "wtype":
        cmd = ["wtype"]
        for m in mods:
            cmd += ["-M", "logo" if m == "super" else m]
        for k in keys:
            cmd += ["-k", k]
        for m in reversed(mods):
            cmd += ["-m", "logo" if m == "super" else m]
        return [cmd]
    if backend == "ydotool":
        codes = [EVDEV.get(k) for k in chord]
        primary = []
        if all(c is not None for c in codes):
            primary = [["ydotool", "key", *[f"{c}:1" for c in codes], *[f"{c}:0" for c in reversed(codes)]]]
        legacy = [["ydotool", "key", "+".join(chord)]]   # ydotool 0.1.x (Ubuntu's package)
        return primary + legacy
    return []


def text_command(backend: str, text: str) -> list[str]:
    return {"xdotool": ["xdotool", "type", "--clearmodifiers", "--delay", "8", text],
            "wtype": ["wtype", text],
            "ydotool": ["ydotool", "type", text]}.get(backend, [])


# ------------------------------------------------------------------ plugin
class DeckPlugin(PluginBase):
    MODULE = "icuelinux.deck"

    deckChanged = Signal(str, str)        # profile, deck JSON (no commands)
    pressed = Signal(str, str)            # profile, JSON {page, index, ok, message}
    mediaChanged = Signal(str)            # JSON {player, status, title, artist, art, running}
    pageRequested = Signal(str, str)      # profile, page name

    def __init__(self, parent=None):
        super().__init__(parent)
        self.decks: dict[str, dict] = {}
        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self._file_changed)
        self.media = {}
        self.media_timer = QTimer(self)
        self.media_timer.setInterval(1000)
        self.media_timer.timeout.connect(self._poll_media)
        self.log: list[list[str]] = []            # dry-run record (tests, debugging)

    # ------------------------------------------------------------ config
    def _path(self, profile: str) -> Path:
        name = "".join(c for c in (profile or "deck") if c.isalnum() or c in "-_") or "deck"
        return CONFIG_DIR / f"{name}.toml"

    def _load(self, profile: str) -> dict:
        path = self._path(profile)
        if not path.exists() and EXAMPLE.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(EXAMPLE, path)
        try:
            cfg = tomllib.loads(path.read_text(encoding="utf-8"))
            err = ""
        except (OSError, tomllib.TOMLDecodeError) as e:
            cfg, err = self.decks.get(profile, {}).get("cfg", {}), f"{path.name}: {e}"
        if str(path) not in self.watcher.files() and path.exists():
            self.watcher.addPath(str(path))
        settings = cfg.get("deck", {})
        pages = cfg.get("page", [])
        self.decks[profile] = {"cfg": cfg, "path": path, "pages": pages, "settings": settings,
                               "backend": settings.get("key_backend") or detect_backend(),
                               "error": err}
        if any(self._is_media(b) for p in pages for b in p.get("button", [])):
            if not self.media_timer.isActive():
                self.media_timer.start()
                QTimer.singleShot(0, self._poll_media)
        return self.decks[profile]

    @staticmethod
    def _is_media(b: dict) -> bool:
        return "spotify" in b or "media" in b

    def _public(self, profile: str) -> str:
        d = self.decks[profile]
        pages = []
        for p in d["pages"]:
            btns = []
            for b in p.get("button", []):
                kind = next((k for k in ("app", "command", "shell", "url", "keys", "text",
                                         "spotify", "media", "page") if k in b), "none")
                btns.append({"label": b.get("label", ""), "icon": b.get("icon", ""),
                             "color": b.get("color", ""), "span": int(b.get("span", 1)),
                             "kind": kind,
                             "verb": b.get("spotify") or b.get("media") or "",
                             "player": "spotify" if "spotify" in b else ("any" if "media" in b else ""),
                             "target": b.get("page", "")})
            pages.append({"name": p.get("name", f"Page {len(pages) + 1}"),
                          "columns": p.get("columns"), "rows": p.get("rows"), "buttons": btns})
        return json.dumps({"profile": profile, "pages": pages, "error": d["error"],
                           "backend": d["backend"], "file": str(d["path"]),
                           "style": {k: d["settings"][k] for k in ("button_color", "radius", "labels")
                                     if k in d["settings"]}})

    def _file_changed(self, path: str):
        for profile, d in list(self.decks.items()):
            if str(d["path"]) == path:
                # editors replace files; re-add the watch after the rename settles
                QTimer.singleShot(150, lambda p=profile: (self._load(p),
                                                          self.deckChanged.emit(p, self._public(p))))

    # --------------------------------------------------------------- API
    @Slot(int, str)
    def getDeck(self, rid: int, profile: str):
        self._load(profile or "deck")
        self.respond(rid, self._public(profile or "deck"))

    @Slot(int, str, int, int)
    def press(self, rid: int, profile: str, page: int, index: int):
        profile = profile or "deck"
        d = self.decks.get(profile) or self._load(profile)
        try:
            btn = d["pages"][page]["button"][index]
        except (IndexError, KeyError, TypeError):
            return self._done(rid, profile, page, index, False, "no such button")
        ok, msg = self._run(d, btn, profile)
        self._done(rid, profile, page, index, ok, msg)

    def _done(self, rid, profile, page, index, ok, msg):
        result = json.dumps({"page": page, "index": index, "ok": ok, "message": msg})
        if rid >= 0:
            self.respond(rid, result)
        self.pressed.emit(profile, result)

    # ----------------------------------------------------------- actions
    def _dry(self, d) -> bool:
        return bool(d["settings"].get("dry_run")) or os.environ.get("ICUE_LINUX_DECK_DRYRUN") == "1"

    def _spawn(self, d, cmd: list[str], shell: bool = False) -> tuple[bool, str]:
        if self._dry(d):
            self.log.append(cmd)
            return True, "dry-run: " + (cmd[-1] if shell else shlex.join(cmd))
        try:
            subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             cwd=str(Path.home()))
            return True, ""
        except OSError as e:
            return False, f"{cmd[0]}: {e.strerror or e}"

    def _call(self, d, cmds: list[list[str]]) -> tuple[bool, str]:
        """Run quick commands synchronously, trying alternatives until one works."""
        if self._dry(d):
            self.log.append(cmds[0])
            return True, "dry-run: " + shlex.join(cmds[0])
        last = "nothing to run"
        for cmd in cmds:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
                if r.returncode == 0:
                    return True, ""
                last = (r.stderr or r.stdout).strip().splitlines()[-1:] or [f"exit {r.returncode}"]
                last = f"{cmd[0]}: {last[0]}"
            except FileNotFoundError:
                last = f"{cmd[0]} is not installed"
            except subprocess.TimeoutExpired:
                last = f"{cmd[0]} timed out"
        return False, last

    def _run(self, d, b: dict, profile: str) -> tuple[bool, str]:
        expand = lambda s: os.path.expandvars(os.path.expanduser(str(s)))   # noqa: E731
        if "app" in b:
            app = str(b["app"])
            desktop = app[:-8] if app.endswith(".desktop") else app
            if shutil.which("gtk-launch") and self._desktop_exists(desktop):
                return self._spawn(d, ["gtk-launch", desktop])
            return self._spawn(d, [expand(x) for x in shlex.split(app)])
        if "command" in b:
            cmd = b["command"]
            cmd = [expand(x) for x in (cmd if isinstance(cmd, list) else shlex.split(cmd))]
            return self._spawn(d, cmd)
        if "shell" in b:
            return self._spawn(d, ["/bin/sh", "-c", str(b["shell"])], shell=True)
        if "url" in b:
            return self._spawn(d, ["xdg-open", expand(b["url"])])
        if "keys" in b or "text" in b:
            backend = d["backend"]
            if not backend:
                return False, "no key tool: install xdotool (X11), wtype or ydotool (Wayland)"
            if "text" in b:
                return self._call(d, [text_command(backend, str(b["text"]))])
            chords = b["keys"] if isinstance(b["keys"], list) else [b["keys"]]
            for chord in chords:
                ok, msg = self._call(d, key_commands(backend, parse_chord(str(chord))))
                if not ok:
                    return ok, msg
            return True, ""
        if "spotify" in b or "media" in b:
            return self._media(d, b)
        if "page" in b:
            self.pageRequested.emit(profile, str(b["page"]))
            return True, ""
        return False, "button has no action"

    @staticmethod
    def _desktop_exists(name: str) -> bool:
        dirs = [Path.home() / ".local/share/applications"] + [
            Path(p) / "applications" for p in
            os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")]
        dirs.append(Path("/var/lib/flatpak/exports/share/applications"))
        dirs.append(Path.home() / ".local/share/flatpak/exports/share/applications")
        return any((p / f"{name}.desktop").exists() for p in dirs)

    # ------------------------------------------------------------- media
    def _media(self, d, b: dict) -> tuple[bool, str]:
        player = ["-p", "spotify"] if "spotify" in b else []
        verb = str(b.get("spotify") or b.get("media"))
        if verb == "now-playing":
            verb = "play-pause"
        if verb not in SPOTIFY_VERBS:
            return False, f"unknown media action {verb!r}"
        if "spotify" in b and not self.media.get("running") and verb in ("play-pause", "play"):
            # Spotify closed: start it instead
            launch = d["settings"].get("spotify_command", "spotify")
            return self._spawn(d, shlex.split(launch))
        args = {"volume-up": ["volume", "0.05+"], "volume-down": ["volume", "0.05-"],
                "shuffle": ["shuffle", "Toggle"], "repeat": ["loop", "Playlist"
                                                              if self.media.get("loop") == "None" else "None"]
                }.get(verb, [verb])
        ok, msg = self._call(d, [["playerctl", *player, *args]])
        QTimer.singleShot(250, self._poll_media)
        return ok, msg

    def _poll_media(self):
        if not shutil.which("playerctl"):
            info = {"running": False, "error": "playerctl is not installed"}
        else:
            fmt = "{{status}}\t{{title}}\t{{artist}}\t{{mpris:artUrl}}\t{{playerName}}\t{{loop}}\t{{shuffle}}"
            info = {"running": False}
            for name, args in (("spotify", ["-p", "spotify"]), ("any", [])):
                try:
                    out = subprocess.run(["playerctl", *args, "metadata", "--format", fmt],
                                         capture_output=True, text=True, timeout=1).stdout.strip()
                except (OSError, subprocess.TimeoutExpired):
                    out = ""
                parts = out.split("\t")
                if len(parts) >= 5 and parts[0]:
                    info[name] = {"status": parts[0], "title": parts[1], "artist": parts[2],
                                  "art": parts[3], "player": parts[4],
                                  "loop": parts[5] if len(parts) > 5 else "",
                                  "shuffle": parts[6] if len(parts) > 6 else ""}
            info["running"] = "spotify" in info
            if info["running"]:
                info["loop"] = info["spotify"].get("loop", "")
        if info != self.media:
            self.media = info
            self.mediaChanged.emit(json.dumps(info))

    @Slot(int)
    def getMedia(self, rid: int):
        self.respond(rid, json.dumps(self.media))
